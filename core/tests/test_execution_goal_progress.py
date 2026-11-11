"""Goal display projections use synthetic tools only; no DB or live control plane."""

import json
import tempfile
import unittest
from pathlib import Path

from helpers import CORE  # noqa: F401
from kanpilot import dsh_events
from kanpilot import pi_desktop_worker as worker
from test_execution_progress import ROW


def tool(name, arguments, *, output=None, turn="execution", **extra):
    return dict(type="tool_result", name=name, input=json.dumps(arguments),
                output=json.dumps(output if output is not None else
                                  {"details": {"ok": True, "revision": 1}}),
                turn_id=turn, ts="2026-11-12T01:00:00Z", **extra)


def submit(title="目标", **extra):
    return tool("SubmitGoal", {"markdown": "# " + title + "\n\n## 目标说明\n保留说明与验收。"}, **extra)


def update(status="pending", **extra):
    return tool("UpdateGoalProgress", {"items": [
        {"id": "one", "label": "做第一项", "status": status},
        {"id": "two", "label": "做第二项", "status": "failed"},
    ]}, **extra)


class GoalProgressTests(unittest.TestCase):
    def test_host_proposal_title_and_markdown_are_authoritative(self):
        event = tool("SubmitGoal", {"title": "请求标题", "markdown": "# Goal"},
                     output={"details": {"proposal": {
                         "title": "宿主目标名称", "markdown": "# 契约\n核对证据"}}})
        goal = dsh_events.latest_goal_progress([event])
        self.assertEqual(goal["title"], "宿主目标名称")
        self.assertEqual(goal["markdown"], "# 契约\n核对证据")

    def test_title_description_items_and_counts(self):
        goal = dsh_events.latest_goal_progress([submit(), update("completed")])
        self.assertEqual(goal["title"], "目标")
        self.assertIn("保留说明与验收。", goal["markdown"])
        self.assertEqual((goal["completed"], goal["total"]), (1, 2))
        self.assertEqual(goal["items"][1]["status"], "failed")
        self.assertEqual(goal["revision"], 1)
        self.assertEqual(goal["at"], "2026-11-12T01:00:00Z")

    def test_failed_or_unconfirmed_progress_does_not_replace_accepted_items(self):
        accepted = update("in_progress")
        bad = [update("completed", is_error=True),
               update("completed", output={"details": {"ok": False}}),
               update("completed", output={"details": {"revision": 2}}),
               dict(update("completed"), type="tool_call")]
        goal = dsh_events.latest_goal_progress([submit(), accepted] + bad)
        self.assertEqual(goal["items"][0]["status"], "in_progress")

    def test_failed_goal_does_not_reset_existing_goal(self):
        for bad in (submit("错误", is_error=True),
                    submit("错误", output={"details": {"ok": False}}),
                    submit("错误", output={"details": {"errorCode": "PLAN_SUBMIT_FAILED"}, "isError": True}),
                    submit("错误", output={"content": [{"text": "Validation failed: invalid goal"}]})):
            with self.subTest(bad=bad):
                goal = dsh_events.latest_goal_progress([submit(), update("completed"), bad])
                self.assertEqual(goal["title"], "目标")
                self.assertEqual(goal["completed"], 1)

    def test_new_goal_resets_old_items(self):
        goal = dsh_events.latest_goal_progress([submit("旧"), update("completed"), submit("新")])
        self.assertEqual((goal["title"], goal["items"], goal["total"]), ("新", [], 0))

    def test_invalid_checklists_do_not_replace_existing_items(self):
        bad_items = [None, {}, ["wrong"],
                     [{"id": "one", "label": "项", "status": "cancelled"}],
                     [{"label": "项", "status": "completed"}],
                     [{"id": "one", "label": "项", "status": "completed"}] * 2]
        for items in bad_items:
            with self.subTest(items=items):
                goal = dsh_events.latest_goal_progress([update("in_progress"),
                    tool("UpdateGoalProgress", {"items": items})])
                self.assertEqual(goal["items"][0]["status"], "in_progress")

    def test_empty_checklist_is_not_fabricated_as_completed(self):
        goal = dsh_events.latest_goal_progress([tool("UpdateGoalProgress", {"items": []})])
        self.assertEqual((goal["completed"], goal["total"], goal["items"]), (0, 0, []))

    def test_no_goal_or_status_only_events_return_none(self):
        self.assertIsNone(dsh_events.latest_goal_progress([]))
        self.assertIsNone(dsh_events.latest_goal_progress([
            {"type": "assistant_message", "content": "# 目标"},
            {"type": "session", "session_id": "s"},
            {"type": "terminal", "outcome": "completed"}]))

    def test_missing_contract_uses_approval_title(self):
        goal = dsh_events.latest_goal_progress([update()], {"goal_approval": {"title": "已批准目标"}})
        self.assertEqual(goal["title"], "已批准目标")
        self.assertEqual(goal["markdown"], "")

    def test_job_turn_limits_updates_to_linked_execution_and_adoptions(self):
        job = {"turn_id": "continued", "goal_approval": {
            "planning_turn_id": "planning", "execution_turn_id": "execution"},
            "turn_adopted": [{"from": "execution", "to": "continued"}]}
        events = [submit(turn="planning"), update("completed"),
                  submit("无关目标", turn="other"), update("pending", turn="other")]
        goal = dsh_events.latest_goal_progress(events, job)
        self.assertEqual((goal["title"], goal["completed"]), ("目标", 1))
        goal = dsh_events.latest_goal_progress(events + [update("in_progress", turn="continued")], job)
        self.assertEqual(goal["items"][0]["status"], "in_progress")

    def test_unrelated_turn_or_subagent_cannot_supply_goal(self):
        self.assertIsNone(dsh_events.latest_goal_progress([update(turn="other")], {"turn_id": "execution"}))
        self.assertIsNone(dsh_events.latest_goal_progress([update(parent_tool_call_id="sub")]))

    def test_source_revision_replaces_original_logical_position(self):
        events = [submit("初稿", source_message_id="goal", source_revision=1),
                  update("completed", source_message_id="progress", source_revision=1),
                  submit("修订稿", source_message_id="goal", source_revision=2),
                  update("pending", source_message_id="progress", source_revision=0)]
        goal = dsh_events.latest_goal_progress(events)
        self.assertEqual((goal["title"], goal["completed"]), ("修订稿", 1))

    def test_error_revision_removes_previously_accepted_tool(self):
        events = [submit(), update("completed", source_message_id="progress", source_revision=1),
                  update("completed", source_message_id="progress", source_revision=2,
                         output={"details": {"ok": False}})]
        goal = dsh_events.latest_goal_progress(events)
        self.assertEqual(goal["items"], [])

    def test_host_revision_cannot_regress(self):
        goal = dsh_events.latest_goal_progress([
            update("completed", output={"details": {"ok": True, "revision": 3}}), update()])
        self.assertEqual((goal["revision"], goal["completed"]), (3, 1))

    def test_round_detail_adds_display_only_goal(self):
        detail = dsh_events.round_detail(ROW, {"status": "running"}, [submit(), update()])
        self.assertEqual(detail["goal_progress"]["total"], 2)
        self.assertEqual(detail["run_status"], "running")
        self.assertIsNone(detail["terminal"])

    def test_collector_real_shape_tools_project_approved_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            log = worker._EventLog(Path(directory) / "events.jsonl")
            collector = worker._Collector(None, "session", log, "")
            collector.gate_open = True
            collector._emit({"id": "goal", "role": "tool", "toolName": "SubmitGoal",
                "toolCallId": "goal-call", "turnId": "planning", "toolStatus": "success",
                "timestamp": "2026-11-12T01:00:00Z",
                "toolArgs": {"title": "图一目标标题", "markdown": "# Goal\n\n完成实际目标。", "question": "开始？"},
                "toolResult": {"content": [{"type": "text", "text":
                    "Goal contract submitted for approval. Autonomous execution will begin only after approval."}],
                    "details": {"proposal": {"id": "proposal", "title": "图一目标标题"}}, "terminate": True}})
            collector._emit({"id": "update", "role": "tool", "toolName": "UpdateGoalProgress",
                "toolCallId": "progress-call", "turnId": "execution", "toolStatus": "success",
                "timestamp": "2026-11-12T01:01:00Z",
                "toolArgs": {"items": [{"id": "check", "label": "实际验证", "status": "completed"}]},
                "toolResult": {"content": [{"type": "text", "text":
                    "Goal progress updated successfully (revision 2, 1/1 completed)."}],
                    "details": {"ok": True, "revision": 2}}})
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
            job = {"turn_id": "execution", "goal_approval": {
                "planning_turn_id": "planning", "execution_turn_id": "execution", "status": "approved"}}
            goal = dsh_events.round_detail(ROW, job, events)["goal_progress"]
            self.assertEqual(goal["title"], "图一目标标题")
            self.assertEqual(goal["markdown"], "# Goal\n\n完成实际目标。")
            self.assertEqual(goal["items"], [{"id": "check", "label": "实际验证", "status": "completed"}])
            self.assertEqual((goal["completed"], goal["total"], goal["revision"]), (1, 1, 2))


if __name__ == "__main__":
    unittest.main()
