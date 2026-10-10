"""旧规划回收：隔离 SQLite/临时记录目录，模拟 PID，不调用 Plus。"""

import errno
from unittest.mock import patch

from kanpilot import cmd_session, db, round_store, session_control, util
from test_session import HeadlessCase, pi_job, simple_events, write_json


class TestStalePlanSession(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("stale-plan-task", None, status="planning")
        self.run_id = "cancelled-plan-run"
        self.request_id = "cancelled-plan-request"
        self.add_request(self.request_id, self.run_id, "cancelled")
        self.add_attempt(self.request_id, self.run_id, "cancelled", util.now_iso())
        self.add_request("new-plan-request", "new-plan-run", "claimed")
        self.add_attempt("new-plan-request", "new-plan-run", "claimed", None)
        self.job = pi_job(
            self.task_id, self.run_id, backend="pi_desktop", status="running",
            pid=15120, request_id=self.request_id, started_at=util.now_iso(),
            ended_at=None, exit_code=None,
        )
        self.directory = self.make_run(
            self.task_id, self.run_id, simple_events("旧规划记录"), job=self.job,
        )
        self.make_run(self.task_id, "new-plan-run", job=pi_job(
            self.task_id, "new-plan-run", backend="pi_desktop", status="running",
            request_id="new-plan-request", pid=58689, started_at=util.now_iso(),
        ))
        def process_probe(pid, signal):
            if pid > 2 ** 31 - 1:
                raise OverflowError("PID out of range")
            if pid != 58689:
                raise OSError(errno.ESRCH, "gone")
        self.kill = self.enter_patch(patch.object(session_control.os, "kill", side_effect=process_probe))
        self.entry()  # 现场已存在索引；身份损坏时仍需校验该历史索引的保护。

    def enter_patch(self, context):
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def connection(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def add_request(self, request_id, run_id, status, kind="plan"):
        now = util.now_iso()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,"
                "run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (request_id, request_id, self.task_id, kind, kind, status, run_id, now, now),
            )

    def add_attempt(self, request_id, run_id, outcome, finished_at):
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO attempts(request_id,task_id,run_id,stage,kind,outcome,finished_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (request_id, self.task_id, run_id, "plan", "plan", outcome, finished_at),
            )

    def entry(self, run_id=None):
        return self.call("get-session", {
            "task_id": self.task_id, "run_id": run_id or self.run_id,
        })["session"]

    def command(self, name, **payload):
        return self.call(name, {"task_id": self.task_id, "run_id": self.run_id, **payload})

    def assert_blocked(self):
        entry = self.entry()
        self.assertNotIn("trash-session", entry["allowed_actions"])
        error = self.call_err("trash-session", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(error.code, "conflict")
        self.assertEqual(error.message, entry["blocked_reason"])

    def test_cancelled_dead_old_plan_can_trash_restore_purge_without_touching_new(self):
        self.entry("new-plan-run")
        with self.connection() as conn:
            before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                      for table in ("tasks", "requests", "attempts")}
            new_round = dict(round_store.require_round(conn, self.task_id, "new-plan-run"))
        files = {path: path.read_bytes() for path in self.runs_root.rglob("*") if path.is_file()}
        self.assertEqual(self.entry()["run_status"], "running")
        self.assertIn("trash-session", self.entry()["allowed_actions"])
        listed = self.call("list-sessions", {"task_id": self.task_id})["sessions"]
        self.assertIn("trash-session", next(row for row in listed if row["run_id"] == self.run_id)["allowed_actions"])
        self.assertNotIn("trash-session", self.entry("new-plan-run")["allowed_actions"])
        self.assertFalse(self.command("trash-session")["deduplicated"])
        self.assertTrue(self.command("trash-session")["deduplicated"])
        self.assertEqual(self.entry()["allowed_actions"], ["restore-session", "purge-session"])
        self.command("restore-session")
        self.command("trash-session")
        self.assertFalse(self.command("purge-session")["deduplicated"])
        self.assertTrue(self.command("purge-session")["deduplicated"])
        # get/list 的运行记录回填不得复活移除状态。
        self.assertEqual(self.entry()["save_state"], "purged")
        self.assertEqual(self.entry()["allowed_actions"], [])
        self.assertNotIn(self.run_id, [row["run_id"] for row in self.call(
            "list-sessions", {"task_id": self.task_id})["sessions"]])
        self.assertEqual(self.call_err("restore-session", {
            "task_id": self.task_id, "run_id": self.run_id}).code, "conflict")
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        with self.connection() as conn:
            for table, original in before.items():
                self.assertEqual([dict(row) for row in conn.execute(f"SELECT * FROM {table}")], original)
            self.assertEqual(dict(round_store.require_round(conn, self.task_id, "new-plan-run")), new_round)

    def test_terminal_plan_statuses_and_stale_current_marker(self):
        self.set_task_status(self.task_id, "planning", run_id=self.run_id,
                             session_marker=f"{self.task_id}/{self.run_id}")
        for status in ("cancelled", "failed", "succeeded"):
            with self.subTest(status=status):
                with self.connection() as conn:
                    conn.execute("UPDATE requests SET status=? WHERE request_id=?", (status, self.request_id))
                self.assertIn("trash-session", self.entry()["allowed_actions"])

    def test_live_unknown_and_permission_denied_pid_remain_blocked(self):
        for pid in (58689, None, True, "15120", 0, 2 ** 100):
            with self.subTest(pid=pid):
                write_json(self.directory / "job.json", {**self.job, "pid": pid})
                self.assert_blocked()
        write_json(self.directory / "job.json", self.job)
        self.kill.side_effect = OSError(errno.EPERM, "denied")
        self.assert_blocked()

    def test_live_or_unknown_observer_protects_old_round(self):
        for pid in (58689, "unknown", 2 ** 100):
            with self.subTest(pid=pid):
                write_json(self.directory / "job.json", {**self.job, "observe_pid": pid})
                self.assert_blocked()
        write_json(self.directory / "job.json", {**self.job, "observe_pid": 15121})
        self.assertIn("trash-session", self.entry()["allowed_actions"])

    def test_same_round_active_request_and_unfinished_attempt_protect_round(self):
        for status in ("queued", "claimed"):
            with self.subTest(status=status):
                self.add_request("other-" + status, self.run_id, status)
                self.assert_blocked()
                with self.connection() as conn:
                    conn.execute("DELETE FROM requests WHERE request_id=?", ("other-" + status,))
        self.add_attempt(self.request_id, self.run_id, "claimed", None)
        self.assert_blocked()

    def test_job_identity_request_backend_and_index_pid_mismatch_block(self):
        for key, value in (("task_id", "other-task"), ("run_id", "other-run"),
                           ("request_id", "new-plan-request"), ("backend", "unknown")):
            with self.subTest(key=key):
                write_json(self.directory / "job.json", {**self.job, key: value})
                self.assert_blocked()
        write_json(self.directory / "job.json", self.job)
        with self.connection() as conn:
            conn.execute("UPDATE execution_rounds SET pid=99999 WHERE task_id=? AND run_id=?",
                         (self.task_id, self.run_id))
        self.assert_blocked()

    def test_purge_rechecks_process_and_database_after_trash(self):
        self.command("trash-session")
        write_json(self.directory / "job.json", {**self.job, "observe_pid": 58689})
        entry = self.entry()
        self.assertEqual(entry["allowed_actions"], ["restore-session"])
        self.assertEqual(self.call_err("purge-session", {
            "task_id": self.task_id, "run_id": self.run_id}).code, "conflict")
        self.assertEqual(self.entry()["save_state"], "trashed")
        write_json(self.directory / "job.json", self.job)
        self.add_request("claimed-after-trash", self.run_id, "claimed")
        self.assertEqual(self.entry()["allowed_actions"], ["restore-session"])
        self.assertEqual(self.call_err("purge-session", {
            "task_id": self.task_id, "run_id": self.run_id}).code, "conflict")

    def test_finished_historical_plan_without_pid_preserves_old_delete_behavior(self):
        write_json(self.directory / "job.json", {**self.job, "status": "finished", "pid": None})
        self.assertIn("trash-session", self.entry()["allowed_actions"])
        self.command("trash-session")
        self.command("purge-session")

    def test_finished_plan_with_active_request_still_protected(self):
        write_json(self.directory / "job.json", {**self.job, "status": "finished"})
        self.add_request("claimed-finished", self.run_id, "claimed")
        self.assert_blocked()

    def test_finished_plan_with_unfinished_new_attempt_still_protected(self):
        write_json(self.directory / "job.json", {**self.job, "status": "finished"})
        self.add_attempt(self.request_id, self.run_id, None, None)
        self.assert_blocked()

    def test_execution_and_review_current_task_protection_not_overridden(self):
        for status in ("running", "in_review", "submitting"):
            with self.subTest(status=status):
                self.set_task_status(self.task_id, status, run_id=self.run_id,
                                     session_marker=f"{self.task_id}/{self.run_id}")
                self.assert_blocked()

    def test_missing_source_cannot_supply_stale_plan_exit_proof(self):
        with self.connection() as conn:
            row = round_store.require_round(conn, self.task_id, self.run_id)
            reason = cmd_session._session_trash_reason(conn, None, row, util.now_dt())
            self.assertIn("无法确认", reason)
            with patch.object(cmd_session.headless, "resolve_source", return_value=None):
                fields = cmd_session._round_fields(
                    conn, self.task_id, self.run_id, row, util.now_dt(), 600, source=None,
                )
            self.assertNotIn("trash-session", fields["allowed_actions"])
            self.assertEqual(fields["blocked_reason"], reason)
