"""Status-only polling: isolated SQLite/files, no model or real runtime calls."""

import datetime
import json
from unittest.mock import patch

from kanpilot import cmd_session, db, dsh_events, headless_adapter as h, round_store, session_store, util
from test_session import HeadlessCase, pi_event, pi_job, write_json, write_jsonl


class TestSessionStatus(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("status-task", "status-run", status="ready")
        self.run_id = "status-run"
        self.now = util.now_dt()
        self.job = pi_job(self.task_id, self.run_id, backend="pi_desktop", status="running",
                          started_at=util.to_iso(self.now), ended_at=None, exit_code=None,
                          turn_id="goal-turn", turn_adopted={"turn_id": "goal-turn"})
        self.events = [pi_event(1, "session", session_id=self.job["session_id"],
                                turn_id="startup-turn", ts=util.to_iso(self.now))]
        # A real multi-megabyte event log, including private message bodies.
        self.events.extend(pi_event(index, "assistant_message", content="private-body-" + "x" * 2048,
                                    ts=util.to_iso(self.now)) for index in range(2, 2002))
        self.directory = self.make_run(self.task_id, self.run_id, self.events, job=self.job)

    def status(self, **extra):
        return self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id,
                                         "status_only": True, **extra})

    def conn(self):
        connection = db.connect(self.db_path)
        self.addCleanup(connection.close)
        return connection

    def change_job(self, **changes):
        self.job.update(changes)
        write_json(self.directory / "job.json", self.job)

    def test_initial_and_idle_never_inspect_or_convert_messages(self):
        with patch.object(h.HeadlessSource, "inspect", side_effect=AssertionError("full inspect")), \
                patch.object(dsh_events, "to_messages", side_effect=AssertionError("body conversion")), \
                patch.object(cmd_session, "_load_snapshot", side_effect=AssertionError("large snapshot")), \
                patch.object(session_store, "get_position", side_effect=AssertionError("revisions")), \
                patch.object(session_store, "checked_at", side_effect=AssertionError("reader timestamp")), \
                patch.object(dsh_events, "_read_events", wraps=dsh_events._read_events) as reads:
            first = self.status()
            self.assertEqual(reads.call_count, 1)
            for _ in range(5):
                current = self.status()
                self.assertEqual(current["session"]["message_count"], first["session"]["message_count"])
                self.assertEqual(current["session"]["run_status"], "running")
            self.assertEqual(reads.call_count, 1)
        cached = self.conn().execute("SELECT value FROM settings WHERE key=?", (
            cmd_session._STATUS_SNAPSHOT_PREFIX + f"{self.task_id}/{self.run_id}",)).fetchone()[0]
        self.assertLess(len(cached), 5000)
        self.assertNotIn("private-body", cached)
        self.assertIsNone(session_store.get_position(self.conn(), f"{self.task_id}/{self.run_id}"))
        self.assertEqual(first["round_detail"]["startup_turn_id"], "startup-turn")
        self.assertEqual(first["round_detail"]["turn_id"], "goal-turn")
        self.assertEqual(first["round_detail"]["turn_adopted"], {"turn_id": "goal-turn"})

    def test_append_terminal_updates_failure_and_new_idle_version(self):
        self.status()
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(pi_event(2002, "terminal", outcome="failed", reason="provider unavailable")) + "\n")
        self.change_job(status="failed", detail="provider unavailable", exit_code=1)
        with patch.object(dsh_events, "_read_events", wraps=dsh_events._read_events) as reads:
            result = self.status()
            self.assertEqual(result["session"]["run_status"], "failed")
            self.assertEqual(result["round_detail"]["terminal"],
                             {"outcome": "failed", "reason": "provider unavailable"})
            self.assertEqual(result["round_detail"]["detail"], "provider unavailable")
            self.status()
            self.assertEqual(reads.call_count, 1)

    def test_timeout_and_checked_time_recomputed_without_body_read(self):
        self.status()
        future = self.now + datetime.timedelta(days=2)
        with patch.object(util, "now_dt", return_value=future), \
                patch.object(dsh_events, "_read_events", side_effect=AssertionError("idle log read")):
            result = self.status()
        self.assertEqual(result["session"]["last_checked_at"], util.to_iso(future))
        self.assertEqual(result["session"]["run_status"], "failed")
        self.assertIn("超时", result["round_detail"]["detail"])
        self.assertEqual(json.loads((self.directory / "job.json").read_text())["status"], "running")

    def test_save_state_actions_recomputed_and_purged_never_revived(self):
        self.change_job(status="finished")
        self.status()
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("idle log read")):
            self.assertIn("trash-session", self.status()["session"]["allowed_actions"])
            round_store.set_save_state(self.conn(), self.task_id, self.run_id, "trashed", util.to_iso(self.now))
            self.assertIn("restore-session", self.status()["session"]["allowed_actions"])
            round_store.set_save_state(self.conn(), self.task_id, self.run_id, "purged", util.to_iso(self.now))
            result = self.status()
            self.assertEqual(result["session"]["save_state"], "purged")
            self.assertEqual(result["session"]["allowed_actions"], [])
        self.change_job(status="failed")
        self.assertEqual(self.status()["session"]["save_state"], "purged")

    def test_half_written_log_retains_summary_but_running_is_unconfirmed(self):
        first = self.status()
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('{"v":1,"seq":2002,"type":"terminal"')
        result = self.status()
        self.assertEqual(result["session"]["message_count"], first["session"]["message_count"])
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertTrue(result["connection"]["detail"])
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("unchanged partial log read")):
            self.assertEqual(self.status()["session"]["run_status"], "unknown")
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(',"outcome":"failed","reason":"real error"}\n')
        self.change_job(status="failed")
        self.assertEqual(self.status()["round_detail"]["terminal"]["reason"], "real error")

    def test_identity_mismatch_never_uses_previous_running_summary(self):
        self.status()
        self.change_job(task_id="another-task")
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("foreign log read")):
            result = self.status()
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertIsNone(result["session"]["session_id"])

    def test_unreachable_source_keeps_known_scalar_summary_as_unconfirmed(self):
        first = self.status()
        self.runs_root.rename(self.root / "offline-runs")
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("offline log read")):
            result = self.status()
        self.assertEqual(result["session"]["message_count"], first["session"]["message_count"])
        self.assertEqual(result["session"]["session_id"], first["session"]["session_id"])
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertEqual(result["connection"]["state"], "unavailable")
        self.assertEqual(result["round_detail"]["turn_adopted"], first["round_detail"]["turn_adopted"])

    def test_half_written_job_keeps_known_summary_as_unconfirmed(self):
        first = self.status()
        (self.directory / "job.json").write_text('{"task_id":', encoding="utf-8")
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("invalid job log read")):
            result = self.status()
        self.assertEqual(result["session"]["message_count"], first["session"]["message_count"])
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertEqual(result["round_detail"]["startup_turn_id"], "startup-turn")
        self.assertNotEqual(result["session"]["state"], "unavailable")
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIn("解析失败", result["connection"]["detail"])
        self.change_job()
        recovered = self.status()
        self.assertEqual(recovered["session"]["state"], "associated")
        self.assertEqual(recovered["session"]["run_status"], "running")
        self.assertIsNone(recovered["connection"]["detail"])

    def test_initial_half_written_job_is_pending_and_recovers(self):
        (self.directory / "job.json").write_text('{"task_id":', encoding="utf-8")
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("invalid job log read")):
            result = self.status()
        self.assertEqual(result["session"]["state"], "pending")
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIn("解析失败", result["connection"]["detail"])
        self.change_job()
        recovered = self.status()
        self.assertEqual(recovered["session"]["state"], "associated")
        self.assertEqual(recovered["session"]["run_status"], "running")

    def test_replaced_job_session_identity_does_not_keep_previous_summary(self):
        self.status()
        self.change_job(session_id="replacement-session")
        (self.directory / "events.jsonl").write_text('{"v":', encoding="utf-8")
        result = self.status()
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertIsNone(result["session"]["session_id"])

    def test_root_change_does_not_reuse_other_source_summary(self):
        self.status()
        replacement = self.root / "replacement-runs"
        replacement.mkdir()
        h.set_source(h.HeadlessSource(str(replacement)))
        result = self.status()
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertIsNone(result["session"]["session_id"])

    def test_event_symlink_refuses_even_matching_identity(self):
        self.status()
        path = self.directory / "events.jsonl"
        moved = self.root / "foreign-events.jsonl"
        path.rename(moved)
        path.symlink_to(moved)
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("symlink log read")):
            result = self.status()
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertIn("符号链接", result["connection"]["detail"])

    def test_force_purge_system_only_uses_cached_file_proof_and_live_activity(self):
        self.change_job(session_id=None, status="failed", turn_id=None)
        write_jsonl(self.directory / "events.jsonl", [
            pi_event(index, "system", content="system-only " + "x" * 2048)
            for index in range(1, 2002)])
        self.assertIn("force-purge-session", self.status()["session"]["allowed_actions"])
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("idle log read")), \
                patch.object(cmd_session, "_force_purge_file_reason", side_effect=AssertionError("idle file proof")):
            self.assertIn("force-purge-session", self.status()["session"]["allowed_actions"])
            self.set_task_status(self.task_id, "running")
            self.assertNotIn("force-purge-session", self.status()["session"]["allowed_actions"])

    def test_purge_proof_invalidates_when_other_content_file_appears(self):
        self.change_job(session_id=None, status="failed")
        write_jsonl(self.directory / "events.jsonl", [pi_event(1, "system", content="startup failure")])
        self.assertIn("force-purge-session", self.status()["session"]["allowed_actions"])
        (self.directory / "last-message.txt").write_text("actual content", encoding="utf-8")
        self.assertNotIn("force-purge-session", self.status()["session"]["allowed_actions"])

    def test_source_changes_during_read_cannot_confirm_running(self):
        original = dsh_events._read_events
        def read_and_change(path):
            value = original(path)
            self.change_job(status="failed")
            return value
        with patch.object(dsh_events, "_read_events", side_effect=read_and_change):
            result = self.status()
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertTrue(result["connection"]["detail"])

    def test_default_get_session_retains_full_inspection(self):
        with patch.object(h.HeadlessSource, "inspect", wraps=self.source.inspect) as inspect:
            result = self.status(status_only=False)
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(result["session"]["state"], "associated")

    def test_unconfirmed_sources_keep_legacy_management_actions(self):
        self.change_job(session_id=None, status="failed")
        conditions = ("missing-events", "empty-events", "missing-directory", "missing-root")
        for condition in conditions:
            with self.subTest(condition=condition):
                if condition == "missing-events":
                    (self.directory / "events.jsonl").unlink()
                elif condition == "empty-events":
                    (self.directory / "events.jsonl").write_text(" \n", encoding="utf-8")
                elif condition == "missing-directory":
                    self.directory.rename(self.root / "removed-run")
                else:
                    self.runs_root.rename(self.root / "removed-root")
                legacy = self.status(status_only=False)
                current = self.status()
                self.assertEqual(current["session"]["allowed_actions"], legacy["session"]["allowed_actions"])
                self.assertEqual(current["session"]["blocked_reason"], legacy["session"]["blocked_reason"])
                self.assertEqual(current["session"]["run_status"], "unknown")
                if condition in ("missing-events", "empty-events"):
                    self.assertEqual(current["session"]["state"], "pending")
                    self.assertIsNone(current["connection"]["detail"])

    def test_unchanged_empty_log_waits_without_repeated_body_read(self):
        (self.directory / "events.jsonl").write_text(" \n", encoding="utf-8")
        self.status()
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("idle empty log read")):
            result = self.status()
        self.assertEqual(result["session"]["state"], "pending")
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIsNone(result["connection"]["detail"])
