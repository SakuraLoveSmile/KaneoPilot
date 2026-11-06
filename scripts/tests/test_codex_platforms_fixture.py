"""Fixture guard and reset checks; never download tools or start a compiler."""
import importlib.util
import io
import json
import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
