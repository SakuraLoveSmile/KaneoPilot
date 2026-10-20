"""User-only recovery keeps the real unfinished merge and the executor's results."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_task_ai_action as fixtures
import test_task_ai_action_lifecycle as lifecycle
from test_git_delivery import git
from kanpilot import (cmd_dispatch, cmd_queue, core, db, git_delivery, harness_plus_adapter,
                      mcp_client, repair_prompt, task_ai_action, util)


class TaskAIActionVerificationRecovery(fixtures.ConflictActionFixture):
    claim_action = lifecycle.TaskAIActionLifecycle.claim_action
    prepare_merge = lifecycle.TaskAIActionLifecycle.prepare_merge
    finish_files_and_claim_monitor = lifecycle.TaskAIActionLifecycle.finish_files_and_claim_monitor
    goal_evidence = lifecycle.TaskAIActionLifecycle.goal_evidence

    def blocked(self):
        payload = self.finish_files_and_claim_monitor()
        result = self.goal_evidence(payload, checks=[], expected_ok=False)
        assert not result["verification_ok"]
        self.call("report", dict(request_id=payload["request_id"], run_id=payload["run_id"],
                                 outcome="needs_user", failure_code="verification_unconfirmed"))
        turn = {"sessionId": "session", "turnId": "action-turn", "status": "completed"}
        mock = patch.object(harness_plus_adapter, "_get_client",
                            return_value=(SimpleNamespace(turn_get=lambda *a: dict(turn)), {}))
        mock.start(); self.addCleanup(mock.stop)
        self.retry = dict(task_id=self.task_id, action="retry_verification", expected_execution_run_id=self.run)
        self.original = core.review_repair_of(self.task())
        self.index = git(self.worktree, "ls-files", "--stage")
        self.contents = (self.worktree / "tracked.txt").read_bytes()
        self.old_run = self.run

    def reject(self, payload=None):
        before, requests = dict(self.task()), self.conn.execute("SELECT count(*) FROM requests").fetchone()[0]
        with self.assertRaises(util.KanpilotError):
            self.call("resolve", payload or self.retry)
        assert dict(self.task()) == before
        assert self.conn.execute("SELECT count(*) FROM requests").fetchone()[0] == requests

    def test_missing_checks_recovery_claim_reuses_merge_results_and_frozen_config(self):
        self.blocked()
        assert cmd_queue.verification_retry_candidate(self.conn, self.task())
        result = self.call("resolve", self.retry)
        assert result["task_status"] == "ready" and result["request"]["kind"] == "dispatch"
        fix = core.review_repair_of(self.task())
        assert fix["verification_recovery"]["source_run_id"] == self.old_run
        assert fix["action_id"] == self.original["action_id"]
        assert fix["source_execution_run_id"] == self.original["source_execution_run_id"]
        assert fix["action_config"] == self.original["action_config"]
        self.reject()
        service = Mock(); service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch"))
        assert claimed["claimed"], claimed
        assert claimed["run_id"] != self.old_run
        with patch("kanpilot.git_delivery.prepare_conflict_merge", side_effect=AssertionError("must not merge again")):
            path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"], self.conn)
        assert path == self.worktree and mode == "branch"
        assert git(path, "rev-parse", "MERGE_HEAD") == self.base
        assert git(path, "rev-parse", "ORIG_HEAD") == self.head
        assert git(path, "ls-files", "--stage") == self.index
        assert (path / "tracked.txt").read_bytes() == self.contents
        row = self.conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
                                (self.task_id, claimed["run_id"])).fetchone()
        assert util.jload(row["config_json"]) == self.original["action_config"]
        spec = dict(task_id=self.task_id, repository=self.task()["repository"], cwd=str(path),
                    run_id=claimed["run_id"], plan_sha256=self.task()["plan_sha256"], plan_path=self.task()["plan_path"])
        spec.update(repair_prompt.freeze(self.task()))
        assert "补齐" in repair_prompt.execution_body(spec)
        assert "不重新实施" in cmd_dispatch._execution_instruction(self.task(), path, desktop=True)
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='plan'").fetchone()[0] == self.before_plans
        task_ai_action.record_progress(self.conn, self.task(), self.now, "running", session_id="recovery-session")
        assert task_ai_action.projection(self.conn, self.task(), self.ctx.now)["history"][0]["execution_run_id"] == claimed["run_id"]

    def test_recovery_rejects_automation_old_run_and_running_plus(self):
        self.blocked()
        self.reject(dict(self.retry, actor="automation"))
        self.reject(dict(self.retry, expected_execution_run_id="exec1"))
        for action in ("resume", "retry", "replan"):
            self.reject(dict(self.retry, action=action))
        with patch.object(harness_plus_adapter, "plus_status", return_value={"is_running": True}):
            self.reject()

    def test_recovery_rejects_changed_merge_identity_and_committed_merge(self):
        self.blocked()
        marker = self.worktree / git(self.worktree, "rev-parse", "--git-path", "MERGE_HEAD")
        marker.write_text(self.head + "\n")
        assert not cmd_queue.verification_retry_candidate(self.conn, self.task())
        self.reject()
        marker.write_text(self.base + "\n")
        git(self.worktree, "commit", "-m", "merge already committed")
        assert not cmd_queue.verification_retry_candidate(self.conn, self.task())
        self.reject()

    def test_dispatch_rejects_source_or_index_changes_after_confirmation(self):
        self.blocked()
        git(self.worktree, "update-index", "--cacheinfo", "100644," + git(self.repo, "rev-parse", self.base + ":tracked.txt") + ",tracked.txt")
        assert git(self.worktree, "status", "--porcelain").startswith("MM")
        self.call("resolve", self.retry)
        (self.worktree / "tracked.txt").write_text("changed after confirmation\n")
        with self.assertRaises(util.KanpilotError) as caught:
            cmd_dispatch._ensure_worktree(self.task(), self.task_id, "not-started", self.conn)
        assert "源码、暂存区或合并状态已变化" in caught.exception.message
        (self.worktree / "tracked.txt").write_bytes(self.contents)
        git(self.worktree, "update-index", "--cacheinfo", "100644," + git(self.repo, "rev-parse", self.original_base + ":tracked.txt") + ",tracked.txt")
        assert git(self.worktree, "status", "--porcelain").startswith("MM")
        retry = core.execution_retry_of(self.task())
        assert git_delivery.worktree_state_sha256(retry["worktree"]) == retry["worktree_sha256"]
        with self.assertRaises(util.KanpilotError) as caught:
            cmd_dispatch._ensure_worktree(self.task(), self.task_id, "not-started", self.conn)
        assert "源码、暂存区或合并状态已变化" in caught.exception.message

    def test_recovery_rejects_remote_branch_and_frozen_config_changes(self):
        self.blocked()
        git(self.bare, "update-ref", "refs/heads/main", self.head)
        self.reject()
        # Restore only the disposable bare remote ref, never the executor worktree.
        git(self.bare, "update-ref", "refs/heads/main", self.base)
        fix = dict(self.original)
        fix["action_config"] = dict(fix["action_config"], model_id="tampered-model")
        db.set_extra(self.conn, self.task(), self.now, fix_request=fix)
        assert not cmd_queue.verification_retry_candidate(self.conn, self.task())
        self.reject()

    def test_second_missing_report_stays_blocked_without_commit_review_or_automatic_retry(self):
        self.blocked()
        self.call("resolve", self.retry)
        service = Mock(); service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch"))
        assert claimed["claimed"], claimed
        self.run, self.dispatch = claimed["run_id"], claimed
        cmd_dispatch._ensure_worktree(self.task(), self.task_id, self.run, self.conn)
        self.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
                          (str(self.worktree), self.task_id, self.run))
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor",
                       sent=1, send_state="sent", session_id="session")
        self.call("report", dict(request_id=claimed["request_id"], run_id=self.run, outcome="succeeded"))
        monitor = self.call("claim", dict(stage="monitor", executor="kaneo-monitor"))
        assert monitor["claimed"], monitor
        payload = dict(task_id=self.task_id, request_id=monitor["request_id"], run_id=monitor["run_id"],
                       expected_execution_run_id=self.run)
        self.goal_evidence(payload, checks=[], expected_ok=False)
        assert self.call("delivery-commit", payload)["outcome"] == "blocked"
        self.call("report", dict(request_id=monitor["request_id"], run_id=monitor["run_id"],
                                 outcome="needs_user", failure_code="verification_unconfirmed"))
        assert self.task()["status"] == "needs_user"
        assert git(self.worktree, "rev-parse", "HEAD") == self.head
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == self.base
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind IN ('plan','review') AND status='queued'").fetchone()[0] == 0
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch' AND status='queued'").fetchone()[0] == 0

    def test_recovered_report_commits_original_merge_and_enqueues_new_review(self):
        self.blocked()
        self.call("resolve", self.retry)
        service = Mock(); service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch"))
        assert claimed["claimed"], claimed
        self.run = claimed["run_id"]
        cmd_dispatch._ensure_worktree(self.task(), self.task_id, self.run, self.conn)
        self.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
                          (str(self.worktree), self.task_id, self.run))
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor",
                       sent=1, send_state="sent", session_id="session")
        self.call("report", dict(request_id=claimed["request_id"], run_id=self.run, outcome="succeeded"))
        monitor = self.call("claim", dict(stage="monitor", executor="kaneo-monitor"))
        assert monitor["claimed"], monitor
        payload = dict(task_id=self.task_id, request_id=monitor["request_id"], run_id=monitor["run_id"],
                       expected_execution_run_id=self.run)
        self.goal_evidence(payload)
        assert self.call("delivery-commit", payload)["outcome"] == "committed"
        assert git(self.worktree, "show", "-s", "--format=%P", "HEAD").split() == [self.head, self.base]
        self.call("advance-task", dict(task_id=self.task_id, run_id=monitor["run_id"],
                                       target="in-review", expected_current="in-progress"))
        self.call("report", dict(request_id=monitor["request_id"], run_id=monitor["run_id"], outcome="succeeded"))
        assert core.review_repair_of(self.task())["state"] == "awaiting_review"
        assert db.task_extra(self.task()).get("review_conclusion") is None
        assert self.conn.execute("SELECT 1 FROM requests WHERE kind='review' AND status='queued'").fetchone()
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='plan'").fetchone()[0] == self.before_plans

    def test_claim_rejects_remote_base_drift_after_confirmation_without_starting_model(self):
        self.blocked()
        self.call("resolve", self.retry)
        git(self.bare, "update-ref", "refs/heads/main", self.head)
        service = Mock(); service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        rounds = self.conn.execute("SELECT count(*) FROM execution_rounds").fetchone()[0]
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service), \
                patch.object(harness_plus_adapter, "plus_session_create") as create:
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch"))
        assert not claimed["claimed"], claimed
        assert "来源或目标分支已经变化" in claimed["detail"], claimed
        create.assert_not_called()
        assert self.conn.execute("SELECT count(*) FROM execution_rounds").fetchone()[0] == rounds
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == self.base
        assert git(self.worktree, "rev-parse", "ORIG_HEAD") == self.head
        assert git(self.worktree, "ls-files", "--stage") == self.index
        assert (self.worktree / "tracked.txt").read_bytes() == self.contents

    def test_send_guard_rejects_remote_head_drift_after_claim_without_starting_model(self):
        self.blocked()
        self.call("resolve", self.retry)
        service = Mock(); service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", dict(stage="dispatch", executor="kaneo-dispatch"))
        assert claimed["claimed"], claimed
        git(self.bare, "update-ref", "refs/heads/" + self.branch, self.base)
        with patch.object(harness_plus_adapter, "plus_session_create") as create, \
                patch.object(git_delivery, "prepare_conflict_merge", side_effect=AssertionError("must not merge again")):
            with self.assertRaises(util.KanpilotError) as caught:
                cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"], self.conn)
        assert "来源或目标分支已经变化" in caught.exception.message
        create.assert_not_called()
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == self.base
        assert git(self.worktree, "rev-parse", "ORIG_HEAD") == self.head
        assert git(self.worktree, "ls-files", "--stage") == self.index
        assert (self.worktree / "tracked.txt").read_bytes() == self.contents

