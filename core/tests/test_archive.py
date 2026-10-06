"""归档/回收站/恢复 v1.3（protocol.md §5.15.2/§5.15.3 / state-model.md §2.1）：
save_state 转换、禁用条件、排队取消与「已被领取」竞争、幂等、调度入口排除
（enqueue/resolve/sync-tasks）、恢复后自动推进暂停与归列、重启后持久化。
本地归档/恢复只影响调度台，不修改 Kaneo 状态。
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import util  # noqa: E402

PROJECT_ID = "ryryo1reph1xfemqtwkjyiiz"


class ArchiveCase(KanpilotTestCase):
    def add_task(self, task_id, remote_status="to-do"):
        task = self.fake.add_task(task_id, PROJECT_ID, status=remote_status)
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        return task_id

    def save_state(self, task_id):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT save_state, saved_at, auto_advance_paused FROM tasks "
            "WHERE task_id=?",
            (task_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def request_status(self, request_id):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        conn.close()
        return row["status"] if row else None

    def events(self, task_id, kind=None):
        out = self.call(
            "list-events", {"task_id": task_id, "limit": 200}
        )["events"]
        return [e for e in out if kind is None or e["kind"] == kind]

    def insert_claimed_request(
        self, task_id, kind="monitor", expired=False, lease_seconds=1200
    ):
        """直接插入一条 claimed 请求（模拟执行端领取；expired 控制租约是否过期）。"""
        conn = sqlite3.connect(str(self.db_path))
        now = util.now_dt()
        if expired:
            lease = (now - timedelta(seconds=60)).isoformat()
        else:
            lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        now_iso = util.to_iso(now)
        request_id = util.new_uuid()
        conn.execute(
            "INSERT INTO requests(request_id, idempotency_key, task_id, stage, "
            "kind, status, priority, run_id, claimed_by, claimed_at, "
            "heartbeat_at, lease_expires_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, f"{kind}:{task_id}:comp", task_id, kind, kind,
             "claimed", 100, "run-comp", "exec-comp", now_iso, now_iso,
             lease, now_iso, now_iso),
        )
        conn.commit()
        conn.close()
        return request_id


class TestArchivePreconditions(ArchiveCase):
    def test_blocked_active_statuses(self):
        for task_id, status in [
            ("taskarc-planning1", "planning"),
            ("taskarc-dispatch1", "dispatching"),
            ("taskarc-prepared1", "prepared"),
            ("taskarc-submittin1", "submitting"),
            ("taskarc-running01", "running"),
            ("taskarc-inreview1", "in_review"),
        ]:
            self.add_task(task_id)
            self.set_task_status(task_id, status)
            exc = self.call_err("archive-task", {"task_id": task_id})
            self.assertEqual(exc.code, "conflict", task_id)
            self.assertEqual(self.save_state(task_id)["save_state"], "active")
            exc = self.call_err("trash-task", {"task_id": task_id})
            self.assertEqual(exc.code, "conflict", task_id)

    def test_blocked_unresolved_lost(self):
        task_id = "taskarc-lost0001"
        self.add_task(task_id)
        # 可归档状态（needs_plan），但存在未解除失联请求 → 命中失联禁用条件。
        self.set_task_status(task_id, "needs_plan")
        self.insert_claimed_request(task_id, kind="plan", expired=True)
        exc = self.call_err("archive-task", {"task_id": task_id})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("失联", exc.message)
        self.assertEqual(self.save_state(task_id)["save_state"], "active")

    def test_blocked_send_unknown(self):
        task_id = "taskarc-sendunk1"
        self.add_task(task_id)
        self.set_task_status(
            task_id, "needs_user", send_state="unknown",
            failure_code="send_unknown", failure_stage="submitting",
            failure_reason="unsure", blocked_summary="unsure",
        )
        exc = self.call_err("archive-task", {"task_id": task_id})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("发送", exc.message)

    def test_allowed_states(self):
        for task_id, status, extra in [
            ("taskarc-needsplan", "needs_plan", {}),
            ("taskarc-ready0001", "ready", {}),
            ("taskarc-done000001", "done", {}),
            ("taskarc-nublock01", "needs_user",
             {"send_state": "sent", "failure_code": "execution_failed",
              "failure_stage": "running", "failure_reason": "x",
              "blocked_summary": "x"}),
        ]:
            self.add_task(task_id)
            self.set_task_status(task_id, status, **extra)
            result = self.call("archive-task", {"task_id": task_id})
            self.assertTrue(result["ok"], task_id)
            self.assertEqual(
                self.save_state(task_id)["save_state"], "archived", task_id
            )


class TestArchiveTrashRestore(ArchiveCase):
    def test_archive_cancels_queued_and_emits_event(self):
        task_id = "taskarc2-ready01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        enq = self.call("enqueue", {"task_id": task_id, "kind": "dispatch"})
        request_id = enq["request"]["request_id"]
        result = self.call("archive-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        self.assertIn(request_id, result["cancelled_requests"])
        # queued 请求被取消 + request_cancelled 事件。
        self.assertEqual(self.request_status(request_id), "cancelled")
        # save_state/saved_at 写入 + task_archived 事件。
        state = self.save_state(task_id)
        self.assertEqual(state["save_state"], "archived")
        self.assertIsNotNone(state["saved_at"])
        self.assertTrue(self.events(task_id, "task_archived"))
        # 返回刷新后 task，含新字段。
        self.assertEqual(result["task"]["save_state"], "archived")
        self.assertIn("lifecycle_column", result["task"])
        self.assertIn("exception", result["task"])

    def test_archive_idempotent(self):
        task_id = "taskarc2-idem001"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        events_before = len(self.events(task_id, "task_archived"))
        result = self.call("archive-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertTrue(result["deduplicated"])
        # 幂等不重复写事件。
        self.assertEqual(
            len(self.events(task_id, "task_archived")), events_before
        )

    def test_trash_sets_state_and_event(self):
        task_id = "taskarc2-trash01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        result = self.call("trash-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertEqual(self.save_state(task_id)["save_state"], "trashed")
        self.assertTrue(self.events(task_id, "task_trashed"))

    def test_restore_defaults_paused(self):
        task_id = "taskarc2-rest01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        result = self.call("restore-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        state = self.save_state(task_id)
        self.assertEqual(state["save_state"], "active")
        self.assertIsNone(state["saved_at"])
        # 恢复后默认暂停自动推进。
        self.assertEqual(state["auto_advance_paused"], 1)
        self.assertTrue(self.events(task_id, "task_restored"))
        self.assertIn("resume-auto", result["task"]["allowed_actions"])

    def test_restore_resume_auto(self):
        task_id = "taskarc2-resume1"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("trash-task", {"task_id": task_id})
        self.call(
            "restore-task", {"task_id": task_id, "resume_auto": True}
        )
        self.assertEqual(
            self.save_state(task_id)["auto_advance_paused"], 0
        )

    def test_restore_active_is_idempotent(self):
        task_id = "taskarc2-idemact1"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        result = self.call("restore-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertTrue(result["deduplicated"])
        self.assertEqual(self.save_state(task_id)["save_state"], "active")


class TestClaimedCompetition(ArchiveCase):
    def test_archive_aborts_when_claimed_not_cancel_queued(self):
        # 排队取消与「已被领取」竞争：事务内复核，已被 claimed → conflict，
        # 不取消 queued、不改保存状态。
        task_id = "taskarc3-comp01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        enq = self.call("enqueue", {"task_id": task_id, "kind": "dispatch"})
        queued_id = enq["request"]["request_id"]
        # 执行端已领取另一条请求（租约未过期，非失联）。
        self.insert_claimed_request(task_id, kind="monitor", expired=False)
        exc = self.call_err("archive-task", {"task_id": task_id})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("领取", exc.message)
        # 未取消 queued、未改保存状态。
        self.assertEqual(self.request_status(queued_id), "queued")
        self.assertEqual(self.save_state(task_id)["save_state"], "active")
        self.assertFalse(self.events(task_id, "task_archived"))


class TestSchedulingExclusions(ArchiveCase):
    def test_enqueue_archived_conflict(self):
        task_id = "taskarc4-enq01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        exc = self.call_err("enqueue", {"task_id": task_id, "kind": "dispatch"})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("归档", exc.message)

    def test_enqueue_trashed_conflict(self):
        task_id = "taskarc4-enq02"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("trash-task", {"task_id": task_id})
        exc = self.call_err("enqueue", {"task_id": task_id, "kind": "dispatch"})
        self.assertEqual(exc.code, "conflict")

    def test_enqueue_paused_conflict(self):
        task_id = "taskarc4-enq03"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        self.call("restore-task", {"task_id": task_id})  # 默认暂停
        exc = self.call_err("enqueue", {"task_id": task_id, "kind": "dispatch"})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("暂停自动推进", exc.message)

    def test_resolve_non_active_conflict(self):
        task_id = "taskarc4-res01"
        self.add_task(task_id)
        self.set_task_status(
            task_id, "needs_user", send_state="sent",
            failure_code="execution_failed", failure_stage="running",
            failure_reason="x", blocked_summary="x",
        )
        self.call("archive-task", {"task_id": task_id})
        exc = self.call_err(
            "resolve", {"task_id": task_id, "action": "replan"}
        )
        self.assertEqual(exc.code, "conflict")

    def test_sync_skips_archived_no_reimport(self):
        # sync-tasks 对 save_state != active 任务整体跳过（不 upsert/不改状态/
        # 不建请求），防重导入。
        task_id = "taskarc4-sync01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        before = self.save_state(task_id)
        # 远端状态变为 done，并给新标题。
        self.fake.tasks[task_id]["status"] = "done"
        self.fake.tasks[task_id]["title"] = "changed title"
        result = self.sync_task(
            self.fake.tasks[task_id], project_name="Feedback",
            title="changed title",
        )
        # 该任务未被 upsert。
        self.assertNotIn(task_id, [c["task_id"] for c in result["changed"]])
        after = self.save_state(task_id)
        self.assertEqual(after["save_state"], "archived")
        # 状态/观测未被同步改动。
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, kaneo_status, title FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "ready")
        self.assertNotEqual(row["kaneo_status"], "done")
        self.assertNotEqual(row["title"], "changed title")

    def test_archived_not_in_default_list(self):
        task_id = "taskarc4-list01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        default_ids = {
            t["task_id"] for t in self.call("list-tasks")["tasks"]
        }
        self.assertNotIn(task_id, default_ids)
        archived_ids = {
            t["task_id"]
            for t in self.call(
                "list-tasks", {"save_state": "archived"}
            )["tasks"]
        }
        self.assertIn(task_id, archived_ids)


class TestRestoreReverifyAndColumn(ArchiveCase):
    def test_restore_recomputes_column_and_remote_observation(self):
        # 恢复后按核验后状态重新归列；needs_user 任务恢复后仍归 failed 列。
        task_id = "taskarc5-revcol1"
        self.add_task(task_id)
        self.set_task_status(
            task_id, "needs_user", send_state="sent",
            failure_code="execution_failed", failure_stage="running",
            failure_reason="x", blocked_summary="x",
        )
        self.call("archive-task", {"task_id": task_id})
        result = self.call("restore-task", {"task_id": task_id})
        task = result["task"]
        self.assertEqual(task["save_state"], "active")
        self.assertEqual(task["status"], "needs_user")
        self.assertEqual(task["board_column"], "failed")
        self.assertEqual(task["exception"]["kind"], "failed")
        # 恢复后默认暂停自动推进，不自动建监控/评审请求。
        self.assertEqual(self.save_state(task_id)["auto_advance_paused"], 1)

    def test_restore_remote_unreachable_warns_not_blocks(self):
        # 远端不可达 → warning，不阻断恢复。
        task_id = "taskarc5-unreach1"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        self.fake.fail_next = Exception("kaneo down")
        result = self.call("restore-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertEqual(self.save_state(task_id)["save_state"], "active")
        self.assertIn("warnings", result)


class TestPersistence(ArchiveCase):
    def test_state_survives_reconnect(self):
        # 归档/恢复后跨连接（重启）状态与归列保持一致。
        task_id = "taskarc6-persist1"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        self.call("archive-task", {"task_id": task_id})
        # 每次都经 cli.run 新建连接（等同重启）。
        again = self.task(task_id)
        self.assertEqual(again["save_state"], "archived")
        self.assertIn("restore", again["allowed_actions"])
        self.assertNotIn("enqueue", again["allowed_actions"])
        self.call("restore-task", {"task_id": task_id})
        restored = self.task(task_id)
        self.assertEqual(restored["save_state"], "active")
        self.assertEqual(restored["auto_advance_paused"], 1)


class TestBatchEligibilityAndExpectedSaveState(ArchiveCase):
    def test_move_out_eligibility(self):
        task_id = "taskarc7-elig01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")
        summary = self.task(task_id)
        self.assertIn("move_out_eligibility", summary)
        self.assertTrue(summary["move_out_eligibility"]["allowed"])
        self.assertIsNone(summary["move_out_eligibility"]["reason"])

        # running 状态禁止移出
        self.set_task_status(task_id, "running")
        summary = self.task(task_id)
        self.assertFalse(summary["move_out_eligibility"]["allowed"])
        self.assertIn("执行", summary["move_out_eligibility"]["reason"])

        # send_state=unknown 禁止移出
        self.set_task_status(task_id, "needs_user", send_state="unknown")
        summary = self.task(task_id)
        self.assertFalse(summary["move_out_eligibility"]["allowed"])
        self.assertIn("发送结果不明", summary["move_out_eligibility"]["reason"])

        # claimed 请求禁止移出
        self.set_task_status(task_id, "needs_user", send_state="sent")
        self.insert_claimed_request(task_id, kind="monitor", expired=False)
        summary = self.task(task_id)
        self.assertFalse(summary["move_out_eligibility"]["allowed"])
        self.assertIn("领取", summary["move_out_eligibility"]["reason"])

    def test_expected_save_state(self):
        task_id = "taskarc7-exp01"
        self.add_task(task_id)
        self.set_task_status(task_id, "ready")

        # 匹配 expected_save_state="active" 正常归档
        res = self.call("archive-task", {"task_id": task_id, "expected_save_state": "active"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["task"]["save_state"], "archived")

        # 已是 archived，幂等传入 expected_save_state="active" 应成功
        res2 = self.call("archive-task", {"task_id": task_id, "expected_save_state": "active"})
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["deduplicated"])

        # 已是 archived，调用 trash-task 传入 expected_save_state="active" 应报 conflict
        exc = self.call_err("trash-task", {"task_id": task_id, "expected_save_state": "active"})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("冲突", exc.message)


if __name__ == "__main__":
    unittest.main()
