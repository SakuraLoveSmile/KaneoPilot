"""Direct repairs use disposable Git/SQLite and fake Kaneo; no models or live writes."""
from pathlib import Path
from unittest.mock import patch

from helpers import KanpilotTestCase, make_pi_fixture
from test_task_workspace import git
from kanpilot import cli, core, db, review_repair, util
from kanpilot.engine import decide, loop, steps


class DirectReviewRepairTest(KanpilotTestCase):
    task_id = "directrepair0001"

    def setUp(self):
        super().setUp()
        self.fixture = self.make_ready_task(task_id=self.task_id)
        self.fake.tasks[self.task_id]["status"] = "in-review"
        self.repo = self.repos_root / "Feedback"
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.branch = "kaneo/" + self.task_id
        self.worktree = self.app_support / "repair-worktree"
        git(self.repo, "worktree", "add", "-b", self.branch, str(self.worktree), "HEAD")
        self.source_directory = self.runs_root / self.task_id / "exec1"
        self.source_directory.mkdir(parents=True)
        (self.source_directory / "job.json").write_text(util.jdump({"v": 1, "backend": "pi", "task_id": self.task_id, "run_id": "exec1", "status": "finished", "ended_at": util.now_iso(), "exit_code": 0, "pid": None}))
        (self.source_directory / "events.jsonl").write_text(util.jdump({"seq": 1, "type": "terminal", "outcome": "completed", "ts": util.now_iso()}) + "\n")
        self.report = self.reports_root / "review.md"
        self.report.write_text("# Review\nFix only the bounded download.\n")
        self.conn = db.connect(self.db_path)
        self.now = util.now_iso()
        db.update_task(self.conn, self.task_id, self.now, status="in_review", stage="review", run_id="exec1",
            sent=1, send_state="sent", kaneo_status="in-review")
        self.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,worktree_path,config_json,created_at,updated_at) VALUES(?,?,'pi','finished',?,'{}',?,?)", (self.task_id, "exec1", str(self.worktree), self.now, self.now))
        task = self.task()
        core.update_delivery(self.conn, task, self.now, mode="manual", workspace_version=1,
            branch=self.branch, base_branch="main", base_sha=self.head, head_sha=self.head,
            worktree_path=str(self.worktree), commits=[{"execution_run_id": "exec1", "sha": self.head}])
        self.review, _ = db.create_request(self.conn, self.now, "review", "review:" + self.task_id,
            task_id=self.task_id, payload={"review_branch_snapshot": core.branch_review_snapshot(self.task())})
        db.update_request(self.conn, self.review["request_id"], self.now, status="claimed", run_id="review1",
            claimed_by="kaneo-review", lease_expires_at=util.plus_seconds(600))
        self.original_plan_requests = len([r for r in self.requests() if r["kind"] == "plan"])
        self.review_payload = dict(task_id=self.task_id, request_id=self.review["request_id"], run_id="review1",
            expected_execution_run_id="exec1", report_path=str(self.report), conclusion="changes_requested",
            review_key="review-key-1", fix_mode="same_task", fix={"title": "有界读取", "description": "修复下载上限", "priority": "high"})

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def task(self):
        return db.require_task(self.conn, self.task_id)

    def register(self):
        return self.call("register-review", self.review_payload)

    def payload(self):
        value = review_repair.candidate(self.task())
        return dict(task_id=self.task_id, **{k: value[v] for k, v in review_repair.IDENTITY.items()})

    def finish_review(self):
        return self.call("report", dict(request_id=self.review["request_id"],
            run_id="review1", outcome="succeeded"))

    def managed(self):
        core.update_automation(self.conn, self.task(), self.now, fix_round=0, segment_started_at=self.now)
        db.set_setting(self.conn, "project_mappings", {"revision": 1, "items": {self.task()["project_id"]:
            {"project_name": "Feedback", "repository": str(self.repo), "enabled": True,
             "automation": {"enabled": True, "max_fix_rounds": 2}}}})

    def test_registration_waits_for_report_then_managed_queues_dispatch(self):
        self.managed()
        self.register()
        assert self.task()["run_id"] == "exec1"
        assert core.review_repair_of(self.task())["state"] == "held"
        assert not any(r["kind"] == "dispatch" for r in self.requests())
        self.finish_review()
        assert self.task()["status"] == "ready"
        assert self.task()["run_id"] == "exec1"
        assert core.review_repair_of(self.task())["state"] == "queued"
        assert core.automation_of(self.task())["fix_round"] == 1
        assert sorted(r["kind"] for r in self.requests() if r["kind"] != "plan") == ["dispatch", "review"]
        assert self.task()["plan_sha256"] == util.sha256_file(self.fixture["plan_path"])

    def test_manual_is_held_until_explicit_start_and_not_adopted(self):
        self.register()
        self.finish_review()
        assert core.review_repair_of(self.task())["state"] == "held"
        db.set_extra(self.conn, self.task(), self.now, fix_request=dict(core.review_repair_of(self.task()), reason="旧阻塞原因"))
        result = self.call("start-review-repair", self.payload())
        assert result["review_repair"]["state"] == "queued"
        assert review_repair.view(self.conn, self.task(), util.now_dt())["reason"] == "修复会话待启动"
        assert not core.is_automation_managed(self.task())
        assert "automation" not in db.task_extra(self.task())
        assert db.task_extra(self.task())["review_repair_round"] == 1
        assert self.task()["review_report_path"] is None
        assert len(db.task_extra(self.task())["review_history"]) == 1

    def test_duplicate_start_is_read_only_and_identity_change_rejected(self):
        self.register(); self.finish_review()
        payload = self.payload()
        self.call("start-review-repair", payload)
        before = [dict(r) for r in self.requests()]
        assert self.call("start-review-repair", payload)["already_started"]
        assert before == [dict(r) for r in self.requests()]
        assert self.call_err("start-review-repair", dict(payload, expected_review_key="stale")).code == "conflict"

    def test_active_review_cannot_start_or_replace_execution(self):
        self.register()
        assert not review_repair.view(self.conn, self.task(), util.now_dt())["can_start"]
        assert self.call_err("start-review-repair", self.payload()).code == "conflict"
        assert self.task()["run_id"] == "exec1"

    def test_report_and_plan_bytes_are_frozen(self):
        self.register(); self.finish_review()
        payload = self.payload()
        self.report.write_text("changed report")
        assert self.call_err("start-review-repair", payload).code == "conflict"
        assert core.review_repair_of(self.task())["state"] == "held"

    def test_dirty_worktree_cannot_start(self):
        self.register(); self.finish_review()
        (self.worktree / "unexpected").write_text("user changes")
        assert self.call_err("start-review-repair", self.payload()).code == "conflict"
        assert core.review_repair_of(self.task())["state"] == "held"

    def test_remote_requirements_change_cannot_start(self):
        self.register(); self.finish_review()
        self.fake.tasks[self.task_id]["description"] = "new requirement"
        assert self.call_err("start-review-repair", self.payload()).code == "remote_conflict"

    def test_pause_and_round_limit_preserve_held(self):
        self.register(); self.finish_review()
        db.set_setting(self.conn, "paused_global", True)
        assert self.call_err("start-review-repair", self.payload()).code == "conflict"
        db.set_setting(self.conn, "paused_global", False)
        db.set_extra(self.conn, self.task(), self.now, review_repair_round=2)
        assert self.call_err("start-review-repair", self.payload()).code == "conflict"

    def test_historical_candidate_is_read_only_until_start(self):
        self.register(); self.finish_review()
        db.set_extra(self.conn, self.task(), self.now, fix_request=None)
        db.update_task(self.conn, self.task_id, self.now, fix_task_id="historical-child")
        before = dict(self.task())
        value = review_repair.view(self.conn, self.task(), util.now_dt())
        assert value["state"] == "held" and value["can_start"]
        assert dict(self.task()) == before
        self.call("start-review-repair", self.payload())
        assert self.task()["fix_task_id"] == "historical-child"
        assert core.review_repair_of(self.task())["mode"] == "direct"

    def test_claim_new_run_binds_repair_and_does_not_plan(self):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        with patch.dict(__import__('os').environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert claimed["claimed"]
        fix = core.review_repair_of(self.task())
        assert fix["state"] == "running"
        assert fix["repair_execution_run_id"] == self.task()["run_id"] != "exec1"
        assert len([r for r in self.requests() if r["kind"] == "plan"]) == self.original_plan_requests

    def test_awaiting_review_ignores_old_pr_and_round_counter(self):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        fix = core.review_repair_of(self.task())
        db.set_extra(self.conn, self.task(), self.now, fix_request=dict(fix, state="awaiting_review", repair_execution_run_id="repair2"))
        db.update_task(self.conn, self.task_id, self.now, status="in_review", run_id="repair2")
        core.update_delivery(self.conn, self.task(), self.now, mode="pr", pr_number=4, pushed_sha=self.head)
        reconciler = loop.Reconciler.__new__(loop.Reconciler)
        snapshot = reconciler._snapshot(self.conn, self.task(), util.now_dt())
        assert (snapshot["phase"], snapshot["condition"]) == ("review", "reviewing")
        assert decide.phase_condition("in_review", fix_round=2, fix_open=False) == ("review", "reviewing")

    def test_verify_frozen_accepts_dto_and_allows_running_dirty_head(self):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        fix = core.review_repair_of(self.task())
        db.set_extra(self.conn, self.task(), self.now, fix_request=dict(fix, state="running", repair_execution_run_id="repair2"))
        db.update_task(self.conn, self.task_id, self.now, run_id="repair2")
        (self.worktree / "fix.py").write_text("bounded repair")
        dto = dict(self.task()); dto["extra"] = db.task_extra(self.task()); dto.pop("extra_json")
        assert review_repair.verify_frozen(dto)["state"] == "running"

    def test_manual_engine_scope_claims_only_authorized_repair(self):
        import os
        from kanpilot import engine_claim_scope, process_owner
        from kanpilot.engine import health
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        reconciler = loop.Reconciler(self.db_path, mode="legacy")
        assert reconciler._queued_stages(self.conn, {}) == {"dispatch": 1}
        health.acquire_or_renew_lease(self.conn, util.now_iso(), instance_id="direct-engine",
            stage=None, pid=os.getpid(), lease_seconds=1200)
        environment = {**make_pi_fixture(self.root), engine_claim_scope.WORKER_ENV: "1",
            engine_claim_scope.MODE_ENV: "legacy", engine_claim_scope.FORCE_ENV: "0", process_owner.INSTANCE_ENV: "direct-engine"}
        with patch.dict(os.environ, environment):
            result = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert result["claimed"]
        assert core.review_repair_of(self.task())["state"] == "running"
        assert not core.is_automation_managed(self.task())
        db.update_task(self.conn, self.task_id, util.now_iso(), status="running", stage="monitor")
        self.call("report", {"request_id": result["request_id"], "run_id": result["run_id"], "outcome": "succeeded"})
        assert reconciler._queued_stages(self.conn, {}) == {"monitor": 1}
        with patch.dict(os.environ, environment):
            monitor = self.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        assert monitor["claimed"]
        self.call("report", {"request_id": monitor["request_id"], "run_id": monitor["run_id"], "outcome": "succeeded"})
        assert reconciler._queued_stages(self.conn, {}) == {"monitor": 1}

    def test_legacy_held_materials_without_current_pointers_are_explicit_candidate(self):
        self.managed(); self.register()
        fix = core.review_repair_of(self.task())
        fix.pop("mode")
        db.set_extra(self.conn, self.task(), self.now, fix_request=fix, review_conclusion=None)
        db.update_task(self.conn, self.task_id, self.now, review_report_path=None, review_report_sha256=None, review_key=None)
        self.call("report", {"request_id": self.review["request_id"], "run_id": "review1", "outcome": "succeeded"})
        before = dict(self.task())
        value = review_repair.view(self.conn, self.task(), util.now_dt())
        assert value["can_start"] and value["state"] == "held"
        assert dict(self.task()) == before
        self.call("start-review-repair", self.payload())
        assert core.review_repair_of(self.task())["state"] == "queued"

    def test_queued_repair_rejects_source_execution_that_resumed(self):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        job = __import__('json').loads((self.source_directory / "job.json").read_text())
        job.update(status="running", ended_at=None)
        (self.source_directory / "job.json").write_text(util.jdump(job))
        with patch.dict(__import__('os').environ, make_pi_fixture(self.root)):
            result = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert not result["claimed"]
        assert self.task()["run_id"] == "exec1"

    def test_repair_commit_opens_new_review_then_passes_same_task(self):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        with patch.dict(__import__('os').environ, make_pi_fixture(self.root)):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        execution = claimed["run_id"]
        # Fake executor transport completes; subsequent commit/advance/Review are real core commands.
        db.update_task(self.conn, self.task_id, util.now_iso(), status="running", stage="monitor", sent=1, send_state="sent")
        self.call("report", {"request_id": claimed["request_id"], "run_id": execution, "outcome": "succeeded"})
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        (self.worktree / "bounded.py").write_text("MAX_BYTES = 1024\n")
        monitor = self.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        assert monitor["claimed"]
        self.call("delivery-commit", {"task_id": self.task_id, "request_id": monitor["request_id"],
            "run_id": monitor["run_id"], "expected_execution_run_id": execution})
        assert core.delivery_of(self.task())["head_sha"] != self.head
        self.call("advance-task", {"task_id": self.task_id, "run_id": monitor["run_id"],
            "target": "in-review", "expected_current": "in-progress"})
        assert core.review_repair_of(self.task())["state"] == "awaiting_review"
        assert self.task()["review_report_path"] is None
        self.call("report", {"request_id": monitor["request_id"], "run_id": monitor["run_id"], "outcome": "succeeded"})
        # Formal Review claim freezes the new execution/head; seed claimed transport only.
        review = next(r for r in self.requests() if r["kind"] == "review" and r["status"] == "queued")
        db.merge_request_payload(self.conn, review["request_id"], util.now_iso(), review_branch_snapshot=core.branch_review_snapshot(self.task()))
        db.update_request(self.conn, review["request_id"], util.now_iso(), status="claimed", run_id="review2", claimed_by="kaneo-review", lease_expires_at=util.plus_seconds(600))
        new_report = self.reports_root / "review2.md"; new_report.write_text("# Review\n修复通过\n")
        self.call("register-review", {"task_id": self.task_id, "request_id": review["request_id"], "run_id": "review2",
            "expected_execution_run_id": execution, "report_path": str(new_report), "review_key": "review-key-2", "conclusion": "pass"})
        assert not core.review_repair_of(self.task())
        assert db.task_extra(self.task())["review_conclusion"] == "pass"
        assert len(db.task_extra(self.task())["review_history"]) == 2
        assert len([r for r in self.requests() if r["kind"] == "plan"]) == self.original_plan_requests

    def test_plus_source_manually_running_is_rejected_despite_finished_requests(self):
        import json
        from kanpilot import harness_plus_adapter
        self.register(); self.finish_review()
        self.conn.execute("UPDATE execution_rounds SET backend='pi_desktop', session_id='old-session', config_json=? WHERE task_id=? AND run_id='exec1'",
            (util.jdump({"backend": "pi_desktop", "connection_file": str(self.root / "fake-plus.json")}), self.task_id))
        job_path = self.source_directory / "job.json"
        job = json.loads(job_path.read_text())
        job.update(backend="pi_desktop", session_id="old-session", turn_id="old-turn", send_state="sent")
        job_path.write_text(util.jdump(job))
        with patch.object(harness_plus_adapter, "plus_status", return_value={"is_running": True, "current_turn_id": "manually-continued-turn", "pending_permission": None}) as probe:
            error = self.call_err("start-review-repair", self.payload())
        assert error.code == "conflict" and "Plus" in error.message
        probe.assert_called_once()
        assert core.review_repair_of(self.task())["state"] == "held"
        assert self.task()["run_id"] == "exec1"

    def test_remote_review_or_done_cannot_skip_running_repair_verification(self):
        self.register(); self.finish_review()
        self.call("start-review-repair", self.payload())
        value = core.review_repair_of(self.task())
        db.set_extra(self.conn, self.task(), self.now, fix_request=dict(value, state="running", repair_execution_run_id="repair2"))
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor", run_id="repair2")
        for remote_status in ("in-review", "done"):
            self.fake.tasks[self.task_id]["status"] = remote_status
            self.sync_task(self.fake.tasks[self.task_id], project_name="Feedback",
                requirements_sha256=self.fixture["requirements_sha256"], source_sha256=self.fixture["source_sha256"], source_path=self.fixture["source_path"])
            assert self.task()["status"] == "running"
            assert core.review_repair_of(self.task())["state"] == "running"

    def test_active_explicit_legacy_child_blocks_duplicate_repair_until_closed(self):
        self.register(); self.finish_review()
        child_id = "activelegacyfix0001"
        self.make_ready_task(task_id=child_id)
        db.update_task(self.conn, self.task_id, util.now_iso(), fix_task_id=child_id)
        assert not review_repair.view(self.conn, self.task(), util.now_dt())["can_start"]
        error = self.call_err("start-review-repair", self.payload())
        assert "历史修复子任务仍在处理" in error.message
        assert core.review_repair_of(self.task())["state"] == "held"
        db.update_task(self.conn, child_id, util.now_iso(), save_state="archived")
        assert review_repair.view(self.conn, self.task(), util.now_dt())["can_start"]
        assert self.call("start-review-repair", self.payload())["review_repair"]["state"] == "queued"

    def test_manual_takeover_does_not_reset_spent_managed_rounds(self):
        self.register(); self.finish_review()
        core.update_automation(self.conn, self.task(), util.now_iso(), manual_takeover=True, fix_round=2)
        assert not core.is_automation_managed(self.task())
        assert "次数" in self.call_err("start-review-repair", self.payload()).message
