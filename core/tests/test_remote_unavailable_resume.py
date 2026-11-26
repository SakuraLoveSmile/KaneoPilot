"""remote_unavailable 的「继续」入口：只有 monitor 阶段回 running，其余阶段拒绝。"""
from kanpilot import core, db, derive
import test_engine_recovery as fixture


class TestRemoteUnavailableResume(fixture.TestEngineRecovery):
    def block_on_monitor(self, *, managed=True):
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso,
                               manual_takeover=not managed)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       status="needs_user", stage="monitor", failure_code="remote_unavailable",
                       failure_stage="monitor", failure_reason="Kaneo 请求失败: URLError",
                       blocked_summary="Kaneo 请求失败: URLError", blocked_at=self.ctx.now_iso)
        return self.task()

    def test_monitor_remote_unavailable_resumes_original_run(self):
        self.block_on_monitor()
        self.assertIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        result = self.call("resolve", {"task_id": self.task_id, "action": "resume"})
        self.assertEqual((result["task_status"], result["request"]["kind"]), ("running", "monitor"))
        task = self.task()
        self.assertEqual(task["run_id"], self.run_id)
        self.assertEqual(task["stage"], "monitor")
        self.assertIsNone(task["failure_code"])
        request = db.get_request(self.ctx.conn, result["request"]["request_id"])
        self.assertEqual(request["requested_by"], "user")

    def test_review_stage_remote_unavailable_never_exposes_resume(self):
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       status="needs_user", stage="review", failure_code="remote_unavailable",
                       failure_stage="review", failure_reason="Kaneo 请求失败: URLError")
        self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        error = self.call_err("resolve", {"task_id": self.task_id, "action": "resume"})
        self.assertEqual(error.code, "conflict")

    def test_manual_takeover_task_can_still_resume(self):
        self.block_on_monitor(managed=False)
        before = core.automation_of(self.task())
        result = self.call("resolve", {"task_id": self.task_id, "action": "resume"})
        self.assertEqual((result["task_status"], result["request"]["kind"]), ("running", "monitor"))
        self.assertEqual(core.automation_of(self.task()), before)
        self.assertEqual(self.task()["run_id"], self.run_id)


for _name in fixture.TestEngineRecovery.__dict__:
    if _name.startswith("test_") and _name not in TestRemoteUnavailableResume.__dict__:
        setattr(TestRemoteUnavailableResume, _name, None)
