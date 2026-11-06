"""Model-before-send recovery: disposable HOME/SQLite/Git and fake Kaneo only."""
import copy
import os
import subprocess
from pathlib import Path
from unittest import mock

from kanpilot import (cli, cmd_dispatch, cmd_exec, cmd_queue, codex_execution, core,
                      db, derive, execution_contract, git_delivery, process_owner,
                      remote, round_store, runtime_client, runtime_operations, util)
from test_session import HeadlessCase, pi_event, write_json, write_jsonl


def seed_preflight(conn, task, directory, worktree, now, *, request_id=None):
    """Persist the production failed dispatch/worker shape without starting a CLI."""
    task_id, run_id = task["task_id"], task["run_id"]
    if request_id is None:
        request, _ = db.create_request(conn, now, "dispatch", "original:" + run_id, task_id=task_id)
        request_id = request["request_id"]
    db.update_request(conn, request_id, now, status="failed", run_id=run_id)
    config = {"backend": "codex", **codex_execution.DEFAULT_CONFIG,
              "sandbox": "workspace-write", "execution_question_max_wait_seconds": 0}
    scope = runtime_client.stable_scope_id("execution", task_id=task_id, run_id=run_id)
    directory.mkdir(parents=True, exist_ok=True)
    spec = {"v": 1, "backend": "codex", "task_id": task_id, "run_id": run_id,
            "request_id": request_id, "scope_id": scope, "config": config,
            "cwd": str(worktree), "repository": task["repository"],
            "plan_path": task["plan_path"], "plan_sha256": task["plan_sha256"],
            "runs_dir": str(directory), "instruction": "Frozen task",
            "completion_policy": "command_verification", "required_verification_commands": ["python3 -V"]}
    if core.review_repair_of(task):
        from kanpilot import repair_prompt
        spec.update(repair_prompt.freeze(task))
    execution_contract.freeze_spec(spec, requirements_sha256=task["requirements_sha256"])
    write_json(directory / "spec.json", spec)
    pid = 99999998
    job = execution_contract.attach_to_job(spec, {
        "task_id": task_id, "run_id": run_id, "backend": "codex", "request_id": request_id,
        "scope_id": scope, "runs_dir": str(directory), "config": config,
        "plan_sha256": task["plan_sha256"], "spec_sha256": util.sha256_file(directory / "spec.json"),
        "pid": pid, "worker_birth": {"started_seconds": 0, "started_microseconds": 0},
        "status": "failed", "exit_code": 3, "send_state": "not_sent", "turns": [],
        "session_id": None, "turn_id": None, "started_at": now, "ended_at": now, "cli_terminal": True})
    write_json(directory / "job.json", job)
    write_jsonl(directory / "events.jsonl", [pi_event(1, "terminal", outcome="failed", reason="preflight")])
    round_store.upsert_on_claim(conn, task_id, run_id, config, now, backend="codex")
    conn.execute("UPDATE execution_rounds SET backend='codex',config_json=?,session_id=NULL WHERE task_id=? AND run_id=?",
                 (util.jdump(config), task_id, run_id))
    round_store.mark_started(conn, task_id, run_id, now, pid=pid, worktree_path=str(worktree))
    runtime_operations.begin_operation(conn, scope, "codex-start:" + run_id, "codex", "codex_execution_start",
                                       {"task_id": task_id, "run_id": run_id, "config": config}, now=now)
    runtime_operations.complete_operation(conn, scope, "codex-start:" + run_id, result={"pid": pid}, now=now)
    db.update_task(conn, task_id, now, status="needs_user", stage="dispatch", failure_code="dispatch_failed",
                   failure_stage="submitting", sent=None, send_state="unknown", session_id=None)
    return spec, job


