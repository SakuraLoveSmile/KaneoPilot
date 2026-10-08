"""无实际会话残留轮次强制移除：隔离 DB、临时运行目录，不接真实服务。"""

import json
from pathlib import Path
from unittest.mock import patch

from kanpilot import cli, cmd_session, db, headless_adapter as h, round_store, util
from test_session import HeadlessCase, pi_event, pi_job, write_json, write_jsonl


class TestForcePurgeSession(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("force-purge-task", "orphan-run", status="ready")
        self.run_id = "orphan-run"
        with self.connection() as conn:
            round_store.upsert_discovered(
                conn, self.task_id, self.run_id, util.to_iso(util.now_dt()),
                backend="pi_desktop", run_status="running",
            )

    def connection(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def force(self, **extra):
        ctx = cli.Context(self.db_path)
        try:
            return cmd_session.cmd_force_purge_session(
                ctx, {"task_id": self.task_id, "run_id": self.run_id, **extra},
            )
        finally:
            ctx.close()

    def empty_run(self, *, events=None, **job_fields):
        directory = self.runs_root / self.task_id / self.run_id
        write_json(directory / "job.json", pi_job(
            self.task_id, self.run_id, backend="pi_desktop", session_id=None,
            session_title=None, status="running", **job_fields,
        ))
        if events is not None:
            write_jsonl(directory / "events.jsonl", events)
        return directory

    def entry(self):
        return self.call("get-session", {
            "task_id": self.task_id, "run_id": self.run_id,
        })["session"]

    def assert_blocked(self):
        self.assertNotIn("force-purge-session", self.entry()["allowed_actions"])
        with self.assertRaises(util.KanpilotError) as error:
            self.force()
        self.assertEqual(error.exception.code, "conflict")
        self.assertEqual(self.entry()["save_state"], "active")

    def add_request(self, status, run_id=None, kind="plan"):
        now = util.to_iso(util.now_dt())
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,"
                "status,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("request-" + status, "key-" + status, self.task_id, "plan", kind,
                 status, run_id, now, now),
            )

    def test_missing_directory_stale_running_can_be_removed_and_stays_removed(self):
        with self.connection() as conn:
            before = dict(db.require_task(conn, self.task_id))
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        listed = self.call("list-sessions", {"task_id": self.task_id})["sessions"]
        self.assertIn("force-purge-session", listed[0]["allowed_actions"])
        self.assertEqual(self.force(expected_save_state="active"),
                         {"ok": True, "deduplicated": False})
        self.assertEqual(self.force(expected_save_state="active"),
                         {"ok": True, "deduplicated": True})
        with self.connection() as conn:
            self.assertEqual(dict(db.require_task(conn, self.task_id)), before)
        events = [event for event in self.call("list-events", {
            "task_id": self.task_id})["events"] if event["kind"] == "session_purged"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["detail"]["forced"])
        # 外部目录重新出现并触发读取回填，也不能复活已移除轮次。
        self.empty_run()
        self.assertEqual(self.entry()["save_state"], "purged")
        self.assertEqual(self.entry()["allowed_actions"], [])
        self.assertEqual(self.call("list-sessions", {"task_id": self.task_id})["sessions"], [])

    def test_missing_and_empty_event_logs_allow_force_purge(self):
        directory = self.empty_run()
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        (directory / "events.jsonl").write_text("  \n", encoding="utf-8")
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        self.force()
        self.assertTrue((directory / "job.json").exists())
        self.assertTrue((directory / "events.jsonl").exists())

    def test_empty_directory_and_missing_job_allow_force_purge(self):
        directory = self.runs_root / self.task_id / self.run_id
        directory.mkdir(parents=True)
        (directory / "events.jsonl").write_text("", encoding="utf-8")
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        self.force()

    def test_system_failure_only_is_not_actual_session(self):
        directory = self.empty_run(events=[
            pi_event(1, "system", content="启动失败"),
            pi_event(2, "terminal", outcome="failed", reason="没有创建会话"),
        ])
        original = (directory / "events.jsonl").read_bytes()
        self.force()
        self.assertEqual((directory / "events.jsonl").read_bytes(), original)

    def test_force_from_trash_and_expected_state_conflict(self):
        with self.connection() as conn:
            round_store.set_save_state(conn, self.task_id, self.run_id, "trashed",
                                       util.to_iso(util.now_dt()))
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        with self.assertRaises(util.KanpilotError) as error:
            self.force(expected_save_state="active")
        self.assertEqual(error.exception.code, "conflict")
        self.assertFalse(self.force(expected_save_state="trashed")["deduplicated"])

    def test_queued_and_claimed_requests_protect_exact_round(self):
        for status in ("queued", "claimed"):
            with self.subTest(status=status):
                self.add_request(status, self.run_id)
                self.assert_blocked()
                with self.connection() as conn:
                    conn.execute("DELETE FROM requests WHERE request_id=?", ("request-" + status,))

    def test_current_active_task_and_unknown_send_protect_round(self):
        for status in ("planning", "dispatching", "prepared", "submitting", "running", "in_review"):
            with self.subTest(status=status):
                self.set_task_status(self.task_id, status)
                self.assert_blocked()
        self.set_task_status(self.task_id, "ready", send_state="unknown")
        self.assert_blocked()

    def test_other_planning_request_does_not_protect_old_task_run(self):
        self.set_task_status(self.task_id, "planning")
        self.add_request("claimed", "new-plan-run")
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        self.force()
        self.assertEqual(self.requests()[0]["status"], "claimed")

    def test_latest_plan_attempt_identifies_new_round(self):
        self.set_task_status(self.task_id, "planning")
        with self.connection() as conn:
            conn.execute("INSERT INTO attempts(request_id,task_id,run_id,kind) VALUES(?,?,?,?)",
                         ("new-request", self.task_id, "new-plan-run", "plan"))
        self.assertIn("force-purge-session", self.entry()["allowed_actions"])
        self.force()

    def test_different_current_execution_is_not_blocked(self):
        self.set_task_status(self.task_id, "running", run_id="current-run",
                             session_marker=f"{self.task_id}/current-run")
        self.add_request("claimed", "current-run", kind="dispatch")
        self.force()
        self.assertEqual(self.task(self.task_id)["status"], "running")

    def test_session_id_in_index_job_or_task_protects_record(self):
        for location in ("index", "job", "task"):
            with self.subTest(location=location):
                directory = self.empty_run()
                with self.connection() as conn:
                    conn.execute("UPDATE execution_rounds SET session_id=NULL")
                self.set_task_status(self.task_id, "ready", session_id=None)
                if location == "index":
                    with self.connection() as conn:
                        conn.execute("UPDATE execution_rounds SET session_id='real-session'")
                elif location == "job":
                    document = json.loads((directory / "job.json").read_text())
                    document["session_id"] = "real-session"
                    write_json(directory / "job.json", document)
                else:
                    self.set_task_status(self.task_id, "ready", session_id="real-session")
                self.assert_blocked()

    def test_actual_and_unrecognized_events_are_not_empty_records(self):
        for kind in ("session", "user", "assistant_message", "tool_call", "subagent_start", "unknown"):
            with self.subTest(kind=kind):
                self.empty_run(events=[pi_event(1, kind, content="记录")])
                self.assert_blocked()

    def test_missing_or_unreadable_root_does_not_imply_missing_record(self):
        h.set_source(h.HeadlessSource(str(self.root / "missing-root")))
        self.assert_blocked()
        h.set_source(self.source)
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assert_blocked()

    def test_corrupt_or_mismatched_job_is_not_missing(self):
        directory = self.empty_run()
        for content in ("{", "null", "[]", json.dumps(pi_job("other-task", self.run_id))):
            with self.subTest(content=content):
                (directory / "job.json").write_text(content, encoding="utf-8")
                self.assert_blocked()

    def test_partial_or_invalid_event_log_cannot_be_force_removed(self):
        directory = self.empty_run()
        for content in ("{", json.dumps(pi_event(1, "system")) + "\n{", "{}", "[]"):
            with self.subTest(content=content):
                (directory / "events.jsonl").write_text(content, encoding="utf-8")
                self.assert_blocked()

    def test_snapshot_of_actual_session_protects_missing_source(self):
        with self.connection() as conn:
            cmd_session._save_snapshot(conn, f"{self.task_id}/{self.run_id}",
                                       [{"type": "user", "content": "历史记录"}], {})
        self.assert_blocked()

    def test_corrupt_or_invalid_snapshots_do_not_imply_absent_records(self):
        marker = f"{self.task_id}/{self.run_id}"
        cases = [
            (cmd_session._SNAPSHOT_PREFIX, "{"),
            (cmd_session._SNAPSHOT_PREFIX, "null"),
            (cmd_session._SNAPSHOT_PREFIX, '{"record":null,"messages":[]}'),
            (cmd_session._SNAPSHOT_PREFIX, '{"record":{"session_id":false},"messages":[]}'),
            (cmd_session._SNAPSHOT_PREFIX, '{"record":{},"messages":{}}'),
            (cmd_session._SNAPSHOT_PREFIX, '{"record":{},"messages":[null]}'),
            (cmd_session._SNAPSHOT_PREFIX, '{"record":{},"messages":[{}]}'),
            (cmd_session._SNAPSHOT_META_PREFIX, "{"),
            (cmd_session._SNAPSHOT_META_PREFIX, '{"record":[]}'),
            (cmd_session._SNAPSHOT_META_PREFIX, '{"record":{},"cache_schema":0}'),
        ]
        for prefix, value in cases:
            with self.subTest(prefix=prefix, value=value):
                with self.connection() as conn:
                    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                 (prefix + marker, value))
                self.assert_blocked()
                with self.connection() as conn:
                    conn.execute("DELETE FROM settings WHERE key=?", (prefix + marker,))

    def test_valid_snapshot_metadata_with_session_identity_protects_record(self):
        meta = {"record": {"session_id": "actual-session"}, "cache_schema": 1,
                "max_seq": 1, "source_version": "version"}
        with self.connection() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                         (cmd_session._SNAPSHOT_META_PREFIX + f"{self.task_id}/{self.run_id}",
                          json.dumps(meta)))
        self.assert_blocked()

    def test_snapshot_metadata_without_cached_body_is_not_proof_of_absence(self):
        meta = {"record": {"session_id": None}, "cache_schema": 1,
                "max_seq": 2, "source_version": "version", "available": False}
        with self.connection() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                         (cmd_session._SNAPSHOT_META_PREFIX + f"{self.task_id}/{self.run_id}",
                          json.dumps(meta)))
        self.assert_blocked()

    def test_empty_discovered_session_id_cannot_erase_known_identity(self):
        directory = self.empty_run()
        with self.connection() as conn:
            conn.execute("UPDATE execution_rounds SET session_id='actual-session'")
        job = json.loads((directory / "job.json").read_text())
        job["session_id"] = ""
        write_json(directory / "job.json", job)
        self.assert_blocked()
        with self.connection() as conn:
            row = round_store.require_round(conn, self.task_id, self.run_id)
            self.assertEqual(row["session_id"], "actual-session")
            self.assertFalse(cmd_session._refresh_needed(row, job))

    def test_alternate_content_file_and_symlink_are_not_missing(self):
        directory = self.empty_run()
        output = directory / "last-message.txt"
        output.write_text("实际模型回复", encoding="utf-8")
        self.assert_blocked()
        output.unlink()
        outside = self.root / "outside-events.jsonl"
        outside.write_text("", encoding="utf-8")
        (directory / "events.jsonl").symlink_to(outside)
        self.assert_blocked()

    def test_list_override_root_is_respected_by_capability(self):
        self.empty_run(events=[pi_event(1, "user", content="存在会话")])
        h.set_source(None)
        with self.connection() as conn:
            db.set_setting(conn, "session_runs_root", str(self.runs_root))
        alternate = self.root / "alternate-runs"
        alternate.mkdir()
        entries = self.call("list-sessions", {
            "task_id": self.task_id, "session_runs_root": str(alternate),
        })["sessions"]
        self.assertIn("force-purge-session", entries[0]["allowed_actions"])
        # 命令只使用配置的数据源，不能借预览覆盖绕过实际记录保护。
        self.assert_blocked()
