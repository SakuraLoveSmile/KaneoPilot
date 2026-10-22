"""v2 测试（protocol §5.20–§5.22）：purge 终态、get-request/list-events 扩展、
手动规划链（fetch-todos → prepare-plan-input → start-plans → claim →
publish-plan）与 plan_execution_hold 门控、KANPILOT_CODEX_FAKE runner。"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import cli, constants, db, util  # noqa: E402


class TestPurge(KanpilotTestCase):
    """§5.20：purged 终态（回收站 → purge；历史/文件保留；不复活）。"""

    def _task(self, task_id="taskpurge00000000001"):
        task = self.fake.add_task(
            task_id, "fixture-project-feedback")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        return task

    def test_purge_requires_trashed(self):
        task = self._task()
        exc = self.call_err("purge-task", {"task_id": task["id"]})
        self.assertEqual(exc.code, "conflict")
        self.call("trash-task", {"task_id": task["id"]})
        result = self.call("purge-task", {"task_id": task["id"]})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        # 幂等：再次 purge 返回 deduplicated。
        again = self.call("purge-task", {"task_id": task["id"]})
        self.assertTrue(again["deduplicated"])

    def test_purged_excluded_from_lists_but_not_history(self):
        task = self._task()
        self.call("trash-task", {"task_id": task["id"]})
        self.call("purge-task", {"task_id": task["id"]})
        # 常规列表（active / all）都不含 purged。
        active = self.call("list-tasks", {})["tasks"]
        self.assertNotIn(task["id"], [t["task_id"] for t in active])
        all_tasks = self.call("list-tasks", {"save_state": "all"})["tasks"]
        self.assertNotIn(task["id"], [t["task_id"] for t in all_tasks])
        # save_state=purged 可回看。
        purged = self.call(
            "list-tasks", {"save_state": "purged"})["tasks"]
        self.assertIn(task["id"], [t["task_id"] for t in purged])
        # 历史保留：事件仍可查。
        events = self.call(
            "list-events", {"task_id": task["id"]})["events"]
        self.assertTrue(any(e["kind"] == "task_purged" for e in events))
        # 写入口拒绝（不复活）。
        exc = self.call_err("enqueue", {"task_id": task["id"]})
        self.assertEqual(exc.code, "conflict")
        exc = self.call_err(
            "resolve", {"task_id": task["id"], "action": "replan"})
        self.assertEqual(exc.code, "conflict")

    def test_purge_session(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        # 造一个轮次会话：claim plan（backend=codex）。
        request_id, _ = self.queue_plan(
            task_id, source_sha256=fixture["source_sha256"],
            requirements_sha256=fixture["requirements_sha256"])
        # 任务已 ready → plan guard 需要 needs_plan，先搬回去。
        self.set_task_status(task_id, "needs_plan")
        claim = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request_id})
        self.assertTrue(claim["claimed"])
        run_id = claim["run_id"]
        sessions = self.call(
            "list-sessions", {"task_id": task_id})["sessions"]
        self.assertTrue(any(s["run_id"] == run_id for s in sessions))
        # 活跃会话不可 purge；先 trash 再 purge。
        exc = self.call_err("purge-session", {
            "task_id": task_id, "run_id": run_id})
        self.assertEqual(exc.code, "conflict")
        # 规划轮次必须先显式结束，否则 _session_trash_reason 的
        # _plan_round_has_activity 守卫会以 conflict 阻止 trash（§5.16.2）。
        self.call("report", {"request_id": request_id, "run_id": run_id,
                             "outcome": "succeeded", "note": "fixture done"})
        self.call("trash-session", {"task_id": task_id, "run_id": run_id})
        result = self.call("purge-session", {
            "task_id": task_id, "run_id": run_id})
        self.assertTrue(result["ok"])
        # purged 会话退出常规列表。
        sessions = self.call(
            "list-sessions", {"task_id": task_id})["sessions"]
        self.assertNotIn(run_id, [s["run_id"] for s in sessions])
        purged = self.call("list-sessions", {
            "task_id": task_id, "save_state": "purged"})["sessions"]
        self.assertTrue(any(s["run_id"] == run_id for s in purged))


class TestRequestDetail(KanpilotTestCase):
    """§5.21.1 get-request：attempts / allowed_actions / attention_state。"""

    def test_get_request_fields(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.enqueue_dispatch(task_id)
        request = next(r for r in self.requests(status="queued")
                       if r["kind"] == "dispatch")
        detail = self.call(
            "get-request", {"request_id": request["request_id"]})
        self.assertEqual(detail["request"]["request_id"],
                         request["request_id"])
        self.assertIn("allowed_actions", detail)
        self.assertIn("attention_state", detail)
        self.assertIn("attempts", detail)
        self.assertEqual(detail["request"]["task_id"], task_id)

    def test_list_events_cursor_and_filters(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        all_events = self.call(
            "list-events", {"task_id": task_id})["events"]
        self.assertTrue(len(all_events) >= 2)
        # 游标分页：before_id 取第一页末条 id → 第二页更早。
        first = self.call("list-events", {"task_id": task_id, "limit": 2})
        self.assertLessEqual(len(first["events"]), 2)
        if first.get("next_before_id"):
            second = self.call("list-events", {
                "task_id": task_id,
                "before_id": first["next_before_id"]})
            self.assertTrue(
                all(e["id"] < first["next_before_id"]
                    for e in second["events"]))
        # 事件视图携带任务/项目标题字段（§5.21.3）。
        self.assertIn("task_title", all_events[0])
        self.assertIn("summary", all_events[0])


class TestPlanningChain(KanpilotTestCase):
    """§5.22：fetch-todos → prepare-plan-input → start-plans → claim →
    publish-plan；hold 门控。"""

    def _fetch_and_prepare(self, task_id="taskplan00000000000001",
                           title="规划甲"):
        remote_task = self.fake.add_task(
            task_id, "fixture-project-feedback",
            status="to-do", title=title)
        self.make_repo("Feedback")
        fetched = self.call("fetch-todos", {})
        self.assertIn("items", fetched)
        item = next(
            i for i in fetched["items"] if i["task_id"] == remote_task["id"])
        self.assertEqual(item["title"], "规划甲")
        self.assertEqual(item["project_name"], "Feedback")
        self.assertTrue(item["selectable"])
        # prepare-plan-input：远端重取 + 建不可变材料目录。
        prepared = self.call("prepare-plan-input", {
            "task_id": remote_task["id"],
            "expected_requirements_sha256": item["requirements_sha256"],
        })
        self.assertTrue(prepared["input_id"])
        self.assertTrue(prepared["input_sha256"])
        self.assertTrue(prepared["can_start"])
        self.assertTrue(Path(prepared["input_dir"]).is_dir())
        self.assertTrue(
            (Path(prepared["input_dir"]) / "manifest.json").is_file())
        self.assertTrue(
            (Path(prepared["input_dir"]) / "source.json").is_file())
        return prepared

    def test_fetch_todos_and_prepare(self):
        self._fetch_and_prepare()

    def test_prepare_conflict_on_requirements_change(self):
        remote_task = self.fake.add_task(
            "taskplan00000000000002", "fixture-project-feedback",
            status="to-do")
        self.make_repo("Feedback")
        fetched = self.call("fetch-todos", {})
        item = next(
            i for i in fetched["items"] if i["task_id"] == remote_task["id"])
        exc = self.call_err("prepare-plan-input", {
            "task_id": remote_task["id"],
            "expected_requirements_sha256": "bogus-sha",
        })
        self.assertEqual(exc.code, "conflict")

    def test_start_plans_and_full_plan_round(self):
        prepared = self._fetch_and_prepare()
        task_id = prepared["input_dir"] and json.loads(
            Path(prepared["input_dir"], "manifest.json").read_text()
        )["task"]["task_id"]
        started = self.call("start-plans", {
            "items": [{"input_id": prepared["input_id"],
                       "input_sha256": prepared["input_sha256"]}]})
        self.assertEqual(started["started"], 1)
        entry = started["results"][0]
        self.assertTrue(entry["request_id"])
        # 请求已授权 → claim 成功，任务转 planning。
        claim = self.call("claim", {
            "stage": "plan", "executor": constants.PLANNER_EXECUTOR_ID,
            "request_id": entry["request_id"]})
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["context"]["input_id"],
                         prepared["input_id"])
        self.assertTrue(claim["context"]["input_dir"])
        self.assertEqual(self.task(task_id)["status"], "planning")

    def test_start_plans_rejections(self):
        # 不存在的 input_id → not_found。
        result = self.call("start-plans", {
            "items": [{"input_id": "missing", "input_sha256": "x"}]})
        self.assertEqual(result["started"], 0)
        self.assertEqual(result["results"][0]["code"], "not_found")

    def test_unauthorized_plan_request_not_claimable(self):
        task = self.fake.add_task(
            "taskplan00000000000003", "fixture-project-feedback")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        # 手工造一个「未授权」旧请求（payload 无 manual_authorized）。
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.create_request(
                    conn, util.now_iso(), "plan", f"plan:{task['id']}",
                    task_id=task["id"], payload={"legacy": True},
                    requested_by="test")
        finally:
            conn.close()
        # 定点 claim 未授权请求 → unauthorized_plan；自动取单则跳过 → empty。
        request = next(r for r in self.requests(status="queued")
                       if r["kind"] == "plan")
        result = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request["request_id"]})
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "unauthorized_plan")
        result = self.call("claim", {
            "stage": "plan", "executor": "test-planner"})
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "empty")

    def test_dispatch_hold_gate(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        # publish 后 hold：不带确认直接 enqueue dispatch → conflict。
        exc = self.call_err("enqueue", {
            "task_id": task_id, "kind": "dispatch"})
        self.assertEqual(exc.code, "conflict")
        # 确认放行 → dispatch 可领取。
        self.enqueue_dispatch(task_id)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"])
        # 既有 dispatch 请求在 hold 下不可领取（plan_hold）。
        fixture2 = self.make_ready_task(
            task_id="taskplan00000000000004")
        tid2 = fixture2["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.create_request(
                    conn, util.now_iso(), "dispatch", f"dispatch:{tid2}",
                    task_id=tid2)
        finally:
            conn.close()
        # 清掉前面 dispatch 的串行占用，让 plan_hold 单独生效。
        self.call("report", {
            "request_id": claim["request_id"], "run_id": claim["run_id"],
            "outcome": "failed", "note": "abort for test"})
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        # tid2 的 dispatch 请求仍在 hold → 自动取单跳过 → empty/plan_hold。
        self.assertFalse(result["claimed"])
        self.assertIn(result["reason"], ("plan_hold", "empty", "stage_busy"))

    def test_planner_runner_fake_ready(self):
        """KANPILOT_CODEX_FAKE=ready：runner 端到端 claim→codex(fake)→
        publish-plan → ready + hold。"""
        remote_task = self.fake.add_task(
            "taskplan00000000000005", "fixture-project-feedback",
            status="to-do", title="runner 冒烟")
        self.make_repo("Feedback")
        prepared = self.call("prepare-plan-input", {
            "task_id": remote_task["id"]})
        self.call("start-plans", {
            "items": [{"input_id": prepared["input_id"],
                       "input_sha256": prepared["input_sha256"]}]})
        os.environ["KANPILOT_CODEX_FAKE"] = "ready"
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)
        from kanpilot import planner_runner

        rc = planner_runner.run_loop(self.db_path, None, once=True)
        self.assertEqual(rc, 0)
        view = self.task(remote_task["id"])
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["display_status"], "待确认执行")
        self.assertTrue(view["plan_sha256"])
        # 计划文件落在 plan_root 且含 frontmatter。
        self.assertTrue(view["plan_path"])
        self.assertTrue(Path(view["plan_path"]).is_file())
        self.assertTrue(
            str(Path(view["plan_path"]).resolve()).startswith(
                str(Path(self.plan_root).resolve())))
        meta, _raw = util.read_frontmatter(Path(view["plan_path"]))
        self.assertEqual(meta["task_id"], remote_task["id"])
        self.assertEqual(meta["planner_model"], "gpt-6-astra")
        # 轮次记录：backend=codex + job.json。
        sessions = self.call(
            "list-sessions", {"task_id": remote_task["id"]})["sessions"]
        self.assertTrue(sessions)
        self.assertEqual(sessions[0]["backend"], "codex")

    def test_planner_runner_fake_needs_input(self):
        remote_task = self.fake.add_task(
            "taskplan00000000000006", "fixture-project-feedback",
            status="to-do")
        self.make_repo("Feedback")
        prepared = self.call("prepare-plan-input", {
            "task_id": remote_task["id"]})
        self.call("start-plans", {
            "items": [{"input_id": prepared["input_id"],
                       "input_sha256": prepared["input_sha256"]}]})
        os.environ["KANPILOT_CODEX_FAKE"] = "needs_input"
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)
        from kanpilot import planner_runner

        rc = planner_runner.run_loop(self.db_path, None, once=True)
        self.assertEqual(rc, 0)
        view = self.task(remote_task["id"])
        self.assertEqual(view["status"], "needs_user")


if __name__ == "__main__":
    unittest.main()
