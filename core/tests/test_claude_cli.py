"""Claude adapter tests use local subprocess stubs, never a model service."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanpilot import claude_cli, codex_planner, util
from test_cli_review import runner_scene


class ClaudeCLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "claude-config"
        self.config.mkdir()
        isolated = {key: value for key, value in os.environ.items() if key not in claude_cli._USER_ENV_KEYS}
        isolated["CLAUDE_CONFIG_DIR"] = str(self.config)
        patch = mock.patch.dict(os.environ, isolated, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def write_settings(self, payload):
        path = self.config / "settings.json"
        path.write_text(json.dumps(payload))
        return path

    def test_child_env_inherits_only_global_auth_route_and_aliases(self):
        selected = {key: "test-allowed-" + key for key in claude_cli._USER_ENV_KEYS}
        excluded = {"PATH": "/untrusted", "BASH_ENV": "/untrusted-hook",
                    "NODE_OPTIONS": "--require unsafe.js", "CLAUDE_CODE_ENABLE_AGENT_TEAMS": "1",
                    "MCP_CONFIG": "untrusted", "UNRELATED_SETTINGS_ENV": "forbidden"}
        settings = self.write_settings({"env": dict(selected, **excluded),
                                       "hooks": {"PreToolUse": "ignored"}, "mcpServers": {"ignored": {}}})
        before = settings.read_bytes()
        with mock.patch.dict(os.environ, {"PATH": "existing-process-path"}):
            env = claude_cli.child_env()
        for key, value in selected.items():
            self.assertEqual(env[key], value)
        self.assertEqual(env["PATH"], "existing-process-path")
        for key in excluded:
            if key != "PATH":
                self.assertNotEqual(env.get(key), excluded[key])
        self.assertEqual(settings.read_bytes(), before)

    def test_explicit_process_environment_wins_including_empty_value(self):
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "configured-token-placeholder",
                                     "ANTHROPIC_BASE_URL": "configured-route-placeholder"}})
        with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "process-token-placeholder", "ANTHROPIC_BASE_URL": ""}):
            env = claude_cli.child_env()
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "process-token-placeholder")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "")

    def test_default_global_directory_ignores_project_settings(self):
        home = self.root / "isolated-home"
        global_config = home / ".claude"
        global_config.mkdir(parents=True)
        (global_config / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "global-model"}}))
        project = self.root / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "project-model"}}))
        (project / ".claude" / "settings.local.json").write_text("broken project settings")
        env_without_dir = dict(os.environ)
        env_without_dir.pop("CLAUDE_CONFIG_DIR")
        # Patch home resolution, without repurposing the real HOME variable.
        with mock.patch.dict(os.environ, env_without_dir, clear=True), \
                mock.patch.object(claude_cli.Path, "home", return_value=home), \
                mock.patch.object(claude_cli.Path, "cwd", return_value=project):
            env = claude_cli.child_env()
        self.assertEqual(env["ANTHROPIC_MODEL"], "global-model")

    def test_whitespace_directory_uses_default_and_relative_directory_fails(self):
        home = self.root / "isolated-home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "default-model"}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "  \t"}), \
                mock.patch.object(claude_cli.Path, "home", return_value=home):
            env = claude_cli.child_env()
        self.assertEqual(env["ANTHROPIC_MODEL"], "default-model")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        for value in (".claude", "project/.claude", "../settings"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": value}):
                with self.assertRaises(util.KanpilotError) as raised:
                    claude_cli.child_env()
                self.assertEqual(raised.exception.code, "validation_failed")

    def test_global_settings_symlink_is_supported_without_writing(self):
        target = self.root / "selected-global-settings.json"
        target.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "linked-model"}}))
        (self.config / "settings.json").symlink_to(target)
        before = target.read_bytes()
        self.assertEqual(claude_cli.child_env()["ANTHROPIC_MODEL"], "linked-model")
        self.assertEqual(target.read_bytes(), before)

    def test_missing_settings_is_allowed_and_invalid_settings_fail_closed(self):
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", claude_cli.child_env())
        path = self.config / "settings.json"
        for contents in ('{"env":{"ANTHROPIC_AUTH_TOKEN":"never-print-this', '[]', '{"env":[]}'):
            with self.subTest(contents=contents[:8]):
                path.write_text(contents)
                with self.assertRaises(util.KanpilotError) as raised:
                    claude_cli.child_env()
                self.assertEqual(raised.exception.code, "validation_failed")
                self.assertNotIn("never-print-this", str(raised.exception))
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": {"never-print-this": True}}})
        with self.assertRaises(util.KanpilotError) as raised:
            claude_cli.child_env()
        self.assertNotIn("never-print-this", str(raised.exception))

    def test_inherited_credentials_stay_out_of_argv_and_raw_log(self):
        token = "test-inherited-secret-0000"
        route = "https://route.invalid/private-route-token"
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": token, "ANTHROPIC_BASE_URL": route}})
        argv = claude_cli.build_argv(model="sonnet", effort="high", schema={}, budget_usd=1)
        self.assertNotIn(token, str(argv))
        self.assertNotIn(route, str(argv))
        result = self.run_frames([self.init(), self.result()],
            "print(os.environ['ANTHROPIC_AUTH_TOKEN'])\nprint(os.environ['ANTHROPIC_BASE_URL'])\n")
        self.assertFalse(result["is_error"])
        log = (self.root / "raw.jsonl").read_text()
        self.assertNotIn(token, log)
        self.assertNotIn(route, log)

    def test_invalid_settings_settles_planner_without_launching(self):
        import test_claude_planner as fixtures
        from kanpilot import db, planner_runner
        case = fixtures.ClaudePlannerTest("test_needs_input_keeps_draft")
        case.setUp()
        self.addCleanup(case.doCleanups)
        request_id = case.prepare()
        (self.config / "settings.json").write_text('{"env":{"ANTHROPIC_AUTH_TOKEN":"never-print-this')
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}), \
                mock.patch.object(claude_cli, "run") as process:
            self.assertTrue(planner_runner.run_one(case.db_path, request_id))
        process.assert_not_called()
        conn = db.connect(case.db_path)
        try:
            request = db.get_request(conn, request_id)
            self.assertEqual(request["status"], "failed")
            task = db.require_task(conn, case.task_id)
            self.assertIn("settings.json", task["failure_reason"])
            self.assertNotIn("never-print-this", task["failure_reason"])
        finally:
            conn.close()

    def run_frames(self, frames, suffix="", prompt="hello", schema=False):
        program = self.root / "stub.py"
        program.write_text("import json,sys,os\n"
            "open('stdin.txt','w').write(sys.stdin.read())\n"
            + "\n".join("print(" + repr(json.dumps(frame)) + ", flush=True)" for frame in frames)
            + "\n" + suffix)
        argv = [sys.executable, str(program)]
        if schema:
            argv += ["--json-schema", '{"type":"object"}']
        return claude_cli.run(argv, prompt=prompt,
            cwd=self.root, env=claude_cli.child_env(), timeout=2,
            log_path=self.root / "raw.jsonl")

    def init(self, tools=None):
        return {"type": "system", "subtype": "init", "session_id": "session-1",
                "tools": tools if tools is not None else ["Read", "Grep", "Glob"]}

    def result(self, **kwargs):
        return dict({"type": "result", "subtype": "success", "is_error": False,
            "structured_output": {"status": "ready"}, "total_cost_usd": 0.42,
            "session_id": "session-1"}, **kwargs)

    def test_argv_snapshot(self):
        with mock.patch.object(claude_cli.constants, "claude_bin", return_value="/claude"):
            argv = claude_cli.build_argv(model="sonnet", effort="high", schema={"type": "object"},
                                        budget_usd=3, add_dirs=("/inputs",))
        self.assertEqual(argv, ["/claude", "-p", "--output-format", "stream-json", "--verbose",
            "--restricted", "--tools", "Read,Grep,Glob", "--strict-mcp-config", "--permission-mode",
            "dontAsk", "--no-session-persistence", "--json-schema", '{"type":"object"}',
            "--model", "sonnet", "--effort", "high", "--max-budget-usd", "3.0", "--add-dir", "/inputs"])
        with self.assertRaises(util.KanpilotError):
            claude_cli.build_argv(model="sonnet", effort="high", schema={}, budget_usd=1, tools=("Bash",))
        with self.assertRaises(util.KanpilotError):
            claude_cli.build_argv(model="sonnet", effort="high", schema={}, budget_usd=1,
                                  tools=("Read", "StructuredOutput"))

    def test_success_stdin_and_cwd(self):
        result = self.run_frames([self.init(), self.result()], prompt="计划从 stdin 进入")
        self.assertFalse(result["is_error"])
        self.assertEqual(result["total_cost_usd"], .42)
        self.assertEqual((self.root / "stdin.txt").read_text(), "计划从 stdin 进入")
        self.assertEqual(result["session_id"], "session-1")

    def test_errors_budget_and_missing_output(self):
        for subtype in ("error_max_budget_usd", "error_during_execution"):
            with self.subTest(subtype=subtype):
                result = self.run_frames([self.init(), self.result(subtype=subtype, is_error=True)])
                self.assertTrue(result["is_error"])
                self.assertEqual(result["subtype"], subtype)
        result = self.run_frames([self.init(), self.result(structured_output=None)])
        self.assertIn("structured_output", result["error"])
        result = self.run_frames([self.result()])
        self.assertIn("init", result["error"])
        result = self.run_frames([self.init(), self.result(total_cost_usd="unknown")])
        self.assertIsNone(result["total_cost_usd"])
        self.assertIn("费用", result["error"])

    def test_tool_violation_terminates_before_side_effect(self):
        result = self.run_frames([self.init(["Read", "Bash"])],
            "import time\ntime.sleep(0.4)\nopen('forbidden','w').write('bad')\n")
        self.assertTrue(result["is_error"])
        self.assertIn("只读", result["error"])
        self.assertFalse((self.root / "forbidden").exists())

    def test_real_schema_init_allows_host_structured_output(self):
        init = {"type": "system", "subtype": "init", "session_id": "session-1",
                "tools": ["Glob", "Grep", "Read", "StructuredOutput"],
                "model": "claude-sonnet-5", "permissionMode": "dontAsk"}
        output = {"conclusion": "pass", "report_markdown": "只读核验完成", "fix": None}
        result = self.run_frames([init, self.result(structured_output=output)], schema=True)
        self.assertFalse(result["is_error"])
        self.assertEqual(result["tools"], init["tools"])
        self.assertEqual(result["structured_output"], output)
        self.assertEqual(result["exit_code"], 0)

    def test_structured_output_requires_schema_flag(self):
        result = self.run_frames([self.init(["Read", "StructuredOutput"]), self.result()])
        self.assertTrue(result["is_error"])
        self.assertIn("只读", result["error"])

    def test_schema_does_not_allow_write_or_mcp_tools(self):
        for tool in ("Bash", "Edit", "Write", "mcp__kaneo__post_comment", "Agent"):
            with self.subTest(tool=tool):
                result = self.run_frames([self.init(["Read", "StructuredOutput", tool])],
                    "import time\ntime.sleep(0.4)\nopen('forbidden','w').write('bad')\n", schema=True)
                self.assertTrue(result["is_error"])
                self.assertIn("只读", result["error"])
                self.assertFalse((self.root / "forbidden").exists())

    def test_non_json_logs_and_secret_redaction(self):
        with mock.patch.dict(claude_cli.os.environ, {"TEST_API_KEY": "test-secret-value-0000"}):
            result = self.run_frames([self.init(), self.result()], "print('plain diagnostic test-secret-value-0000')\n")
        self.assertFalse(result["is_error"])
        self.assertIn("plain diagnostic", (self.root / "raw.jsonl").read_text())
        self.assertNotIn("test-secret-value-0000", (self.root / "raw.jsonl").read_text())

    def test_timeout_and_nonzero(self):
        result = self.run_frames([self.init()], "import time\ntime.sleep(3)\n")
        self.assertEqual(result["subtype"], "timeout")
        self.assertTrue(result["is_error"])
        result = self.run_frames([self.init(), self.result()], "sys.exit(3)\n")
        self.assertEqual(result["exit_code"], 3)
        self.assertTrue(result["is_error"])

    def test_callback_failure_terminates_child(self):
        program = self.root / "sleep.py"
        program.write_text("import time\ntime.sleep(1)\nopen('forbidden','w').write('bad')\n")
        def stale(event):
            raise util.KanpilotError("stale_run", "lease lost")
        with self.assertRaises(util.KanpilotError):
            claude_cli.run([sys.executable, str(program)], prompt="", cwd=self.root,
                env=claude_cli.child_env(), timeout=2, log_path=self.root / "raw.jsonl", on_event=stale)
        self.assertFalse((self.root / "forbidden").exists())

    def test_codex_plan_cwd_priority(self):
        argv = codex_planner.launcher_argv({"plan_cwd": "/task/branch", "repository": "/repo"},
                                          Path("schema"), Path("out"), [])
        self.assertEqual(argv[argv.index("-C") + 1], "/task/branch")
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")


if __name__ == "__main__":
    unittest.main()


def test_invalid_settings_env_settles_review_without_launch(runner_scene, tmp_path, monkeypatch):
    from kanpilot import cli_review, review_config
    from unittest.mock import Mock
    config = tmp_path / "claude-config"
    config.mkdir()
    (config / "settings.json").write_text('{"env": ["never-print-this"]}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr(review_config, "snapshot", lambda _conn: {"backend": "claude", "model": "test", "effort": "high"})
    process = Mock()
    monkeypatch.setattr(claude_cli, "run", process)
    conn, _task, calls = runner_scene
    rc = cli_review.run_stage("review", conn=conn, db_path=tmp_path / "isolated.db", timeout=3,
                               log_path=tmp_path / "review.log", record_run=lambda **kwargs: None)
    assert rc == 1
    process.assert_not_called()
    reports = [payload for command, payload in calls if command == "report"]
    assert len(reports) == 1
    assert reports[0]["outcome"] == "needs_user"
    assert "settings.json.env" in reports[0]["note"]
    assert "never-print-this" not in str(reports)
