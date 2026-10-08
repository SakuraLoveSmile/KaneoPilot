"""Review dismissal/recovery never repeats execution or starts a paid Review."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import KanpilotTestCase
from kanpilot import cmd_queue, core, db, derive, remote, util


class TestReviewStageRecovery(KanpilotTestCase):
    task_id = "taskreviewrecovery000001"
    execution_run = "execution-completed"
    review_run = "review-timed-out"

    def raw_task(self):
        conn = db.connect(self.db_path)
        try:
            return dict(db.require_task(conn, self.task_id))
        finally:
            conn.close()

    def make_failure(self, legacy_dismiss=False, automated=False):
        self.fixture = self.make_ready_task(task_id=self.task_id)
        self.fake.tasks[self.task_id]["status"] = "in-review"
        conn = db.connect(self.db_path)
        try:
            now = util.now_iso()
            task = db.require_task(conn, self.task_id)
            dispatch, _ = db.create_request(conn, now, "dispatch", "dispatch:" + self.task_id, task_id=self.task_id)
            db.update_request(conn, dispatch["request_id"], now, status="succeeded", run_id=self.execution_run,
                              finished_at=now, result_json={"send_state": "sent"})
            db.update_task(conn, self.task_id, now, status="in_review", stage="review", run_id=self.execution_run,
                           sent=1, send_state="sent", last_progress="本地执行完成且验证通过", kaneo_status="in-review")
            conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,created_at,updated_at) VALUES(?,?,'pi','finished',?,?)",
                         (self.task_id, self.execution_run, now, now))
            if automated:
                core.update_automation(conn, db.require_task(conn, self.task_id), now, adopted_at=now, manual_takeover=False)
                core.update_delivery(conn, db.require_task(conn, self.task_id), now, mode="pr", branch="retained-branch")
            req, _ = db.create_request(conn, now, "review", "review:" + self.task_id, task_id=self.task_id)
            self.failed_request = req["request_id"]
            db.update_request(conn, self.failed_request, now, status="failed", run_id=self.review_run,
                              finished_at=now, payload_json={"task_status_at_claim": "in_review"},
                              result_json={"outcome": "needs_user", "note": "timeout 1800s"})
            core.block_task(conn, db.require_task(conn, self.task_id), now, "timeout 1800s", "review_blocked", "kaneo-review", stage="review")
            db.add_event(conn, now, "reported", task_id=self.task_id, request_id=self.failed_request,
                         actor="kaneo-review", detail={"outcome": "needs_user"})
            if legacy_dismiss:
                resolution = {"action": "dismiss", "actor": "user", "at": now}
                core.transition_task(conn, db.require_task(conn, self.task_id), "ready", now, "user",
                                     detail={"via": "resolve dismiss"}, **core.clear_block_fields(), resolution=resolution)
                db.add_event(conn, now, "resolved", task_id=self.task_id, actor="user", detail=resolution)
        finally:
            conn.close()
        self.directory = self.runs_root / self.task_id / self.execution_run
        self.directory.mkdir(parents=True)
        self.job_path = self.directory / "job.json"
        self.events_path = self.directory / "events.jsonl"
        self.job = {"task_id": self.task_id, "run_id": self.execution_run, "backend": "pi",
                    "status": "finished", "plan_sha256": self.raw_task()["plan_sha256"]}
        self.job_path.write_text(json.dumps(self.job))
        self.events_path.write_text(json.dumps({"seq": 1, "type": "terminal", "outcome": "completed", "ts": util.now_iso()}) + "\n")
        self.review_path = self.app_support / "executor-sessions" / "review" / "invocation" / "job.json"
        self.review_path.parent.mkdir(parents=True)
        self.review_job = {"task_id": self.task_id, "request_id": self.failed_request, "run_id": self.review_run,
                           "execution_run_id": self.execution_run, "status": "failed", "conclusion": None, "report_path": None}
        self.review_path.write_text(json.dumps(self.review_job))

    def payload(self, **overrides):
        return {"task_id": self.task_id, "action": "recover_review", "expected_execution_run_id": self.execution_run,
                "expected_failed_review_request_id": self.failed_request, **overrides}

    def count_requests(self, kind=None):
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM requests WHERE task_id=? ORDER BY rowid", (self.task_id,)).fetchall()
            return [dict(r) for r in rows if kind is None or r["kind"] == kind]
        finally:
            conn.close()

    def assert_no_write_failure(self, payload=None, code="conflict"):
        before = self.raw_task()
        requests = self.count_requests()
        err = self.call_err("resolve", payload or self.payload())
        self.assertEqual(getattr(err, "code", None), code, repr(err))
        self.assertEqual(self.raw_task(), before)
        self.assertEqual(self.count_requests(), requests)

    def test_normal_and_automated_dismiss_keep_review_and_pause(self):
        for automated in (False, True):
            with self.subTest(automated=automated):
                if not hasattr(self, "fixture"):
                    self.make_failure(automated=automated)
                else:
                    conn = db.connect(self.db_path)
                    core.update_automation(conn, db.require_task(conn, self.task_id), util.now_iso(), adopted_at=util.now_iso(), manual_takeover=False)
                    core.update_delivery(conn, db.require_task(conn, self.task_id), util.now_iso(), mode="pr", branch="retained-branch")
                    core.block_task(conn, db.require_task(conn, self.task_id), util.now_iso(), "timeout", "review_blocked", "review", stage="review")
                    conn.close()
                before = self.raw_task()
                reqs = self.count_requests()
                self.assertEqual(self.call("resolve", {"task_id": self.task_id, "action": "dismiss"})["task_status"], "in_review")
                after = self.raw_task()
                self.assertEqual(after["stage"], "review")
                self.assertEqual(after["auto_advance_paused"], 1)
                for key in ("run_id", "sent", "send_state", "plan_path", "plan_sha256", "last_progress"):
                    self.assertEqual(after[key], before[key])
                view = self.call("get-task", {"task_id": self.task_id})["task"]
                self.assertEqual(view["display_status"], "待评审")
                self.assertEqual(view["board_substatus"], "待评审")
                self.assertNotIn("enqueue", view["allowed_actions"])
                self.assertIn("resume-auto", view["allowed_actions"])
                self.call("tick", {})
                self.assertEqual(self.count_requests(), reqs)
                if automated:
                    self.assertTrue(core.automation_of(after)["manual_takeover"])
                    self.assertEqual(core.delivery_of(after)["mode"], "manual")
                    self.assertEqual(core.delivery_of(after)["branch"], "retained-branch")

    def test_recovery_preserves_evidence_and_does_not_queue_or_write_remote(self):
        self.make_failure(legacy_dismiss=True)
        before = self.raw_task()
        requests = self.count_requests()
        view = self.call("get-task", {"task_id": self.task_id})["task"]
        self.assertIn("recover_review", view["allowed_actions"])
        self.assertNotIn("enqueue", view["allowed_actions"])
        self.assertEqual(view["review_recovery"], {"execution_run_id": self.execution_run, "failed_review_request_id": self.failed_request})
        result = self.call("resolve", self.payload())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["request"])
        after = self.raw_task()
        self.assertEqual((after["status"], after["stage"], after["auto_advance_paused"]), ("in_review", "review", 1))
        for key in ("run_id", "sent", "send_state", "plan_path", "plan_sha256", "last_progress"):
            self.assertEqual(after[key], before[key])
        self.call("tick", {})
        self.assertEqual(self.count_requests(), requests)
        self.assertFalse(self.call("claim", {"stage": "dispatch", "executor": "test-dispatch"})["claimed"])
        self.assertFalse(self.call("claim", {"stage": "review", "executor": "test-review"})["claimed"])
        self.assertTrue(all(method == "GET" for method, path, body in self.fake.requests_log))

    def test_repeat_identity_is_idempotent_even_after_progress_and_unpause(self):
        self.make_failure(legacy_dismiss=True)
        self.call("resolve", self.payload())
        self.set_task_status(self.task_id, "done", stage="done", auto_advance_paused=0)
        before = self.raw_task()
        result = self.call("resolve", self.payload())
        self.assertTrue(result["deduplicated"])
        self.assertEqual(self.raw_task(), before)
        self.set_task_status(self.task_id, "ready", run_id="new-execution", auto_advance_paused=0)
        self.assert_no_write_failure()

    def test_both_expected_ids_are_required_and_must_match(self):
        self.make_failure(legacy_dismiss=True)
        for key in ("expected_execution_run_id", "expected_failed_review_request_id"):
            payload = self.payload()
            payload.pop(key)
            self.assert_no_write_failure(payload, code="bad_request")
            self.assert_no_write_failure(self.payload(**{key: "wrong"}))

    def test_execution_and_review_file_identity_guards(self):
        self.make_failure(legacy_dismiss=True)
        for fields in ({"status": "running"}, {"plan_sha256": "wrong"}, {"run_id": "wrong"}, {"backend": "pi_desktop"}):
            self.job_path.write_text(json.dumps({**self.job, **fields}))
            self.assert_no_write_failure()
        self.job_path.write_text(json.dumps(self.job))
        self.events_path.write_text(json.dumps({"type": "terminal", "outcome": "failed"}) + "\n")
        self.assert_no_write_failure()
        self.events_path.write_text(json.dumps({"type": "terminal", "outcome": "completed"}) + "\n")
        for fields in ({"execution_run_id": "wrong"}, {"request_id": "wrong"}, {"run_id": "wrong"},
                       {"status": "running"}, {"conclusion": "pass"}, {"report_path": "report.md"}):
            self.review_path.write_text(json.dumps({**self.review_job, **fields}))
            self.assert_no_write_failure()
        self.review_path.write_text(json.dumps(self.review_job))
        self.fixture["plan_path"].write_text(self.fixture["plan_path"].read_text() + "\nchanged")
        self.assert_no_write_failure()

    def test_database_guards_and_listing_use_only_local_audit(self):
        self.make_failure(legacy_dismiss=True)
        original = self.raw_task()
        for fields in ({"run_id": "different"}, {"send_state": "unknown"}, {"sent": 0},
                       {"save_state": "trashed"}, {"review_key": "new-review"}, {"resolution": {"action": "retry"}}):
            conn = db.connect(self.db_path)
            db.update_task(conn, self.task_id, util.now_iso(), **fields)
            conn.close()
            self.assert_no_write_failure()
            conn = db.connect(self.db_path)
            db.update_task(conn, self.task_id, util.now_iso(), **{key: original[key] for key in fields})
            conn.close()
        with patch.object(remote, "get_task", side_effect=AssertionError("listing must not call remote")), patch.object(cmd_queue, "_accept_remote_for_resolution", side_effect=AssertionError("not used")):
            view = self.call("get-task", {"task_id": self.task_id})["task"]
            self.assertIn("recover_review", view["allowed_actions"])
        conn = db.connect(self.db_path)
        db.create_request(conn, util.now_iso(), "monitor", "monitor:" + self.task_id, task_id=self.task_id)
        conn.close()
        self.assert_no_write_failure()
        self.assertNotIn("recover_review", self.call("get-task", {"task_id": self.task_id})["task"]["allowed_actions"])

    def test_remote_guards_and_transaction_snapshot_races(self):
        self.make_failure(legacy_dismiss=True)
        live = remote.get_task(remote.get_client(), self.task_id)
        for value in ({**live, "requirements_sha256": "changed"},
                      {**live, "task": {**live["task"], "status": "in-progress"}},
                      {**live, "task": {**live["task"], "projectId": "different"}}):
            with patch.object(remote, "get_task", return_value=value):
                self.assert_no_write_failure()
        for target in ("task", "round", "request", "events", "file"):
            # Restore files/DB between independent race checks.
            before = self.raw_task()
            def raced(*args):
                conn = db.connect(self.db_path)
                self.assertFalse(conn.in_transaction)
                if target == "task":
                    db.update_task(conn, self.task_id, util.now_iso(), last_progress="raced")
                elif target == "round":
                    conn.execute("UPDATE execution_rounds SET run_status='running' WHERE task_id=?", (self.task_id,))
                elif target == "request":
                    db.update_request(conn, self.failed_request, util.now_iso(), result_json={"changed": True})
                elif target == "events":
                    db.add_event(conn, util.now_iso(), "rechecked", task_id=self.task_id, detail={"changed": True})
                else:
                    self.review_path.write_text(json.dumps({**self.review_job, "detail": "changed"}))
                conn.close()
                return live
            with patch.object(remote, "get_task", side_effect=raced):
                err = self.call_err("resolve", self.payload())
                self.assertEqual(getattr(err, "code", None), "conflict", repr(err))
            self.assertEqual(self.raw_task()["status"], "ready")
            conn = db.connect(self.db_path)
            db.update_task(conn, self.task_id, util.now_iso(), last_progress=before["last_progress"])
            conn.execute("UPDATE execution_rounds SET run_status='finished' WHERE task_id=?", (self.task_id,))
            conn.close()
            self.review_path.write_text(json.dumps(self.review_job))

    def test_missing_or_newer_failed_review_audit_is_not_recoverable(self):
        self.make_failure(legacy_dismiss=True)
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM events WHERE task_id=? AND kind='reported'", (self.task_id,))
        conn.close()
        self.assert_no_write_failure()

    def test_new_cycle_or_registered_conclusion_blocks_old_recovery(self):
        self.make_failure(legacy_dismiss=True)
        conn = db.connect(self.db_path)
        now = util.now_iso()
        db.add_event(conn, now, "review_registered", task_id=self.task_id, detail={"conclusion": "pass"})
        conn.close()
        self.assert_no_write_failure()
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM events WHERE task_id=? AND kind='review_registered'", (self.task_id,))
        request, _ = db.create_request(conn, now, "review", "review:" + self.task_id, task_id=self.task_id)
        db.update_request(conn, request["request_id"], now, status="failed", run_id="new-review", finished_at=now)
        conn.close()
        self.assert_no_write_failure()
        self.assertNotIn("recover_review", self.call("get-task", {"task_id": self.task_id})["task"]["allowed_actions"])

    def test_duplicate_matching_review_jobs_are_ambiguous(self):
        self.make_failure(legacy_dismiss=True)
        other = self.review_path.parent.parent / "another-invocation" / "job.json"
        other.parent.mkdir()
        other.write_text(json.dumps(self.review_job))
        self.assert_no_write_failure()

    def test_sent_dispatch_is_rejected_with_and_without_confirmation(self):
        self.make_failure(legacy_dismiss=True)
        for extra in ({}, {"confirm_plan": True, "expected_plan_sha256": self.raw_task()["plan_sha256"]}):
            self.assertEqual(self.call_err("enqueue", {"task_id": self.task_id, "kind": "dispatch", **extra}).code, "conflict")
        self.assertEqual(len(self.count_requests("dispatch")), 1)

    def test_retry_failed_review_with_sent_execution_creates_review_only(self):
        self.make_failure()
        self.assertIn("retry", self.call("get-task", {"task_id": self.task_id})["task"]["allowed_actions"])
        result = self.call("resolve", {"task_id": self.task_id, "action": "retry"})
        self.assertEqual(result["task_status"], "in_review")
        self.assertEqual(result["request"]["kind"], "review")
        self.assertEqual(len(self.count_requests("dispatch")), 1)
        self.assertEqual(self.raw_task()["run_id"], self.execution_run)


@pytest.mark.parametrize("action", ["start", "retry", "resume"])
def test_registered_blocked_result_kept_until_explicit_new_review(factory, tmp_env, action):
    """Use real claim/register/report commands, preserving the old report audit."""
    from kanpilot import cli
    if action == "resume":
        conn = db.connect(tmp_env.db)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "review_config", {"backend": "codex"})
        finally:
            conn.close()
    initial = factory.make_in_review()
    task_id = initial.task.task_id
    if action == "resume":
        conn = db.connect(tmp_env.db)
        try:
            with db.write_txn(conn):
                core.update_automation(conn, db.require_task(conn, task_id), util.now_iso(), adopted_at=util.now_iso(), manual_takeover=False)
        finally:
            conn.close()
    claim = cli.run("claim", {"stage": "review", "executor": "kaneo-review"}, tmp_env.db)
    assert claim["claimed"]
    before = cli.run("get-task", {"task_id": task_id}, tmp_env.db)["task"]
    report = Path(tmp_env.env["KANPILOT_REPORTS_ROOT"]) / "blocked-review.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Review\n缺少验收证据，评审受阻。\n")
    old_hash = util.sha256_file(report)
    cli.run("register-review", {"task_id": task_id, "request_id": claim["request_id"],
                               "run_id": claim["run_id"], "expected_execution_run_id": before["run_id"],
                               "report_path": str(report), "conclusion": "blocked", "review_key": "blocked-key"}, tmp_env.db)
    cli.run("report", {"request_id": claim["request_id"], "run_id": claim["run_id"],
                       "outcome": "needs_user", "note": "评审受阻"}, tmp_env.db)
    blocked = cli.run("get-task", {"task_id": task_id}, tmp_env.db)["task"]
    assert blocked["review_summary"]["conclusion"] == "blocked"
    assert blocked["review_report_sha256"] == old_hash
    if action == "start":
        cli.run("resolve", {"task_id": task_id, "action": "dismiss"}, tmp_env.db)
        held = cli.run("get-task", {"task_id": task_id}, tmp_env.db)["task"]
        assert held["status"] == "in_review" and held["auto_advance_paused"] == 1
        assert held["review_summary"]["conclusion"] == "blocked"
        assert held["review_report_sha256"] == old_hash
        assert not cli.run("tick", {}, tmp_env.db)["created"]
        cli.run("restore-task", {"task_id": task_id, "resume_auto": True}, tmp_env.db)
        created = cli.run("tick", {}, tmp_env.db)["created"]
        assert len(created) == 1 and created[0]["kind"] == "review"
    else:
        result = cli.run("resolve", {"task_id": task_id, "action": action}, tmp_env.db)
        assert result["request"]["kind"] == "review"
        assert not cli.run("tick", {}, tmp_env.db)["created"]
    fresh = cli.run("get-task", {"task_id": task_id}, tmp_env.db)["task"]
    assert fresh["status"] == "in_review" and fresh["run_id"] == before["run_id"]
    assert fresh["sent"] == 1 and fresh["send_state"] == "sent"
    assert fresh["review_summary"] is None
    assert all(fresh[key] is None for key in ("review_report_path", "review_report_sha256", "review_key"))
    assert util.sha256_file(report) == old_hash
    assert fresh["review_history"] == blocked["review_history"]
    conn = db.connect(tmp_env.db)
    try:
        events = conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='review_result_cleared'", (task_id,)).fetchall()
        assert len(events) == 1
        archived = util.jload(events[0]["detail_json"])
        assert archived["execution_run_id"] == before["run_id"]
        assert archived["previous"]["review_report_path"] == str(report)
        assert archived["previous"]["review_report_sha256"] == old_hash
        assert archived["previous"]["review_key"] == "blocked-key"
        assert archived["previous"]["review_conclusion"] == "blocked"
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE task_id=? AND kind='dispatch'", (task_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE task_id=? AND kind='review' AND status IN ('queued','claimed')", (task_id,)).fetchone()[0] == 1
    finally:
        conn.close()
    new_claim = cli.run("claim", {"stage": "review", "executor": "kaneo-review"}, tmp_env.db)
    assert new_claim["claimed"]