class CodexPreflightRetryTests(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.make_ready_task(task_id="t")
        self.fake.tasks["t"]["status"] = "in-progress"
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.repo = self.repos_root / "Feedback"
        self.worktree = self.root / "execution"
        self.git("worktree", "add", "--detach", str(self.worktree), "HEAD")
        (self.worktree / "partial.txt").write_text("preserve implementation\n")
        (self.repo / "primary.txt").write_text("preserve primary work\n")
        db.update_task(self.ctx.conn, "t", self.ctx.now_iso, run_id="r", session_marker="t/r")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, plan_execution_hold=False, delivery={"mode": "manual"})
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso, manual_takeover=True)
        self.directory = self.runs_root / "t" / "r"
        self.spec, self.job = seed_preflight(self.ctx.conn, self.task(), self.directory, self.worktree, self.ctx.now_iso)
        self.request_id = self.spec["request_id"]
        self.scope = self.spec["scope_id"]
        self.payload = {"task_id": "t", "action": "resume", "expected_execution_run_id": "r"}
        self.configure_codex_execution()
        self.call("set-exec-config", {"backend": "codex", "codex": {"sandbox": "danger-full-access"}})

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                              env=git_delivery.git_env()).stdout.decode().strip()

    def task(self):
        return db.require_task(self.ctx.conn, "t")

    def save_job(self):
        write_json(self.directory / "job.json", self.job)

    def reject(self, payload=None):
        before = dict(self.task())
        requests = [dict(row) for row in self.ctx.conn.execute("SELECT * FROM requests")]
        with self.assertRaises(util.KanpilotError) as error:
            cmd_queue.cmd_resolve(self.ctx, payload or self.payload)
        self.assertEqual(before, dict(self.task()))
        self.assertEqual(requests, [dict(row) for row in self.ctx.conn.execute("SELECT * FROM requests")])
        return error.exception

    def test_strict_candidate_is_read_only_and_request_action_is_current_only(self):
        before = dict(self.task())
        row = dict(round_store.get_round(self.ctx.conn, "t", "r"))
        writes = self.ctx.conn.total_changes
        self.assertIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        request = db.get_request(self.ctx.conn, self.request_id)
        self.assertIn("resume", derive.request_allowed_actions(self.ctx.conn, request, self.task(), self.ctx.now))
        self.assertEqual(self.ctx.conn.total_changes, writes)
        historical, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "dispatch", "historical", task_id="t")
        db.update_request(self.ctx.conn, historical["request_id"], self.ctx.now_iso, status="failed", run_id="historical")
        self.assertNotIn("resume", derive.request_allowed_actions(self.ctx.conn,
            db.get_request(self.ctx.conn, historical["request_id"]), self.task(), self.ctx.now))
        db.update_request(self.ctx.conn, historical["request_id"], self.ctx.now_iso, run_id="r")
        self.assertNotIn("resume", derive.request_allowed_actions(self.ctx.conn,
            db.get_request(self.ctx.conn, historical["request_id"]), self.task(), self.ctx.now))
        self.assertEqual(before, dict(self.task()))
        self.assertEqual(row, dict(round_store.get_round(self.ctx.conn, "t", "r")))

    def test_confirm_reuses_dirty_worktree_claims_new_uuid_and_current_settings(self):
        old_round = dict(round_store.get_round(self.ctx.conn, "t", "r"))
        old_job = (self.directory / "job.json").read_bytes()
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual(self.task()["status"], "ready")
        self.assertIsNone(self.task()["run_id"])
        retry = core.execution_retry_of(self.task())
        self.assertEqual(retry["source_kind"], "codex_preflight")
        self.assertEqual(retry["source_run_id"], "r")
        self.reject()
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "test",
                                              "request_id": result["request"]["request_id"]})
        self.assertTrue(claimed["claimed"], claimed)
        self.assertNotEqual(claimed["run_id"], "r")
        config = util.jload(round_store.get_round(self.ctx.conn, "t", claimed["run_id"])["config_json"])
        self.assertEqual(config["sandbox"], "danger-full-access")
        path, _mode = cmd_dispatch._ensure_worktree(self.task(), "t", claimed["run_id"], self.ctx.conn, require_isolated=True)
        self.assertEqual(path, self.worktree)
        self.assertEqual((path / "partial.txt").read_text(), "preserve implementation\n")
        self.assertEqual((self.repo / "primary.txt").read_text(), "preserve primary work\n")
        self.assertEqual(old_round, dict(round_store.get_round(self.ctx.conn, "t", "r")))
        self.assertEqual(old_job, (self.directory / "job.json").read_bytes())
        self.reject()
        with mock.patch("kanpilot.cmd_dispatch.subprocess.Popen") as popen:
            self.assertEqual(self.call_err("dispatch-start", {"task_id": "t", "run_id": "r"}).code, "stale_run")
            popen.assert_not_called()

    def test_general_unknown_live_worker_cli_activity_or_incomplete_terminal_is_rejected(self):
        original = copy.deepcopy(self.job)
        for fields in ({"pid": os.getpid(), "worker_birth": process_owner.process_birth(os.getpid())},
                       {"worker_birth": None}, {"status": "running"}, {"exit_code": 0},
                       {"cli_terminal": False}, {"ended_at": None}, {"turns": [{}]},
                       {"session_id": "thread"}, {"turn_id": "turn"}, {"send_state": "unknown"}):
            with self.subTest(fields=fields):
                self.job = dict(copy.deepcopy(original), **fields)
                self.save_job()
                self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
                self.reject()
        self.job = original
        self.save_job()

    def test_identity_tampering_never_opens_recovery(self):
        original = copy.deepcopy(self.job)
        for fields in ({"request_id": "other"}, {"scope_id": "other"}, {"plan_sha256": "0" * 64},
                       {"spec_sha256": "0" * 64}, {"config": {}}, {"runs_dir": "/other"}):
            with self.subTest(fields=fields):
                self.job = dict(copy.deepcopy(original), **fields)
                self.save_job()
                self.assertFalse(cmd_queue.codex_preflight_retry_candidate(self.ctx.conn, self.task()))
                self.reject()
        self.job = original
        self.save_job()
        self.ctx.conn.execute("UPDATE runtime_operations SET status='unknown' WHERE scope_id=?", (self.scope,))
        self.reject()

    def test_exact_live_worker_identity_rejects_even_with_terminal_record(self):
        self.job.update(pid=os.getpid(), worker_birth=process_owner.process_birth(os.getpid()))
        self.save_job()
        self.ctx.conn.execute("UPDATE execution_rounds SET pid=? WHERE task_id='t' AND run_id='r'", (os.getpid(),))
        self.ctx.conn.execute("UPDATE runtime_operations SET result_json=? WHERE scope_id=?",
                              (util.jdump({"pid": os.getpid()}), self.scope))
        self.assertFalse(cmd_queue.codex_preflight_retry_candidate(self.ctx.conn, self.task()))
        self.reject()

    def test_expected_run_user_confirmation_and_special_repairs_are_required(self):
        self.reject({"task_id": "t", "action": "resume"})
        self.assertEqual(self.reject(dict(self.payload, expected_execution_run_id="old")).code, "stale_run")
        self.reject(dict(self.payload, actor="automation"))
        for source in ("merge_conflict", "build_failure"):
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, fix_request={"source_kind": source, "mode": "direct"})
            self.assertFalse(cmd_queue.codex_preflight_retry_candidate(self.ctx.conn, self.task()))
            self.reject()

    def test_remote_plan_and_transaction_evidence_drift_reject_without_writes(self):
        self.fake.tasks["t"]["status"] = "done"
        self.reject()
        self.fake.tasks["t"]["status"] = "in-progress"
        original = remote.get_task
        def mutate_after_remote(*args, **kwargs):
            result = original(*args, **kwargs)
            self.spec["instruction"] += " changed"
            write_json(self.directory / "spec.json", self.spec)
            return result
        with mock.patch.object(remote, "get_task", side_effect=mutate_after_remote):
            self.reject()

    def test_queued_source_fingerprint_or_worktree_drift_blocks_dispatch(self):
        cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.job["detail"] = "late changed evidence"
        self.save_job()
        with self.assertRaises(util.KanpilotError):
            cmd_queue.verify_execution_retry_terminal(self.ctx.conn, self.task())
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(self.task(), "t", "new-run", self.ctx.conn, require_isolated=True)

    def test_remote_requirement_or_plan_drift_and_active_requests_reject(self):
        description = self.fake.tasks["t"]["description"]
        self.fake.tasks["t"]["description"] = "different requirements"
        self.reject()
        self.fake.tasks["t"]["description"] = description
        plan = Path(self.task()["plan_path"])
        raw = plan.read_bytes()
        plan.write_bytes(raw + b"\nchanged plan\n")
        self.reject()
        plan.write_bytes(raw)
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "monitor", "active", task_id="t")
        self.assertFalse(cmd_queue.codex_preflight_retry_candidate(self.ctx.conn, self.task()))
        self.reject()
        db.update_request(self.ctx.conn, request["request_id"], self.ctx.now_iso, status="failed")

    def test_transaction_request_drift_and_queued_worktree_drift_reject(self):
        original = cmd_queue._execution_retry_materials
        def changed_request(*args, **kwargs):
            prepared = original(*args, **kwargs)
            self.ctx.conn.execute("UPDATE requests SET result_json=? WHERE request_id=?",
                                  (util.jdump({"changed": True}), self.request_id))
            return prepared
        before = dict(self.task())
        with mock.patch.object(cmd_queue, "_execution_retry_materials", side_effect=changed_request):
            with self.assertRaises(util.KanpilotError):
                cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual(dict(self.task()), before)
        cmd_queue.cmd_resolve(self.ctx, self.payload)
        (self.worktree / "partial.txt").write_text("changed after confirmation")
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(self.task(), "t", "new-run", self.ctx.conn, require_isolated=True)

    def test_late_worker_start_after_queue_is_rejected_without_another_launch(self):
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.job.update(status="running", exit_code=None, ended_at=None, cli_terminal=False)
        self.save_job()
        with mock.patch("kanpilot.cmd_dispatch.subprocess.Popen") as popen:
            self.assertEqual(self.call_err("dispatch-start", {"task_id": "t", "run_id": "r"}).code, "stale_run")
            popen.assert_not_called()
        count = self.ctx.conn.execute("SELECT COUNT(*) FROM execution_rounds").fetchone()[0]
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "test",
                                              "request_id": result["request"]["request_id"]})
        self.assertFalse(claimed["claimed"], claimed)
        self.assertEqual(count, self.ctx.conn.execute("SELECT COUNT(*) FROM execution_rounds").fetchone()[0])

    def test_read_actions_tolerate_missing_or_malformed_evidence_without_writes(self):
        request = db.get_request(self.ctx.conn, self.request_id)
        for value in ([], {"stage_contract_required": True}, {**self.spec, "repair_prompt": "invalid"}):
            write_json(self.directory / "spec.json", value)
            writes = self.ctx.conn.total_changes
            self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
            self.assertNotIn("resume", derive.request_allowed_actions(self.ctx.conn, request, self.task(), self.ctx.now))
            self.assertEqual(self.ctx.conn.total_changes, writes)
        (self.directory / "spec.json").unlink()
        self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
