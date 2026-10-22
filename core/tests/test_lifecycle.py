"""Lifecycle commands: publish-plan, register-review, advance-task,
create-review-fix, resolve actions, tick cycles, sync-tasks."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import util  # noqa: E402


class TestPublishPlan(KanpilotTestCase):
    def test_publish_plan_ready(self):
        fixture = self.make_ready_task()
        view = self.task(fixture["remote"]["id"])
        self.assertEqual(view["status"], "ready")
        # v2 §5.22.4：发布后 plan_execution_hold → 展示「待确认执行」。
        self.assertEqual(view["display_status"], "待确认执行")
        self.assertTrue(view["plan_sha256"])
        events = self.call(
            "list-events", {"task_id": fixture["remote"]["id"]}
        )["events"]
        self.assertTrue(any(e["kind"] == "plan_published" for e in events))

    def test_publish_plan_rejects_bad_meta(self):
        remote_task = self.fake.add_task(
            "taskbadplan00000000001", "fixture-project-feedback"
        )
        self.make_repo("Feedback")
        source_sha, req_sha, source_path = self.make_source_snapshot(remote_task)
        self.sync_task(
            remote_task, project_name="Feedback",
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path,
        )
        bad_plan = self.write_plan(
            "Feedback", remote_task,
            {"source_sha256": source_sha, "requirements_sha256": req_sha,
             "implementation_status": "已完成"},
        )
        # v2：先建授权规划请求并 claim，再走 publish-plan（validation_failed
        # 由 frontmatter 校验触发）。
        request_id, input_sha = self.queue_plan(
            remote_task["id"], source_sha256=source_sha,
            requirements_sha256=req_sha)
        claim = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request_id})
        self.assertTrue(claim["claimed"])
        exc = self.call_err(
            "publish-plan",
            {"task_id": remote_task["id"], "plan_path": str(bad_plan),
             "plan_kind": "standard", "source_sha256": source_sha,
             "requirements_sha256": req_sha,
             "generated_at": "2026-09-20T00:00:00+00:00",
             "planner_model": "gpt-6-astra", "reasoning_effort": "medium",
             "request_id": request_id, "run_id": claim["run_id"],
             "planner_input_sha256": input_sha},
        )
        self.assertEqual(exc.code, "validation_failed")

    def test_publish_plan_conflict_during_dispatch(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        # v2：dispatch 中段拒绝覆盖派发输入。任务已 ready（hold），建授权
        # plan 请求会撞 dispatch guard——改为先建请求再搬状态到 dispatching。
        self.set_task_status(task_id, "needs_plan")
        request_id, input_sha = self.queue_plan(
            task_id, source_sha256=fixture["source_sha256"],
            requirements_sha256=fixture["requirements_sha256"])
        claim = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request_id})
        self.assertTrue(claim["claimed"])
        self.set_task_status(task_id, "dispatching")
        exc = self.call_err(
            "publish-plan",
            {"task_id": task_id, "plan_path": str(fixture["plan_path"]),
             "plan_kind": "standard",
             "source_sha256": fixture["source_sha256"],
             "requirements_sha256": fixture["requirements_sha256"],
             "generated_at": "2026-09-20T00:00:00+00:00",
             "planner_model": "gpt-6-astra", "reasoning_effort": "medium",
             "request_id": request_id, "run_id": claim["run_id"],
             "planner_input_sha256": input_sha},
        )
        self.assertEqual(exc.code, "conflict")


class TestReviewAndAdvance(KanpilotTestCase):
    def _running_task(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.fake.tasks[task_id]["status"] = "in-progress"
        self.set_task_status(
            task_id, "running", sent=1, send_state="sent",
            session_marker=f"{task_id}/run-1",
        )
        return fixture

    def test_advance_to_review_then_done(self):
        fixture = self._running_task()
        task_id = fixture["remote"]["id"]
        result = self.call(
            "advance-task",
            {"task_id": task_id, "target": "in-review",
             "expected_current": "in-progress"},
        )
        self.assertEqual(result["task_status"], "in_review")
        self.assertEqual(result["kaneo_status"], "in-review")
        self.assertEqual(self.fake.tasks[task_id]["status"], "in-review")
        self.assertEqual(self.task(task_id)["status"], "in_review")
        # review request was created
        reqs = [r for r in self.requests(status="queued")
                if r["kind"] == "review" and r["task_id"] == task_id]
        self.assertEqual(len(reqs), 1)
        # then done
        self.fake.tasks[task_id]["status"] = "in-review"
        result = self.call(
            "advance-task",
            {"task_id": task_id, "target": "done",
             "expected_current": "in-review"},
        )
        self.assertEqual(result["task_status"], "done")

    def test_advance_remote_conflict_blocks(self):
        fixture = self._running_task()
        task_id = fixture["remote"]["id"]
        self.fake.tasks[task_id]["status"] = "done"  # unexpected
        exc = self.call_err(
            "advance-task",
            {"task_id": task_id, "target": "in-review",
             "expected_current": "in-progress"},
        )
        self.assertEqual(exc.code, "remote_conflict")
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["kaneo_status"], "done")

    def _write_report(self, task_id, name="report.md"):
        directory = self.reports_root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("# review report\n", encoding="utf-8")
        return path

    def test_register_review_pass_and_blocked(self):
        fixture = self._running_task()
        task_id = fixture["remote"]["id"]
        self.set_task_status(task_id, "in_review")
        report = self._write_report(task_id)
        result = self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "pass"},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["report_sha256"])
        view = self.task(task_id)
        self.assertEqual(view["status"], "in_review")  # awaits advance
        self.assertEqual(view["review_report_path"], str(report))
        # pass + kaneo_status done -> done
        result = self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "pass", "kaneo_status": "done"},
        )
        self.assertEqual(self.task(task_id)["status"], "done")

    def test_register_review_blocked(self):
        fixture = self._running_task()
        task_id = fixture["remote"]["id"]
        self.set_task_status(task_id, "in_review")
        report = self._write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "blocked"},
        )
        self.assertEqual(self.task(task_id)["status"], "needs_user")

    def test_create_review_fix_idempotent(self):
        fixture = self._running_task()
        task_id = fixture["remote"]["id"]
        self.set_task_status(task_id, "in_review")
        report = self._write_report(task_id)
        dedupe = "ab" * 32
        result = self.call(
            "create-review-fix",
            {"source_task_id": task_id, "title": "fix it",
             "description": "repair the thing", "priority": "high",
             "dedupe_key": dedupe},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        fix_id = result["fix_task_id"]
        remote_task = self.fake.tasks[fix_id]
        self.assertIn(dedupe, remote_task["description"])
        view = self.task(fix_id)
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(view["plan_kind"], "review_fix")
        self.assertEqual(view["review_source_task_id"], task_id)
        self.assertEqual(view["review_key"], dedupe)
        # v2 §5.22.2：create-review-fix 不再自动创建 plan 请求（等待规划
        # 面板手动提交）；任务停在 needs_plan。
        reqs = [r for r in self.requests(status="queued")
                if r["task_id"] == fix_id and r["kind"] == "plan"]
        self.assertEqual(reqs, [])
        # repeat -> dedup
        again = self.call(
            "create-review-fix",
            {"source_task_id": task_id, "title": "fix it",
             "description": "repair the thing", "priority": "high",
             "dedupe_key": dedupe},
        )
        self.assertFalse(again["created"])
        self.assertEqual(again["fix_task_id"], fix_id)


class TestTick(KanpilotTestCase):
    def test_tick_no_longer_creates_plan_scan(self):
        # v2 §5.22.2：tick 不再生成 plan-scan（手动规划链取代定时扫描）。
        first = self.call("tick")
        kinds = [c["kind"] for c in first["created"]]
        self.assertNotIn("plan-scan", kinds)

    def test_tick_monitor_and_review(self):
        running = self.fake.add_task(
            "taskrun0000000000000001", "fixture-project-feedback",
            status="in-progress",
        )
        self.make_repo("Feedback")
        self.sync_task(running, project_name="Feedback")
        self.set_task_status(running["id"], "running")
        reviewing = self.fake.add_task(
            "taskrev0000000000000001", "fixture-project-feedback",
            status="in-review",
        )
        self.sync_task(reviewing, project_name="Feedback")
        self.set_task_status(reviewing["id"], "in_review")
        result = self.call("tick")
        created = {(c["kind"], c["task_id"]) for c in result["created"]}
        self.assertIn(("monitor", running["id"]), created)
        self.assertIn(("review", reviewing["id"]), created)
        again = self.call("tick")
        self.assertFalse(
            any(c["task_id"] in (running["id"], reviewing["id"])
                for c in again["created"])
        )

    def test_monitor_cycle_respects_interval(self):
        task = self.fake.add_task(
            "taskmon0000000000000001", "fixture-project-feedback",
            status="in-progress",
        )
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        self.set_task_status(task["id"], "running")
        self.call("tick")
        claim = self.call(
            "claim", {"stage": "monitor", "executor": "kaneo-monitor"}
        )
        self.assertTrue(claim["claimed"])
        self.call(
            "report",
            {"request_id": claim["request_id"], "run_id": claim["run_id"],
             "outcome": "succeeded", "progress": "still running"},
        )
        # report queued the next cycle with not_before=+15min -> no claim now
        result = self.call(
            "claim", {"stage": "monitor", "executor": "kaneo-monitor"}
        )
        self.assertFalse(result["claimed"])
        # force the queued request due, tick should not duplicate
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("UPDATE requests SET not_before=NULL WHERE status='queued'")
        conn.commit()
        conn.close()
        tick = self.call("tick")
        self.assertFalse(
            any(c["kind"] == "monitor" for c in tick["created"])
        )


class TestSyncTasks(KanpilotTestCase):
    def test_requirements_change_replans(self):
        task = self.fake.add_task(
            "tasksync00000000000001", "fixture-project-feedback"
        )
        self.make_repo("Feedback")
        source_sha, req_sha, source_path = self.make_source_snapshot(task)
        self.sync_task(
            task, project_name="Feedback", requirements_sha256=req_sha,
            source_sha256=source_sha, source_path=source_path,
        )
        self.set_task_status(task["id"], "ready")
        self.sync_task(
            task, project_name="Feedback",
            requirements_sha256="newrequirements", source_sha256="newsource",
            source_path=source_path,
        )
        view = self.task(task["id"])
        self.assertEqual(view["status"], "needs_plan")
        # v2 §5.22.2：需求变化更新状态与证据但不自动建 plan 请求。
        reqs = [r for r in self.requests(status="queued")
                if r["task_id"] == task["id"] and r["kind"] == "plan"]
        self.assertEqual(reqs, [])

    def test_kaneo_done_archives_and_dispatch_untouched(self):
        done_task = self.fake.add_task(
            "taskdone00000000000001", "fixture-project-feedback", status="done"
        )
        self.make_repo("Feedback")
        self.sync_task(done_task, project_name="Feedback")
        # state-model §2: kaneo_status=done archives regardless of status,
        # including a task first observed already done.
        self.assertEqual(self.task(done_task["id"])["status"], "done")
        self.sync_task(done_task, project_name="Feedback")
        self.assertEqual(self.task(done_task["id"])["status"], "done")

        mid = self.fake.add_task(
            "taskmid0000000000000001", "fixture-project-feedback",
            status="in-progress",
        )
        self.sync_task(mid, project_name="Feedback")
        self.set_task_status(
            mid["id"], "submitting", plan_sha256="keepme",
            requirements_sha256="keepreq",
        )
        self.sync_task(mid, project_name="Feedback",
                       requirements_sha256="changed")
        view = self.task(mid["id"])
        self.assertEqual(view["status"], "submitting")
        self.assertEqual(view["requirements_sha256"], "keepreq")
        self.assertEqual(view["kaneo_status"], "in-progress")


class TestResolve(KanpilotTestCase):
    def _blocked(self, send_state=None,
                 task_id="taskready000000000000001"):
        fixture = self.make_ready_task(task_id=task_id)
        task_id = fixture["remote"]["id"]
        self.set_task_status(
            task_id, "needs_user", send_state=send_state,
            failure_reason="blocked", failure_stage="ready",
        )
        return fixture

    def test_resolve_retry_dispatch(self):
        fixture = self._blocked(send_state="not_sent")
        task_id = fixture["remote"]["id"]
        result = self.call(
            "resolve", {"task_id": task_id, "action": "retry"}
        )
        self.assertEqual(result["task_status"], "ready")
        self.assertEqual(result["request"]["kind"], "dispatch")
        self.assertNotIn("retry", self.task(task_id)["allowed_actions"])

    def test_resolve_restore_and_mark_sent(self):
        fixture = self._blocked(send_state="unknown")
        task_id = fixture["remote"]["id"]
        exc = self.call_err(
            "resolve", {"task_id": task_id, "action": "retry"}
        )
        self.assertEqual(exc.code, "bad_request")
        result = self.call(
            "resolve", {"task_id": task_id, "action": "restore"}
        )
        self.assertEqual(result["task_status"], "ready")
        self.assertEqual(result["request"]["kind"], "dispatch")
        view = self.task(task_id)
        self.assertEqual(view["send_state"], "not_sent")

        fixture2 = self.make_ready_task(task_id="taskms00000000000000001")
        task_id = fixture2["remote"]["id"]
        self.set_task_status(
            task_id, "needs_user", send_state="unknown",
            failure_reason="blocked",
        )
        result = self.call(
            "resolve", {"task_id": task_id, "action": "mark_sent"}
        )
        self.assertEqual(result["task_status"], "running")
        self.assertEqual(result["request"]["kind"], "monitor")
        view = self.task(task_id)
        self.assertEqual(view["send_state"], "sent")
        self.assertEqual(view["sent"], 1)

    def test_resolve_replan_dismiss_mark_done(self):
        fixture = self._blocked()
        task_id = fixture["remote"]["id"]
        result = self.call(
            "resolve", {"task_id": task_id, "action": "replan"}
        )
        self.assertEqual(result["task_status"], "needs_plan")
        # v2 §5.22.2：replan 只恢复状态，不自动创建 plan 请求。
        self.assertIsNone(result["request"])

        fixture = self._blocked(task_id="taskready000000000000002")
        task_id = fixture["remote"]["id"]
        result = self.call(
            "resolve", {"task_id": task_id, "action": "dismiss"}
        )
        self.assertEqual(result["task_status"], "ready")
        self.assertIsNone(result["request"])

        fixture = self._blocked(task_id="taskready000000000000003")
        task_id = fixture["remote"]["id"]
        result = self.call(
            "resolve", {"task_id": task_id, "action": "mark_done"}
        )
        self.assertEqual(result["task_status"], "done")

    def test_resolve_release_lost_restores_prior_status(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        # simulate a monitor request claimed on a running task then lost
        self.set_task_status(task_id, "running")
        req = self.call("enqueue", {"task_id": task_id, "kind": "monitor"})
        claim = self.call(
            "claim", {"stage": "monitor", "executor": "kaneo-monitor"}
        )
        self.assertTrue(claim["claimed"])
        conn = sqlite3.connect(str(self.db_path))
        past = (util.now_dt() - timedelta(seconds=60)).isoformat()
        conn.execute(
            "UPDATE requests SET lease_expires_at=? WHERE request_id=?",
            (past, claim["request_id"]),
        )
        conn.commit()
        conn.close()
        result = self.call(
            "resolve", {"task_id": task_id, "action": "release_lost"}
        )
        cancelled = [r for r in self.requests(status="cancelled")
                     if r["request_id"] == claim["request_id"]]
        self.assertEqual(len(cancelled), 1)
        view = self.task(task_id)
        self.assertEqual(view["status"], "running")
        # replacement monitor request queued
        reqs = [r for r in self.requests(status="queued")
                if r["task_id"] == task_id and r["kind"] == "monitor"]
        self.assertTrue(reqs)


if __name__ == "__main__":
    unittest.main()
