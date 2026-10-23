"""Direct repair retry lifecycle; disposable Git/DB and fake Plus/Kaneo only."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import test_direct_review_repair as direct
from helpers import make_pi_fixture
from test_session import pi_event, pi_job, write_json, write_jsonl
from kanpilot import (cmd_dispatch, core, db, engine_claim_scope, git_delivery,
                      harness_plus_adapter, remote, review_repair, util)


class ReviewRepairRetryTest(direct.DirectReviewRepairTest):
    def failed_repair(self, managed=False):
        if managed:
            self.managed()
        self.register(); self.finish_review()
        if not managed:
            self.call("start-review-repair", self.payload())
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            dispatch = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert dispatch["claimed"], dispatch
        self.failed_run = dispatch["run_id"]
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor",
                       sent=1, send_state="sent", session_id="failed-session")
        self.call("report", dict(request_id=dispatch["request_id"], run_id=self.failed_run, outcome="succeeded"))
        # An executor failed after producing useful, uncommitted changes.
        (self.worktree / "partial.txt").write_text("keep partial repair\n")
        config = {"backend": "pi_desktop", "connection_file": str(self.root / "fake-plus.json")}
        self.conn.execute("UPDATE execution_rounds SET backend='pi_desktop', run_status='failed', session_id='failed-session', config_json=?,worktree_path=? WHERE task_id=? AND run_id=?",
                          (util.jdump(config), str(self.worktree), self.task_id, self.failed_run))
        directory = self.runs_root / self.task_id / self.failed_run
        directory.mkdir(parents=True, exist_ok=True)
        self.job = pi_job(self.task_id, self.failed_run, backend="pi_desktop", status="failed", exit_code=1,
                          pid=None, observe_pid=None, config=config, session_id="failed-session",
                          turn_id="failed-turn", send_state="sent", worktree_path=str(self.worktree))
        write_json(directory / "job.json", self.job)
        write_jsonl(directory / "events.jsonl", [pi_event(1, "session", session_id="failed-session", turn_id="failed-turn"),
                                                   pi_event(2, "terminal", outcome="failed", reason="NETWORK_ERROR")])
        self.plus_status = {"is_running": False, "pending_permission": None, "current_turn_id": "failed-turn"}
        self.plus_turn = {"sessionId": "failed-session", "turnId": "failed-turn", "status": "error"}
        for name, value in (("plus_status", lambda *a, **k: dict(self.plus_status)),
                            ("_get_client", lambda *a, **k: (SimpleNamespace(turn_get=lambda *a: dict(self.plus_turn)), {}))):
            patcher = patch.object(harness_plus_adapter, name, side_effect=value)
            patcher.start(); self.addCleanup(patcher.stop)
        db.update_task(self.conn, self.task_id, self.now, status="needs_user", stage="monitor",
                       failure_code="execution_failed", failure_stage="running")
        self.conn.execute("UPDATE requests SET status='failed' WHERE task_id=? AND kind='monitor' AND status='queued'", (self.task_id,))
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        self.retry_payload = dict(task_id=self.task_id, action="resume", expected_execution_run_id=self.failed_run)
        self.fix = core.review_repair_of(self.task())

    def claim_retry(self, result):
        request = db.get_request(self.conn, result["request"]["request_id"])
        assert db.request_payload(request)["review_repair_key"] == self.fix["review_key"]
        if self.fix["authorized_by"] == "user":
            assert engine_claim_scope.manual_review_repair(self.conn, request, self.task())
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch", request_id=request["request_id"]))
        assert claimed["claimed"], claimed
        assert claimed["run_id"] != self.failed_run
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"], self.conn)
        assert (path, mode) == (self.worktree, "branch")
        assert (path / "partial.txt").read_text() == "keep partial repair\n"
        assert core.review_repair_of(self.task())["source_execution_run_id"] == "exec1"
        assert core.review_repair_of(self.task())["repair_execution_run_id"] == claimed["run_id"]
        assert core.review_repair_of(self.task())["fix_round"] == 1
        assert len([r for r in self.requests() if r["kind"] == "plan"]) == self.original_plan_requests
        return claimed

    def legacy_block(self):
        self.failed_repair()
        # Persist the exact pre-fix shape, after an authorized failed-execution retry.
        delivery = dict(core.delivery_of(self.task()), repository=self.task()["repository"])
        retry = dict(source_run_id=self.failed_run, requested_at=self.now,
                     plan_sha256=self.task()["plan_sha256"], plan_generated_at=self.task()["plan_generated_at"],
                     worktree=delivery, detached=False, worktree_sha256=git_delivery.worktree_state_sha256(delivery))
        db.set_extra(self.conn, self.task(), self.now, execution_retry=retry)
        db.update_task(self.conn, self.task_id, self.now, status="needs_user", stage="dispatch",
                       failure_code="conflict", failure_stage="ready", failure_reason="修复执行轮次已变化",
                       run_id=None, sent=0, send_state=None, session_id=None, session_marker=None, lock_retained=0)
        failed, _ = db.create_request(self.conn, self.now, "dispatch", "dispatch:" + self.task_id, task_id=self.task_id)
        db.update_request(self.conn, failed["request_id"], self.now, status="failed", result_json={"outcome":"failed"})

    def reject_retry(self, payload):
        before = dict(self.task()); requests = [dict(r) for r in self.requests()]
        with self.assertRaises(util.KanpilotError):
            self.call("resolve", payload)
        assert dict(self.task()) == before
        assert [dict(r) for r in self.requests()] == requests

    def test_failed_repair_retry_claims_new_run_and_preserves_results(self):
        self.failed_repair()
        result = self.call("resolve", self.retry_payload)
        assert core.review_repair_of(self.task())["state"] == "queued"
        review_repair.verify_frozen(self.task())
        self.claim_retry(result)

    def test_managed_failed_repair_retry_does_not_charge_another_fix_round(self):
        self.failed_repair(managed=True)
        result = self.call("resolve", self.retry_payload)
        assert core.automation_of(self.task())["fix_round"] == 1
        self.claim_retry(result)

    def test_existing_broken_retry_shape_recovers_through_same_retry_button(self):
        self.legacy_block()
        result = self.call("resolve", dict(task_id=self.task_id, action="retry"))
        assert core.review_repair_of(self.task())["state"] == "queued"
        self.claim_retry(result)

    def test_legacy_recovery_rejects_running_or_waiting_plus(self):
        self.legacy_block()
        for update in ({"is_running": True}, {"pending_permission": True}):
            old = dict(self.plus_status); self.plus_status.update(update)
            self.reject_retry(dict(task_id=self.task_id, action="retry"))
            self.plus_status = old

    def test_legacy_recovery_rejects_tampered_source_or_plan_identity(self):
        self.legacy_block()
        original = db.task_extra(self.task())["execution_retry"]
        for fields in ({"source_run_id": "different-run"}, {"plan_sha256": "0"*64}, {"plan_generated_at": "changed"}):
            db.set_extra(self.conn, self.task(), self.now, execution_retry=dict(original, **fields))
            self.reject_retry(dict(task_id=self.task_id, action="retry"))
        db.set_extra(self.conn, self.task(), self.now, execution_retry=original)

    def test_legacy_recovery_rejects_changed_report_and_worktree(self):
        self.legacy_block()
        self.report.write_text("tampered")
        self.reject_retry(dict(task_id=self.task_id, action="retry"))
        self.report.write_text("# Review\nFix only the bounded download.\n")
        (self.worktree / "partial.txt").write_text("changed after retry confirmation")
        self.reject_retry(dict(task_id=self.task_id, action="retry"))

    def test_legacy_recovery_rejects_changed_requirements_active_request_and_unknown_send(self):
        self.legacy_block()
        self.fake.tasks[self.task_id]["description"] = "different requirement"
        self.reject_retry(dict(task_id=self.task_id, action="retry"))

    def test_legacy_recovery_rejects_unknown_send_and_active_request(self):
        self.legacy_block()
        db.update_task(self.conn, self.task_id, self.now, send_state="unknown")
        self.reject_retry(dict(task_id=self.task_id, action="retry"))
        db.update_task(self.conn, self.task_id, self.now, send_state=None)
        db.create_request(self.conn, self.now, "monitor", "active-monitor", task_id=self.task_id)
        self.reject_retry(dict(task_id=self.task_id, action="retry"))

    def test_retry_claim_rejects_worktree_changes_after_confirmation(self):
        self.failed_repair()
        result = self.call("resolve", self.retry_payload)
        (self.worktree / "partial.txt").write_text("changed after retry confirmation")
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch", request_id=result["request"]["request_id"]))
        assert not claimed["claimed"], claimed
        assert self.task()["run_id"] is None

    def test_retry_send_guard_rejects_worktree_changes_after_claim(self):
        self.failed_repair()
        result = self.call("resolve", self.retry_payload)
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch", request_id=result["request"]["request_id"]))
        assert claimed["claimed"], claimed
        (self.worktree / "partial.txt").write_text("changed after claim")
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"], self.conn)

    def test_retry_claim_and_send_reject_source_that_resumed(self):
        self.failed_repair()
        result = self.call("resolve", self.retry_payload)
        self.plus_status["is_running"] = True
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch", request_id=result["request"]["request_id"]))
        assert not claimed["claimed"], claimed
        self.plus_status["is_running"] = False
        result = self.call("resolve", dict(task_id=self.task_id, action="retry"))
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch", request_id=result["request"]["request_id"]))
        assert claimed["claimed"], claimed
        self.plus_status["is_running"] = True
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"], self.conn)

    def test_legacy_recovery_rechecks_report_after_external_reads(self):
        self.legacy_block()
        original = remote.get_task
        def change_report(*args, **kwargs):
            result = original(*args, **kwargs)
            self.report.write_text("report changed while remote was read")
            return result
        with patch.object(remote, "get_task", side_effect=change_report):
            self.reject_retry(dict(task_id=self.task_id, action="retry"))

    def test_legacy_recovery_accepts_explicit_dispatch_failure_stage_too(self):
        self.legacy_block()
        db.update_task(self.conn, self.task_id, self.now, failure_stage="dispatch")
        result = self.call("resolve", dict(task_id=self.task_id, action="retry"))
        self.claim_retry(result)

    def test_queued_retry_frozen_identity_supports_task_dto(self):
        self.failed_repair()
        self.call("resolve", self.retry_payload)
        dto = dict(self.task()); dto["extra"] = db.task_extra(self.task()); dto.pop("extra_json")
        assert review_repair.verify_frozen(dto)["state"] == "queued"

    def unstarted_dispatch_block(self, send_state="not_sent"):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        with patch.dict(os.environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch"))
        assert claimed["claimed"], claimed
        self.unstarted_run = claimed["run_id"]
        self.call("dispatch-fail", dict(task_id=self.task_id,
                  run_id=self.unstarted_run, send_state=send_state, reason="transport preflight unavailable"))

    def test_unsent_claim_retry_requires_a_safe_start_boundary(self):
        self.unstarted_dispatch_block()
        self.reject_retry(dict(task_id=self.task_id, action="retry"))
        assert self.task()["run_id"] == self.unstarted_run
        assert core.review_repair_of(self.task())["state"] == "running"

    def test_unsent_claim_with_start_intent_cannot_be_requeued(self):
        self.unstarted_dispatch_block()
        self.conn.execute("UPDATE execution_rounds SET started_at=? WHERE task_id=? AND run_id=?",
                          (self.now, self.task_id, self.unstarted_run))
        self.reject_retry(dict(task_id=self.task_id, action="retry"))

    def test_unknown_claim_send_state_cannot_be_requeued(self):
        self.unstarted_dispatch_block(send_state="unknown")
        self.reject_retry(dict(task_id=self.task_id, action="retry"))
