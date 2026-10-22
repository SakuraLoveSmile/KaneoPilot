"""看板六列（protocol.md §2「看板列」/§5.14/state-model.md §2.1）：
board_column 归列、board_substatus 中文词表、blocked_reason 扩展、
last_checked_at、review_summary、list-tasks 过滤、overview counts.board、
advance-task Review 前置守卫。"""

from __future__ import annotations

import sqlite3
import unittest
from collections import Counter
from datetime import timedelta

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import util  # noqa: E402

PROJECT_ID = "fixture-project-feedback"


class BoardCase(KanpilotTestCase):
    """Helpers shared by the board tests: sync creates a needs_plan task
    with a queued plan request; set_task_status then moves it anywhere."""

    def add_task(self, task_id, remote_status="to-do"):
        task = self.fake.add_task(task_id, PROJECT_ID, status=remote_status)
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        return task_id

    def running_task(self, task_id):
        self.add_task(task_id, remote_status="in-progress")
        self.set_task_status(
            task_id, "running", sent=1, send_state="sent",
            session_marker=f"{task_id}/run-1",
        )
        return task_id

    def write_report(self, task_id, name="report.md"):
        directory = self.reports_root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("# review report\n", encoding="utf-8")
        return path

    def claim_stage(self, stage, executor):
        claim = self.call("claim", {"stage": stage, "executor": executor})
        self.assertTrue(claim["claimed"])
        return claim

    def expire_lease(self, request_id, seconds=60):
        conn = sqlite3.connect(str(self.db_path))
        past = (util.now_dt() - timedelta(seconds=seconds)).isoformat()
        conn.execute(
            "UPDATE requests SET lease_expires_at=? WHERE request_id=?",
            (past, request_id),
        )
        conn.commit()
        conn.close()


class TestBoardDirectMapping(BoardCase):
    def test_status_to_column_matrix(self):
        cases = [
            ("taskbmp-needsplan01", "needs_plan", "needs_plan", "待规划"),
            ("taskbmp-planning001", "planning", "needs_plan", "规划中"),
            ("taskbmp-ready000001", "ready", "todo", "就绪"),
            ("taskbmp-dispatching", "dispatching", "todo", "派发中"),
            ("taskbmp-prepared001", "prepared", "todo", "派发中"),
            ("taskbmp-submitting1", "submitting", "todo", "派发中"),
            ("taskbmp-running0001", "running", "in_progress", "执行中"),
            ("taskbmp-inreview001", "in_review", "in_review", "评审中"),
            ("taskbmp-done0000001", "done", "done", "已完成"),
        ]
        for task_id, status, column, substatus in cases:
            self.add_task(task_id)
            if status != "needs_plan":
                self.set_task_status(task_id, status)
            view = self.task(task_id)
            self.assertEqual(view["board_column"], column, task_id)
            self.assertEqual(view["board_substatus"], substatus, task_id)
            self.assertIsNone(view["blocked_reason"], task_id)
        # get-task 与 list-tasks 同任务同列（§5.14.2）
        listed = {
            t["task_id"]: t for t in self.call("list-tasks")["tasks"]
        }
        self.assertEqual(len(listed), len(cases))
        for task_id, _status, column, substatus in cases:
            self.assertEqual(listed[task_id]["board_column"], column)
            self.assertEqual(listed[task_id]["board_substatus"], substatus)

    def test_ready_with_queued_dispatch_is_queued(self):
        task_id = "taskbmp-queued00001"
        self.make_ready_task(task_id=task_id)
        self.enqueue_dispatch(task_id)
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "todo")
        self.assertEqual(view["board_substatus"], "已排队")

    def test_planning_via_claim_and_context_summary(self):
        task_id = self.add_task("taskbmp-planclaim01")
        self.queue_plan(task_id)  # v2：规划请求需手动授权（测试助手代建）
        claim = self.claim_stage("plan", "kaneo-plan")
        view = self.task(task_id)
        self.assertEqual(view["status"], "planning")
        self.assertEqual(view["board_column"], "needs_plan")
        self.assertEqual(view["board_substatus"], "规划中")
        # claim context 里的 TaskSummary 自动带新字段
        ctx_task = claim["context"]["task"]
        self.assertEqual(ctx_task["board_column"], "needs_plan")
        self.assertEqual(ctx_task["board_substatus"], "规划中")
        self.assertIn("last_checked_at", ctx_task)
        self.assertIn("review_summary", ctx_task)


