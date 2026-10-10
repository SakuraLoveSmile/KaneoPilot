"""Integration test for the `attention-list` command (protocol §5.27.3).

DB-backed via the shared KanpilotTestCase harness. Run:
``cd core && python3 -m pytest tests/test_engine_attention_cmd.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot.engine import attention  # noqa: E402


class TestAttentionListCommand(KanpilotTestCase):
    def _make_needs_user(self, task_id, failure_code, stage="delivery", save_state=None):
        info = self.make_ready_task(task_id=task_id)
        remote = info["remote"]
        fields = {
            "failure_code": failure_code, "failure_stage": stage,
            "failure_reason": "reason", "blocked_summary": "summary",
            "blocked_at": "2026-10-10T00:00:00+00:00",
        }
        if save_state:
            fields["save_state"] = save_state
        self.set_task_status(remote["id"], "needs_user", **fields)
        return remote["id"]

    def test_empty_inbox(self):
        self.assertEqual(self.call("attention-list", {}), {"items": []})

    def test_item_shape_and_fields(self):
        tid = self._make_needs_user("taskattn00000000000001", "pr_closed")
        items = self.call("attention-list", {})["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(set(item), set(attention.ITEM_FIELDS))
        self.assertEqual(item["task_id"], tid)
        self.assertEqual(item["failure_code"], "pr_closed")
        self.assertEqual(item["failure_stage"], "delivery")
        self.assertEqual(item["attention_state"], attention.ACTIONABLE)
        self.assertIn("dismiss", item["allowed_actions"])
        # union (A15) fields
        self.assertEqual(item["reason_code"], "pr_closed_unmerged")
        self.assertEqual(item["attention_id"], f"attn_{tid}_pr_closed_unmerged")
        self.assertIn("recommended", item)
        self.assertIsInstance(item["alternatives"], list)
        self.assertIsInstance(item["evidence"], list)   # structured [{kind,id,at,detail}]
        self.assertEqual(item["since"], "2026-10-10T00:00:00+00:00")

    def test_orders_by_ladder_priority(self):
        hi = self._make_needs_user("taskattnhi0000000000001", "pr_closed")            # 4
        lo = self._make_needs_user("taskattnlo0000000000001", "verification_unconfirmed")  # 3
        items = self.call("attention-list", {})["items"]
        self.assertEqual([it["task_id"] for it in items], [hi, lo])

    def test_archived_only_with_historical(self):
        tid = self._make_needs_user("taskattnarc00000000001", "pr_closed", save_state="archived")
        self.assertEqual(self.call("attention-list", {})["items"], [])
        items = self.call("attention-list", {"include_historical": True})["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["task_id"], tid)
        self.assertEqual(items[0]["attention_state"], attention.HISTORICAL)

    def test_save_state_all_includes_archived(self):
        self._make_needs_user("taskattnall00000000001", "pr_closed", save_state="trashed")
        self.assertEqual(len(self.call("attention-list", {"save_state": "all"})["items"]), 1)

    def test_invalid_save_state_rejected(self):
        exc = self.call_err("attention-list", {"save_state": "bogus"})
        self.assertEqual(exc.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
