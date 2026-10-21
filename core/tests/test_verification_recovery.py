"""Reverification uses isolated files/SQLite and read-only fake Plus status."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from helpers import KanpilotTestCase, goal_checks, goal_report_read
from kanpilot import (cmd_read, core, db, harness_plus_adapter, round_store, util,
                      verification_recovery)


class TestVerificationRecovery(KanpilotTestCase):
    task_id = "reverify-task"
    run_id = "completed-run"
    commands = ["dart format --output=none lib/a.dart", "flutter test --no-pub test/a_test.dart"]

    def setUp(self):
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
        # Plus's Goal report is the only result source; each test edits this fake read.
        self.goal = {"checks": goal_checks(self.commands), "turn_id": "execution-turn",
                     "approved": None, "state": "ready", "structured": True,
                     "execution_status": "completed", "verdict": "met"}
        goal_read = patch.object(cmd_read, "read_goal_report", side_effect=lambda *a, **k: goal_report_read(
            self.goal["checks"], turn_id=self.goal["turn_id"], approved=self.goal["approved"], state=self.goal["state"],
            structured=self.goal["structured"], execution_status=self.goal["execution_status"],
            verdict=self.goal["verdict"]))
        goal_read.start()
        self.addCleanup(goal_read.stop)

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

    def mutate(self, **fields):
        conn = db.connect(self.db_path)
        db.update_task(conn, self.task_id, util.now_iso(), **fields)
        conn.close()

    def reject(self, payload=None, code="conflict"):
        before = self.state()
        error = self.call_err("resolve", payload or self.payload)
        self.assertIsInstance(error, util.KanpilotError, repr(error))
        self.assertEqual(error.code, code)
        self.assertEqual(before, self.state())

    def test_success_preserves_round_plan_worktree_and_automation(self):
        conn = db.connect(self.db_path)
        task = db.require_task(conn, self.task_id)
        core.update_automation(conn, task, util.now_iso(), manual_takeover=True)
        db.update_task(conn, self.task_id, util.now_iso(), auto_advance_paused=1)
        conn.close()
        before = self.state()
        self.assertIn("reverify", self.task(self.task_id)["allowed_actions"])
        result = self.call("resolve", self.payload)
        after = self.state()
        self.assertEqual(result["task_status"], "running")
        self.assertEqual(result["request"]["kind"], "monitor")
        self.assertEqual(after["round"], before["round"])
        self.assertIsNone(after["task"]["failure_code"])
        for key in ("run_id", "plan_path", "plan_sha256", "sent", "send_state", "session_id",
                    "extra_json", "auto_advance_paused"):
            self.assertEqual(after["task"][key], before["task"][key])
        self.assertNotIn("verification_override", db.task_extra(after["task"]))
        self.assertEqual((self.worktree / "unchanged.txt").read_text(), "preserve existing changes")
        self.assertEqual(len(after["requests"]), len(before["requests"]) + 1)
        audit = next(e for e in after["events"] if e["kind"] == "verification_reverified")
        self.assertEqual(util.jload(audit["detail_json"])["previous"]["failure_code"], "verification_unconfirmed")
        self.assertFalse(self.fake.requests_log)

    def test_expected_current_run_is_mandatory(self):
        for payload in ({"task_id": self.task_id, "action": "reverify"},
                        {**self.payload, "expected_execution_run_id": ""}):
            self.reject(payload, "bad_request")
        self.reject({**self.payload, "expected_execution_run_id": "old-run"})

    def test_db_candidate_rejects_wrong_status_round_and_active_requests(self):
        original = self.state()["task"]
        for fields in ({"status": "running"}, {"stage": "review"}, {"sent": 0},
                       {"send_state": "unknown"}, {"save_state": "trashed"},
                       {"failure_code": "execution_failed"}, {"session_id": "different"}):
            self.mutate(**fields)
            self.reject()
            self.mutate(**{key: original[key] for key in fields})
        for fields in ({"save_state": "trashed"}, {"backend": "pi"}, {"session_id": "different"}):
            conn = db.connect(self.db_path)
            old = dict(round_store.get_round(conn, self.task_id, self.run_id))
            for key, value in fields.items():
                conn.execute(f"UPDATE execution_rounds SET {key}=? WHERE task_id=?", (value, self.task_id))
            conn.close()
            self.reject()
            conn = db.connect(self.db_path)
            for key in fields:
                conn.execute(f"UPDATE execution_rounds SET {key}=? WHERE task_id=?", (old[key], self.task_id))
            conn.close()
        conn = db.connect(self.db_path)
        db.create_request(conn, util.now_iso(), "review", "review:" + self.task_id, task_id=self.task_id)
        conn.close()
        self.reject()
        self.assertNotIn("reverify", self.task(self.task_id)["allowed_actions"])

    def test_job_spec_identity_and_terminal_guards(self):
        for filename, original, changes in (
            ("job.json", self.job, [{"task_id": "other"}, {"run_id": "other"}, {"backend": "pi"},
                                   {"status": "running"}, {"exit_code": None}, {"exit_code": False},
                                   {"send_state": "unknown"}, {"plan_sha256": "other"},
                                   {"session_id": "other"}, {"completion_policy": "goal_report"},
                                   {"observe_pid": os.getpid()}, {"pid": os.getpid()}]),
            ("spec.json", self.spec, [{"run_id": "other"}, {"plan_path": "/other"},
                                     {"plan_sha256": "other"}, {"repository": "/other"},
                                     {"cwd": "/other"}, {"config": {}},
                                     {"completion_policy": "goal_report"}]),
        ):
            for change in changes:
                (self.directory / filename).write_text(json.dumps({**original, **change}))
                self.reject()
            (self.directory / filename).write_text(json.dumps(original))

    def test_complete_events_and_matching_identity_are_required(self):
        path = self.directory / "events.jsonl"
        raw = path.read_text()
        for text in (raw + '{"seq":', raw + "[]\n", raw.replace('"completed"', '"failed"'),
                     raw.replace('"session"', '"other"'), raw.replace('"seq": 3', '"seq": 2'),
                     raw.replace('"seq": 1', '"seq": 0'), raw.replace('"seq": 3', '"seq": 9'),
                     raw + json.dumps({"seq": 5, "type": "assistant_message", "content": "later"}) + "\n"):
            path.write_text(text)
            self.reject()
        path.write_text(raw)
        self.events[1]["run_id"] = "other"
        self.write_files()
        self.reject()

    def test_report_missing_unknown_or_failed_check_blocks_recovery(self):
        passed = goal_checks(self.commands)
        for checks in (passed[:1],                                                  # no record for the second command
                       [passed[0], dict(passed[1], exitCode=None)],                 # exit code is not an integer
                       [passed[0], dict(passed[1], result="failed")],               # contradictory result
                       [passed[0], dict(passed[1], exitCode=1, result="failed")]):  # real failure
            self.goal["checks"] = checks
            self.reject()

    def test_transcript_results_never_override_a_passing_report(self):
        # Transcript tool results are audit data; the Plus Goal report decides.
        self.events[1]["exit_code"] = 1
        self.write_files()
        self.assertEqual(self.call("resolve", self.payload)["task_status"], "running")

    def test_reverify_reports_the_exact_check_gap_and_preserves_the_block(self):
        passed = goal_checks(self.commands)
        for checks, reason in ((passed[:1], "缺少 1 项检查记录"),
                               ([passed[0], dict(passed[1], exitCode=None)], "缺少整数退出码"),
                               ([passed[0], dict(passed[1], result="failed")], "检查状态与退出码不一致")):
            self.goal["checks"] = checks
            before = self.state()
            error = self.call_err("resolve", self.payload)
            self.assertEqual(error.code, "conflict")
            self.assertIn(reason, error.message)
            self.assertIn(self.commands[1], error.message)
            self.assertNotIn("未就绪", error.message)
            self.assertEqual(before, self.state())

    def test_report_check_with_inline_env_prefix_matches_frozen_commands(self):
        self.goal["checks"] = goal_checks(["FLUTTER_ROOT=/opt/flutter " + command for command in self.commands])
        self.assertEqual(self.call("resolve", self.payload)["task_status"], "running")

    def test_plan_file_and_frozen_declaration_must_match(self):
        self.plan.write_text(self.plan.read_text() + "changed")
        self.reject()
        self.plan.write_text(self.plan.read_text()[:-7])
        conn = db.connect(self.db_path)
        db.set_extra(conn, db.require_task(conn, self.task_id), util.now_iso(),
                     plan_verify={"plan_sha256": self.plan_sha, "commands": self.commands[:1]})
        conn.close()
        self.reject()

    def test_live_running_continued_permission_and_unknown_turn_rejected(self):
        original = dict(self.status)
        for fields in ({"is_running": True}, {"current_turn_id": "manual-later-turn"},
                       {"pending_permission": True}, {"session_id": "other"}, {"is_running": None},
                       {"raw_status": {"planningState": "awaiting_approval"}}):
            self.status.update(fields)
            self.reject()
            self.status = dict(original)
        for fields in ({"status": "error"}, {"status": "running"}, {"turn_id": "other"},
                       {"session_id": "other"}):
            self.turn = {"session_id": "session", "turn_id": "execution-turn", "status": "completed", **fields}
            self.reject()

    def test_listing_never_reads_plus(self):
        with patch.object(harness_plus_adapter, "plus_status", side_effect=AssertionError("listing is DB only")):
            self.assertIn("reverify", self.task(self.task_id)["allowed_actions"])

    def test_request_entry_only_for_current_blocking_monitor(self):
        view = self.call("get-request", {"request_id": self.monitor_request_id})
        self.assertIn("reverify", view["allowed_actions"])
        conn = db.connect(self.db_path)
        historical, _ = db.create_request(conn, "2026-01-01T00:00:00+00:00", "monitor",
            "monitor:historical", task_id=self.task_id)
        db.update_request(conn, historical["request_id"], "2026-01-01T00:00:00+00:00",
            status="failed", claimed_at="2026-01-01T00:00:00+00:00",
            run_id="historical-claim", finished_at="2026-01-01T01:00:00+00:00")
        conn.close()
        history = self.call("get-request", {"request_id": historical["request_id"]})
        self.assertNotIn("reverify", history["allowed_actions"])
        self.assertIn("reverify", self.call("get-request", {"request_id": self.monitor_request_id})["allowed_actions"])

    def test_request_entry_rejects_stale_execution_and_block_identity(self):
        self.mutate(blocked_at="2026-01-01T00:00:00+00:00")
        self.assertNotIn("reverify", self.call("get-request", {"request_id": self.monitor_request_id})["allowed_actions"])

        conn = db.connect(self.db_path)
        request = db.get_request(conn, self.monitor_request_id)
        db.update_task(conn, self.task_id, util.now_iso(), blocked_at=request["finished_at"], run_id="new-execution")
        round_store.upsert_on_claim(conn, self.task_id, "new-execution", self.config,
                                    "2099-01-01T00:00:00+00:00", backend="pi_desktop")
        round_store.upsert_discovered(conn, self.task_id, "new-execution", "2099-01-01T00:00:00+00:00",
                                     backend="pi_desktop", session_id="session", run_status="finished")
        conn.close()
        self.assertNotIn("reverify", self.call("get-request", {"request_id": self.monitor_request_id})["allowed_actions"])

    def test_request_entry_requires_matching_reported_audit(self):
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM events WHERE request_id=? AND kind='reported'", (self.monitor_request_id,))
        conn.close()
        self.assertNotIn("reverify", self.call("get-request", {"request_id": self.monitor_request_id})["allowed_actions"])

    def test_turn_read_detects_new_live_or_completed_turn_without_file_changes(self):
        for running in (True, False):
            self.status = {"session_id": "session", "is_running": False,
                           "current_turn_id": "execution-turn", "pending_permission": None}
            def race(*args, **kwargs):
                self.status.update(current_turn_id="manual-continuation", is_running=running)
                return {"session_id": "session", "turn_id": "execution-turn", "status": "completed"}
            with patch.object(harness_plus_adapter, "plus_turn_get", side_effect=race):
                self.reject()

    def test_transaction_rechecks_db_requests_and_file_races(self):
        original = self.state()
        for target in ("task", "round", "requests", "file"):
            def race(*args, **kwargs):
                conn = db.connect(self.db_path)
                if target == "task":
                    db.update_task(conn, self.task_id, util.now_iso(), last_progress="raced")
                elif target == "round":
                    conn.execute("UPDATE execution_rounds SET run_status='running' WHERE task_id=?", (self.task_id,))
                elif target == "requests":
                    db.create_request(conn, util.now_iso(), "review", "review:" + self.task_id, task_id=self.task_id)
                else:
                    self.spec["instruction"] = "changed"
                    self.write_files()
                conn.close()
                return dict(self.status)
            with patch.object(harness_plus_adapter, "plus_status", side_effect=race):
                error = self.call_err("resolve", self.payload)
            self.assertEqual(error.code, "conflict")
            after = self.state()
            self.assertEqual(after["task"]["status"], "needs_user")
            self.assertFalse(any(e["kind"] == "verification_reverified" for e in after["events"]))
            conn = db.connect(self.db_path)
            db.update_task(conn, self.task_id, util.now_iso(), last_progress=original["task"]["last_progress"])
            conn.execute("UPDATE execution_rounds SET run_status='finished' WHERE task_id=?", (self.task_id,))
            conn.execute("DELETE FROM requests WHERE task_id=? AND kind='review'", (self.task_id,))
            conn.close()
            self.spec.pop("instruction", None)
            self.write_files()

    def test_repeat_and_concurrent_clicks_queue_one_monitor(self):
        barrier = Barrier(2)
        # Both invocations use the before/after status reads around turn_get.
        with patch.object(harness_plus_adapter, "plus_status", side_effect=lambda *a, **k:
                          (barrier.wait(timeout=5), dict(self.status))[1]):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.call("resolve", self.payload), range(2)))
        self.assertEqual(sum(not r["deduplicated"] for r in results), 1)
        self.assertEqual(len({r["request"]["request_id"] for r in results}), 1)
        before = self.state()
        self.assertTrue(self.call("resolve", self.payload)["deduplicated"])
        self.assertEqual(before, self.state())
        self.mutate(run_id="new-run")
        self.reject()


    def test_historical_environment_and_identity_blocks_can_only_recheck_original_run(self):
        for code in ("verification_environment_failure", "verification_identity_unconfirmed"):
            self.mutate(failure_code=code)
            self.assertIn("reverify", self.task(self.task_id)["allowed_actions"])
            if code == "verification_identity_unconfirmed":
                self.assertNotIn("retry_verification", self.task(self.task_id)["allowed_actions"])
        before = self.state()
        result = self.call("resolve", self.payload)
        self.assertEqual(result["request"]["kind"], "monitor")
        self.assertEqual(before["round"], self.state()["round"])

    def test_old_environment_resume_returns_to_monitor_without_creating_execution(self):
        self.mutate(failure_code="verification_environment_failure")
        result = self.call("resolve", {"task_id": self.task_id, "action": "resume"})
        self.assertEqual(result["request"]["kind"], "monitor")
        self.assertEqual(self.state()["task"]["run_id"], self.run_id)

    def test_historical_core_pass_cannot_override_failed_report_check(self):
        conn = db.connect(self.db_path)
        db.set_extra(conn, db.require_task(conn, self.task_id), util.now_iso(), verification_checks={
            "state": "completed", "binding": {"execution_run_id": self.run_id},
            "receipt": {"verification_ok": True}})
        conn.close()
        self.goal["checks"][1] = dict(self.goal["checks"][1], exitCode=1, result="failed")
        self.reject()

    def test_report_from_another_turn_cannot_recover_this_round(self):
        # The report's turn and this round's execution turn must both sit in the approved chain.
        self.goal["turn_id"] = "drafting-turn"
        self.reject()
        self.goal.update(turn_id="drafting-turn", approved=["execution-turn"])
        self.reject()
        self.goal.update(turn_id="execution-turn", approved=["drafting-turn"])
        self.reject()

    def test_unready_report_stays_unconfirmed(self):
        for state in ("pending", "not_found", "truncated"):
            self.goal["state"] = state
            self.reject(code="verification_unconfirmed")

    def test_report_floor_requires_structured_completed_and_met(self):
        # S1 floor without reviews: a fallback, unfinished or unmet report is not completion evidence.
        defaults = {"structured": True, "execution_status": "completed", "verdict": "met"}
        for field, value in (("structured", False), ("execution_status", "running"), ("verdict", "partial")):
            with self.subTest(field=field):
                self.goal[field] = value
                self.reject(code="verification_unconfirmed")
                self.goal[field] = defaults[field]
        self.assertEqual(self.call("resolve", self.payload)["task_status"], "running")

    def read_goal_evidence(self):
        conn = db.connect(self.db_path)
        try:
            return verification_recovery.read_goal_evidence(conn, db.require_task(conn, self.task_id))
        finally:
            conn.close()

    def test_honest_failed_reports_are_failure_evidence_but_cannot_recover(self):
        self.goal["checks"][1] = dict(self.goal["checks"][1], exitCode=1, result="failed")
        for verdict in ("partial", "blocked"):
            with self.subTest(verdict=verdict):
                self.goal["verdict"] = verdict
                before = self.state()
                with patch.object(subprocess, "run", side_effect=AssertionError("Pilot must not run verify")):
                    evidence = self.read_goal_evidence()
                self.assertFalse(evidence["coverage"]["ok"])
                self.assertEqual(evidence["coverage"]["missing"], [])
                self.assertEqual([row["exit_code"] for row in evidence["coverage"]["failed"]], [1])
                self.assertEqual(evidence["report"]["verdict"], verdict)
                self.assertEqual(before, self.state())
                self.reject()

    def test_nonmet_reports_require_complete_consistent_real_failures(self):
        passed = goal_checks(self.commands)
        failed = dict(passed[1], exitCode=1, result="failed")
        cases = [
            ("partial", passed),
            ("unknown", [passed[0], failed]),
            ("partial", [failed]),
            ("partial", [dict(passed[0], exitCode=None), failed]),
            ("partial", [dict(passed[0], exitCode=True), failed]),
            ("partial", [dict(passed[0], exitCode=1, result="passed"), failed]),
            ("partial", [passed[0], failed, dict(failed)]),
            ("partial", [passed[0], dict(failed, command=" " + self.commands[1])]),
        ]
        for verdict, checks in cases:
            with self.subTest(verdict=verdict, checks=checks):
                self.goal.update(verdict=verdict, checks=checks)
                self.reject(code="verification_unconfirmed")


    def test_versioned_plus_evidence_uses_current_turn_and_preserves_command_results(self):
        self.job["verification_evidence_version"] = 1
        for event in self.events[1:-1]:
            event["turn_id"] = "execution-turn"
        self.write_files()
        conn = db.connect(self.db_path)
        try:
            _, _, result = verification_recovery._read_evidence(conn, db.require_task(conn, self.task_id), require_pass=False)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["command_results"]), 2)
        finally:
            conn.close()
        # Legacy transcript evidence still needs its own execution window when it decides a pass.
        self.events[1]["turn_id"] = "drafting-turn"
        self.write_files()
        conn = db.connect(self.db_path)
        try:
            with self.assertRaises(util.KanpilotError) as caught:
                verification_recovery._read_evidence(conn, db.require_task(conn, self.task_id))
            self.assertEqual(caught.exception.code, "conflict")
        finally:
            conn.close()