class TestBoardNeedsUser(BoardCase):
    def test_send_state_unknown_is_blocked_not_failed(self):
        # send_state='unknown' 优先于一切 failure_code（§5.15.4）：即便 code 是
        # dispatch_failed 也归第六列 failed，但 exception.severity=blocked（橙），
        # 不是红色失败。
        task_id = self.add_task("tasknu-unknown0001")
        self.set_task_status(
            task_id, "needs_user", send_state="unknown",
            failure_code="dispatch_failed", failure_stage="submitting",
            failure_reason="send uncertain", blocked_summary="发送结果不明",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["board_substatus"], "发送不明")
        self.assertEqual(view["exception"]["kind"], "send_unknown")
        self.assertEqual(view["exception"]["severity"], "blocked")
        self.assertEqual(view["blocked_reason"], "发送结果不明")

    def test_failure_code_matrix(self):
        # §5.15.4：needs_user 一律归第六列 failed；子状态/标签由 exception.kind
        # 决定（失败码→失败标签，其余→需人工处理/监控异常/评审受阻）。
        cases = [
            # (failure_code, failure_stage, send_state, column, substatus)
            ("validation_failed", "ready", "not_sent", "failed", "派发失败"),
            ("dispatch_failed", "prepared", "not_sent", "failed", "派发失败"),
            ("execution_failed", "running", "sent", "failed", "执行失败"),
            ("plan_failed", "planning", None, "failed", "规划失败"),
            ("reported", "planning", None, "failed", "需人工处理"),
            ("observation_blocked", "running", "sent", "failed", "监控异常"),
            ("review_blocked", "in_review", "sent", "failed", "评审受阻"),
            ("send_conflict", "ready", "not_sent", "failed", "需人工处理"),
            ("remote_unavailable", "ready", "not_sent", "failed", "需人工处理"),
            ("interrupted_dispatch", "submitting", "not_sent",
             "failed", "需人工处理"),
            ("migrated", "in_review", "sent", "failed", "需人工处理"),
            ("unknown_code_xyz", "running", "sent", "failed", "需人工处理"),
            (None, "ready", "not_sent", "failed", "需人工处理"),
        ]
        for index, (code, stage, send_state, column, substatus) in enumerate(
            cases
        ):
            task_id = f"tasknu-code{index:07d}"
            self.add_task(task_id)
            self.set_task_status(
                task_id, "needs_user", send_state=send_state,
                failure_code=code, failure_stage=stage,
                failure_reason=f"reason-{index}",
                blocked_summary=f"reason-{index}",
            )
            view = self.task(task_id)
            self.assertEqual(
                view["board_column"], column, (code, stage)
            )
            self.assertEqual(
                view["board_substatus"], substatus, (code, stage)
            )
            self.assertEqual(view["blocked_reason"], f"reason-{index}")

    def test_failure_stage_accepts_stage_slugs(self):
        # §5.15.4：board_column 恒为 failed，但 lifecycle_column 仍按
        # failure_stage 归列（保留纯生命周期位置）。
        for stage_slug, column in [
            ("plan", "needs_plan"),
            ("dispatch", "todo"),
            ("monitor", "in_progress"),
            ("review", "in_review"),
            ("done", "done"),
        ]:
            task_id = f"tasknu-stg-{stage_slug}"
            self.add_task(task_id)
            self.set_task_status(
                task_id, "needs_user", failure_code="reported",
                failure_stage=stage_slug, failure_reason="x",
                blocked_summary="x",
            )
            view = self.task(task_id)
            self.assertEqual(view["board_column"], "failed", stage_slug)
            self.assertEqual(
                view["lifecycle_column"], column, stage_slug
            )

    def test_missing_failure_stage_falls_back_to_task_stage(self):
        task_id = self.add_task("tasknu-nofstage001")
        self.set_task_status(
            task_id, "needs_user", stage="monitor",
            failure_code="reported", failure_stage=None,
            failure_reason="stuck", blocked_summary="stuck",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        # failure_stage 缺失 → lifecycle_column 回退 task.stage=monitor。
        self.assertEqual(view["lifecycle_column"], "in_progress")
        self.assertEqual(view["board_substatus"], "需人工处理")

    def test_needs_user_column_independent_of_active_requests(self):
        # §5.15.4：失联优先于 needs_user 细分——监视请求失联 + needs_user 阻塞
        # 同时存在时 kind=executor_lost，归第六列 failed。
        task_id = self.running_task("tasknu-lost000001")
        claim = self.claim_stage("monitor", "kaneo-monitor")
        self.expire_lease(claim["request_id"])
        self.set_task_status(
            task_id, "needs_user", failure_code="observation_blocked",
            failure_stage="running", failure_reason="锁屏无法观测",
            blocked_summary="锁屏无法观测",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "executor_lost")
        self.assertEqual(view["board_substatus"], "执行端失联")
        # 失联的 blocked_reason/detail 是失联文案，而非 needs_user 阻塞摘要。
        self.assertEqual(
            view["blocked_reason"], "执行端失联（监控请求租约过期）"
        )
        # lifecycle_column 仍保留 needs_user 的 failure_stage=running 位置。
        self.assertEqual(view["lifecycle_column"], "in_progress")


class TestBoardLost(BoardCase):
    def test_lost_monitor_keeps_column_sets_blocked_reason(self):
        # §5.15.4：失联（claimed 且租约过期）一律归第六列 failed。
        task_id = self.running_task("tasklost-mon00001")
        claim = self.claim_stage("monitor", "kaneo-monitor")
        self.expire_lease(claim["request_id"])
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "executor_lost")
        self.assertEqual(view["board_substatus"], "执行端失联")
        self.assertEqual(
            view["blocked_reason"], "执行端失联（监控请求租约过期）"
        )
        self.assertEqual(view["display_status"], "执行端失联")
        # lifecycle_column 保留 running 的纯生命周期位置。
        self.assertEqual(view["lifecycle_column"], "in_progress")

    def test_lost_plan_request_blocked_reason(self):
        task_id = self.add_task("tasklost-plan0001")
        self.queue_plan(task_id)  # v2：同上
        claim = self.claim_stage("plan", "kaneo-plan")
        self.expire_lease(claim["request_id"])
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["board_substatus"], "执行端失联")
        self.assertEqual(
            view["blocked_reason"], "执行端失联（规划请求租约过期）"
        )

    def test_no_lost_no_blocked_reason(self):
        task_id = self.running_task("tasklost-none0001")
        self.claim_stage("monitor", "kaneo-monitor")  # lease 未过期
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "in_progress")
        self.assertIsNone(view["blocked_reason"])


