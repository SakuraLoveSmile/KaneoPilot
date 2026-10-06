"""六列看板异常归列 v1.3（protocol.md §5.15.4 / state-model.md §2.2）：
lifecycle_column / board_column / exception 六类互斥分类、severity 红/橙、
board_substatus=exception.label、恢复后历史失败不污染。

六类异常：明确失败(failed)、发送不明(send_unknown)、失联(executor_lost)、
评审未通过(review_rejected)、监控异常(observation_blocked)、需人工(manual_block)；
另覆盖评审受阻(review_blocked)。异常一律 board_column=failed，正常列只放正常卡片。
"""

from __future__ import annotations

import sqlite3
import unittest
from collections import Counter
from datetime import timedelta

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import util  # noqa: E402

PROJECT_ID = "example-project-05"


class ColumnCase(KanpilotTestCase):
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

    def set_needs_user(self, task_id, **fields):
        self.add_task(task_id)
        self.set_task_status(task_id, "needs_user", **fields)


class TestNormalColumns(ColumnCase):
    def test_normal_tasks_have_no_exception(self):
        cases = [
            ("taskcol-needsplan1", "needs_plan", "needs_plan"),
            ("taskcol-ready00001", "ready", "todo"),
            ("taskcol-running001", "running", "in_progress"),
            ("taskcol-inreview01", "in_review", "in_review"),
            ("taskcol-done000001", "done", "done"),
        ]
        for task_id, status, _column in cases:
            self.add_task(task_id)
            if status != "needs_plan":
                self.set_task_status(task_id, status)
        for task_id, _status, column in cases:
            view = self.task(task_id)
            # 正常卡片：board_column == lifecycle_column，无异常。
            self.assertEqual(view["board_column"], column, task_id)
            self.assertEqual(view["lifecycle_column"], column, task_id)
            self.assertIsNone(view["exception"], task_id)
            self.assertIsNone(view["blocked_reason"], task_id)


