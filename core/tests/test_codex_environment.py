"""Frozen Flutter environment and admission checks; no model or live SDK use."""
import copy
import contextlib
import errno
import io
import json
import os
import plistlib
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
    stub = execution_fixtures.CodexExecutionTests.stub
    guards = execution_fixtures.CodexExecutionTests.guards
    job = execution_fixtures.CodexExecutionTests.job

    def setUp(self):
        execution_fixtures.CodexExecutionTests.setUp(self)
        # macOS /var is an OS alias; dispatch freezes the canonical workspace.
        self.root = self.root.resolve()
        self.repo = self.repo.resolve()
        self.spec.update(cwd=str(self.repo), repository=str(self.repo))
        # The base fixture restores this environment at teardown, but retains
        # ambient tool overrides unless they are explicitly isolated here.
        os.environ.update(XDG_CONFIG_HOME=str(self.root / "host-xdg-config"),
                          PUB_CACHE=str(self.root / ".pub-cache"),
                          GRADLE_USER_HOME=str(self.root / ".gradle"))
        os.environ.pop("JDK_JAVA_OPTIONS", None)
        os.environ.pop("_JAVA_OPTIONS", None)

    def flutter_sdk(self):
        sdk, binary = execution_fixtures.CodexExecutionTests.flutter_sdk(self)
        plugin = sdk / "packages/flutter_tools/gradle"
        (plugin / "src/main/kotlin").mkdir(parents=True)
        (plugin / "build.gradle.kts").write_text("// fixture plugin build source\n")
        (plugin / "src/main/kotlin/FlutterPlugin.kt").write_text("// fixture plugin source\n")
        return sdk, binary

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
        sdk = self.freeze_flutter()
        frozen = self.spec["tool_environment"]
        root = self.repo / ".pi/kanpilot/codex" / self.spec["run_id"]
        self.assertEqual(frozen, {
            "v": 2,
            "environment": {
                "XDG_CONFIG_HOME": str(root / "config"),
                "TMPDIR": str(root / "tmp"),
                "FLUTTER_SUPPRESS_ANALYTICS": "true",
                "BOT": "true",
                "ANALYZER_STATE_LOCATION_OVERRIDE": str(root / "analyzer"),
            },
            "network": {"enabled": True, "mode": "limited", "allow_local_binding": True,
                        "allow_upstream_proxy": False, "domains": {}},
            "platforms": [], "writable_roots": [],
            "setup": {"flutter": str(sdk / "bin/flutter"), "pub_offline": False,
                      "android": False, "pub_cache_source": None,
                      "gradle_cache_source": None, "flutter_settings": {}},
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
            ("version", lambda value: value.update(v=3)),
            ("boolean version", lambda value: value.update(v=True)),
            ("extra key", lambda value: value.update(extra=True)),
            ("host config", lambda value: value["environment"].update(XDG_CONFIG_HOME=str(self.root))),
            ("temporary directory", lambda value: value["environment"].update(TMPDIR="/tmp")),
            ("analyzer state", lambda value: value["environment"].update(ANALYZER_STATE_LOCATION_OVERRIDE=str(self.root))),
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
                          ".pi/kanpilot/codex/r/tmp", ".pi/kanpilot/codex/r/analyzer"):
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
        for value in (None, {}, {"v": 1}, {**self.spec["tool_environment"], "v": 1}):
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
        for key in ("XDG_CONFIG_HOME", "TMPDIR", "ANALYZER_STATE_LOCATION_OVERRIDE"):
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
        receipt = self.probe_receipt()
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt), "")
        base = {"KEEP": "keep", "XDG_CONFIG_HOME": "/host/config"}
        with mock.patch.object(environment.subprocess, "run", return_value=result) as probe:
            self.assertEqual(environment.preflight(self.spec, base), {**receipt, "v": 2})
        kwargs = probe.call_args.kwargs
        argv = probe.call_args.args[0]
        self.assertEqual(argv[1], "sandbox")
        self.assertEqual(argv[argv.index("-C") + 1], str(self.repo))
        self.assertEqual(argv[argv.index("-P") + 1], environment.permission_profile(self.spec))
        self.assertEqual(self.configs(argv)["default_permissions"], environment.permission_profile(self.spec))
        self.assertEqual(kwargs["env"], {**environment.child_env(self.spec, base),
                                       "KANPILOT_PROBE_WRITABLE_ROOTS": json.dumps(list(receipt["writable_roots"]))})
        self.assertTrue(kwargs["capture_output"])
        self.assertLessEqual(kwargs["timeout"], 30)
        for key in ("XDG_CONFIG_HOME", "TMPDIR", "ANALYZER_STATE_LOCATION_OVERRIDE"):
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
        analyzer = self.spec["tool_environment"]["environment"]["ANALYZER_STATE_LOCATION_OVERRIDE"]
        for writable_roots in ({}, {analyzer: False}):
            value = copy.deepcopy(receipt)
            value["writable_roots"] = writable_roots
            damaged.append(value)
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

    def freeze_native(self, platform="apk"):
        sdk, binary = self.flutter_sdk()
        self.spec["required_verification_commands"] = [binary + " build " + platform + " --debug --no-pub"]
        self.spec["sandbox_writable_roots"] = adapter.verification_writable_roots(
            self.spec["required_verification_commands"], self.spec["cwd"])
        (self.repo / "pubspec.yaml").write_text("name: fixture\nenvironment:\n  sdk: '>=3.0.0 <4.0.0'\n")
        (self.repo / "pubspec.lock").write_text("packages: {}\n")
        (self.repo / ".gitignore").write_text(".dart_tool/\n.flutter-plugins-dependencies\nmacos/Flutter/ephemeral/\n")
        environment.freeze(self.spec)
        return sdk

    def probe_receipt(self):
        frozen = self.spec["tool_environment"]
        roots = frozen["writable_roots"] + [frozen["environment"][key]
                 for key in ("ANALYZER_STATE_LOCATION_OVERRIDE", "PUB_CACHE", "GRADLE_USER_HOME")
                 if key in frozen["environment"]]
        names = environment.CHECKS + (("workspace_uti",) if frozen["v"] == 2 and "macos" in frozen["platforms"] else ())
        return {"v": 1, "status": "passed", "checks": {name: True for name in names},
                "writable_roots": {path: True for path in roots}}

    @contextlib.contextmanager
    def native_runs(self, results):
        # The sandbox is synthetic; source snapshots still run real isolated Git.
        original = subprocess.run
        native = mock.Mock(side_effect=results)

        def run(argv, **kwargs):
            if len(argv) > 1 and argv[1] == "sandbox":
                return native(argv, **kwargs)
            return original(argv, **kwargs)

        with mock.patch.object(environment.subprocess, "run", side_effect=run), mock.patch(
                "kanpilot.codex_setup_process.run", side_effect=native):
            yield native

    def test_v1_frozen_spec_remains_byte_compatible_without_new_capabilities(self):
        self.freeze_flutter()
        legacy = environment._expected(self.spec)
        self.spec["tool_environment"] = legacy
        before = copy.deepcopy(self.spec)
        self.assertEqual(environment.validate(self.spec), legacy)
        self.assertNotIn("BOT", environment.child_env(self.spec, {}))
        self.assertNotIn("XCODE_USER_DEFAULTS_FILE", environment.child_env(self.spec, {}))
        self.assertNotIn("JDK_JAVA_OPTIONS", environment.child_env(self.spec, {}))
        self.assertNotIn("setup", legacy)
        checks = {name: True for name in environment.CHECKS}
        native = subprocess.CompletedProcess([], 0, json.dumps({"v": 1, "status": "passed", "checks": checks}), "")
        with mock.patch.object(environment.subprocess, "run", return_value=native), mock.patch(
                "kanpilot.codex_tool_setup.seed_caches") as seed:
            receipt = environment.preflight(self.spec, {})
        self.assertEqual(receipt, {"v": 1, "status": "passed", "checks": checks})
        seed.assert_not_called()
        self.assertEqual(self.spec, before)

    def test_android_capability_uses_private_homes_and_only_readonly_dependency_cache(self):
        modules = self.root / ".gradle/caches/modules-2"
        modules.mkdir(parents=True)
        artifact = modules / "files-2.1/fixture/1.0/hash/library.jar"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"cached dependency")
        settings = Path(os.environ["XDG_CONFIG_HOME"]) / "settings"
        settings.parent.mkdir(parents=True)
        allowed_settings = {"jdk-dir": str(self.root / "jdk17"),
                            "android-sdk": str(self.root / "android-sdk"),
                            "android-studio-dir": str(self.root / "android-studio"),
                            "enable-swift-package-manager": False, "enable-macos-desktop": True}
        settings.write_text(json.dumps({**allowed_settings, "api_key": "do-not-copy"}))
        sdk = self.freeze_native()
        frozen = self.spec["tool_environment"]
        private = self.repo / ".pi/kanpilot/codex/r"
        self.assertEqual(frozen["platforms"], ["android"])
        plugin = sdk / "packages/flutter_tools/gradle"
        self.assertEqual(frozen["writable_roots"], [str(plugin / ".gradle"), str(plugin / "build")])
        self.assertEqual(frozen["environment"]["GRADLE_USER_HOME"], str(private / "gradle"))
        self.assertEqual(frozen["environment"]["PUB_CACHE"], str(private / "pub-cache"))
        self.assertEqual(frozen["environment"]["GRADLE_RO_DEP_CACHE"], str(modules.parent))
        gradle_lookup = Path(frozen["environment"]["GRADLE_RO_DEP_CACHE"]) / "modules-2" / artifact.relative_to(modules)
        self.assertEqual(gradle_lookup.read_bytes(), b"cached dependency")
        self.assertEqual(frozen["environment"]["JAVA_HOME"], str(self.root / "jdk17"))
        self.assertEqual(frozen["environment"]["ANDROID_HOME"], str(self.root / "android-sdk"))
        self.assertEqual(frozen["environment"]["ANDROID_SDK_ROOT"], str(self.root / "android-sdk"))
        self.assertEqual(frozen["environment"]["JDK_JAVA_OPTIONS"], "-Djava.net.preferIPv4Stack=true")
        self.assertEqual(frozen["setup"]["flutter_settings"], allowed_settings)
        self.assertNotIn("xcode_user_defaults", frozen["setup"])
        self.assertNotIn("XCODE_USER_DEFAULTS_FILE", frozen["environment"])
        self.assertNotIn("api_key", frozen["setup"]["flutter_settings"])
        profile = self.configs(environment.launcher_config(self.spec))
        filesystem = profile["permissions"][profile["default_permissions"]]["filesystem"]
        self.assertNotIn(str(self.root / ".gradle"), filesystem)
        self.assertNotIn(str(self.root / ".pub-cache"), filesystem)
        self.assertNotIn(str(modules.parent), filesystem)
        self.persist_contract()
        self.assertEqual(self.spec["stage_contract"]["inputs"]["tool_environment"], frozen)

    def test_android_ipv4_option_is_fixed_and_preserves_other_jvm_configuration(self):
        fixed = "-Djava.net.preferIPv4Stack=true"
        (self.repo / "android").mkdir()
        properties = self.repo / "android/gradle.properties"
        properties.write_text("org.gradle.jvmargs=-Xmx4G -Dfile.encoding=UTF-8\n")
        before = properties.read_bytes()
        with mock.patch.dict(os.environ, JAVA_TOOL_OPTIONS="-Dfixture.tool=true", _JAVA_OPTIONS="-Xmx64m"):
            self.freeze_native()
            for inherited in ("", fixed):
                with self.subTest(inherited=inherited), mock.patch.dict(os.environ, JDK_JAVA_OPTIONS=inherited):
                    environment.freeze(self.spec)
                    self.assertEqual(environment.validate(self.spec)["environment"]["JDK_JAVA_OPTIONS"], fixed)
                    actual = environment.child_env(self.spec, dict(os.environ))
                    self.assertEqual(actual["JDK_JAVA_OPTIONS"], fixed)
                    self.assertEqual(actual["JAVA_TOOL_OPTIONS"], "-Dfixture.tool=true")
                    self.assertEqual(actual["_JAVA_OPTIONS"], "-Xmx64m")
            self.assertEqual(properties.read_bytes(), before)
            self.persist_contract()
            self.assertEqual(self.spec["stage_contract"]["inputs"]["tool_environment"]["environment"]["JDK_JAVA_OPTIONS"], fixed)

    def test_android_plugin_outputs_are_prepared_but_sdk_sources_stay_readonly(self):
        sdk = self.freeze_native()
        plugin = sdk / "packages/flutter_tools/gradle"
        outputs = [plugin / ".gradle", plugin / "build"]
        self.assertTrue(all(not path.exists() for path in outputs))
        policy = self.configs(environment.launcher_config(self.spec))
        filesystem = policy["permissions"][policy["default_permissions"]]["filesystem"]

        def access(path):
            matching = [root for root in filesystem if Path(root) == path or Path(root) in path.parents]
            return filesystem[max(matching, key=len)]

        sources = [plugin / "build.gradle.kts", plugin / "src/main/kotlin/FlutterPlugin.kt",
                   sdk / "bin/internal/shared.sh"]
        before = {str(path): path.read_bytes() for path in sources}
        self.assertTrue(all(access(path) == "read" for path in sources))
        self.assertEqual(access(plugin / ".gradle/buildOutputCleanup/cache.properties.lock"), "write")
        self.assertEqual(access(plugin / "build/classes/kotlin/main/FlutterPlugin.class"), "write")
        self.assertEqual(access(plugin / "settings.gradle.kts"), "read")
        self.assertNotIn(str(plugin), filesystem)
        probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
        prepared = subprocess.CompletedProcess([], 0, "offline dependencies ready", "")
        with self.native_runs([probe, prepared]) as run, mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}):
            environment.preflight(self.spec, {})
        self.assertTrue(all(path.is_dir() for path in outputs))
        self.assertEqual({str(path): path.read_bytes() for path in sources}, before)
        roots = json.loads(run.call_args_list[0].kwargs["env"]["KANPILOT_PROBE_WRITABLE_ROOTS"])
        self.assertTrue(all(str(path) in roots for path in outputs))
        for path in outputs:
            failed = self.probe_receipt()
            failed["writable_roots"][str(path)] = False
            result = subprocess.CompletedProcess([], 0, json.dumps(failed), "")
            with self.subTest(unwritable=str(path)), mock.patch.object(environment.subprocess, "run", return_value=result), mock.patch(
                    "kanpilot.codex_tool_setup.seed_caches") as seed:
                with self.assertRaises(util.KanpilotError):
                    environment.preflight(self.spec, {})
                seed.assert_not_called()

    def test_android_plugin_missing_or_replaced_paths_fail_before_sandbox(self):
        sdk = self.freeze_native()
        plugin = sdk / "packages/flutter_tools/gradle"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        for name in (".gradle", "build"):
            path = plugin / name
            path.symlink_to(outside, target_is_directory=True)
            try:
                with self.subTest(name=name), mock.patch.object(environment.subprocess, "run") as run:
                    with self.assertRaises(util.KanpilotError):
                        environment.preflight(self.spec, {})
                    run.assert_not_called()
            finally:
                path.unlink()
        saved = plugin.with_name("saved-plugin")
        plugin.rename(saved)
        try:
            with self.assertRaises(util.KanpilotError):
                environment.validate(self.spec)
            plugin.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(util.KanpilotError):
                    environment.freeze(copy.deepcopy(self.spec))
            finally:
                plugin.unlink()
        finally:
            saved.rename(plugin)
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["sentinel"])
        self.assertEqual((outside / "sentinel").read_text(), "untouched")

    def test_android_plugin_and_readonly_cache_scope_cannot_be_widened_or_replaced(self):
        modules = self.root / ".gradle/caches/modules-2"
        modules.mkdir(parents=True)
        sdk = self.freeze_native()
        plugin = sdk / "packages/flutter_tools/gradle"
        for key, value in (("GRADLE_RO_DEP_CACHE", str(modules)),
                           ("GRADLE_RO_DEP_CACHE", str(self.root)),
                           ("writable_roots", [str(plugin)])):
            changed = copy.deepcopy(self.spec)
            target = changed["tool_environment"] if key == "writable_roots" else changed["tool_environment"]["environment"]
            target[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(util.KanpilotError):
                environment.validate(changed)
        outside = self.root / "outside"
        outside.mkdir()
        for path in (modules, modules.parent):
            saved = path.with_name(path.name + "-saved")
            path.rename(saved)
            path.symlink_to(outside, target_is_directory=True)
            try:
                with self.subTest(replaced=str(path)), self.assertRaises(util.KanpilotError):
                    environment.validate(self.spec)
                with self.assertRaises(util.KanpilotError):
                    environment.freeze(copy.deepcopy(self.spec))
            finally:
                path.unlink()
                saved.rename(path)

    def test_non_android_and_v1_do_not_gain_plugin_output_writes(self):
        sdk = self.freeze_flutter()
        plugin = sdk / "packages/flutter_tools/gradle"
        self.assertEqual(self.spec["tool_environment"]["writable_roots"], [])
        command = self.spec["required_verification_commands"][0].replace(" test --no-pub", " build apk --no-pub")
        self.spec["required_verification_commands"] = [command]
        self.spec["tool_environment"] = environment._expected(self.spec)
        before = copy.deepcopy(self.spec)
        self.assertEqual(environment.validate(self.spec), before["tool_environment"])
        policy = self.configs(environment.launcher_config(self.spec))
        filesystem = policy["permissions"][policy["default_permissions"]]["filesystem"]
        self.assertTrue(all(str(plugin / name) not in filesystem for name in (".gradle", "build")))
        self.assertNotIn("GRADLE_RO_DEP_CACHE", self.spec["tool_environment"]["environment"])
        self.assertEqual(self.spec, before)

    def test_android_conflicting_jdk_options_fail_without_exposing_host_parameters(self):
        self.freeze_native()
        for conflicting in (" ", "-Djava.net.preferIPv4Stack=false", "-Dpassword=fixture-private-secret"):
            with self.subTest(conflicting=conflicting), mock.patch.dict(os.environ, JDK_JAVA_OPTIONS=conflicting):
                for operation in (environment.freeze, environment.validate):
                    with self.subTest(operation=operation.__name__), self.assertRaises(util.KanpilotError) as caught:
                        operation(copy.deepcopy(self.spec))
                    self.assertEqual(caught.exception.code, "codex_environment_unavailable")
                    self.assertIn("固定 JDK_JAVA_OPTIONS", caught.exception.message)
                    self.assertNotIn("fixture-private-secret", caught.exception.message)
                    self.assertNotIn("preferIPv4Stack=false", caught.exception.message)

    def test_ipv4_java_option_does_not_change_non_android_or_legacy_specs(self):
        with mock.patch.dict(os.environ, JDK_JAVA_OPTIONS="-Dfixture.keep=true"):
            self.freeze_flutter()
            self.assertNotIn("JDK_JAVA_OPTIONS", self.spec["tool_environment"]["environment"])
            self.assertEqual(environment.child_env(self.spec, dict(os.environ))["JDK_JAVA_OPTIONS"], "-Dfixture.keep=true")
        self.spec["required_verification_commands"] = [
            self.spec["required_verification_commands"][0].replace(" test --no-pub", " build apk --no-pub")]
        self.spec["tool_environment"] = environment._expected(self.spec)
        legacy = copy.deepcopy(self.spec)
        with mock.patch.dict(os.environ, JDK_JAVA_OPTIONS="-Dfixture.keep=true"):
            self.assertEqual(environment.validate(self.spec), legacy["tool_environment"])
            self.assertNotIn("JDK_JAVA_OPTIONS", self.spec["tool_environment"]["environment"])
            self.assertEqual(environment.child_env(self.spec, dict(os.environ))["JDK_JAVA_OPTIONS"], "-Dfixture.keep=true")
        self.assertEqual(self.spec, legacy)

    def test_android_java_options_allow_true_and_preserve_unrelated_options_verbatim(self):
        self.freeze_native()
        accepted = [
            "-Xmx64m -Dfixture.private=do-not-freeze",
            '-Djava.net.preferIPv4Stack="true" -Dfixture.path="C:\\tools"',
            "-Djava.net.preferIPv4Stack='TRUE' -Xmx64m",
            '-Djava.net.preferIPv4Stack=tr""ue',
            '-Djava.net."preferIPv4Stack"=true',
            "-Dfixture.value=-Djava.net.preferIPv4Stack=false",
            "-Djava.net.preferIPv4StackOther=false",
        ]
        for value in accepted:
            with self.subTest(value=value), mock.patch.dict(os.environ, _JAVA_OPTIONS=value,
                    JAVA_TOOL_OPTIONS="-Djava.net.preferIPv4Stack=false -Dfixture.private=do-not-freeze"):
                environment.freeze(self.spec)
                actual = environment.child_env(self.spec, dict(os.environ))
                self.assertEqual(actual["_JAVA_OPTIONS"], value)
                self.assertEqual(actual["JAVA_TOOL_OPTIONS"], os.environ["JAVA_TOOL_OPTIONS"])
                self.assertEqual(actual["JDK_JAVA_OPTIONS"], "-Djava.net.preferIPv4Stack=true")
                self.assertNotIn("do-not-freeze", util.jdump(self.spec["tool_environment"]))

    def test_android_java_options_reject_conflicting_property_without_shell_unescaping_or_leaks(self):
        self.freeze_native()
        rejected = [
            "-Djava.net.preferIPv4Stack=false",
            '-Djava.net."preferIPv4Stack"=false',
            '-Djava.net.preferIPv4"Stack"=false',
            '-Djava.net.preferIPv4Stack="false" -Dpassword=fixture-private-secret',
            "-Djava.net.preferIPv4Stack='false'",
            "-Djava.net.preferIPv4Stack",
            "-Djava.net.preferIPv4Stack=",
            '-Djava.net.preferIPv4Stack=""',
            r"-Djava.net.preferIPv4Stack=tr\ue",
            "-Djava.net.preferIPv4Stack=true -Djava.net.preferIPv4Stack=false",
            '-Djava.net.preferIPv4Stack="true -Dpassword=fixture-private-secret',
            '-Dfixture.unrelated="unterminated',
        ]
        for value in rejected:
            with self.subTest(value=value), mock.patch.dict(os.environ, _JAVA_OPTIONS=value):
                for operation in (environment.freeze, environment.validate):
                    with self.subTest(operation=operation.__name__), self.assertRaises(util.KanpilotError) as caught:
                        operation(copy.deepcopy(self.spec))
                    self.assertEqual(caught.exception.code, "codex_environment_unavailable")
                    self.assertIn("_JAVA_OPTIONS", caught.exception.message)
                    self.assertNotIn("fixture-private-secret", caught.exception.message)
                    self.assertNotIn("preferIPv4Stack=false", caught.exception.message)

    def test_java_options_ipv4_guard_does_not_apply_to_non_android_or_v1(self):
        value = "-Djava.net.preferIPv4Stack=false"
        with mock.patch.dict(os.environ, _JAVA_OPTIONS=value):
            self.freeze_flutter()
            self.assertEqual(environment.child_env(self.spec, dict(os.environ))["_JAVA_OPTIONS"], value)
            self.spec["required_verification_commands"] = [
                self.spec["required_verification_commands"][0].replace(" test --no-pub", " build apk --no-pub")]
            self.spec["tool_environment"] = environment._expected(self.spec)
            self.assertEqual(environment.validate(self.spec)["v"], 1)
            self.assertEqual(environment.child_env(self.spec, dict(os.environ))["_JAVA_OPTIONS"], value)

    def test_default_flutter_settings_without_xdg_are_consistent_on_linux_and_macos(self):
        settings = self.root / ".config/flutter/settings"
        settings.parent.mkdir(parents=True)
        allowed = {"jdk-dir": str(self.root / "default-jdk"), "enable-macos-desktop": True}
        settings.write_text(json.dumps({**allowed, "token": "do-not-copy"}))
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            for platform in ("linux", "darwin"):
                with self.subTest(platform=platform), mock.patch.object(environment.sys, "platform", platform):
                    self.assertEqual(environment._flutter_settings(), allowed)

    def test_platform_capability_recognizes_only_direct_literal_build_commands(self):
        _, binary = self.flutter_sdk()
        commands = [binary + " test --no-pub", binary + " build apk --debug --no-pub && echo x",
                    "echo " + binary + " build macos", binary + " build $PLATFORM"]
        self.spec["required_verification_commands"] = commands
        self.spec["sandbox_writable_roots"] = adapter.verification_writable_roots(commands, self.repo)
        environment.freeze(self.spec)
        self.assertEqual(self.spec["tool_environment"]["platforms"], [])
        self.assertFalse(self.spec["tool_environment"]["setup"]["pub_offline"])

    def test_previously_supported_compound_flutter_commands_keep_basic_environment(self):
        _, binary = self.flutter_sdk()
        self.spec["required_verification_commands"] = [binary + " analyze --no-pub && true"]
        self.spec["sandbox_writable_roots"] = adapter.verification_writable_roots(
            self.spec["required_verification_commands"], self.repo)
        environment.freeze(self.spec)
        frozen = environment.validate(self.spec)
        self.assertEqual(frozen["platforms"], [])
        self.assertEqual(frozen["environment"]["BOT"], "true")
        self.assertFalse(frozen["setup"]["pub_offline"])

    def test_macos_host_roots_are_frozen_and_a_replaced_root_is_rejected(self):
        host_tmp = self.root / "system-temp"
        host_tmp.mkdir()
        with mock.patch.object(environment.sys, "platform", "darwin"), mock.patch.object(
                environment.os, "confstr", return_value=str(host_tmp), create=True):
            sdk = self.freeze_native("macos")
            roots = self.spec["tool_environment"]["writable_roots"]
            derived = self.root / "Library/Developer/Xcode/DerivedData"
            manifests = self.root / "Library/Caches/org.swift.swiftpm/manifests"
            self.assertEqual(roots, [str(derived), str(host_tmp), str(manifests)])
            self.assertTrue(all(str(sdk / "packages/flutter_tools/gradle" / name) not in roots
                                for name in (".gradle", "build")))
            profile = self.configs(environment.launcher_config(self.spec))
            filesystem = profile["permissions"][profile["default_permissions"]]["filesystem"]
            self.assertTrue(all(filesystem[path] == "write" for path in roots))
            self.assertNotIn(str(self.root / "Library"), filesystem)
            self.assertNotIn(str(manifests.parent), filesystem)
            for replaced in (derived, manifests):
                with self.subTest(replaced=str(replaced)):
                    replaced.parent.mkdir(parents=True, exist_ok=True)
                    replaced.symlink_to(host_tmp, target_is_directory=True)
                    try:
                        with self.assertRaises(util.KanpilotError):
                            environment.validate(self.spec)
                    finally:
                        replaced.unlink()

    @contextlib.contextmanager
    def macos_environment(self):
        host_tmp = self.root / "system-temp"
        host_tmp.mkdir(exist_ok=True)
        with mock.patch.object(environment.sys, "platform", "darwin"), mock.patch.object(
                environment.os, "confstr", return_value=str(host_tmp), create=True):
            yield

    def test_android_and_macos_generated_roots_are_combined_and_all_preflighted(self):
        with self.macos_environment():
            sdk = self.freeze_native()
            command = self.spec["required_verification_commands"][0]
            self.spec["required_verification_commands"].append(command.replace(" build apk ", " build macos "))
            environment.freeze(self.spec)
            frozen = self.spec["tool_environment"]
            plugin = sdk / "packages/flutter_tools/gradle"
            expected = [plugin / ".gradle", plugin / "build", self.root / "Library/Developer/Xcode/DerivedData",
                        self.root / "system-temp", self.root / "Library/Caches/org.swift.swiftpm/manifests"]
            self.assertEqual(frozen["platforms"], ["android", "macos"])
            self.assertEqual(frozen["writable_roots"], [str(path) for path in expected])
            probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
            prepared = subprocess.CompletedProcess([], 0, "offline dependencies ready", "")
            with self.native_runs([probe, prepared]) as run, mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}):
                environment.preflight(self.spec, {})
            self.assertTrue(all(path.is_dir() for path in expected))
            roots = json.loads(run.call_args_list[0].kwargs["env"]["KANPILOT_PROBE_WRITABLE_ROOTS"])
            self.assertTrue(all(str(path) in roots for path in expected))

    def test_macos_manifest_defaults_are_private_frozen_and_readonly_in_both_launches(self):
        with self.macos_environment():
            self.freeze_native("macos")
            frozen = self.spec["tool_environment"]
            private = self.repo / ".pi/kanpilot/codex/r"
            target = private / "config/xcode-user-defaults.plist"
            expected = {"IDEPackageSupportDisableManifestSandbox": True}
            self.assertEqual(frozen["environment"]["XCODE_USER_DEFAULTS_FILE"], str(target))
            self.assertEqual(frozen["setup"]["xcode_user_defaults"], expected)
            self.assertNotIn("JDK_JAVA_OPTIONS", frozen["environment"])
            self.assertNotIn(str(target), frozen["writable_roots"])
            environment._directory(self.spec, create=True)
            environment._prepare_settings(self.spec)
            contents = target.read_bytes()
            self.assertEqual(plistlib.loads(contents), expected)
            self.assertIn(b"<true/>", contents)
            environment._prepare_settings(self.spec)
            self.assertEqual(target.read_bytes(), contents)
            self.assertEqual(json.loads((target.parent / "settings").read_text()), {})
            provider = mock.Mock(config_args=[])
            for session in (None, "same-thread"):
                with self.subTest(session=session):
                    config = self.configs(adapter.launcher_argv(self.spec, "schema", "output", provider, session))
                    filesystem = config["permissions"][config["default_permissions"]]["filesystem"]
                    self.assertEqual(filesystem[str(self.repo)], "write")
                    self.assertEqual(filesystem[str(target)], "read")
                    self.assertNotIn(str(target.parent), filesystem)
                    self.assertEqual(config["shell_environment_policy"]["set"]["XCODE_USER_DEFAULTS_FILE"], str(target))
            self.persist_contract()
            self.assertEqual(self.spec["stage_contract"]["inputs"]["tool_environment"], frozen)

    def test_macos_manifest_defaults_path_dictionary_and_value_types_cannot_change(self):
        with self.macos_environment():
            self.freeze_native("macos")
            changes = [
                lambda value: value["environment"].update(XCODE_USER_DEFAULTS_FILE=str(self.root / "outside.plist")),
                lambda value: value["environment"].pop("XCODE_USER_DEFAULTS_FILE"),
                lambda value: value["setup"].pop("xcode_user_defaults"),
                lambda value: value["setup"].update(xcode_user_defaults={"IDEPackageSupportDisableManifestSandbox": False}),
                lambda value: value["setup"].update(xcode_user_defaults={"IDEPackageSupportDisableManifestSandbox": 1}),
                lambda value: value["setup"]["xcode_user_defaults"].update(extra=True),
            ]
            for index, mutate in enumerate(changes):
                changed = copy.deepcopy(self.spec)
                mutate(changed["tool_environment"])
                with self.subTest(index=index), self.assertRaises(util.KanpilotError):
                    environment.validate(changed)

    def test_macos_manifest_defaults_reject_replaced_or_oversized_files_before_sandbox(self):
        with self.macos_environment():
            self.freeze_native("macos")
            environment._directory(self.spec, create=True)
            target = Path(self.spec["tool_environment"]["environment"]["XCODE_USER_DEFAULTS_FILE"])
            outside = self.root / "outside.plist"
            outside.write_bytes(b"untouched")
            replacements = [
                ("symlink", lambda: target.symlink_to(outside)),
                ("fifo", lambda: os.mkfifo(target)),
                ("directory", lambda: target.mkdir()),
                ("oversized", lambda: target.write_bytes(b"x" * 65537)),
                ("invalid", lambda: target.write_bytes(b"invalid plist")),
                ("invalid XML", lambda: target.write_bytes(b'<?xml version="1.0"?><plist><dict>')),
            ]
            for name, replace in replacements:
                replace()
                try:
                    with self.subTest(name=name), mock.patch.object(environment.subprocess, "run") as run:
                        with self.assertRaises(util.KanpilotError):
                            environment.preflight(self.spec, {})
                        run.assert_not_called()
                    self.assertEqual(outside.read_bytes(), b"untouched")
                finally:
                    if target.is_dir() and not target.is_symlink():
                        target.rmdir()
                    else:
                        target.unlink()

    def test_resume_rechecks_macos_manifest_defaults_contents_before_sandbox(self):
        with self.macos_environment():
            self.freeze_native("macos")
            probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
            prepared = subprocess.CompletedProcess([], 0, "offline dependencies ready", "")
            with self.native_runs([probe, prepared]), mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}):
                self.assertEqual(environment.preflight(self.spec, {})["status"], "passed")
            target = Path(self.spec["tool_environment"]["environment"]["XCODE_USER_DEFAULTS_FILE"])
            for changed in ({"IDEPackageSupportDisableManifestSandbox": False},
                            {"IDEPackageSupportDisableManifestSandbox": 1},
                            {"IDEPackageSupportDisableManifestSandbox": True, "extra": True}):
                target.write_bytes(plistlib.dumps(changed))
                with self.subTest(changed=changed), mock.patch.object(environment.subprocess, "run") as run:
                    with self.assertRaises(util.KanpilotError) as caught:
                        environment.preflight(self.spec, {})
                    run.assert_not_called()
                    self.assertIn("冻结的 Xcode 构建设置已变化", caught.exception.message)

    def test_native_preparation_uses_same_sandbox_and_does_not_run_verify(self):
        self.freeze_native()
        frozen = self.spec["tool_environment"]
        probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
        prepared = subprocess.CompletedProcess([], 0, "offline dependencies ready", "")
        with self.native_runs([probe, prepared]) as run, mock.patch(
                "kanpilot.codex_tool_setup.seed_caches", return_value={"pub_packages": 1}) as seed:
            receipt = environment.preflight(self.spec, {})
        self.assertEqual(run.call_count, 2)
        seed.assert_called_once_with(self.spec)
        first, second = [call.args[0] for call in run.call_args_list]
        self.assertEqual(first[:first.index("--")], second[:second.index("--")])
        self.assertEqual(second[second.index("--") + 1:],
                         [frozen["setup"]["flutter"], "pub", "get", "--offline", "--enforce-lockfile"])
        self.assertEqual(receipt["setup"]["exit_code"], 0)
        self.assertEqual(receipt["v"], 2)
        config = Path(frozen["environment"]["XDG_CONFIG_HOME"])
        self.assertEqual(json.loads((config / "settings").read_text()), {})
        self.assertFalse((config / "tool_state").exists())

    def test_native_setup_fails_with_specific_redacted_exit_detail_and_source_guard(self):
        self.freeze_native()
        probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
        secret = "fixture-private-token-12345"
        failed = subprocess.CompletedProcess([], 69, "Missing locked package token=" + secret, "")
        with self.native_runs([probe, failed]), mock.patch(
                "kanpilot.codex_tool_setup.seed_caches", return_value={}):
            with self.assertRaises(util.KanpilotError) as caught:
                environment.preflight(self.spec, {"MY_API_KEY": secret})
        self.assertIn("退出码 69", caught.exception.message)
        self.assertIn("Missing locked package", caught.exception.message)
        self.assertNotIn(secret, caught.exception.message)

        def mutate(argv, **kwargs):
            if "pub" in argv:
                (self.repo / "pubspec.lock").write_text("changed lock\n")
                return subprocess.CompletedProcess([], 0, "ok", "")
            return probe

        with self.native_runs(mutate), mock.patch(
                "kanpilot.codex_tool_setup.seed_caches", return_value={}):
            with self.assertRaises(util.KanpilotError) as caught:
                environment.preflight(self.spec, {})
        self.assertIn("改动了源码或锁文件", caught.exception.message)

    def test_replaced_native_settings_fifo_or_directory_fails_before_sandbox(self):
        self.freeze_native()
        config = Path(self.spec["tool_environment"]["environment"]["XDG_CONFIG_HOME"])
        config.mkdir(parents=True)
        settings = config / "settings"
        os.mkfifo(settings)
        try:
            with mock.patch.object(environment.subprocess, "run") as run:
                with self.assertRaises(util.KanpilotError) as caught:
                    environment.preflight(self.spec, {})
            run.assert_not_called()
            self.assertIn("特殊文件", caught.exception.message)
        finally:
            settings.unlink()
        settings.mkdir()
        with mock.patch.object(environment.subprocess, "run") as run:
            with self.assertRaises(util.KanpilotError):
                environment.preflight(self.spec, {})
        run.assert_not_called()

    def test_custom_xdg_settings_use_flutter_actual_path_and_preserve_feature_flags(self):
        config = self.root / "custom-xdg"
        config.mkdir()
        (config / "settings").write_text(json.dumps({"enable-swift-package-manager": False,
                                                     "android-studio-dir": str(self.root / "studio"),
                                                     "token": "never-copy"}))
        with mock.patch.dict(os.environ, XDG_CONFIG_HOME=str(config)):
            self.freeze_native()
            frozen = self.spec["tool_environment"]
            self.assertEqual(frozen["setup"]["flutter_settings"], {
                "enable-swift-package-manager": False, "android-studio-dir": str(self.root / "studio")})
            environment._directory(self.spec, create=True)
            environment._prepare_settings(self.spec)
            settings = Path(frozen["environment"]["XDG_CONFIG_HOME"]) / "settings"
            self.assertEqual(json.loads(settings.read_text()), frozen["setup"]["flutter_settings"])
            self.assertFalse((settings.parent / "flutter/settings").exists())

    def test_legacy_tool_state_is_an_actionable_error_before_any_model(self):
        self.freeze_flutter()
        (self.root / ".flutter_tool_state").write_text("{}")
        with mock.patch.object(environment.subprocess, "run") as run:
            with self.assertRaises(util.KanpilotError) as caught:
                environment.preflight(self.spec, {})
        run.assert_not_called()
        self.assertIn(".flutter_tool_state", caught.exception.message)

    def test_missing_generated_spm_package_is_not_a_successful_preparation(self):
        host_tmp = self.root / "system-temp"
        host_tmp.mkdir()
        with mock.patch.object(environment.sys, "platform", "darwin"), mock.patch.object(
                environment.os, "confstr", return_value=str(host_tmp), create=True):
            self.freeze_native("macos")
            project = self.repo / "macos/Runner.xcodeproj/project.pbxproj"
            project.parent.mkdir(parents=True)
            project.write_text("FlutterGeneratedPluginSwiftPackage")
            probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
            prepared = subprocess.CompletedProcess([], 0, "ok", "")
            with self.native_runs([probe, prepared]), mock.patch(
                    "kanpilot.codex_tool_setup.seed_caches", return_value={}):
                with self.assertRaises(util.KanpilotError) as caught:
                    environment.preflight(self.spec, {})
            self.assertIn("macOS 平台生成包仍缺失", caught.exception.message)

    def test_unconfirmed_setup_cleanup_detail_reaches_durable_job_before_model(self):
        self.freeze_native()
        self.persist_contract()
        self.stub()
        probe = subprocess.CompletedProcess([], 0, json.dumps(self.probe_receipt()), "")
        error = RuntimeError("Codex 离线准备进程组清理未确认")
        with self.guards(), self.native_runs([probe, error]), mock.patch(
                "kanpilot.codex_tool_setup.seed_caches", return_value={}):
            self.assertEqual(worker.run_real(self.spec), 3)
        job = self.job()
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["cli_terminal"])
        self.assertIsNone(job["session_id"])
        self.assertIn("进程组清理未确认", job["detail"])
        self.assertFalse(list(self.run_dir.glob("codex-final-*.json.argv")))
        self.assertFalse((self.run_dir / adapter.REPORT_NAME).exists())


    def test_unrestricted_flutter_freezes_private_state_without_permission_profile(self):
        self.spec["config"]["sandbox"] = "danger-full-access"
        self.freeze_flutter()
        frozen = self.spec["tool_environment"]
        self.assertEqual(frozen["v"], 3)
        self.assertEqual(frozen["isolation"], "none")
        self.assertEqual(frozen["network"], {"mode": "unrestricted"})
        self.assertEqual(frozen["writable_roots"], [])
        self.assertEqual(frozen["environment"]["BOT"], "true")
        self.assertIn("ANALYZER_STATE_LOCATION_OVERRIDE", frozen["environment"])
        for session in (None, "thread-1"):
            argv = adapter.launcher_argv(self.spec, "schema", "out", mock.Mock(config_args=[]), session)
            config = self.configs(argv)
            self.assertEqual(argv[argv.index("-s") + 1], "danger-full-access")
            self.assertEqual(config["sandbox_mode"], "danger-full-access")
            self.assertEqual(config["approval_policy"], "never")
            self.assertEqual(config["shell_environment_policy"]["set"], frozen["environment"])
            self.assertNotIn("--add-dir", argv)
            self.assertNotIn("permissions", config)
            self.assertNotIn("default_permissions", config)
            self.assertNotIn("features", config)
        with mock.patch.object(environment.subprocess, "run", side_effect=AssertionError("sandbox or network probe forbidden")):
            receipt = environment.preflight(self.spec, {})
        self.assertEqual(receipt["isolation"], "none")
        self.assertEqual(receipt["network"], {"mode": "unrestricted"})
        self.assertEqual(receipt["checks"], {"config_write": True, "temp_write": True})

    def test_old_frozen_modes_cannot_be_widened_and_v3_cannot_be_downgraded(self):
        self.freeze_flutter()
        for version in (1, 2):
            old = copy.deepcopy(self.spec)
            if version == 1:
                old["tool_environment"] = environment._expected(old)
                old["config"].pop("sandbox")
            execution_contract.freeze_spec(old, requirements_sha256="a" * 64)
            self.assertIsNotNone(environment.validate(old))
            old["config"]["sandbox"] = "danger-full-access"
            with self.subTest(version=version), self.assertRaises(util.KanpilotError):
                environment.validate(old)
            with self.assertRaises(util.KanpilotError):
                execution_contract.validate_spec(old)
        self.spec["config"]["sandbox"] = "danger-full-access"
        environment.freeze(self.spec)
        for mutate in (lambda value: value["config"].update(sandbox="workspace-write"),
                       lambda value: value["tool_environment"].update(isolation="sandbox"),
                       lambda value: value["tool_environment"]["network"].update(mode="limited")):
            changed = copy.deepcopy(self.spec)
            mutate(changed)
            with self.assertRaises(util.KanpilotError):
                environment.validate(changed)

    def test_unrestricted_native_setup_runs_directly_with_private_cache_and_lifecycle(self):
        self.spec["config"]["sandbox"] = "danger-full-access"
        with mock.patch.dict(os.environ, JDK_JAVA_OPTIONS="-Djava.net.preferIPv4Stack=false",
                             _JAVA_OPTIONS="-Djava.net.preferIPv4Stack=false"):
            self.freeze_native()
            frozen = self.spec["tool_environment"]
            self.assertNotIn("JDK_JAVA_OPTIONS", frozen["environment"])
            self.assertEqual(frozen["writable_roots"], [])
            self.assertIn("GRADLE_USER_HOME", frozen["environment"])
            self.assertIn("PUB_CACHE", frozen["environment"])
            prepared = subprocess.CompletedProcess([], 0, "ok", "")
            check = mock.Mock()
            # Real isolated Git snapshots remain available; no Codex process.
            with self.native_runs([prepared]) as run, mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={"pub_packages": 1}) as seed:
                first = environment.preflight(self.spec, os.environ.copy(), check=check)
            self.assertEqual(run.call_args.args[0], [frozen["setup"]["flutter"], "pub", "get", "--offline", "--enforce-lockfile"])
            self.assertIs(run.call_args.kwargs["check"], check)
            self.assertEqual(run.call_args.kwargs["env"]["JDK_JAVA_OPTIONS"], "-Djava.net.preferIPv4Stack=false")
            self.assertGreaterEqual(check.call_count, 2)
            seed.assert_called_once_with(self.spec)
            self.assertEqual(first["setup"]["exit_code"], 0)
            # Resume uses the same direct managed setup and stable receipt.
            with self.native_runs([prepared]) as run, mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={"pub_packages": 1}):
                self.assertEqual(environment.preflight(self.spec, os.environ.copy()), first)
            self.assertNotIn("sandbox", run.call_args.args[0])

    def test_unrestricted_setup_retains_exit_source_timeout_and_stop_guards(self):
        self.spec["config"]["sandbox"] = "danger-full-access"
        self.freeze_native()
        for result, message in ((subprocess.CompletedProcess([], 7, "failed", ""), "退出码 7"),
                                (subprocess.TimeoutExpired("fixture", 180), "超时"),
                                (RuntimeError("进程组清理未确认"), "进程组清理未确认")):
            with self.subTest(message=message), self.native_runs([result]), mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}):
                with self.assertRaises(util.KanpilotError) as caught:
                    environment.preflight(self.spec, {})
            self.assertIn(message, caught.exception.message)
        def mutate(argv, **kwargs):
            (self.repo / "pubspec.lock").write_text("changed\n")
            return subprocess.CompletedProcess([], 0, "ok", "")
        with self.native_runs(mutate), mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}):
            with self.assertRaises(util.KanpilotError) as caught:
                environment.preflight(self.spec, {})
        self.assertIn("改动了源码或锁文件", caught.exception.message)
        stop = util.KanpilotError("conflict", "fixture stop")
        with mock.patch("kanpilot.codex_tool_setup.seed_caches") as seed:
            with self.assertRaises(util.KanpilotError) as caught:
                environment.preflight(self.spec, {}, check=mock.Mock(side_effect=stop))
            seed.assert_not_called()
        self.assertIs(caught.exception, stop)

    def test_unrestricted_macos_avoids_restricted_xcode_settings_and_host_roots(self):
        self.spec["config"]["sandbox"] = "danger-full-access"
        with mock.patch.object(environment.sys, "platform", "darwin"), mock.patch.object(environment.os, "confstr", side_effect=AssertionError("restricted host root lookup"), create=True):
            self.freeze_native("macos")
            frozen = self.spec["tool_environment"]
            self.assertNotIn("XCODE_USER_DEFAULTS_FILE", frozen["environment"])
            self.assertNotIn("xcode_user_defaults", frozen["setup"])
            self.assertEqual(frozen["writable_roots"], [])
            with self.native_runs([subprocess.CompletedProcess([], 0, "ok", "")]), mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}):
                receipt = environment.preflight(self.spec, {})
            self.assertNotIn("workspace_uti", receipt["checks"])
            self.assertFalse((Path(frozen["environment"]["XDG_CONFIG_HOME"]) / "xcode-user-defaults.plist").exists())

    def test_shared_workspace_uti_probe_requires_identifier_and_package_conformance(self):
        import ctypes
        namespace = {}
        exec(environment.MACOS_WORKSPACE_UTI_PROBE, namespace)
        for identifier, conforms, preferred, expected in (
                (b"com.apple.dt.document.workspace", True, 5, True),
                (b"dyn.invalid", True, 5, False),
                (b"com.apple.dt.document.workspace", False, 5, False),
                (b"com.apple.dt.document.workspace", True, 0, False)):
            cf, cs = mock.Mock(), mock.Mock()
            cf.CFStringCreateWithCString.side_effect = [1, 2, 3, 4]
            def output(pointer, buffer, length, encoding):
                buffer.value = identifier
                return True
            cf.CFStringGetCString.side_effect = output
            cs.UTTypeCreatePreferredIdentifierForTag.return_value = preferred
            cs.UTTypeConformsTo.return_value = conforms
            with self.subTest(identifier=identifier, conforms=conforms, preferred=preferred), mock.patch.object(
                    ctypes, "CDLL", side_effect=[cf, cs]):
                self.assertIs(namespace["workspace_uti"](), expected)
            self.assertEqual(cf.CFRelease.call_args_list, [mock.call(value) for value in ([5, 4, 3, 2, 1] if preferred else [4, 3, 2, 1])])
            if preferred:
                self.assertEqual(cf.CFStringGetCString.call_args.args[0], 5)
            if expected:
                cs.UTTypeConformsTo.assert_called_once_with(3, 4)

    def test_restricted_macos_uti_failure_blocks_setup_and_model_with_actionable_detail(self):
        with self.macos_environment():
            self.freeze_native("macos")
            self.persist_contract()
            self.stub()
            receipt = self.probe_receipt()
            receipt["checks"]["workspace_uti"] = False
            with self.guards(), self.native_runs([subprocess.CompletedProcess([], 0, json.dumps(receipt), "")]) as run, mock.patch("kanpilot.codex_tool_setup.seed_caches") as seed:
                self.assertEqual(worker.run_real(self.spec), 3)
            seed.assert_not_called()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["env"]["KANPILOT_PROBE_WORKSPACE_UTI"], "1")
            job = self.job()
            self.assertEqual(job["status"], "failed")
            self.assertIsNone(job["session_id"])
            self.assertIn("设置→运行端→执行→Codex CLI", job["detail"])
            self.assertIn("新轮次", job["detail"])
            self.assertFalse(list(self.run_dir.glob("codex-final-*.json.argv")))

    def test_restricted_macos_resume_rechecks_uti_before_second_model(self):
        with self.macos_environment():
            self.freeze_native("macos")
            self.persist_contract()
            self.stub("questions")
            good = self.probe_receipt()
            failed = copy.deepcopy(good)
            failed["checks"]["workspace_uti"] = False
            results = [subprocess.CompletedProcess([], 0, json.dumps(good), ""),
                       subprocess.CompletedProcess([], 0, "ok", ""),
                       subprocess.CompletedProcess([], 0, json.dumps(failed), "")]
            def answer(command, payload, *args):
                if command == "report-execution-question":
                    job = self.job()
                    pending = job["pending_input"]
                    value = {"scope_id": self.spec["scope_id"], "session_id": job["session_id"],
                             "request_id": pending["requestId"], "tool_call_id": pending["toolCallId"],
                             "answers": [["A"]], "operation_id": "answer-op"}
                    (self.run_dir / ("answer-" + util.sha256_text(pending["requestId"]) + ".json")).write_text(util.jdump(value))
            with self.guards(answer), self.native_runs(results) as run, mock.patch("kanpilot.codex_tool_setup.seed_caches", return_value={}) as seed:
                self.assertEqual(worker.run_real(self.spec), 3)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(seed.call_count, 1)
            self.assertEqual(self.job()["session_id"], "thread-1")
            self.assertIn("workspace 类型", self.job()["detail"])
            self.assertTrue((self.run_dir / "codex-final-1.json.argv").exists())
            self.assertFalse((self.run_dir / "codex-final-2.json.argv").exists())


if __name__ == "__main__":
    unittest.main()