class TestBoardRecovery(BoardCase):
    def test_retry_returns_to_direct_mapping(self):
        task_id = self.add_task("taskrec-retry0001")
        self.set_task_status(
            task_id, "needs_user", send_state="not_sent",
            failure_code="dispatch_failed", failure_stage="dispatching",
            failure_reason="ui blocked", blocked_summary="ui blocked",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["board_substatus"], "派发失败")
        self.call("resolve", {"task_id": task_id, "action": "retry"})
        view = self.task(task_id)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["board_column"], "todo")
        # retry 按原 kind 建了 dispatch 请求 -> 已排队
        self.assertEqual(view["board_substatus"], "已排队")
        self.assertIsNone(view["blocked_reason"])

    def test_dismiss_and_mark_sent(self):
        task_id = self.add_task("taskrec-dismiss01")
        self.set_task_status(
            task_id, "needs_user", send_state="not_sent",
            failure_code="reported", failure_stage="ready",
            failure_reason="x", blocked_summary="x",
        )
        self.assertEqual(self.task(task_id)["board_column"], "failed")
        self.call("resolve", {"task_id": task_id, "action": "dismiss"})
        view = self.task(task_id)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["board_column"], "todo")
        self.assertEqual(view["board_substatus"], "就绪")  # dismiss 不建请求
        self.assertIsNone(view["blocked_reason"])

        task_id = self.add_task("taskrec-marksent1")
        self.set_task_status(
            task_id, "needs_user", send_state="unknown",
            failure_code="send_unknown", failure_stage="submitting",
            failure_reason="maybe sent", blocked_summary="maybe sent",
        )
        self.assertEqual(self.task(task_id)["board_column"], "failed")
        self.call("resolve", {"task_id": task_id, "action": "mark_sent"})
        view = self.task(task_id)
        self.assertEqual(view["status"], "running")
        self.assertEqual(view["board_column"], "in_progress")
        self.assertEqual(view["board_substatus"], "执行中")
        self.assertIsNone(view["blocked_reason"])

    def test_replan_returns_to_needs_plan(self):
        task_id = self.add_task("taskrec-replan001")
        self.set_task_status(
            task_id, "needs_user", failure_code="plan_failed",
            failure_stage="planning", failure_reason="bad plan",
            blocked_summary="bad plan",
        )
        self.assertEqual(self.task(task_id)["board_column"], "failed")
        self.call("resolve", {"task_id": task_id, "action": "replan"})
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "needs_plan")
        self.assertEqual(view["board_substatus"], "待规划")


