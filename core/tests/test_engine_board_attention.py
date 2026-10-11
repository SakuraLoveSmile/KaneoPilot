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

    def test_task_attention_is_same_item_and_phase_as_inbox(self):
        task_id = self._make_needs_user("taskbrdshared00000001")
        view = self.call("get-task", {"task_id": task_id})["task"]
        listed = self.call("list-tasks", {})["tasks"][0]
        item = self.call("attention-list", {})["items"][0]
        self.assertEqual(view["attention"], item)
        self.assertEqual(listed["attention"], item)
        self.assertEqual((view["phase"], view["condition"]), ("needs-you", "blocked"))
        self.set_task_status(task_id, "running", failure_code=None)
        view = self.call("get-task", {"task_id": task_id})["task"]
        self.assertIsNone(view["attention"])
        self.assertEqual((view["phase"], view["condition"]), ("observe", "running"))

    def test_project_engine_fields_round_trip(self):
        from kanpilot import constants
        project_id = next(iter(constants.PROJECTS.values()))
        self.call("set-project-engine-mode", {"project_id": project_id, "engine_mode": "shadow"})
        self.call("set-project-verify-env", {"project_id": project_id, "verify_env": {"KP_TEST": "yes"}})
        self.call("set-project-fallback-model", {"project_id": project_id,
            "fallback_model": {"provider_id": "fake", "model_id": "model"}})
        item = next(x for x in self.call("get-project-mappings", {})["items"]
                    if x["project_id"] == project_id)
        self.assertEqual(item["engine_mode"], "shadow")
        self.assertEqual(item["verify_env"], {"KP_TEST": "yes"})
        self.assertEqual(item["fallback_model"], {"provider_id": "fake", "model_id": "model"})

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
