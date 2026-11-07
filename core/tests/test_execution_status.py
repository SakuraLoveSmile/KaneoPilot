"""Current-run status checks: isolated DB/files and fake Plus, never a model call."""

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_session import HeadlessCase, pi_event, pi_job, write_json, write_jsonl
from kanpilot import (cli, cmd_session, codex_execution, db, execution_status,
                      harness_plus_adapter, round_store, session_control, util, verification_checks)


class ExecutionStatusTests(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id, self.run_id = "check-task", "check-run"
        self.make_task(self.task_id, self.run_id, status="needs_user")
        self.set_task_status(self.task_id, "needs_user", stage="monitor",
                             session_id="check-session", failure_code="execution_failed",
                             failure_reason="turn error: NETWORK_ERROR")
        self.job = pi_job(self.task_id, self.run_id, backend="codex", status="running",
                          started_at=util.now_iso(), ended_at=None, exit_code=None,
                          session_id="check-session", turn_id="check-turn", pid=99999999,
                          worker_birth={"started_seconds": 1, "started_microseconds": 2},
                          cli_terminal=False, turns=[])
        self.events = [pi_event(1, "session", session_id="check-session", turn_id="check-turn")]
        self.directory = self.make_run(self.task_id, self.run_id, self.events, job=self.job)
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        with db.write_txn(self.conn):
            round_store.upsert_discovered(self.conn, self.task_id, self.run_id, util.now_iso(),
                                          backend=self.job["backend"], session_id=self.job["session_id"],
                                          run_status=self.job["status"])
        self.ctx = SimpleNamespace(conn=self.conn, now=util.now_dt(), now_iso=util.now_iso(), close=lambda: None)

    def rewrite(self, **changes):
        self.job.update(changes)
        write_json(self.directory / "job.json", self.job)
        if changes.get("backend"):
            with db.write_txn(self.conn):
                self.conn.execute("UPDATE execution_rounds SET backend=? WHERE task_id=? AND run_id=?",
                                  (changes["backend"], self.task_id, self.run_id))

    def terminal(self, status="failed", outcome="failed", **changes):
        fields = dict(status=status, ended_at=util.now_iso(), exit_code=0 if status == "finished" else 1,
                      cli_terminal=True, detail="turn error: NETWORK_ERROR" if status == "failed" else None)
        fields.update(changes)
        self.rewrite(**fields)
        write_jsonl(self.directory / "events.jsonl", self.events + [
            pi_event(2, "terminal", outcome=outcome, reason="NETWORK_ERROR" if outcome == "failed" else "completed")])

    def control(self, **changes):
        value = {"task_id": self.task_id, "run_id": self.run_id, "backend": "pi_desktop",
                 "state": "idle", "detail": "Plus 当前空闲", "connected": True,
                 "session_id": "check-session", "turn_id": "check-turn", "observed_turn_id": "check-turn"}
        value.update(changes)
        if "turn_id" in changes and "observed_turn_id" not in changes:
            value["observed_turn_id"] = changes["turn_id"]
        return value

    def observe(self, *, alive=False, control=None, readonly=True, process_exited=None):
        # SQLite query_only actively rejects every accidental write, including
        # backfill/cache writes hidden in the shared status-reader path.
        self.conn.execute("PRAGMA query_only=" + ("ON" if readonly else "OFF"))
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(codex_execution, "job_alive", return_value=alive))
                stack.enter_context(patch.object(util, "pid_alive", return_value=alive))
                stack.enter_context(patch.object(session_control, "_process_exited",
                                                return_value=alive is False if process_exited is None else process_exited))
                stack.enter_context(patch.object(cli, "Context", return_value=self.ctx))
                live = stack.enter_context(patch.object(session_control, "cmd_session_control_status",
                                                       return_value=control or self.control()))
                result = self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
                if self.job["backend"] != "pi_desktop":
                    live.assert_not_called()
                return result
        finally:
            self.conn.execute("PRAGMA query_only=OFF")

    def snapshot(self):
        return (list(self.conn.iterdump()), {
            str(path.relative_to(self.directory)): path.read_bytes()
            for path in self.directory.rglob("*") if path.is_file()
        })

    def test_dead_or_unidentified_codex_does_not_reuse_running_file(self):
        for alive in (False, None):
            with self.subTest(alive=alive):
                result = self.observe(alive=alive)
                self.assertEqual(result["state"], "unknown")
                self.assertIn("不要重复启动", result["next_step"])
                self.assertEqual(result["original_failure"], "turn error: NETWORK_ERROR")
        self.assertEqual(self.observe(alive=True)["state"], "running")

    def test_durable_codex_terminals_require_process_exit_and_matching_event(self):
        for status, outcome in (("finished", "completed"), ("failed", "failed"), ("aborted", "aborted")):
            self.terminal(status, outcome)
            with self.subTest(status=status):
                result = self.observe()
                self.assertEqual(result["state"], status)
                self.assertIn("需求完成仍以验证", result["next_step"])
                self.assertEqual(self.observe(alive=True)["state"], "unknown")
                self.assertEqual(self.observe(alive=None)["state"], "unknown")
        self.terminal("finished", "failed")
        self.assertEqual(self.observe()["state"], "unknown")
        self.terminal("failed", "completed")
        self.assertEqual(self.observe()["state"], "unknown")

    def test_codex_terminal_missing_exit_or_cli_evidence_is_unknown(self):
        for changes in ({"exit_code": None}, {"exit_code": True}, {"ended_at": None}, {"cli_terminal": False}):
            self.terminal()
            self.rewrite(**changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.observe()["state"], "unknown")

    def test_timeout_derived_failure_is_not_a_confirmed_terminal(self):
        self.rewrite(started_at="2020-01-01T00:00:00Z")
        result = self.observe(alive=False)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(json.loads((self.directory / "job.json").read_text())["status"], "running")

    def test_plus_real_activity_overrides_original_failed_status(self):
        self.rewrite(backend="pi_desktop")
        self.terminal()
        for state in ("running", "waiting_permission"):
            with self.subTest(state=state):
                result = self.observe(control=self.control(state=state))
                self.assertEqual(result["state"], state)
                self.assertIn("NETWORK_ERROR", result["original_failure"])

    def test_plus_idle_and_running_record_is_unknown(self):
        self.rewrite(backend="pi_desktop")
        result = self.observe(control=self.control())
        self.assertEqual(result["state"], "unknown")
        self.assertIn("空闲", result["summary"])
        self.assertIn("尚不完整", result["summary"])

    def test_plus_idle_and_consistent_terminal_can_confirm_failure(self):
        self.rewrite(backend="pi_desktop")
        self.terminal()
        result = self.observe()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.observe(alive=True)["state"], "unknown")

    def test_plus_verified_continuation_retains_original_failure(self):
        self.rewrite(backend="pi_desktop")
        self.terminal()
        for state in ("running", "waiting_permission", "finished", "failed", "aborted"):
            control = self.control(
                state=state if state in ("running", "waiting_permission") else "idle",
                turn_id="continued-turn", continued_turn_id="continued-turn", continued_state=state,
                continued_detail="Plus 手动继续的轮次状态已核验",
                original_turn_ended_at="2026-11-07T10:00:00Z",
                continued_ended_at="2026-11-07T10:10:00Z" if state in ("finished", "failed", "aborted") else None)
            with self.subTest(state=state):
                result = self.observe(control=control)
                self.assertEqual(result["state"], state)
                self.assertIn("NETWORK_ERROR", result["original_failure"])
                self.assertIn("原失败记录保留", result["summary"])

    def test_plus_unverified_or_partial_continuation_is_unknown(self):
        self.rewrite(backend="pi_desktop")
        self.terminal()
        for changes in (
                {"turn_id": "another-turn"},
                {"continued_state": "finished"},
                {"turn_id": "another-turn", "continued_state": "finished", "continued_turn_id": "another-turn",
                 "continued_detail": "finished", "original_turn_ended_at": "2026-11-07T10:00:00Z"},
                {"turn_id": "another-turn", "continued_state": "running", "continued_turn_id": "wrong-turn",
                 "continued_detail": "running", "original_turn_ended_at": "2026-11-07T10:00:00Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.observe(control=self.control(**changes))["state"], "unknown")

    def test_plus_disconnected_or_wrong_identity_is_unknown(self):
        self.rewrite(backend="pi_desktop")
        for changes in ({"connected": False, "state": "disconnected"},
                        {"task_id": "wrong-task"}, {"run_id": "wrong-run"},
                        {"session_id": "wrong-session"}, {"turn_id": None},
                        {"backend": "codex"}, {"state": "unsupported"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.observe(control=self.control(**changes))["state"], "unknown")

    def test_strict_no_database_or_source_writes_even_when_index_is_stale(self):
        with db.write_txn(self.conn):
            self.conn.execute("UPDATE execution_rounds SET run_status='pending' WHERE task_id=?", (self.task_id,))
        before = self.snapshot()
        self.assertEqual(self.observe(alive=True)["state"], "running")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.observe(alive=True)["state"], "running")
        self.assertEqual(self.snapshot(), before)

    def test_missing_index_stays_unknown_and_unmodified(self):
        with db.write_txn(self.conn):
            self.conn.execute("DELETE FROM execution_rounds WHERE task_id=?", (self.task_id,))
        before = self.snapshot()
        self.assertEqual(self.observe(alive=True)["state"], "unknown")
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(round_store.get_round(self.conn, self.task_id, self.run_id))

    def test_existing_get_session_still_backfills_and_caches(self):
        with db.write_txn(self.conn):
            self.conn.execute("DELETE FROM execution_rounds WHERE task_id=?", (self.task_id,))
        result = self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id, "status_only": True})
        self.assertEqual(result["session"]["run_status"], "running")
        self.assertIsNotNone(round_store.get_round(self.conn, self.task_id, self.run_id))
        self.assertIsNotNone(self.conn.execute("SELECT value FROM settings WHERE key=?", (
            cmd_session._STATUS_SNAPSHOT_PREFIX + f"{self.task_id}/{self.run_id}",)).fetchone())

    def test_half_written_or_foreign_job_and_symlink_are_unknown(self):
        before = self.directory / "job.json"
        original = before.read_bytes()
        before.write_text('{"task_id":')
        self.assertEqual(self.observe()["state"], "unknown")
        before.write_bytes(original)
        self.rewrite(task_id="foreign-task")
        self.assertEqual(self.observe()["state"], "unknown")
        before.write_bytes(original)
        foreign = self.root / "foreign-job.json"
        foreign.write_bytes(original)
        before.unlink()
        before.symlink_to(foreign)
        self.assertEqual(self.observe()["state"], "unknown")

    def test_half_written_log_does_not_reuse_prior_healthy_cache(self):
        self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id, "status_only": True})
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write('{"type":"terminal"')
        before = self.snapshot()
        self.assertEqual(self.observe(alive=True)["state"], "unknown")
        self.assertEqual(self.snapshot(), before)

    def test_stale_run_is_rejected_before_and_after_network_observation(self):
        with self.assertRaises(util.KanpilotError) as caught:
            self.call("check-execution-status", {"task_id": self.task_id, "run_id": "old-run"})
        self.assertEqual(caught.exception.code, "stale_run")
        self.rewrite(backend="pi_desktop")
        def switched(_ctx, _payload):
            with db.write_txn(self.conn):
                self.conn.execute("UPDATE tasks SET run_id='new-run' WHERE task_id=?", (self.task_id,))
            return self.control(state="running")
        with patch.object(session_control, "cmd_session_control_status", side_effect=switched):
            with self.assertRaises(util.KanpilotError) as caught:
                self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(caught.exception.code, "stale_run")

    def test_job_binding_replaced_during_plus_read_is_rejected(self):
        self.rewrite(backend="pi_desktop")
        def replaced(_ctx, _payload):
            self.rewrite(session_id="replacement")
            return self.control(state="running")
        with patch.object(session_control, "cmd_session_control_status", side_effect=replaced):
            with self.assertRaises(util.KanpilotError) as caught:
                self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(caught.exception.code, "stale_run")

    def test_status_progressed_during_plus_read_is_unknown(self):
        self.rewrite(backend="pi_desktop")
        def completed(_ctx, _payload):
            self.terminal("finished", "completed")
            return self.control(state="running")
        with patch.object(session_control, "cmd_session_control_status", side_effect=completed):
            result = self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(result["state"], "unknown")
        self.assertIn("核验期间发生变化", result["summary"])

    def test_legacy_pi_does_not_claim_pid_is_a_verified_live_identity(self):
        self.rewrite(backend="pi")
        self.assertEqual(self.observe(alive=True)["state"], "unknown")

    def test_missing_observer_pid_cannot_prove_exit_or_confirm_plus_terminal(self):
        self.rewrite(backend="pi_desktop", pid=None, observe_pid=None)
        self.terminal()
        result = self.observe(process_exited=False)
        self.assertEqual(result["state"], "unknown")
        process = next(item for item in result["checks"] if item["name"] == "process")
        self.assertEqual(process["state"], "unknown")
        self.assertNotIn("已退出", process["detail"])
        control = self.control(turn_id="continued", continued_turn_id="continued", continued_state="finished",
                               continued_detail="Plus 后续轮次已完成", original_turn_ended_at="2026-11-07T10:00:00Z",
                               continued_ended_at="2026-11-07T10:10:00Z")
        self.assertEqual(self.observe(control=control, process_exited=False)["state"], "unknown")

    def test_plain_alive_pid_never_confirms_plus_observer_ownership(self):
        self.rewrite(backend="pi_desktop")
        result = self.observe(alive=True, control=self.control(state="running"))
        self.assertEqual(result["state"], "running")
        process = next(item for item in result["checks"] if item["name"] == "process")
        self.assertEqual(process["label"], "本机观察进程")
        self.assertEqual(process["state"], "unknown")
        self.assertIn("归属尚未核验", process["detail"])

    def test_terminal_event_changed_during_plus_read_cannot_publish_previous_terminal(self):
        self.rewrite(backend="pi_desktop")
        self.terminal()
        def replaced(_ctx, _payload):
            (self.directory / "events.jsonl").write_text('{"type":"terminal"')
            return self.control()
        with patch.object(session_control, "cmd_session_control_status", side_effect=replaced), \
                patch.object(util, "pid_alive", return_value=False), \
                patch.object(session_control, "_process_exited", return_value=True):
            result = self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(result["state"], "unknown")
        self.assertIn("结束证据在核验期间发生变化", result["summary"])

    def test_real_plus_adapter_without_current_turn_does_not_reuse_persistent_turn(self):
        self.rewrite(backend="pi_desktop", config={"connection_file": str(self.root / "fake-plus.json")})
        self.terminal()
        for status in ({"sessionId": "check-session", "isRunning": False},
                       {"sessionId": "check-session", "isRunning": True, "currentTurnId": None}):
            client = MagicMock()
            def call_tool(name, _payload):
                self.assertEqual(name, "pi_agent_status")
                return {"status": status}
            client.call_tool.side_effect = call_tool
            with self.subTest(status=status), \
                    patch.object(harness_plus_adapter, "_get_client", return_value=(client, {"token": "fixture-token"})), \
                    patch.object(util, "pid_alive", return_value=False), \
                    patch.object(session_control, "_process_exited", return_value=True):
                control = self.call("session-control-status", {"task_id": self.task_id, "run_id": self.run_id})
                self.assertEqual(control["turn_id"], "check-turn")
                self.assertIsNone(control["observed_turn_id"])
                result = self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
            self.assertEqual(result["state"], "unknown")

    def test_verified_goal_awaiting_approval_is_waiting_even_when_plus_idle(self):
        self.rewrite(backend="pi_desktop", config={"connection_file": str(self.root / "fake-plus.json")})
        self.terminal()
        status = {"session_id": "check-session", "is_running": False, "current_turn_id": "goal-turn",
                  "pending_permission": None, "raw_status": {"sessionId": "check-session", "isRunning": False,
                    "currentTurnId": "goal-turn", "planningState": "awaiting_approval", "pendingPlanId": "goal-plan"}}
        def turn(_connection, *, session_id, turn_id, **_extra):
            return {"session_id": session_id, "turn_id": turn_id,
                    "status": "error" if turn_id == "check-turn" else "completed",
                    "ended_at": "2026-11-07T10:00:00Z" if turn_id == "check-turn" else "2026-11-07T10:10:00Z"}
        with patch.object(harness_plus_adapter, "plus_status", return_value=status), \
                patch.object(harness_plus_adapter, "plus_turn_get", side_effect=turn), \
                patch.object(harness_plus_adapter, "plus_proposals", return_value={"proposals": [
                    {"kind": "goal", "sessionId": "check-session", "turnId": "goal-turn",
                     "id": "goal-plan", "status": "pending"}]}):
            result = self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(result["state"], "waiting_permission")
        self.assertEqual(result["label"], "等待确认")
        self.assertIn("目标契约已生成", result["summary"])

    def test_cli_read_check_bypasses_write_guard_without_registering_authorization(self):
        real_check = execution_status.cmd_check_execution_status
        def captured(ctx, payload):
            self.assertIsNone(ctx.conn.authorization_guard)
            return real_check(ctx, payload)
        before = self.snapshot()
        with patch.object(verification_checks, "require_not_running", side_effect=AssertionError("write guard")), \
                patch.object(execution_status, "cmd_check_execution_status", side_effect=captured), \
                patch.object(codex_execution, "job_alive", return_value=True):
            result = self.call("check-execution-status", {"task_id": self.task_id, "run_id": self.run_id})
        self.assertEqual(result["state"], "running")
        self.assertEqual(self.snapshot(), before)

    def test_failure_text_is_redacted_and_no_raw_plus_response_is_exported(self):
        self.rewrite(backend="pi_desktop")
        self.terminal(detail="NETWORK_ERROR token=private-token-value")
        result = self.observe(control=self.control(state="disconnected", connected=False,
                                                  detail="token=private-token-value", raw_status={"token": "private-token-value"}))
        exported = json.dumps(result)
        self.assertNotIn("private-token-value", exported)
        self.assertNotIn("raw_status", exported)
        self.assertIn("NETWORK_ERROR", result["original_failure"])
