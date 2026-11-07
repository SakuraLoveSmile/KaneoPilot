"""Same-run continuation and verification-only retries with disposable evidence."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import test_execution_retry as retry_fixtures
from test_session import pi_event, write_json, write_jsonl
from kanpilot import (cmd_dispatch, cmd_exec, cmd_queue, cmd_read, db, derive, execution_contract,
                      execution_continuation as continuation, execution_status,
                      headless_adapter, pi_desktop_worker, repair_prompt, runtime_client,
                      runtime_operations, session_control, util)
from kanpilot.engine import verdict


class ExecutionContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f = retry_fixtures.TestExecutionRetry(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        plan = Path(f.task()["plan_path"])
        plan.write_text(plan.read_text() + "\nORIGINAL_IMPLEMENTATION_MARKER\n```verify\npython3 -m unittest\n```\n")
        sha = util.sha256_file(plan)
        db.update_task(f.ctx.conn, f.task_id, f.ctx.now_iso, plan_sha256=sha,
                       failure_reason="turn error: NETWORK_ERROR")
        db.set_extra(f.ctx.conn, f.task(), f.ctx.now_iso,
                     plan_verify={"plan_sha256": sha, "commands": ["python3 -m unittest"]})
        scope = runtime_client.stable_scope_id("execution", task_id=f.task_id, run_id=f.run_id)
        runtime_operations.begin_operation(f.ctx.conn, scope, "fixture-session", "pi_desktop",
                                           "kp_runtime_session_create", {})
        runtime_operations.complete_operation(f.ctx.conn, scope, "fixture-session", session_id="failed-session")
        f.job.update(scope_id=scope, detail="turn error: NETWORK_ERROR")
        write_json(f.directory / "job.json", f.job)
        self.control = {"task_id": f.task_id, "run_id": f.run_id, "backend": "pi_desktop",
                        "state": "idle", "detail": "当前会话空闲", "connected": True,
                        "session_id": "failed-session", "observed_turn_id": "continued-turn",
                        "continued_turn_id": "continued-turn", "continued_state": "finished",
                        "continued_detail": "手动继续已完成",
                        "original_turn_ended_at": "2026-11-07T10:00:00Z",
                        "continued_ended_at": "2026-11-07T10:10:00Z"}
        control = patch.object(session_control, "cmd_session_control_status",
                               side_effect=lambda *a, **k: dict(self.control))
        control.start()
        self.addCleanup(control.stop)
        exited = patch.object(session_control, "_process_exited", return_value=True)
        exited.start()
        self.addCleanup(exited.stop)
        self.request_count = f.ctx.conn.execute("SELECT count(*) FROM requests").fetchone()[0]

    def check(self):
        f = self.f
        return execution_status.cmd_check_execution_status(f.ctx, {"task_id": f.task_id, "run_id": f.run_id})

    def scheduler_tick(self):
        f = self.f
        with db.write_txn(f.ctx.conn):
            result = cmd_read.tick_inline(f.ctx.conn, util.now_dt())
        self.assertFalse([row for row in result["created"] if row["kind"] == "monitor"])
        self.assertEqual(f.task()["status"], "needs_user")
        self.assertEqual(f.task()["failure_code"], "execution_failed")

    def payload(self):
        f = self.f
        candidate = self.check()["continuation"]
        return {"task_id": f.task_id, "run_id": f.run_id,
                "expected_session_id": candidate["session_id"], "recovery_turn_id": candidate["turn_id"],
                "expected_plan_sha256": candidate["plan_sha256"], "expected_job_sha256": candidate["job_sha256"]}

    def finish(self):
        f = self.f
        f.job.update(status="finished", exit_code=0, turn_id="continued-turn", ended_at=util.now_iso())
        write_json(f.directory / "job.json", f.job)
        write_jsonl(f.directory / "events.jsonl", [
            pi_event(1, "session", session_id="failed-session", turn_id="failed-turn"),
            pi_event(2, "turn_adopted", **{"from": "failed-turn", "to": "continued-turn"}),
            pi_event(3, "terminal", outcome="completed")])
        for key in ("continued_turn_id", "continued_state", "continued_detail", "original_turn_ended_at", "continued_ended_at"):
            self.control.pop(key, None)
        f.plus_status["current_turn_id"] = "continued-turn"
        f.plus_turn.update(turnId="continued-turn", status="completed")

    def observe_finished(self, *args, **kwargs):
        self.finish()
        return {"status": "settled"}

    def continue_finished(self):
        payload = self.payload()
        with patch.object(cmd_dispatch, "cmd_round_observe", side_effect=self.observe_finished) as observer:
            result = continuation.cmd_continue_execution(self.f.ctx, payload)
        return result, payload, observer

    def test_status_candidate_is_strictly_read_only_and_not_success(self):
        f = self.f
        before = (list(f.ctx.conn.iterdump()), (f.directory / "job.json").read_bytes())
        f.ctx.conn.execute("PRAGMA query_only=ON")
        try:
            result = self.check()
        finally:
            f.ctx.conn.execute("PRAGMA query_only=OFF")
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["continuation"]["turn_id"], "continued-turn")
        self.assertIn("NETWORK_ERROR", result["original_failure"])
        self.assertEqual(before, (list(f.ctx.conn.iterdump()), (f.directory / "job.json").read_bytes()))
        self.assertEqual(f.task()["status"], "needs_user")

    def test_continue_queues_only_same_run_monitor_and_is_idempotent(self):
        f = self.f
        source_before = verdict.git_delivery.source_state_sha256(f.worktree)
        with patch.object(runtime_client, "RuntimeClient") as model:
            result, payload, observer = self.continue_finished()
            replay = continuation.cmd_continue_execution(f.ctx, payload)
            model.assert_not_called()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(replay["request_id"], result["request_id"])
        self.assertTrue(replay["deduplicated"])
        observer.assert_called_once_with(f.ctx, {"task_id": f.task_id, "run_id": f.run_id,
                                               "executor": "user", "recovery_turn_id": "continued-turn"},
                                         preserve_execution_failure=True)
        self.assertEqual(f.task()["run_id"], f.run_id)
        self.assertEqual(f.task()["status"], "running")
        requests = f.ctx.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind!='plan'", (f.task_id,)).fetchall()
        self.assertEqual([r["kind"] for r in requests], ["monitor"])
        self.assertEqual(source_before, verdict.git_delivery.source_state_sha256(f.worktree))
        proof = db.task_extra(f.task())["execution_continuation"]
        self.assertEqual(proof["previous_failure"]["failure_code"], "execution_failed")
        self.assertIn("NETWORK_ERROR", proof["previous_failure"]["failure_reason"])
        self.assertIsNone(f.task()["failure_code"])

    def test_candidate_rejects_running_permission_foreign_and_changed_evidence(self):
        f = self.f
        for changes in ({"state": "running", "continued_state": "running"},
                        {"state": "waiting_permission", "continued_state": "waiting_permission"},
                        {"session_id": "foreign"}, {"continued_turn_id": "foreign"},
                        {"connected": False}):
            original = dict(self.control)
            self.control.update(changes)
            with self.subTest(changes=changes):
                self.assertNotIn("continuation", self.check())
            self.control = original
        payload = self.payload()
        f.job["detail"] = "changed after status check"
        write_json(f.directory / "job.json", f.job)
        with patch.object(cmd_dispatch, "cmd_round_observe") as observer:
            with self.assertRaises(util.KanpilotError):
                continuation.cmd_continue_execution(f.ctx, payload)
            observer.assert_not_called()
        self.assertNotIn("execution_continuation", db.task_extra(f.task()))

    def test_current_block_active_request_and_scope_ownership_hide_candidate(self):
        f = self.f
        db.update_task(f.ctx.conn, f.task_id, f.ctx.now_iso, failure_code="execution_followup_required")
        self.assertNotIn("continuation", self.check())
        db.update_task(f.ctx.conn, f.task_id, f.ctx.now_iso, failure_code="execution_failed")
        request, _ = cmd_queue._new_cycle_request(f.ctx.conn, f.ctx.now_iso, f.task(), "monitor")
        self.assertNotIn("continuation", self.check())
        f.ctx.conn.execute("DELETE FROM requests WHERE request_id=?", (request["request_id"],))
        f.ctx.conn.execute("DELETE FROM runtime_operations")
        self.assertNotIn("continuation", self.check())

    def test_observer_starting_is_not_success_and_duplicate_does_not_relaunch(self):
        f = self.f
        payload = self.payload()
        with patch.object(cmd_dispatch, "cmd_round_observe", return_value={"status": "starting"}) as observer:
            first = continuation.cmd_continue_execution(f.ctx, payload)
        self.assertEqual(first["status"], "starting")
        self.scheduler_tick()
        self.assertEqual(f.ctx.conn.execute("SELECT count(*) FROM requests").fetchone()[0], self.request_count)
        self.assertEqual(f.task()["failure_code"], "execution_failed")
        # Model the observed worker's actual adoption/finalizing checkpoint.
        f.job.update(status="running", turn_id="continued-turn", observe_pid=56789)
        write_json(f.directory / "job.json", f.job)
        for key in ("continued_turn_id", "continued_state", "continued_detail", "original_turn_ended_at", "continued_ended_at"):
            self.control.pop(key, None)
        with patch.object(cmd_dispatch, "_observer_pid_alive", return_value=True), \
             patch.object(cmd_dispatch, "cmd_round_observe") as duplicate:
            self.assertEqual(continuation.cmd_continue_execution(f.ctx, payload)["status"], "starting")
            duplicate.assert_not_called()
        observer.assert_called_once()
        # A dead observer with no successful terminal cannot wait forever.
        with patch.object(cmd_dispatch, "_observer_pid_alive", return_value=False):
            with self.assertRaises(util.KanpilotError):
                continuation.cmd_continue_execution(f.ctx, payload)

    def test_changed_source_or_block_during_observer_never_queues_monitor(self):
        f = self.f
        payload = self.payload()
        def changed(*args, **kwargs):
            self.finish()
            (f.worktree / "tracked.txt").write_text("unexpected changed result")
            return {"status": "settled"}
        with patch.object(cmd_dispatch, "cmd_round_observe", side_effect=changed):
            with self.assertRaises(util.KanpilotError):
                continuation.cmd_continue_execution(f.ctx, payload)
        self.assertEqual(f.ctx.conn.execute("SELECT count(*) FROM requests").fetchone()[0], self.request_count)

    def test_finalized_observer_accepts_fresh_query_hash_and_keeps_initial_failure(self):
        f = self.f
        original = self.payload()
        with patch.object(cmd_dispatch, "cmd_round_observe", return_value={"status": "starting"}):
            self.assertEqual(continuation.cmd_continue_execution(f.ctx, original)["status"], "starting")
        self.finish()
        # The observer can finish between calls, but the original task block
        # stays until the wrapper atomically clears it and enqueues monitor.
        self.scheduler_tick()
        refreshed = self.payload()
        self.assertNotEqual(refreshed["expected_job_sha256"], original["expected_job_sha256"])
        with patch.object(cmd_dispatch, "cmd_round_observe") as observer:
            result = continuation.cmd_continue_execution(f.ctx, refreshed)
            observer.assert_not_called()
        self.assertEqual(result["status"], "queued")
        proof = db.task_extra(f.task())["execution_continuation"]
        self.assertIn("NETWORK_ERROR", proof["previous_failure"]["failure_reason"])
        self.assertEqual(continuation.cmd_continue_execution(f.ctx, original)["request_id"], result["request_id"])

    def test_scheduler_cannot_queue_monitor_before_final_continuation_transaction(self):
        f = self.f
        payload = self.payload()
        def observe(*args, **kwargs):
            self.assertTrue(kwargs["preserve_execution_failure"])
            self.scheduler_tick()
            self.finish()
            # This is the former settled-return / wrapper-transaction gap.
            self.scheduler_tick()
            return {"status": "settled"}
        with patch.object(cmd_dispatch, "cmd_round_observe", side_effect=observe):
            result = continuation.cmd_continue_execution(f.ctx, payload)
        self.assertEqual(result["status"], "queued")
        with db.write_txn(f.ctx.conn):
            tick = cmd_read.tick_inline(f.ctx.conn, util.now_dt())
        self.assertEqual(tick["created"], [])
        requests = f.ctx.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='monitor'", (f.task_id,)).fetchall()
        self.assertEqual([row["request_id"] for row in requests], [result["request_id"]])

    def test_busy_reservation_deduplicates_overlapping_action(self):
        f = self.f
        payload = self.payload()
        duplicates = []
        def observe(*args, **kwargs):
            duplicates.append(continuation.cmd_continue_execution(f.ctx, payload))
            return self.observe_finished(*args)
        with patch.object(cmd_dispatch, "cmd_round_observe", side_effect=observe) as observer:
            result = continuation.cmd_continue_execution(f.ctx, payload)
        observer.assert_called_once()
        self.assertEqual(duplicates[0]["status"], "starting")
        self.assertTrue(duplicates[0]["deduplicated"])
        self.assertEqual(result["status"], "queued")

    def test_expired_action_reservation_can_recover_without_losing_failure(self):
        f = self.f
        payload = self.payload()
        with patch.object(cmd_dispatch, "cmd_round_observe", return_value={"status": "starting"}):
            continuation.cmd_continue_execution(f.ctx, payload)
        proof = db.task_extra(f.task())["execution_continuation"]
        db.set_extra(f.ctx.conn, f.task(), f.ctx.now_iso, execution_continuation=dict(
            proof, busy=True, expires_at="2020-01-01T00:00:00Z"))
        with patch.object(cmd_dispatch, "cmd_round_observe", side_effect=self.observe_finished) as observer:
            result = continuation.cmd_continue_execution(f.ctx, payload)
        self.assertEqual(result["status"], "queued")
        observer.assert_called_once()
        self.assertIn("NETWORK_ERROR", db.task_extra(f.task())["execution_continuation"]["previous_failure"]["failure_reason"])

    def verification_retry(self, intent=True):
        f = self.f
        result, payload, _ = self.continue_finished()
        f.ctx.conn.execute("UPDATE requests SET status='succeeded' WHERE request_id=?", (result["request_id"],))
        db.update_task(f.ctx.conn, f.task_id, f.ctx.now_iso, status="needs_user", stage="monitor",
                       failure_code="verification_unconfirmed", failure_stage="monitor")
        self.assertTrue(derive.task_detail(f.ctx.conn, f.task(), f.ctx.now)["verification_report_supplement"])
        replay = continuation.cmd_continue_execution(f.ctx, payload)
        self.assertEqual(replay["status"], "verification_required")
        payload = dict(f.payload, action="retry_verification")
        if intent is not None:
            payload["verification_report_supplement"] = intent
        resolved = cmd_queue.cmd_resolve(f.ctx, payload)
        claimed = cmd_exec.cmd_claim(f.ctx, {"stage": "dispatch", "executor": "verify-test",
                                           "request_id": resolved["request"]["request_id"]})
        self.assertTrue(claimed["claimed"], claimed)
        retry = db.task_extra(f.task())["execution_retry"]
        self.assertNotEqual(claimed["run_id"], f.run_id)
        spec = {"task_id": f.task_id, "run_id": claimed["run_id"], "request_id": resolved["request"]["request_id"],
                "repository": f.task()["repository"], "cwd": str(f.worktree), "runs_dir": str(f.directory),
                "backend": "pi_desktop", "config": f.job["config"], "completion_policy": "command_verification",
                "required_verification_commands": ["python3 -m unittest"],
                "plan_path": f.task()["plan_path"], "plan_sha256": f.task()["plan_sha256"]}
        continuation.freeze_verification_spec(f.task(), spec)
        return retry, spec

    def test_omitted_supplement_flag_preserves_proof_based_compatibility(self):
        retry, spec = self.verification_retry(intent=None)
        self.assertIn("verification_only", retry)
        self.assertNotIn("ORIGINAL_IMPLEMENTATION_MARKER", repair_prompt.execution_body(spec))

    def test_explicit_full_plan_retry_keeps_worktree_and_original_body(self):
        f = self.f
        original = verdict.git_delivery.source_state_sha256(f.worktree)
        retry, spec = self.verification_retry(intent=False)
        self.assertNotIn("verification_only", retry)
        self.assertNotIn("verification_only", spec)
        self.assertIn("ORIGINAL_IMPLEMENTATION_MARKER", repair_prompt.execution_body(spec))
        self.assertIn("完整执行", cmd_dispatch._execution_instruction(f.task(), f.worktree, desktop=True))
        self.assertEqual(retry["worktree"]["worktree_path"], str(f.worktree))
        self.assertEqual(original, verdict.git_delivery.source_state_sha256(f.worktree))

    def test_supplement_flag_cannot_bypass_special_repair_or_user_action_guard(self):
        f = self.f
        for intent in (True, False):
            for changes in ({"action":"resume"}, {"action":"retry_verification", "actor":"automation"}):
                with self.subTest(intent=intent, changes=changes), self.assertRaises(util.KanpilotError):
                    cmd_queue.cmd_resolve(f.ctx, dict(f.payload, verification_report_supplement=intent, **changes))
        db.set_extra(f.ctx.conn, f.task(), f.ctx.now_iso, fix_request={"source_kind":"merge_conflict"})
        for intent in (True, False):
            with self.subTest(intent=intent), self.assertRaises(util.KanpilotError):
                cmd_queue.cmd_resolve(f.ctx, dict(f.payload, action="retry_verification", verification_report_supplement=intent))
        self.assertEqual(self.request_count, f.ctx.conn.execute("SELECT count(*) FROM requests").fetchone()[0])

    def test_explicit_supplement_intent_never_downgrades_to_full_plan_retry(self):
        f = self.f
        self.finish()
        db.update_task(f.ctx.conn, f.task_id, f.ctx.now_iso, failure_code="verification_unconfirmed",
                       failure_stage="monitor")
        before = self.request_count
        for changes in ({"action":"retry_verification"}, {"action":"resume"},
                        {"action":"retry_verification", "actor":"automation"}):
            with self.subTest(changes=changes), self.assertRaises(util.KanpilotError):
                cmd_queue.cmd_resolve(f.ctx, dict(f.payload, verification_report_supplement=True, **changes))
        self.assertEqual(before, f.ctx.conn.execute("SELECT count(*) FROM requests").fetchone()[0])
        self.assertNotIn("execution_retry", db.task_extra(f.task()))

    def test_missing_report_is_genuine_verification_only_round_with_frozen_body(self):
        f = self.f
        retry, spec = self.verification_retry()
        frozen = retry["verification_only"]
        body = repair_prompt.execution_body(spec)
        self.assertNotIn("ORIGINAL_IMPLEMENTATION_MARKER", body)
        self.assertIn("python3 -m unittest", body)
        self.assertIn("不修复或修改源码", body)
        instruction = cmd_dispatch._execution_instruction(f.task(), f.worktree, desktop=True)
        self.assertIn("仅补验证报告", instruction)
        self.assertNotIn("完整执行", instruction)
        contract = execution_contract.freeze_spec(spec, requirements_sha256=f.task()["requirements_sha256"])
        self.assertEqual(contract["inputs"]["verification_only"], frozen)
        job = pi_desktop_worker._base_job(spec)
        self.assertEqual(job["verification_only"], frozen)
        execution_contract.validate_spec(spec, job=job, starting=True)
        job["verification_evidence_version"] = 1
        job["verification_source_sha256"] = frozen["source_sha256"]
        verdict._require_snapshot(job, frozen["source_sha256"])
        (f.worktree / "tracked.txt").write_text("cannot modify implementation while supplementing")
        changed = verdict.git_delivery.source_state_sha256(f.worktree)
        job["verification_source_sha256"] = changed
        with self.assertRaises(util.KanpilotError):
            verdict._require_snapshot(job, changed)
        with self.assertRaises(util.KanpilotError):
            execution_contract.validate_spec(spec, starting=True)

    def test_verification_only_contract_cannot_be_removed_or_body_forged(self):
        f = self.f
        _, spec = self.verification_retry()
        execution_contract.freeze_spec(spec, requirements_sha256=f.task()["requirements_sha256"])
        for mutation in ("remove", "body", "command", "source", "run"):
            altered = copy.deepcopy(spec)
            if mutation == "remove":
                altered.pop("verification_only")
            elif mutation == "run":
                altered["run_id"] = f.run_id
            else:
                key = {"body": "body", "command": "commands", "source": "source_sha256"}[mutation]
                altered["verification_only"][key] = ["echo fabricated"] if key == "commands" else "forged"
            with self.subTest(mutation=mutation), self.assertRaises(util.KanpilotError):
                execution_contract.validate_spec(altered)
        job = pi_desktop_worker._base_job(spec)
        job.pop("verification_only")
        with self.assertRaises(util.KanpilotError):
            execution_contract.validate_spec(spec, job=job)
