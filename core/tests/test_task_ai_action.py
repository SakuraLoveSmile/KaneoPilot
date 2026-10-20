"""Task actions use disposable Git/SQLite, fake Kaneo and read-only fake GitHub."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import KanpilotTestCase
from test_git_delivery import git
from kanpilot import cli, core, db, exec_config, gh_client, git_delivery, task_ai_action as action, util


class ConflictActionFixture(KanpilotTestCase):
    task_id = "conflictaction0001"

    def setUp(self):
        super().setUp()
        self.fixture = self.make_ready_task(task_id=self.task_id)
        self.fake.tasks[self.task_id]["status"] = "in-review"
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.conn, self.now = self.ctx.conn, self.ctx.now_iso
        self.repo = self.repos_root / "Feedback"
        self.bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.bare)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(self.bare))
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "tracked.txt"); git(self.repo, "commit", "-m", "shared base")
        self.original_base = git(self.repo, "rev-parse", "HEAD")
        self.branch = "kaneo/" + self.task_id
        self.worktree = self.root / "trees" / self.task_id / "branch"
        self.worktree.parent.mkdir(parents=True)
        git(self.repo, "worktree", "add", "-b", self.branch, str(self.worktree), "HEAD")
        (self.worktree / "tracked.txt").write_text("task implementation\n")
        git(self.worktree, "add", "tracked.txt"); git(self.worktree, "commit", "-m", "task change")
        self.head = git(self.worktree, "rev-parse", "HEAD")
        (self.repo / "tracked.txt").write_text("new base implementation\n")
        git(self.repo, "add", "tracked.txt"); git(self.repo, "commit", "-m", "base changed")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", "main", self.branch)
        (self.repo / "primary-untracked.txt").write_text("must survive")
        self.primary = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))
        self.addCleanup(lambda: self.assertEqual(self.primary, (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))))
        plan = self.fixture["plan_path"]
        plan.write_text(plan.read_text() + "\n```verify\npython3 -c 'assert True'\n```\n")
        self.report = self.reports_root / "pass-review.md"; self.report.write_text("# Review\nPassed the original bounded implementation.\n")
        self.source_directory = self.runs_root / self.task_id / "exec1"
        self.source_directory.mkdir(parents=True)
        (self.source_directory / "job.json").write_text(util.jdump({"v": 1, "backend": "pi", "task_id": self.task_id,
            "run_id": "exec1", "status": "finished", "ended_at": self.now, "exit_code": 0, "pid": None}))
        (self.source_directory / "events.jsonl").write_text(util.jdump({"seq": 1, "type": "terminal", "outcome": "completed", "ts": self.now}) + "\n")
        self.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,worktree_path,config_json,created_at,updated_at) VALUES(?,?,'pi','finished',?,'{}',?,?)",
                          (self.task_id, "exec1", str(self.worktree), self.now, self.now))
        db.update_task(self.conn, self.task_id, self.now, status="in_review", stage="review", run_id="exec1",
                       sent=1, send_state="sent", plan_sha256=util.sha256_file(plan), review_key="review-pass-1",
                       review_report_path=str(self.report), review_report_sha256=util.sha256_file(self.report))
        core.update_delivery(self.conn, self.task(), self.now, mode="pr", branch=self.branch, worktree_path=str(self.worktree),
                             head_sha=self.head, base_branch="main", base_sha=self.original_base, pr_number=7,
                             commits=[{"execution_run_id": "exec1", "sha": self.head}])
        db.set_extra(self.conn, self.task(), self.now, review_conclusion="pass", review_history=[{
            "review_key": "review-pass-1", "conclusion": "pass", "execution_run_id": "exec1", "head_sha": self.head,
            "report_path": str(self.report), "report_sha256": util.sha256_file(self.report)}])
        db.set_setting(self.conn, "project_mappings", {"items": {self.task()["project_id"]: {
            "project_name": "Feedback", "repository": str(self.repo), "enabled": True}}})
        db.set_setting(self.conn, "exec_config", {"backend": "pi_desktop", "pi_desktop": {
            "model_source": "custom", "connection_file": str(self.root / "fake-plus.json"),
            "provider_id": "test-provider", "model_id": "test-model", "thinking_level": "high", "permission_mode": "auto"}})
        # Capability reads are simulated; all task/Git transitions remain real.
        available = patch.object(exec_config, "_probe_pi_desktop", return_value={"available": True})
        available.start(); self.addCleanup(available.stop)
        self.pr = {"number": 7, "url": "https://github.com/owner/repo/pull/7", "state": "OPEN", "isCrossRepository": False,
                   "headRefOid": self.head, "baseRefOid": self.base, "headRefName": self.branch, "baseRefName": "main", "mergeable": "CONFLICTING"}
        def remote_pr(*args, **kwargs):
            self.assertFalse(self.conn.in_transaction)
            return dict(self.pr)
        for module, name, kw in [(gh_client, "pr_view", {"side_effect": remote_pr}),
                                  (git_delivery, "origin_github_repo", {"return_value": "owner/repo"})]:
            mock = patch.object(module, name, **kw); mock.start(); self.addCleanup(mock.stop)

    def task(self):
        return db.require_task(self.conn, self.task_id)

    def prepare(self):
        return action.cmd_prepare_task_ai_action(self.ctx, {"task_id": self.task_id, "kind": action.KIND})["candidate"]

    def start(self, candidate=None):
        candidate = candidate or self.prepare()
        return action.cmd_start_task_ai_action(self.ctx, {"task_id": self.task_id, "kind": action.KIND, "confirm": True,
            "expected_context_sha256": candidate["context_sha256"], "expected_config_sha256": candidate["config_sha256"]})


class TaskAIActionTest(ConflictActionFixture):
    def _start_with_delivery_poll(self, *, during_start):
        notice = {"state": "blocked", "reason": "PR 存在合并冲突", "head_sha": self.head, "updated_at": self.now}
        core.update_delivery(self.conn, self.task(), self.now, pr_state="OPEN", publish_failures=0,
                             last_sync_at=self.now, next_sync_at=util.plus_seconds(300, self.ctx.now), auto_merge=notice)
        candidate = self.prepare()
        observer = cli.Context(self.db_path)
        self.addCleanup(observer.close)
        poll_time = util.plus_seconds(1, self.ctx.now)
        def poll():
            with db.write_txn(observer.conn):
                core.update_delivery(observer.conn, db.require_task(observer.conn, self.task_id), poll_time,
                                     pr_state="OPEN", publish_failures=0, last_sync_at=poll_time,
                                     next_sync_at=util.plus_seconds(301, self.ctx.now),
                                     auto_merge=dict(notice, updated_at=poll_time))
        if during_start:
            prepare = action._prepare
            def prepare_then_poll(*args):
                value = prepare(*args)
                poll()
                return value
            with patch.object(action, "_prepare", side_effect=prepare_then_poll):
                result = self.start(candidate)
        else:
            poll()
            result = self.start(candidate)
        assert result["ok"]
        assert self.start(candidate)["already_started"]
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 1
        assert self.conn.execute("SELECT count(*) FROM events WHERE kind='task_ai_action_started'").fetchone()[0] == 1
        delivery = core.delivery_of(self.task())
        assert delivery["last_sync_at"] == delivery["auto_merge"]["updated_at"] == poll_time
        assert delivery["next_sync_at"] == util.plus_seconds(301, self.ctx.now)

    def test_delivery_poll_between_prepare_and_start_keeps_confirmation_valid(self):
        self._start_with_delivery_poll(during_start=False)

    def test_delivery_poll_during_start_does_not_reject_or_overwrite_observation(self):
        self._start_with_delivery_poll(during_start=True)

    def test_start_rejects_concurrent_identity_state_configuration_and_gate_changes(self):
        from kanpilot import runtime_operations
        core.update_delivery(self.conn, self.task(), self.now, pr_state="OPEN", publish_failures=0,
                             auto_merge={"state": "blocked", "reason": "PR 合并冲突", "head_sha": self.head})
        candidate, original = self.prepare(), dict(self.task())
        config = db.get_setting(self.conn, "exec_config")
        observer = cli.Context(self.db_path)
        self.addCleanup(observer.close)
        prepare = action._prepare
        cases = [("task", key, value) for key, value in (
            ("run_id", "other-run"), ("project_id", "other-project"), ("repository", str(self.root / "other")),
            ("requirements_sha256", "a" * 64), ("plan_sha256", "b" * 64), ("review_key", "other-review"),
            ("review_report_path", str(self.root / "other.md")), ("review_report_sha256", "c" * 64),
            ("status", "needs_user"), ("stage", "monitor"), ("save_state", "archived"),
            ("auto_advance_paused", 1), ("send_state", "unknown"))]
        cases += [("delivery", key, value) for key, value in (
            ("pr_number", 8), ("head_sha", "d" * 40), ("base_sha", "e" * 40),
            ("branch", "other-branch"), ("base_branch", "other-base"), ("worktree_path", str(self.root / "other-tree")),
            ("pr_state", "MERGED"), ("publish_failures", 1),
            ("auto_merge", {"state": "unknown", "reason": "合并结果待确认", "head_sha": self.head}))]
        cases += [("extra", "review_conclusion", "blocked"), ("extra", "automation", {"manual_takeover": True}),
                  ("setting", "exec_config", {"backend": "pi"}), ("setting", "paused_stages", ["delivery"]),
                  ("setting", "paused_stages", ["dispatch"]), ("request", "delivery", None),
                  ("operation", "pr-merge", None)]
        for group, key, replacement in cases:
            with self.subTest(group=group, field=key, value=replacement):
                def prepare_then_change(*args):
                    value = prepare(*args)
                    with db.write_txn(observer.conn):
                        fresh = db.require_task(observer.conn, self.task_id)
                        if group == "task":
                            db.update_task(observer.conn, self.task_id, self.now, **{key: replacement})
                        elif group == "delivery":
                            core.update_delivery(observer.conn, fresh, self.now, **{key: replacement})
                        elif group == "extra":
                            db.set_extra(observer.conn, fresh, self.now, **{key: replacement})
                        elif group == "setting":
                            db.set_setting(observer.conn, key, replacement)
                        elif group == "request":
                            db.create_request(observer.conn, self.now, "delivery", "concurrent-delivery",
                                              task_id=self.task_id, requested_by="automation", payload={})
                        else:
                            runtime_operations.begin_operation(observer.conn, "pr-merge:" + self.task_id,
                                                               "concurrent-merge", "github", "github_pr_merge", {})
                    return value
                with patch.object(action, "_prepare", side_effect=prepare_then_change):
                    with pytest.raises(util.KanpilotError) as failure:
                        self.start(candidate)
                assert failure.value.code == "conflict"
                assert not core.review_repair_of(self.task())
                assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 0
                assert self.conn.execute("SELECT count(*) FROM events WHERE kind='task_ai_action_started'").fetchone()[0] == 0
                with db.write_txn(observer.conn):
                    db.update_task(observer.conn, self.task_id, self.now,
                                   **{key: value for key, value in original.items() if key not in ("task_id", "updated_at")})
                    db.set_setting(observer.conn, "exec_config", config)
                    db.set_setting(observer.conn, "paused_stages", [])
                    observer.conn.execute("DELETE FROM requests WHERE idempotency_key='concurrent-delivery'")
                    observer.conn.execute("DELETE FROM runtime_operations WHERE operation_id='concurrent-merge'")

    def test_only_passed_review_and_delivery_stages_offer_action(self):
        for stage in ("review", "delivery", "plan", "dispatch", "monitor"):
            with self.subTest(stage=stage):
                db.update_task(self.conn, self.task_id, self.now, stage=stage)
                before = self.conn.total_changes, dict(self.task())
                view = action.projection(self.conn, self.task(), self.ctx.now)
                if stage in ("review", "delivery"):
                    assert view["can_prepare"]
                    assert self.prepare()["head_sha"] == self.head
                else:
                    assert not view["can_prepare"]
                    with pytest.raises(util.KanpilotError, match="仅评审通过"):
                        self.prepare()
                assert before == (self.conn.total_changes, dict(self.task()))

    def test_prepare_read_only_cancel_leaves_task_and_git_unchanged(self):
        before = self.conn.total_changes, dict(self.task()), git(self.worktree, "status", "--porcelain"), git(self.worktree, "rev-parse", "HEAD")
        candidate = self.prepare()
        assert candidate["base_sha"] == self.base != self.original_base
        assert candidate["head_sha"] == self.head
        assert before == (self.conn.total_changes, dict(self.task()), git(self.worktree, "status", "--porcelain"), git(self.worktree, "rev-parse", "HEAD"))

    def test_start_one_dispatch_preserves_pass_history_rounds_and_cost(self):
        core.update_automation(self.conn, self.task(), self.now, fix_round=2, cost_usd=1.25, segment_cost_base_usd=0.25,
                               segment_started_at="2020-01-01T00:00:00Z")
        candidate = self.prepare()
        first = self.start(candidate)
        db.set_setting(self.conn, "exec_config", dict(exec_config.load(self.conn), model="changed-after-authorized"))
        second = self.start(candidate)
        assert second["already_started"] and first["request_id"] == second["request_id"]
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 1
        assert core.automation_of(self.task())["fix_round"] == 2
        assert core.automation_of(self.task())["segment_cost_base_usd"] == 0.25
        assert core.automation_of(self.task())["segment_started_at"] == self.now
        fix = core.review_repair_of(self.task())
        assert fix["authorized_by"] == "user" and fix["source_kind"] == "merge_conflict"
        assert self.task()["review_key"] is None
        assert db.task_extra(self.task())["review_history"][0]["conclusion"] == "pass"

    def test_start_rejects_configuration_and_head_base_drift(self):
        candidate = self.prepare()
        config = exec_config.load(self.conn)
        config["pi_desktop"]["thinking_level"] = "low"
        db.set_setting(self.conn, "exec_config", config)
        with pytest.raises(util.KanpilotError, match="配置已变化"):
            self.start(candidate)
        for field, value in (("headRefOid", "a" * 40), ("baseRefOid", "b" * 40), ("isCrossRepository", True)):
            saved = self.pr[field]; self.pr[field] = value
            with pytest.raises(util.KanpilotError):
                self.prepare()
            self.pr[field] = saved
        assert not core.review_repair_of(self.task())

    def test_backend_without_structured_evidence_is_rejected_before_writes(self):
        db.set_setting(self.conn, "exec_config", {"backend": "pi"})
        before = self.conn.total_changes, git(self.worktree, "status", "--porcelain")
        with pytest.raises(util.KanpilotError, match="结构化验证报告"):
            self.prepare()
        assert before == (self.conn.total_changes, git(self.worktree, "status", "--porcelain"))

    def test_dirty_tree_and_unstopped_source_rejected(self):
        (self.worktree / "unexpected").write_text("user edit")
        with pytest.raises(util.KanpilotError, match="未提交改动"):
            self.prepare()
        (self.worktree / "unexpected").unlink()
        job = util.jload((self.source_directory / "job.json").read_text()); job.update(status="running", ended_at=None)
        (self.source_directory / "job.json").write_text(util.jdump(job))
        with pytest.raises(util.KanpilotError):
            self.prepare()

    def test_unresolved_merge_operation_blocks_and_remote_change_rejects(self):
        from kanpilot import runtime_operations
        with db.write_txn(self.conn):
            runtime_operations.begin_operation(self.conn, "pr-merge:" + self.task_id, "old", "github", "github_pr_merge", {})
        with pytest.raises(util.KanpilotError, match="结果待确认"):
            self.prepare()

    def test_spent_cost_cannot_be_reset_by_explicit_action(self):
        core.update_automation(self.conn, self.task(), self.now, cost_usd=2.0, segment_cost_base_usd=0.0)
        mappings = db.get_setting(self.conn, "project_mappings")
        mappings["items"][self.task()["project_id"]]["automation"] = {"max_task_cost_usd": 1.0}
        db.set_setting(self.conn, "project_mappings", mappings)
        with pytest.raises(util.KanpilotError, match="费用额度"):
            self.prepare()

    def test_plus_running_source_rejected_without_any_db_writes(self):
        from kanpilot import harness_plus_adapter
        self.conn.execute("UPDATE execution_rounds SET backend='pi_desktop',session_id='old-session',config_json=? WHERE task_id=? AND run_id='exec1'",
                          (util.jdump({"backend": "pi_desktop", "connection_file": str(self.root / "fake-connection.json")}), self.task_id))
        job = util.jload((self.source_directory / "job.json").read_text())
        job.update(backend="pi_desktop", session_id="old-session", turn_id="old-turn")
        (self.source_directory / "job.json").write_text(util.jdump(job))
        before = self.conn.total_changes
        with patch.object(harness_plus_adapter, "plus_status", return_value={"is_running": True, "pending_permission": None, "current_turn_id": "old-turn"}):
            with pytest.raises(util.KanpilotError, match="Plus"):
                self.prepare()
        assert self.conn.total_changes == before

    def test_projection_failed_and_blocked_update_same_history_entry(self):
        self.start()
        for code, expected in (("execution_failed", "failed"), ("review_blocked", "blocked")):
            db.update_task(self.conn, self.task_id, self.now, status="needs_user", failure_code=code, blocked_summary="真实原因")
            view = action.projection(self.conn, self.task(), self.ctx.now)
            assert view["current"]["state"] == view["history"][-1]["state"] == expected
            assert view["current"]["reason"] == "真实原因"

    def test_ensure_operation_precedes_git_and_recovers_only_same_merge(self):
        self.start()
        real = git_delivery.prepare_conflict_merge
        def operation_before_git(*args, **kwargs):
            assert not self.conn.in_transaction
            row = self.conn.execute("SELECT * FROM runtime_operations WHERE tool='prepare_conflict_merge'").fetchone()
            assert row["status"] in ("unknown", "accepted")
            return real(*args, **kwargs)
        with patch.object(git_delivery, "prepare_conflict_merge", side_effect=operation_before_git):
            assert action.ensure_worktree(self.conn, self.task()) == self.worktree
            assert action.ensure_worktree(self.conn, self.task()) == self.worktree
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == self.base
        assert git(self.worktree, "rev-parse", "ORIG_HEAD") == self.head
        body = action.prompt_body(core.review_repair_of(self.task()), self.task()["plan_path"])
        assert "禁止提交" in body and "python3 -c 'assert True'" in body

    def test_context_tamper_and_ambiguous_unknown_preserve_tree(self):
        self.start()
        fix = core.review_repair_of(self.task()); fix["target_base_sha"] = "f" * 40
        with pytest.raises(util.KanpilotError, match="冻结上下文"):
            action.verify_frozen(self.task(), fix)
        from kanpilot import runtime_operations
        fix = core.review_repair_of(self.task())
        identity = {key: fix[key] for key in ("action_id", "action_context_sha256", "head_sha", "target_base_sha", "worktree_path")}
        runtime_operations.begin_operation(self.conn, "task-ai-action:" + self.task_id, "merge:" + fix["action_id"],
                                           "git", "prepare_conflict_merge", identity)
        with pytest.raises(util.KanpilotError, match="不属于本次"):
            action.ensure_worktree(self.conn, self.task())
        assert git(self.worktree, "status", "--porcelain") == ""

    def test_history_survives_fix_clear_and_queued_not_started(self):
        self.start()
        action.record_progress(self.conn, self.task(), self.now, "running", execution_run_id="exec2", session_id="session2")
        db.update_task(self.conn, self.task_id, self.now, status="prepared", run_id="exec2")
        assert action.projection(self.conn, self.task(), self.ctx.now)["current"]["state"] == "queued"
        action.record_review_result(self.conn, self.task(), self.now, "pass")
        db.set_extra(self.conn, self.task(), self.now, fix_request=None)
        view = action.projection(self.conn, self.task(), self.ctx.now)
        assert view["current"] is None and view["history"][-1]["state"] == "completed"
        assert view["history"][-1]["execution_run_id"] == "exec2"
