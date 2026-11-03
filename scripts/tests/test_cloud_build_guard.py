"""Build-context guards; never start a compiler, JVM or network call."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ENTRIES = (
    ("bash", "scripts/build_app.sh"),
    ("bash", "scripts/build_dmg.sh"),
    ("bash", "scripts/preview_automation_ui.sh"),
    ("bash", "scripts/preview_session_reader.sh"),
    ("bash", "scripts/preview_desktop_intake.sh"),
    ("python", "scripts/preview_review_resolution.py"),
    ("python", "scripts/preview_verification_checks.py"),
    ("python", "scripts/preview_plan_questions.py"),
    ("python", "macos/tests/preview_execution_questions.py"),
    ("python", "scripts/fetch_python_runtime.py"),
    ("python", "scripts/prepare_python_runtime.py"),
    ("bash", "macos/tests/run-python-runtime-fixtures.sh"),
    ("bash", "macos/tests/run-software-update-fixtures.sh"),
    ("bash", "macos/tests/run-software-update-ui-fixtures.sh"),
    ("bash", "macos/tests/run-software-update-termination-fixtures.sh"),
    ("bash", "macos/tests/run-software-update-appkit-termination-fixtures.sh"),
    ("bash", "android/tests/export-update-acceptance.sh"),
    ("sh", "android/gradlew"),
)


class CloudBuildGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        for relative in ("scripts/require_actions.sh", *(entry[1] for entry in ENTRIES)):
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "unexpected-build-tools.log"
        for name in ("xcodebuild", "swiftc", "java", "mktemp", "mkdir", "codesign", "hdiutil"):
            command = self.bin / name
            command.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$STUB_LOG"\nexit 99\n')
            command.chmod(0o755)
        self.env = os.environ.copy()
        for name in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "KANPILOT_LOCAL_BUILD"):
            self.env.pop(name, None)
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            "STUB_LOG": str(self.log),
            "TMPDIR": str(self.scratch),
            "RUNNER_TEMP": str(self.root / "runner"),
            "APP_OUTPUT_DIR": str(self.root / "app-output"),
            "DERIVED_DATA_DIR": str(self.root / "derived-data"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.original_paths = set(self.repo.rglob("*"))

    def run_command(self, command, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run(command, cwd=self.repo, env=env, capture_output=True,
                              text=True, timeout=10)

    def assert_no_build_side_effects(self):
        self.assertFalse(self.log.exists(), "A build tool or directory creator was invoked")
        self.assertEqual(list(self.scratch.iterdir()), [], "A preview directory was created")
        for name in ("runner", "app-output", "derived-data"):
            self.assertFalse((self.root / name).exists())
        self.assertEqual(set(self.repo.rglob("*")), self.original_paths)

    def test_gate_requires_actions_and_a_github_hosted_runner(self):
        for extra_env in ({}, {"GITHUB_ACTIONS": "true"},
                          {"RUNNER_ENVIRONMENT": "github-hosted"},
                          {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}):
            with self.subTest(env=extra_env):
                result = self.run_command(["sh", "scripts/require_actions.sh"], extra_env)
                self.assertEqual(result.returncode, 1)
                self.assertIn("actions/workflows/packages.yml", result.stderr)
                self.assert_no_build_side_effects()

    def test_gate_accepts_github_hosted_actions_without_starting_a_build(self):
        result = self.run_command(["sh", "scripts/require_actions.sh"],
                                  {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_build_side_effects()

    def stub_platform(self, system="Darwin", machine="arm64"):
        command = self.bin / "uname"
        command.write_text('#!/bin/sh\ncase "$1" in -s) echo %s;; -m) echo %s;; esac\n' % (system, machine))
        command.chmod(0o755)

    def test_local_macos_requires_both_entry_opt_in_and_explicit_environment(self):
        self.stub_platform()
        for args, extra_env, expected in (
            ([], {"KANPILOT_LOCAL_BUILD": "1"}, 1),
            (["--allow-local-macos"], {}, 1),
            (["--allow-local-macos"], {"KANPILOT_LOCAL_BUILD": "1"}, 0),
            (["--allow-local-macos"], {"KANPILOT_LOCAL_BUILD": "1", "GITHUB_ACTIONS": "true",
                                      "RUNNER_ENVIRONMENT": "self-hosted"}, 1),
        ):
            with self.subTest(args=args, env=extra_env):
                result = self.run_command(["sh", "scripts/require_actions.sh", *args], extra_env)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assert_no_build_side_effects()

    def test_local_opt_in_rejects_other_platforms(self):
        for system, machine in (("Linux", "aarch64"), ("Darwin", "x86_64")):
            with self.subTest(system=system, machine=machine):
                self.stub_platform(system, machine)
                result = self.run_command(["sh", "scripts/require_actions.sh", "--allow-local-macos"],
                                          {"KANPILOT_LOCAL_BUILD": "1"})
                self.assertEqual(result.returncode, 1)
                self.assertIn("Apple Silicon", result.stderr)
                self.assert_no_build_side_effects()

    def test_local_environment_check_rejects_the_requested_missing_sdk(self):
        self.stub_platform()
        shutil.copy2(ROOT / "scripts/build_local_macos.sh", self.repo / "scripts/build_local_macos.sh")
        (self.bin / "xcodebuild").write_text('#!/bin/sh\nexit 0\n')
        command = self.bin / "xcrun"
        command.write_text('#!/bin/sh\nif [ "$1" = --sdk ] && [ "$2" = missing-sdk ]; then\n'
                           '  echo "SDK missing-sdk not found" >&2\n  exit 72\nfi\nexit 0\n')
        command.chmod(0o755)
        result = self.run_command(["bash", "scripts/build_local_macos.sh", "--check"],
                                  {"KANPILOT_MACOS_SDK": "missing-sdk"})
        self.assertEqual(result.returncode, 72, result.stderr)
        self.assertIn("SDK missing-sdk not found", result.stderr)
        self.assertFalse((self.repo / "build").exists())
        self.assertFalse(self.log.exists())

    def test_local_macos_opt_in_does_not_open_android_or_delivery_entries(self):
        self.stub_platform()
        for interpreter, relative in ENTRIES:
            if relative in ("scripts/build_app.sh", "scripts/fetch_python_runtime.py",
                            "scripts/prepare_python_runtime.py", "scripts/preview_automation_ui.sh"):
                continue
            with self.subTest(entry=relative):
                executable = sys.executable if interpreter == "python" else interpreter
                result = self.run_command([executable, str(self.repo / relative)], {"KANPILOT_LOCAL_BUILD": "1"})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("actions/workflows/packages.yml", result.stderr)
                self.assert_no_build_side_effects()

    def test_local_and_self_hosted_entries_refuse_before_creating_or_compiling(self):
        for interpreter, relative in ENTRIES:
            for extra_env in ({}, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}):
                with self.subTest(entry=relative, env=extra_env):
                    executable = sys.executable if interpreter == "python" else interpreter
                    result = self.run_command([executable, str(self.repo / relative)], extra_env)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("actions/workflows/packages.yml", result.stderr)
                    self.assert_no_build_side_effects()

    def test_execution_preview_help_stays_available_without_a_build(self):
        result = self.run_command([sys.executable, "macos/tests/preview_execution_questions.py", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--scenario", result.stdout)
        self.assert_no_build_side_effects()


if __name__ == "__main__":
    unittest.main()
