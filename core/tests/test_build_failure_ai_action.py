"""Build repair uses disposable Git/SQLite, fake GitHub and synthetic executor evidence."""
from copy import deepcopy
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

import pytest

from test_git_delivery import git
from test_task_ai_action import ConflictActionFixture
import test_task_ai_action_lifecycle as lifecycle
from kanpilot import (cmd_dispatch, cmd_queue, core, db, derive, engine_claim_scope,
    exec_config, execution_contract, gh_client, git_delivery, mcp_client, pr_auto_merge,
    release_delivery, release_github, repair_prompt, round_store, runtime_operations,
    remote, task_ai_action as action, util, verify_evidence)
from kanpilot.engine import verdict


class BuildFailureFixture(ConflictActionFixture):
    def setUp(self):
        super().setUp()
        self.source_tree, self.source_head = self.worktree, self.head
        # GitHub creates the merge remotely; the caller has not fetched it yet.
        producer = self.root / "merge-producer"
        subprocess.run(["git", "clone", "--no-local", str(self.bare), str(producer)], check=True, capture_output=True)
        git(producer, "config", "user.name", "Build Fixture")
        git(producer, "config", "user.email", "build@example.invalid")
        tree = git(producer, "rev-parse", self.base + "^{tree}")
        self.target = subprocess.run(["git", "-C", str(producer), "commit-tree", tree, "-p", self.base,
            "-p", self.head, "-m", "original merged task"], check=True, capture_output=True, text=True).stdout.strip()
        git(producer, "push", "origin", self.target + ":refs/heads/main")
        assert subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", self.target], capture_output=True).returncode != 0
        self.pr.update(state="MERGED", mergedAt=self.now)
        core.update_delivery(self.conn, self.task(), self.now, pr_state="MERGED", merged_at=self.now,
            pr_url=self.pr["url"], pushed_sha=self.head, auto_merge={"state": "merged"})
        self.batch = dict(batch_id="failed-build-1", project_id=self.task()["project_id"], repository="owner/repo",
            root_task_id=self.task_id, state="failed", dispatch_status="sent_or_unknown", target_sha=self.target,
            inputs={"target_sha": self.target}, members=[release_delivery._identity(self.task())],
            context_sha256="b" * 64, policy={"channel": "prerelease"}, version="0.1.0", tag="v0.1.0-pilot.1", build_number=1,
            workflow={"id": 44, "ref": "main", "head_sha": self.base}, workflow_run_id=100,
            workflow_url="https://github.com/owner/repo/actions/runs/100")
        runtime_operations.begin_operation(self.conn, release_delivery._scope(self.task()["project_id"]),
            "batch:" + self.batch["batch_id"], "github", release_delivery.BATCH_TOOL, {})
        release_delivery._save(self.conn, self.batch, self.now)
        db.set_extra(self.conn, self.task(), self.now, release_ref={"batch_id": self.batch["batch_id"], "required": True})
        self.run_value = dict(id=100, workflow_id=44, event="workflow_dispatch", display_title="pilot-release-failed-build-1",
            head_branch="main", head_sha=self.base, repository={"full_name": "owner/repo"}, run_attempt=1,
            html_url=self.batch["workflow_url"], status="completed", conclusion="failure")
        self.jobs = [dict(id=101, run_id=100, run_attempt=1, name="Build", status="completed", conclusion="failure")]
        for module, name, kwargs in (
            (release_github, "run", {"side_effect": lambda *args: deepcopy(self.run_value)}),
            (release_github, "jobs", {"side_effect": lambda *args: deepcopy(self.jobs)}),
            (release_github, "_binary", {"return_value": b"compile error: missing symbol\nGH_TOKEN=ghp_secretfixture\n"}),
            (release_github, "merge_commit_sha", {"return_value": self.target})):
            mock = patch.object(module, name, **kwargs); mock.start(); self.addCleanup(mock.stop)

    def prepare(self):
        return action.cmd_prepare_task_ai_action(self.ctx, {"task_id": self.task_id, "kind": "build_failure"})["candidate"]

    def start(self, candidate=None):
        candidate = candidate or self.prepare()
        return action.cmd_start_task_ai_action(self.ctx, dict(task_id=self.task_id, kind="build_failure", confirm=True,
            expected_context_sha256=candidate["context_sha256"], expected_config_sha256=candidate["config_sha256"]))