class TestAdvanceReviewGuard(BoardCase):
    def test_done_from_in_progress_requires_pass(self):
        task_id = self.running_task("taskadv-guard0001")
        exc = self.call_err(
            "advance-task",
            {"task_id": task_id, "target": "done",
             "expected_current": "in-progress"},
        )
        self.assertEqual(exc.code, "conflict")
        self.assertIn("review", exc.message.lower())
        # 远端未动、本地未转
        self.assertEqual(self.fake.tasks[task_id]["status"], "in-progress")
        self.assertEqual(self.task(task_id)["status"], "running")

    def test_done_after_registered_pass(self):
        task_id = self.running_task("taskadv-pass00001")
        report = self.write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "pass"},
        )
        result = self.call(
            "advance-task",
            {"task_id": task_id, "target": "done",
             "expected_current": "in-progress"},
        )
        self.assertEqual(result["task_status"], "done")
        self.assertEqual(self.fake.tasks[task_id]["status"], "done")
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "done")
        self.assertEqual(view["board_substatus"], "已完成")

    def test_changes_requested_does_not_satisfy_guard(self):
        task_id = self.running_task("taskadv-changes01")
        report = self.write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "changes_requested",
             "fix_task_id": "fixtask000000000001", "review_key": "rk-1"},
        )
        exc = self.call_err(
            "advance-task",
            {"task_id": task_id, "target": "done",
             "expected_current": "in-progress"},
        )
        self.assertEqual(exc.code, "conflict")
        self.assertEqual(self.fake.tasks[task_id]["status"], "in-progress")

    def test_in_review_to_done_unguarded(self):
        task_id = self.add_task(
            "taskadv-revdone001", remote_status="in-review"
        )
        self.set_task_status(task_id, "in_review")
        result = self.call(
            "advance-task",
            {"task_id": task_id, "target": "done",
             "expected_current": "in-review"},
        )
        self.assertEqual(result["task_status"], "done")

    def test_new_review_cycle_clears_conclusion(self):
        task_id = self.running_task("taskadv-cycle0001")
        # 第一周期：进入评审并登记 changes_requested
        self.call(
            "advance-task",
            {"task_id": task_id, "target": "in-review",
             "expected_current": "in-progress"},
        )
        report = self.write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "changes_requested",
             "fix_task_id": "fixtask000000000002", "review_key": "rk-2"},
        )
        view = self.task(task_id)
        self.assertEqual(
            view["review_summary"]["conclusion"], "changes_requested"
        )
        # §5.15.4：评审未通过·待修复归第六列 failed。
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["board_substatus"], "评审未通过·待修复")
        # 修复后回到 running，再进新一轮 in-review
        self.fake.tasks[task_id]["status"] = "in-progress"
        self.set_task_status(task_id, "running")
        self.call(
            "advance-task",
            {"task_id": task_id, "target": "in-review",
             "expected_current": "in-progress"},
        )
        view = self.task(task_id)
        self.assertEqual(view["status"], "in_review")
        self.assertIsNone(view["review_summary"])
        self.assertIsNone(view["review_report_path"])
        self.assertEqual(view["board_substatus"], "评审中")
        # fix_task_id / review_key 历史关联保留
        self.assertEqual(view["fix_task_id"], "fixtask000000000002")
        self.assertEqual(view["review_key"], "rk-2")


