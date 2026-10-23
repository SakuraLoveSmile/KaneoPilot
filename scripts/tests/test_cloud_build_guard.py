"""Cloud-only build entry checks; never start a compiler, JVM or network call."""
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
        for name in ("xcodebuild", "java", "mktemp", "mkdir", "codesign", "hdiutil"):
            command = self.bin / name
            command.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$STUB_LOG"\nexit 99\n')
            command.chmod(0o755)
        self.env = os.environ.copy()
        for name in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"):
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
