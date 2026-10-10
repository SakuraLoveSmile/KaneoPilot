"""Tests for `session-view` (protocol §5.27.5, minimal read-only status).

Run: ``cd core && python3 -m pytest tests/test_engine_session_view.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import db, util  # noqa: E402


class TestSessionView(KanpilotTestCase):
    def _task_with_round(self, task_id, backend, *, session_id="sess-1",
                         run_status="running", status="running", **task_fields):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        run_id = "run-" + task_id
        self.set_task_status(tid, status, run_id=run_id, **task_fields)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "INSERT INTO execution_rounds("
                    "task_id, run_id, backend, session_id, run_status, "
                    "save_state, created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                    (tid, run_id, backend, session_id, run_status,
                     util.now_iso(), util.now_iso()))
        finally:
            conn.close()
        return tid

    def test_live_backend_connection_ok(self):
        tid = self._task_with_round("tasksvA00000000000001", "pi_desktop")
        s = self.call("session-view", {"task_id": tid})["session"]
        self.assertEqual(s["task_id"], tid)
        self.assertEqual(s["run_id"], "run-tasksvA00000000000001")
        self.assertEqual(s["backend"], "pi_desktop")
        self.assertEqual(s["run_status"], "running")
        self.assertEqual(s["connection"]["state"], "ok")
        self.assertIsInstance(s["allowed_actions"], list)

    def test_old_backends_unavailable(self):
        for backend in ("dsh", "headless", "atif"):
            with self.subTest(backend=backend):
                tid = self._task_with_round("tasksvold" + backend + "0000001", backend)
                s = self.call("session-view", {"task_id": tid})["session"]
                self.assertEqual(s["connection"]["state"], "unavailable")
                self.assertEqual(s["connection"]["detail"], "旧后端会话已弃用")
                self.assertEqual(s["state"], "unavailable")

    def test_no_session_degraded(self):
        tid = self._task_with_round("tasksvnoses00000000001", "pi_desktop", session_id=None)
        s = self.call("session-view", {"task_id": tid})["session"]
        self.assertEqual(s["connection"]["state"], "degraded")

    def test_needs_user_blocked_reason(self):
        tid = self._task_with_round("tasksvnu00000000000001", "pi_desktop",
                                    status="needs_user", blocked_summary="stuck")
        s = self.call("session-view", {"task_id": tid})["session"]
        self.assertEqual(s["blocked_reason"], "stuck")

    def test_status_only_has_no_cursor(self):
        tid = self._task_with_round("tasksvso00000000000001", "pi_desktop")
        s = self.call("session-view", {"task_id": tid, "status_only": True})["session"]
        self.assertNotIn("cursor", s)

    def test_task_not_found(self):
        exc = self.call_err("session-view", {"task_id": "does-not-exist"})
        self.assertEqual(exc.code, "not_found")

    def test_requires_task_or_run(self):
        exc = self.call_err("session-view", {})
        self.assertEqual(exc.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
