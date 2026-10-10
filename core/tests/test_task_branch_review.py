"""Local-only task branch/Review fixtures: real Git, isolated DB, no models."""
import subprocess
from pathlib import Path
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, cmd_delivery, cmd_exec, cmd_queue, cmd_remote, core, db, git_delivery, util
from kanpilot import pi_desktop_review as review


class TaskBranchReview(KanpilotTestCase):
    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, env=git_delivery.git_env()).stdout.decode().strip()

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "--initial-branch=main")
        self.git(self.repo, "config", "user.name", "Isolated Review Fixture")
        self.git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "app.py").write_text("baseline\n")
        self.git(self.repo, "add", "app.py")
        self.git(self.repo, "commit", "-qm", "baseline")
        base = self.git(self.repo, "rev-parse", "HEAD")
        self.worktree = self.root / "trees" / "task1234" / "execution"
        self.git(self.repo, "worktree", "add", "-b", "kaneo/task1234", str(self.worktree), base)
        # The primary checkout must remain dirty and untouched.
        (self.repo / "user-work.txt").write_text("preserve primary checkout")
        self.before_primary = (self.git(self.repo, "rev-parse", "HEAD"),
                               self.git(self.repo, "status", "--porcelain"))
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.addCleanup(self.assert_primary_preserved)
        self.ctx.conn.execute(
            "INSERT INTO tasks(task_id,project_id,project_name,repository,title,status,stage,"
            "run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("task1234", "fixture-project", "Fixture", str(self.repo), "New feature",
             "running", "monitor", "exec1", self.ctx.now_iso, self.ctx.now_iso))
        self.delivery = dict(mode="manual", workspace_version=1, branch="kaneo/task1234",
                             worktree_path=str(self.worktree), base_branch="main",
                             base_sha=base, head_sha=base, commits=[])
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=self.delivery)
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "monitor", "fixture-monitor",
                                       task_id="task1234")
        db.update_request(self.ctx.conn, request["request_id"], self.ctx.now_iso,
                          status="claimed", run_id="monitor1", claimed_by="kaneo-monitor",
                          lease_expires_at=util.plus_seconds(3600, self.ctx.now))
        self.payload = dict(task_id="task1234", request_id=request["request_id"], run_id="monitor1",
                            expected_execution_run_id="exec1")

    def task(self):
        return db.require_task(self.ctx.conn, "task1234")

    def assert_primary_preserved(self):
        self.assertEqual(self.before_primary, (self.git(self.repo, "rev-parse", "HEAD"),
                                              self.git(self.repo, "status", "--porcelain")))

    def commit(self, filename="app.py", body="new feature\n"):
        (self.worktree / filename).write_text(body)
        return cmd_delivery.cmd_delivery_commit(self.ctx, self.payload)

    def claim_review(self):
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="in_review", stage="review")
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "review", "fixture-review",
                                       task_id="task1234")
        result = cmd_exec.cmd_claim(self.ctx, dict(stage="review", executor="kaneo-review",
                                                  request_id=request["request_id"]))
        self.assertTrue(result["claimed"])
        return dict(task_id="task1234", request_id=result["request_id"], run_id=result["run_id"],
                    expected_execution_run_id=self.task()["run_id"])

    def registration(self, identity):
        report = self.reports_root / "fixture-review.md"
        report.write_text("# Fixed branch review\nEvidence\n")
        return dict(identity, report_path=str(report), conclusion="pass")

    def test_local_commit_is_idempotent_and_never_changes_delivery_mode(self):
        original_commit = git_delivery.commit_worktree
        def commit_outside_txn(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return original_commit(*args, **kwargs)
        with patch.object(git_delivery, "commit_worktree", side_effect=commit_outside_txn), \
                patch.object(git_delivery, "push_branch", side_effect=AssertionError("must never push")):
            first = self.commit()
            self.assertEqual(first["outcome"], "committed")
            second = cmd_delivery.cmd_delivery_commit(self.ctx, self.payload)
        self.assertEqual(second, {"outcome": "already_committed", "sha": first["sha"]})
        delivery = core.delivery_of(self.task())
        self.assertEqual(delivery["mode"], "manual")
        self.assertEqual(len(delivery["commits"]), 1)
        self.assertEqual(self.git(self.worktree, "status", "--porcelain"), "")
        self.assertIn("KaneoPilot-Run: exec1", self.git(self.worktree, "log", "-1", "--format=%B"))

    def test_no_changes_and_unsafe_files_cannot_be_committed(self):
        self.assertEqual(cmd_delivery.cmd_delivery_commit(self.ctx, self.payload)["failure_code"], "no_changes")
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="running", **core.clear_block_fields())
        result = self.commit(".env", "API_KEY=fixture-secret")
        self.assertEqual(result["failure_code"], "delivery_blocked_files")
        self.assertEqual(self.git(self.worktree, "rev-parse", "HEAD"), self.delivery["base_sha"])
        self.assertEqual(self.git(self.worktree, "diff", "--cached"), "")

    def test_same_run_registration_during_commit_is_deduplicated(self):
        original = git_delivery.commit_worktree
        def already_registered(*args, **kwargs):
            result = original(*args, **kwargs)
            core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso,
                                 head_sha=result["sha"],
                                 commits=[{"execution_run_id": "exec1", "sha": result["sha"]}])
            return result
        with patch.object(git_delivery, "commit_worktree", side_effect=already_registered):
            result = self.commit()
        self.assertEqual(result["outcome"], "already_committed")
        self.assertEqual(len(core.delivery_of(self.task())["commits"]), 1)

    def test_review_cannot_claim_without_current_execution_commit(self):
        with self.assertRaises(util.KanpilotError) as caught:
            self.claim_review()
        self.assertEqual(caught.exception.code, "validation_failed")

    def test_advance_and_enqueue_cannot_bypass_core_commit(self):
        with self.assertRaises(util.KanpilotError):
            cmd_remote.cmd_advance_task(self.ctx, dict(task_id="task1234", target="in-review",
                                                     expected_current="in-progress"))
        self.assertEqual(self.task()["status"], "running")
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="in_review", stage="review")
        with self.assertRaises(util.KanpilotError):
            cmd_queue.cmd_enqueue(self.ctx, dict(task_id="task1234", kind="review"))
        self.assertFalse(self.fake.requests_log)

    def test_default_pi_claim_receives_fixed_root_and_full_branch_materials(self):
        self.commit()
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, run_id="exec2")
        self.payload["expected_execution_run_id"] = "exec2"
        result = self.commit("test.py", "assert True\n")
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="in_review", stage="review")
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "review", "default-pi-review", task_id="task1234")
        original_verify = git_delivery.verify_branch_worktree
        def verify_outside_txn(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return original_verify(*args, **kwargs)
        with patch.object(git_delivery, "verify_branch_worktree", side_effect=verify_outside_txn):
            claim = cmd_exec.cmd_claim(self.ctx, dict(stage="review", executor="kaneo-review",
                                                     request_id=request["request_id"]))
        self.assertTrue(claim["claimed"])
        context = claim["context"]
        self.assertEqual(self.git(context["review_root"], "rev-parse", "HEAD"), result["sha"])
        self.assertEqual(self.git(context["review_root"], "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(context["review_branch_snapshot"]["base_sha"], self.delivery["base_sha"])
        full = Path(context["materials"]["changes.diff"]).read_text()
        self.assertIn("new feature", full)
        self.assertIn("assert True", full)
        self.assertEqual(context["payload"]["pi_branch_review"]["execution_run_id"], "exec2")

    def test_default_pi_prepare_failure_reports_blocked_without_live_claim(self):
        self.commit()
        (self.worktree / "dirty.txt").write_text("later uncommitted work")
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="in_review", stage="review")
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "review", "default-pi-review", task_id="task1234")
        result = cmd_exec.cmd_claim(self.ctx, dict(stage="review", executor="kaneo-review",
                                                  request_id=request["request_id"]))
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "review_blocked")
        failed = db.get_request(self.ctx.conn, request["request_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(util.jload(failed["result_json"])["outcome"], "needs_user")
        self.assertEqual(self.task()["failure_code"], "review_blocked")
        self.assertIsNone(self.task()["review_report_path"])

    def test_default_pi_material_drift_rejects_core_registration(self):
        self.commit()
        identity = self.claim_review()
        prepared = db.request_payload(db.get_request(self.ctx.conn, identity["request_id"]))["pi_branch_review"]
        Path(prepared["materials"]["changes.diff"]).write_text("tampered review material")
        with self.assertRaises(util.KanpilotError):
            cmd_exec.cmd_register_review(self.ctx, self.registration(identity))
        self.assertIsNone(self.task()["review_report_path"])

    def test_fixed_full_range_includes_original_change_and_fix_increment(self):
        first = self.commit()
        previous_report = self.reports_root / "previous.md"
        previous_report.write_text("Previous review")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                     review_history=[dict(head_sha=first["sha"], report_path=str(previous_report))])
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, run_id="exec2")
        self.payload["expected_execution_run_id"] = "exec2"
        second = self.commit("test.py", "assert True\n")
        task = dict(self.task())
        root = self.root / "trees" / "task1234" / ("review-" + second["sha"][:12])
        self.assertIsNone(review.prepare_worktree(task, self.worktree, root))
        self.assertEqual(self.git(root, "rev-parse", "HEAD"), second["sha"])
        self.assertEqual(self.git(root, "status", "--porcelain"), "")
        materials = review.collect_materials(db_path=self.db_path, conn=self.ctx.conn,
            materials=self.root / "materials", task=task,
            execution_worktree=self.worktree, execution_run_id="exec2")
        full = Path(materials["files"]["changes.diff"]).read_text()
        increment = Path(materials["files"]["fix-increment.diff"]).read_text()
        self.assertIn("new feature", full)
        self.assertIn("assert True", full)
        self.assertNotIn("new feature", increment)
        instruction = review.review_instruction("Review", job=dict(execution_run_id="exec2"),
                                               review_root=root, materials=materials, facts=dict(task=task))
        self.assertIn("kaneo/task1234", instruction)
        self.assertIn(self.delivery["base_sha"] + ".." + second["sha"], instruction)
        self.assertIn("不能替代完整范围", instruction)

    def test_registered_pass_is_bound_to_claimed_branch_version(self):
        self.commit()
        identity = self.claim_review()
        request = db.get_request(self.ctx.conn, identity["request_id"])
        self.assertEqual(db.request_payload(request)["review_branch_snapshot"], core.branch_review_snapshot(self.task()))
        original_verify = git_delivery.verify_branch_worktree
        def verify_outside_txn(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return original_verify(*args, **kwargs)
        with patch.object(git_delivery, "verify_branch_worktree", side_effect=verify_outside_txn):
            self.assertTrue(cmd_exec.cmd_register_review(self.ctx, self.registration(identity))["ok"])

    def test_branch_metadata_head_and_dirty_drift_reject_registration(self):
        self.commit()
        identity = self.claim_review()
        payload = self.registration(identity)
        original = core.delivery_of(self.task())
        for key in ("branch", "base_sha", "head_sha", "worktree_path", "workspace_version", "mode"):
            with self.subTest(key=key):
                core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, **{key: "changed"})
                with self.assertRaises(util.KanpilotError):
                    cmd_exec.cmd_register_review(self.ctx, payload)
                db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=original)
        (self.worktree / "app.py").write_text("later edit\n")
        with self.assertRaises(util.KanpilotError):
            cmd_exec.cmd_register_review(self.ctx, payload)
        self.assertIsNone(self.task()["review_report_path"])
        self.assertNotEqual(self.git(self.worktree, "status", "--porcelain"), "")

    def test_unrecorded_local_commit_drift_cannot_register_or_dedupe(self):
        self.commit()
        identity = self.claim_review()
        (self.worktree / "app.py").write_text("unrecorded future commit\n")
        self.git(self.worktree, "add", "app.py")
        self.git(self.worktree, "commit", "-qm", "unexpected future commit")
        self.assertEqual(self.git(self.worktree, "status", "--porcelain"), "")
        with self.assertRaises(util.KanpilotError):
            cmd_exec.cmd_register_review(self.ctx, self.registration(identity))
        with self.assertRaises(util.KanpilotError):
            cmd_delivery.cmd_delivery_commit(self.ctx, self.payload)
        self.assertIsNone(self.task()["review_report_path"])

    def test_snapshot_mutation_and_material_mutation_block_before_settle(self):
        result = self.commit()
        identity = self.claim_review()
        task = dict(self.task())
        root = self.root / "trees" / "task1234" / ("review-" + result["sha"][:12])
        self.assertIsNone(review.prepare_worktree(task, self.worktree, root))
        materials = review.collect_materials(db_path=self.db_path, conn=self.ctx.conn,
            materials=self.root / "materials", task=task,
            execution_worktree=self.worktree, execution_run_id="exec1")
        job = dict(identity, execution_run_id="exec1", review_root=str(root))
        review._freeze_branch_job(job, task, materials)
        review._guard_branch_job(job, task)
        material = Path(materials["files"]["changes.diff"])
        original = material.read_bytes()
        material.write_text("changed")
        with self.assertRaises(util.KanpilotError):
            review._guard_branch_job(job, task)
        material.write_bytes(original)
        (root / "app.py").write_text("snapshot mutated")
        with self.assertRaises(util.KanpilotError):
            review._guard_branch_job(job, task)

    def test_historical_unmarked_manual_tasks_keep_uncommitted_snapshot(self):
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery={"mode": "manual"})
        (self.worktree / "app.py").write_text("uncommitted historic work")
        (self.worktree / "untracked.txt").write_text("historical untracked work")
        root = self.root / "trees" / "task1234" / "review-legacy"
        self.assertFalse(core.requires_branch_review(self.task()))
        self.assertIsNone(review.prepare_worktree(dict(self.task()), self.worktree, root))
        self.assertEqual((root / "app.py").read_text(), "uncommitted historic work")
        self.assertEqual((root / "untracked.txt").read_text(), "historical untracked work")

    def test_historical_claimed_pr_restores_original_fixed_materials(self):
        result = self.commit()
        core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, mode="pr", workspace_version=None)
        task = dict(self.task())
        root = self.root / "trees" / "task1234" / ("review-" + result["sha"][:12])
        self.assertIsNone(review.prepare_worktree(task, self.worktree, root))
        materials = review.collect_materials(db_path=self.db_path, conn=self.ctx.conn,
            materials=self.root / "materials", task=task,
            execution_worktree=self.worktree, execution_run_id="exec1")
        job = dict(execution_run_id="exec1", review_root=str(root), materials=materials["files"])
        review._restore_legacy_pr_job(job, task)
        self.assertEqual(job["review_branch_snapshot"]["head_sha"], result["sha"])
        Path(materials["files"]["changes.diff"]).write_text("untrusted replacement")
        with self.assertRaises(util.KanpilotError):
            review._restore_legacy_pr_job(dict(execution_run_id="exec1", review_root=str(root),
                                             materials=materials["files"]), task)

    def test_historical_claimed_pr_can_register_without_new_snapshot_field(self):
        self.commit()
        core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, mode="pr", workspace_version=None)
        identity = self.claim_review()
        request = db.get_request(self.ctx.conn, identity["request_id"])
        old_payload = db.request_payload(request)
        old_payload.pop("review_branch_snapshot")
        db.update_request(self.ctx.conn, identity["request_id"], self.ctx.now_iso, payload_json=old_payload)
        self.assertTrue(cmd_exec.cmd_register_review(self.ctx, self.registration(identity))["ok"])
