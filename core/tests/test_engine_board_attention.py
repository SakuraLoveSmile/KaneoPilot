"""A15: board responses carry the same attention inbox as `attention-list`.

overview / list-tasks gain an `attention` section that must equal the
`attention-list` command output field-for-field (single aggregation source).
Run: ``cd core && python3 -m pytest tests/test_engine_board_attention.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import db, util  # noqa: E402
from kanpilot.engine import attention  # noqa: E402


class TestBoardAttention(KanpilotTestCase):
    def _make_needs_user(self, task_id, failure_code="pr_closed"):
        info = self.make_ready_task(task_id=task_id)
        remote = info["remote"]
        self.set_task_status(
            remote["id"], "needs_user", failure_code=failure_code,
            failure_stage="delivery", failure_reason="closed",
            blocked_summary="closed unmerged",
            blocked_at="2026-10-10T00:00:00+00:00")
        return remote["id"]

    def test_overview_attention_equals_attention_list(self):
        self._make_needs_user("taskbrdovw00000000001")
        overview = self.call("overview", {})
        listing = self.call("attention-list", {})
        self.assertEqual(overview["attention"], listing)   # 逐字段相等
        self.assertEqual(len(overview["attention"]["items"]), 1)

    def test_list_tasks_attention_equals_attention_list(self):
        self._make_needs_user("taskbrdlist00000000001")
        listed = self.call("list-tasks", {})
        listing = self.call("attention-list", {})
        self.assertEqual(listed["attention"], listing)
        self.assertEqual(len(listed["attention"]["items"]), 1)

    def test_empty_inbox_consistent_across_all_three(self):
        overview = self.call("overview", {})
        listed = self.call("list-tasks", {})
        listing = self.call("attention-list", {})
        self.assertEqual(overview["attention"], {"items": []})
        self.assertEqual(listed["attention"], {"items": []})
        self.assertEqual(listing, {"items": []})

    def test_item_shape_is_contract(self):
        self._make_needs_user("taskbrdshape0000000001")
        item = self.call("overview", {})["attention"]["items"][0]
        self.assertEqual(set(item), set(attention.ITEM_FIELDS))

    def test_overview_backward_compatible(self):
        # existing overview fields must remain untouched.
        overview = self.call("overview", {})
        for key in ("paused", "counts", "last_tick_at", "schema_version",
                    "settings", "paths", "attention"):
            self.assertIn(key, overview)

    def test_list_tasks_backward_compatible(self):
        listed = self.call("list-tasks", {})
        self.assertIn("tasks", listed)
        self.assertIsInstance(listed["tasks"], list)


if __name__ == "__main__":
    unittest.main()
