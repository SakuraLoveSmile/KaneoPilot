import json
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, cmd_delivery, core, db, git_delivery, harness_plus_adapter, round_store, util
from test_git_delivery import make_repo, branch, commit, git


class DeliveryCommit(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.root / "delivery-repo")
        self.fixture = self.make_ready_task(task_id="task1234")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.delivery = branch(self.repo, self.root / "worktrees")
        with db.write_txn(self.ctx.conn):
            task = db.require_task(self.ctx.conn, "task1234")
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso,
                           repository=str(self.repo), status="running", stage="monitor", run_id="exec1")
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, **self.delivery, mode="pr", commits=[])
            core.update_automation(self.ctx.conn, task, self.ctx.now_iso, fix_round=0)
            request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "monitor", "monitor:test", task_id="task1234")
            db.update_request(self.ctx.conn, request["request_id"], self.ctx.now_iso,
                              status="claimed", run_id="monitor1", lease_expires_at=util.plus_seconds(3600, self.ctx.now))
        self.payload = {"task_id": "task1234", "request_id": request["request_id"],
                        "run_id": "monitor1", "expected_execution_run_id": "exec1"}
        from pathlib import Path
        self.wt = Path(self.delivery["worktree_path"])
        self.primary = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))
        self.addCleanup(lambda: self.assertEqual(self.primary, (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))))
        for name, response in (
            ("plus_status", {"session_id": "delivery-session", "is_running": False,
                             "current_turn_id": "delivery-turn", "pending_permission": None}),
            ("plus_turn_get", {"session_id": "delivery-session", "turn_id": "delivery-turn",
                               "status": "completed"}),
        ):
            mocked = patch.object(harness_plus_adapter, name, return_value=response)
            mocked.start()
            self.addCleanup(mocked.stop)

    def call_commit(self, payload=None):
        original = git_delivery._git
        def checked(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction, "Git IO in write transaction")
            return original(*args, **kwargs)
        with patch.object(git_delivery, "_git", side_effect=checked):
            return cmd_delivery.cmd_delivery_commit(self.ctx, payload or self.payload)

    def live_verdict(self):
        task = db.require_task(self.ctx.conn, "task1234")
        self.call("set-project-mapping", {"project_id": task["project_id"], "repository": str(self.repo), "enabled": True,
                                         "expected_revision": self.call("get-project-mappings")["revision"]})
        self.call("set-project-engine-mode", {"project_id": task["project_id"], "engine_mode": "live"})
        plan = self.write_plan(task["project_name"], self.fixture["remote"],
            {"repository": str(self.repo), "source_sha256": task["source_sha256"],
             "requirements_sha256": task["requirements_sha256"]},
            body="# Frozen\n```verify\ntrue\n```\n")
        sha = util.sha256_file(plan)
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, plan_sha256=sha)
        db.set_extra(self.ctx.conn, db.require_task(self.ctx.conn, "task1234"), self.ctx.now_iso,
                     plan_verify={"plan_sha256": sha, "commands": ["true"]})
        config = {"connection_file": str(self.root / "fake-plus.json")}
        round_store.upsert_on_claim(self.ctx.conn, "task1234", "exec1", config,
                                    self.ctx.now_iso, backend="pi_desktop")
        round_store.mark_started(self.ctx.conn, "task1234", "exec1", self.ctx.now_iso, worktree_path=str(self.wt))
        round_store.upsert_discovered(self.ctx.conn, "task1234", "exec1", self.ctx.now_iso,
                                     backend="pi_desktop", session_id="delivery-session", run_status="finished")
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso,
                       session_id="delivery-session", sent=1, send_state="sent")
        directory = self.runs_root / "task1234" / "exec1"
        directory.mkdir(parents=True)
        job = {"task_id": "task1234", "run_id": "exec1", "backend": "pi_desktop",
               "config": config, "status": "finished", "exit_code": 0,
               "session_id": "delivery-session", "turn_id": "delivery-turn", "send_state": "sent",
               "plan_sha256": sha, "completion_policy": "command_verification",
               "verification_evidence_version": 1,
               "verification_source_sha256": git_delivery.source_state_sha256(self.wt)}
        spec = dict(job, plan_path=str(plan), repository=str(self.repo), cwd=str(self.wt), runs_dir=str(directory))
        for name, data in (("job.json", job), ("spec.json", spec)):
            (directory / name).write_text(json.dumps(data))
        events = [{"seq": 1, "type": "session", "session_id": "delivery-session", "turn_id": "delivery-turn"},
                  {"seq": 2, "type": "command_output", "turn_id": "delivery-turn", "name": "exec",
                   "input": json.dumps({"command": "true", "cwd": str(self.wt)}), "exit_code": 0, "output": "passed"},
                  {"seq": 3, "type": "terminal", "outcome": "completed"}]
        (directory / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        result = self.call("engine-verdict", dict(self.payload, expected_plan_sha256=sha))
        self.assertTrue(result["verification_ok"], result)
        return result

    def test_live_source_change_after_verdict_never_commits(self):
        (self.wt / "change").write_text("verified")
        self.live_verdict()
        (self.wt / "change").write_text("unverified")
        result = self.call_commit()
        self.assertEqual(result["failure_code"], "delivery_commit_failed")
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.delivery["head_sha"])

    def test_live_staging_race_never_commits(self):
        (self.wt / "change").write_text("verified")
        self.live_verdict()
        original = git_delivery._git
        def changed(args, *a, **kw):
            result = original(args, *a, **kw)
            if args[:2] == ["add", "-A"]:
                (self.wt / "change").write_text("unverified while staging")
            return result
        with patch.object(git_delivery, "_git", side_effect=changed):
            result = self.call_commit()
        self.assertEqual(result["failure_code"], "delivery_commit_failed")
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.delivery["head_sha"])

    def test_live_crash_after_git_commit_recovers_without_model_rerun(self):
        (self.wt / "change").write_text("verified")
        receipt = self.live_verdict()
        result = git_delivery.commit_worktree(dict(self.delivery, repository=str(self.repo)), task_id="task1234", title="x",
            execution_run_id="exec1", fix_round=0, verified_source_sha256=receipt["binding"]["source_sha256"],
            verified_index_sha256=receipt["index_source_sha256"])
        recovered = self.call("engine-verdict", dict(self.payload, expected_plan_sha256=receipt["plan_sha256"], operation_id="recovery-monitor"))
        self.assertTrue(recovered["verification_ok"])
        self.assertTrue(recovered["deduplicated"])
        self.assertEqual(self.call_commit()["sha"], result["sha"])
        self.assertEqual(len(core.delivery_of(db.require_task(self.ctx.conn, "task1234"))["commits"]), 1)

    def test_live_late_monitor_reuses_verified_core_commit(self):
        (self.wt / "change").write_text("verified")
        receipt = self.live_verdict()
        self.call_commit()
        result = self.call("engine-verdict", dict(self.payload, expected_plan_sha256=receipt["plan_sha256"], operation_id="late-monitor"))
        self.assertTrue(result["verification_ok"])
        self.assertTrue(result["deduplicated"])

    def test_live_crlf_git_normalization_is_accepted(self):
        (self.wt / ".gitattributes").write_text("*.txt text eol=lf\n")
        (self.wt / "crlf.txt").write_bytes(b"verified\r\n")
        self.live_verdict()
        self.assertEqual(self.call_commit()["outcome"], "committed")

    def test_live_hook_changed_tree_keeps_local_commit_unregistered(self):
        (self.wt / "change").write_text("verified")
        self.live_verdict()
        from pathlib import Path
        hook = Path(git(self.repo, "rev-parse", "--git-path", "hooks"))
        if not hook.is_absolute():
            hook = self.repo / hook
        hook = hook / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf unsafe > hook-source.txt\ngit add hook-source.txt\n")
        hook.chmod(0o755)
        result = self.call_commit()
        self.assertEqual(result["failure_code"], "delivery_commit_failed")
        self.assertIn("未登记", result["message"])
        self.assertNotEqual(git(self.wt, "rev-parse", "HEAD"), self.delivery["head_sha"])
        self.assertEqual(core.delivery_of(db.require_task(self.ctx.conn, "task1234"))["commits"], [])

    def test_live_concurrent_index_change_is_preserved(self):
        (self.wt / "change").write_text("verified")
        self.live_verdict()
        original = git_delivery._git
        def changed(args, *a, **kw):
            result = original(args, *a, **kw)
            if args[:1] == ["commit"]:
                blob = original(["hash-object", "-w", "--stdin"], self.wt, 10, input=b"user staged bytes").stdout.decode().strip()
                original(["update-index", "--add", "--cacheinfo", "100644," + blob + ",change"], self.wt, 10)
            return result
        with patch.object(git_delivery, "_git", side_effect=changed):
            result = self.call_commit()
        self.assertEqual(result["failure_code"], "delivery_commit_failed")
        self.assertEqual(git(self.wt, "show", ":change"), "user staged bytes")
        self.assertEqual(core.delivery_of(db.require_task(self.ctx.conn, "task1234"))["commits"], [])

    def test_commit_idempotent_and_event(self):
        (self.wt / "change").touch()
        result = self.call_commit()
        self.assertEqual(result["outcome"], "committed")
        second = self.call_commit()
        self.assertEqual(second, {"outcome": "already_committed", "sha": result["sha"]})
        task = db.require_task(self.ctx.conn, "task1234")
        self.assertEqual(core.delivery_of(task)["head_sha"], result["sha"])
        self.assertEqual(len(core.delivery_of(task)["commits"]), 1)
        self.assertEqual(self.ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind='delivery_committed'").fetchone()[0], 1)

    def test_crash_recovery(self):
        (self.wt / "change").touch()
        result = commit(self.delivery, run="exec1")
        self.assertEqual(self.call_commit()["sha"], result["sha"])

    def test_head_drift_blocks(self):
        (self.wt / "change").touch()
        commit(self.delivery, run="other-run")
        result = self.call_commit()
        self.assertEqual(result["failure_code"], "delivery_commit_failed")
        self.assertIn("分支被执行端或他人改动", result["message"])
        self.assertEqual(db.require_task(self.ctx.conn, "task1234")["status"], "needs_user")

    def test_no_changes_blocks(self):
        result = self.call_commit()
        self.assertEqual(result["failure_code"], "no_changes")
        self.assertEqual(db.require_task(self.ctx.conn, "task1234")["status"], "needs_user")

    def test_secret_blocks(self):
        (self.wt / ".env").touch()
        self.assertEqual(self.call_commit()["files"], [".env"])

    def test_identity_required_and_stage_checked(self):
        for update, expected in (({"expected_execution_run_id": "old"}, "stale_run"),
                                 ({"run_id": "old"}, "stale_run"),
                                 ({"request_id": ""}, "bad_request")):
            with self.assertRaises(util.KanpilotError) as caught:
                self.call_commit(dict(self.payload, **update))
            self.assertEqual(caught.exception.code, expected)
        with db.write_txn(self.ctx.conn):
            db.update_request(self.ctx.conn, self.payload["request_id"], self.ctx.now_iso, stage="review")
        with self.assertRaises(util.KanpilotError) as caught:
            self.call_commit()
        self.assertEqual(caught.exception.code, "validation_failed")

    def test_concurrent_delivery_change_not_overwritten(self):
        (self.wt / "change").touch()
        original = git_delivery.commit_worktree
        def concurrent(*args, **kwargs):
            result = original(*args, **kwargs)
            with db.write_txn(self.ctx.conn):
                core.update_delivery(self.ctx.conn, db.require_task(self.ctx.conn, "task1234"),
                                     self.ctx.now_iso, head_sha="changed-by-another-command")
            return result
        with patch.object(git_delivery, "commit_worktree", side_effect=concurrent):
            with self.assertRaises(util.KanpilotError) as caught:
                self.call_commit()
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(core.delivery_of(db.require_task(self.ctx.conn, "task1234"))["head_sha"], "changed-by-another-command")

    def test_lease_loss_during_commit_not_registered(self):
        (self.wt / "change").touch()
        original = git_delivery.commit_worktree
        def expired(*args, **kwargs):
            result = original(*args, **kwargs)
            with db.write_txn(self.ctx.conn):
                db.update_request(self.ctx.conn, self.payload["request_id"], self.ctx.now_iso,
                                  lease_expires_at=util.plus_seconds(-1, self.ctx.now))
            return result
        with patch.object(git_delivery, "commit_worktree", side_effect=expired):
            with self.assertRaises(util.KanpilotError) as caught:
                self.call_commit()
        self.assertEqual(caught.exception.code, "stale_run")
        self.assertEqual(core.delivery_of(db.require_task(self.ctx.conn, "task1234"))["commits"], [])