class TestBoardFields(BoardCase):
    def test_review_summary_shape(self):
        task_id = self.add_task("taskfld-summary01")
        self.assertIsNone(self.task(task_id)["review_summary"])
        self.set_task_status(task_id, "in_review")
        report = self.write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "pass"},
        )
        summary = self.task(task_id)["review_summary"]
        self.assertEqual(
            set(summary),
            {"conclusion", "registered_at", "fix_task_id",
             "review_key", "report_path"},
        )
        self.assertEqual(summary["conclusion"], "pass")
        self.assertIsInstance(summary["registered_at"], str)
        self.assertIsNone(summary["fix_task_id"])
        self.assertIsNone(summary["review_key"])
        self.assertEqual(summary["report_path"], str(report))
        # 普通 pass 仍是评审中，不是待修复
        self.assertEqual(self.task(task_id)["board_substatus"], "评审中")

    def test_review_summary_changes_requested_fields(self):
        task_id = self.add_task("taskfld-fixsum001")
        self.set_task_status(task_id, "in_review")
        report = self.write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "changes_requested",
             "fix_task_id": "fixtask000000000003", "review_key": "rk-3"},
        )
        view = self.task(task_id)
        summary = view["review_summary"]
        self.assertEqual(summary["conclusion"], "changes_requested")
        self.assertEqual(summary["fix_task_id"], "fixtask000000000003")
        self.assertEqual(summary["review_key"], "rk-3")
        # §5.15.4：评审未通过·待修复归第六列 failed，关联 fix_task_id。
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["board_substatus"], "评审未通过·待修复")
        self.assertEqual(view["exception"]["kind"], "review_rejected")
        self.assertEqual(view["exception"]["fix_task_id"], "fixtask000000000003")

    def test_last_checked_at_max_sources(self):
        task_id = self.running_task("taskfld-checked01")
        # 初始：只有 sync 写入的 kaneo_checked_at
        view = self.task(task_id)
        self.assertEqual(
            view["last_checked_at"], view["kaneo_checked_at"]
        )
        self.claim_stage("monitor", "kaneo-monitor")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE tasks SET kaneo_checked_at='2026-01-01T00:00:00+00:00' "
            "WHERE task_id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE requests SET heartbeat_at='2026-02-02T00:00:00+00:00' "
            "WHERE task_id=? AND status='claimed'",
            (task_id,),
        )
        conn.execute(
            "UPDATE attempts SET finished_at='2026-03-03T00:00:00+00:00' "
            "WHERE task_id=? AND kind='monitor'",
            (task_id,),
        )
        conn.commit()
        view = self.task(task_id)
        self.assertEqual(
            view["last_checked_at"], "2026-03-03T00:00:00+00:00"
        )
        # attempt finished 移除 -> heartbeat 成为最大值
        conn.execute(
            "UPDATE attempts SET finished_at=NULL "
            "WHERE task_id=? AND kind='monitor'",
            (task_id,),
        )
        conn.commit()
        self.assertEqual(
            self.task(task_id)["last_checked_at"],
            "2026-02-02T00:00:00+00:00",
        )
        # 请求不再活动（取消）-> heartbeat 不计；dispatch 类 attempt 不算观测
        conn.execute(
            "UPDATE requests SET status='cancelled' "
            "WHERE task_id=? AND status='claimed'",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO attempts(request_id, task_id, kind, finished_at, "
            "outcome) VALUES ('r-x', ?, 'dispatch', "
            "'2027-01-01T00:00:00+00:00', 'succeeded')",
            (task_id,),
        )
        conn.commit()
        self.assertEqual(
            self.task(task_id)["last_checked_at"],
            "2026-01-01T00:00:00+00:00",
        )
        # 全无 -> null
        conn.execute(
            "UPDATE tasks SET kaneo_checked_at=NULL WHERE task_id=?",
            (task_id,),
        )
        conn.commit()
        conn.close()
        self.assertIsNone(self.task(task_id)["last_checked_at"])


class TestBoardCountsAndFilter(BoardCase):
    def test_overview_board_and_list_filter(self):
        cases = [
            # (setup produces) -> expected column
            ("taskcnt-needsplan1", None, "needs_plan"),
            ("taskcnt-ready00001", "ready", "todo"),
            ("taskcnt-readyqueue1", "ready+queue", "todo"),
            ("taskcnt-running001", "running", "in_progress"),
            ("taskcnt-inreview01", "in_review", "in_review"),
            ("taskcnt-done000001", "done", "done"),
            ("taskcnt-failed0001", "failed", "failed"),
            ("tasknu-cnt-todo01", "blocked_todo", "failed"),
        ]
        for task_id, mode, _column in cases:
            self.add_task(task_id)
            if mode == "ready+queue":
                self.make_ready_task(task_id=task_id)
                self.enqueue_dispatch(task_id)
            elif mode == "failed":
                self.set_task_status(
                    task_id, "needs_user", send_state="sent",
                    failure_code="execution_failed",
                    failure_stage="running", failure_reason="boom",
                    blocked_summary="boom",
                )
            elif mode == "blocked_todo":
                self.set_task_status(
                    task_id, "needs_user", send_state="unknown",
                    failure_code="send_unknown", failure_stage="submitting",
                    failure_reason="unsure", blocked_summary="unsure",
                )
            elif mode:
                self.set_task_status(task_id, mode)

        # 每任务恰出现一次：list-tasks 全量分组计数 == counts.board
        tasks = self.call("list-tasks")["tasks"]
        self.assertEqual(len(tasks), len(cases))
        grouped = Counter(t["board_column"] for t in tasks)
        board = self.call("overview")["counts"]["board"]
        self.assertEqual(set(board), {
            "needs_plan", "todo", "in_progress", "in_review",
            "done", "failed",
        })
        self.assertEqual(dict(grouped), board)
        self.assertEqual(sum(board.values()), len(tasks))
        expected = Counter(column for _id, _m, column in cases)
        self.assertEqual(dict(grouped), dict(expected))

        # board_column 派生后过滤
        todo = self.call("list-tasks", {"board_column": "todo"})["tasks"]
        self.assertEqual(len(todo), 2)
        self.assertTrue(
            all(t["board_column"] == "todo" for t in todo)
        )
        # §5.15.4：needs_user（含发送不明）一律进第六列 failed。
        failed = self.call("list-tasks", {"board_column": "failed"})["tasks"]
        self.assertEqual(len(failed), 2)
        self.assertEqual(
            {t["task_id"] for t in failed},
            {"taskcnt-failed0001", "tasknu-cnt-todo01"},
        )
        empty = self.call(
            "list-tasks", {"board_column": "needs_plan"}
        )["tasks"]
        self.assertEqual(len(empty), 1)
        exc = self.call_err(
            "list-tasks", {"board_column": "bogus"}
        )
        self.assertEqual(exc.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
