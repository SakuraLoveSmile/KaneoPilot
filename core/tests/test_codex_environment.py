"""Frozen Flutter environment and admission checks; no model or live SDK use."""
import copy
import contextlib
import errno
import io
import json
import os
import socket
import subprocess
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import test_codex_execution as execution_fixtures

from kanpilot import codex_environment as environment
from kanpilot import codex_execution as adapter, codex_provider, codex_worker as worker
from kanpilot import execution_contract, git_delivery, util


class CodexEnvironmentTests(unittest.TestCase):
    # Reuse only the isolated fixture, not the older test methods.
    tearDown = execution_fixtures.CodexExecutionTests.tearDown
    flutter_sdk = execution_fixtures.CodexExecutionTests.flutter_sdk
    stub = execution_fixtures.CodexExecutionTests.stub
    guards = execution_fixtures.CodexExecutionTests.guards
    job = execution_fixtures.CodexExecutionTests.job

    def setUp(self):
        execution_fixtures.CodexExecutionTests.setUp(self)
        # macOS /var is an OS alias; dispatch freezes the canonical workspace.
        self.root = self.root.resolve()
        self.repo = self.repo.resolve()
        self.spec.update(cwd=str(self.repo), repository=str(self.repo))

    def freeze_flutter(self):
        sdk, binary = self.flutter_sdk()
        self.spec["required_verification_commands"] = [binary + " test --no-pub"]
        self.spec["sandbox_writable_roots"] = adapter.verification_writable_roots(
            self.spec["required_verification_commands"], self.spec["cwd"])
        environment.freeze(self.spec)
        return sdk

    def persist_contract(self):
        execution_contract.freeze_spec(self.spec, requirements_sha256="a" * 64)
        (self.run_dir / "spec.json").write_text(util.jdump(self.spec))

    def configs(self, argv):
        if "--" in argv:
            argv = argv[:argv.index("--")]
        values = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
        return codex_provider.tomllib.loads("\n".join(values))

    def test_freeze_is_run_scoped_and_does_not_write_host_configuration(self):
        self.freeze_flutter()
        frozen = self.spec["tool_environment"]
        root = self.repo / ".pi/kanpilot/codex" / self.spec["run_id"]
        self.assertEqual(frozen, {
            "v": 1,
            "environment": {
                "XDG_CONFIG_HOME": str(root / "config"),
                "TMPDIR": str(root / "tmp"),
                "FLUTTER_SUPPRESS_ANALYTICS": "true",
            },
            "network": {"enabled": True, "mode": "limited", "allow_local_binding": True,
                        "allow_upstream_proxy": False, "domains": {}},
        })
        self.assertEqual(environment.validate(self.spec), frozen)
        self.assertFalse((self.root / ".config/flutter/tool_state").exists())
        other = copy.deepcopy(self.spec)
        other.pop("tool_environment")
        other["run_id"] = "other-run"
        environment.freeze(other)
        self.assertNotEqual(other["tool_environment"]["environment"], frozen["environment"])

    def test_non_flutter_and_historical_specs_keep_existing_permissions_and_environment(self):
        environment.freeze(self.spec)
        self.assertNotIn("tool_environment", self.spec)
        self.assertIsNone(environment.validate(self.spec))
        self.assertEqual(environment.launcher_config(self.spec), [])
        _, binary = self.flutter_sdk()
        self.spec["required_verification_commands"] = [binary + " test --no-pub"]
        self.spec["sandbox_writable_roots"] = adapter.verification_writable_roots(
            self.spec["required_verification_commands"], self.spec["cwd"])
        # Merely loading a historical Flutter spec must not freeze a new environment.
        before = copy.deepcopy(self.spec)
        self.assertIsNone(environment.validate(self.spec))
        self.assertEqual(self.spec, before)
        base = {"XDG_CONFIG_HOME": "/legacy/config", "KEEP": "value"}
        self.assertEqual(environment.child_env(self.spec, base), base)
        argv = adapter.launcher_argv(self.spec, "schema", "out", mock.Mock(config_args=[]))
        self.assertIn("--add-dir", argv)
        self.assertIn('sandbox_mode="workspace-write"', argv)

    def test_environment_only_overrides_frozen_keys_and_copies_parent(self):
        self.freeze_flutter()
        base = {"KEEP": "keep", "XDG_CONFIG_HOME": "/host/config", "TMPDIR": "/host/tmp"}
        original = copy.deepcopy(base)
        actual = environment.child_env(self.spec, base)
        self.assertEqual(base, original)
        self.assertIsNot(actual, base)
        self.assertEqual(actual, {**base, **self.spec["tool_environment"]["environment"]})

    def test_full_environment_shape_and_values_cannot_be_tampered_with(self):
        self.freeze_flutter()
        replacements = [
            ("version", lambda value: value.update(v=2)),
            ("boolean version", lambda value: value.update(v=True)),
            ("extra key", lambda value: value.update(extra=True)),
            ("host config", lambda value: value["environment"].update(XDG_CONFIG_HOME=str(self.root))),
            ("temporary directory", lambda value: value["environment"].update(TMPDIR="/tmp")),
            ("analytics", lambda value: value["environment"].update(FLUTTER_SUPPRESS_ANALYTICS=False)),
            ("extra environment", lambda value: value["environment"].update(HOME=str(self.root))),
            ("network mode", lambda value: value["network"].update(mode="unrestricted")),
            ("proxy bypass", lambda value: value["network"].update(allow_upstream_proxy=True)),
            ("external domain", lambda value: value["network"].update(domains={"example.com": "allow"})),
            ("missing network", lambda value: value.pop("network")),
        ]
        for label, mutate in replacements:
            changed = copy.deepcopy(self.spec)
            mutate(changed["tool_environment"])
            with self.subTest(label=label), self.assertRaises(util.KanpilotError):
                environment.validate(changed)

    def test_changed_workspace_run_commands_or_roots_cannot_reuse_frozen_environment(self):
        self.freeze_flutter()
        cases = [("cwd", str(self.root)), ("run_id", "other-run"),
                 ("required_verification_commands", ["python3 -V"]),
                 ("sandbox_writable_roots", []), ("sandbox_writable_roots", [str(self.root)])]
        for key, value in cases:
            changed = copy.deepcopy(self.spec)
            changed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(util.KanpilotError):
                environment.validate(changed)

    def test_run_id_cannot_escape_private_tool_directory(self):
        self.freeze_flutter()
        for run_id in ("../outside", "a/b", "/outside", ".", "..", ""):
            changed = copy.deepcopy(self.spec)
            changed.pop("tool_environment")
            changed["run_id"] = run_id
            with self.subTest(run_id=run_id), self.assertRaises(util.KanpilotError):
                environment.freeze(changed)

    def test_replaced_sdk_cache_is_rejected_before_launch(self):
        sdk = self.freeze_flutter()
        cache = sdk / "bin/cache"
        cache.rmdir()
        cache.symlink_to(sdk, target_is_directory=True)
        with self.assertRaises(util.KanpilotError):
            environment.validate(self.spec)
        with self.assertRaises(util.KanpilotError):
            adapter.launcher_argv(self.spec, "schema", "out", mock.Mock(config_args=[]))

    def test_symlink_in_each_private_directory_component_cannot_escape_workspace(self):
        self.freeze_flutter()
        external = self.root / "private-data"
        external.mkdir()
        (external / "sentinel").write_text("unchanged")
        for component in (".pi", ".pi/kanpilot", ".pi/kanpilot/codex",
                          ".pi/kanpilot/codex/r", ".pi/kanpilot/codex/r/config",
                          ".pi/kanpilot/codex/r/tmp"):
            path = self.repo / component
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(external, target_is_directory=True)
            try:
                with self.subTest(component=component), mock.patch.object(environment.subprocess, "run") as probe:
                    with self.assertRaises(util.KanpilotError):
                        environment.preflight(self.spec, os.environ.copy())
                    probe.assert_not_called()
                self.assertEqual(sorted(item.name for item in external.iterdir()), ["sentinel"])
                self.assertEqual((external / "sentinel").read_text(), "unchanged")
            finally:
                path.unlink()

    def test_named_profile_preserves_limited_network_environment_and_resume_identity(self):
        self.freeze_flutter()
        provider = mock.Mock(config_args=['model_provider="fixture"'])
        for session in (None, "same-thread"):
            argv = adapter.launcher_argv(self.spec, "schema", "out", provider, session)
            config = self.configs(argv)
            self.assertNotIn("-s", argv)
            self.assertNotIn("--add-dir", argv)
            self.assertNotIn("sandbox_mode", config)
            self.assertTrue(config["features"]["network_proxy"])
            self.assertEqual(config["approval_policy"], "never")
            self.assertEqual(config["model_provider"], "fixture")
            self.assertEqual(config["default_permissions"], environment.permission_profile(self.spec))
            profile = config["permissions"][config["default_permissions"]]
            self.assertEqual(profile["network"], self.spec["tool_environment"]["network"])
            self.assertEqual(profile["filesystem"], {
                "/": "read", str(self.repo): "write",
                **{path: "write" for path in self.spec["sandbox_writable_roots"]},
                **{str(self.repo / name): "read" for name in (".git", ".codex", ".agents")},
            })
            self.assertEqual(config["shell_environment_policy"]["set"],
                             self.spec["tool_environment"]["environment"])
            if session:
                self.assertIn("resume", argv)
                self.assertEqual(argv[-2:], [session, "-"])
            else:
                self.assertNotIn("resume", argv)

    def test_permission_profile_is_stable_and_unique_to_the_frozen_run_and_workspace(self):
        self.freeze_flutter()
        original = environment.permission_profile(self.spec)
        self.assertRegex(original, r"^kanpilot_flutter_[0-9a-f]{24}$")
        self.assertEqual(environment.permission_profile(copy.deepcopy(self.spec)), original)
        self.assertEqual(self.configs(environment.launcher_config(self.spec))["default_permissions"], original)
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        profiles = {original}
        for changed in ({"run_id": "second-run"}, {"cwd": str(other_workspace)},
                        {"run_id": "second-run", "cwd": str(other_workspace)}):
            spec = copy.deepcopy(self.spec)
            spec.pop("tool_environment")
            spec.update(changed)
            environment.freeze(spec)
            profile = environment.permission_profile(spec)
            with self.subTest(changed=changed):
                self.assertNotIn(profile, profiles)
                self.assertEqual(environment.permission_profile(copy.deepcopy(spec)), profile)
                self.assertEqual(self.configs(environment.launcher_config(spec))["default_permissions"], profile)
            profiles.add(profile)

    def test_stage_contract_binds_every_frozen_environment_field_and_omission(self):
        self.freeze_flutter()
        self.persist_contract()
        contract = self.spec["stage_contract"]
        self.assertEqual(contract["inputs"]["tool_environment"], self.spec["tool_environment"])
        self.assertEqual(execution_contract.validate_spec(self.spec, starting=True), contract)
        for value in (None, {}, {"v": 1}, {**self.spec["tool_environment"], "v": 2}):
            changed = copy.deepcopy(self.spec)
            if value is None:
                changed.pop("tool_environment")
            else:
                changed["tool_environment"] = value
            with self.subTest(value=value), self.assertRaises(util.KanpilotError):
                execution_contract.validate_spec(changed)

    def test_private_runtime_files_do_not_change_verification_source_snapshot(self):
        self.freeze_flutter()
        before = git_delivery.source_state_sha256(self.repo)
        for key in ("XDG_CONFIG_HOME", "TMPDIR"):
            path = Path(self.spec["tool_environment"]["environment"][key])
            path.mkdir(parents=True, exist_ok=True)
            (path / "tool-state").write_text("runtime only")
        self.assertEqual(git_delivery.source_state_sha256(self.repo), before)

    def test_failed_or_invalid_native_preflight_is_an_environment_failure(self):
        self.freeze_flutter()
        results = [subprocess.CompletedProcess([], 1, "", "sandbox failed"),
                   subprocess.CompletedProcess([], 0, "not JSON", ""),
                   subprocess.CompletedProcess([], 0, "{}", ""),
                   subprocess.CompletedProcess([], 0, "[]", ""),
                   subprocess.CompletedProcess([], 0, "null", ""),
                   subprocess.TimeoutExpired("codex sandbox", 30)]
        for result in results:
            with self.subTest(result=type(result).__name__):
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch.object(environment.subprocess, "run", **kwargs), self.assertRaises(util.KanpilotError) as caught:
                    environment.preflight(self.spec, os.environ.copy())
                self.assertEqual(caught.exception.code, "codex_environment_unavailable")

    def test_native_preflight_requires_every_check_and_uses_the_frozen_sandbox(self):
        self.freeze_flutter()
        checks = {name: True for name in ("config_write", "temp_write", "ipv4", "ipv6",
                                          "direct_denied", "proxy_active", "proxy_denied")}
        receipt = {"v": 1, "status": "passed", "checks": checks}
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt), "")
        base = {"KEEP": "keep", "XDG_CONFIG_HOME": "/host/config"}
        with mock.patch.object(environment.subprocess, "run", return_value=result) as probe:
            self.assertEqual(environment.preflight(self.spec, base), receipt)
        kwargs = probe.call_args.kwargs
        argv = probe.call_args.args[0]
        self.assertEqual(argv[1], "sandbox")
        self.assertEqual(argv[argv.index("-C") + 1], str(self.repo))
        self.assertEqual(argv[argv.index("-P") + 1], environment.permission_profile(self.spec))
        self.assertEqual(self.configs(argv)["default_permissions"], environment.permission_profile(self.spec))
        self.assertEqual(kwargs["env"], environment.child_env(self.spec, base))
        self.assertTrue(kwargs["capture_output"])
        self.assertLessEqual(kwargs["timeout"], 30)
        for key in ("XDG_CONFIG_HOME", "TMPDIR"):
            self.assertTrue(Path(self.spec["tool_environment"]["environment"][key]).is_dir())
        damaged = []
        for name in checks:
            value = copy.deepcopy(receipt)
            value["checks"][name] = False
            damaged.append(value)
        missing = copy.deepcopy(receipt)
        missing["checks"].pop("proxy_denied")
        damaged.append(missing)
        numeric = copy.deepcopy(receipt)
        numeric["checks"]["direct_denied"] = 1
        damaged.append(numeric)
        for value in damaged:
            result = subprocess.CompletedProcess([], 0, json.dumps(value), "")
            with self.subTest(checks=value["checks"]), mock.patch.object(environment.subprocess, "run", return_value=result):
                with self.assertRaises(util.KanpilotError) as caught:
                    environment.preflight(self.spec, base)
                self.assertEqual(caught.exception.code, "codex_environment_unavailable")

    def test_probe_distinguishes_proxy_tunnel_denial_from_target_http_errors(self):
        self.freeze_flutter()
        tool_env = self.spec["tool_environment"]["environment"]
        for key in ("XDG_CONFIG_HOME", "TMPDIR"):
            Path(tool_env[key]).mkdir(parents=True, exist_ok=True)
        real_socket = socket.socket

        class LocalOnlySocket(real_socket):
            def connect(self, address):
                if address[0] == "198.51.100.1":
                    raise PermissionError(errno.EPERM, "isolated direct-network denial")
                if address[0] not in ("127.0.0.1", "::1"):
                    raise AssertionError("test attempted a non-loopback connection")
                return super().connect(address)

        tunnel_reason = "Tunnel connection failed: 403 Forbidden"
        cases = [
            ("proxy tunnel", urllib.error.URLError(OSError(tunnel_reason)), True),
            # Even a matching reason cannot turn a target response into proxy evidence.
            ("target HTTP 403", urllib.error.HTTPError("https://example.com/", 403,
                                                      OSError(tunnel_reason), None, None), False),
            ("timeout", TimeoutError("timed out"), False),
            ("other URL error", urllib.error.URLError(OSError("connection refused")), False),
            ("untyped tunnel message", urllib.error.URLError(tunnel_reason), False),
        ]
        for label, error, expected in cases:
            output = io.StringIO()
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(case=label), mock.patch.dict(os.environ, {
                **tool_env, "CODEX_NETWORK_PROXY_ACTIVE": "1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
            }), mock.patch("socket.socket", LocalOnlySocket), mock.patch(
                    "urllib.request.build_opener", return_value=opener), contextlib.redirect_stdout(output):
                exec(compile(environment._PROBE, "<codex-environment-probe>", "exec"), {})
            checks = json.loads(output.getvalue())["checks"]
            self.assertIs(checks["proxy_denied"], expected)
            self.assertTrue(checks["direct_denied"])
            self.assertTrue(checks["config_write"])
            self.assertTrue(checks["temp_write"])
            opener.open.assert_called_once_with("https://example.com/", timeout=2)

    def test_preflight_failure_is_durable_and_does_not_invoke_model(self):
        self.freeze_flutter()
        self.persist_contract()
        self.stub()
        error = util.KanpilotError("codex_environment_unavailable", "fixture loopback preflight denied")
        with self.guards(), mock.patch.object(environment, "preflight", side_effect=error) as preflight:
            self.assertEqual(worker.run_real(self.spec), 3)
        preflight.assert_called_once()
        job = self.job()
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["cli_terminal"])
        self.assertIsNone(job["session_id"])
        self.assertIn("fixture loopback preflight denied", job["detail"])
        self.assertFalse(list(self.run_dir.glob("codex-final-*.json.argv")))
        self.assertFalse((self.run_dir / adapter.REPORT_NAME).exists())

    def test_resume_rechecks_environment_before_sending_a_second_model_turn(self):
        self.freeze_flutter()
        self.persist_contract()
        self.stub("questions")
        receipt = {"v": 1, "status": "passed", "checks": {"fixture": True}}
        failure = util.KanpilotError("codex_environment_unavailable", "fixture resumed environment denied")

        def answer(command, payload, *args):
            if command == "report-execution-question":
                job = self.job()
                pending = job["pending_input"]
                value = {"scope_id": self.spec["scope_id"], "session_id": job["session_id"],
                         "request_id": pending["requestId"], "tool_call_id": pending["toolCallId"],
                         "answers": [["A"]], "operation_id": "answer-op"}
                path = self.run_dir / ("answer-" + util.sha256_text(pending["requestId"]) + ".json")
                path.write_text(util.jdump(value))

        with self.guards(answer), mock.patch.object(environment, "preflight", side_effect=[receipt, failure]) as preflight:
            self.assertEqual(worker.run_real(self.spec), 3)
        self.assertEqual(preflight.call_count, 2)
        job = self.job()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["session_id"], "thread-1")
        self.assertIn("fixture resumed environment denied", job["detail"])
        self.assertTrue((self.run_dir / "codex-final-1.json.argv").exists())
        self.assertFalse((self.run_dir / "codex-final-2.json.argv").exists())
        self.assertEqual(job["turns"][0]["input_resolution"], "answered")

    def test_directory_replaced_during_question_wait_cannot_be_used_by_resume(self):
        self.freeze_flutter()
        self.persist_contract()
        self.stub("questions")
        external = self.root / "private-data"
        external.mkdir()
        receipt = {"v": 1, "status": "passed", "checks": {"fixture": True}}

        def answer_and_replace(command, payload, *args):
            if command == "report-execution-question":
                job = self.job()
                pending = job["pending_input"]
                config = Path(self.spec["tool_environment"]["environment"]["XDG_CONFIG_HOME"])
                config.parent.mkdir(parents=True, exist_ok=True)
                config.symlink_to(external, target_is_directory=True)
                value = {"scope_id": self.spec["scope_id"], "session_id": job["session_id"],
                         "request_id": pending["requestId"], "tool_call_id": pending["toolCallId"],
                         "answers": [["A"]], "operation_id": "answer-op"}
                path = self.run_dir / ("answer-" + util.sha256_text(pending["requestId"]) + ".json")
                path.write_text(util.jdump(value))

        with self.guards(answer_and_replace), mock.patch.object(environment, "preflight", return_value=receipt) as preflight:
            self.assertEqual(worker.run_real(self.spec), 3)
        # The second child environment is rejected before another sandbox or model process.
        preflight.assert_called_once()
        job = self.job()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["session_id"], "thread-1")
        self.assertIn("符号链接", job["detail"])
        self.assertTrue((self.run_dir / "codex-final-1.json.argv").exists())
        self.assertFalse((self.run_dir / "codex-final-2.json.argv").exists())
        self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