class BuildFailureActionTest(BuildFailureFixture):
    claim_action = lifecycle.TaskAIActionLifecycle.claim_action
    goal_evidence = lifecycle.TaskAIActionLifecycle.goal_evidence

    def prepare_merge(self):
        self.claim_action()
        self.worktree = Path(core.delivery_of(self.task())["worktree_path"])
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, self.run, self.conn)
        assert path == self.worktree and mode == "branch"
        self.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
            (str(self.worktree), self.task_id, self.run))
        commands = verify_evidence.parse_verify_block(self.fixture["plan_path"].read_text())["commands"]
        db.set_extra(self.conn, self.task(), self.now, plan_verify={"plan_sha256": self.task()["plan_sha256"], "commands": commands})
        return path

    finish_files_and_claim_monitor = lifecycle.TaskAIActionLifecycle.finish_files_and_claim_monitor

    def test_prepare_is_read_only_and_candidate_separates_original_pr_and_new_identity(self):
        def snapshot():
            return (self.conn.total_changes, dict(self.task()), git(self.repo, "worktree", "list", "--porcelain"),
                git(self.repo, "show-ref"), git(self.repo, "count-objects", "-v"))
        before = snapshot()
        value = self.prepare()
        assert value["kind"] == "build_failure" and value["pr_number"] is None
        assert value["source_pr_number"] == 7 and value["source_pr_url"] == self.pr["url"]
        assert value["head_sha"] == value["base_sha"] == self.target
        assert value["branch"] != self.branch and value["workflow_attempt"] == 1
        assert before == snapshot()
        assert subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", self.target], capture_output=True).returncode != 0
        assert action.projection(self.conn, self.task(), self.ctx.now)["kind"] == "build_failure"

    def test_start_freezes_logs_config_and_old_delivery_without_touching_failed_batch(self):
        old_verification = dict(verification_id="old-proof", context_sha256="a"*64, state="accepted")
        db.set_extra(self.conn, self.task(), self.now, release_verification_ref=old_verification)
        old = deepcopy(release_delivery._batch(self.conn, self.batch["batch_id"]))
        candidate = self.prepare()
        self.start(candidate)
        assert self.start(candidate)["already_started"]
        fix = core.review_repair_of(self.task())
        assert fix["source_worktree_path"] == str(self.source_tree) and fix["source_head_sha"] == self.source_head
        assert fix["worktree_path"] != str(self.source_tree)
        assert "ghp_secretfixture" not in util.jdump(fix)
        assert "missing symbol" in action.prompt_body(fix, self.task()["plan_path"])
        assert not Path(fix["worktree_path"]).exists()
        delivery = core.delivery_of(self.task())
        for key in ("pr_number", "pr_url", "pushed_sha", "merged_at", "pr_state", "auto_merge", "stage_contracts"):
            assert not delivery.get(key)
        assert db.task_extra(self.task())["build_failure_sources"][0]["delivery"]["pr_number"] == 7
        assert db.task_extra(self.task())["build_failure_sources"][0]["release_verification_ref"] == old_verification
        assert "release_verification_ref" not in db.task_extra(self.task())
        assert release_delivery._batch(self.conn, self.batch["batch_id"]) == old
        shown = release_delivery.projection(self.conn, self.task())
        assert shown["state"] == "failed" and not shown["can_reconcile"] and not shown["can_start"]
        with pytest.raises(util.KanpilotError, match="归档"):
            release_delivery.cmd_sync_release_batch(self.ctx, {"batch_id": self.batch["batch_id"]})
        assert release_delivery._batch(self.conn, self.batch["batch_id"]) == old
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 1

    def test_background_delivery_observations_do_not_invalidate_confirmation(self):
        candidate = self.prepare()
        core.update_delivery(self.conn, self.task(), self.now, last_sync_at=self.now, next_sync_at=self.now,
            stage_contracts={"sync": {"run_id": "poll"}}, stage_results={"sync": {"outcome": "waiting"}},
            auto_merge={"state": "merged", "updated_at": self.now})
        assert self.start(candidate)["ok"]
        assert db.task_extra(self.task())["build_failure_sources"][0]["delivery"]["last_sync_at"] == self.now

    def test_attempt_log_config_or_remote_base_drift_rejects_confirmation(self):
        candidate = self.prepare()
        self.run_value["run_attempt"] = 2
        self.jobs[0]["run_attempt"] = 2
        with pytest.raises(util.KanpilotError, match="已变化"):
            self.start(candidate)
        self.run_value["run_attempt"] = self.jobs[0]["run_attempt"] = 1
        with patch.object(release_github, "_binary", return_value=b"different error"):
            with pytest.raises(util.KanpilotError, match="已变化"):
                self.start(candidate)
        config = db.get_setting(self.conn, "exec_config")
        config["pi_desktop"]["model_id"] = "new-model"
        db.set_setting(self.conn, "exec_config", config)
        with pytest.raises(util.KanpilotError, match="已变化"):
            self.start(candidate)
        git(self.repo, "push", "--force", "origin", self.base + ":refs/heads/main")
        with pytest.raises(util.KanpilotError, match="目标分支"):
            self.prepare()
        assert not core.review_repair_of(self.task())

    def test_missing_logs_job_mismatch_and_unconfirmed_run_reject_without_writes(self):
        before = self.conn.total_changes
        for field, value in (("run_attempt", 2), ("run_id", 200)):
            original = self.jobs[0][field]; self.jobs[0][field] = value
            with pytest.raises(util.KanpilotError):
                self.prepare()
            self.jobs[0][field] = original
        with patch.object(release_github, "_binary", return_value=b""):
            with pytest.raises(util.KanpilotError):
                self.prepare()
        self.run_value.update(status="in_progress", conclusion=None)
        with pytest.raises(util.KanpilotError, match="失败终态"):
            self.prepare()
        assert self.conn.total_changes == before

    def test_git_receipt_precedes_new_worktree_and_exact_reentry_is_safe(self):
        self.start()
        refs = git(self.repo, "show-ref")
        observed = []
        real = git_delivery._git
        def inspect(args, *rest, **kwargs):
            if args[0] == "fetch" or args[:2] == ["worktree", "add"]:
                receipt = self.conn.execute("SELECT * FROM runtime_operations WHERE tool='prepare_build_repair_worktree'").fetchone()
                assert receipt is not None and receipt["status"] == "unknown" and not self.conn.in_transaction
                assert core.review_repair_of(self.task())["failure_logs"]
                observed.append(args[0])
            if args[:2] == ["worktree", "add"]:
                assert git(self.repo, "show-ref") == refs
            return real(args, *rest, **kwargs)
        with patch.object(git_delivery, "_git", side_effect=inspect):
            path = action.ensure_worktree(self.conn, self.task())
        assert observed == ["fetch", "worktree"]
        assert path == action.ensure_worktree(self.conn, self.task())
        assert git(Path(path), "rev-parse", "HEAD") == self.target
        assert git(self.source_tree, "rev-parse", "HEAD") == self.source_head
        assert git(self.source_tree, "status", "--porcelain") == ""
        (Path(path) / "uncommitted.txt").write_text("keep")
        with pytest.raises(git_delivery.GitDeliveryError):
            action.ensure_worktree(self.conn, self.task())
        assert (Path(path) / "uncommitted.txt").read_text() == "keep"

    def test_remote_drift_during_fetch_never_creates_worktree_or_claims_execution(self):
        self.start()
        fix = core.review_repair_of(self.task())
        rounds = self.conn.execute("SELECT count(*) FROM execution_rounds WHERE task_id=?", (self.task_id,)).fetchone()[0]
        real = git_delivery._git
        observed = []
        def drift(args, *rest, **kwargs):
            result = real(args, *rest, **kwargs)
            if args[0] == "fetch":
                observed.append("fetch")
                git(self.repo, "push", "--force", "origin", self.base + ":refs/heads/main")
            return result
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(git_delivery, "_git", side_effect=drift), \
                patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert observed == ["fetch"]
        assert not claimed["claimed"] and "目标分支" in claimed["detail"]
        assert not Path(fix["worktree_path"]).exists()
        assert git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + fix["branch"]) == ""
        assert self.conn.execute("SELECT count(*) FROM execution_rounds WHERE task_id=?", (self.task_id,)).fetchone()[0] == rounds

    def test_dispatch_start_rechecks_remote_after_worktree_prepared(self):
        self.claim_action()
        git(self.repo, "push", "--force", "origin", self.base + ":refs/heads/main")
        db.update_task(self.conn, self.task_id, self.now, status="submitting")
        process = Mock(wraps=subprocess)
        process.SubprocessError = subprocess.SubprocessError
        process.Popen.side_effect = AssertionError("worker must not start after base drift")
        with patch.object(cmd_dispatch, "subprocess", process), \
                patch.object(cmd_dispatch, "_require_session_naming_capability"):
            with pytest.raises(util.KanpilotError, match="目标分支"):
                self.call("dispatch-start", dict(task_id=self.task_id, run_id=self.run))
        process.Popen.assert_not_called()
        row = round_store.require_round(self.conn, self.task_id, self.run)
        assert not row["pid"] and not row["session_id"]

    def test_dispatch_rejects_project_push_target_before_worktree_creation(self):
        self.start()
        fix = core.review_repair_of(self.task())
        git(self.repo, "config", "remote.origin.pushurl", "https://github.com/other/repo.git")
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(action, "ensure_worktree") as prepare, \
                patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert not claimed["claimed"] and "单一 GitHub 仓库" in claimed["detail"]
        prepare.assert_not_called()
        assert not Path(fix["worktree_path"]).exists()

    def test_dispatch_checks_new_worktree_push_target_before_claim(self):
        self.start()
        fix = core.review_repair_of(self.task())
        rounds = self.conn.execute("SELECT count(*) FROM execution_rounds WHERE task_id=?", (self.task_id,)).fetchone()[0]
        ensure = action.ensure_worktree
        def unsafe_worktree(*args):
            path = ensure(*args)
            git(self.repo, "config", "extensions.worktreeConfig", "true")
            git(Path(path), "config", "--worktree", "remote.origin.pushurl", "https://github.com/other/repo.git")
            return path
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(action, "ensure_worktree", side_effect=unsafe_worktree), \
                patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert Path(fix["worktree_path"]).is_dir()
        assert not claimed["claimed"] and "单一 GitHub 仓库" in claimed["detail"]
        assert self.conn.execute("SELECT count(*) FROM execution_rounds WHERE task_id=?", (self.task_id,)).fetchone()[0] == rounds

    def test_same_sha_origin_replacement_during_fetch_never_claims_execution(self):
        self.start()
        fix = core.review_repair_of(self.task())
        rounds = self.conn.execute("SELECT count(*) FROM execution_rounds WHERE task_id=?", (self.task_id,)).fetchone()[0]
        other = self.root / "other-origin.git"
        subprocess.run(["git", "clone", "--bare", "--no-local", str(self.bare), str(other)], check=True, capture_output=True)
        real = git_delivery._git
        observed = []
        def replace_origin(args, *rest, **kwargs):
            result = real(args, *rest, **kwargs)
            if args[0] == "fetch":
                observed.append("fetch")
                git(self.repo, "remote", "set-url", "origin", str(other))
                assert git(self.repo, "ls-remote", "origin", "refs/heads/main").split()[0] == self.target
            return result
        def origin_identity(repository):
            return "other/repo" if git(repository, "remote", "get-url", "origin") == str(other) else "owner/repo"
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(git_delivery, "_git", side_effect=replace_origin), \
                patch.object(git_delivery, "origin_github_repo", side_effect=origin_identity), \
                patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert observed == ["fetch"]
        assert not claimed["claimed"] and "远端仓库身份已变化" in claimed["detail"]
        assert not Path(fix["worktree_path"]).exists()
        assert git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + fix["branch"]) == ""
        assert self.conn.execute("SELECT count(*) FROM execution_rounds WHERE task_id=?", (self.task_id,)).fetchone()[0] == rounds

    def _register_review(self, monitor, conclusion="pass"):
        self.call("advance-task", dict(task_id=self.task_id, run_id=monitor["run_id"], target="in-review", expected_current="in-progress"))
        summary = derive.task_summary(self.conn, self.task(), self.ctx.now)
        assert summary["phase"] == "review" and summary["display_status"] == "处理后待复审", summary
        self.call("report", dict(request_id=monitor["request_id"], run_id=monitor["run_id"], outcome="succeeded"))
        review = self.call("claim", {"stage": "review", "executor": "kaneo-review"})
        assert review["claimed"], review
        if conclusion == "changes_requested":
            core.update_automation(self.conn, self.task(), self.now, fix_round=0)
        report = self.reports_root / "build-repair-review.md"; report.write_text("# Review\nBuild repair reviewed.\n")
        payload = dict(task_id=self.task_id, request_id=review["request_id"], run_id=review["run_id"],
            expected_execution_run_id=self.run, review_key="build-repair-review", conclusion=conclusion, report_path=str(report))
        if conclusion == "changes_requested":
            payload.update(fix_mode="same_task", fix=dict(title="Still broken", description="Build fix incomplete", priority="high"))
        self.call("register-review", payload)
        self.call("report", dict(request_id=review["request_id"], run_id=review["run_id"], outcome="succeeded"))
        return review

    def test_plus_strict_commit_is_single_parent_then_review_only_authorizes_new_pr(self):
        monitor = self.finish_files_and_claim_monitor()
        self.goal_evidence(monitor)
        assert self.call("delivery-commit", monitor)["outcome"] == "committed"
        assert git(self.worktree, "show", "-s", "--format=%P", "HEAD").split() == [self.target]
        self._register_review(monitor)
        continuation = action.delivery_continuation(self.task())
        assert continuation and continuation["action"]["source_kind"] == "build_failure"
        assert engine_claim_scope.action_delivery_allowed(self.conn, self.task())
        assert core.delivery_of(self.task()).get("pr_number") is None
        assert pr_auto_merge.snapshot(self.conn, self.task()) is None
        with patch.object(release_delivery, "cmd_start_task_release", side_effect=AssertionError("no release")):
            assert release_delivery.after_merge(self.ctx, self.task())["outcome"] == "blocked"
        shown = action.projection(self.conn, self.task(), self.ctx.now)
        assert shown["kind"] == "build_failure" and shown["history"][-1]["source_pr_number"] == 7
        assert not shown["can_prepare"]

    def _publish_new_pr(self):
        claimed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery"})
        assert claimed["claimed"], claimed
        delivery = core.delivery_of(self.task())
        new_pr = dict(self.pr, number=8, url="https://github.com/owner/repo/pull/8", state="OPEN",
            headRefOid=delivery["head_sha"], headRefName=delivery["branch"], mergedAt=None)
        old_comment = {"id": "original-comment", "content": "[kaneopilot:delivery:" + self.task_id + "] 已创建 PR：" + self.pr["url"]}
        comments = [old_comment]
        parser = git_delivery.github_repository
        def find(_, task_id, marker):
            return next((comment for comment in comments if marker in comment["content"]), None)
        def post(_, task_id, content):
            comments.append(dict(id="new-build-pr-comment", content=content)); return comments[-1]
        identity = dict(task_id=self.task_id, request_id=claimed["request_id"], run_id=claimed["run_id"], expected_execution_run_id=self.run)
        with patch.object(git_delivery, "github_repository", side_effect=lambda url, **kw: "owner/repo" if url == str(self.bare) else parser(url, **kw)), \
             patch.object(gh_client, "pr_list", return_value=[]), patch.object(gh_client, "pr_create", return_value=new_pr) as create, \
             patch.object(remote, "find_marker_comment", side_effect=find), patch.object(remote, "post_comment", side_effect=post):
            result = self.call("delivery-publish", identity)
            assert result["outcome"] == "published", result
        assert create.call_count == 1
        assert len(comments) == 2 and comments[0] == old_comment
        assert ":build:" in comments[1]["content"] and ":pr:8]" in comments[1]["content"]
        assert comments[1]["content"].endswith(new_pr["url"])
        assert git(self.bare, "rev-parse", "refs/heads/" + delivery["branch"]) == delivery["head_sha"]
        assert git(self.bare, "rev-parse", "refs/heads/" + self.branch) == self.source_head
        self.pr = new_pr
        return identity

    def test_new_pr_publishes_its_own_comment_and_never_automatically_merges_or_releases(self):
        db.set_extra(self.conn, self.task(), self.now, review_repair_round=1)
        monitor = self.finish_files_and_claim_monitor(); self.goal_evidence(monitor)
        assert self.call("delivery-commit", monitor)["outcome"] == "committed"
        self._register_review(monitor)
        assert db.task_extra(self.task())["review_history"][-1]["fix_round"] == 1
        identity = self._publish_new_pr()
        assert derive.task_summary(self.conn, self.task(), self.ctx.now)["display_status"] != "构建失败"
        with patch.object(gh_client, "pr_merge", side_effect=AssertionError("no automatic merge")), \
             patch.object(release_delivery, "cmd_start_task_release", side_effect=AssertionError("no automatic release")):
            assert self.call("delivery-sync", identity)["outcome"] == "waiting"
            self.pr.update(state="MERGED", mergedAt=self.now)
            result = self.call("delivery-sync", identity)
            assert result["outcome"] == "waiting" and (not result.get("release") or not result["release"]["can_reconcile"])
        assert release_delivery._batch(self.conn, self.batch["batch_id"])["state"] == "failed"

    def test_stable_successor_requires_explicit_start_and_only_supersedes_its_exact_failed_batch(self):
        self.batch.update(policy={"channel": "stable"}, tag="v0.1.0")
        release_delivery._save(self.conn, self.batch, self.now)
        monitor = self.finish_files_and_claim_monitor(); self.goal_evidence(monitor)
        assert self.call("delivery-commit", monitor)["outcome"] == "committed"
        self._register_review(monitor)
        identity = self._publish_new_pr()
        self.pr.update(state="MERGED", mergedAt=self.now)
        assert self.call("delivery-sync", identity)["outcome"] == "waiting"
        mappings = db.get_setting(self.conn, "project_mappings")
        policy = dict(enabled=True, channel="stable", workflow_path=".github/workflows/pilot-release.yml",
            workflow_ref="main", version_base="0.1.0", required_assets=["app.zip"], required_jobs=["Build"])
        mappings["items"][self.task()["project_id"]]["automation"] = {"release": policy}
        db.set_setting(self.conn, "project_mappings", mappings)
        old = deepcopy(release_delivery._batch(self.conn, self.batch["batch_id"]))
        proof = dict(github_repository="owner/repo", target_sha=core.delivery_of(self.task())["head_sha"])
        workflow = dict(id=44, path=policy["workflow_path"], ref="main", head_sha=self.base, state="active", repository="owner/repo")
        with patch.object(release_delivery, "_software_proof", return_value=proof), \
             patch.object(release_github, "workflow_snapshot", return_value=workflow), \
             patch.object(release_github, "list_releases", return_value=[]):
            preview = release_delivery.cmd_prepare_task_release(self.ctx, {"task_id": self.task_id})
            assert preview["context_sha256"] and preview["release"]["can_start"], preview
            assert preview["release"]["version"] == "0.1.1"
            other = dict(old, batch_id="unrelated-stable", version="0.2.0", tag="v0.2.0")
            scope = release_delivery._scope(self.task()["project_id"])
            runtime_operations.begin_operation(self.conn, scope, "batch:unrelated-stable", "github", release_delivery.BATCH_TOOL, {})
            release_delivery._save(self.conn, other, self.now)
            blocked = release_delivery.cmd_prepare_task_release(self.ctx, {"task_id": self.task_id})
            assert blocked["context_sha256"] is None
            hold = db.task_extra(self.task())["build_failure_hold"]
            db.set_extra(self.conn, self.task(), self.now, build_failure_hold=dict(hold, release_batch_id=other["batch_id"]))
            with pytest.raises(release_delivery.ReleaseBlocked, match="归属"):
                release_delivery._superseded_build_batch(self.conn, self.task(), proof)
            db.set_extra(self.conn, self.task(), self.now, build_failure_hold=hold)
            self.conn.execute("DELETE FROM runtime_operations WHERE scope_id=? AND operation_id='batch:unrelated-stable'", (scope,))
            with patch.object(release_delivery, "cmd_start_task_release", side_effect=AssertionError("must require explicit start")):
                assert release_delivery.after_merge(self.ctx, self.task())["outcome"] == "blocked"
            with patch.object(release_delivery, "_dispatch_reserved", side_effect=lambda ctx, batch, task: batch):
                reserved = release_delivery.cmd_start_task_release(self.ctx, dict(task_id=self.task_id,
                    expected_context_sha256=preview["context_sha256"]))
            assert reserved["batch_id"] != old["batch_id"] and reserved["tag"] == "v0.1.1"
            assert reserved["superseded_build_batch_id"] == old["batch_id"]
            assert db.task_extra(self.task())["build_failure_hold"]["release_authorized_batch_id"] == reserved["batch_id"]
        assert release_delivery._batch(self.conn, old["batch_id"]) == old
        assert release_delivery.projection(self.conn, self.task())["batch_id"] == reserved["batch_id"]
        # A merely reserved or forged successor cannot release the old slot.
        with pytest.raises(release_delivery.ReleaseBlocked):
            release_delivery._stable_available(self.conn, self.task()["project_id"], policy)
        forged = deepcopy(reserved); forged.update(state="published")
        release_delivery._save(self.conn, forged, self.now)
        with pytest.raises(release_delivery.ReleaseBlocked):
            release_delivery._stable_available(self.conn, self.task()["project_id"], policy)
        # A published successor has a core-accepted receipt, frozen context and
        # exact publication proof. The predecessor stays failed and archived.
        published = deepcopy(reserved)
        published.update(state="published", workflow_run_id=200, publication_proof=dict(workflow_run_id=200,
            run_attempt=1, target_sha=reserved["target_sha"], manifest_sha256="c"*64, release_id=10,
            task_manifest_sha256=reserved["inputs"]["task_manifest_sha256"]))
        # _save intentionally never overwrites a published record; update the
        # disposable receipt to model the verifier's one atomic acceptance.
        runtime_operations.complete_operation(self.conn, scope, "batch:"+reserved["batch_id"],
            status="accepted", result=published, now=self.now)
        assert release_delivery._published_stable_predecessors(self.conn, self.task()["project_id"]) == {old["batch_id"]}
        self.make_ready_task(task_id="anotherstable0001")
        db.update_task(self.conn, "anotherstable0001", self.now, project_id=self.task()["project_id"], repository=str(self.repo))
        with patch.object(release_delivery, "_software_proof", return_value=proof), \
             patch.object(release_github, "workflow_snapshot", return_value=workflow), \
             patch.object(release_github, "list_releases", return_value=[]):
            next_context = release_delivery._prepare(self.ctx, "anotherstable0001")
        assert next_context["version"] == "0.1.2" and "superseded_build_batch_id" not in next_context
        tampered = dict(published, superseded_build_batch_id="other-batch")
        runtime_operations.complete_operation(self.conn, scope, "batch:"+reserved["batch_id"],
            status="accepted", result=tampered, now=self.now)
        with pytest.raises(release_delivery.ReleaseBlocked):
            release_delivery._stable_available(self.conn, self.task()["project_id"], policy)
        assert release_delivery._batch(self.conn, old["batch_id"]) == old

    def test_stable_supersession_never_ignores_another_pending_batch_or_tampered_origin(self):
        self.batch["policy"] = {"channel": "stable"}
        release_delivery._save(self.conn, self.batch, self.now)
        with pytest.raises(release_delivery.ReleaseBlocked):
            release_delivery._stable_available(self.conn, self.task()["project_id"], {"channel": "stable"}, "unrelated")

    def test_unknown_release_dispatch_and_previously_handled_batch_are_not_new_authority(self):
        from kanpilot import task_ai_build_failure
        scope = release_delivery._scope(self.task()["project_id"])
        runtime_operations.begin_operation(self.conn, scope, "dispatch:unknown", "github", release_delivery.DISPATCH_TOOL, {})
        with pytest.raises(util.KanpilotError, match="待确认"):
            self.prepare()
        self.conn.execute("DELETE FROM runtime_operations WHERE scope_id=? AND operation_id='dispatch:unknown'", (scope,))
        runtime_operations.begin_operation(self.conn, "release-verify:"+self.task_id, "verification:active", "core", "release_target_verification", {})
        with pytest.raises(util.KanpilotError, match="补验证"):
            self.prepare()
        self.conn.execute("DELETE FROM runtime_operations WHERE scope_id=?", ("release-verify:"+self.task_id,))
        db.set_extra(self.conn, self.task(), self.now, ai_action_history=[dict(kind="build_failure", release_batch_id=self.batch["batch_id"], state="failed")])
        assert "已处理过一次" in task_ai_build_failure.gate(self.conn, self.task())
        with pytest.raises(util.KanpilotError, match="已处理过一次"):
            self.prepare()

    def test_batch_sync_skips_read_only_predecessor_and_keeps_polling_active_batch(self):
        self.start()
        old = deepcopy(release_delivery._batch(self.conn, self.batch["batch_id"]))
        current = dict(old, batch_id="active-build", state="queued", workflow_run_id=200,
            workflow_url="https://github.com/owner/repo/actions/runs/200", members=[release_delivery._identity(self.task())])
        scope = release_delivery._scope(self.task()["project_id"])
        runtime_operations.begin_operation(self.conn, scope, "batch:active-build", "core", release_delivery.BATCH_TOOL, {})
        release_delivery._save(self.conn, current, self.now)
        self.run_value.update(id=200, display_title="pilot-release-active-build", status="in_progress", conclusion=None,
            html_url=current["workflow_url"])
        result = release_delivery.cmd_sync_release_batches(self.ctx, {"limit": 1})
        assert len(result["results"]) == 1 and result["results"][0]["batch_id"] == "active-build"
        assert result["results"][0]["release"]["state"] == "building"
        assert release_delivery._batch(self.conn, old["batch_id"]) == old
        # A later action may replace the current hold, but the archive still
        # protects the original predecessor in individual and batch commands.
        db.set_extra(self.conn, self.task(), self.now, build_failure_hold=None)
        with pytest.raises(util.KanpilotError, match="归档"):
            release_delivery.cmd_sync_release_batch(self.ctx, {"batch_id": old["batch_id"]})

    def test_multiple_failed_stable_successors_require_a_complete_trusted_chain(self):
        self.batch.update(policy={"channel": "stable"}, tag="v0.1.0")
        release_delivery._save(self.conn, self.batch, self.now)
        old = deepcopy(release_delivery._batch(self.conn, self.batch["batch_id"]))
        project_id=self.task()["project_id"];scope=release_delivery._scope(project_id)
        db.set_extra(self.conn, self.task(), self.now, build_failure_sources=[dict(release_batch_id=old["batch_id"])])
        policy=dict(enabled=True,channel="stable",workflow_path=".github/workflows/pilot-release.yml",workflow_ref="main",
                    version_base="0.1.0",required_assets=["app.zip"],required_jobs=["Build"])
        mappings=db.get_setting(self.conn,"project_mappings")
        mappings["items"][project_id]["automation"]={"release":policy}
        db.set_setting(self.conn,"project_mappings",mappings)
        workflow=dict(id=44,path=policy["workflow_path"],ref="main",head_sha=self.base,state="active",repository="owner/repo")
        revision=release_delivery._policy(self.conn,self.task())[1]
        def persist(context,batch_id,state,run_id):
            context=deepcopy(context)
            context["context_sha256"]=util.sha256_text(util.jdump(context))
            batch=dict(context,batch_id=batch_id,state=state,dispatch_status="sent_or_unknown",workflow_run_id=run_id,
                tag="v"+context["version"],build_number=run_id,target_sha=context["proof"]["target_sha"])
            manifest,inputs=release_delivery._inputs(batch);batch.update(task_manifest=manifest,inputs=inputs)
            runtime_operations.begin_operation(self.conn,scope,"batch:"+batch_id,"core",release_delivery.BATCH_TOOL,
                dict(context_sha256=context["context_sha256"]))
            if state=="published":
                batch["publication_proof"]=dict(workflow_run_id=run_id,run_attempt=1,target_sha=batch["target_sha"],
                    manifest_sha256="f"*64,release_id=run_id,task_manifest_sha256=inputs["task_manifest_sha256"])
            release_delivery._save(self.conn,batch,self.now)
            runtime_operations.begin_operation(self.conn,scope,"dispatch:"+batch_id,"github",release_delivery.DISPATCH_TOOL,
                dict(batch_id=batch_id,workflow=workflow,inputs=inputs))
            runtime_operations.complete_operation(self.conn,scope,"dispatch:"+batch_id,status="accepted",
                result=dict(batch_id=batch_id,workflow_run_id=run_id),now=self.now)
            return batch
        context_b=dict(project_id=project_id,repository="owner/repo",root_task_id=self.task_id,
            members=[release_delivery._identity(self.task())],proof=dict(github_repository="owner/repo",target_sha="d"*40),
            policy=policy,policy_snapshot=revision,workflow=workflow,stable_versions=[[0,1,0]],
            frozen_stable_versions=["0.1.0"],version="0.1.1",superseded_build_batch_id=old["batch_id"])
        second=persist(context_b,"failed-second","failed",200)
        proof_c=dict(github_repository="owner/repo",target_sha="e"*40)
        with patch.object(release_delivery,"_software_proof",return_value=proof_c), \
             patch.object(release_github,"workflow_snapshot",return_value=workflow), \
             patch.object(release_github,"list_releases",return_value=[]), \
             patch.object(release_delivery,"_superseded_build_batch",return_value=second["batch_id"]):
            context_c=release_delivery._prepare(self.ctx,self.task_id)
        assert context_c["version"]=="0.1.2"
        db.set_extra(self.conn,self.task(),self.now,build_failure_sources=[dict(release_batch_id=old["batch_id"]),dict(release_batch_id=second["batch_id"])])
        context_c.pop("context_sha256")
        third=persist(context_c,"published-third","published",300)
        assert release_delivery._published_stable_predecessors(self.conn,project_id)=={old["batch_id"],second["batch_id"]}
        self.make_ready_task(task_id="laterstable0001")
        db.update_task(self.conn,"laterstable0001",self.now,project_id=project_id,repository=str(self.repo))
        with patch.object(release_delivery,"_software_proof",return_value=proof_c), \
             patch.object(release_github,"workflow_snapshot",return_value=workflow), \
             patch.object(release_github,"list_releases",return_value=[]):
            assert release_delivery._prepare(self.ctx,"laterstable0001")["version"]=="0.1.3"
        for damage in ("context","unknown-dispatch","cycle"):
            changed=deepcopy(second)
            if damage=="context":changed["context_sha256"]="0"*64
            if damage=="cycle":changed["superseded_build_batch_id"]=third["batch_id"]
            runtime_operations.complete_operation(self.conn,scope,"batch:"+second["batch_id"],status="prepared",result=changed,now=self.now)
            if damage=="unknown-dispatch":runtime_operations.mark_operation_unknown(self.conn,scope,"dispatch:"+second["batch_id"])
            assert release_delivery._published_stable_predecessors(self.conn,project_id)==set()
            with pytest.raises(release_delivery.ReleaseBlocked):
                release_delivery._stable_available(self.conn,project_id,policy,second["batch_id"])
            runtime_operations.complete_operation(self.conn,scope,"batch:"+second["batch_id"],status="prepared",result=second,now=self.now)
            runtime_operations.complete_operation(self.conn,scope,"dispatch:"+second["batch_id"],status="accepted",
                result=dict(batch_id=second["batch_id"],workflow_run_id=200),now=self.now)
        unrelated=dict(old,batch_id="unrelated-stable",version="0.5.0",tag="v0.5.0")
        runtime_operations.begin_operation(self.conn,scope,"batch:unrelated-stable","core",release_delivery.BATCH_TOOL,{})
        release_delivery._save(self.conn,unrelated,self.now)
        with pytest.raises(release_delivery.ReleaseBlocked):
            release_delivery._stable_available(self.conn,project_id,policy)
        assert release_delivery._batch(self.conn,old["batch_id"])==old

    def test_review_changes_preserve_action_and_cannot_auto_retry_or_replan(self):
        monitor = self.finish_files_and_claim_monitor()
        self.goal_evidence(monitor)
        assert self.call("delivery-commit", monitor)["outcome"] == "committed"
        self._register_review(monitor, "changes_requested")
        fix = core.review_repair_of(self.task())
        assert fix["source_kind"] == "build_failure" and fix["state"] == "needs_followup"
        assert derive.task_summary(self.conn, self.task(), self.ctx.now)["display_status"] == "需要后续处理"
        assert action.delivery_continuation(self.task()) is None
        assert not self.conn.execute("SELECT 1 FROM requests WHERE kind='dispatch' AND status='queued'").fetchone()
        for requested in ("resume", "retry", "replan", "retry_verification"):
            with pytest.raises(util.KanpilotError):
                self.call("resolve", dict(task_id=self.task_id, action=requested))
        assert not cmd_queue.execution_retry_candidate(self.conn, self.task(), verification_retry=True)

    def test_legacy_build_action_missing_evidence_cannot_commit_or_override_verify(self):
        monitor = self.finish_files_and_claim_monitor()
        db.set_extra(self.conn, self.task(), self.now, verification_override={"execution_run_id": self.run})
        with pytest.raises(util.KanpilotError):
            self.call("advance-task", dict(task_id=self.task_id, run_id=monitor["run_id"], target="in-review", expected_current="in-progress"))
        assert self.call("delivery-commit", monitor)["outcome"] == "blocked"
        assert git(self.worktree, "rev-parse", "HEAD") == self.target

    def test_build_repair_rejects_executor_merge_even_with_strict_pass(self):
        self.prepare_merge()
        tree = git(self.repo, "rev-parse", self.target + "^{tree}")
        topic = subprocess.run(["git", "-C", str(self.repo), "commit-tree", tree, "-p", self.target,
            "-m", "unexpected merge source"], check=True, capture_output=True, text=True).stdout.strip()
        git(self.worktree, "merge", "--no-ff", "--no-commit", topic)
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == topic
        (self.worktree / "tracked.txt").write_text("bounded repair\n")
        git(self.worktree, "add", "tracked.txt")
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor", sent=1,
            send_state="sent", session_id="session")
        self.call("report", dict(request_id=self.dispatch["request_id"], run_id=self.run, outcome="succeeded"))
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        claimed = self.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        assert claimed["claimed"], claimed
        monitor = dict(task_id=self.task_id, request_id=claimed["request_id"], run_id=claimed["run_id"], expected_execution_run_id=self.run)
        self.goal_evidence(monitor)
        result = self.call("delivery-commit", monitor)
        assert result["outcome"] == "blocked" and "合并" in result["message"]
        assert git(self.worktree, "rev-parse", "HEAD") == self.target
        assert git(self.worktree, "rev-parse", "MERGE_HEAD") == topic

    def test_codex_frozen_backend_strict_native_evidence_commit_and_review(self):
        config = {"backend": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "high", "sandbox": "workspace-write"}
        with patch.object(exec_config, "snapshot", return_value=config), patch.object(exec_config, "check_available", return_value=(True, None)):
            monitor = self.finish_files_and_claim_monitor()
        task = self.task()
        assert core.review_repair_of(task)["action_config"] == config
        directory = self.runs_root / self.task_id / self.run; directory.mkdir(parents=True, exist_ok=True)
        source_sha = git_delivery.source_state_sha256(self.worktree)
        commands = db.task_extra(task)["plan_verify"]["commands"]
        turn = "codex:" + self.run + ":1"
        job = dict(task_id=self.task_id, run_id=self.run, backend="codex", config=config, plan_sha256=task["plan_sha256"],
            completion_policy="command_verification", verification_evidence_version=1, verification_source_sha256=source_sha,
            status="finished", exit_code=0, session_id="session", turn_id=turn, send_state="sent", cli_terminal=True,
            ended_at=self.now, report_id="codex-build-repair", runs_dir=str(directory), pid=1073741823,
            worker_birth={"started_seconds": 1, "started_microseconds": 0})
        spec = dict(job, plan_path=task["plan_path"], repository=task["repository"], cwd=str(self.worktree),
            request_id=self.dispatch["request_id"], scope_id="codex-build:" + self.task_id,
            instruction="Repair only this build failure", required_verification_commands=commands)
        spec.update(repair_prompt.freeze(task))
        execution_contract.freeze_spec(spec, requirements_sha256=task["requirements_sha256"])
        execution_contract.attach_to_job(spec, job)
        with db.write_txn(self.conn):
            round_store.mark_started(self.conn, self.task_id, self.run, self.now, pid=job["pid"])
            self.conn.execute("UPDATE execution_rounds SET run_status='finished',session_id='session' WHERE task_id=? AND run_id=?", (self.task_id, self.run))
        native = [{"type": "thread.started", "thread_id": "session"}]
        for i, command in enumerate(commands):
            native.extend([{"type": "item.started", "item": {"id": str(i), "type": "command_execution", "command": command, "status": "in_progress"}},
                {"type": "item.completed", "item": {"id": str(i), "type": "command_execution", "command": command, "status": "completed", "exit_code": 0}}])
        native.append({"type": "turn.completed"})
        native_path = directory / "codex-turn-1.jsonl"
        native_path.write_text("".join(util.jdump(dict(seq=i, turn_id=turn, event=event, source_sha256=source_sha)) + "\n" for i, event in enumerate(native, 1)))
        report = dict(status="completed", summary="bounded build fix", checks=[dict(command=c, exit_code=0) for c in commands], questions=[])
        output = directory / "codex-output-1.json"; output.write_text(util.jdump(report))
        job["turns"] = [dict(turn_id=turn, source="codex_adapter", session_id="session", pid=1073741822,
            birth={"started_seconds": 1, "started_microseconds": 0}, status="completed", exit_code=0, cli_terminal=True,
            ended_at=self.now, events_file=native_path.name, output_file=output.name,
            native_sha256=util.sha256_file(native_path), output_sha256=util.sha256_file(output))]
        envelope = {key: job[key] for key in ("task_id", "run_id", "session_id", "turn_id", "plan_sha256", "verification_source_sha256", "report_id")}
        envelope.update(report=report, native_sha256=util.sha256_file(native_path))
        (directory / "codex-report.json").write_text(util.jdump(envelope))
        (directory / "events.jsonl").write_text(util.jdump(dict(seq=1, type="session", session_id="session", turn_id=turn)) + "\n" + util.jdump(dict(seq=2, type="terminal", outcome="completed")) + "\n")
        (directory / "spec.json").write_text(util.jdump(spec)); job["spec_sha256"] = util.sha256_file(directory / "spec.json")
        (directory / "job.json").write_text(util.jdump(job))
        with patch.object(verdict, "run_verify_commands", side_effect=AssertionError("no local verify")), patch.object(verdict, "evaluate_goal_task", side_effect=AssertionError("no Plus fallback")):
            receipt = self.call("engine-verdict", dict(monitor, expected_plan_sha256=task["plan_sha256"], operation_id="codex-build-verdict"))
        assert receipt["verification_ok"] is True, receipt
        assert receipt["evidence_source"] == "codex_report"
        assert self.call("delivery-commit", monitor)["outcome"] == "committed"
        assert git(self.worktree, "show", "-s", "--format=%P", "HEAD").split() == [self.target]
        self._register_review(monitor)
        assert engine_claim_scope.action_delivery_allowed(self.conn, self.task())