class TestSixExceptionClasses(ColumnCase):
    """六类（+评审受阻）异常互斥分类：board_column=failed，kind/label/severity。"""

    def test_explicit_failure_is_red(self):
        # 明确失败：failure_code ∈ 失败集 → kind=failed, severity=failed(红)。
        cases = [
            ("plan_failed", "规划失败"),
            ("validation_failed", "派发失败"),
            ("dispatch_failed", "派发失败"),
            ("execution_failed", "执行失败"),
        ]
        for index, (code, label) in enumerate(cases):
            task_id = f"taskcol-fail{index:07d}"
            self.set_needs_user(
                task_id, send_state="sent", failure_code=code,
                failure_stage="running", failure_reason=f"boom-{code}",
                blocked_summary=f"boom-{code}",
            )
            view = self.task(task_id)
            self.assertEqual(view["board_column"], "failed", code)
            self.assertEqual(view["exception"]["kind"], "failed", code)
            self.assertEqual(view["exception"]["severity"], "failed", code)
            self.assertEqual(view["exception"]["label"], label, code)
            self.assertEqual(view["exception"]["failure_code"], code, code)
            self.assertEqual(view["board_substatus"], label, code)
            # lifecycle_column 保留 failure_stage=running 的纯生命周期位置。
            self.assertEqual(view["lifecycle_column"], "in_progress", code)

    def test_send_unknown_priority_over_code(self):
        # 发送不明优先于一切 failure_code；severity=blocked(橙)。
        task_id = "taskcol-sendunk01"
        self.set_needs_user(
            task_id, send_state="unknown", failure_code="execution_failed",
            failure_stage="running", failure_reason="unsure",
            blocked_summary="unsure",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "send_unknown")
        self.assertEqual(view["exception"]["severity"], "blocked")
        self.assertEqual(view["exception"]["label"], "发送不明")
        self.assertEqual(view["board_substatus"], "发送不明")

    def test_manual_block(self):
        task_id = "taskcol-manual01"
        self.set_needs_user(
            task_id, send_state="not_sent", failure_code="send_conflict",
            failure_stage="ready", failure_reason="需人工",
            blocked_summary="需人工",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "manual_block")
        self.assertEqual(view["exception"]["severity"], "blocked")
        self.assertEqual(view["exception"]["label"], "需人工处理")

    def test_review_blocked(self):
        task_id = "taskcol-revblk01"
        self.set_needs_user(
            task_id, send_state="sent", failure_code="review_blocked",
            failure_stage="in_review", failure_reason="评审受阻",
            blocked_summary="评审受阻",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "review_blocked")
        self.assertEqual(view["exception"]["label"], "评审受阻")
        self.assertEqual(view["exception"]["severity"], "blocked")

    def test_observation_blocked(self):
        task_id = "taskcol-obsblk01"
        self.set_needs_user(
            task_id, send_state="sent", failure_code="observation_blocked",
            failure_stage="running", failure_reason="锁屏",
            blocked_summary="锁屏",
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "observation_blocked")
        self.assertEqual(view["exception"]["label"], "监控异常")
        self.assertEqual(view["exception"]["severity"], "blocked")

    def test_executor_lost(self):
        # 失联：claimed 且租约过期 → kind=executor_lost（优先于 needs_user 细分）。
        task_id = self.running_task("taskcol-lost00001")
        claim = self.claim_stage("monitor", "kaneo-monitor")
        self.expire_lease(claim["request_id"])
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "executor_lost")
        self.assertEqual(view["exception"]["severity"], "blocked")
        self.assertEqual(view["exception"]["label"], "执行端失联")
        self.assertEqual(view["lifecycle_column"], "in_progress")

    def test_review_rejected(self):
        # 评审未通过·待修复：in_review 且当前周期 review_conclusion=
        # changes_requested → kind=review_rejected，关联 fix_task_id。
        task_id = "taskcol-revrej01"
        self.add_task(task_id, remote_status="in-review")
        self.set_task_status(task_id, "in_review")
        report = self.write_report(task_id)
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "changes_requested",
             "fix_task_id": "fixtask0000000000aa", "review_key": "rk-col"},
        )
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "failed")
        self.assertEqual(view["exception"]["kind"], "review_rejected")
        self.assertEqual(view["exception"]["label"], "评审未通过·待修复")
        self.assertEqual(view["exception"]["severity"], "blocked")
        self.assertEqual(
            view["exception"]["fix_task_id"], "fixtask0000000000aa"
        )
        self.assertEqual(view["lifecycle_column"], "in_review")

    def test_fix_task_id_only_for_review_rejected(self):
        # 非 review_rejected 的异常 fix_task_id 恒为 null。
        task_id = "taskcol-nofix0001"
        self.set_needs_user(
            task_id, send_state="sent", failure_code="execution_failed",
            failure_stage="running", failure_reason="x", blocked_summary="x",
        )
        self.assertIsNone(self.task(task_id)["exception"]["fix_task_id"])


