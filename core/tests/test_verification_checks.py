"""Explicit reruns use real local processes and disposable registered Git worktrees."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import auto_monitor, cli, db, util, verification_checks, verification_recovery, harness_plus_adapter, round_store


class TestVerificationChecks(KanpilotTestCase):
    task_id = "check-task"
    run_id = "completed-run"
    commands = ["/usr/bin/true", "/bin/echo check-passed"]

    def _fixture_set_up(self):
        super().setUp()
        self.fixture = self.make_ready_task(task_id=self.task_id)
        self.plan = self.fixture["plan_path"]
        self.plan.write_text(self.plan.read_text() + "\n```verify\n" + "\n".join(self.commands) + "\n```\n")
        self.plan_sha = util.sha256_file(self.plan)
        self.config = {"connection_file": str(self.root / "fake-plus.json")}
        self.worktree = self.root / "retained-worktree"
        self.worktree.mkdir()
        (self.worktree / "unchanged.txt").write_text("preserve existing changes")
        self.directory = self.runs_root / self.task_id / self.run_id
        self.directory.mkdir(parents=True)
        self.spec = {"task_id": self.task_id, "run_id": self.run_id, "backend": "pi_desktop",
                     "config": self.config, "plan_path": str(self.plan), "plan_sha256": self.plan_sha,
                     "repository": str(self.repos_root / "Feedback"), "cwd": str(self.worktree),
                     "runs_dir": str(self.directory), "completion_policy": "command_verification"}
        self.job = {"task_id": self.task_id, "run_id": self.run_id, "backend": "pi_desktop",
                    "config": self.config, "status": "finished", "exit_code": 0,
                    "session_id": "session", "turn_id": "execution-turn", "send_state": "sent",
                    "plan_sha256": self.plan_sha, "completion_policy": "command_verification"}
        self.events = [{"seq": 1, "type": "session", "session_id": "session", "turn_id": "execution-turn"},
                       *[{"seq": n + 2, "type": "command_output", "name": "exec",
                          "input": json.dumps({"command": command}), "exit_code": 0, "output": "passed"}
                         for n, command in enumerate(self.commands)],
                       {"seq": 4, "type": "terminal", "outcome": "completed"}]
        self.write_files()
        conn = db.connect(self.db_path)
        now = util.now_iso()
        db.update_task(conn, self.task_id, now, status="needs_user", stage="monitor",
                       failure_code="verification_unconfirmed", failure_stage="monitor",
                       failure_reason="old parser result unknown", blocked_at=now, run_id=self.run_id,
                       plan_sha256=self.plan_sha, sent=1, send_state="sent", session_id="session")
        db.set_extra(conn, db.require_task(conn, self.task_id), now,
                     plan_verify={"plan_sha256": self.plan_sha, "commands": self.commands})
        round_store.upsert_on_claim(conn, self.task_id, self.run_id, self.config, now, backend="pi_desktop")
        round_store.mark_started(conn, self.task_id, self.run_id, now, worktree_path=str(self.worktree))
        round_store.upsert_discovered(conn, self.task_id, self.run_id, now,
                                     backend="pi_desktop", session_id="session", run_status="finished")
        request, _ = db.create_request(conn, now, "monitor", "monitor:" + self.task_id, task_id=self.task_id)
        self.monitor_request_id = request["request_id"]
        db.update_request(conn, request["request_id"], now, status="failed", claimed_at=now,
                          run_id="monitor-claim", finished_at=now)
        db.add_event(conn, now, "reported", task_id=self.task_id, request_id=request["request_id"],
                     actor="monitor", detail={"outcome": "needs_user"})
        conn.close()
        self.status = {"session_id": "session", "is_running": False,
                       "current_turn_id": "execution-turn", "pending_permission": None}
        self.turn = {"session_id": "session", "turn_id": "execution-turn", "status": "completed"}
        for name, side_effect in (("plus_status", lambda *a, **k: dict(self.status)),
                                  ("plus_turn_get", lambda *a, **k: dict(self.turn))):
            mock = patch.object(harness_plus_adapter, name, side_effect=side_effect)
            mock.start()
            self.addCleanup(mock.stop)
        self.payload = {"task_id": self.task_id, "action": "reverify",
                        "expected_execution_run_id": self.run_id}
        self.fake.requests_log.clear()

    def write_files(self):
        (self.directory / "job.json").write_text(json.dumps(self.job))
        (self.directory / "spec.json").write_text(json.dumps(self.spec))
        (self.directory / "events.jsonl").write_text("\n".join(json.dumps(e) for e in self.events) + "\n")

    def state(self):
        conn = db.connect(self.db_path)
        try:
            return verification_recovery._snapshot(conn, self.task_id, self.run_id)
        finally:
            conn.close()

    def setUp(self):
        self._fixture_set_up()
        (self.worktree / "unchanged.txt").unlink()
        self.worktree.rmdir()
        subprocess.run(["git", "-C", str(self.repos_root / "Feedback"), "worktree", "add", "--detach", str(self.worktree), "HEAD"], check=True, capture_output=True)
        (self.worktree / "unchanged.txt").write_text("preserve existing changes")
        self.events = [self.events[0], {"seq": 2, "type": "command_output", "name": "exec",
            "input": json.dumps({"command": '\n'.join(command + '; echo $?' for command in self.commands)}),
            "exit_code": 0, "output": "check-passed\n0\n"},
            {"seq": 3, "type": "terminal", "outcome": "completed"}]
        self.write_files()
        self.check_payload = {"task_id": self.task_id, "expected_execution_run_id": self.run_id,
            "expected_plan_sha256": self.plan_sha, "operation_id": "isolated-operation"}
        self.fake.tasks[self.task_id]["status"] = "in-progress"

    def test_independent_exit_codes_recover_without_rewriting_original_events(self):
        original = (self.directory / "events.jsonl").read_bytes()
        result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["verification_ok"])
        self.assertEqual([item["exit_code"] for item in result["checks"]], [0, 0])
        self.assertEqual(result["request"]["kind"], "monitor")
        self.assertEqual(self.task(self.task_id)["status"], "running")
        self.assertEqual((self.directory / "events.jsonl").read_bytes(), original)
        self.assertNotIn("verification_override", db.task_extra(self.state()["task"]))
        self.assertTrue(all(method == "GET" for method, _, _ in self.fake.requests_log))
        again = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(again["deduplicated"])
        self.assertEqual(result["checks"], again["checks"])
        self.assertEqual(result["request"]["request_id"], again["request"]["request_id"])
        status = self.call("get-verification-check-status", {"task_id": self.task_id, "operation_id": "isolated-operation"})
        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["verification_ok"])

    def test_failure_records_true_code_and_runs_remaining_check(self):
        original = verification_checks._run_command
        def failing(ctx, operation_id, command, cwd, environment, output):
            # Run a real failing process and associate it with its frozen command
            # only in this isolated test harness, never in production code.
            result = original(ctx, operation_id, "/usr/bin/false" if command == self.commands[0] else command, cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verification_checks, "_run_command", side_effect=failing):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertFalse(result["verification_ok"])
        self.assertEqual([item["exit_code"] for item in result["checks"]], [1, 0])
        self.assertEqual(self.task(self.task_id)["status"], "needs_user")
        self.assertIsNone(result["request"])
        self.assertEqual(self.call_err("resolve", self.payload).code, "conflict")
        # Same operation is idempotent even for failures; retry needs a new ID.
        self.assertTrue(self.call("run-verification-checks", self.check_payload)["deduplicated"])
        result = self.call("run-verification-checks", dict(self.check_payload, operation_id="second-operation"))
        self.assertTrue(result["verification_ok"])

    def test_worktree_plan_output_and_audit_drift_rejected(self):
        self.call("run-verification-checks", self.check_payload)
        status_payload = {"task_id": self.task_id, "operation_id": "isolated-operation"}
        path = self.worktree / "unchanged.txt"
        path.write_text("changed after verified")
        self.assertFalse(self.call("get-verification-check-status", status_payload)["verification_ok"])
        path.write_text("preserve existing changes")
        self.plan.write_text(self.plan.read_text() + "\nchanged")
        self.assertFalse(self.call("get-verification-check-status", status_payload)["verification_ok"])
        self.plan.write_text(self.plan.read_text()[:-8])
        conn = db.connect(self.db_path)
        record = db.task_extra(db.require_task(conn, self.task_id))["verification_checks"]
        conn.close()
        Path(record["receipt"]["checks"][0]["output_path"]).write_text("forged status")
        self.assertFalse(self.call("get-verification-check-status", status_payload)["verification_ok"])

    def test_changes_during_checks_never_pass(self):
        original = verification_checks._run_command
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.worktree / "unchanged.txt").write_text("changed during checks")
            return result
        with patch.object(verification_checks, "_run_command", side_effect=changing):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertFalse(result["verification_ok"])
        self.assertEqual(self.task(self.task_id)["status"], "needs_user")

    def test_task_write_guard_and_duplicate_click(self):
        original = verification_checks._run_command
        def guarded(*args, **kwargs):
            err = self.call_err("resolve", {"task_id": self.task_id, "action": "accept_verification",
                "note": "must not override running tests"})
            self.assertEqual(err.code, "conflict")
            self.assertEqual(self.call_err("run-verification-checks", self.check_payload).code, "conflict")
            status = self.call("get-verification-check-status", {"task_id": self.task_id, "operation_id": "isolated-operation"})
            self.assertEqual(status["state"], "running")
            self.assertEqual(self.task(self.task_id)["verification_checks"]["operation_id"], "isolated-operation")
            return original(*args, **kwargs)
        with patch.object(verification_checks, "_run_command", side_effect=guarded):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["verification_ok"])

    def test_interrupted_guard_never_becomes_success_and_new_operation_allowed(self):
        conn = db.connect(self.db_path)
        task = db.require_task(conn, self.task_id)
        db.set_extra(conn, task, util.now_iso(), verification_checks={"operation_id": "interrupted-op",
            "state": "running", "owner_pid": None, "command_pid": None})
        conn.close()
        result = self.call("get-verification-check-status", {"task_id": self.task_id, "operation_id": "interrupted-op"})
        self.assertEqual(result["state"], "interrupted")
        self.assertNotIn("verification_ok", result)
        self.assertTrue(self.call("run-verification-checks", self.check_payload)["verification_ok"])

    def test_environment_reuses_literal_frozen_path_but_not_substitution(self):
        directory = self.root / "local-bin"
        directory.mkdir()
        executable = directory / "fixturetool"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        event = {"seq": 2, "type": "command_output", "name": "exec", "exit_code": 0,
            "input": json.dumps({"command": 'export PATH="' + str(directory) + ':$PATH"\nfixturetool; echo $?'}), "output": "0"}
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        task = db.require_task(conn, self.task_id)
        env = verification_checks._verification_environment(conn, task, [self.events[0], event, self.events[-1]])
        self.assertTrue(env["PATH"].startswith(str(directory) + ":"))
        event["input"] = json.dumps({"command": 'export PATH="$(echo /bad):$PATH"\nfixturetool'})
        self.assertEqual(verification_checks._verification_environment(conn, task, [event])["PATH"], os.environ["PATH"])

    def test_project_environment_overrides_path_and_redacts_values_from_receipt_and_logs(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        task = db.require_task(conn, self.task_id)
        secret = "private-project-injected-value"
        project_env = {"PATH": os.environ["PATH"], "FIXTURE_VALUE": secret}
        from kanpilot import project_config
        _, _, items, _ = project_config.load_raw_mappings(conn)
        items[task["project_id"]]["verify_env"] = project_env
        setting = db.get_setting(conn, "project_mappings") or {"version": 1, "revision": 0}
        setting["items"] = items
        db.set_setting(conn, "project_mappings", setting)
        env = verification_checks._verification_environment(conn, task, [])
        self.assertEqual(env["PATH"], project_env["PATH"])
        original = verification_checks._run_command
        def injected(ctx, operation_id, command, cwd, environment, output):
            result = original(ctx, operation_id, '/usr/bin/printf %s "$FIXTURE_VALUE"', cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verification_checks, "_run_command", side_effect=injected):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["verification_ok"])
        fresh = db.require_task(conn, self.task_id)
        receipt = db.task_extra(fresh)["verification_checks"]["receipt"]
        self.assertEqual(receipt["verify_env_keys"], sorted(project_env))
        self.assertEqual(receipt["verify_env_sha256"], util.sha256_text(util.jdump(project_env)))
        self.assertEqual(len(receipt["verify_env_sha256"]), 64)
        self.assertNotIn(secret, util.jdump(receipt))
        for check in receipt["checks"]:
            self.assertNotIn(secret, Path(check["output_path"]).read_text())
            self.assertIn("«redacted»", check["output_tail"])
        for event in conn.execute("SELECT detail_json FROM events"):
            self.assertNotIn(secret, event["detail_json"] or "")

    def test_short_sensitive_environment_values_are_redacted_across_output_blocks(self):
        from kanpilot.engine import verdict
        project_env = {"PASSWORD": "123456", "TOKEN": "abcdef", "SECRET": "uvwxyz",
                       "API_KEY": "987654", "AUTHORIZATION": "auth56", "KP_VERIFY_READY": "1"}
        original = verification_checks._run_command
        def injected(ctx, operation_id, command, cwd, environment, output):
            script = ('printf "%4093s" ""; printf "%s|%s|%s|%s|%s|ready=%s" '
                      '"$PASSWORD" "$TOKEN" "$SECRET" "$API_KEY" "$AUTHORIZATION" "$KP_VERIFY_READY"; '
                      'printf "%5000s" ""')
            result = original(ctx, operation_id, script, cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verdict, "project_verify_env", return_value=project_env), \
                patch.object(verification_checks, "_run_command", side_effect=injected):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["verification_ok"])
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        receipt = db.task_extra(db.require_task(conn, self.task_id))["verification_checks"]["receipt"]
        logs = "".join(Path(check["output_path"]).read_text() for check in receipt["checks"])
        persisted = util.jdump(receipt) + logs + "".join(row[0] or "" for row in conn.execute("SELECT detail_json FROM events"))
        for key, value in project_env.items():
            if key != "KP_VERIFY_READY":
                self.assertNotIn(value, persisted, key)
        self.assertIn("ready=1", logs)
        self.assertIn("«redacted»", logs)

    def test_project_environment_is_read_once_for_execution_receipt_and_redaction(self):
        from kanpilot.engine import verdict
        first = {"SNAPSHOT_A": "first-private-snapshot"}
        second = {"SNAPSHOT_B": "second-private-snapshot"}
        original = verification_checks._run_command
        observed = []
        def injected(ctx, operation_id, command, cwd, environment, output):
            observed.append({key: environment[key] for key in ("SNAPSHOT_A", "SNAPSHOT_B") if key in environment})
            result = original(ctx, operation_id, 'printf "%s%s" "$SNAPSHOT_A" "$SNAPSHOT_B"', cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verdict, "project_verify_env", side_effect=[first, second]) as read, \
                patch.object(verification_checks, "_run_command", side_effect=injected):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["verification_ok"])
        self.assertEqual(observed, [first] * len(self.commands))
        self.assertEqual(read.call_count, 1)
        receipt = db.task_extra(self.state()["task"])["verification_checks"]["receipt"]
        self.assertEqual(receipt["verify_env_keys"], sorted(first))
        self.assertEqual(receipt["verify_env_sha256"], util.sha256_text(util.jdump(first)))
        for check in receipt["checks"]:
            self.assertEqual(Path(check["output_path"]).read_text(), "«redacted»")

    def test_failed_interactive_checks_only_persist_redacted_output_and_never_queue_recovery(self):
        from kanpilot.engine import verdict
        original = verification_checks._run_command
        def injected(ctx, operation_id, command, cwd, environment, output):
            return original(ctx, operation_id, "printf 'SDK location not found.\\nsecret=%s' \"$PASSWORD\"; exit 1",
                            cwd, environment, output)
        with patch.object(verdict, "project_verify_env", return_value={"PASSWORD": "a"}), \
                patch.object(verification_checks, "_run_command", side_effect=injected):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertFalse(result["verification_ok"])
        self.assertIsNone(result["request"])
        task = self.state()["task"]
        self.assertEqual(task["status"], "needs_user")
        receipt = db.task_extra(task)["verification_checks"]["receipt"]
        for check in receipt["checks"]:
            self.assertEqual(Path(check["output_path"]).read_text(), "SDK loc«redacted»tion not found.\nsecret=«redacted»")
        self.assertNotIn("secret=a", util.jdump(receipt))

    def test_explicit_project_path_resolves_conflicting_historical_prefixes(self):
        from kanpilot.engine import verdict
        events = [{"seq": index + 2, "type": "command_output", "name": "exec", "exit_code": 0,
                   "input": json.dumps({"command": 'export PATH="/' + name + ':$PATH"\nfixturetool'}), "output": "0"}
                  for index, name in enumerate(("first", "second"))]
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        task = db.require_task(conn, self.task_id)
        with patch.object(verdict, "project_verify_env", return_value={"PATH": "/explicit/project/bin"}):
            environment = verification_checks._verification_environment(conn, task, events)
        self.assertEqual(environment["PATH"], "/explicit/project/bin")
        with patch.object(verdict, "project_verify_env", return_value={}):
            with self.assertRaises(util.KanpilotError) as caught:
                verification_checks._verification_environment(conn, task, events)
        self.assertEqual(caught.exception.code, "conflict")

    def test_timeouts_are_failures_and_leave_no_command_process(self):
        original = verification_checks._run_command
        def slow(ctx, operation_id, command, cwd, environment, output):
            result = original(ctx, operation_id, "/bin/sleep 5" if command == self.commands[0] else command, cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verification_checks, "CHECK_TIMEOUT_SECONDS", 0.05), patch.object(verification_checks, "_run_command", side_effect=slow):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertFalse(result["verification_ok"])
        self.assertTrue(result["checks"][0]["timed_out"])
        self.assertIsNone(db.task_extra(self.state()["task"])["verification_checks"]["command_pid"])

    def test_timeout_cleans_ignoring_descendant_even_if_shell_exits(self):
        marker = self.root / "must-not-be-written"
        descendant = self.root / "ignores-term.py"
        descendant.write_text("import signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(1)\nPath(" + repr(str(marker)) + ").write_text('survived')\n")
        original = verification_checks._run_command
        def background(ctx, operation_id, command, cwd, environment, output):
            script = sys.executable + " " + str(descendant) + " &\nwait"
            result = original(ctx, operation_id, script if command == self.commands[0] else command, cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verification_checks, "CHECK_TIMEOUT_SECONDS", 0.3), patch.object(verification_checks, "_run_command", side_effect=background):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["checks"][0]["timed_out"])
        time.sleep(1)
        self.assertFalse(marker.exists())

    def test_spawn_registration_failure_never_starts_source_command(self):
        marker = self.root / "unregistered-command"
        original_run, original_update = verification_checks._run_command, verification_checks._update_operation
        armed = [True]
        def failed_registration(ctx, operation_id, **fields):
            if armed[0] and fields.get("command_pid"):
                armed[0] = False
                raise util.KanpilotError("conflict", "simulated registration interruption")
            return original_update(ctx, operation_id, **fields)
        def marking(ctx, operation_id, command, cwd, environment, output):
            result = original_run(ctx, operation_id, "/usr/bin/touch " + str(marker), cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verification_checks, "_update_operation", side_effect=failed_registration), patch.object(verification_checks, "_run_command", side_effect=marking):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertFalse(result["verification_ok"])
        self.assertFalse(marker.exists())

    def test_direct_database_request_and_task_writes_are_guarded(self):
        original = verification_checks._run_command
        def competing(*args, **kwargs):
            conn = db.connect(self.db_path)
            try:
                with self.assertRaises(util.KanpilotError):
                    db.create_request(conn, util.now_iso(), "plan", "competing-plan", task_id=self.task_id)
                with self.assertRaises(util.KanpilotError):
                    db.update_task(conn, self.task_id, util.now_iso(), status="needs_plan")
                with self.assertRaises(util.KanpilotError):
                    db.update_request(conn, self.monitor_request_id, util.now_iso(), status="queued")
            finally:
                conn.close()
            return original(*args, **kwargs)
        with patch.object(verification_checks, "_run_command", side_effect=competing):
            self.assertTrue(self.call("run-verification-checks", self.check_payload)["verification_ok"])

    def test_recovery_failure_preserves_completed_checks_and_retries_same_operation(self):
        with patch.object(verification_recovery, "recover", side_effect=util.KanpilotError("remote_unavailable", "connection temporarily unavailable")):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["verification_ok"])
        self.assertTrue(result["recovery_pending"])
        with patch.object(verification_checks, "_run_command", side_effect=AssertionError("must not rerun completed commands")):
            result = self.call("run-verification-checks", self.check_payload)
        self.assertTrue(result["deduplicated"])
        self.assertFalse(result["recovery_pending"])
        self.assertIsNotNone(result["request"])

    def test_core_registered_commit_preserves_receipt_but_manual_commit_does_not(self):
        self.call("run-verification-checks", self.check_payload)
        subprocess.run(["git", "-C", str(self.worktree), "switch", "-c", "isolated-delivery"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.worktree), "add", "unchanged.txt"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid", "commit", "-m", "checked content"], check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(self.worktree), "rev-parse", "HEAD"]).decode().strip()
        payload = {"task_id": self.task_id, "operation_id": "isolated-operation"}
        self.assertFalse(self.call("get-verification-check-status", payload)["verification_ok"])
        conn = db.connect(self.db_path)
        from kanpilot import core
        core.update_delivery(conn, db.require_task(conn, self.task_id), util.now_iso(), branch="isolated-delivery",
            worktree_path=str(self.worktree), head_sha=sha, commits=[{"execution_run_id": self.run_id, "sha": sha}])
        conn.close()
        self.assertTrue(self.call("get-verification-check-status", payload)["verification_ok"])

    def test_old_operation_has_terminal_status_after_replacement(self):
        original = verification_checks._run_command
        def failed(ctx, operation_id, command, cwd, environment, output):
            result = original(ctx, operation_id, "/usr/bin/false", cwd, environment, output)
            result["command"] = command
            return result
        with patch.object(verification_checks, "_run_command", side_effect=failed):
            self.call("run-verification-checks", self.check_payload)
        self.call("run-verification-checks", dict(self.check_payload, operation_id="new-operation"))
        status = self.call("get-verification-check-status", {"task_id": self.task_id, "operation_id": "isolated-operation"})
        self.assertEqual(status["state"], "completed")
        self.assertFalse(status["verification_ok"])
        self.assertIn("取代", status["detail"])

    def test_remote_requirement_drift_blocks_before_any_check(self):
        self.fake.tasks[self.task_id]["description"] = "changed remote requirement"
        with patch.object(verification_checks, "_run_command", side_effect=AssertionError("old requirement must not run")):
            error = self.call_err("run-verification-checks", self.check_payload)
        self.assertEqual(error.code, "remote_conflict")

    def test_no_external_process_is_spawned_while_sqlite_writer_lock_is_held(self):
        original_txn, original_process = db.write_txn, subprocess.Popen
        inside = [False]
        @contextmanager
        def tracked_txn(conn):
            with original_txn(conn):
                inside[0] = True
                try:
                    yield
                finally:
                    inside[0] = False
        def checked_process(*args, **kwargs):
            self.assertFalse(inside[0], "external process spawned while holding SQLite write lock")
            return original_process(*args, **kwargs)
        with patch.object(db, "write_txn", tracked_txn), patch.object(subprocess, "Popen", side_effect=checked_process):
            self.assertTrue(self.call("run-verification-checks", self.check_payload)["verification_ok"])

    def test_real_core_commit_then_interrupted_monitor_can_consume_receipt(self):
        from kanpilot import core
        repo = self.repos_root / "Feedback"
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@local.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "switch", "-c", "core-delivery"], check=True, capture_output=True)
        base = subprocess.check_output(["git", "-C", str(self.worktree), "rev-parse", "HEAD"]).decode().strip()
        base_branch = subprocess.check_output(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"]).decode().strip()
        conn = db.connect(self.db_path)
        core.update_delivery(conn, db.require_task(conn, self.task_id), util.now_iso(), mode="manual",
            workspace_version=1, worktree_path=str(self.worktree), branch="core-delivery",
            base_branch=base_branch, base_sha=base, head_sha=base, commits=[])
        conn.close()
        result = self.call("run-verification-checks", self.check_payload)
        request_id = result["request"]["request_id"]
        conn = db.connect(self.db_path)
        db.update_request(conn, request_id, util.now_iso(), status="claimed", run_id="first-monitor",
            claimed_by="kaneo-monitor", lease_expires_at=util.plus_seconds(3600, util.now_dt()))
        conn.close()
        commit = self.call("delivery-commit", {"task_id": self.task_id, "request_id": request_id,
            "run_id": "first-monitor", "expected_execution_run_id": self.run_id})
        self.assertEqual(commit["outcome"], "committed")
        # The monitor disconnects before advance; the next claim sees the core's
        # new HEAD and must retain its current verification, not block again.
        conn = db.connect(self.db_path)
        db.update_request(conn, request_id, util.now_iso(), status="failed")
        second, _ = db.create_request(conn, util.now_iso(), "monitor", "second-monitor", task_id=self.task_id)
        db.update_request(conn, second["request_id"], util.now_iso(), status="claimed", run_id="second-monitor",
            claimed_by="kaneo-monitor", lease_expires_at=util.plus_seconds(3600, util.now_dt()))
        conn.close()
        status = self.call("get-verification-check-status", {"task_id": self.task_id, "operation_id": "isolated-operation"})
        self.assertTrue(status["verification_ok"])
        repeated = self.call("delivery-commit", {"task_id": self.task_id, "request_id": second["request_id"],
            "run_id": "second-monitor", "expected_execution_run_id": self.run_id})
        self.assertEqual(repeated["outcome"], "already_committed")
        self.assertEqual(repeated["sha"], commit["sha"])
        advance_payload = {"task_id": self.task_id, "run_id": "second-monitor",
            "target": "in-review", "expected_current": "in-progress"}
        original_receipt = verification_checks.valid_receipt
        def drift_round(conn, task):
            proof = original_receipt(conn, task)
            conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
                (str(self.root / "changed-round"), self.task_id, self.run_id))
            return proof
        with patch.object(verification_checks, "valid_receipt", side_effect=drift_round):
            error = self.call_err("advance-task", advance_payload)
        self.assertEqual(error.code, "conflict")
        self.assertTrue(all(method == "GET" for method, _, _ in self.fake.requests_log))
        conn = db.connect(self.db_path)
        conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
            (str(self.worktree), self.task_id, self.run_id))
        conn.close()
        original_txn, original_process, original_read = db.write_txn, subprocess.Popen, Path.read_bytes
        inside = [False]
        @contextmanager
        def tracked_txn(conn):
            with original_txn(conn):
                inside[0] = True
                try:
                    yield
                finally:
                    inside[0] = False
        def process_outside_txn(*args, **kwargs):
            self.assertFalse(inside[0], "advance must validate Git/process evidence outside SQL writer lock")
            return original_process(*args, **kwargs)
        def read_outside_txn(path):
            self.assertFalse(inside[0], "advance receipt path must read its evidence outside SQL writer lock")
            return original_read(path)
        with patch.object(db, "write_txn", tracked_txn), patch.object(subprocess, "Popen", side_effect=process_outside_txn), patch.object(Path, "read_bytes", read_outside_txn):
            advance = self.call("advance-task", advance_payload)
        self.assertEqual(advance["kaneo_status"], "in-review")
        self.assertEqual(self.task(self.task_id)["status"], "in_review")

    def test_recycled_pid_does_not_own_old_verification(self):
        self.assertFalse(verification_checks._alive({"state": "running", "owner_pid": os.getpid(),
            "owner_birth": {"started_seconds": 0, "started_microseconds": 0}}))

    def test_shared_review_collector_copies_authenticated_checks_and_full_output(self):
        from kanpilot import pi_desktop_review
        self.call("run-verification-checks", self.check_payload)
        materials = self.root / "review-materials"
        conn = db.connect(self.db_path)
        try:
            task = self.task(self.task_id)
            result = pi_desktop_review.collect_materials(db_path=self.db_path, conn=conn,
                materials=materials, task=task, execution_worktree=self.worktree,
                execution_run_id=self.run_id)
            manifest = json.loads(Path(result["files"]["core-verification-checks.json"]).read_text())
            self.assertTrue(manifest["receipt"]["verification_ok"])
            self.assertEqual(manifest["receipt"]["binding"]["execution_run_id"], self.run_id)
            self.assertIn("不替代", manifest["note"])
            for check, output in zip(manifest["receipt"]["checks"], manifest["outputs"]):
                copied = Path(result["files"][output["material"]])
                self.assertEqual(copied.read_bytes(), Path(check["output_path"]).read_bytes())
                self.assertEqual(util.sha256_file(copied), check["output_sha256"])
            original_events = (self.directory / "events.jsonl").read_bytes()
            self.assertEqual(Path(result["files"]["execution-events.jsonl"]).read_bytes(), original_events)
            # A tampered core output must block material collection, never turn
            # into trusted Review evidence or silently disappear from its input.
            Path(manifest["receipt"]["checks"][0]["output_path"]).write_text("forged output")
            with self.assertRaises(util.KanpilotError) as error:
                pi_desktop_review.collect_materials(db_path=self.db_path, conn=conn,
                    materials=self.root / "changed-materials", task=task,
                    execution_worktree=self.worktree, execution_run_id=self.run_id)
            self.assertEqual(error.exception.code, "review_blocked")
        finally:
            conn.close()
