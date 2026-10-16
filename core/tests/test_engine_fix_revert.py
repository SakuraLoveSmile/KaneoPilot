"""Withdrawal never creates a round, changes source bytes, or writes on conflict."""
from unittest.mock import patch

from kanpilot import core, db, derive, engine_fix_revert as revert, remote, util
from kanpilot.engine import loop, verdict
import test_engine_recovery as fixture


class TestEngineFixRevert(fixture.TestEngineRecovery):
    def blocked(self, *, historical=False, detached=False, environment=True):
        if detached:
            self.git(self.worktree, "switch", "--detach")
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery={"mode": "manual"})
        self.quality_failure(empty=False)
        receipt = db.task_extra(self.task())["engine_verdict"]
        if environment:
            # Historical check shape predates environment_reason/environment_reasons.
            receipt["checks"] = [{"command": "gradle verify", "exit_code": 1, "timed_out": False,
                                   "command_not_found": False, "output_tail": "This build uses a Java 8 JVM"}]
        saved = revert.before(self.task())
        saved["automation"]["plan_attempts"] = 1
        at = util.plus_seconds(1, self.ctx.now)
        row = {"execution_run_id": self.run_id, "plan_sha256": self.task()["plan_sha256"],
               "reason": "verify_failed", "model": "primary", "at": at}
        if not historical:
            row["before"] = saved
        db.add_event(self.ctx.conn, self.ctx.now_iso, "dispatch_sent", task_id=self.task_id, detail={"run_id": self.run_id})
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, engine_verdict=receipt, engine_fix_rounds=[row],
                     delivery={"mode": "pr", "branch": "kaneo/orphan", "worktree_path": "/orphan/tree", "pr_url": "stale"})
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso, plan_wanted=True, plan_attempts=0,
                               replan_requested_at=at, last_skip={"reason": "branch_exists"}, fix_round=1)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, status="needs_user", stage="plan",
                       failure_code="branch_exists", failure_stage="plan", sent=0, send_state=None, sent_at=None, lock_retained=0)
        self.revert_payload = {"task_id": self.task_id, "action": "revert_engine_fix",
                               "expected_execution_run_id": self.run_id,
                               "expected_plan_sha256": self.task()["plan_sha256"]}
        return saved, receipt

    def reject_revert(self, payload=None):
        before = dict(self.task())
        counts = tuple(self.ctx.conn.execute("SELECT (SELECT COUNT(*) FROM events),(SELECT COUNT(*) FROM requests)").fetchone())
        error = self.call_err("resolve", payload or self.revert_payload)
        self.assertEqual(dict(self.task()), before)
        self.assertEqual(tuple(self.ctx.conn.execute("SELECT (SELECT COUNT(*) FROM events),(SELECT COUNT(*) FROM requests)").fetchone()), counts)
        return error

    def test_historical_detached_restores_manual_environment_and_refunds(self):
        _, receipt = self.blocked(historical=True, detached=True)
        self.assertIsNotNone(revert.candidate(self.ctx.conn, self.task()))
        original = (self.worktree / "tracked.txt").read_bytes()
        with patch.object(verdict, "binding", wraps=verdict.binding) as binding:
            self.call("resolve", self.revert_payload)
            self.assertFalse(self.ctx.conn.in_transaction)
            self.assertIsNotNone(binding.call_args.kwargs.get("deadline"))
        task = self.task()
        self.assertEqual((task["status"], task["stage"], task["failure_stage"]), ("needs_user", "monitor", "monitor"))
        self.assertEqual(task["failure_code"], "verification_environment_failure")
        self.assertEqual((task["sent"], task["send_state"], task["sent_at"], task["lock_retained"]), (1, "sent", self.ctx.now_iso, 0))
        self.assertEqual(core.delivery_of(task), {"mode": "manual"})
        self.assertEqual(db.task_extra(task)["engine_verdict"], receipt)
        self.assertEqual(core.automation_of(task)["fix_round"], 1)
        detail = derive.task_detail(self.ctx.conn, task, self.ctx.now)
        self.assertEqual(detail["engine_fix_summary"], {"used": 0, "max": 2})
        self.assertIsNone(detail["engine_fix_revert"])
        self.assertNotIn("revert_engine_fix", detail["allowed_actions"])
        self.assertEqual((self.worktree / "tracked.txt").read_bytes(), original)
        event = self.ctx.conn.execute("SELECT detail_json FROM events WHERE kind='engine_fix_reverted'").fetchone()
        self.assertEqual(util.jload(event["detail_json"])["orphaned_branch"]["branch"], "kaneo/orphan")
        self.assertEqual(self.call("resolve", {"task_id": self.task_id, "action": "resume"})["request"]["kind"], "monitor")
        self.assertEqual(self.task()["run_id"], self.run_id)

    def test_before_snapshot_restores_full_delivery_and_keeps_unrelated_extra(self):
        saved, _ = self.blocked()
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, user_note={"preserve": True})
        self.call("resolve", self.revert_payload)
        self.assertEqual(core.delivery_of(self.task()), saved["delivery"])
        self.assertNotIn("pr_url", core.delivery_of(self.task()))
        self.assertEqual(self.task()["lock_retained"], saved["task"]["lock_retained"])
        self.assertEqual(core.automation_of(self.task())["plan_attempts"], 1)
        self.assertFalse(core.automation_of(self.task())["plan_wanted"])
        self.assertNotIn("last_skip", core.automation_of(self.task()))
        self.assertEqual(db.task_extra(self.task())["user_note"], {"preserve": True})

    def test_equal_plan_generation_timestamp_is_still_a_candidate(self):
        self.blocked()
        at = db.task_extra(self.task())["engine_fix_rounds"][-1]["at"]
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, plan_generated_at=at)
        self.assertIsNotNone(revert.candidate(self.ctx.conn, self.task()))
        self.call("resolve", self.revert_payload)
        self.assertEqual(self.task()["stage"], "monitor")

    def test_revert_has_no_io_under_write_lock(self):
        self.blocked()
        original_binding, original_remote = verdict.binding, remote.get_task
        def binding(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return original_binding(*args, **kwargs)
        def read_remote(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return original_remote(*args, **kwargs)
        with patch.object(verdict, "binding", side_effect=binding), patch.object(remote, "get_task", side_effect=read_remote):
            self.call("resolve", self.revert_payload)

    def test_missing_binding_and_missing_historical_dispatch_evidence_reject(self):
        self.blocked(historical=True, detached=True)
        self.ctx.conn.execute("DELETE FROM events WHERE kind='dispatch_sent'")
        self.assertEqual(self.reject_revert().code, "conflict")
        receipt = db.task_extra(self.task())["engine_verdict"]
        receipt.pop("binding")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, engine_verdict=receipt)
        self.assertIsNone(revert.candidate(self.ctx.conn, self.task()))
        self.assertEqual(self.reject_revert().code, "conflict")

    def test_new_record_captures_before_state_before_replan(self):
        payload = self.quality_failure(empty=False)
        original = revert.before(self.task())
        self.call("resolve", payload)
        row = db.task_extra(self.task())["engine_fix_rounds"][-1]
        self.assertEqual(row["before"], original)
        self.assertEqual(row["before"]["task"]["send_state"], "sent")
        self.assertEqual(self.task()["sent"], 0)

    def test_missing_and_stale_expected_fields_are_zero_write(self):
        self.blocked()
        for key in ("expected_execution_run_id", "expected_plan_sha256"):
            payload = dict(self.revert_payload)
            payload.pop(key)
            self.assertEqual(self.reject_revert(payload).code, "bad_request")
            payload[key] = "stale"
            self.assertEqual(self.reject_revert(payload).code, "conflict")

    def test_candidates_exclude_every_consumed_or_changed_shape(self):
        self.blocked()
        original = dict(self.task())
        at = db.task_extra(self.task())["engine_fix_rounds"][-1]["at"]
        for fields in ({"plan_generated_at": util.plus_seconds(1, util.parse_ts(at))}, {"run_id": "other"}, {"plan_sha256": "other"},
                       {"failure_code": "other"}, {"failure_stage": "monitor"}):
            db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, **fields)
            self.assertIsNone(revert.candidate(self.ctx.conn, self.task()))
            self.assertEqual(self.reject_revert().code, "conflict")
            db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, **{key: original[key] for key in fields})
        request, _ = db.create_request(self.ctx.conn, at, "plan", "plan:"+self.task_id, task_id=self.task_id)
        self.assertEqual(self.reject_revert().code, "conflict")
        db.update_request(self.ctx.conn, request["request_id"], at, status="cancelled")
        self.assertEqual(self.reject_revert().code, "conflict")
        self.ctx.conn.execute("DELETE FROM requests WHERE request_id=?", (request["request_id"],))
        self.ctx.conn.execute("INSERT INTO planner_inputs VALUES(?,?,?,?,?,?,?)", ("new-input", self.task_id, "a", "b", "c", "{}", at))
        self.assertEqual(self.reject_revert().code, "conflict")

    def test_worktree_remote_binding_and_snapshot_races_are_zero_write(self):
        self.blocked()
        original_remote = remote.get_task
        def remote_change(client, task_id):
            result = original_remote(client, task_id)
            result["task"]["status"] = "to-do"
            return result
        with patch.object(remote, "get_task", side_effect=remote_change):
            self.assertEqual(self.reject_revert().code, "conflict")
        with patch.object(verdict, "binding", return_value={}):
            self.assertEqual(self.reject_revert().code, "conflict")
        (self.worktree / "tracked.txt").write_text("concurrent source change")
        self.assertEqual(self.reject_revert().code, "conflict")
        (self.worktree / "tracked.txt").write_text("partial implementation\n")
        def race(client, task_id):
            result = original_remote(client, task_id)
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, concurrent_write=True)
            return result
        with patch.object(remote, "get_task", side_effect=race):
            self.assertEqual(self.call_err("resolve", self.revert_payload).code, "conflict")
        self.assertTrue(db.task_extra(self.task())["concurrent_write"])
        self.assertFalse(db.task_extra(self.task())["engine_fix_rounds"][0].get("reverted_at"))

    def test_candidate_read_has_no_external_io_and_exposes_stable_contract(self):
        self.blocked()
        with patch.object(verdict, "binding", side_effect=AssertionError("read must not bind")), patch.object(remote, "get_task", side_effect=AssertionError("read must not fetch")):
            detail = derive.task_detail(self.ctx.conn, self.task(), self.ctx.now)
        self.assertIn("revert_engine_fix", detail["allowed_actions"])
        self.assertEqual(detail["engine_fix_revert"]["expected_failure_code"], "branch_exists")
        self.assertEqual(detail["engine_fix_summary"], {"used": 1, "max": 2})

    def test_reverted_empty_round_refunds_both_loop_and_authoritative_same_plan_limit(self):
        payload = self.quality_failure(empty=True)
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, engine_fix_rounds=[
            {"execution_run_id": "old-run", "plan_sha256": self.task()["plan_sha256"],
             "reason": "empty_diff", "reverted_at": self.ctx.now_iso}])
        reconciler = loop.Reconciler(self.db_path)
        snap = reconciler._snapshot(self.ctx.conn, self.task(), self.ctx.now)
        decision = reconciler._quality_retry(self.ctx.conn, self.task(), self.ctx.now, snap, "live")
        self.assertEqual(decision.action, "start_fix_round")
        result = self.call("resolve", payload)
        self.assertEqual(result["request"]["kind"], "dispatch")
        self.assertEqual(revert.summary(self.ctx.conn, self.task()), {"used": 1, "max": 2})
        self.assertEqual(len(revert.history(self.task())), 2)

    def test_fast_plan_gate_ignores_reverted_history_and_summary_respects_policy(self):
        self.blocked()
        self.call("resolve", self.revert_payload)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, status="needs_plan")
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso, plan_wanted=True)
        from kanpilot import auto_intake
        with patch.object(auto_intake, "_submit", side_effect=AssertionError("reverted row cannot authorize planning")):
            self.assertIsNone(loop.Reconciler(self.db_path)._schedule_engine_plan(self.ctx.conn, self.task_id, self.ctx.now))
        self.call("set-project-automation", {"project_id": self.task()["project_id"], "automation": {"enabled": True, "max_fix_rounds": 1}})
        self.assertEqual(revert.summary(self.ctx.conn, self.task()), {"used": 0, "max": 1})
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso, manual_takeover=True)
        self.assertIsNone(revert.summary(self.ctx.conn, self.task()))

    def test_reverted_rows_refund_quota_but_never_allow_same_run_again(self):
        self.blocked(environment=False)
        self.call("resolve", self.revert_payload)
        receipt = db.task_extra(self.task())["engine_verdict"]
        self.assertEqual(self.task()["failure_code"], "verification_unconfirmed")
        self.assertEqual(loop.Reconciler._engine_fix_rounds(self.task()), [])
        self.assertEqual(len(loop.Reconciler._engine_fix_rounds(self.task(), include_reverted=True)), 1)
        snapshot = loop.Reconciler(self.db_path)._snapshot(self.ctx.conn, self.task(), self.ctx.now)
        self.assertIsNone(loop.Reconciler(self.db_path)._quality_retry(self.ctx.conn, self.task(), self.ctx.now, snapshot, "live"))
        payload = {"task_id": self.task_id, "action": "replan", "actor": "automation",
                   "expected_execution_run_id": self.run_id,
                   "engine_fix": {"execution_run_id": self.run_id, "plan_sha256": receipt["plan_sha256"], "reason": "verify_failed"}}
        self.assertTrue(self.call("resolve", payload)["deduplicated"])


for _name in fixture.TestEngineRecovery.__dict__:
    if _name.startswith("test_") and _name not in TestEngineFixRevert.__dict__:
        setattr(TestEngineFixRevert, _name, None)
