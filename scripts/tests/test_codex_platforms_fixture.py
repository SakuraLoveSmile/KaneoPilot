"""Fixture guard and reset checks; never download tools or start a compiler."""
import importlib.util
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
    def test_restricted_base_removes_every_bot_signal_without_changing_tool_paths(self):
        original = {name: "false" for name in fixture.BOT_DETECTOR_SIGNALS}
        original.update(PUB_CACHE="/fixture/pub", GRADLE_USER_HOME="/fixture/gradle", JAVA_HOME="/fixture/java")
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
