"""Fixture guard and reset checks; never download tools or start a compiler."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("validate_codex_platforms", ROOT / "scripts/validate_codex_platforms.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def bot_receipt(bot, state, *, permission_errno=1):
    return {"config_path": str(state / "tool_state"), "initial_config_exists": False,
            "persisted_config_exists": True, "initial_state": None, "is_bot": bot,
            "persisted_state": bot, "http_client_count": 0 if bot else 1,
            "present_signals": ["BOT"] if bot else [],
            "http_attempts": [] if bot else [{"url": fixture.METADATA_URL, "outcome": "socket_exception",
                                             "errno": permission_errno, "address": "169.254.169.254", "port": 80}]}


class CodexPlatformFixtureTests(unittest.TestCase):
    def test_no_sandbox_runs_directly_and_never_uses_the_sandbox_subcommand(self):
        environment = mock.Mock()
        constants = mock.Mock()
        command = ["/bin/sh", "-c", "flutter analyze --no-pub"]
        spec = {"config": {"sandbox": "danger-full-access"},
                "tool_environment": {"v": 3, "isolation": "none"}}
        self.assertEqual(fixture.native_argv(environment, constants, spec, command, log_denials=True), command)
        environment.assert_not_called()
        environment.launcher_config.assert_not_called()
        constants.codex_bin.assert_not_called()
        with self.assertRaisesRegex(AssertionError, "cannot represent"):
            fixture.sandbox_argv(environment, constants, spec, command)
        spec["tool_environment"]["v"] = 2
        with self.assertRaisesRegex(AssertionError, "requires v3"):
            fixture.native_argv(environment, constants, spec, command)

    def test_default_mode_keeps_real_sandbox_and_original_command_sets(self):
        environment = SimpleNamespace(permission_profile=lambda spec: "frozen", launcher_config=lambda spec: ["-c", "test=1"])
        constants = SimpleNamespace(codex_bin=lambda: "/codex")
        spec = {"config": {}, "cwd": "/fixture"}
        self.assertEqual(fixture.native_argv(environment, constants, spec, ["/tool"], log_denials=True),
                         ["/codex", "sandbox", "-P", "frozen", "-C", "/fixture", "-c", "test=1",
                          "--log-denials", "--", "/tool"])
        full = fixture.verification_commands(Path("/a b/flutter"))
        self.assertEqual(full, ["'/a b/flutter' analyze --no-pub", "'/a b/flutter' test --no-pub",
                                "LANG=en_US.UTF-8 '/a b/flutter' build macos --debug --no-pub",
                                "'/a b/flutter' build apk --debug --no-pub"])
        self.assertEqual(fixture.verification_commands(Path("/a b/flutter"), macos=False),
                         [full[0], full[1], full[3]])
        self.assertEqual(fixture.verification_commands(Path("/a b/flutter"), macos=False, android=False), full[:2])

    def test_android_admission_uses_actual_sdk_capability_and_preserves_blocking_error(self):
        class EnvironmentError(Exception):
            code = "codex_environment_unavailable"
        util = SimpleNamespace(KanpilotError=EnvironmentError, redact=lambda value: value)
        requirement = {"gradle_version": "9.3.1", "project_root": "/fixture/sdk/gradle"}
        denied = {"exists": True, "is_directory": True, "writable": False}
        error = EnvironmentError("Gradle 9.3.1 Flutter SDK /fixture/sdk/gradle 不可写；请在设置选择无沙箱并创建新轮次")
        environment = SimpleNamespace(preflight=mock.Mock(side_effect=error))
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            result = fixture.restricted_android_preflight({}, environment, {}, requirement, denied, util, evidence)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["detail"], str(error))
            self.assertEqual(result["native_commands"], [])
            self.assertFalse(result["native_execution_performed"])
            self.assertEqual(result["native_status"], "not_run")
            self.assertEqual(result["model_calls"], 0)
            environment.preflight = mock.Mock(return_value={"status": "passed"})
            with self.assertRaisesRegex(AssertionError, "disagrees"):
                fixture.restricted_android_preflight({}, environment, {}, requirement, denied, util, evidence)
            allowed = {**denied, "writable": True}
            environment.preflight.return_value = {"status": "passed", "android_gradle_sdk": {**requirement, **allowed}}
            result = fixture.restricted_android_preflight({}, environment, {}, requirement, allowed, util, evidence)
            self.assertEqual(result["status"], "supported")
            self.assertFalse(result["native_execution_performed"])
            environment.preflight.return_value = {"status": "passed"}
            self.assertEqual(fixture.restricted_android_preflight({}, environment, {}, None, None, util, evidence)["status"],
                             "supported")
            environment.preflight.side_effect = EnvironmentError("Gradle cache missing")
            with self.assertRaisesRegex(AssertionError, "specific Gradle SDK"):
                fixture.restricted_android_preflight({}, environment, {}, requirement, denied, util, evidence)
            with self.assertRaisesRegex(AssertionError, "probe is invalid"):
                fixture.restricted_android_preflight({}, environment, {}, requirement, {**denied, "writable": 0}, util, evidence)

    def test_cold_android_refreshes_actual_generated_capability_after_preflight_blocks(self):
        class EnvironmentError(Exception):
            code = "codex_environment_unavailable"
        util = SimpleNamespace(KanpilotError=EnvironmentError, redact=lambda value: value)
        requirement = {"gradle_version": "9.3.1", "project_root": "/fixture/sdk/gradle"}
        denied = {"exists": True, "is_directory": True, "writable": False}
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            local = evidence / "android/local.properties"
            def preflight(spec, base):
                self.assertFalse(local.exists())
                local.parent.mkdir()
                local.write_text("flutter.sdk=/fixture/sdk\n")
                raise EnvironmentError("Gradle 9.3.1 Flutter SDK /fixture/sdk/gradle 不可写；请在设置选择无沙箱并创建新轮次")
            def capability():
                self.assertEqual(local.read_text(), "flutter.sdk=/fixture/sdk\n")
                return requirement, denied
            result = fixture.restricted_android_preflight({}, SimpleNamespace(preflight=preflight), {}, None, None,
                util, evidence, capability=capability)
            self.assertEqual(result["status"], "blocked")
            self.assertIsNone(result["initial_gradle_sdk_requirement"])
            self.assertEqual(result["gradle_sdk_requirement"], requirement)
            self.assertEqual(result["sdk_write_probe"], denied)
            self.assertFalse(result["native_execution_performed"])
            self.assertEqual(result["native_commands"], [])

    def test_macos_preflight_records_actual_uti_block_or_support_without_fallback(self):
        class EnvironmentError(Exception):
            code = "codex_environment_unavailable"
        util = SimpleNamespace(KanpilotError=EnvironmentError, redact=lambda value: value)
        error = EnvironmentError("当前受限沙箱无法识别 Xcode workspace；请在设置选择无沙箱")
        environment = SimpleNamespace(preflight=mock.Mock(side_effect=error))
        uti = {"outside": {"workspace_uti": True}, "inside": {"workspace_uti": False}}
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            result = fixture.restricted_macos_preflight({}, environment, {}, uti, util, evidence)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["detail"], str(error))
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["uti"], uti)
            environment.preflight.assert_called_once_with({}, {})
            environment.preflight = mock.Mock(return_value={"status": "passed", "checks": {"workspace_uti": True}})
            with self.assertRaisesRegex(AssertionError, "disagrees"):
                fixture.restricted_macos_preflight({}, environment, {}, uti, util, evidence)
            uti["inside"]["workspace_uti"] = True
            self.assertEqual(fixture.restricted_macos_preflight({}, environment, {}, uti, util, evidence)["status"],
                             "supported")
            environment.preflight = mock.Mock(side_effect=EnvironmentError("unrelated Gradle failure"))
            with self.assertRaisesRegex(AssertionError, "specific UTI"):
                fixture.restricted_macos_preflight({}, environment, {}, uti, util, evidence)

    def test_diagnostic_success_cannot_replace_original_verify_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            spec = {"config": {"sandbox": "danger-full-access"}, "cwd": str(evidence),
                    "tool_environment": {"v": 3, "isolation": "none"},
                    "required_verification_commands": ["original verify"]}
            def run(argv, **kwargs):
                label = kwargs["label"]
                fixture.write_json(evidence / (label + ".json"),
                                   {"exit_code": 7 if label == "free-verify-1" else 0, "log": label + ".log"})
            with mock.patch.object(fixture, "run", side_effect=run) as process:
                with self.assertRaisesRegex(AssertionError, "original native commands failed"):
                    fixture.verify_native(spec, mock.Mock(), mock.Mock(), mock.Mock(), {}, evidence, label="free")
            records = json.loads((evidence / "free-native-results.json").read_text())
            self.assertEqual(records[0]["exit_code"], 7)
            self.assertEqual(records[0]["command"], "original verify")
            self.assertEqual(process.call_count, 2)

    def test_restricted_failure_keeps_native_results_and_runs_all_unrestricted_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            report = {"status": "failed"}
            shell_python = shlex.quote(sys.executable)
            restricted = {"cwd": str(evidence), "required_verification_commands":
                          [shell_python + " -c 'raise SystemExit(7)'"]}
            unrestricted = {"cwd": str(evidence), "required_verification_commands":
                            [shell_python + " -c 'print(" + str(index) + ")'" for index in range(4)]}
            # Exercise real isolated Python processes; replacing only the
            # platform sandbox wrapper avoids invoking installed tools.
            with mock.patch.object(fixture, "native_argv", side_effect=lambda env, constants, spec, command, **kw: command), \
                    mock.patch.object(fixture.sys, "stderr", io.StringIO()):
                with fixture.mode_attempt(report, "restricted", evidence):
                    report["restricted"]["native_commands"] = fixture.verify_native(
                        restricted, mock.Mock(), mock.Mock(), mock.Mock(), os.environ.copy(), evidence, label="restricted")
                    report["restricted"]["status"] = "passed"
                with fixture.mode_attempt(report, "unrestricted", evidence):
                    report["unrestricted"]["native_commands"] = fixture.verify_native(
                        unrestricted, mock.Mock(), mock.Mock(), mock.Mock(), os.environ.copy(), evidence, label="unrestricted")
                    report["unrestricted"]["status"] = "passed"
            self.assertEqual(report["restricted"]["status"], "failed")
            self.assertEqual(report["restricted"]["native_commands"][0]["exit_code"], 7)
            self.assertIn("original native commands failed", report["restricted"]["error"])
            self.assertEqual(report["unrestricted"]["status"], "passed")
            self.assertEqual([item["exit_code"] for item in report["unrestricted"]["native_commands"]], [0] * 4)
            self.assertEqual(json.loads((evidence / "restricted-failure.json").read_text()), report["restricted"])
            with self.assertRaisesRegex(AssertionError, "acceptance failed: restricted"):
                fixture.require_modes_passed(report)
            self.assertEqual(report["status"], "failed")

    def test_native_timeout_preserves_partial_logs_and_prevents_shared_resource_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence"
            evidence.mkdir()
            report = {"status": "failed"}
            spec = {"cwd": str(evidence), "required_verification_commands": ["native verify"]}
            second_mode = mock.Mock()
            def timeout(argv, **kwargs):
                kwargs["stdout"].write("partial native output\n")
                kwargs["stderr"].write("partial native error\n")
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            with mock.patch.object(fixture.subprocess, "run", side_effect=timeout), \
                    mock.patch.object(fixture, "native_argv", side_effect=lambda env, constants, spec, command, **kw: command), \
                    mock.patch.object(fixture.sys, "stderr", io.StringIO()):
                with self.assertRaises(subprocess.TimeoutExpired):
                    with fixture.mode_attempt(report, "restricted", evidence):
                        fixture.verify_native(spec, mock.Mock(), mock.Mock(), mock.Mock(), {}, evidence, label="restricted")
                    with fixture.mode_attempt(report, "unrestricted", evidence):
                        second_mode()
            second_mode.assert_not_called()
            self.assertNotIn("unrestricted", report)
            failure = json.loads((evidence / "restricted-failure.json").read_text())
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["shared_resource_reuse"], "blocked_descendant_cleanup_unconfirmed")
            native = failure["native_commands"][0]
            self.assertEqual(native["command"], "native verify")
            self.assertIsNone(native["exit_code"])
            self.assertTrue(native["timed_out"])
            self.assertEqual(native["descendant_cleanup"], "unconfirmed")
            self.assertEqual((evidence / native["log"]).read_text(), "partial native output\n")
            self.assertEqual((evidence / native["stderr_log"]).read_text(), "partial native error\n")
            self.assertEqual(native["log_sha256"], fixture.sha256(evidence / native["log"]))

    def test_timeout_snapshots_stay_stable_when_inherited_log_descriptors_keep_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            evidence.mkdir()
            descriptors = []
            limit = 4 * 1024 * 1024
            def timeout(argv, **kwargs):
                for name, data in (("stdout", "prefix" + "x" * limit + "tail"),
                                   ("stderr", "initial stderr\n")):
                    stream = kwargs[name]
                    stream.write(data)
                    stream.flush()
                    descriptors.append(os.dup(stream.fileno()))
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            try:
                with mock.patch.object(fixture.subprocess, "run", side_effect=timeout):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        fixture.run(["never-started"], cwd=root, env={}, evidence=evidence,
                                    label="inherited", separate_stderr=True)
                record = json.loads((evidence / "inherited.json").read_text())
                snapshots = {}
                for log_key, hash_key, snapshot_key in (("log", "log_sha256", "log_snapshot"),
                                                       ("stderr_log", "stderr_sha256", "stderr_snapshot")):
                    path = evidence / record[log_key]
                    snapshots[log_key] = path.read_bytes()
                    self.assertEqual(record[hash_key], fixture.sha256(path))
                    metadata = record[snapshot_key]
                    self.assertEqual(metadata["kind"], "bounded_tail_snapshot")
                    self.assertEqual(metadata["max_bytes"], limit)
                    self.assertEqual(metadata["snapshot_bytes"], len(snapshots[log_key]))
                    self.assertTrue(metadata["raw_may_still_be_written"])
                    self.assertEqual((root / metadata["raw_path"]).parent, root / "unconfirmed-live-logs")
                self.assertTrue(record["log_snapshot"]["truncated"])
                self.assertEqual(len(snapshots["log"]), limit)
                self.assertTrue(snapshots["log"].endswith(b"tail"))
                self.assertFalse(record["stderr_snapshot"]["truncated"])
                for descriptor in descriptors:
                    os.write(descriptor, b"late descendant output\n")
                for log_key, hash_key, snapshot_key in (("log", "log_sha256", "log_snapshot"),
                                                       ("stderr_log", "stderr_sha256", "stderr_snapshot")):
                    self.assertEqual((evidence / record[log_key]).read_bytes(), snapshots[log_key])
                    self.assertEqual(fixture.sha256(evidence / record[log_key]), record[hash_key])
                    self.assertTrue((root / record[snapshot_key]["raw_path"]).read_bytes().endswith(b"late descendant output\n"))
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

    def test_diagnostic_timeout_also_stops_shared_resource_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            report = {"status": "failed"}
            spec = {"cwd": str(evidence), "required_verification_commands": ["native verify"]}
            def run(argv, **kwargs):
                label = kwargs["label"]
                if "diagnostic" in label:
                    raise subprocess.TimeoutExpired(argv, 300)
                fixture.write_json(evidence / (label + ".json"), {"exit_code": 7, "log": "original.log"})
            with mock.patch.object(fixture, "run", side_effect=run), \
                    mock.patch.object(fixture, "native_argv", side_effect=lambda env, constants, spec, command, **kw: command), \
                    mock.patch.object(fixture.sys, "stderr", io.StringIO()):
                with self.assertRaises(subprocess.TimeoutExpired):
                    with fixture.mode_attempt(report, "restricted", evidence):
                        fixture.verify_native(spec, mock.Mock(), mock.Mock(), mock.Mock(), {}, evidence, label="restricted")
            self.assertEqual(report["restricted"]["native_commands"][0]["exit_code"], 7)
            self.assertEqual(report["restricted"]["shared_resource_reuse"], "blocked_descendant_cleanup_unconfirmed")

    def test_mode_error_before_native_commands_does_not_skip_other_mode_or_allow_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            report = {"status": "failed"}
            with mock.patch.object(fixture.sys, "stderr", io.StringIO()):
                with fixture.mode_attempt(report, "restricted", evidence):
                    raise RuntimeError("preflight unavailable")
                with fixture.mode_attempt(report, "unrestricted", evidence):
                    report["unrestricted"]["status"] = "passed"
            self.assertEqual(report["restricted"]["error"], "preflight unavailable")
            self.assertEqual(report["restricted"]["error_type"], "RuntimeError")
            self.assertNotIn("native_commands", report["restricted"])
            with self.assertRaisesRegex(AssertionError, "restricted"):
                fixture.require_modes_passed(report)
            report["restricted"]["status"] = "passed"
            report["unrestricted"]["status"] = "failed"
            with self.assertRaisesRegex(AssertionError, "unrestricted"):
                fixture.require_modes_passed(report)
            report["unrestricted"]["status"] = "passed"
            fixture.require_modes_passed(report)

    def test_launcher_receipts_reject_restricted_parameters_for_exec_and_resume(self):
        def launcher(spec, schema, output, provider, session_id=None):
            return ["codex", "exec", "-s", "danger-full-access", 'approval_policy="never"',
                    *(["resume", session_id] if session_id else []), "-"]
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            adapter = SimpleNamespace(launcher_argv=mock.Mock(side_effect=launcher))
            receipt = fixture.launcher_evidence({}, adapter, evidence)
            self.assertEqual(set(receipt), {"exec", "resume"})
            self.assertIn("fixture-session-no-model", receipt["resume"])
            self.assertNotIn("resume", receipt["exec"])
            self.assertEqual(adapter.launcher_argv.call_count, 2)
            adapter.launcher_argv = lambda *args, **kwargs: [*launcher(*args, **kwargs), "--add-dir", "/cache"]
            with self.assertRaisesRegex(AssertionError, "retained restricted"):
                fixture.launcher_evidence({}, adapter, evidence)

    def test_sdk_plugin_reset_only_removes_its_two_generated_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            flutter = sdk / "bin/flutter"
            plugin = sdk / "packages/flutter_tools/gradle"
            for name in (".gradle", "build", "src"):
                (plugin / name).mkdir(parents=True)
                (plugin / name / "sentinel").write_text(name)
            (plugin / "build.gradle.kts").write_text("source")
            fixture.reset_sdk_plugin_generated(flutter)
            self.assertFalse((plugin / ".gradle").exists())
            self.assertFalse((plugin / "build").exists())
            self.assertEqual((plugin / "src/sentinel").read_text(), "src")
            self.assertEqual((plugin / "build.gradle.kts").read_text(), "source")
            (plugin / "build").symlink_to(plugin / "src", target_is_directory=True)
            with self.assertRaisesRegex(AssertionError, "symlink"):
                fixture.reset_sdk_plugin_generated(flutter)

    def test_xcode_private_entries_are_found_in_selected_bundle_and_remain_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            contents = Path(temporary) / "Pinned.app/Contents"
            developer = contents / "Developer"
            developer.mkdir(parents=True)
            core = contents / "Frameworks/Xcode3Core.framework/Versions/A/Xcode3Core"
            swiftpm = contents / "SharedFrameworks/SwiftPM.framework/Versions/A/SwiftPM"
            core.parent.mkdir(parents=True)
            swiftpm.parent.mkdir(parents=True)
            core_entry = core.parents[2] / "Xcode3Core"
            swiftpm_entry = swiftpm.parents[2] / "SwiftPM"
            core_entry.symlink_to("Versions/A/Xcode3Core")
            swiftpm_entry.symlink_to("Versions/A/SwiftPM")
            # Include an entry split across the streaming read boundary.
            core.write_bytes(b"x" * (1024 * 1024 - 4) + b"XCODE_USER_DEFAULTS_FILE")
            swiftpm.write_bytes(b"IDEPackageSupportDisableManifestSandbox")
            found = fixture.xcode_defaults_support(developer)
            self.assertEqual({item["binary"] for item in found}, {str(core.resolve()), str(swiftpm.resolve())})
            swiftpm.write_bytes(b"missing compatibility entry")
            with self.assertRaisesRegex(AssertionError, "lacks the required"):
                fixture.xcode_defaults_support(developer)
            swiftpm.unlink()
            outside = Path(temporary) / "SwiftPM"
            outside.write_bytes(b"IDEPackageSupportDisableManifestSandbox")
            swiftpm.symlink_to(outside)
            with self.assertRaisesRegex(AssertionError, "escapes its bundle"):
                fixture.xcode_defaults_support(developer)

    def test_machine_output_keeps_stderr_diagnostics_out_of_strict_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            command = [sys.executable, "-c", "import json,sys; "
                       "print('Building flutter tool...',file=sys.stderr); "
                       "print('Resolving tool dependencies...',file=sys.stderr); "
                       "print(json.dumps({'frameworkVersion':'3.47.7'}))"]
            log = fixture.run(command, cwd=evidence, env=os.environ.copy(), evidence=evidence,
                              label="machine", separate_stderr=True)
            self.assertEqual(json.loads(log.read_text()), {"frameworkVersion": "3.47.7"})
            errors = evidence / "machine.stderr.log"
            self.assertEqual(errors.read_text(), "Building flutter tool...\nResolving tool dependencies...\n")
            record = json.loads((evidence / "machine.json").read_text())
            self.assertEqual(record["exit_code"], 0)
            self.assertEqual(record["stderr_log"], errors.name)
            self.assertEqual(record["stderr_sha256"], fixture.sha256(errors))

    def test_machine_output_does_not_filter_non_json_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            log = fixture.run([sys.executable, "-c", "print('bootstrap on stdout'); print('{}')"],
                              cwd=evidence, env=os.environ.copy(), evidence=evidence, label="polluted",
                              separate_stderr=True)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(log.read_text())
            self.assertEqual(log.read_text(), "bootstrap on stdout\n{}\n")

    def test_native_build_output_stays_in_files_and_actual_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            diagnostic = io.StringIO()
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("must not read full native log")), \
                    mock.patch.object(fixture.sys, "stderr", diagnostic):
                log = fixture.run([sys.executable, "-c", "import sys; sys.stdout.write('x'*200000); sys.exit(7)"],
                                  cwd=evidence, env=os.environ.copy(), evidence=evidence, label="native", check=False)
            self.assertEqual(log.stat().st_size, 200000)
            self.assertEqual(len(diagnostic.getvalue()), 12001)
            record = json.loads((evidence / "native.json").read_text())
            self.assertEqual(record["exit_code"], 7)
            self.assertNotIn("stderr_log", record)

    def test_restricted_base_removes_every_bot_signal_without_changing_tool_paths(self):
        original = {name: "false" for name in fixture.BOT_DETECTOR_SIGNALS}
        original.update(PUB_CACHE="/fixture/pub", GRADLE_USER_HOME="/fixture/gradle", JAVA_HOME="/fixture/java")
        original["ORG_GRADLE_PROJECT_kotlin.compiler.execution.strategy"] = "in-process"
        actual = fixture.restricted_environment(original)
        self.assertEqual(actual, {"PUB_CACHE": "/fixture/pub", "GRADLE_USER_HOME": "/fixture/gradle",
                                  "JAVA_HOME": "/fixture/java"})
        self.assertEqual(original["CI"], "false")
        self.assertEqual(original["BOT"], "false")

    def test_exec_boundary_removes_alternate_signals_and_can_remove_bot_itself(self):
        # Run the actual env wrapper with all possible inherited alternatives,
        # so containsKey signals (including CI=false) cannot warm the fixture.
        env = dict(os.environ, **{name: "false" for name in fixture.BOT_DETECTOR_SIGNALS})
        env["BOT"] = "true"
        positive = subprocess.run(fixture.without_ci_signals(
            [sys.executable, "-c", fixture._BOT_ENV_PROBE, json.dumps(fixture.BOT_DETECTOR_SIGNALS)]),
            env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(positive.returncode, 0, positive.stdout + positive.stderr)
        no_bot_probe = "import os,sys; sys.exit(any(k in os.environ for k in sys.argv[1:]))"
        negative = subprocess.run(fixture.without_ci_signals(
            [sys.executable, "-c", no_bot_probe, *fixture.BOT_DETECTOR_SIGNALS], remove_bot=True),
            env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(negative.returncode, 0, negative.stdout + negative.stderr)

    def test_metadata_counterexample_requires_a_denial_for_the_exact_endpoint(self):
        self.assertEqual(fixture.metadata_denials("Package config missing; exit 1"), [])
        self.assertEqual(fixture.metadata_denials("Contacting 169.254.169.254\nPackage config missing; exit 1"), [])
        self.assertEqual(fixture.metadata_denials("deny network-outbound 192.0.2.1:80"), [])
        self.assertEqual(fixture.metadata_denials("Permission denied\nContacting 169.254.169.254"), [])
        self.assertTrue(fixture.metadata_denials("Sandbox: dart deny(1) network-outbound 169.254.169.254:80"))
        self.assertTrue(fixture.metadata_denials("SocketException: Operation not permitted, address = 169.254.169.254"))

    def test_explicit_bot_detector_receipts_require_the_complete_cold_paths(self):
        state = Path("/controlled/xdg")
        for bot in (True, False):
            expected = bot_receipt(bot, state)
            self.assertEqual(fixture.parse_bot_detector_receipt(json.dumps(expected), bot=bot, state=state), expected)
            mutations = [{**expected, "initial_state": bot}, {**expected, "is_bot": int(bot)},
                         {**expected, "persisted_state": not bot}, {**expected, "http_client_count": bot},
                         {**expected, "present_signals": ["CI"]}, {**expected, "config_path": "/home/tool_state"},
                         {**expected, "initial_config_exists": True}, {**expected, "persisted_config_exists": False}]
            for changed in mutations:
                with self.subTest(bot=bot, changed=changed), self.assertRaisesRegex(AssertionError, "receipt differs"):
                    fixture.parse_bot_detector_receipt(json.dumps(changed), bot=bot, state=state)
            with self.assertRaisesRegex(AssertionError, "fields differ"):
                fixture.parse_bot_detector_receipt(json.dumps({**expected, "extra": True}), bot=bot, state=state)
        with self.assertRaises(json.JSONDecodeError):
            fixture.parse_bot_detector_receipt("bootstrap on stdout\n{}", bot=False, state=state)
        for code in (None, True, 0, 8, 60, 61, 65, 403):
            with self.subTest(errno=code), self.assertRaisesRegex(AssertionError, "permission errno"):
                fixture.parse_bot_detector_receipt(json.dumps(bot_receipt(False, state, permission_errno=code)),
                                                   bot=False, state=state)
        fixture.parse_bot_detector_receipt(json.dumps(bot_receipt(False, state, permission_errno=13)), bot=False, state=state)
        for updates in ({"url": "http://192.0.2.1/metadata/instance"}, {"outcome": "connected"}):
            changed = bot_receipt(False, state)
            changed["http_attempts"][0].update(updates)
            with self.assertRaisesRegex(AssertionError, "permission errno"):
                fixture.parse_bot_detector_receipt(json.dumps(changed), bot=False, state=state)

    def test_explicit_getter_uses_pinned_sdk_packages_same_profile_and_separate_cold_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            evidence.mkdir()
            flutter = root / "sdk/bin/flutter"
            dart = root / "sdk/bin/cache/dart-sdk/bin/dart"
            packages = root / "sdk/packages/flutter_tools/.dart_tool/package_config.json"
            dart.parent.mkdir(parents=True)
            packages.parent.mkdir(parents=True)
            dart.write_text("never executed")
            packages.write_text('{"packages": []}')
            source = evidence / "bot-detector-probe.dart"
            source.write_text(fixture.BOT_DETECTOR_PROBE)
            spec = {"cwd": str(root), "config": {}}
            environment = SimpleNamespace(permission_profile=lambda spec: "frozen-analysis", launcher_config=lambda spec: [])
            constants = SimpleNamespace(codex_bin=lambda: "/pinned/codex")
            captured = []
            include_denial = True
            def run(argv, **kwargs):
                captured.append(argv)
                state = Path(argv[-1])
                self.assertFalse(state.exists())
                state.mkdir()
                bot = kwargs["label"] == "cold-with-bot"
                receipt = bot_receipt(bot, state)
                (state / "tool_state").write_text(json.dumps({"is-bot": bot}))
                log = evidence / (kwargs["label"] + ".log")
                errors = evidence / (kwargs["label"] + ".stderr.log")
                log.write_text(json.dumps(receipt) + "\n")
                errors.write_text("Sandbox: dart deny(1) network-outbound 169.254.169.254:80\n"
                                  if not bot and include_denial else "")
                fixture.write_json(evidence / (kwargs["label"] + ".json"),
                    {"argv": argv, "exit_code": 0, "log": log.name, "log_sha256": fixture.sha256(log),
                     "stderr_log": errors.name, "stderr_sha256": fixture.sha256(errors)})
                return log
            with mock.patch.object(fixture, "run", side_effect=run) as process:
                positive, _ = fixture.bot_detector_probe(flutter, source, root / "positive", spec,
                    environment, constants, {"BOT": "true"}, evidence, label="cold-with-bot", bot=True)
                negative, denied = fixture.bot_detector_probe(flutter, source, root / "negative", spec,
                    environment, constants, {"BOT": "true"}, evidence, label="cold-without-bot", bot=False)
                self.assertEqual(positive["receipt"]["http_client_count"], 0)
                self.assertEqual(negative["receipt"]["http_client_count"], 1)
                self.assertTrue(denied)
                self.assertEqual(positive["source_sha256"], fixture.sha256(source))
                self.assertEqual(negative["package_config_sha256"], fixture.sha256(packages))
                self.assertNotEqual(positive["state_directory"], negative["state_directory"])
                for argv in captured:
                    self.assertEqual(argv[:6], ["/pinned/codex", "sandbox", "-P", "frozen-analysis", "-C", str(root)])
                    self.assertIn("--log-denials", argv)
                    self.assertEqual(argv[-4:-1], [str(dart), "--packages=" + str(packages), str(source)])
                    self.assertIn("XDG_CONFIG_HOME=" + argv[-1], argv)
                    self.assertNotIn("test", argv)
                self.assertNotIn("BOT", captured[0])
                self.assertIn("BOT", captured[1])
                with self.assertRaisesRegex(AssertionError, "not cold"):
                    fixture.bot_detector_probe(flutter, source, root / "negative", spec,
                        environment, constants, {}, evidence, label="warm", bot=False)
                self.assertEqual(process.call_count, 2)
                include_denial = False
                with self.assertRaisesRegex(AssertionError, "specific 169.254.169.254 denial"):
                    fixture.bot_detector_probe(flutter, source, root / "no-denial", spec,
                        environment, constants, {}, evidence, label="missing-denial", bot=False)

    def test_real_flutter_tool_state_snapshot_binds_only_the_frozen_task_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, evidence = root / "frozen-xdg", root / "evidence"
            config.mkdir()
            evidence.mkdir()
            state = config / "tool_state"
            state.write_text('{"is-bot": true, "task-owned": "sentinel"}\n')
            result = fixture.snapshot_flutter_tool_state(config, evidence)
            self.assertEqual(result["source_path"], str(state))
            self.assertTrue(result["is_bot"])
            self.assertEqual((evidence / result["artifact"]).read_bytes(), state.read_bytes())
            self.assertEqual(result["sha256"], fixture.sha256(evidence / result["artifact"]))
            state.write_text('{"is-bot": false}')
            with self.assertRaisesRegex(AssertionError, "expected BOT"):
                fixture.snapshot_flutter_tool_state(config, evidence)
            state.unlink()
            with self.assertRaisesRegex(AssertionError, "not created"):
                fixture.snapshot_flutter_tool_state(config, evidence)

    def test_local_entry_refuses_before_creating_directories_or_starting_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-exist"
            env = {key: value for key, value in os.environ.items()
                   if key not in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT")}
            for additions in ({}, {"GITHUB_ACTIONS": "true"},
                              {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}):
                with self.subTest(env=additions), mock.patch.dict(os.environ, {**env, **additions}, clear=True), \
                        mock.patch.object(fixture.subprocess, "run") as process:
                    with self.assertRaisesRegex(AssertionError, "GitHub-hosted"):
                        fixture.validate(SimpleNamespace(output=str(output), flutter="/missing/flutter"))
                    process.assert_not_called()
                    self.assertFalse(output.exists())

    def test_cloud_entry_requires_macos_arm64(self):
        with mock.patch.dict(os.environ, GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted"):
            for system, machine in (("Linux", "aarch64"), ("Darwin", "x86_64")):
                with self.subTest(system=system, machine=machine), \
                        mock.patch.object(fixture.platform, "system", return_value=system), \
                        mock.patch.object(fixture.platform, "machine", return_value=machine):
                    with self.assertRaisesRegex(AssertionError, "macOS arm64"):
                        fixture.require_cloud()

    def test_generated_reset_requires_fixture_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            (app / "build").mkdir()
            (app / "build/evidence").write_text("preserve")
            with self.assertRaisesRegex(AssertionError, "owned"):
                fixture.reset_generated(app)
            self.assertEqual((app / "build/evidence").read_text(), "preserve")

    def test_generated_reset_preserves_source_and_refuses_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, external = root / "app", root / "external"
            app.mkdir()
            external.mkdir()
            (external / "sentinel").write_text("preserve")
            (app / ".kanpilot-codex-platform-fixture").write_text("owned")
            (app / "pubspec.lock").write_text("locked")
            (app / "android").mkdir()
            (app / "android/local.properties").write_text("generated warm paths")
            (app / "android/settings.gradle.kts").write_text("source settings")
            (app / "lib").mkdir()
            (app / "lib/main.dart").write_text("source")
            (app / "build").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(AssertionError, "symlink"):
                fixture.reset_generated(app)
            self.assertEqual((external / "sentinel").read_text(), "preserve")
            (app / "build").unlink()
            (app / "build").mkdir()
            (app / "build/product").write_text("generated")
            fixture.reset_generated(app)
            self.assertFalse((app / "build").exists())
            self.assertEqual((app / "pubspec.lock").read_text(), "locked")
            self.assertEqual((app / "lib/main.dart").read_text(), "source")
            self.assertFalse((app / "android/local.properties").exists())
            self.assertEqual((app / "android/settings.gradle.kts").read_text(), "source settings")


if __name__ == "__main__":
    unittest.main()
