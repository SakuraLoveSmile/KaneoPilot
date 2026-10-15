"""Formal Desktop MCP with isolated roots, in-memory Kaneo and local Git only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import io
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import (cli, db, desktop_queue as desktop, headless_adapter,
                      pi_desktop_review, remote, review_snapshot, round_store, util, worktree_pool)


class DesktopQueueTests(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repos_root = self.repos_root.resolve()
        self.call("set-planner-config", {"backend": "codex_desktop"})
        self.call("set-review-config", {"backend": "codex_desktop"})
        self.configure(enabled=True)

    def configure(self, **payload):
        ctx = cli.Context(self.db_path)
        try:
            return desktop.cmd_set_desktop_queue_config(ctx, payload)
        finally:
            ctx.close()

    def native(self, name, payload=None, error=False):
        result = desktop._tool_result("kp_desktop_" + name, payload or {}, self.db_path)
        self.assertEqual(result["isError"], error, result)
        return json.loads(result["content"][0]["text"])

    def identity(self, claim):
        return {key: claim[key] for key in desktop._IDENTITY}

    def plan(self, suffix="1", method="builtin"):
        task = self.fake.add_task("desktopformalplan" + suffix, "ryryo1reph1xfemqtwkjyiiz",
                                  status="to-do", description="需求：限制整数输入并显示平方")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        prepared = self.call("prepare-plan-input", {"task_id": task["id"], "local_paths": [],
                                                    "supplement": "只读规划", "excluded_remote_ids": []})
        result = self.call("start-plans", {"items": [{"input_id": prepared["input_id"],
                                                     "input_sha256": prepared["input_sha256"],
                                                     "planner_backend": "codex_desktop", "planner_method": method}]})["results"][0]
        self.assertIn("request_id", result, result)
        return task, result["request_id"]

    def claim(self, role="plan", op="op1"):
        return self.native("claim", {"role": role, "operation_id": op, "thread_id": "native-thread"})

    def submit_plan(self, claim, **override):
        result = {"status": "ready", "plan_markdown": "# 规划\n\n1. 校验整数。\n2. 运行验证。", "questions": [], "evidence": []}
        result.update(override)
        return self.native("submit_plan", {**self.identity(claim), "result": result})

    def mutate(self, sql, args=()):
        with closing(db.connect(self.db_path)) as conn:
            conn.execute(sql, args)
            conn.commit()

    def read_job(self, claim):
        return json.loads(Path(claim["context"]["job_path"]).read_text())

    def make_review(self, suffix="1", automated=False, return_plan=False):
        task, _request = self.plan(suffix)
        claimed = self.claim(op="plan-" + suffix)
        self.submit_plan(claimed)
        if return_plan:
            self.return_slot(claimed)
        repository = self.repos_root / "Feedback"
        def git(*args):
            subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
        if not (repository / ".git").is_dir():
            git("init")
            git("config", "user.name", "Isolated Fixture")
            git("config", "user.email", "fixture@example.invalid")
        if not (repository / "sample.py").is_file():
            (repository / "sample.py").write_text("def square(n):\n    return n*n\n")
            git("add", "sample.py")
            git("commit", "-m", "isolated initial")
        self.fake.tasks[task["id"]]["status"] = "in-review"
        execution_run = "exec-" + suffix
        if automated:
            self.call("set-project-automation", {"project_id": task["projectId"],
                                                 "automation": {"enabled": True, "max_fix_rounds": 2},
                                                 "expected_policy_revision": 0})
        with closing(db.connect(self.db_path)) as conn:
            db.update_task(conn, task["id"], util.now_iso(), status="in_review", stage="review", run_id=execution_run)
            row = db.require_task(conn, task["id"])
            # These cases cover historical uncommitted Review snapshots.
            # New branch-only Review has its own committed-worktree cases.
            extra = db.task_extra(row)
            extra.pop("delivery", None)
            db.update_task(conn, task["id"], util.now_iso(), extra_json=extra)
            row = db.require_task(conn, task["id"])
            if automated:
                db.set_extra(conn, row, util.now_iso(), automation={"manual_takeover": False, "fix_round": 0,
                                                                  "max_fix_rounds": 3})
            round_store.upsert_on_claim(conn, task["id"], execution_run, {"backend": "pi"}, util.now_iso(), backend="pi")
            conn.execute("UPDATE execution_rounds SET worktree_path=?,run_status='finished' WHERE task_id=? AND run_id=?",
                         (str(repository), task["id"], execution_run))
            evidence = headless_adapter.resolve_source(conn).run_dir(task["id"], execution_run)
            evidence.mkdir(parents=True)
            (evidence / "job.json").write_text(json.dumps({"task_id": task["id"], "run_id": execution_run,
                                                         "backend": "pi", "status": "finished", "exit_code": 0}))
            (evidence / "events.jsonl").write_text('{"type":"terminal","outcome":"completed"}\n')
            request, _ = db.create_request(conn, util.now_iso(), "review", "review:" + task["id"], task_id=task["id"])
            conn.commit()
        return task, request["request_id"]

    def review_result(self, conclusion):
        return {"conclusion": conclusion, "report_markdown": "# Review\n证据、结论与待补事项。",
                "fix": {"title": "补充校验", "description": "覆盖小数与非数字输入", "priority": "high"}
                if conclusion == "changes_requested" else None}

    def return_slot(self, claim, error=False, **override):
        return self.native("return_slot", {**self.identity(claim), "confirm": True, **override}, error=error)

    def slot(self, claim):
        lease = self.read_job(claim)["pool_lease"]
        with closing(db.connect(self.db_path)) as conn:
            return dict(conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (lease["slot_id"],)).fetchone())

    def test_return_requires_explicit_terminal_identity_and_persisted_output(self):
        self.plan()
        claim = self.claim()
        self.assertEqual(self.return_slot(claim, error=True)["code"], "conflict")
        self.submit_plan(claim)
        for override in ({"confirm": False}, {"confirm": 1}, {"claim_token": "foreign"},
                         {"run_id": "foreign"}, {"path": "/not-accepted"}):
            self.return_slot(claim, error=True, **override)
            self.assertEqual(self.slot(claim)["state"], "leased")
        job = self.read_job(claim)
        output = Path(job["plan_path"])
        original = output.read_bytes()
        output.write_text("changed output")
        self.assertEqual(self.return_slot(claim, error=True)["code"], "conflict")
        self.assertEqual(self.slot(claim)["state"], "leased")
        output.write_bytes(original)
        reply = self.return_slot(claim)
        self.assertEqual(reply["state"], "free")
        self.assertEqual(self.slot(claim)["owner_state"], "returned")
        self.assertNotIn("owner_stopped", self.read_job(claim))
        with closing(db.connect(self.db_path)) as conn:
            lease = job["pool_lease"]
            receipt = db.get_runtime_operation(conn, claim["run_id"], "desktop-return:" + lease["lease_token"])
            self.assertEqual(receipt["status"], "accepted")
            self.assertTrue(worktree_pool.consumer_returned(conn, lease))
        self.assertTrue(self.return_slot(claim)["deduplicated"])
        self.assertFalse(self.submit_plan(claim)["return_slot_required"])
        self.native("heartbeat", {**self.identity(claim), "progress": "too late"}, error=True)

    def test_return_needs_input_and_fail_preserve_persistent_results(self):
        self.call("set-config", {"worktree_pool_size": 1})
        self.plan("draft")
        draft_claim = self.claim(op="draft")
        self.submit_plan(draft_claim, status="needs_input", plan_markdown="# 草稿", questions=["范围？"])
        self.assertEqual(self.return_slot(draft_claim)["state"], "free")
        draft = Path(draft_claim["context"]["job_path"]).parent / "plan-draft.md"
        self.assertEqual(draft.read_text(), "# 草稿")
        self.plan("failed")
        failed = self.claim(op="failed")
        response = self.native("fail", {**self.identity(failed), "reason": "fixture failure"})
        self.assertTrue(response["return_slot_required"])
        self.assertEqual(self.slot(failed)["state"], "leased")
        self.assertEqual(self.return_slot(failed)["state"], "free")
        with patch.object(desktop, "_secured_command", side_effect=AssertionError("terminal replay wrote")):
            retry = self.native("fail", {**self.identity(failed), "reason": "fixture failure"})
        self.assertTrue(retry["deduplicated"])
        self.assertFalse(retry["return_slot_required"])
        self.native("fail", {**self.identity(failed), "reason": "different failure"}, error=True)

    def test_manual_branch_without_pool_never_cleans_persistent_directory(self):
        task, request_id = self.plan()
        with closing(db.connect(self.db_path)) as conn:
            row = db.require_task(conn, task["id"])
            extra = db.task_extra(row)
            delivery = extra["delivery"]
            delivery.pop("workspace_version", None)
            delivery["mode"] = "manual"
            extra["automation"] = {"manual_takeover": True}
            db.update_task(conn, task["id"], util.now_iso(), extra_json=extra)
            request = db.get_request(conn, request_id)
            payload = db.request_payload(request)
            payload.pop("workspace_version", None)
            payload.update(delivery_mode="manual", plan_cwd=delivery["worktree_path"])
            db.update_request(conn, request_id, util.now_iso(), payload_json=payload)
        claim = self.claim()
        self.assertNotIn("pool_lease", self.read_job(claim))
        persistent = Path(delivery["worktree_path"])
        sentinel = persistent / "manual-notes.txt"
        sentinel.write_text("keep manual work")
        self.submit_plan(claim)
        with patch.object(desktop.task_workspace, "release_pool", side_effect=AssertionError("manual directory cleaned")):
            response = self.return_slot(claim)
        self.assertEqual(response["reason"], "no_pool_lease")
        self.assertFalse(response["return_slot_required"])
        self.assertEqual(sentinel.read_text(), "keep manual work")

    def test_fail_missing_or_foreign_job_does_not_settle_request(self):
        self.plan()
        claim = self.claim()
        path = Path(claim["context"]["job_path"])
        original = path.read_bytes()
        path.unlink()
        self.native("fail", {**self.identity(claim), "reason": "fixture failure"}, error=True)
        path.write_bytes(original)
        job = self.read_job(claim)
        job["request_id"] = "foreign"
        path.write_text(json.dumps(job))
        self.native("fail", {**self.identity(claim), "reason": "fixture failure"}, error=True)
        self.assertEqual(self.call("get-request", {"request_id": claim["request_id"]})["request"]["status"], "claimed")

    def test_return_old_generation_replay_never_reads_reused_slot(self):
        self.call("set-config", {"worktree_pool_size": 1})
        self.plan("old")
        old = self.claim(op="old")
        self.submit_plan(old)
        self.return_slot(old)
        self.plan("new")
        new = self.claim(op="new")
        self.assertEqual(self.slot(old)["path"], self.slot(new)["path"])
        before = self.slot(new)
        with patch.object(worktree_pool, "_validate", side_effect=AssertionError("old replay read slot")), \
             patch.object(desktop, "_guard_materials", side_effect=AssertionError("old replay read materials")):
            self.assertTrue(self.return_slot(old)["deduplicated"])
            self.assertTrue(self.submit_plan(old)["deduplicated"])
            reply = self.claim(op="old")
            self.assertEqual(reply["reason"], "slot_returned")
            self.assertNotIn("context", reply)
            self.assertNotIn("claim_token", reply)
        self.assertEqual(before, self.slot(new))

    def test_return_receipt_crash_reaper_preserves_artifacts_before_cleanup(self):
        self.plan()
        claim = self.claim()
        self.submit_plan(claim)
        root = Path(claim["context"]["plan_cwd"])
        evidence = root / ".pi" / "plan" / "proposal.md"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("durable optional proposal")
        material = root / ".kaneopilot-plan-material"
        material.mkdir()
        (material / "input.md").write_text("durable optional material")
        with patch.object(desktop.task_workspace, "release_pool", side_effect=SystemExit("crash after receipt")):
            with self.assertRaises(SystemExit):
                self.return_slot(claim)
        self.assertEqual(self.slot(claim)["state"], "leased")
        self.assertNotIn("return_result", self.read_job(claim))
        self.mutate("UPDATE worktree_slots SET lease_expires_at=?", ("2000-01-01T00:00:00Z",))
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(worktree_pool.reap_expired(conn)[0]["state"], "free")
        saved = Path(claim["context"]["job_path"]).parent
        self.assertEqual((saved / "plus-plan-evidence" / "proposal.md").read_text(), "durable optional proposal")
        self.assertEqual((saved / "plan-materials" / "input.md").read_text(), "durable optional material")
        self.assertFalse(evidence.exists())
        self.assertEqual(self.return_slot(claim)["state"], "free")

    def test_return_crash_before_receipt_keeps_slot_and_retry_recovers(self):
        self.plan()
        claim = self.claim()
        self.submit_plan(claim)
        with patch.object(desktop.runtime_operations, "complete_operation", side_effect=SystemExit("receipt transaction crash")):
            with self.assertRaises(SystemExit):
                self.return_slot(claim)
        with closing(db.connect(self.db_path)) as conn:
            lease = self.read_job(claim)["pool_lease"]
            self.assertIsNone(db.get_runtime_operation(conn, claim["run_id"], "desktop-return:" + lease["lease_token"]))
        self.assertEqual(self.slot(claim)["state"], "leased")
        self.assertEqual(self.return_slot(claim)["state"], "free")

    def test_return_crash_after_release_recovers_without_reading_slot(self):
        self.plan()
        claim = self.claim()
        self.submit_plan(claim)
        with patch.object(desktop, "_write", side_effect=SystemExit("job write crash")):
            with self.assertRaises(SystemExit):
                self.return_slot(claim)
        self.assertEqual(self.slot(claim)["state"], "free")
        with patch.object(worktree_pool, "_validate", side_effect=AssertionError("recovery read freed slot")):
            self.assertEqual(self.return_slot(claim)["state"], "free")

    def test_replayed_claim_cannot_overwrite_or_expose_after_return(self):
        self.plan()
        claim = self.claim()
        entered, resume = threading.Event(), threading.Event()
        expose = desktop._expose_claim
        def paused(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(timeout=15))
            return expose(*args, **kwargs)
        with patch.object(desktop, "_expose_claim", paused):
            with ThreadPoolExecutor(max_workers=1) as executor:
                replay = executor.submit(self.claim)
                self.assertTrue(entered.wait(timeout=15))
                self.submit_plan(claim)
                self.return_slot(claim)
                before = Path(claim["context"]["job_path"]).read_bytes()
                resume.set()
                reply = replay.result(timeout=15)
        self.assertEqual(reply["reason"], "slot_returned")
        self.assertNotIn("context", reply)
        self.assertEqual(before, Path(claim["context"]["job_path"]).read_bytes())

    def test_claim_exposure_holds_same_lock_as_terminal_submission(self):
        self.plan()
        claim = self.claim()
        entered, resume = threading.Event(), threading.Event()
        owner = desktop.task_workspace.pool_owner
        def paused(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(timeout=15))
            return owner(*args, **kwargs)
        with patch.object(desktop.task_workspace, "pool_owner", paused):
            with ThreadPoolExecutor(max_workers=1) as executor:
                replay = executor.submit(self.claim)
                self.assertTrue(entered.wait(timeout=15))
                result = {"status": "ready", "plan_markdown": "# 规划"}
                failure = self.native("submit_plan", {**self.identity(claim), "result": result}, error=True)
                self.assertEqual(failure["code"], "busy")
                resume.set()
                self.assertTrue(replay.result(timeout=15)["claimed"])
        self.submit_plan(claim)
        self.assertEqual(self.return_slot(claim)["state"], "free")

    def test_desktop_forged_stop_flags_and_backend_cannot_release_without_receipt(self):
        self.plan()
        claim = self.claim()
        self.submit_plan(claim)
        for override in ({"owner_stopped": True}, {"send_state": "not_sent"},
                         {"backend": "codex", "owner_stopped": True, "send_state": "not_sent"}):
            job = self.read_job(claim)
            job.update(override)
            desktop.task_workspace.release_pool(self.db_path, job, Path(claim["context"]["job_path"]).parent)
            self.assertEqual(self.slot(claim)["state"], "leased")
            self.assertEqual(self.slot(claim)["owner_state"], "unknown")

    def test_returned_quarantine_recovery_preserves_previously_uncopied_ignored_artifacts(self):
        self.plan()
        claim = self.claim()
        self.submit_plan(claim)
        root = Path(claim["context"]["plan_cwd"])
        ignore = self.repos_root / "Feedback" / ".git" / "info" / "exclude"
        with ignore.open("a") as stream:
            stream.write("\n.pi/\n.kaneopilot-plan-material/\n")
        proposal = root / ".pi" / "plan" / "proposal.md"
        proposal.parent.mkdir(parents=True)
        proposal.write_text("preserve before unquarantine")
        material = root / ".kaneopilot-plan-material"
        material.mkdir()
        (material / "input.md").write_text("preserve material before unquarantine")
        with patch.object(desktop.task_workspace, "preserve_pool_artifacts", side_effect=OSError("copy temporarily failed")):
            self.assertEqual(self.return_slot(claim)["state"], "quarantined")
        directory = Path(claim["context"]["job_path"]).parent
        self.assertFalse((directory / "plus-plan-evidence" / "proposal.md").exists())
        lease = self.read_job(claim)["pool_lease"]
        with closing(db.connect(self.db_path)) as conn:
            result = worktree_pool.unquarantine(conn, **desktop.task_workspace.pool_identity(lease), confirm=True)
        self.assertEqual(result["state"], "free")
        self.assertEqual((directory / "plus-plan-evidence" / "proposal.md").read_text(), "preserve before unquarantine")
        self.assertEqual((directory / "plan-materials" / "input.md").read_text(), "preserve material before unquarantine")
        replay = self.return_slot(claim)
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["pool_released"])
        self.assertEqual(replay["state"], "free")

    def test_unknown_return_receipt_is_not_promoted_or_cleaned(self):
        self.plan()
        claim = self.claim()
        self.submit_plan(claim)
        lease = self.read_job(claim)["pool_lease"]
        with closing(db.connect(self.db_path)) as conn:
            db.insert_runtime_operation(conn, claim["run_id"], "desktop-return:" + lease["lease_token"],
                                        "desktop_queue", "worktree_consumer_return", "unknown", "unknown", util.now_iso())
        self.assertEqual(self.return_slot(claim, error=True)["code"], "conflict")
        self.assertEqual(self.slot(claim)["state"], "leased")
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(db.get_runtime_operation(conn, claim["run_id"], "desktop-return:" + lease["lease_token"])["status"], "unknown")

    def test_fail_response_loss_recovers_persisted_failure_then_can_return(self):
        self.plan()
        claim = self.claim()
        entry = cli.COMMANDS["report"]
        def lost(ctx, payload):
            entry[1](ctx, payload)
            raise util.KanpilotError("response_lost", "isolated response lost")
        with patch.dict(cli.COMMANDS, {"report": (entry[0], lost)}):
            self.native("fail", {**self.identity(claim), "reason": "fixture failure"}, error=True)
        self.assertEqual(self.slot(claim)["state"], "leased")
        with patch.object(desktop, "_secured_command", side_effect=AssertionError("failure replay wrote")):
            self.native("fail", {**self.identity(claim), "reason": "fixture failure"})
        self.assertEqual(self.return_slot(claim)["state"], "free")

    def test_return_receipt_reaper_missing_or_foreign_checkpoint_retains_slot(self):
        self.call("set-config", {"worktree_pool_size": 2})
        for kind in ("missing", "lease"):
            self.plan(kind)
            claim = self.claim(op=kind)
            self.submit_plan(claim)
            with patch.object(desktop.task_workspace, "release_pool", side_effect=SystemExit("crash")):
                with self.assertRaises(SystemExit):
                    self.return_slot(claim)
            path = Path(claim["context"]["job_path"])
            job = self.read_job(claim)
            if kind == "missing":
                path.unlink()
            else:
                job["pool_lease"]["path"] = "/foreign"
                path.write_text(json.dumps(job))
            self.mutate("UPDATE worktree_slots SET lease_expires_at=?", ("2000-01-01T00:00:00Z",))
            with closing(db.connect(self.db_path)) as conn:
                with patch.object(desktop.task_workspace, "release_pool", side_effect=AssertionError("invalid checkpoint released")):
                    rows = worktree_pool.reap_expired(conn)
                self.assertTrue(all(row["state"] == "leased" for row in rows))

    def test_return_preserves_unknown_operations_and_unknown_changes(self):
        self.call("set-config", {"worktree_pool_size": 2})
        for kind in ("operation", "changes"):
            self.plan(kind)
            claim = self.claim(op=kind)
            self.submit_plan(claim)
            root = Path(claim["context"]["plan_cwd"])
            if kind == "operation":
                with closing(db.connect(self.db_path)) as conn:
                    db.insert_runtime_operation(conn, claim["run_id"], "unknown", "fixture", "fixture", "hash", "unknown", util.now_iso())
            else:
                (root / "unknown.txt").write_text("preserve me")
            response = self.return_slot(claim)
            self.assertFalse(response["ok"])
            self.assertTrue(response["returned"])
            self.assertEqual(response["state"], "leased" if kind == "operation" else "quarantined")
            if kind == "changes":
                self.assertEqual((root / "unknown.txt").read_text(), "preserve me")

    def test_twenty_desktop_plan_review_consumers_reuse_one_slot_with_outputs(self):
        self.call("set-config", {"worktree_pool_size": 1})
        tokens, roots, outputs = [], set(), []
        for number in range(10):
            task, _ = self.make_review("reuse" + str(number), return_plan=True)
            with closing(db.connect(self.db_path)) as conn:
                outputs.append(Path(db.require_task(conn, task["id"])["plan_path"]))
                plan_request = conn.execute("SELECT * FROM requests WHERE task_id=? AND stage='plan'", (task["id"],)).fetchone()
                plan_lease = db.request_payload(plan_request)["pool_lease"]
                tokens.append(plan_lease["lease_token"])
                roots.add(plan_lease["path"])
            claim = self.claim("review", "review-reuse" + str(number))
            lease = self.read_job(claim)["pool_lease"]
            tokens.append(lease["lease_token"])
            roots.add(lease["path"])
            reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")})
            self.assertEqual(self.slot(claim)["state"], "leased")
            outputs.append(Path(reply["report_path"]))
            self.assertEqual(self.return_slot(claim)["state"], "free")
        self.assertEqual(len(tokens), 20)
        self.assertEqual(len(set(tokens)), 20)
        self.assertEqual(len(roots), 1)
        self.assertTrue(all(path.is_file() and path.read_text().strip() for path in outputs))
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worktree_slots").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM runtime_operations WHERE tool='worktree_consumer_return' AND status='accepted'").fetchone()[0], 20)

    def test_formal_plan_publishes_hold_and_terminal_retry_is_read_only(self):
        task, request_id = self.plan()
        claim = self.claim()
        self.assertEqual(claim["request_id"], request_id)
        self.assertLessEqual(claim["heartbeat_seconds"], 60)
        job = self.read_job(claim)
        self.assertEqual(Path(claim["context"]["plan_cwd"]).name, "slot-1")
        self.assertNotEqual(claim["context"]["plan_cwd"], job["context"]["payload"]["plan_cwd"])
        self.assertEqual(self.native("heartbeat", {**self.identity(claim), "progress": "读取冻结材料"})["ok"], True)
        result = self.submit_plan(claim)
        self.assertEqual(result["task_status"], "ready")
        self.assertEqual(result["request_status"], "succeeded")
        current = self.call("get-task", {"task_id": task["id"]})["task"]
        self.assertTrue(current["extra"]["plan_execution_hold"])
        with closing(db.connect(self.db_path)) as conn:
            row = db.require_task(conn, task["id"])
            self.assertEqual(row["planner_model"], "codex-desktop")
            slot = worktree_pool.validate_lease(conn, **desktop.task_workspace.pool_identity(job["pool_lease"]))
            self.assertEqual(slot["state"], "leased")
            self.assertEqual(slot["owner_state"], "unknown")
            self.assertEqual(row["reasoning_effort"], "inherit")
            self.assertEqual(round_store.get_round(conn, task["id"], claim["run_id"])["backend"], "codex_desktop")
        published = Path(result["plan_path"])
        before = published.read_bytes(), published.stat().st_mtime_ns
        with patch.object(desktop.cmd_exec, "cmd_publish_plan", side_effect=AssertionError("terminal retry wrote")), \
             patch.object(desktop.planner_runner, "write_plan_file", side_effect=AssertionError("terminal retry generated")):
            retry = self.submit_plan(claim)
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(before, (published.read_bytes(), published.stat().st_mtime_ns))
        self.assertEqual(self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "不同"}}, error=True)["code"], "conflict")

    def test_new_task_desktop_review_reads_only_committed_branch_snapshot(self):
        task, _ = self.plan("branch")
        plan_claim = self.claim()
        self.submit_plan(plan_claim)
        with closing(db.connect(self.db_path)) as conn:
            row = db.require_task(conn, task["id"])
            delivery = db.task_extra(row)["delivery"]
            execution = Path(delivery["worktree_path"])
            self.assertEqual(delivery["workspace_version"], 1)
            (execution / "feature.py").write_text("def feature():\n    return 42\n")
            now = util.now_iso()
            db.update_task(conn, task["id"], now, status="running", stage="monitor", run_id="execbranch")
            round_store.upsert_on_claim(conn, task["id"], "execbranch", {"backend": "pi"}, now, backend="pi")
            conn.execute("UPDATE execution_rounds SET worktree_path=?,run_status='finished' WHERE task_id=? AND run_id=?",
                         (str(execution), task["id"], "execbranch"))
            directory = headless_adapter.resolve_source(conn).run_dir(task["id"], "execbranch")
            directory.mkdir(parents=True)
            (directory / "job.json").write_text(json.dumps({"task_id": task["id"], "run_id": "execbranch",
                                                            "backend": "pi", "status": "finished", "exit_code": 0}))
            (directory / "events.jsonl").write_text('{"type":"terminal","outcome":"completed"}\n')
            monitor, _ = db.create_request(conn, now, "monitor", "monitor:" + task["id"], task_id=task["id"])
            conn.commit()
        self.fake.tasks[task["id"]]["status"] = "in-progress"
        claimed = self.call("claim", {"stage": "monitor", "request_id": monitor["request_id"], "executor": "fixture"})
        committed = self.call("delivery-commit", {"task_id": task["id"], "request_id": monitor["request_id"],
                                                   "run_id": claimed["run_id"], "expected_execution_run_id": "execbranch"})
        self.assertEqual(committed["outcome"], "committed")
        self.call("advance-task", {"task_id": task["id"], "run_id": claimed["run_id"],
                                   "target": "in-review", "expected_current": "in-progress"})
        self.call("report", {"request_id": monitor["request_id"], "run_id": claimed["run_id"], "outcome": "succeeded"})
        review_claim = self.claim("review", "reviewbranch")
        job = self.read_job(review_claim)
        root = Path(review_claim["context"]["review_root"])
        self.assertEqual((root / "feature.py").read_text(), (execution / "feature.py").read_text())
        self.assertEqual(job["review_branch_snapshot"]["head_sha"], committed["sha"])
        self.assertIn(delivery["branch"], job["instruction"])
        self.assertIn(delivery["base_sha"] + ".." + committed["sha"], job["instruction"])
        self.assertIn("feature.py", Path(job["materials"]["changes.diff"]).read_text())
        self.assertEqual(self.native("heartbeat", {**self.identity(review_claim), "progress": "只读固定分支"})["ok"], True)

    def test_needs_input_draft_and_disabled_inflight_settles(self):
        self.plan()
        claim = self.claim()
        self.configure(enabled=False)
        self.assertEqual(self.claim(op="new")["reason"], "disabled")
        recovered = self.claim()
        self.assertEqual(recovered["claim_token"], claim["claim_token"])
        result = self.submit_plan(claim, status="needs_input", plan_markdown="# 草稿", questions=["整数范围？"])
        self.assertEqual((result["request_status"], result["task_status"]), ("failed", "needs_user"))
        self.assertEqual((Path(claim["context"]["job_path"]).parent / "plan-draft.md").read_text(), "# 草稿")

    def test_structured_question_answer_continues_in_fresh_desktop_claim(self):
        from test_plan_answers import QUESTION
        task, request_id = self.plan()
        claim = self.claim()
        self.submit_plan(claim, status="needs_input", plan_markdown="# 原草稿", questions=[QUESTION])
        item = self.call("get-task", {"task_id": task["id"]})["task"]["plan_clarification"]
        self.assertEqual(item["questions"], [QUESTION])
        response = self.call("submit-plan-answers", {"task_id": task["id"], "clarification_id": item["id"],
                             "answers": [{"question_id": QUESTION["id"], "option_id": "theme"}], "operation_id": "answer-1"})
        next_claim = self.claim(op="next-plan")
        self.assertEqual(next_claim["request_id"], response["request_id"])
        self.assertNotEqual(next_claim["run_id"], claim["run_id"])
        job = self.read_job(next_claim)
        self.assertIn("用户已回答的规划问题", job["instruction"])
        self.assertIn("theme", job["instruction"])
        self.submit_plan(next_claim)
        view = self.call("get-task", {"task_id": task["id"]})["task"]
        self.assertEqual(view["status"], "ready")
        self.assertTrue(view["extra"]["plan_execution_hold"])

    def test_native_skill_and_process_evidence_without_cli_credentials(self):
        self.plan(method="spec_plan")
        with patch("kanpilot.codex_planner.resolve_provider", create=True, side_effect=AssertionError("CLI provider")):
            claim = self.claim()
        self.assertIn("$spec-planner:spec-plan", claim["instructions"])
        job = self.read_job(claim)
        self.assertEqual((job["pid"], job["ppid"]), (os.getpid(), os.getppid()))
        self.assertEqual(job["session_id"], "native-thread")
        self.assertEqual(job["config"], desktop.MODEL_SOURCE)
        self.assertNotIn(claim["claim_token"], json.dumps(job))

    def test_claim_response_loss_and_concurrent_operation_do_not_take_next_request(self):
        _task, expected = self.plan()
        self.plan("2")
        # Repeated same operation recovers the original receipt; checkpoint
        # preparation can return busy while another process is preparing it.
        first = self.claim()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: desktop._tool_result("kp_desktop_claim", {"role": "plan", "operation_id": "op1"}, self.db_path), range(4)))
        successful = [value["structuredContent"] for value in values if not value["isError"]]
        self.assertTrue(successful)
        self.assertTrue(all(value["request_id"] == expected and value["claim_token"] == first["claim_token"] for value in successful))
        self.assertEqual(self.native("claim", {"role": "review", "operation_id": "op1"}, error=True)["code"], "conflict")
        self.assertEqual(self.claim(op="fresh")["reason"], "stage_busy")
        key = next((self.db_path.parent / ".desktop-queue").glob("*/claim.key"))
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)

    def test_wrong_token_run_role_and_body_token_never_write_or_leak(self):
        self.plan()
        claim = self.claim()
        result = {"status": "ready", "plan_markdown": "# 规划"}
        for override in ({"claim_token": "foreign-token"}, {"run_id": "different-run"}):
            response = self.native("submit_plan", {**self.identity(claim), **override, "result": result}, error=True)
            self.assertNotIn(override.get("claim_token", "<not-token>"), json.dumps(response))
        self.assertEqual(self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)["code"], "forbidden")
        self.assertEqual(self.native("submit_plan", {**self.identity(claim), "result": {**result, "plan_markdown": claim["claim_token"]}}, error=True)["code"], "bad_request")
        self.assertEqual(self.native("heartbeat", {**self.identity(claim), "progress": claim["claim_token"]}, error=True)["code"], "bad_request")
        self.assertEqual(self.call("get-request", {"request_id": claim["request_id"]})["request"]["status"], "claimed")

    def test_expired_archive_cancel_and_changed_source_deny_submit(self):
        self.call("set-config", {"worktree_pool_size": 4})
        for suffix, sql, value in (
            ("expired", "UPDATE requests SET lease_expires_at=? WHERE request_id=?", util.to_iso(util.now_dt() - timedelta(seconds=1))),
            ("archive", "UPDATE tasks SET save_state=? WHERE task_id=?", "archived"),
            ("cancel", "UPDATE requests SET status=? WHERE request_id=?", "cancelled"),
            ("source", "UPDATE tasks SET source_sha256=? WHERE task_id=?", "changed-source"),
        ):
            task, request_id = self.plan(suffix)
            claim = self.claim(op=suffix)
            self.mutate(sql, (value, request_id if "requests" in sql else task["id"]))
            result = self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}}, error=True)
            self.assertIn(result["code"], ("conflict", "stale_run"))
            self.mutate("UPDATE requests SET status='cancelled' WHERE request_id=?", (request_id,))

    def test_frozen_plan_input_tampering_denied(self):
        self.plan()
        claim = self.claim()
        (Path(claim["context"]["input_dir"]) / "supplement.md").write_text("变更后的材料")
        self.assertEqual(self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}}, error=True)["code"], "conflict")

    def test_plan_publish_commits_then_loses_response_retry_reads_same_result(self):
        self.plan()
        claim = self.claim()
        publish = desktop.cmd_exec.cmd_publish_plan
        def lost(ctx, payload):
            publish(ctx, payload)
            raise util.KanpilotError("response_lost", "fixture response lost")
        with patch.object(desktop.cmd_exec, "cmd_publish_plan", lost), \
             patch.dict(cli.COMMANDS, {"publish-plan": (cli.COMMANDS["publish-plan"][0], lost)}):
            self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}}, error=True)
        result = self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}})
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["request_status"], "succeeded")

    def test_status_configuration_is_local_and_does_not_disclose_token(self):
        self.plan()
        claim = self.claim()
        with patch.object(remote, "get_client", side_effect=AssertionError("status used external service")):
            value = self.configure(poll_minutes=15, auto_configured=True, planner_thread_id="plan-thread", review_thread_id="review-thread", automation_ids=["a", "b"])
            self.assertEqual(value["queue"], {"plan": 0, "review": 0})
            self.assertEqual(value["queue_details"]["plan"]["claimed"], 1)
            self.assertTrue(value["auto_configured"])
            self.assertNotIn(claim["claim_token"], json.dumps(value))
            self.assertNotIn("token_hash", json.dumps(value))

    def test_all_review_conclusions_reuse_core_and_exact_retry(self):
        # Each Desktop plan and review consumer keeps its unknown lease.
        self.call("set-config", {"worktree_pool_size": 6})
        for suffix, conclusion in enumerate(("pass", "changes_requested", "blocked")):
            task, request_id = self.make_review(str(suffix))
            claim = self.claim("review", "review-" + str(suffix))
            self.assertEqual(claim["request_id"], request_id)
            self.assertTrue(Path(claim["context"]["review_root"]).is_dir())
            result = self.review_result(conclusion)
            reply = self.native("submit_review", {**self.identity(claim), "result": result})
            self.assertEqual(reply["request_status"], "failed" if conclusion == "blocked" else "succeeded")
            self.assertTrue(Path(reply["report_path"]).is_file())
            job = json.loads(Path(claim["context"]["job_path"]).read_text())
            with closing(db.connect(self.db_path)) as conn:
                slot = conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?",
                                    (job["pool_lease"]["slot_id"],)).fetchone()
                self.assertEqual(slot["state"], "leased")
                self.assertEqual(slot["owner_state"], "unknown")
            before = len(self.fake.requests_log)
            self.assertTrue(self.native("submit_review", {**self.identity(claim), "result": result})["deduplicated"])
            self.assertEqual(before, len(self.fake.requests_log))
            if conclusion == "pass":
                self.assertEqual(self.fake.tasks[task["id"]]["status"], "done")
            elif conclusion == "changes_requested":
                current = self.call("get-task", {"task_id": task["id"]})["task"]
                self.assertTrue(current["review_summary"]["fix_task_id"])
            else:
                self.assertEqual(reply["task_status"], "needs_user")
            self.assertEqual(self.return_slot(claim)["state"], "free")

    def test_automated_changes_requested_keeps_same_task(self):
        task, _request = self.make_review(automated=True)
        claim = self.claim("review")
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("changes_requested")})
        self.assertEqual(reply["request_status"], "succeeded")
        current = self.call("get-task", {"task_id": task["id"]})["task"]
        self.assertEqual(current["status"], "needs_plan")
        self.assertEqual(current["extra"]["fix_request"]["state"], "open")

    def test_review_new_execution_plan_or_material_changes_stop_before_remote(self):
        # Five plan and five review consumers intentionally retain their leases.
        self.call("set-config", {"worktree_pool_size": 10})
        for suffix, mutation in enumerate(("run", "plan", "material", "snapshot", "execution")):
            task, request_id = self.make_review(str(suffix))
            claim = self.claim("review", "review-" + str(suffix))
            if mutation == "run":
                self.mutate("UPDATE tasks SET run_id=? WHERE task_id=?", ("new-exec", task["id"]))
            elif mutation == "plan":
                with closing(db.connect(self.db_path)) as conn:
                    Path(db.require_task(conn, task["id"])["plan_path"]).write_text("changed")
            elif mutation == "material":
                Path(claim["context"]["materials"]["changes.diff"]).write_text("changed")
            elif mutation == "snapshot":
                (Path(claim["context"]["review_root"]) / "sample.py").write_text("changed snapshot")
            else:
                (self.repos_root / "Feedback" / "sample.py").write_text("changed execution")
            before = len(self.fake.requests_log)
            failure = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
            self.assertIn(failure["code"], ("conflict", "stale_run"))
            self.assertEqual(before, len(self.fake.requests_log))
            self.mutate("UPDATE requests SET status='cancelled' WHERE request_id=?", (request_id,))
            self.mutate("UPDATE tasks SET save_state='archived' WHERE task_id=?", (task["id"],))

    def test_review_missing_execution_evidence_blocks_instead_of_reviewing_main(self):
        task, request_id = self.make_review()
        with closing(db.connect(self.db_path)) as conn:
            directory = headless_adapter.resolve_source(conn).run_dir(task["id"], "exec-1")
        for path in directory.iterdir():
            path.unlink()
        failure = self.native("claim", {"role": "review", "operation_id": "review"}, error=True)
        self.assertEqual(failure["code"], "review_blocked")
        self.assertEqual(self.call("get-request", {"request_id": request_id})["request"]["status"], "failed")

    def test_concurrent_submit_is_busy_and_never_reports_other_submit_failed(self):
        self.plan()
        claim = self.claim()
        identity = self.identity(claim)
        result = {"status": "ready", "plan_markdown": "# 正式规划"}
        entered, release = threading.Event(), threading.Event()
        entry = cli.COMMANDS["publish-plan"]
        def slow(ctx, payload):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return entry[1](ctx, payload)
        with patch.dict(cli.COMMANDS, {"publish-plan": (entry[0], slow)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(desktop._tool_result, "kp_desktop_submit_plan", {**identity, "result": result}, self.db_path)
                self.assertTrue(entered.wait(timeout=5))
                second = desktop._tool_result("kp_desktop_submit_plan", {**identity, "result": result}, self.db_path)
                self.assertTrue(second["isError"])
                self.assertEqual(json.loads(second["content"][0]["text"])["code"], "busy")
                self.assertEqual(self.call("get-request", {"request_id": claim["request_id"]})["request"]["status"], "claimed")
                release.set()
                self.assertFalse(first.result(timeout=5)["isError"])

    def test_review_response_loss_after_each_committed_core_stage_recovers(self):
        self.call("set-config", {"worktree_pool_size": 8})
        for suffix, command, conclusion in (("register", "register-review", "pass"),
                                            ("fix", "create-review-fix", "changes_requested"),
                                            ("advance", "advance-task", "pass"),
                                            ("report", "report", "pass")):
            self.make_review(suffix)
            claim = self.claim("review", suffix)
            entry = cli.COMMANDS[command]
            def lost(ctx, payload, original=entry[1]):
                original(ctx, payload)
                raise util.KanpilotError("response_lost", "isolated committed response lost")
            with patch.dict(cli.COMMANDS, {command: (entry[0], lost)}):
                reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result(conclusion)})
            self.assertEqual(reply["request_status"], "succeeded")
            writes_before = len([log for log in self.fake.requests_log if log[0] in ("POST", "PUT", "PATCH")])
            self.assertTrue(self.native("submit_review", {**self.identity(claim), "result": self.review_result(conclusion)})["deduplicated"])
            self.assertEqual(writes_before, len([log for log in self.fake.requests_log if log[0] in ("POST", "PUT", "PATCH")]))

    def test_review_crash_before_unknown_remote_phase_never_replays_write(self):
        self.make_review()
        claim = self.claim("review")
        result = self.review_result("pass")
        entry = cli.COMMANDS["advance-task"]
        def crashed(_ctx, _payload):
            raise RuntimeError("isolated process crash before remote outcome")
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], crashed)}):
            with self.assertRaises(RuntimeError):
                desktop._tool_result("kp_desktop_submit_review", {**self.identity(claim), "result": result}, self.db_path)
        before = len(self.fake.requests_log)
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], lambda *_: self.fail("unknown phase replayed"))}):
            reply = self.native("submit_review", {**self.identity(claim), "result": result})
        self.assertEqual((reply["request_status"], reply["task_status"]), ("failed", "needs_user"))
        self.assertEqual(before, len(self.fake.requests_log))
        self.assertIn("结果未知", self.read_job(claim)["detail"])

    def test_review_crash_after_advance_reconciles_locally_without_second_remote_write(self):
        self.make_review()
        claim = self.claim("review")
        result = self.review_result("pass")
        entry = cli.COMMANDS["advance-task"]
        def crashed(ctx, payload):
            entry[1](ctx, payload)
            raise RuntimeError("isolated process crash after commit")
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], crashed)}):
            with self.assertRaises(RuntimeError):
                desktop._tool_result("kp_desktop_submit_review", {**self.identity(claim), "result": result}, self.db_path)
        before = len(self.fake.requests_log)
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], lambda *_: self.fail("committed advance replayed"))}):
            reply = self.native("submit_review", {**self.identity(claim), "result": result})
        self.assertEqual((reply["request_status"], reply["task_status"]), ("succeeded", "done"))
        self.assertEqual(before, len(self.fake.requests_log))

    def test_valid_fail_only_ends_own_claim_and_expired_heartbeat_is_not_renewed(self):
        _task, request_id = self.plan()
        _other, next_request = self.plan("other")
        claim = self.claim()
        response = self.native("fail", {**self.identity(claim), "reason": "缺少只读权限"})
        self.assertEqual(response["request_status"], "failed")
        self.assertEqual(self.call("get-request", {"request_id": next_request})["request"]["status"], "queued")
        next_claim = self.claim(op="other")
        expired = util.to_iso(util.now_dt() - timedelta(seconds=1))
        self.mutate("UPDATE requests SET lease_expires_at=? WHERE request_id=?", (expired, next_request))
        self.assertEqual(self.native("heartbeat", self.identity(next_claim), error=True)["code"], "stale_run")
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(db.get_request(conn, next_request)["lease_expires_at"], expired)

    def test_review_untracked_body_is_frozen_and_same_name_content_change_denied(self):
        self.make_review()
        untracked = self.repos_root / "Feedback" / "new.py"
        untracked.write_text("def added():\n    return 42\n")
        claim = self.claim("review")
        manifest = json.loads(Path(claim["context"]["materials"]["untracked-manifest.json"]).read_text())
        self.assertEqual(manifest["files"][0]["repository_path"], "new.py")
        target = Path(manifest["files"][0]["material_path"])
        self.assertEqual(target.read_bytes(), untracked.read_bytes())
        self.assertNotEqual(target, untracked)
        snapshot = Path(claim["context"]["review_root"])
        self.assertEqual((snapshot / "new.py").read_bytes(), target.read_bytes())
        untracked.write_text("def added():\n    return -1\n")
        before = len(self.fake.requests_log)
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
        self.assertEqual(reply["code"], "conflict")
        self.assertEqual(before, len(self.fake.requests_log))

    def test_review_untracked_symlink_is_blocked_without_copying_target(self):
        task, request_id = self.make_review()
        outside = self.root / "outside-secret.txt"
        outside.write_text("not a review input")
        (self.repos_root / "Feedback" / "linked.txt").symlink_to(outside)
        reply = self.native("claim", {"role": "review", "operation_id": "review"}, error=True)
        self.assertEqual(reply["code"], "review_blocked")
        self.assertIn("symlink", reply["error"])
        self.assertNotIn(outside.read_text(), json.dumps(reply))
        self.assertEqual(self.call("get-request", {"request_id": request_id})["request"]["status"], "failed")

    def test_review_untracked_executable_mode_change_denies_submit(self):
        self.make_review()
        added = self.repos_root / "Feedback" / "new.py"
        added.write_text("print('isolated fixture')\n")
        added.chmod(0o644)
        claim = self.claim("review")
        added.chmod(0o755)
        before = len(self.fake.requests_log)
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
        self.assertEqual(reply["code"], "conflict")
        self.assertEqual(before, len(self.fake.requests_log))

    def test_tracked_binary_bytes_change_invalidates_review_identity(self):
        self.make_review()
        repository = self.repos_root / "Feedback"
        binary = repository / "data.bin"
        binary.write_bytes(b"\x00initial\x01")
        subprocess.run(["git", "-C", str(repository), "add", "data.bin"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-m", "isolated binary baseline"], check=True, capture_output=True)
        binary.write_bytes(b"\x00first-change\x01")
        claim = self.claim("review")
        self.assertIn("GIT binary patch", Path(claim["context"]["materials"]["changes.diff"]).read_text())
        self.assertEqual((Path(claim["context"]["review_root"]) / "data.bin").read_bytes(), binary.read_bytes())
        binary.write_bytes(b"\x00second-change\x01")
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
        self.assertEqual(reply["code"], "conflict")

    def test_review_snapshot_matches_combined_staged_and_unstaged_materials(self):
        self.make_review()
        repository = self.repos_root / "Feedback"
        tracked = repository / "sample.py"
        tracked.write_text("def square(n):\n    return n ** 2\n")
        subprocess.run(["git", "-C", str(repository), "add", "sample.py"], check=True, capture_output=True)
        tracked.write_text("def square(n):\n    if not isinstance(n, int):\n        raise ValueError('integer required')\n    return n ** 2\n")
        added = repository / "new test.py"
        added.write_text("assert square(4) == 16\n")
        before_status = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
        frozen_diff = subprocess.check_output(["git", "-C", str(repository), "diff", "HEAD", "--binary", "--full-index", "--no-color"])
        claim = self.claim("review")
        root = Path(claim["context"]["review_root"])
        self.assertEqual((root / "sample.py").read_bytes(), tracked.read_bytes())
        self.assertEqual((root / added.name).read_bytes(), added.read_bytes())
        self.assertEqual(Path(claim["context"]["materials"]["changes.diff"]).read_bytes(), frozen_diff)
        self.assertEqual(subprocess.check_output(["git", "-C", str(root), "diff", "HEAD", "--binary", "--full-index", "--no-color"]), frozen_diff)
        self.assertEqual(subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"]), before_status)
        self.assertEqual(self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")})["request_status"], "succeeded")

    def test_review_source_change_during_preparation_never_exposes_a_claim(self):
        task, request_id = self.make_review()
        repository = self.repos_root / "Feedback"
        tracked = repository / "sample.py"
        tracked.write_text("def square(n):\n    return n ** 2\n")
        prepare = pi_desktop_review.prepare_pooled_worktree
        def changed_after_snapshot(*args, **kwargs):
            result = prepare(*args, **kwargs)
            tracked.write_text("def square(n):\n    return -1\n")
            return result
        with patch.object(pi_desktop_review, "prepare_pooled_worktree", changed_after_snapshot):
            failure = self.native("claim", {"role": "review", "operation_id": "review"}, error=True)
        self.assertEqual(failure["code"], "review_blocked")
        self.assertEqual(self.call("get-request", {"request_id": request_id})["request"]["status"], "failed")
        self.assertEqual(tracked.read_text(), "def square(n):\n    return -1\n")
        with closing(db.connect(self.db_path)) as conn:
            slot = conn.execute("SELECT * FROM worktree_slots WHERE holder_stage='review'").fetchone()
            self.assertEqual(slot["state"], "free")

    def test_plan_change_after_preparation_releases_unexposed_slot(self):
        task, request_id = self.plan()
        prepare = desktop._prepare
        def change_after_prepare(*args, **kwargs):
            job = prepare(*args, **kwargs)
            branch = Path(job["context"]["payload"]["plan_cwd"])
            (branch / "sample.py").write_text("changed after preparation\n")
            return job
        with patch.object(desktop, "_prepare", change_after_prepare):
            failure = self.native("claim", {"role": "plan", "operation_id": "changed-plan"}, error=True)
        self.assertEqual(failure["code"], "worktree_invalid")
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(db.get_request(conn, request_id)["status"], "failed")
            self.assertEqual(conn.execute("SELECT state FROM worktree_slots").fetchone()["state"], "free")

    def assert_plan_ownership_lost_before_delivery_releases_slot(self, takeover):
        task, request_id = self.plan()
        prepare = desktop._prepare
        def lose_after_prepare(ctx, *args, **kwargs):
            job = prepare(ctx, *args, **kwargs)
            if takeover:
                self.mutate("UPDATE requests SET run_id=? WHERE request_id=?", ("replacement-run", request_id))
            else:
                self.mutate("UPDATE requests SET lease_expires_at=? WHERE request_id=?",
                            (util.to_iso(util.now_dt() - timedelta(seconds=1)), request_id))
            return job
        before = len(self.fake.requests_log)
        with patch.object(desktop, "_prepare", lose_after_prepare):
            failure = self.native("claim", {"role": "plan", "operation_id": "ownership-lost"}, error=True)
        self.assertIn(failure["code"], ("stale_run", "forbidden"))
        with closing(db.connect(self.db_path)) as conn:
            request = db.get_request(conn, request_id)
            self.assertEqual(request["status"], "claimed")
            self.assertIsNone(request["result_json"])
            if takeover:
                self.assertEqual(request["run_id"], "replacement-run")
            self.assertEqual(conn.execute("SELECT state FROM worktree_slots").fetchone()["state"], "free")
        self.assertEqual(before, len(self.fake.requests_log))

    def test_expired_plan_before_delivery_releases_unexposed_slot(self):
        self.assert_plan_ownership_lost_before_delivery_releases_slot(False)

    def test_reclaimed_plan_before_delivery_releases_unexposed_slot(self):
        self.assert_plan_ownership_lost_before_delivery_releases_slot(True)

    def test_full_plan_pool_replays_waiting_receipt_without_stale_run(self):
        task, request_id = self.plan()
        with closing(db.connect(self.db_path)) as conn:
            payload = db.request_payload(db.get_request(conn, request_id))
            repository = db.require_task(conn, task["id"])["repository"]
            with db.write_txn(conn):
                db.set_setting(conn, "worktree_pool_size", 1)
            worktree_pool.acquire(conn, repository, stage="plan", holder_id="other-reader", sha=payload["delivery_head_sha"])
        first = self.claim(op="full-pool")
        self.assertEqual(first, {"claimed": False, "reason": "worktree_pool_waiting", "request_id": request_id})
        self.assertEqual(self.claim(op="full-pool"), first)
        with closing(db.connect(self.db_path)) as conn:
            request = db.get_request(conn, request_id)
            self.assertEqual(request["status"], "queued")
            self.assertIsNone(request["run_id"])
            self.assertEqual(db.require_task(conn, task["id"])["status"], "needs_plan")

    def test_manual_root_and_materials_share_frozen_bytes_and_collection_detects_source_race(self):
        task, _request = self.make_review()
        repository = self.repos_root / "Feedback"
        tracked = repository / "sample.py"
        tracked.write_text("def square(n):\n    return n ** 2\n")
        added = repository / "new.py"
        added.write_text("assert square(4) == 16\n")
        frozen = review_snapshot.freeze(repository)
        root = self.root / "direct-review"
        with closing(db.connect(self.db_path)) as conn:
            summary = dict(db.require_task(conn, task["id"]))
            root.parent.mkdir(parents=True, exist_ok=True)
            subprocess.check_call(["git", "-C", str(repository), "worktree", "add", "--detach", str(root), frozen["head"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            review_snapshot.populate(root, frozen)
            self.assertIsNone(pi_desktop_review.prepare_worktree(summary, repository, root, snapshot=frozen))
            write = review_snapshot.write_materials
            def source_changes_after_frozen_materials(directory, snapshot):
                self.assertIs(snapshot, frozen)
                files = write(directory, snapshot)
                tracked.write_text("def square(n):\n    return -1\n")
                added.write_text("assert square(4) == -1\n")
                return files
            materials = self.root / "direct-materials"
            with patch.object(review_snapshot, "write_materials", source_changes_after_frozen_materials):
                with self.assertRaises(util.KanpilotError) as failure:
                    pi_desktop_review.collect_materials(
                        db_path=self.db_path, conn=conn, materials=materials, task=summary,
                        execution_worktree=repository, execution_run_id="exec-1", snapshot=frozen)
            self.assertEqual(failure.exception.code, "review_blocked")
        self.assertEqual((root / "sample.py").read_text(), "def square(n):\n    return n ** 2\n")
        self.assertEqual((materials / "changes.diff").read_bytes(), frozen["diff"])
        self.assertEqual((root / "new.py").read_bytes(), (materials / "untracked" / "new.py").read_bytes())
        self.assertNotEqual((root / "new.py").read_bytes(), added.read_bytes())

    def test_crash_after_plan_file_write_recovers_fixed_file_without_regenerating(self):
        self.plan()
        claim = self.claim()
        result = {"status": "ready", "plan_markdown": "# 固定计划文件"}
        write = desktop.planner_runner.write_plan_file
        def crashed(*args, **kwargs):
            write(*args, **kwargs)
            raise RuntimeError("isolated process crash after file write")
        with patch.object(desktop.planner_runner, "write_plan_file", crashed):
            with self.assertRaises(RuntimeError):
                desktop._tool_result("kp_desktop_submit_plan", {**self.identity(claim), "result": result}, self.db_path)
        job = self.read_job(claim)
        target = Path(job["plan_path"])
        self.assertTrue(target.is_file())
        before = target.read_bytes(), target.stat().st_mtime_ns
        with patch.object(desktop.planner_runner, "write_plan_file", side_effect=AssertionError("fixed file regenerated")):
            reply = self.native("submit_plan", {**self.identity(claim), "result": result})
        self.assertEqual(reply["request_status"], "succeeded")
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mtime_ns))
        self.assertEqual(len(list(target.parent.glob("*.md"))), 1)

    def test_stdio_database_failures_keep_transport_alive(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kp_desktop_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kp_desktop_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        for failure in (sqlite3.OperationalError("database is locked"),
                        util.KanpilotError("migration_needed", "数据库需要显式升级")):
            with self.subTest(failure=type(failure).__name__):
                stdin = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")
                output = io.StringIO()
                with patch.object(desktop.sys, "stdin", stdin), patch.object(desktop.sys, "stdout", output), \
                        patch.object(cli, "Context", side_effect=[failure, cli.Context(self.db_path)]):
                    self.assertEqual(desktop.run_stdio(self.db_path), 0)
                responses = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([response["id"] for response in responses], [1, 2, 3])
                self.assertTrue(responses[0]["result"]["isError"])
                error = json.loads(responses[0]["result"]["content"][0]["text"])
                self.assertEqual(error["code"], "database_unavailable" if isinstance(failure, sqlite3.Error) else "migration_needed")
                self.assertFalse(responses[1]["result"]["isError"])
                self.assertEqual(responses[2]["result"], {})

    def test_tool_database_query_failure_is_reported_and_context_closed(self):
        ctx = cli.Context(self.db_path)
        with patch.object(cli, "Context", return_value=ctx), \
                patch.object(desktop, "cmd_desktop_queue_status", side_effect=sqlite3.OperationalError("private database details")), \
                patch.object(ctx, "close", wraps=ctx.close) as close:
            response = desktop._tool_result("kp_desktop_status", {}, self.db_path)
        self.assertTrue(response["isError"])
        self.assertNotIn("private database details", json.dumps(response))
        close.assert_called_once()

    def test_stdio_namespace_schema_boundary_and_stdout(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "kp_desktop_claim", "arguments": {"role": "plan", "operation_id": "notification"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kp_dispatch", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kp_desktop_claim", "arguments": {"role": "plan", "operation_id": "op", "repository": "/arbitrary"}}},
        ]
        stdin = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n" + "x" * (desktop.MAX_MESSAGE_BYTES + 10) + "\n")
        output = io.StringIO()
        with patch.object(desktop.sys, "stdin", stdin), patch.object(desktop.sys, "stdout", output):
            self.assertEqual(desktop.run_stdio(self.db_path), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 5)
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"kp_desktop_" + name for name in ("status", "claim", "heartbeat", "submit_plan", "submit_review", "return_slot", "fail")})
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertEqual(responses[4]["error"]["message"], "Message too large")
