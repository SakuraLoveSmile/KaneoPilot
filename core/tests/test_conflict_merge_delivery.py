"""Real conflict resolution/commit recovery in disposable bare-repo worktrees."""
from pathlib import Path
from unittest.mock import patch

import pytest

from test_task_ai_action import ConflictActionFixture
from test_git_delivery import git
from kanpilot import core, db, git_delivery as gd, task_ai_action as action, util


class ConflictMergeDeliveryTest(ConflictActionFixture):
    def prepared(self):
        self.start()
        action.ensure_worktree(self.conn, self.task())
        return dict(core.delivery_of(self.task()), repository=str(self.repo))

    def resolve(self):
        (self.worktree / "tracked.txt").write_text("resolved: task implementation plus new base implementation\n")
        git(self.worktree, "add", "tracked.txt")
        return dict(verified_source_sha256=gd.source_state_sha256(self.worktree),
                    verified_index_sha256=gd.expected_index_source_sha256(self.worktree))

    def commit_merge(self, delivery, **kwargs):
        return gd.commit_worktree(delivery, task_id=self.task_id, title="解决合并冲突", execution_run_id="action-run-2",
                                  fix_round=2, merge_parent_sha=self.base, **kwargs)

    def test_resolved_commit_has_exact_double_parents_and_replays_without_new_commit(self):
        delivery = self.prepared()
        verified = self.resolve()
        result = self.commit_merge(delivery, **verified)
        assert git(self.worktree, "show", "-s", "--format=%P", "HEAD").split() == [self.head, self.base]
        assert result["files"] == ["tracked.txt"]
        assert git(self.worktree, "status", "--porcelain") == ""
        recovered = self.commit_merge(delivery, **verified)
        assert recovered == result
        assert git(self.worktree, "rev-list", "--count", "HEAD", "^" + self.head, "^" + self.base) == "1"

    def test_no_verification_unresolved_or_wrong_orig_head_cannot_commit(self):
        delivery = self.prepared()
        with pytest.raises(gd.GitDeliveryError, match="完整验证树"):
            self.commit_merge(delivery)
        verified = self.resolve()
        orig = Path(git(self.worktree, "rev-parse", "--git-path", "ORIG_HEAD"))
        if not orig.is_absolute():
            orig = self.worktree / orig
        orig.write_text(self.original_base + "\n")
        with pytest.raises(gd.GitDeliveryError, match="不属于本次"):
            self.commit_merge(delivery, **verified)
        assert git(self.worktree, "rev-parse", "HEAD") == self.head

    def test_source_changed_after_verification_preserves_uncommitted_merge(self):
        delivery = self.prepared()
        verified = self.resolve()
        (self.worktree / "tracked.txt").write_text("unverified change")
        with pytest.raises(gd.GitDeliveryError, match="未经核心验证"):
            self.commit_merge(delivery, **verified)
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == self.base

    def test_merge_requires_all_verified_source_staged(self):
        delivery = self.prepared()
        self.resolve()
        (self.worktree / "tracked.txt").write_text("resolved with a necessary unstaged fix\n")
        verified = dict(verified_source_sha256=gd.source_state_sha256(self.worktree),
                        verified_index_sha256=gd.expected_index_source_sha256(self.worktree))
        with pytest.raises(gd.GitDeliveryError, match="完整暂存"):
            self.commit_merge(delivery, **verified)
        assert git(self.worktree, "rev-parse", "HEAD") == self.head

    def test_unresolved_entries_are_not_committable(self):
        delivery = self.prepared()
        with pytest.raises(gd.GitDeliveryError, match="尚未全部解决"):
            self.commit_merge(delivery, verified_source_sha256=gd.source_state_sha256(self.worktree), verified_index_sha256="f" * 64)

    def test_recovery_requires_original_head_marker(self):
        delivery = self.prepared()
        verified = self.resolve()
        self.commit_merge(delivery, **verified)
        orig = Path(git(self.worktree, "rev-parse", "--git-path", "ORIG_HEAD"))
        orig.write_text(self.original_base + "\n")
        with pytest.raises(gd.GitDeliveryError, match="恢复状态不符"):
            self.commit_merge(delivery, **verified)

    def test_recovery_rejects_wrong_tree_or_trailers(self):
        delivery = self.prepared()
        verified = self.resolve()
        self.commit_merge(delivery, **verified)
        with pytest.raises(gd.GitDeliveryError, match="验证回执不符"):
            self.commit_merge(delivery, **dict(verified, verified_index_sha256="0" * 64))
        git(self.worktree, "commit", "--amend", "-m", "unexpected model commit")
        with pytest.raises(gd.GitDeliveryError, match="他人改动"):
            self.commit_merge(delivery, **verified)

    def test_remote_head_or_base_drift_rejected_before_merge(self):
        self.start()
        (self.repo / "tracked.txt").write_text("base moved again\n")
        git(self.repo, "add", "tracked.txt"); git(self.repo, "commit", "-m", "drift")
        git(self.repo, "push", "origin", "main")
        # The primary change is deliberate fixture input; preserve it thereafter.
        self.primary = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))
        with pytest.raises(util.KanpilotError, match="分支已经变化"):
            action.ensure_worktree(self.conn, self.task())
        assert git(self.worktree, "rev-parse", "HEAD") == self.head
        assert git(self.worktree, "status", "--porcelain") == ""

    def test_failed_receipt_does_not_allow_second_merge_attempt(self):
        self.start()
        real = gd.prepare_conflict_merge
        with patch.object(gd, "prepare_conflict_merge", side_effect=gd.GitDeliveryError("worktree_invalid", "interrupted")):
            with pytest.raises(util.KanpilotError):
                action.ensure_worktree(self.conn, self.task())
        with patch.object(gd, "prepare_conflict_merge", wraps=real) as probe:
            with pytest.raises(util.KanpilotError, match="不属于本次"):
                action.ensure_worktree(self.conn, self.task())
        assert probe.call_args.kwargs["resume"] is True
        assert git(self.worktree, "status", "--porcelain") == ""