class TestMutualExclusionAndCounts(ColumnCase):
    def test_each_task_one_column_and_counts(self):
        specs = []
        # 正常列（各一）。
        self.add_task("taskmx-needsplan1")
        specs.append(("taskmx-needsplan1", "needs_plan", None))
        self.add_task("taskmx-ready00001")
        self.set_task_status("taskmx-ready00001", "ready")
        specs.append(("taskmx-ready00001", "todo", None))
        self.add_task("taskmx-inreview01", remote_status="in-review")
        self.set_task_status("taskmx-inreview01", "in_review")
        specs.append(("taskmx-inreview01", "in_review", None))
        self.add_task("taskmx-done000001")
        self.set_task_status("taskmx-done000001", "done")
        specs.append(("taskmx-done000001", "done", None))
        # 六类异常（各一）；executor_lost 是唯一 running 任务，领取无歧义。
        self.set_needs_user(
            "taskmx-failed001", send_state="sent",
            failure_code="execution_failed", failure_stage="running",
            failure_reason="x", blocked_summary="x",
        )
        specs.append(("taskmx-failed001", "failed", "failed"))
        self.set_needs_user(
            "taskmx-sendunk01", send_state="unknown",
            failure_code="send_unknown", failure_stage="submitting",
            failure_reason="x", blocked_summary="x",
        )
        specs.append(("taskmx-sendunk01", "failed", "send_unknown"))
        self.running_task("taskmx-lost00001")
        claim = self.claim_stage("monitor", "kaneo-monitor")
        self.expire_lease(claim["request_id"])
        specs.append(("taskmx-lost00001", "failed", "executor_lost"))
        self.add_task("taskmx-revrej01", remote_status="in-review")
        self.set_task_status("taskmx-revrej01", "in_review")
        report = self.write_report("taskmx-revrej01")
        self.call(
            "register-review",
            {"task_id": "taskmx-revrej01", "report_path": str(report),
             "conclusion": "changes_requested",
             "fix_task_id": "fixtask0000000000bb", "review_key": "rk-mx"},
        )
        specs.append(("taskmx-revrej01", "failed", "review_rejected"))
        self.set_needs_user(
            "taskmx-obsblk01", send_state="sent",
            failure_code="observation_blocked", failure_stage="running",
            failure_reason="x", blocked_summary="x",
        )
        specs.append(("taskmx-obsblk01", "failed", "observation_blocked"))

        # 每任务恰归入一列；exception.kind 与预期一致（互斥分类）。
        by_column = Counter()
        for task_id, column, kind in specs:
            view = self.task(task_id)
            self.assertEqual(view["board_column"], column, task_id)
            if kind is None:
                self.assertIsNone(view["exception"], task_id)
            else:
                self.assertEqual(view["exception"]["kind"], kind, task_id)
            by_column[column] += 1
        self.assertEqual(by_column["failed"], 5)
        self.assertEqual(sum(by_column.values()), len(specs))

        # get-task 与 list-tasks 同任务同列（派生一致，第六列=failed）。
        listed = {
            t["task_id"]: t for t in self.call("list-tasks")["tasks"]
        }
        self.assertEqual(len(listed), len(specs))
        for task_id, column, _kind in specs:
            self.assertEqual(listed[task_id]["board_column"], column, task_id)

        # overview counts.board 与 list-tasks 分组一致（失联以外的异常均为行内
        # 可判；失联仅 task_summary 全量派生能判，overview 行内派生记 in_progress，
        # 见 captain 报告中的 cmd_read 共享文件限制）。
        grouped = Counter(
            t["board_column"] for t in self.call("list-tasks")["tasks"]
        )
        board = self.call("overview")["counts"]["board"]
        # needs_plan/todo/in_review/done 与 failed(非失联) 两边一致。
        self.assertEqual(board["needs_plan"], grouped["needs_plan"])
        self.assertEqual(board["todo"], grouped["todo"])
        self.assertEqual(board["in_review"], grouped["in_review"])
        self.assertEqual(board["done"], grouped["done"])


class TestHistoryFailureNotPolluteRecovery(ColumnCase):
    def test_resolve_retry_clears_exception(self):
        # 历史失败不污染恢复后任务：resolve 清阻塞字段后归列回 lifecycle，
        # exception 归 null（本地分类不修改 Kaneo 状态）。
        task_id = "taskcl-recover01"
        self.add_task(task_id)
        self.set_task_status(
            task_id, "needs_user", send_state="not_sent",
            failure_code="execution_failed", failure_stage="running",
            failure_reason="boom", blocked_summary="boom",
        )
        self.assertEqual(self.task(task_id)["board_column"], "failed")
        self.call("resolve", {"task_id": task_id, "action": "retry"})
        view = self.task(task_id)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["board_column"], "todo")
        self.assertEqual(view["lifecycle_column"], "todo")
        self.assertIsNone(view["exception"])
        self.assertIsNone(view["blocked_reason"])
        self.assertIsNone(view["failure_code"])

    def test_extra_json_history_not_used_for_column(self):
        # extra_json 历史 review_conclusion 不参与归列：非 in_review 状态即使
        # extra 里残留 changes_requested 也不进 failed。
        task_id = "taskcl-extra01"
        self.add_task(task_id)
        self.set_task_status(task_id, "running", sent=1, send_state="sent")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE tasks SET extra_json=? WHERE task_id=?",
            (util.jdump({"review_conclusion": "changes_requested"}), task_id),
        )
        conn.commit()
        conn.close()
        view = self.task(task_id)
        self.assertEqual(view["board_column"], "in_progress")
        self.assertIsNone(view["exception"])


if __name__ == "__main__":
    unittest.main()
