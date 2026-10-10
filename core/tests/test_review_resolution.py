"""Blocked Review remedies: disposable real Git, FakeKaneo, isolated SQLite."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import KanpilotTestCase
from test_task_workspace import git
from kanpilot import (cli, cmd_dispatch, core, db, desktop_queue, dispatch_validate, git_delivery,
                      planner_inputs, planner_runner, review_resolution, review_snapshot,
                      pi_desktop_review, pi_desktop_worker, pi_worker, exec_config,
                      harness_plus_adapter, remote, util)


class ReviewResolutionTest(KanpilotTestCase):
    task_id = "reviewresolution0001"
    execution = "completed-execution"
    review_run = "blocked-review"

    def setUp(self):
        super().setUp()
        self.fixture = self.make_ready_task(task_id=self.task_id)
        self.repo = self.repos_root / "Feedback"
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Remedy Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.py").write_text("baseline\n")
        (self.repo / "delete.py").write_text("delete me\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "baseline")
        self.worktree = self.app_support / "worktrees" / self.task_id / self.execution
        self.worktree.parent.mkdir(parents=True)
        git(self.repo, "worktree", "add", "--detach", str(self.worktree), "HEAD")
        (self.worktree / "tracked.py").write_bytes(b"feature\r\n")
        (self.worktree / "delete.py").unlink()
        (self.worktree / "new.py").write_text("untracked complete feature\n")
        (self.worktree / "new.py").chmod(0o755)
        (self.repo / "tracked.py").write_text("user dirty primary\n")
        (self.repo / "notes.txt").write_text("user notes\n")
        self.original = review_snapshot.freeze(self.worktree)
        self.primary = review_snapshot.freeze(self.repo)
        self.report = self.reports_root / "blocked.md"
        self.report.write_text("# Review blocked\n\n缺少真实原生验收；需要补充启动后的体验证据，或修复验收流程。\n")
        self.fake.tasks[self.task_id]["status"] = "in-review"
        conn = db.connect(self.db_path)
        now = util.now_iso()
        task = db.require_task(conn, self.task_id)
        self.plan_sha = task["plan_sha256"]
        config = exec_config.snapshot(conn)
        db.update_task(conn, self.task_id, now, status="needs_user", stage="review", run_id=self.execution,
            sent=1, send_state="sent", failure_code="review_blocked", failure_stage="review",
            failure_reason="codex_desktop Review blocked；报告 /technical/path",
            review_report_path=str(self.report), review_report_sha256=util.sha256_file(self.report),
            kaneo_status="in-review")
        core.update_delivery(conn, db.require_task(conn, self.task_id), now, mode="manual")
        core.update_automation(conn, db.require_task(conn, self.task_id), now, manual_takeover=True)
        db.set_extra(conn, db.require_task(conn, self.task_id), now,
                     review_conclusion="blocked", review_registered_at=now)
        conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,worktree_path,config_json,created_at,updated_at) "
                     "VALUES(?,?,'pi','finished',?,?,?,?)", (self.task_id, self.execution, str(self.worktree), util.jdump(config), now, now))
        request, _ = db.create_request(conn, now, "review", "review:" + self.task_id, task_id=self.task_id)
        self.review_request = request["request_id"]
        db.update_request(conn, self.review_request, now, status="failed", run_id=self.review_run,
                          finished_at=now, result_json={"outcome": "needs_user"})
        conn.close()
        self.job_path = self.runs_root / self.task_id / self.execution / "job.json"
        self.job_path.parent.mkdir(parents=True)
        self.spec = {"v": 1, "task_id": self.task_id, "run_id": self.execution,
                     "config": config, "cwd": str(self.worktree), "repository": str(self.repo),
                     "plan_path": str(self.fixture["plan_path"]), "runs_dir": str(self.job_path.parent)}
        self.spec_path = self.job_path.parent / "spec.json"
        self.spec_path.write_text(json.dumps(self.spec))
        self.job = pi_worker._base_job(self.spec)
        self.job.update(status="finished", ended_at=now, exit_code=0, pid=None)
        self.job_path.write_text(json.dumps(self.job))
        self.events = self.job_path.parent / "events.jsonl"
        self.events.write_text(json.dumps({"seq": 1, "type": "terminal", "outcome": "completed", "ts": now}) + "\n")
        self.review_job = self.app_support / "executor-sessions" / "review" / "fixture" / "job.json"
        self.review_job.parent.mkdir(parents=True)
        self.review_job.write_text(json.dumps({"task_id": self.task_id, "request_id": self.review_request,
            "run_id": self.review_run, "execution_run_id": self.execution, "status": "finished",
            "execution_worktree": str(self.worktree), "execution_fingerprint": review_snapshot.fingerprint(self.original),
            "conclusion": "blocked", "report_path": str(self.report), "report_sha256": util.sha256_file(self.report)}))

    def payload(self, **extra):
        return {"task_id": self.task_id, "expected_execution_run_id": self.execution,
                "expected_review_request_id": self.review_request, "expected_report_sha256": util.sha256_file(self.report),
                "expected_plan_sha256": self.plan_sha, **extra}

    def raw_task(self):
        conn = db.connect(self.db_path)
        try:
            return dict(db.require_task(conn, self.task_id))
        finally:
            conn.close()

    def count(self):
        return len([r for r in self.requests() if r["task_id"] == self.task_id])

    def use_real_plus_record_shape(self):
        config = {"backend": "pi_desktop", "connection_file": str(self.root / "fake-plus.json"),
                  "provider_id": "fixture", "model_id": "fixture", "thinking_level": "medium",
                  "permission_mode": "auto"}
        self.spec.update(backend="pi_desktop", config=config, plan_sha256=self.plan_sha)
        self.spec_path.write_text(json.dumps(self.spec))
        self.job = pi_desktop_worker._base_job(self.spec)
        self.job.update(status="finished", ended_at=util.now_iso(), exit_code=0, pid=None,
                        session_id="completed-plus-session", turn_id="completed-plus-turn", send_state="sent")
        self.job_path.write_text(json.dumps(self.job))
        self.events.write_text(json.dumps({"seq": 1, "type": "session", "ts": util.now_iso(),
            "session_id": self.job["session_id"], "turn_id": self.job["turn_id"]}) + "\n" +
            json.dumps({"seq": 2, "type": "terminal", "outcome": "completed", "ts": util.now_iso()}) + "\n")
        conn = db.connect(self.db_path)
        request, _ = db.create_request(conn, util.now_iso(), "dispatch", "dispatch:" + self.task_id, task_id=self.task_id)
        db.update_request(conn, request["request_id"], util.now_iso(), status="succeeded", run_id=self.execution,
                          finished_at=util.now_iso())
        self.spec["request_id"] = request["request_id"]
        self.spec_path.write_text(json.dumps(self.spec))
        conn.execute("UPDATE execution_rounds SET backend='pi_desktop',config_json=?,session_id=? WHERE task_id=? AND run_id=?",
                     (util.jdump(config), self.job["session_id"], self.task_id, self.execution))
        conn.close()
        status = {"is_running": False, "current_turn_id": self.job["turn_id"], "pending_permission": None}
        turn = {"sessionId": self.job["session_id"], "turnId": self.job["turn_id"], "status": "completed"}
        for name, value in (("plus_status", lambda *args, **kwargs: dict(status)),
                            ("_get_client", lambda *args, **kwargs: (SimpleNamespace(turn_get=lambda *args, **kwargs: dict(turn)), {}))):
            patcher = patch.object(harness_plus_adapter, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_actual_plus_job_uses_frozen_startup_spec_and_registered_workspace(self):
        self.use_real_plus_record_shape()
        self.assertNotIn("worktree_path", self.job)
        self.assertNotIn("cwd", self.job)
        result = self.call("prepare-review-repair", self.payload())
        self.assertTrue(result["ok"])
        task = self.raw_task()
        record = review_resolution.remediation_of(task)
        self.assertIn(str(self.spec_path), record["execution_files"])
        review_snapshot.verify_source(self.worktree, self.original)
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        self.assertTrue(prepared["can_start"], prepared)
        self.assertFalse([request for request in self.requests() if request["kind"] == "dispatch" and request["status"] in ("queued", "claimed")])

    def test_actual_plus_job_supplement_preserves_evidence_and_requeues_review(self):
        self.use_real_plus_record_shape()
        self.assertTrue(self.call("supplement-review", self.payload(operation_id="actual-plus-shape", evidence_text="请复核体验证据"))["ok"])
        self.assertEqual(self.raw_task()["status"], "in_review")

    def use_formal_desktop_review_record(self):
        review = json.loads(self.review_job.read_text())
        review.update(backend="codex_desktop", role="review", frozen={"plan_sha256": self.plan_sha})
        self.review_job.unlink()
        self.review_job = self.runs_root / self.task_id / self.review_run / "job.json"
        self.review_job.parent.mkdir(parents=True)
        self.review_job.write_text(json.dumps(review))
        conn = db.connect(self.db_path)
        request = db.get_request(conn, self.review_request)
        payload = db.request_payload(request)
        payload.update(review_backend="codex_desktop", review_config={"backend": "codex_desktop"})
        db.update_request(conn, self.review_request, util.now_iso(), payload_json=payload)
        conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,created_at,updated_at) "
                     "VALUES(?,?,'codex_desktop','finished',?,?)", (self.task_id, self.review_run, util.now_iso(), util.now_iso()))
        conn.close()

    def test_actual_plus_execution_and_formal_desktop_review_prepare_to_materials(self):
        self.use_real_plus_record_shape()
        self.use_formal_desktop_review_record()
        self.assertTrue(self.call("prepare-review-repair", self.payload())["ok"])
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        self.assertTrue(prepared["can_start"], prepared["issues"])
        review_snapshot.verify_source(self.worktree, self.original)

    def test_formal_desktop_review_requires_registered_backend_role_and_run(self):
        self.use_real_plus_record_shape()
        self.use_formal_desktop_review_record()
        before = self.raw_task()
        saved = json.loads(self.review_job.read_text())
        for change in ({"backend": "pi_desktop"}, {"role": "plan"}, {"run_id": "another-run"}):
            self.review_job.write_text(json.dumps({**saved, **change}))
            self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
            self.assertEqual(self.raw_task(), before)
        for field in ("execution_fingerprint", "execution_worktree", "frozen"):
            changed = dict(saved)
            changed.pop(field)
            self.review_job.write_text(json.dumps(changed))
            self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
            self.assertEqual(self.raw_task(), before)
        self.review_job.write_text(json.dumps({**saved, "frozen": {"plan_sha256": "different-plan"}}))
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
        self.assertEqual(self.raw_task(), before)

    def test_real_formal_desktop_blocked_result_can_prepare_plus_execution_repair(self):
        self.use_real_plus_record_shape()
        self.call("set-review-config", {"backend": "codex_desktop"})
        result = self.call("resolve", {"task_id": self.task_id, "action": "retry"})
        self.assertEqual(result["request"]["kind"], "review")
        ctx = cli.Context(self.db_path)
        try:
            desktop_queue.cmd_set_desktop_queue_config(ctx, {"enabled": True})
            claim = desktop_queue._claim(ctx, {"role": "review", "operation_id": "formal-blocked-review", "thread_id": "fixture-review-thread"})
            self.assertTrue(claim["claimed"], claim)
            identity = {key: claim[key] for key in desktop_queue._IDENTITY}
            submitted = desktop_queue._submit(ctx, {**identity, "result": {
                "conclusion": "blocked", "report_markdown": "# 评审受阻\n\n需要原生验收证据，或先修复验收流程。\n", "fix": None}}, "review")
            self.assertTrue(submitted["ok"], submitted)
            task = db.require_task(ctx.conn, self.task_id)
            value = review_resolution.candidate(ctx.conn, task)
            self.assertIsNotNone(value)
            payload = {"task_id": self.task_id, **{key: value[field] for key, field in review_resolution.IDENTITY.items()}}
            prepared = review_resolution.cmd_prepare_review_repair(ctx, payload)
            self.assertTrue(prepared["ok"])
        finally:
            ctx.close()
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        self.assertTrue(prepared["can_start"], prepared["issues"])
        review_snapshot.verify_source(self.worktree, self.original)

    def test_actual_plus_startup_spec_missing_or_mismatched_fails_closed(self):
        self.use_real_plus_record_shape()
        before = self.raw_task()
        for field in ("task_id", "run_id", "cwd", "repository", "plan_path", "plan_sha256", "config", "backend", "runs_dir", "request_id"):
            value = dict(self.spec)
            value.pop(field)
            self.spec_path.write_text(json.dumps(value))
            self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict", field)
            self.assertEqual(self.raw_task(), before)
        self.spec_path.unlink()
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
        self.assertEqual(self.raw_task(), before)

    def test_actual_plus_job_does_not_ignore_conflicting_explicit_workspace(self):
        self.use_real_plus_record_shape()
        self.job_path.write_text(json.dumps({**self.job, "worktree_path": str(self.repo)}))
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")

    def test_actual_plus_request_binding_tamper_is_rejected(self):
        self.use_real_plus_record_shape()
        before = self.raw_task()
        self.spec["request_id"] = self.review_request
        self.spec_path.write_text(json.dumps(self.spec))
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
        self.assertEqual(self.raw_task(), before)

    def test_legacy_pi_config_without_backend_uses_real_pi_spec(self):
        self.spec["config"].pop("backend")
        self.spec_path.write_text(json.dumps(self.spec))
        self.job["config"] = self.spec["config"]
        self.job_path.write_text(json.dumps(self.job))
        conn = db.connect(self.db_path)
        conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id=? AND run_id=?",
                     (util.jdump(self.spec["config"]), self.task_id, self.execution))
        conn.close()
        self.assertTrue(self.call("prepare-review-repair", self.payload())["ok"])

    def test_history_repair_preserves_all_results_and_only_prepares(self):
        count = self.count()
        view = self.task(self.task_id)
        self.assertIn("resolve_review_blocker", view["allowed_actions"])
        self.assertIn("真实原生验收", view["review_resolution"]["summary"])
        result = self.call("prepare-review-repair", self.payload())
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        task = self.raw_task()
        self.assertEqual((task["status"], task["stage"], task["auto_advance_paused"]), ("needs_plan", "plan", 1))
        self.assertEqual(self.count(), count)
        record = review_resolution.remediation_of(task)
        self.assertNotIn("fix_request", db.task_extra(task))
        self.assertEqual(task["plan_kind"], "standard")
        target = Path(core.delivery_of(task)["worktree_path"])
        review_snapshot.verify_root(target, self.original, allow_branch=True)
        review_snapshot.verify_source(self.worktree, self.original)
        review_snapshot.verify_source(self.repo, self.primary)
        self.assertTrue(core.automation_of(task)["manual_takeover"])
        self.assertEqual(record["files"]["original-plan.md"]["sha256"], self.plan_sha)
        self.assertTrue(self.call("prepare-review-repair", self.payload())["deduplicated"])
        self.assertEqual(self.count(), count)
        self.assertTrue(all(method == "GET" for method, path, body in self.fake.requests_log))

    def prepare_and_queue(self):
        self.call("prepare-review-repair", self.payload())
        found = self.call("fetch-todos", {})["items"]
        self.assertTrue(next(item for item in found if item["task_id"] == self.task_id)["selectable"])
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        self.assertTrue(prepared["can_start"], prepared["issues"])
        result = self.call("start-plans", {"items": [{"input_id": prepared["input_id"],
                      "input_sha256": prepared["input_sha256"], "planner_backend": "codex"}]})["results"][0]
        self.assertIn("request_id", result, result)
        return prepared, result

    def test_plan_input_has_original_plan_report_and_full_worktree(self):
        prepared, result = self.prepare_and_queue()
        conn = db.connect(self.db_path)
        try:
            payload = db.request_payload(db.get_request(conn, result["request_id"]))
            manifest = payload["manifest"]
            entries = [entry for entry in manifest["files"] if entry["source"] == "review_remediation"]
            self.assertEqual({entry["name"] for entry in entries}, {"original-plan.md", "blocked-review.md"})
            self.assertIn("delete.py", {entry["path"] for entry in manifest["execution_worktree"]["changes"]})
            self.assertEqual(payload["workspace_version"], 1)
            self.assertIn("in-review", core.expected_remote_slugs(db.require_task(conn, self.task_id)))
            review_resolution.verify_plan_cycle(conn, db.require_task(conn, self.task_id), payload)
        finally:
            conn.close()
        self.assertIn("原计划范围与验收要求", review_resolution.prompt_block(manifest))

    def test_dispatch_rechecks_frozen_result_after_plan(self):
        self.call("prepare-review-repair", self.payload())
        task = self.raw_task()
        target = Path(core.delivery_of(task)["worktree_path"])
        (target / "new.py").write_text("changed after preparation\n")
        conn = db.connect(self.db_path)
        try:
            with self.assertRaises(util.KanpilotError):
                dispatch_validate.validate_dispatch(conn, db.require_task(conn, self.task_id), util.now_dt())
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch._ensure_worktree(db.require_task(conn, self.task_id), self.task_id, "new-exec", conn)
        finally:
            conn.close()

    def test_confirmed_plan_runs_and_still_requires_execution_confirmation(self):
        prepared, result = self.prepare_and_queue()
        with patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}):
            self.assertTrue(planner_runner.run_one(self.db_path, result["request_id"]))
        task = self.task(self.task_id)
        self.assertEqual(task["status"], "ready")
        self.assertTrue(task["extra"]["plan_execution_hold"])
        self.assertEqual(task["sent"], 0)
        self.assertIn("confirm_dispatch", task["allowed_actions"])
        self.assertFalse([request for request in self.requests() if request["kind"] == "dispatch"])
        conn = db.connect(self.db_path)
        try:
            checked = dispatch_validate.validate_dispatch(conn, db.require_task(conn, self.task_id), util.now_dt())
            self.assertEqual(checked["original_todo_slug"], "in-review")
            ordinary = dict(db.require_task(conn, self.task_id), extra_json="{}")
            self.assertNotIn("in-review", core.expected_remote_slugs(ordinary))
        finally:
            conn.close()
        queued = self.enqueue_dispatch(self.task_id, task["plan_sha256"])
        claim = self.call("claim", {"stage": "dispatch", "executor": "test-dispatch", "request_id": queued["request"]["request_id"]})
        self.assertTrue(claim["claimed"], claim)
        self.set_task_status(self.task_id, "submitting", transition_verified_at=util.now_iso())
        view = self.task(self.task_id)
        evidence = self.valid_evidence(view, view["plan_path"], message_visible=True)
        self.call("dispatch-confirm", {"task_id": self.task_id, "run_id": claim["run_id"], "evidence": evidence})
        task = self.raw_task()
        self.assertEqual(db.task_extra(task)["review_remediation"]["state"], "executing")
        (Path(core.delivery_of(task)["worktree_path"]) / "new.py").write_text("later execution results\n")
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(review_resolution.verify_remediation(conn, task))
            self.assertEqual(cmd_dispatch._ensure_worktree(task, self.task_id, claim["run_id"], conn)[1], "branch")
        finally:
            conn.close()
        # A new remediation plan is not a substitute for the original
        # acceptance scope in the subsequent fixed-branch Review.
        delivery = core.delivery_of(task)
        commit = git_delivery.commit_worktree(dict(delivery, repository=task["repository"]),
            task_id=self.task_id, title="repair fixture", execution_run_id=claim["run_id"], fix_round=0)
        conn = db.connect(self.db_path)
        try:
            core.update_delivery(conn, db.require_task(conn, self.task_id), util.now_iso(), head_sha=commit["sha"],
                commits=[{"execution_run_id": claim["run_id"], "sha": commit["sha"]}])
            dto = dict(db.require_task(conn, self.task_id))
            files = pi_desktop_review.collect_materials(db_path=self.db_path, conn=conn,
                materials=self.root / "post-repair-review", task=dto,
                execution_worktree=Path(delivery["worktree_path"]), execution_run_id=claim["run_id"])["files"]
            self.assertEqual(util.sha256_file(Path(files["original-plan.md"])), self.plan_sha)
            self.assertEqual(util.sha256_file(Path(files["blocked-review.md"])), util.sha256_file(self.report))
            self.assertNotEqual(Path(files["original-plan.md"]).read_bytes(), Path(files["plan.md"]).read_bytes())
        finally:
            conn.close()

    def test_failures_keep_block_and_unknown_collision_untouched(self):
        before = self.raw_task()
        for extra in ({"expected_execution_run_id": "wrong"}, {"expected_review_request_id": "wrong"},
                      {"expected_plan_sha256": "wrong"}, {"expected_report_sha256": "wrong"}):
            self.assertEqual(self.call_err("prepare-review-repair", self.payload(**extra)).code, "conflict")
            self.assertEqual(self.raw_task(), before)
        target = self.app_support / "worktrees" / self.task_id / "branch"
        target.mkdir()
        (target / "owner.txt").write_text("unknown user's files")
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
        self.assertEqual((target / "owner.txt").read_text(), "unknown user's files")
        self.assertEqual(self.raw_task(), before)

    def test_interruption_after_population_resumes_owned_intent(self):
        original = review_snapshot.populate
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("fixture interruption")
        with patch.object(review_snapshot, "populate", side_effect=interrupted):
            self.call_err("prepare-review-repair", self.payload())
        self.assertEqual(self.raw_task()["status"], "needs_user")
        self.assertTrue(self.call("prepare-review-repair", self.payload())["ok"])
        review_snapshot.verify_source(self.worktree, self.original)

    def test_user_evidence_creates_review_only_and_operation_replay(self):
        file = self.root / "acceptance.txt"
        file.write_text("user supplied checks, not independently verified\n")
        payload = self.payload(evidence_text="已在原生界面体验，请复核。", local_paths=[str(file)], operation_id="evidence-operation")
        first = self.call("supplement-review", payload)
        second = self.call("supplement-review", payload)
        self.assertFalse(first["deduplicated"])
        self.assertEqual(second, {**first, "deduplicated": True})
        original = file.read_text()
        file.unlink()
        self.assertEqual(self.call("supplement-review", payload), second)
        file.write_text("source changed after successful submission\n")
        self.assertEqual(self.call("supplement-review", payload), second)
        file.write_text(original)
        self.assertEqual(self.call_err("supplement-review", {**payload, "evidence_text": "different"}).code, "conflict")
        view = self.task(self.task_id)
        self.assertEqual(view["status"], "in_review")
        self.assertIsNone(view["review_summary"])
        self.assertEqual(view["run_id"], self.execution)
        self.assertTrue(self.report.exists())
        self.assertFalse([request for request in self.requests() if request["task_id"] == self.task_id and request["kind"] == "dispatch"])
        conn = db.connect(self.db_path)
        try:
            record = db.task_extra(db.require_task(conn, self.task_id))["review_supplements"]
            files = pi_desktop_review.collect_materials(db_path=self.db_path, conn=conn, materials=self.root / "materials",
                task=dict(db.require_task(conn, self.task_id)), execution_worktree=self.worktree,
                execution_run_id=self.execution)["files"]
            self.assertIn("待评审核验", Path(files["user-review-evidence.md"]).read_text())
            self.assertEqual(Path(files["user-evidence-0.txt"]).read_text(), file.read_text())
            self.assertNotIn("verification_override", db.task_extra(db.require_task(conn, self.task_id)))
            Path(record["files"][0]["path"]).write_text("tampered\n")
            with self.assertRaises(util.KanpilotError):
                review_resolution.collect_evidence(conn, dict(db.require_task(conn, self.task_id)), self.execution, self.root / "materials")
        finally:
            conn.close()

    def test_default_pi_without_controller_job_uses_registered_identity(self):
        self.review_job.unlink()
        conn = db.connect(self.db_path)
        now = util.now_iso()
        db.update_task(conn, self.task_id, now, status="in_review", auto_advance_paused=0)
        db.update_request(conn, self.review_request, now, status="claimed", claimed_by="kaneo-review",
                          lease_expires_at=util.plus_seconds(300), finished_at=None)
        conn.close()
        self.call("register-review", {"task_id": self.task_id, "request_id": self.review_request,
            "run_id": self.review_run, "expected_execution_run_id": self.execution,
            "report_path": str(self.report), "conclusion": "blocked"})
        self.call("report", {"request_id": self.review_request, "run_id": self.review_run,
            "outcome": "needs_user", "failure_code": "review_blocked", "note": "blocked acceptance"})
        result = self.call("supplement-review", self.payload(operation_id="no-controller-job", evidence_text="请核验原生验收"))
        self.assertTrue(result["ok"])
        self.assertFalse(self.review_job.exists())
        claim = self.call("claim", {"stage": "review", "executor": "kaneo-review"})
        self.assertTrue(claim["claimed"], claim)
        self.assertIn("user-review-evidence.md", claim["context"]["materials"])

    def test_paused_task_explicit_supplement_can_be_claimed(self):
        self.set_task_status(self.task_id, "needs_user", auto_advance_paused=1)
        result = self.call("supplement-review", self.payload(operation_id="paused-review", evidence_text="只授权本次复核"))
        claim = self.call("claim", {"stage": "review", "executor": "kaneo-review"})
        self.assertTrue(claim["claimed"], claim)
        self.assertTrue(core.automation_of(self.raw_task())["manual_takeover"])

    def test_existing_task_branch_is_reused(self):
        branch = "kaneopilot/" + self.task_id
        git(self.worktree, "switch", "-c", branch)
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "completed task result")
        frozen = review_snapshot.freeze(self.worktree)
        saved = json.loads(self.review_job.read_text())
        saved["execution_fingerprint"] = review_snapshot.fingerprint(frozen)
        self.review_job.write_text(json.dumps(saved))
        conn = db.connect(self.db_path)
        task = db.require_task(conn, self.task_id)
        sha = git(self.worktree, "rev-parse", "HEAD")
        core.update_delivery(conn, task, util.now_iso(), mode="manual", workspace_version=1,
            branch=branch, worktree_path=str(self.worktree), head_sha=sha, base_sha=self.original["head"], base_branch="main",
            commits=[{"execution_run_id": self.execution, "sha": sha}])
        conn.close()
        self.assertTrue(self.call("prepare-review-repair", self.payload())["ok"])
        self.assertEqual(core.delivery_of(self.raw_task())["worktree_path"], str(self.worktree))
        review_snapshot.verify_source(self.worktree, frozen)

    def test_incomplete_legacy_branch_does_not_unlock_into_invalid_plan(self):
        branch = "retained-legacy-branch"
        git(self.worktree, "switch", "-c", branch)
        conn = db.connect(self.db_path)
        core.update_delivery(conn, db.require_task(conn, self.task_id), util.now_iso(),
                             branch=branch, worktree_path=str(self.worktree))
        conn.close()
        before = self.raw_task()
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
        self.assertEqual(self.raw_task(), before)
        review_snapshot.verify_source(self.worktree, self.original)

    def test_queued_original_report_copy_tamper_rejected_before_model(self):
        prepared, result = self.prepare_and_queue()
        root = Path(prepared["input_dir"])
        (root / "files" / "blocked-review.md").write_text("tampered report copy\n")
        with patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}), patch.object(planner_runner, "_run_fake") as fake:
            self.assertTrue(planner_runner.run_one(self.db_path, result["request_id"]))
        fake.assert_not_called()
        request = next(request for request in self.requests() if request["request_id"] == result["request_id"])
        self.assertEqual(request["status"], "failed")

    def test_desktop_repair_plan_remote_checks_run_outside_write_lock(self):
        self.call("prepare-review-repair", self.payload())
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        result = self.call("start-plans", {"items": [{"input_id": prepared["input_id"],
            "input_sha256": prepared["input_sha256"], "planner_backend": "codex_desktop"}]})["results"][0]
        self.assertIn("request_id", result, result)
        ctx = cli.Context(self.db_path)
        original = remote.get_task
        def checked_remote(*args, **kwargs):
            self.assertFalse(ctx.conn.in_transaction, "远端读取持有 SQLite 写锁")
            return original(*args, **kwargs)
        try:
            desktop_queue.cmd_set_desktop_queue_config(ctx, {"enabled": True})
            with patch.object(remote, "get_task", side_effect=checked_remote) as remote_reads:
                claim = desktop_queue._claim(ctx, {"role": "plan", "operation_id": "desktop-repair-fixture", "thread_id": "fixture-thread"})
                self.assertTrue(claim["claimed"], claim)
                identity = {key: claim[key] for key in desktop_queue._IDENTITY}
                submitted = desktop_queue._submit(ctx, {**identity, "result": {"status": "ready",
                    "plan_markdown": "# 修复验收流程\n\n按原计划完成验收，不弱化要求。\n\n```verify\npython3 -m unittest\n```\n",
                    "questions": [], "evidence": []}}, "plan")
                self.assertTrue(submitted["ok"], submitted)
                self.assertGreater(remote_reads.call_count, 0)
        finally:
            ctx.close()

    def test_empty_and_unsafe_evidence_never_unblock(self):
        before = self.raw_task()
        self.assertEqual(self.call_err("supplement-review", self.payload(operation_id="empty")).code, "bad_request")
        source = self.root / "secret.env"
        source.write_text("test secret")
        link = self.root / "link.txt"
        link.symlink_to(source)
        self.assertEqual(self.call_err("supplement-review", self.payload(operation_id="link", local_paths=[str(link)])).code, "validation_failed")
        self.assertEqual(self.raw_task(), before)

    def test_terminal_plan_report_and_remote_guards(self):
        before = self.raw_task()
        self.job_path.write_text(json.dumps({**self.job, "status": "running"}))
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "conflict")
        self.job_path.write_text(json.dumps(self.job))
        conn = db.connect(self.db_path)
        conn.execute("UPDATE execution_rounds SET run_status='finished' WHERE task_id=? AND run_id=?", (self.task_id, self.execution))
        conn.close()
        self.fake.tasks[self.task_id]["status"] = "done"
        self.assertEqual(self.call_err("prepare-review-repair", self.payload()).code, "remote_conflict")
        self.assertEqual(self.raw_task(), before)
