"""Qoder adapter checks use isolated local processes, never model services."""
import json
import errno
import os
from pathlib import Path
import sys
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from kanpilot import qoder_cli, util


class QoderCLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def init(self, **values):
        return dict({"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob"],
                     "session_id": "qoder-session", "mcp_servers": [], "plugins": [], "skills": [],
                     "permissionMode": "dont_ask"}, **values)

    def result(self, **values):
        return dict({"type": "result", "subtype": "success", "is_error": False,
                     "session_id": "qoder-session", "result": '{"status":"ready"}'}, **values)

    def run_frames(self, frames, suffix="", *, prompt="请规划", timeout=2, on_event=None, env=None):
        program = self.root / "stub.py"
        program.write_text("import json,sys,os,time\n"
            "open('stdin.txt','w').write(sys.stdin.read())\n"
            + "\n".join("print(" + repr(json.dumps(frame)) + ", flush=True)" for frame in frames)
            + "\n" + suffix, encoding="utf-8")
        return qoder_cli.run([sys.executable, str(program)], prompt=prompt,
            schema={"type": "object", "properties": {"status": {"type": "string"}}},
            cwd=self.root, env=env or qoder_cli.child_env(), timeout=timeout,
            log_path=self.root / "raw.jsonl", on_event=on_event)

    def test_safe_argv(self):
        with mock.patch.object(qoder_cli.constants, "qoder_bin", return_value="/qoder"):
            argv = qoder_cli.build_argv(model="model-a", add_dirs=("/inputs",))
            effort = qoder_cli.build_argv(model="model-a", reasoning_effort="high")
        self.assertEqual(argv[:5], ["/qoder", "--print", "--output-format", "stream-json", "--tools"])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dont_ask")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]),
                         {"disableAllHooks": True, "general": {"enableAutoUpdate": False},
                          "skills": {"enabled": False, "loadFromAgentsDirectory": False}})
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--max-model-request-retries") + 1], "0")
        self.assertEqual(argv[-2:], ["--add-dir", "/inputs"])
        self.assertNotIn("--reasoning-effort", argv)
        self.assertEqual(effort[-2:], ["--reasoning-effort", "high"])
        self.assertNotIn("--json-schema", argv)

    def test_cli_discovery_with_finder_path_and_explicit_precedence(self):
        path_bin = self.root / "path-bin"
        path_bin.mkdir()
        legacy = self.root / ".local" / "bin" / "qodercli"
        entry = self.root / ".qoder" / "entry" / "qoder"

        def executable(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)

        with mock.patch.object(qoder_cli.constants, "_home", return_value=self.root), \
                mock.patch.dict(os.environ, {"PATH": str(path_bin)}, clear=True):
            self.assertEqual(qoder_cli.constants.qoder_bin(), "qoder")
            executable(legacy)
            self.assertEqual(qoder_cli.constants.qoder_bin(), str(legacy))
            entry.parent.mkdir(parents=True)
            entry.write_text("not executable")
            entry.chmod(0o644)
            self.assertEqual(qoder_cli.constants.qoder_bin(), str(legacy))
            executable(entry)
            self.assertEqual(qoder_cli.constants.qoder_bin(), str(entry))
            executable(path_bin / "qodercli")
            self.assertEqual(qoder_cli.constants.qoder_bin(), str(path_bin / "qodercli"))
            executable(path_bin / "qoder")
            self.assertEqual(qoder_cli.constants.qoder_bin(), str(path_bin / "qoder"))
            os.environ["KANPILOT_QODER_BIN"] = "/explicit/qoder"
            self.assertEqual(qoder_cli.constants.qoder_bin(), "/explicit/qoder")

    def test_stdin_cwd_schema_and_unknown_cost(self):
        result = self.run_frames([self.init(), self.result()], prompt="只读计划")
        self.assertFalse(result["is_error"], result)
        self.assertEqual(result["structured_output"], {"status": "ready"})
        self.assertEqual(result["session_id"], "qoder-session")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["total_cost_usd"])
        sent = (self.root / "stdin.txt").read_text()
        self.assertTrue(sent.startswith("只读计划\n"))
        self.assertIn('"properties":{"status":{"type":"string"}}', sent)
        self.assertIn("JSON 对象", sent)
        result = self.run_frames([self.init(permissionMode="dontAsk"), self.result()])
        self.assertFalse(result["is_error"], result)

    def test_only_model_usage_dollars_are_counted(self):
        for usage, expected in (({"a": {"costUSD": .2}, "b": {"costUSD": .3}}, .5),
                                ({"a": {"costUSD": 0}}, 0), ({}, None),
                                ({"a": {"costUSD": .1}, "b": {}}, None),
                                ({"a": {"costUSD": -1}}, None),
                                ({"a": {"costUSD": True}}, None),
                                ({"a": {"costUSD": "0.2"}}, None)):
            with self.subTest(usage=usage):
                result = self.run_frames([self.init(), self.result(modelUsage=usage, total_credits=4,
                                                                   total_cost_usd=99)])
                self.assertFalse(result["is_error"], result)
                self.assertEqual(result["total_cost_usd"], expected)
        self.assertIsNone(qoder_cli._cost({"a": {"costUSD": float("inf")}}))
        self.assertIsNone(qoder_cli._cost({"a": {"costUSD": float("nan")}}))
        self.assertIsNone(qoder_cli._cost({"a": {"costUSD": 10 ** 1000}}))

    def test_result_requires_exact_json_object_without_assistant_fallback(self):
        assistant = {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"status":"ready"}'}]}}
        for output in ("bad", 'prefix {"status":"ready"}', '```json\n{"status":"ready"}\n```',
                       '{"status":"ready"} trailing', '[]', 'null', '{"n":NaN}', None, {"status": "ready"}):
            with self.subTest(output=output):
                result = self.run_frames([self.init(), assistant, self.result(result=output)])
                self.assertTrue(result["is_error"])
                self.assertIsNone(result["structured_output"])
                self.assertIn("JSON", result["error"])
        result = self.run_frames([self.init(), assistant])
        self.assertTrue(result["is_error"])
        self.assertIn("result", result["error"])

    def test_terminal_contract_and_exit_code(self):
        for frames in ([self.result()], [self.init()], [],
                       [self.init(), self.result(is_error=True)],
                       [self.init(), self.result(is_error=None)],
                       [self.init(), self.result(subtype="error_max_turns", errors=["turn limit"])],
                       [self.init(), self.result(subtype="error_during_execution", errors=["failed"])],
                       [self.init(), self.result(), self.result()]):
            with self.subTest(frames=frames):
                result = self.run_frames(frames)
                self.assertTrue(result["is_error"])
                self.assertTrue(result["error"])
        result = self.run_frames([self.init(), self.result()], "sys.exit(7)\n")
        self.assertTrue(result["is_error"])
        self.assertEqual(result["exit_code"], 7)

    def test_init_rejects_dangerous_tools_and_extensions_before_side_effect(self):
        invalid = [self.init(tools=["Read", tool]) for tool in
                   ("Bash", "Edit", "Write", "Agent", "StructuredOutput", "mcp__server__tool")]
        invalid += [self.init(tools=None), self.init(tools="Read"), self.init(permissionMode="auto")]
        invalid += [self.init(**{key: [{"name": "unexpected"}]}) for key in
                    ("mcp_servers", "plugins", "skills", "hooks")]
        for frame in invalid:
            with self.subTest(frame=frame):
                result = self.run_frames([frame], "time.sleep(.3)\nopen('forbidden','w').write('bad')\n")
                self.assertTrue(result["is_error"])
                self.assertFalse((self.root / "forbidden").exists())

    def test_parse_before_redaction_and_redact_logs_callbacks_outputs(self):
        secret = "test-secret-value-0000"
        env = qoder_cli.child_env()
        env["QODER_TEST_API_KEY"] = secret
        events = []
        result = self.run_frames([self.init(), self.result(result=json.dumps({"token": secret, "status": "ready"}))],
                                 "print('diagnostic " + secret + "')\n", env=env, on_event=events.append)
        self.assertFalse(result["is_error"], result)
        self.assertEqual(result["structured_output"]["status"], "ready")
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, json.dumps(events))
        self.assertNotIn(secret, (self.root / "raw.jsonl").read_text())
        failed = self.run_frames([self.init(), self.result(is_error=True, errors=[secret])], env=env)
        self.assertNotIn(secret, failed["error"])
        env["QODER_TEST_API_KEY"] = "tinykey"
        result = self.run_frames([self.init(), self.result(result='{"status":"tinykey"}')], env=env)
        self.assertFalse(result["is_error"])
        self.assertNotIn("tinykey", json.dumps(result))
        self.assertNotIn("tinykey", (self.root / "raw.jsonl").read_text())

    def test_timeout_after_stdout_eof_is_bounded_and_pulses_continue(self):
        pulses = []
        started = time.monotonic()
        result = self.run_frames([self.init()],
            "os.close(1)\nos.close(2)\ntime.sleep(2)\nopen('forbidden','w').write('bad')\n",
            timeout=.4, on_event=pulses.append)
        self.assertEqual(result["subtype"], "timeout")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertGreater(pulses.count(None), 1)
        self.assertFalse((self.root / "forbidden").exists())

    def test_lease_loss_kills_and_reaps_process_group(self):
        program = self.root / "tree.py"
        program.write_text("import subprocess,sys,time,os\n"
            "child=subprocess.Popen([sys.executable,'-c',\"import time;time.sleep(.5);open('forbidden','w').write('bad')\"])\n"
            "open('pid','w').write(str(os.getpid()))\n"
            "time.sleep(3)\n")
        def stale(event):
            if (self.root / "pid").exists():
                raise util.KanpilotError("stale_run", "lease lost")
        with self.assertRaises(util.KanpilotError):
            qoder_cli.run([sys.executable, str(program)], prompt="", schema={}, cwd=self.root,
                env=qoder_cli.child_env(), timeout=2, log_path=self.root / "raw.jsonl", on_event=stale)
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        time.sleep(.6)
        self.assertFalse((self.root / "forbidden").exists())

    def test_large_stdin_does_not_block_stream_or_timeout(self):
        program = self.root / "blocked.py"
        program.write_text("import time\ntime.sleep(3)\n")
        started = time.monotonic()
        result = qoder_cli.run([sys.executable, str(program)], prompt="x" * 500000, schema={},
            cwd=self.root, env=qoder_cli.child_env(), timeout=.3, log_path=self.root / "raw.jsonl")
        self.assertEqual(result["subtype"], "timeout")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_lease_loss_kills_descendant_after_leader_exits(self):
        program = self.root / "orphan.py"
        program.write_text("import subprocess,sys\n"
            "subprocess.Popen([sys.executable,'-c',\"import time;time.sleep(.7);open('forbidden','w').write('bad')\"])\n"
            "open('started','w').write('yes')\n")
        pulses = []
        def stale(event):
            pulses.append(event)
            if len(pulses) > 2:
                raise util.KanpilotError("stale_run", "lease lost")
        with self.assertRaises(util.KanpilotError):
            qoder_cli.run([sys.executable, str(program)], prompt="", schema={}, cwd=self.root,
                env=qoder_cli.child_env(), timeout=2, log_path=self.root / "raw.jsonl", on_event=stale)
        self.assertTrue((self.root / "started").exists())
        time.sleep(.8)
        self.assertFalse((self.root / "forbidden").exists())

    def test_cleanup_eperm_reaps_and_rechecks_without_losing_result(self):
        # XNU can report EPERM when only an unreaped zombie remains in a group.
        # A successful reap alone is insufficient: the second signal must prove
        # the group disappeared before the original model result is preserved.
        real_popen = subprocess.Popen
        processes = []
        def launch(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            proc.wait = mock.Mock(wraps=proc.wait)
            processes.append(proc)
            return proc
        with mock.patch.object(qoder_cli.subprocess, "Popen", side_effect=launch), \
                mock.patch.object(qoder_cli.os, "killpg", side_effect=[
                PermissionError(errno.EPERM, "synthetic zombie race"),
                ProcessLookupError(errno.ESRCH, "synthetic reaped group")]) as killpg:
            result = self.run_frames([self.init(), self.result()])
        self.assertFalse(result["is_error"], result)
        self.assertEqual(result["structured_output"], {"status": "ready"})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(killpg.call_args_list[0], killpg.call_args_list[1])
        processes[0].wait.assert_called_once_with(timeout=.25)

    def test_cleanup_eperm_with_live_group_remains_explicit_failure(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                cwd=self.root, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        permission_error = PermissionError(errno.EPERM, "synthetic live group denial")
        try:
            with mock.patch.object(qoder_cli.os, "killpg", side_effect=permission_error), \
                    mock.patch.object(proc, "wait", wraps=proc.wait) as wait:
                with self.assertRaises(PermissionError) as raised:
                    qoder_cli._signal_group(proc, signal.SIGTERM)
                self.assertIs(raised.exception, permission_error)
                wait.assert_called_once_with(timeout=.25)
            self.assertIsNone(proc.poll())
            os.killpg(proc.pid, 0)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)

    def test_map_events(self):
        self.assertEqual(qoder_cli.map_event(self.init())["type"], "session")
        self.assertEqual(qoder_cli.map_event(self.result())["type"], "turn_end")
        event = qoder_cli.map_event({"type": "assistant", "timestamp": "now", "message": {
            "content": [{"type": "text", "text": "说明"}, {"type": "tool_use"}]}})
        self.assertEqual(event, {"type": "assistant_message", "ts": "now", "content": "说明"})
        self.assertIsNone(qoder_cli.map_event({"type": "other"}))

    def test_capabilities_checks_local_flags_only(self):
        cli = self.root / "qoder"
        record = self.root / "probes.jsonl"
        cli.write_text("#!" + sys.executable + "\nimport sys,json\n"
            + "with open(" + repr(str(record)) + ",'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            + "print('1.1.74' if '--version' in sys.argv else " + repr(" ".join(qoder_cli._REQUIRED_FLAGS)) + ")\n")
        cli.chmod(0o755)
        with mock.patch.object(qoder_cli.constants, "qoder_bin", return_value=str(cli)):
            caps = qoder_cli.capabilities()
        self.assertTrue(caps["available"], caps)
        self.assertEqual(caps["version"], "1.1.74")
        self.assertEqual([json.loads(line) for line in record.read_text().splitlines()],
                         [["--version"], ["--help"]])
        for flag in qoder_cli._REQUIRED_FLAGS:
            cli.write_text("#!" + sys.executable + "\nimport sys\nprint('1.1.74' if '--version' in sys.argv else "
                           + repr(" ".join(f for f in qoder_cli._REQUIRED_FLAGS if f != flag)) + ")\n")
            with self.subTest(flag=flag), mock.patch.object(qoder_cli.constants, "qoder_bin", return_value=str(cli)):
                caps = qoder_cli.capabilities()
                self.assertFalse(caps["available"])
                self.assertIn(flag, caps["detail"])
        with mock.patch.object(qoder_cli.constants, "qoder_bin", return_value=str(self.root / "missing")):
            caps = qoder_cli.capabilities()
        self.assertFalse(caps["available"])
        self.assertIn("KANPILOT_QODER_BIN", caps["detail"])

    def test_capabilities_rejects_old_or_unparseable_versions(self):
        cli = self.root / "qoder"
        for version in ("1.1.73", "0.9.99", "unknown", ""):
            cli.write_text("#!" + sys.executable + "\nprint(" + repr(version) + ")\n")
            cli.chmod(0o755)
            with self.subTest(version=version), mock.patch.object(qoder_cli.constants, "qoder_bin", return_value=str(cli)):
                caps = qoder_cli.capabilities()
                self.assertFalse(caps["available"])
                self.assertIn("1.1.74", caps["detail"])


class QoderProviderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.patch = mock.patch.object(qoder_cli, "_settings_path", return_value=self.settings)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def provider(self, key="synthetic-provider-key-0000"):
        return {"type": "openai-compatible", "protocol": "openai", "authType": "bearer",
                "baseUrl": "https://models.example.invalid/v1", "apiKey": key, "model": "other-model",
                "models": [{"model": "org/model", "displayName": "Synthetic model", "contextWindow": 128000,
                            "maxOutputTokens": 8192, "maxTokensField": "max_completion_tokens",
                            "capabilities": {"tools": True, "vision": False, "cacheControl": True,
                                "thinking": {"modes": ["enabled"], "supportedEffortLevels": ["high", "medium", "max"],
                                             "supportsEffort": True}, "unknown": "drop"},
                            "preferences": {"model": "other-model"}, "hooks": {"run": "bad"}},
                           {"model": "other-model"}],
                "routing": {"default": "other-model"}, "hooks": {"run": "bad"}, "tools": ["Bash"]}

    def write(self, provider=None):
        settings = {"providers": {"selected": self.provider() if provider is None else provider,
                                  "unselected": {"apiKey": "other-provider-secret-000"}},
                    "routing": {"default": "unselected"}, "hooks": {"onStart": "bad"},
                    "mcpServers": {"unsafe": {}}, "skills": ["unsafe"], "plugins": ["unsafe"]}
        self.settings.write_text(json.dumps(settings))
        return settings

    def test_projects_one_provider_and_upstream_with_slash_without_mutation(self):
        original = self.write()
        before = self.settings.read_bytes()
        env_before = dict(os.environ)
        argv, env = qoder_cli.prepare_launch(model="selected/org/model", reasoning_effort="high", add_dirs=("/inputs",))
        self.assertEqual(os.environ, env_before)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(env[qoder_cli._PROVIDER_API_KEY_ENV], original["providers"]["selected"]["apiKey"])
        self.assertNotIn(env[qoder_cli._PROVIDER_API_KEY_ENV], json.dumps(argv))
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(set(settings), {"disableAllHooks", "general", "skills", "providers"})
        self.assertTrue(settings["disableAllHooks"])
        self.assertFalse(settings["general"]["enableAutoUpdate"])
        self.assertEqual(settings["skills"], {"enabled": False, "loadFromAgentsDirectory": False})
        self.assertEqual(set(settings["providers"]), {"selected"})
        provider = settings["providers"]["selected"]
        self.assertEqual(set(provider), {"type", "protocol", "authType", "baseUrl", "model", "models", "apiKey"})
        self.assertEqual(provider["model"], "org/model")
        self.assertEqual(provider["apiKey"], "${KANPILOT_QODER_PROVIDER_API_KEY}")
        self.assertEqual(len(provider["models"]), 1)
        metadata = provider["models"][0]
        self.assertEqual(metadata["model"], "org/model")
        self.assertNotIn("preferences", metadata)
        self.assertNotIn("hooks", metadata)
        self.assertEqual(set(metadata["capabilities"]), {"tools", "vision", "cacheControl", "thinking"})
        self.assertEqual(metadata["capabilities"]["thinking"], original["providers"]["selected"]["models"][0]["capabilities"]["thinking"])
        self.assertEqual(argv[argv.index("--model") + 1], "selected/org/model")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[-4:], ["--reasoning-effort", "high", "--add-dir", "/inputs"])

    def test_builtin_models_do_not_load_user_providers(self):
        for model in ("auto", "efficient", "short-name"):
            with self.subTest(model=model), mock.patch.object(self.settings.__class__, "read_text", side_effect=AssertionError("must not read")):
                argv, env = qoder_cli.prepare_launch(model=model)
                self.assertNotIn("providers", json.loads(argv[argv.index("--settings") + 1]))
                self.assertEqual(argv[argv.index("--model") + 1], model)

    def test_missing_invalid_ambiguous_or_unselected_settings_fail_generically(self):
        for raw in (None, "not-json synthetic-provider-key-0000", "[]", '{"providers":[]}',
                    json.dumps({"providers": {}})):
            if raw is None:
                self.settings.unlink(missing_ok=True)
            else:
                self.settings.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(util.KanpilotError) as raised:
                qoder_cli.prepare_launch(model="selected/org/model")
            self.assertNotIn("synthetic-provider-key-0000", str(raised.exception))
        self.write()
        for model in ("selected/", "/org/model", "missing/org/model", "selected/missing-model", "selected/model"):
            with self.subTest(model=model), self.assertRaises(util.KanpilotError):
                qoder_cli.prepare_launch(model=model)
        provider = self.provider()
        provider["models"].append(provider["models"][0].copy())
        self.write(provider)
        with self.assertRaises(util.KanpilotError):
            qoder_cli.prepare_launch(model="selected/org/model")

    def test_environment_references_are_resolved_once_without_eval(self):
        cases = (("$SYNTHETIC_KEY", {"SYNTHETIC_KEY": "resolved-secret-000"}, "resolved-secret-000"),
                 ("${SYNTHETIC_KEY}", {"SYNTHETIC_KEY": "resolved-secret-001"}, "resolved-secret-001"),
                 ("${SYNTHETIC_MISSING:-fallback-secret-000}", {}, "fallback-secret-000"),
                 ("${SYNTHETIC_KEY:-fallback-secret-001}", {"SYNTHETIC_KEY": ""}, "fallback-secret-001"))
        for reference, values, expected in cases:
            self.write(self.provider(reference))
            with self.subTest(reference=reference), mock.patch.dict(os.environ, values, clear=True):
                argv, env = qoder_cli.prepare_launch(model="selected/org/model")
                self.assertEqual(env[qoder_cli._PROVIDER_API_KEY_ENV], expected)
                self.assertNotIn(expected, json.dumps(argv))
                self.assertNotIn(reference, json.dumps(argv))
                self.assertNotIn(qoder_cli._PROVIDER_API_KEY_ENV, os.environ)
        for reference, values in (("$SYNTHETIC_MISSING", {}), ("${SYNTHETIC_MISSING}", {}),
                                  ("${SYNTHETIC_KEY}", {"SYNTHETIC_KEY": ""}),
                                  ("${SYNTHETIC_KEY}", {"SYNTHETIC_KEY": "$NESTED_KEY"}),
                                  ("${SYNTHETIC_MISSING:-$NESTED_KEY}", {}), ("", {}), (None, {})):
            self.write(self.provider(reference))
            with self.subTest(reference=reference), mock.patch.dict(os.environ, values, clear=True), self.assertRaises(util.KanpilotError):
                qoder_cli.prepare_launch(model="selected/org/model")

    def test_rejects_unsupported_auth_and_credential_in_other_fields(self):
        providers = []
        for field, value in (("type", "anthropic"), ("protocol", "anthropic"), ("authType", "none"),
                             ("baseUrl", "https://user:password@models.example.invalid/v1"),
                             ("baseUrl", "https://models.example.invalid/v1?key=synthetic-provider-key-0000"),
                             ("baseUrl", "https://models.example.invalid/v1#fragment"),
                             ("baseUrl", "https://models.example.invalid/synthetic-provider-key-0000")):
            provider = self.provider()
            provider[field] = value
            providers.append(provider)
        provider = self.provider()
        provider["models"][0]["displayName"] = provider["apiKey"]
        providers.append(provider)
        provider = self.provider("${SYNTHETIC_KEY}")
        provider["models"][0]["displayName"] = "${SYNTHETIC_KEY}"
        providers.append(provider)
        for provider in providers:
            self.write(provider)
            with self.subTest(provider=provider), mock.patch.dict(os.environ, {"SYNTHETIC_KEY": "resolved-secret-000"}), self.assertRaises(util.KanpilotError) as raised:
                qoder_cli.prepare_launch(model="selected/org/model")
            self.assertNotIn("synthetic-provider-key-0000", str(raised.exception))
            self.assertNotIn("resolved-secret-000", str(raised.exception))

    def test_invalid_model_metadata_fails_before_launch(self):
        for field, value in (("displayName", {}), ("contextWindow", True), ("maxOutputTokens", -1),
                             ("maxTokensField", []), ("capabilities", []),
                             ("capabilities", {"thinking": True}), ("capabilities", {"tools": "yes"})):
            provider = self.provider()
            provider["models"][0][field] = value
            self.write(provider)
            with self.subTest(field=field, value=value), self.assertRaises(util.KanpilotError):
                qoder_cli.prepare_launch(model="selected/org/model")

    def test_settings_path_respects_non_china_overrides(self):
        # Stop only this test's path patch; every value below is synthetic.
        self.patch.stop()
        with mock.patch.object(qoder_cli.Path, "home", return_value=self.root):
            self.assertEqual(qoder_cli._settings_path({}), self.root / ".qoder" / "settings.json")
            self.assertEqual(qoder_cli._settings_path({"QODER_CONFIG_DIR": str(self.root / "direct")}), self.root / "direct" / "settings.json")
            self.assertEqual(qoder_cli._settings_path({"QODER_CLI_HOME": str(self.root / "cli"), "GEMINI_CLI_HOME": str(self.root / "other")}), self.root / "cli" / ".qoder" / "settings.json")
            self.assertEqual(qoder_cli._settings_path({"GEMINI_CLI_HOME": str(self.root / "gemini"), "QODER_CONFIG_DIR_NAME": ".custom"}), self.root / "gemini" / ".custom" / "settings.json")
            for env in ({"QODER_CONFIG_DIR": "relative"}, {"QODER_CLI_HOME": "relative"},
                        {"QODERCN_CONFIG_DIR_NAME": ".qodercn"}, {"QODER_CONFIG_DIR_NAME": "../other"},
                        {"QODER_CLI_HOME": ""}, {"QODER_CONFIG_DIR": ""}):
                with self.subTest(env=env), self.assertRaises(util.KanpilotError):
                    qoder_cli._settings_path(env)

    def test_provider_key_only_in_child_env_and_redacted_from_every_output(self):
        fake = self.root / "fake.py"
        fake.write_text("import json,os,sys\n"
            "sys.stdin.read()\n"
            "settings=sys.argv[sys.argv.index('--settings')+1]\n"
            "open('cli-temp-settings.json','w').write(settings)\n"
            "key=os.environ['KANPILOT_QODER_PROVIDER_API_KEY']\n"
            "print(json.dumps({'type':'system','subtype':'init','tools':['Read'],'session_id':'synthetic'}),flush=True)\n"
            "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':key}]}}),flush=True)\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps({'message':key})}),flush=True)\n"
            "print('diagnostic '+key,flush=True)\n")
        for key in ("synthetic-provider-secret-000", "tinykey"):
            self.write(self.provider(key))
            argv, env = qoder_cli.prepare_launch(model="selected/org/model")
            events = []
            result = qoder_cli.run([sys.executable, str(fake)] + argv[1:], prompt="read only", schema={},
                cwd=self.root, env=env, timeout=2, log_path=self.root / "provider-log.jsonl", on_event=events.append)
            self.assertFalse(result["is_error"], result)
            for text in (json.dumps(argv), json.dumps(result), json.dumps(events),
                         (self.root / "provider-log.jsonl").read_text(), (self.root / "cli-temp-settings.json").read_text()):
                self.assertNotIn(key, text)
            failure = self.root / "failure.py"
            failure.write_text("import json,os,sys\nsys.stdin.read()\n"
                "print(json.dumps({'type':'system','subtype':'init','tools':[]}))\n"
                "print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'errors':[os.environ['KANPILOT_QODER_PROVIDER_API_KEY']]}))\n")
            failed = qoder_cli.run([sys.executable, str(failure)], prompt="", schema={}, cwd=self.root,
                env=env, timeout=2, log_path=self.root / "provider-error.jsonl")
            self.assertTrue(failed["is_error"])
            self.assertNotIn(key, failed["error"])
            self.assertNotIn(key, (self.root / "provider-error.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
