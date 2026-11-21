import subprocess
from pathlib import Path

from helpers import KanpilotTestCase
from kanpilot import cli, core, db, derive, followup_closeout, util


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, timeout=30).stdout.decode().strip()


class FollowupCloseout(KanpilotTestCase):
    """KP-MG06：二级任务的人工确认收口。"""

    task_id = "taskcloseout0000000001"

    def setUp(self):
        super().setUp()
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.repo = Path(self.repos_root) / "Demo"
        self.repo.mkdir(parents=True, exist_ok=True)
        remote = Path(self.repos_root) / "demo-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-b", "kaneo/demo-task")
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "fix")
        self.head_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "main")
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-u", "origin", "main")
        # The reviewed change is already on the target branch (another PR).
        git(self.repo, "merge", "--ff-only", "kaneo/demo-task")
        git(self.repo, "push", "origin", "main")
        self.worktree = Path(self.repos_root) / "demo-worktree"
        git(self.repo, "worktree", "add", str(self.worktree), "kaneo/demo-task")

        self.make_ready_task(task_id=self.task_id, project_name="Feedback")
        now = util.now_iso()
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, self.task_id, now,
                           repository=str(self.repo),
                           review_source_task_id="source0000000000000001",
                           status="in_review", stage="review")
            core.update_delivery(self.ctx.conn, db.require_task(self.ctx.conn, self.task_id), now,
                                 mode="pr", head_sha=self.head_sha, branch="kaneo/demo-task",
                                 worktree_path=str(self.worktree), base_branch="main",
                                 base_sha=self.base_sha)

    def payload(self, **overrides):
        value = {"task_id": self.task_id, "expected_head_sha": self.head_sha, "confirm": True}
        value.update(overrides)
        return value

    def test_advanced_branch_head_closes_out_when_target_matches(self):
        # 收尾提交让分支头前进（真实案例：PR 里最后那个竞态修复），目标分支内容一致即可收口。
        (self.worktree / "a.txt").write_text("two\npolish\n", encoding="utf-8")
        git(self.worktree, "commit", "-am", "polish")
        advanced = git(self.worktree, "rev-parse", "HEAD")
        (self.repo / "a.txt").write_text("two\npolish\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "polish on target")
        git(self.repo, "push", "origin", "main")
        result = followup_closeout.cmd_confirm_followup_closeout(
            self.ctx, self.payload(expected_head_sha=advanced, note="PR #12 已合入"))
        self.assertEqual(result["status"], "done")
        row = db.require_task(self.ctx.conn, self.task_id)
        self.assertEqual(db.task_extra(row)["followup_closeout"]["head_sha"], advanced)

    def test_identity_mismatch_is_refused(self):
        with self.assertRaises(util.KanpilotError) as caught:
            followup_closeout.cmd_confirm_followup_closeout(
                self.ctx, self.payload(expected_head_sha="0" * 40))
        self.assertIn("分支头", str(caught.exception))
        self.assertEqual(db.require_task(self.ctx.conn, self.task_id)["status"], "in_review")

    def test_candidate_and_allowed_action(self):
        task = db.require_task(self.ctx.conn, self.task_id)
        value = followup_closeout.candidate(self.ctx.conn, task)
        self.assertIsNotNone(value)
        self.assertEqual(value["head_sha"], self.head_sha)
        actions = derive.allowed_actions(self.ctx.conn, task, util.now_dt())
        self.assertIn("confirm_closeout", actions)

    def test_confirm_closes_the_task_with_a_verified_receipt(self):
        result = followup_closeout.cmd_confirm_followup_closeout(
            self.ctx, self.payload(note="PR #12 已合入 main"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "done")
        row = db.require_task(self.ctx.conn, self.task_id)
        self.assertEqual(row["status"], "done")
        receipt = db.task_extra(row)["followup_closeout"]
        self.assertEqual(receipt["head_sha"], self.head_sha)
        self.assertEqual(receipt["base_branch"], "main")
        self.assertEqual(receipt["reviewed_paths"], ["a.txt"])
        self.assertEqual(receipt["note"], "PR #12 已合入 main")
        self.assertTrue(followup_closeout.settled(row))
        # 收口后不再提供该动作，也不能重复收口。
        self.assertIsNone(followup_closeout.candidate(self.ctx.conn, row))
        with self.assertRaises(util.KanpilotError):
            followup_closeout.cmd_confirm_followup_closeout(self.ctx, self.payload())

    def test_drifted_target_branch_blocks_the_closeout(self):
        (self.repo / "a.txt").write_text("three\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "later change")
        git(self.repo, "push", "origin", "main")
        with self.assertRaises(util.KanpilotError) as caught:
            followup_closeout.cmd_confirm_followup_closeout(self.ctx, self.payload())
        self.assertIn("不一致", str(caught.exception))
        self.assertEqual(db.require_task(self.ctx.conn, self.task_id)["status"], "in_review")

    def test_plain_task_without_followup_link_is_not_offered(self):
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, self.task_id, util.now_iso(),
                           review_source_task_id=None)
        task = db.require_task(self.ctx.conn, self.task_id)
        self.assertIsNone(followup_closeout.candidate(self.ctx.conn, task))
        actions = derive.allowed_actions(self.ctx.conn, task, util.now_dt())
        self.assertNotIn("confirm_closeout", actions)
