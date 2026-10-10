"""resume_auto 闭环：take_over → resume_auto → 自动交付（scenarios.md 场景 16）。

``resolve {action:"dismiss"}`` 把自动化管理的任务转入人工接管
（``automation.manual_takeover=True`` + ``delivery.mode="manual"`` + 暂停自动推进）。
此前没有反向入口：引擎对该任务恒为 NOOP，``delivery-publish`` 也明确阻断 manual——
即计划 D4 诊断的「人工介入后无法回到自动」单向门。

``restore-task {resume_auto:true}``（桌面端「恢复自动推进」按钮已在调的同一命令，
见 AppViewModel.resumeAuto）现在是这个反向入口：清 ``manual_takeover``、交付模式在
PR 身份齐全时回 ``pr``、清 ``auto_advance_paused``、落 ``automation_resumed`` 事件。
接管是用户决定，交回同样必须是显式动作——普通恢复与幂等恢复都不动自动化归属。

依据：docs/state-model.md §10「resume_auto｜已转人工任务，环境已修复｜恢复自动推进，
继续原 phase｜restore-task {resume_auto:true}」与 docs/protocol.md §5.27.4。

Run: ``cd core && python3 -m pytest tests/test_resume_auto_takeover.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import loop  # noqa: E402

PROJECT_ID = "ryryo1reph1xfemqtwkjyiiz"
PROJECT_NAME = "Feedback"

_PR_IDENTITY = {
    "branch": "kaneo/taskresumeauto00001",
    "base_branch": "main",
    "base_sha": "0" * 40,
    "head_sha": "1" * 40,
    "worktree_path": "/tmp/kaneo-worktree",
}


class ResumeAutoCase(KanpilotTestCase):
    """共享基座：造一个自动化管理的 ready 任务，并可模拟/执行接管。"""

    def _managed(self, task_id, status="ready"):
        remote_task = self.fake.add_task(task_id, PROJECT_ID)
        self.make_repo(PROJECT_NAME)
        self.sync_task(remote_task, project_name=PROJECT_NAME)
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
        """模拟 resolve {action:"dismiss"} 的写入（不依赖 needs_user 前置状态）。

        每次写后都重新读行：``db.set_extra`` 用传入行的 extra_json 全量覆写，
        用写前的过期行会把刚才的 ``manual_takeover`` 抹掉。
        """
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                core.update_automation(conn, task, util.now_iso(),
                                       manual_takeover=True, plan_wanted=False)
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                delivery = dict(core.delivery_of(task), mode="manual")
                if pr_identity:
                    delivery.update(_PR_IDENTITY)
                db.set_extra(conn, task, util.now_iso(), delivery=delivery)
            if paused:
                with db.write_txn(conn):
                    db.update_task(conn, task_id, util.now_iso(),
                                   auto_advance_paused=1)
        finally:
            conn.close()

    def _view(self, task_id):
        conn = db.connect(self.db_path)
        try:
            task = db.require_task(conn, task_id)
            return {
                "automation": core.automation_of(task),
                "delivery": core.delivery_of(task),
                "paused": db.task_auto_paused(task),
                "status": task["status"],
                "save_state": db.task_save_state(task),
            }
        finally:
            conn.close()

    def _events(self, task_id, kind):
        rows = self.call("list-events", {"task_id": task_id, "limit": 200})["events"]
        return [e for e in rows if e["kind"] == kind]

    def _dispatch_requests(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT request_id FROM requests WHERE task_id=? AND kind='dispatch' "
                "AND status IN ('queued','claimed')", (task_id,)).fetchall()
        finally:
            conn.close()

    def _actions(self, task_id):
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        return [a for a in summary["actions"] if a.get("task_id") == task_id]


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
        self.assertFalse(after["automation"]["manual_takeover"], after)
        self.assertEqual(after["delivery"]["mode"], "pr")
        self.assertFalse(after["paused"])
        self.assertTrue(self._events(task_id, "automation_resumed"))

    def test_plain_restore_keeps_manual_takeover(self):
        task_id = "taskresumeauto00002"
        self._managed(task_id)
        self._take_over(task_id)
        # 普通恢复（无 resume_auto）不授权自动工作：接管归属不动。
        self.call("restore-task", {"task_id": task_id})
        view = self._view(task_id)
        self.assertTrue(view["automation"]["manual_takeover"], view)
        self.assertEqual(view["delivery"]["mode"], "manual")
        self.assertFalse(self._events(task_id, "automation_resumed"))

    def test_manual_delivery_without_pr_identity_is_not_switched(self):
        task_id = "taskresumeauto00003"
        self._managed(task_id)
        self._take_over(task_id, pr_identity=False)
        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        view = self._view(task_id)
        # 接管已交回，但交付模式保持 manual——不造「PR 模式但不可发布」的半态。
        self.assertFalse(view["automation"]["manual_takeover"], view)
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


class TestResumeAutoReachability(ResumeAutoCase):
    """``resume-auto`` must be offered for every takeover, not only review ones.

    ``resolve {action:"dismiss"}`` sets ``auto_advance_paused=1`` only for
    review-block failures; a non-review takeover lands in ``ready`` unpaused.
    ``derive.allowed_actions`` used to gate ``resume-auto`` on the pause alone,
    so the desktop button never appeared for those tasks — the reverse entry
    existed but was unreachable, leaving D4's one-way door half open.
    """

    def test_resume_auto_offered_for_unpaused_non_review_takeover(self):
        task_id = "taskresumeauto00009"
        self._managed(task_id)
        self._take_over(task_id, paused=False)
        view = self._view(task_id)
        self.assertTrue(view["automation"]["manual_takeover"])
        self.assertFalse(view["paused"])
        self.assertEqual(view["status"], "ready")
        actions = self.call("get-task", {"task_id": task_id})["task"]["allowed_actions"]
        self.assertIn("resume-auto", actions)

    def test_resume_auto_offered_for_paused_review_takeover(self):
        task_id = "taskresumeauto00010"
        self._managed(task_id)
        self._take_over(task_id, paused=True)
        actions = self.call("get-task", {"task_id": task_id})["task"]["allowed_actions"]
        self.assertIn("resume-auto", actions)

    def test_resume_auto_absent_without_takeover_or_pause(self):
        task_id = "taskresumeauto00011"
        self._managed(task_id)
        actions = self.call("get-task", {"task_id": task_id})["task"]["allowed_actions"]
        self.assertNotIn("resume-auto", actions)


class TestResumeAutoFromArchive(ResumeAutoCase):
    def test_archived_takeover_resume_restores_active_and_automation(self):
        task_id = "taskresumeauto00005"
        self._managed(task_id)
        self._take_over(task_id)
        self.call("archive-task", {"task_id": task_id})
        self.assertEqual(self._view(task_id)["save_state"], "archived")

        result = self.call("restore-task",
                           {"task_id": task_id, "resume_auto": True})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        view = self._view(task_id)
        self.assertEqual(view["save_state"], "active")
        self.assertFalse(view["automation"]["manual_takeover"], view)
        self.assertEqual(view["delivery"]["mode"], "pr")
        self.assertFalse(view["paused"])
        self.assertTrue(self._events(task_id, "automation_resumed"))


class TestResumeAutoEngineClosure(ResumeAutoCase):
    def _live(self):
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT_ID, "repository": str(self.repos_root / PROJECT_NAME),
            "enabled": True, "expected_revision": revision})
        self.call("set-project-engine-mode",
                  {"project_id": PROJECT_ID, "engine_mode": "live"})

    def test_engine_does_not_drive_a_taken_over_task(self):
        task_id = "taskresumeauto00006"
        self._managed(task_id)
        self._live()
        self._take_over(task_id)
        # 接管中：is_automation_managed 为 False，候选阶段即被过滤。
        actions = self._actions(task_id)
        self.assertEqual(actions, [])
        self.assertEqual(self._dispatch_requests(task_id), [])

    def test_engine_drives_the_task_again_after_resume(self):
        task_id = "taskresumeauto00007"
        self._managed(task_id)
        self._live()
        self._take_over(task_id)
        self.assertEqual(self._actions(task_id), [])

        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        actions = self._actions(task_id)
        # 交回后引擎重新驱动：ready + 计划就绪 → 确认并安排执行（enqueue dispatch）。
        self.assertTrue(actions, actions)
        self.assertTrue(any(a["action"] == "confirm_plan" for a in actions), actions)
        self.assertTrue(any(a.get("command") == "enqueue" for a in actions), actions)

    def test_dismiss_then_resume_roundtrip(self):
        """经真实 resolve {action:"dismiss"} 写入，再 resume_auto 交回。"""
        task_id = "taskresumeauto00008"
        self._managed(task_id, status="needs_user")
        self.call("resolve", {"task_id": task_id, "action": "dismiss",
                              "note": "我自己来"})
        view = self._view(task_id)
        self.assertTrue(view["automation"]["manual_takeover"])
        self.assertEqual(view["delivery"]["mode"], "manual")
        self.assertEqual(view["status"], "ready")

        self.call("restore-task", {"task_id": task_id, "resume_auto": True})
        view = self._view(task_id)
        self.assertFalse(view["automation"]["manual_takeover"], view)
        self.assertEqual(view["status"], "ready")
        self.assertFalse(view["paused"])
        self.assertTrue(self._events(task_id, "automation_resumed"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
