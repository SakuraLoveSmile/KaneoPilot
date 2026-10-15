"""Isolated real subprocess planning tests (fake Claude, fake Kaneo)."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
from test_plus_planner import PlusPlannerCase
from kanpilot import (claude_planner, cli, codex_planner, constants, db, git_delivery, pi_desktop_planner,
                       planner_config, planner_runner, util)


class ClaudePlannerTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("Feedback")
        self.task_id = "taskclaude000000000000001"
        self.pid = "example-project-05"
        self.call("set-project-mapping", {"project_id": self.pid,
            "repository": str(self.repo), "enabled": True, "expected_revision": 0})
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root", str(self.runs_root))
                planner_config.save(conn, {"backend": "claude", "claude": {"model": "test-sonnet", "effort": "high"}}, util.now_iso())
        finally:
            conn.close()

    def prepare(self, status="ready", questions=None):
        self.fake.add_task(self.task_id, self.pid, status="to-do", title="Claude 规划测试")
        info = self.call("prepare-plan-input", {"task_id": self.task_id})
        entry = self.call("start-plans", {"items": [{"input_id": info["input_id"],
            "input_sha256": info["input_sha256"], "planner_backend": "claude"}]})["results"][0]
        self.assertTrue(entry.get("request_id"), entry)
        output = {"status": status,
            "plan_markdown": "# 实施计划\n\n修改入口\n\n```verify\npython3 -m unittest\n```\n",
            "questions": (questions if questions is not None else ["确认目标版本"]) if status == "needs_input" else [], "evidence": []}
        stub = self.root / "claude"
        stub.write_text("#!" + sys.executable + "\nimport json,sys,os\n"
            "open(" + repr(str(self.root / "claude-stdin.txt")) + ",'w').write(sys.stdin.read())\n"
            "open(" + repr(str(self.root / "claude-cwd.txt")) + ",'w').write(os.getcwd())\n"
            "print(json.dumps({'type':'system','subtype':'init','tools':['Read','Grep','Glob'],'session_id':'fake-sid'}),flush=True)\n"
            "print(" + repr(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "structured_output": output, "total_cost_usd": .17, "session_id": "fake-sid"})) + ",flush=True)\n")
        stub.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"KANPILOT_CLAUDE_BIN": str(stub)})
        self.env.start()
        self.addCleanup(self.env.stop)
        return entry["request_id"]

    def test_runner_publishes_with_frozen_model_and_cost(self):
        request_id = self.prepare()
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, request_id)
            payload = db.request_payload(request)
            branch = Path(payload["plan_cwd"])
            self.assertEqual(payload["workspace_version"], 1)
        finally:
            conn.close()
        with mock.patch.object(cli, "run", wraps=cli.run) as calls:
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        costs = [c.args[1] for c in calls.call_args_list if c.args[0] == "record-cost"]
        self.assertEqual(len(costs), 1)
        self.assertEqual(costs[0]["cost_usd"], .17)
        self.assertEqual(costs[0]["source"], "claude")
        view = self.call("get-task", {"task_id": self.task_id})["task"]
        self.assertEqual(view["status"], "ready")
        meta, raw = util.read_frontmatter(Path(view["plan_path"]))
        self.assertEqual(meta["planner_model"], "test-sonnet")
        self.assertEqual(meta["reasoning_effort"], "high")
        self.assertIn(b"```verify", raw)
        self.assertTrue((self.root / "claude-stdin.txt").exists())
        self.assertFalse((branch / "claude-stdin.txt").exists())
        self.assertFalse((self.repo / "claude-stdin.txt").exists())
        cwd = Path((self.root / "claude-cwd.txt").read_text())
        self.assertEqual(cwd.name, "slot-1")
        self.assertNotEqual(cwd, branch.resolve())
        with db.connect(self.db_path) as conn:
            slot = conn.execute("SELECT * FROM worktree_slots WHERE path=?", (str(cwd),)).fetchone()
            self.assertEqual(slot["state"], "free")

    def test_missing_claude_binary_reports_failed_and_returns_unstarted_slot(self):
        request_id = self.prepare()
        with mock.patch.dict(os.environ, {"KANPILOT_CLAUDE_BIN": str(self.root / "missing-claude")}):
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        with db.connect(self.db_path) as conn:
            request = db.get_request(conn, request_id)
            self.assertEqual(request["status"], "failed")
            self.assertEqual(conn.execute("SELECT state FROM worktree_slots").fetchone()["state"], "free")
        self.assertIn("Claude 拉起失败", self.call("get-task", {"task_id": self.task_id})["task"]["failure_reason"])

    def test_needs_input_keeps_draft(self):
        request_id = self.prepare("needs_input")
        self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        view = self.call("get-task", {"task_id": self.task_id})["task"]
        self.assertEqual(view["status"], "needs_user")
        self.assertTrue(list(self.runs_root.rglob("plan-draft.md")))

    def test_structured_questions_are_kept_in_software_card(self):
        from test_plan_answers import QUESTION
        request_id = self.prepare("needs_input", questions=[QUESTION])
        self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        view = self.call("get-task", {"task_id": self.task_id})["task"]
        self.assertEqual(view["plan_clarification"]["questions"], [QUESTION])
        self.assertIn("播放全部", view["failure_reason"])
        self.assertNotIn("{'id'", view["failure_reason"])

    def test_claude_snapshot_and_configuration(self):
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(planner_config.model_snapshot(conn, "review_fix", backend="claude"),
                              {"planner_model": "test-sonnet", "reasoning_effort": "high"})
            self.assertIsNotNone(planner_config.spec_plan_blocked_reason("claude"))
            with self.assertRaises(util.KanpilotError):
                planner_config.save(conn, {"claude": {"model": "test", "effort": "ultra"}}, util.now_iso())
        finally:
            conn.close()

    def test_configuration_cli_roundtrip(self):
        saved = self.call("set-planner-config", {"backend": "claude",
            "claude": {"model": "sonnet", "effort": "xhigh"}})["config"]
        self.assertEqual(saved["claude"], {"model": "sonnet", "effort": "xhigh"})
        self.assertEqual(self.call("get-planner-config")["config"]["claude"], saved["claude"])
        error = self.call_err("set-planner-config", {"default_method": "spec_plan"})
        self.assertIn("只支持 builtin", error.message)
        self.assertEqual(self.call("get-planner-config")["config"]["default_method"], "builtin")

    def test_same_task_meta_overrides_model_frontmatter(self):
        context = {"task_id": self.task_id, "task": {"plan_kind": "standard"},
            "payload": {"fix_mode": "same_task", "fix_round": 2,
                "project_snapshot": {"project_id": self.pid},
                "review_report_path": "/reports/fix.md", "review_report_sha256": "abc",
                "review_key": "key", "review_source_task_id": "forged",
                "model_config": {"planner_model": "test", "reasoning_effort": "high"}}}
        meta = planner_runner.build_plan_meta(context, {}, util.now_iso())
        self.assertEqual(meta["fix_mode"], "same_task")
        self.assertEqual(meta["fix_round"], 2)
        self.assertEqual(meta["review_source_task_id"], self.task_id)
        self.assertEqual(meta["review_report_sha256"], "abc")
        path = planner_runner.write_plan_file(context, {}, "# 修复", util.now_iso())
        published, _ = util.read_frontmatter(path)
        self.assertEqual(published["fix_round"], 2)

    def test_budget_minimum_and_remaining(self):
        with mock.patch.object(claude_planner.core, "is_automation_managed", return_value=True), \
                mock.patch.object(claude_planner.automation_policy, "policy_for", return_value={}):
            for remaining, expected in ((None, 5), (9, 5), (2, 2), (.2, .5)):
                with mock.patch.object(claude_planner.core, "automation_budget",
                    return_value={"ok": True, "remaining_cost_usd": remaining}):
                    self.assertEqual(claude_planner.budget_for(None, {"project_id": self.pid}), expected)
            with mock.patch.object(claude_planner.core, "automation_budget", return_value={"ok": False}):
                with self.assertRaises(util.KanpilotError):
                    claude_planner.budget_for(None, {"project_id": self.pid})

    def test_prompt_contract(self):
        prompt = codex_planner.build_prompt({"fix_mode": "same_task"}, {"files": []}, None)
        for token in ("恰好一个 ```verify", "1–5", "专用分支/worktree", "工作区由核心准备", "工作树已含前几轮提交", "只修 Review", "verify 要覆盖修复点"):
            self.assertIn(token, prompt)

    def test_codex_and_claude_prompt_describes_partial_state_materials(self):
        manifest = {"files": [{"path": "files/partial.py", "repository_path": "partial.py"}],
            "execution_worktree": {"head_sha": "a" * 40, "state_sha256": "b" * 64,
                "changes": [{"path": "partial.py", "status": " M", "material_name": "partial.py"},
                            {"path": "obsolete.txt", "status": " D"},
                            {"path": ".env", "status": "??", "reason": "sensitive"}]}}
        prompt = codex_planner.build_prompt({}, manifest, Path("/input"))
        for token in ("基线快照不包含未提交改动", "execution_worktree.changes", "partial.py",
                      "obsolete.txt", "sensitive", "files[].repository_path", "当前保留内容"):
            self.assertIn(token, prompt)
        self.assertNotIn("{EXECUTION_WORKTREE_BLOCK}", prompt)
        # Claude builds its stdin prompt with the same shared renderer.
        with mock.patch.object(codex_planner, "build_prompt", wraps=codex_planner.build_prompt) as renderer:
            request_id = self.prepare("needs_input")
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        renderer.assert_called_once()

    def test_plus_uses_requested_commit_snapshot(self):
        (self.repo / "source.txt").write_text("committed baseline")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=test@example.com",
            "-c", "user.name=test", "commit", "-qm", "baseline"], check=True)
        sha = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        (self.repo / "source.txt").write_text("dirty primary checkout")
        run_dir = self.root / "plus-run"
        run_dir.mkdir()
        with db.connect(self.db_path) as conn:
            now = util.now_iso()
            conn.execute("INSERT INTO requests(request_id,idempotency_key,stage,kind,status,run_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         ("request", "fixture", "plan", "plan", "claimed", "run", now, now))
        instance = pi_desktop_planner._Round(self.db_path, "request", "run", task_id=self.task_id,
            context={"payload": {"delivery_head_sha": sha, "manifest": {"files": []}}},
            task={"repository": str(self.repo)}, run_dir=run_dir, timeout=2, heartbeat_every=1)
        worktree, mode, materials = instance._prepare_materials()
        self.assertEqual(worktree.name, "slot-1")
        self.assertEqual((worktree / "source.txt").read_text(), "committed baseline")
        self.assertEqual((self.repo / "source.txt").read_text(), "dirty primary checkout")
        self.assertEqual(subprocess.check_output(["git", "-C", str(worktree), "rev-parse",
            "--abbrev-ref", "HEAD"], text=True).strip(), "HEAD")
        # 缺失和未知快照直接渲染时不声称权限级别，asktool 禁令照常保留。
        for runtime in (None, {}, {"permission_mode": "unknown"}):
            with self.subTest(runtime=runtime):
                instance.runtime = runtime
                prompt = instance._render_prompt(materials)
                self.assertIn("恰好一个 ```verify", prompt)
                self.assertNotIn("{PERMISSION_LINE}", prompt)
                self.assertNotIn("权限为", prompt)
                self.assertIn("**禁止**调用 `asktool`", prompt)


class PlusPlanningSnapshotTest(PlusPlannerCase):
    def test_partial_replan_snapshot_reads_materials_and_deletion_manifest(self):
        repo = self.git_repo()
        (repo / "partial.py").write_text("baseline source\n")
        (repo / "obsolete.txt").write_text("baseline obsolete\n")
        subprocess.run(["git", "-C", str(repo), "add", "partial.py", "obsolete.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c",
                        "user.email=test@example.com", "commit", "-qm", "baseline files"], check=True)
        fixture = self.plus_request(repo=repo)
        task_id, request_id = fixture["task_id"], fixture["request_id"]
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        branch = self.worktrees_root / task_id / "branch"
        self.assertTrue(branch.is_dir())  # Created by the first formal planning submission.
        partial = b"partial changes retained after failure\n"
        (branch / "partial.py").write_bytes(partial)
        (branch / "obsolete.txt").unlink()
        entry = self.material_entry(partial, name="partial.py", media_type="text/plain", rel="files/partial.py")
        entry.update(source="execution_worktree", repository_path="partial.py")
        self.write_material_file(fixture["info"], entry["path"], partial)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                request = db.get_request(conn, request_id)
                payload = db.request_payload(request)
                manifest = payload["manifest"]
                manifest["files"] = [entry]
                manifest["execution_worktree"] = {"head_sha": sha, "state_sha256": "b" * 64,
                    "changes": [{"path": "partial.py", "status": " M", "material_name": "partial.py"},
                                {"path": "obsolete.txt", "status": " D"},
                                {"path": ".env", "status": "??", "reason": "sensitive"}]}
                payload.update(plan_cwd=str(branch), delivery_mode="pr", delivery_head_sha=sha,
                               planner_input_sha256=pi_desktop_planner.manifest_sha(manifest))
                payload.pop("workspace_version", None)  # Historical PR partial-material contract.
                db.update_request(conn, request_id, util.now_iso(), payload_json=payload)
        finally:
            conn.close()
        markdown = "# 根据保留改动继续施工\n\n```verify\npython3 -m unittest\n```\n"
        self.server.add_message("user", "partial replan")
        self.server.add_message("assistant", markdown)
        self.server.add_plan_proposal(markdown=markdown)
        observed = {}
        real_call = self.server.tool_call
        def observe(name, args):
            if name == "pi_agent_prompt":
                snapshot = Path(self.server.created["projectPath"])
                copied_manifest = next(snapshot.glob(".kaneopilot-plan-material/*/manifest.json"))
                observed["manifest"] = json.loads(copied_manifest.read_text())
                observed["partial"] = (copied_manifest.parent / "partial.py").read_bytes()
                observed["baseline"] = (snapshot / "partial.py").read_text()
                observed["deleted_baseline"] = (snapshot / "obsolete.txt").read_text()
            return real_call(name, args)
        with self.fast(), mock.patch.object(self.server, "tool_call", side_effect=observe):
            self.run_round()
        self.assertEqual(Path(self.server.created["projectPath"]).name, "slot-1")
        self.assertEqual(observed["partial"], partial)
        self.assertEqual(observed["baseline"], "baseline source\n")
        self.assertEqual(observed["deleted_baseline"], "baseline obsolete\n")
        changes = observed["manifest"]["execution_worktree"]["changes"]
        self.assertIn({"path": "obsolete.txt", "status": " D"}, changes)
        self.assertIn("基线快照不包含未提交改动", self.server.prompt_content)
        self.assertIn("工作树路径：partial.py", self.server.prompt_content)
        self.assertIn("sensitive", self.server.prompt_content)
        self.assertEqual((branch / "partial.py").read_bytes(), partial)
        self.assertFalse((branch / "obsolete.txt").exists())
        self.assertEqual(self.call("get-task", {"task_id": task_id})["task"]["status"], "ready")

    def manual_fixture(self, *, takeover=True, payload_cwd=None):
        fixture = self.plus_request()
        task_id, repo = fixture["task_id"], fixture["repo"]
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        branch = self.worktrees_root / task_id / "branch"
        self.assertTrue(branch.is_dir())  # Reuse core's existing branch for legacy takeover fixture.
        # Manual work may include both a newer commit and uncommitted files.
        (branch / "manual.txt").write_text("manual checkpoint")
        subprocess.run(["git", "-C", str(branch), "add", "manual.txt"], check=True)
        subprocess.run(["git", "-C", str(branch), "-c", "user.name=test", "-c",
                        "user.email=test@example.com", "commit", "-qm", "manual checkpoint"], check=True)
        (branch / "AGENTS.md").write_text("# dirty manual changes\n")
        (branch / "notes.txt").write_text("untracked manual notes")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                db.set_extra(conn, task, util.now_iso(), automation={"manual_takeover": takeover},
                    delivery={"mode": "manual", "branch": "kaneo/" + task_id,
                              "worktree_path": str(branch), "head_sha": sha})
                request = db.get_request(conn, fixture["request_id"])
                payload = db.request_payload(request)
                payload.pop("workspace_version", None)  # This fixture tests the historical takeover contract.
                payload.update(delivery_mode="manual", plan_cwd=str(payload_cwd or branch),
                               delivery_head_sha=sha)
                db.update_request(conn, fixture["request_id"], util.now_iso(), payload_json=payload)
        finally:
            conn.close()
        return fixture, branch

    def test_manual_takeover_uses_registered_dirty_branch_and_publishes(self):
        fixture, branch = self.manual_fixture()
        markdown = "# 手动接管计划\n\n```verify\npython3 -m unittest\n```\n"
        self.server.add_message("user", "manual plan")
        self.server.add_message("assistant", markdown)
        self.server.add_plan_proposal(markdown=markdown)
        observed = {}
        real_call = self.server.tool_call
        def observe(name, args):
            if name == "pi_session_create":
                cwd = Path(args["projectPath"])
                observed["tracked"] = (cwd / "AGENTS.md").read_text()
                observed["untracked"] = (cwd / "notes.txt").read_text()
                observed["committed"] = (cwd / "manual.txt").read_text()
            return real_call(name, args)
        with self.fast(), mock.patch.object(self.server, "tool_call", side_effect=observe), \
                mock.patch.object(git_delivery, "prepare_snapshot_worktree", wraps=git_delivery.prepare_snapshot_worktree) as snapshots:
            self.run_round()
        snapshots.assert_not_called()
        self.assertEqual(self.server.created["projectPath"], str(branch))
        self.assertEqual(self.server.created["mode"], "plan")
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        self.assertEqual(observed, {"tracked": "# dirty manual changes\n", "untracked": "untracked manual notes",
                                   "committed": "manual checkpoint"})
        self.assertEqual((branch / "AGENTS.md").read_text(), observed["tracked"])
        self.assertEqual((branch / "notes.txt").read_text(), observed["untracked"])
        self.assertEqual((branch / "manual.txt").read_text(), observed["committed"])
        self.assertEqual((fixture["repo"] / "AGENTS.md").read_text(), "# repo\n")
        self.assertIn("含未提交改动", self.server.prompt_content)
        self.assertEqual(self.call("get-task", {"task_id": fixture["task_id"]})["task"]["status"], "ready")

    def test_manual_payload_requires_actual_takeover(self):
        fixture, _branch = self.manual_fixture(takeover=False)
        with self.fast():
            self.run_round()
        self.assertIsNone(self.server.created)
        view = self.call("get-task", {"task_id": fixture["task_id"]})["task"]
        self.assertIn("登记不符", view["failure_reason"])

    def test_manual_payload_cannot_select_unregistered_cwd(self):
        fixture, _branch = self.manual_fixture(payload_cwd=self.root)
        with self.fast():
            self.run_round()
        self.assertIsNone(self.server.created)
        view = self.call("get-task", {"task_id": fixture["task_id"]})["task"]
        self.assertIn("登记不符", view["failure_reason"])

    def test_manual_materials_cannot_escape_scope_through_symlink(self):
        fixture, branch = self.manual_fixture()
        outside = self.root / "outside-materials"
        outside.mkdir()
        (branch / ".kaneopilot-plan-material").symlink_to(outside, target_is_directory=True)
        with self.fast():
            self.run_round()
        self.assertIsNone(self.server.created)
        self.assertEqual(list(outside.iterdir()), [])
        view = self.call("get-task", {"task_id": fixture["task_id"]})["task"]
        self.assertIn("outside allowed root", view["failure_reason"])

    def test_snapshot_cwd_passes_runtime_scope_and_publishes(self):
        fixture = self.plus_request()
        sha = subprocess.check_output(["git", "-C", str(fixture["repo"]), "rev-parse", "HEAD"], text=True).strip()
        conn = db.connect(self.db_path)
        try:
            payload = db.request_payload(db.get_request(conn, fixture["request_id"]))
            payload["delivery_head_sha"] = sha
            conn.execute("UPDATE requests SET payload_json=? WHERE request_id=?",
                         (util.jdump(payload), fixture["request_id"]))
            conn.commit()
        finally:
            conn.close()
        markdown = "# 计划\n\n```verify\npython3 -m unittest\n```\n"
        self.server.add_message("user", "plan")
        self.server.add_message("assistant", markdown)
        self.server.add_plan_proposal(markdown=markdown)
        with self.fast():
            self.run_round()
        self.assertEqual(Path(self.server.created["projectPath"]).name, "slot-1")
        self.assertEqual(self.call("get-task", {"task_id": fixture["task_id"]})["task"]["status"], "ready")
