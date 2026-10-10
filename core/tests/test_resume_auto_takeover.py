"""resume_auto 闭环：take_over → resume_auto → 自动交付（scenarios.md 场景 16）。

``resolve {action:"dismiss"}`` 把自动化管理的任务转入人工接管
（``automation.manual_takeover=True`` + ``delivery.mode="manual"`` + 暂停自动推进），
此前没有反向入口：引擎对该任务恒为 NOOP，``delivery-publish`` 也明确阻断 manual。

``restore-task {resume_auto:true}``（桌面端「恢复自动推进」按钮已在调的同一命令）
现在是这个反向入口：清 ``manual_takeover``、交付模式在 PR 身份齐全时回 ``pr``、
清 ``auto_advance_paused``。接管是用户决定，交回同样必须是显式动作——普通恢复与
幂等恢复都不动自动化归属。

Run: ``cd core && python3 -m pytest tests/test_resume_auto_takeover.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import loop  # noqa: E402

PROJECT_ID = "ryryo1reph1xfemqtwkjyiiz"

_PR_IDENTITY = {
    "branch": "kaneo/taskresumeauto00001",
    "base_branch": "main",
    "base_sha": "0" * 40,
    "head_sha": "1" * 40,
    "worktree_path": "/tmp/kaneo-worktree",
}


class ResumeAutoCase(KanpilotTestCase):
    def _managed(self, task_id, status="ready"):
        self.add_task(task_id)
        self.set_task_status(task_id, status)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
        finally:
            conn.close()
        return task_id

    def _take_over(self, task_id, *, pr_identity=True, paused=True):
        """模拟 resolve {action:"dismiss"} 的写入（不依赖 needs_user 前置状态）。"""
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                core.update_automation(conn, task, util.now_iso(),
                                       manual_takeover=True, plan_wanted=False)
                delivery = dict(core.delivery_of(task), mode="manual")
                if pr_identity:
                    delivery.update(_PR_IDENTITY)
                db.set_extra(conn, task, util.now_iso(), delivery=delivery)
                if paused:
                    db.update_task(conn, task_id, util.now_iso(),
                                   auto_advance_paused=1)
        finally:
            conn.close()

    def _view(self, task_id):
        conn = db.connect(self.db_path)
        try:
            task = db.require_task(conn, task_id)
            return {"automation": core.automation_of(task),
                    "delivery": core.delivery_of(task),
                    "paused": db.task_auto_paused(task),
                    "status": task["status"],
                    "extra": db.task_extra(task)}
        finally:
            conn.close()

    def _dispatch_requests(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT request_id FROM requests WHERE task_id=? AND kind='dispatch' "
                "AND status IN ('queued','claimed')", (task_id,)).fetchall()
        finally:
            conn.close()


class TestResumeAutoClearsTakeover(ResumeAutoCase):
    def test_clears_takeover_and_restores_pr_delivery(self):
        task_id = "taskresumeauto00001"
        self._managed(task_id)
        self._take_over(task_id)
        before = self._view(task_id)
        self.assertTrue(before["automation"]["manual_takeover"])
        self.assertEqual(before["delivery"]["mode"], "manual")
        self.assertTrue(before["paused"])

        result = self.call("restore-task",
                           {"task_id": task_id, "resume_auto": True})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])

        after = self._view(task_id)
        self.assertNotIn("manual_takeover", after["automation"])
        self.assertEqual(after["delivery"]["mode"], "pr")
        self.assertFalse(after["paused"])
        self.assertTrue(self.events(task_id, "automation_resumed"))

    def test_plain_restore_keeps_manual_takeover(self):
        task_id = "taskresumeauto00002"
        self._managed(task_id)
        self._take_over(task_id)
        # 普通恢复（无 resume_auto）不授权自动工作：接管归属不动。
        self.call("restore-task", {"task_id": task_id})
        view = self._view(task_id)
        self.assertTrue(view["automation"]["manual_takeover"])
        self.assertEqual(view["delivery"]["mode"], "manual")
        self.assertFalse(self.events(task_id, "automation_resumed"))

    def test_manual_delivery_without_pr_identity_is_not_switched(self):
        task_id = "taskresumeauto00003"
        self._managed(task_id)
        self._take_over(task_id, pr_identity=False)
        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        view = self._view(task_id)
        # 接管已交回，但交付模式保持 manual——不造「PR 模式但不可发布」的半态。
        self.assertNotIn("manual_takeover", view["automation"])
        self.assertEqual(view["delivery"]["mode"], "manual")

    def test_resume_auto_is_idempotent(self):
        task_id = "taskresumeauto00004"
        self._managed(task_id)
        self._take_over(task_id)
        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        conn = db.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE task_id=? "
                "AND kind='automation_resumed'", (task_id,)).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 1)


class TestResumeAutoFromArchive(ResumeAutoCase):
    def test_archived_takeover_resume_restores_active_and_automation(self):
        task_id = "taskresumeauto00005"
        self._managed(task_id)
        self._take_over(task_id)
        self.call("archive-task", {"task_id": task_id})
        self.assertEqual(self.save_state(task_id)["save_state"], "archived")

        result = self.call("restore-task",
                           {"task_id": task_id, "resume_auto": True})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        view = self._view(task_id)
        self.assertEqual(self.save_state(task_id)["save_state"], "active")
        self.assertNotIn("manual_takeover", view["automation"])
        self.assertEqual(view["delivery"]["mode"], "pr")
        self.assertFalse(view["paused"])


class TestResumeAutoEngineClosure(ResumeAutoCase):
    def _live(self):
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT_ID, "repository": str(self.repos_root / "Feedback"),
            "enabled": True, "expected_revision": revision})
        self.call("set-project-engine-mode",
                  {"project_id": PROJECT_ID, "engine_mode": "live"})

    def test_engine_drives_the_task_again_after_resume(self):
        task_id = "taskresumeauto00006"
        self._managed(task_id)
        self._live()
        self._take_over(task_id)

        # 接管中：引擎不驱动（decide → not_automation_managed 同源判据）。
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        mine = [a for a in summary["actions"] if a.get("task_id") == task_id]
        self.assertTrue(mine)
        self.assertTrue(all(a["action"] == "noop" for a in mine), mine)
        self.assertEqual(self._dispatch_requests(task_id), [])

        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        mine = [a for a in summary["actions"] if a.get("task_id") == task_id]
        self.assertTrue(mine)
        self.assertTrue(any(a["action"] == "enqueue_dispatch" for a in mine), mine)
        self.assertEqual(len(self._dispatch_requests(task_id)), 1)

    def test_dismiss_then_resume_roundtrip(self):
        """经真实 resolve {action:"dismiss"} 写入，再 resume_auto 交回。"""
        task_id = "taskresumeauto00007"
        self._managed(task_id, status="needs_user")
        self.call("resolve", {"task_id": task_id, "action": "dismiss",
                              "note": "我自己来"})
        view = self._view(task_id)
        self.assertTrue(view["automation"]["manual_takeover"])
        self.assertEqual(view["delivery"]["mode"], "manual")
        self.assertEqual(view["status"], "ready")

        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        view = self._view(task_id)
        self.assertNotIn("manual_takeover", view["automation"])
        self.assertEqual(view["status"], "ready")
        self.assertFalse(view["paused"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
