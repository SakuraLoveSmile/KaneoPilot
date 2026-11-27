"""begin-send 幂等与失败分流（kp-nx01）。

现场（2026-11-27 MediaPlayer 播放页快进快退）：Kaneo 状态写入成功、紧接着的回读
连接层断链，核心按 fail-closed 判成 send_state=unknown，任务卡在 needs_user 且
「继续」被拒。这里固定三条行为：远端已切换时不再写、单次回读断链可自愈、写入
确定没生效时按未发送收尾。
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import bridge, cmd_dispatch, remote


class _Endpoint:
    """在 FakeKaneoClient 外面注入连接层故障，只影响请求的传输层。"""

    def __init__(self, inner):
        self.inner = inner
        self.fail_writes = False
        self.fail_reads_after_write = False
        self.read_limit = 0
        self.read_failures = 0
        self.written = False

    def request(self, path, method="GET", body=None):
        if method == "PUT" and self.fail_writes:
            raise bridge.BridgeError(
                f"Kaneo 请求失败: URLError(Connection reset by peer) PUT {path}")
        payload = self.inner.request(path, method, body)
        if method == "PUT":
            self.written = True
        elif (self.written and self.fail_reads_after_write
              and self.read_failures < self.read_limit):
            self.read_failures += 1
            raise bridge.BridgeError(
                f"Kaneo 请求失败: URLError(timed out) GET {path}")
        return payload


class BeginSendRecovery(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.call("set-exec-config", {"backend": "codex", "codex": {
            "model": "gpt-6.1-sol", "reasoning_effort": "high"}})
        self.endpoint = _Endpoint(self.fake)
        remote.set_client_factory(lambda: self.endpoint)
        sleep = patch.object(cmd_dispatch.time, "sleep", lambda *_: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def _prepared(self, *, automation=False):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        if automation:
            self.seed_auto_confirmation(task_id)
        self.enqueue_dispatch(task_id)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        view = self.task(task_id)
        self.call("dispatch-prepare", {
            "task_id": task_id, "run_id": claim["run_id"],
            "evidence": self.valid_evidence(view, fixture["plan_path"])})
        return task_id, claim["run_id"]

    def _puts(self):
        return [entry for entry in self.fake.requests_log if entry[0] == "PUT"]

    def _requests_of(self, task_id, kind):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "select status from requests where task_id=? and kind=? order by rowid",
                (task_id, kind)).fetchall()
        finally:
            conn.close()

    def _blocked_unknown(self):
        """远端已写入、但三次复读都断链 -> unknown。"""
        task_id, run_id = self._prepared(automation=True)
        self.endpoint.fail_reads_after_write = True
        self.endpoint.read_limit = 3
        exc = self.call_err(
            "dispatch-begin-send", {"task_id": task_id, "run_id": run_id})
        self.assertEqual(exc.code, "remote_conflict")
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["send_state"], "unknown")
        self.assertEqual(self.fake.tasks[task_id]["status"], "in-progress")
        self.endpoint.fail_reads_after_write = False
        return task_id, run_id

    def test_remote_already_in_progress_skips_second_write(self):
        task_id, run_id = self._prepared()
        self.fake.tasks[task_id]["status"] = "in-progress"
        sent = self.call(
            "dispatch-begin-send", {"task_id": task_id, "run_id": run_id})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(self._puts(), [])
        view = self.task(task_id)
        self.assertEqual(view["status"], "submitting")

    def test_single_read_blip_is_absorbed(self):
        task_id, run_id = self._prepared()
        self.endpoint.fail_reads_after_write = True
        self.endpoint.read_limit = 1
        sent = self.call(
            "dispatch-begin-send", {"task_id": task_id, "run_id": run_id})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(len(self._puts()), 1)
        self.assertEqual(self.task(task_id)["status"], "submitting")

    def test_failed_write_with_untouched_remote_is_not_sent(self):
        task_id, run_id = self._prepared()
        self.endpoint.fail_writes = True
        exc = self.call_err(
            "dispatch-begin-send", {"task_id": task_id, "run_id": run_id})
        self.assertEqual(exc.code, "remote_unavailable")
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["send_state"], "not_sent")
        self.assertEqual(self.fake.tasks[task_id]["status"], "to-do")
        self.assertIn("retry", view["allowed_actions"])

    def test_unreadable_verify_stays_unknown(self):
        task_id, _run_id = self._blocked_unknown()
        view = self.task(task_id)
        self.assertIn("restore", view["allowed_actions"])
        self.assertIn("mark_sent", view["allowed_actions"])

    def test_resume_requeues_dispatch_when_remote_moved(self):
        task_id, _run_id = self._blocked_unknown()
        self.call("resolve", {"task_id": task_id, "action": "resume"})
        view = self.task(task_id)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["send_state"], "not_sent")
        self.assertIn(("queued",), self._requests_of(task_id, "dispatch"))
        self.assertEqual(len(self._puts()), 1)

    def test_resume_rejected_when_remote_still_in_original_column(self):
        task_id, _run_id = self._blocked_unknown()
        self.fake.tasks[task_id]["status"] = "to-do"
        exc = self.call_err(
            "resolve", {"task_id": task_id, "action": "resume"})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("In Progress", str(exc))
        self.assertEqual(self.task(task_id)["status"], "needs_user")


if __name__ == "__main__":
    unittest.main()
