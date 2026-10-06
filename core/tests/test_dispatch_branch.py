from pathlib import Path
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers import KanpilotTestCase
from kanpilot import cli, cmd_dispatch, constants, core, db, dispatch_validate, util, round_store, git_delivery
from test_git_delivery import make_repo, branch, git


class DispatchBranch(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.repos_root)
        self.repo = self.repo.rename(self.repos_root / "Feedback")
        self.fixture = self.make_ready_task(task_id="task1234")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.delivery = branch(self.repo, self.root / "worktrees")
        self.wt = Path(self.delivery["worktree_path"])
        with db.write_txn(self.ctx.conn):
            task = db.require_task(self.ctx.conn, "task1234")
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, **self.delivery, mode="pr", commits=[])
            core.update_automation(self.ctx.conn, task, self.ctx.now_iso, fix_round=1)
        self.primary = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))
        self.addCleanup(lambda: self.assertEqual(self.primary, (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))))

    def validate(self):
        return dispatch_validate.validate_dispatch(self.ctx.conn, db.require_task(self.ctx.conn, "task1234"), self.ctx.now)

    def test_branch_cwd_instruction_and_dirty_guard(self):
        task = db.require_task(self.ctx.conn, "task1234")
        self.assertEqual(cmd_dispatch._ensure_worktree(task, "task1234", "exec1", self.ctx.conn), (self.wt, "branch"))
        for desktop in (False, True):
            text = cmd_dispatch._execution_instruction(task, self.wt, desktop=desktop)
            self.assertIn("kaneo/task1234", text)
            self.assertIn(str(self.wt), text)
            self.assertIn("不要新建 worktree 或分支", text)
            self.assertIn("不要 commit", text)
        self.validate()
        (self.wt / "dirty").touch()
        with self.assertRaises(dispatch_validate.DispatchCheckFailed):
            self.validate()
        with self.assertRaises(util.KanpilotError) as caught:
            cmd_dispatch._ensure_worktree(task, "task1234", "exec1", self.ctx.conn)
        self.assertEqual(caught.exception.code, "validation_failed")

    def test_dirty_before_begin_send_never_writes_remote(self):
        self.enqueue_dispatch("task1234")
        claim = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"])
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="prepared")
        (self.wt / "dirty").touch()
        with patch.object(cmd_dispatch.remote, "get_task") as remote_read:
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch.cmd_dispatch_begin_send(self.ctx, {"task_id": "task1234", "run_id": claim["run_id"]})
        remote_read.assert_not_called()
        self.assertEqual(self.fake.tasks["task1234"]["status"], "to-do")

    def manual_takeover(self, mode="manual"):
        with db.write_txn(self.ctx.conn):
            task = db.require_task(self.ctx.conn, "task1234")
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, mode=mode)
            core.update_automation(self.ctx.conn, task, self.ctx.now_iso, manual_takeover=True)
        (self.wt / "user-commit.txt").write_text("preserve user commit")
        git(self.wt, "add", "user-commit.txt")
        git(self.wt, "commit", "-m", "user manual work")
        (self.wt / "user-dirty.txt").write_text("preserve uncommitted work")
        return db.require_task(self.ctx.conn, "task1234")

    def test_manual_takeover_complete_dispatch_preserves_original_branch(self):
        self.manual_takeover()
        self.complete_dispatch_preserves_worktree(manual=True)

    def complete_dispatch_preserves_worktree(self, *, manual):
        before = (git(self.wt, "rev-parse", "HEAD"), git(self.wt, "status", "--porcelain"))
        if manual:
            self.assertNotEqual(before[0], self.delivery["head_sha"])
        else:
            self.assertEqual(before[0], self.delivery["head_sha"])
        self.enqueue_dispatch("task1234")
        claim = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"])
        task = db.require_task(self.ctx.conn, "task1234")
        config = round_store.round_view(round_store.require_round(self.ctx.conn, "task1234", claim["run_id"]))["config"]
        evidence = {"backend": "pi", "repository": str(self.repo),
                    "session_marker": task["session_marker"], "config": config}
        payload = {"task_id": "task1234", "run_id": claim["run_id"]}
        self.call("dispatch-prepare", dict(payload, evidence=evidence))
        self.call("dispatch-begin-send", payload)
        specs = []
        def spawn(argv, **kwargs):
            spec = json.loads(Path(argv[-1]).read_text())
            specs.append(spec)
            (Path(spec["runs_dir"]) / "events.jsonl").write_text(json.dumps({
                "v": 1, "seq": 1, "ts": self.ctx.now_iso, "type": "session",
                "session_id": "manual-session", "session_title": "Manual execution"}) + "\n")
            return Mock(pid=42424)
        isolated_process = SimpleNamespace(Popen=spawn, SubprocessError=subprocess.SubprocessError)
        with patch.object(cmd_dispatch, "subprocess", isolated_process):
            started = self.call("dispatch-start", payload)
        self.assertEqual(started["worktree_path"], str(self.wt))
        self.assertEqual(specs[0]["cwd"], str(self.wt))
        self.assertEqual(specs[0]["worktree_mode"], "branch")
        if manual:
            self.assertIn("不要另建 worktree 或分支", specs[0]["instruction"])
            self.assertIn("保留已有提交和未提交改动", specs[0]["instruction"])
            self.assertNotIn("提交与推送由调度台完成", specs[0]["instruction"])
        else:
            self.assertIn("不要新建 worktree 或分支", specs[0]["instruction"])
            self.assertIn("提交与推送由调度台完成", specs[0]["instruction"])
        result = self.call("dispatch-confirm", dict(payload, evidence={
            **evidence, "protocol_seen": True, "session_id": "manual-session"}))
        self.assertEqual(result["status"], "running")
        self.assertEqual(before, (git(self.wt, "rev-parse", "HEAD"), git(self.wt, "status", "--porcelain")))
        row = round_store.require_round(self.ctx.conn, "task1234", claim["run_id"])
        self.assertEqual(row["worktree_path"], str(self.wt))

    def user_replan(self):
        (self.wt / "partial.py").write_text("# preserved failed-round work\n")
        fingerprint = git_delivery.worktree_state_sha256(self.delivery)
        with db.write_txn(self.ctx.conn):
            task = db.require_task(self.ctx.conn, "task1234")
            core.update_automation(self.ctx.conn, task, self.ctx.now_iso,
                                   replan_requested_at=self.ctx.now_iso,
                                   replan_remote_status="in-progress", replan_worktree_sha256=fingerprint)
        self.fake.tasks["task1234"]["status"] = "in-progress"
        return db.require_task(self.ctx.conn, "task1234")

    def test_user_replan_full_dispatch_preserves_confirmed_partial_work(self):
        self.user_replan()
        self.assertEqual(self.validate()["original_todo_slug"], "in-progress")
        self.complete_dispatch_preserves_worktree(manual=False)

    def test_user_replan_changed_candidate_and_missing_fingerprint_block(self):
        task = self.user_replan()
        original = (self.wt / "partial.py").read_text()
        (self.wt / "partial.py").write_text(original + "# changed after planning\n")
        with self.assertRaises(dispatch_validate.DispatchCheckFailed):
            self.validate()
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(task, "task1234", "exec1", self.ctx.conn)
        (self.wt / "partial.py").write_text(original)
        for value in (None, ""):
            with self.subTest(fingerprint=value):
                with db.write_txn(self.ctx.conn):
                    core.update_automation(self.ctx.conn, task, self.ctx.now_iso, replan_worktree_sha256=value)
                with self.assertRaises(dispatch_validate.DispatchCheckFailed):
                    self.validate()
                with self.assertRaises(util.KanpilotError):
                    cmd_dispatch._ensure_worktree(db.require_task(self.ctx.conn, "task1234"), "task1234", "exec1", self.ctx.conn)

    def test_user_replan_change_before_begin_send_never_touches_remote(self):
        self.user_replan()
        self.enqueue_dispatch("task1234")
        claim = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"])
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="prepared")
        (self.wt / "partial.py").write_text("modified after claim")
        with patch.object(cmd_dispatch.remote, "get_task") as remote_read:
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch.cmd_dispatch_begin_send(self.ctx, {"task_id": "task1234", "run_id": claim["run_id"]})
        remote_read.assert_not_called()
        self.assertEqual(self.fake.tasks["task1234"]["status"], "in-progress")

    def test_user_replan_head_drift_and_unknown_send_still_block(self):
        task = self.user_replan()
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, send_state="unknown")
        with self.assertRaises(dispatch_validate.DispatchCheckFailed) as caught:
            self.validate()
        self.assertEqual(caught.exception.code, "send_conflict")
        with self.assertRaises(util.KanpilotError) as caught:
            cmd_dispatch._ensure_worktree(db.require_task(self.ctx.conn, "task1234"), "task1234", "exec1", self.ctx.conn)
        self.assertEqual(caught.exception.code, "send_conflict")
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, send_state=None)
        git(self.wt, "add", "partial.py")
        git(self.wt, "commit", "-m", "unexpected user commit")
        with self.assertRaises(dispatch_validate.DispatchCheckFailed):
            self.validate()
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(task, "task1234", "exec1", self.ctx.conn)

    def test_fingerprint_alone_does_not_relax_regular_automatic_dispatch(self):
        task = self.user_replan()
        with db.write_txn(self.ctx.conn):
            core.update_automation(self.ctx.conn, task, self.ctx.now_iso, replan_requested_at=None)
        self.fake.tasks["task1234"]["status"] = "to-do"
        with self.assertRaises(dispatch_validate.DispatchCheckFailed):
            self.validate()

    def test_manual_takeover_precedes_retained_pr_mode_and_uses_desktop_instruction(self):
        task = self.manual_takeover(mode="pr")
        self.validate()
        self.assertEqual(cmd_dispatch._ensure_worktree(task, "task1234", "exec1", self.ctx.conn, require_isolated=True),
                         (self.wt, "branch"))
        text = cmd_dispatch._execution_instruction(task, self.wt, desktop=True)
        self.assertIn("下面已核验的计划正文", text)
        self.assertIn("保留已有提交和未提交改动", text)
        self.assertNotIn("提交与推送由调度台完成", text)

    def test_manual_takeover_still_checks_branch_ownership_and_unknown_send(self):
        task = self.manual_takeover()
        with db.write_txn(self.ctx.conn):
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, branch="other-branch")
        with self.assertRaises(dispatch_validate.DispatchCheckFailed):
            self.validate()
        with db.write_txn(self.ctx.conn):
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso,
                                 branch=self.delivery["branch"], worktree_path=str(self.repo))
        with self.assertRaises(dispatch_validate.DispatchCheckFailed):
            self.validate()
        with db.write_txn(self.ctx.conn):
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, worktree_path=str(self.wt))
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, send_state="unknown")
        with self.assertRaises(dispatch_validate.DispatchCheckFailed) as caught:
            self.validate()
        self.assertEqual(caught.exception.code, "send_conflict")

    def setup_fix(self, **overrides):
        report = self.reports_root / "review.md"
        report.write_text("review evidence")
        meta = {"fix_mode": "same_task", "fix_round": 1, "review_source_task_id": "task1234",
                "review_report_path": str(report), "review_report_sha256": util.sha256_file(report), "review_key": "key1"}
        fix = {"state": "open", "report_path": str(report), "report_sha256": meta["review_report_sha256"], "review_key": "key1"}
        meta.update(overrides)
        plan = self.write_plan("Feedback", self.fixture["remote"], {
            **meta, "source_sha256": self.fixture["source_sha256"],
            "requirements_sha256": self.fixture["requirements_sha256"]})
        with db.write_txn(self.ctx.conn):
            task = db.require_task(self.ctx.conn, "task1234")
            db.set_extra(self.ctx.conn, task, self.ctx.now_iso, fix_request=fix)
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, plan_sha256=util.sha256_file(plan))
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, commits=[{"execution_run_id": "previous", "sha": self.delivery["head_sha"]}])
        self.fake.tasks["task1234"]["status"] = "in-review"

    def test_same_task_accepts_remote_review_without_marker(self):
        self.setup_fix()
        self.assertEqual(self.validate()["original_todo_slug"], "in-review")

    def test_same_task_mismatches(self):
        for overrides in ({"fix_round": 2}, {"review_source_task_id": "different"},
                          {"review_key": "wrong"}, {"review_report_sha256": "wrong"},
                          {"review_report_path": str(self.root / "outside.md")}):
            with self.subTest(overrides=overrides):
                self.setup_fix(**overrides)
                with self.assertRaises(dispatch_validate.DispatchCheckFailed):
                    self.validate()

    def test_mcp_ready_required_attach_not_required(self):
        client = Mock()
        client.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        config = {"backend": "pi_desktop", "connection_file": "/not-needed"}
        with patch.object(dispatch_validate.exec_config, "snapshot", return_value=config), \
             patch.object(dispatch_validate.exec_config, "check_available", return_value=(True, None)), \
             patch.object(dispatch_validate.mcp_client.MCPClient, "from_server_file", return_value=client), \
             patch("kanpilot.harness_plus_adapter.plus_capabilities", side_effect=AssertionError("attach probe called")):
            self.assertEqual(self.validate()["backend"], "pi_desktop")
            client.ping.assert_called_once()
            client.ping.side_effect = util.KanpilotError("validation_failed", "MCP unavailable")
            with self.assertRaises(dispatch_validate.DispatchCheckFailed) as caught:
                self.validate()
            self.assertIn("本机 MCP 服务未就绪", caught.exception.message)
