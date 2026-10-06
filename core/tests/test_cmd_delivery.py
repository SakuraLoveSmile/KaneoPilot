from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, cmd_delivery, core, db, git_delivery, util
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

    def call_commit(self, payload=None):
        original = git_delivery._git
        def checked(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction, "Git IO in write transaction")
            return original(*args, **kwargs)
        with patch.object(git_delivery, "_git", side_effect=checked):
            return cmd_delivery.cmd_delivery_commit(self.ctx, payload or self.payload)

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
