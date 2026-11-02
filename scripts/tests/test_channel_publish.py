"""Test-channel release creation, crash/retry and channel isolation checks. No network."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest

HERE = Path(__file__).parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_bundle = load("release_bundle", HERE.parent / "release_bundle.py")
stable_tests = load("stable_release_tests", HERE / "test_release_bundle.py")
SHA = "a" * 40
VERSION = "0.2.1-test.42"
TAG = "v" + VERSION
BUILD = 42
BRANCH = "feat/test-channel"
NOTES = "Try the new update channel.\n\nSecond paragraph."
RUN_URL = "https://github.com/test/private/actions/runs/123"
MARKER = "<!-- kaneopilot-ci:%s:%s -->" % (SHA, BUILD)


class FakeTestChannelGitHub(stable_tests.FakeGitHub):
    """The stable fake plus the two Git-data endpoints that create an annotated tag."""

    def __init__(self):
        super().__init__()
        self.tag = None
        self.annotation = None
        self.created = []
        self.requests = []

    def request(self, method, path, data=None, binary=False, upload=None):
        self.requests.append((method, path))
        if method == "GET" and "/git/ref/" in path and self.tag is None:
            return None
        if method == "POST" and path.endswith("/git/tags"):
            self.writes.append((method, path))
            self.created.append(data)
            self.annotation = {"tag": data["tag"], "object": {"type": data["type"], "sha": data["object"]},
                               "message": data["message"]}
            return {"sha": "f" * 40}
        if method == "POST" and path.endswith("/git/refs"):
            self.writes.append((method, path))
            self.created.append(data)
            self.tag = {"type": "tag", "sha": data["sha"]}
            return {}
        return super().request(method, path, data, binary, upload)


def build_bundle(root, version, tag, build, branch="", macos_name=None):
    for platform, name in (("android", "android.apk"), ("macos", macos_name or "macos-arm64.dmg")):
        package = root / ("KaneoPilot-%s-%s" % (version, name))
        package.write_bytes((platform + " signed fixture").encode())
        release_bundle.metadata(types.SimpleNamespace(
            package=package, platform=platform, fingerprint="b" * 64, version=version,
            source_sha=SHA, build_number=build,
            architectures="any" if platform == "android" else "arm64",
            output=root / (platform + "-metadata.json")))
    return release_bundle.prepare(root, tag, SHA, build, branch)


class TestChannelPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = FakeTestChannelGitHub()
        self.manifest = build_bundle(self.root, VERSION, TAG, BUILD, BRANCH)

    def publish(self, **overrides):
        options = {"channel": "test", "notes": NOTES, "branch": BRANCH, "run_url": RUN_URL}
        options.update(overrides)
        return release_bundle.publish(self.api, self.root, TAG, SHA, BUILD, **options)

    def test_manifest_marks_the_test_channel_and_its_source(self):
        self.assertEqual(self.manifest["channel"], "test")
        self.assertEqual(self.manifest["base_version"], "0.2.1")
        self.assertEqual(self.manifest["branch"], BRANCH)
        self.assertEqual((self.manifest["version"], self.manifest["tag"], self.manifest["build_number"]), (VERSION, TAG, BUILD))
        self.assertEqual({item["version"] for item in self.manifest["artifacts"]}, {VERSION})
        self.assertEqual(json.loads((self.root / "manifest.json").read_text()), self.manifest)
        self.assertIn("manifest.json", (self.root / "SHA256SUMS").read_text())

    def test_a_stable_manifest_keeps_its_historical_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_bundle(Path(directory), "1.2.3", "v1.2.3", 7, "feature/ignored")
        self.assertEqual(set(manifest), {"version", "tag", "source_sha", "build_number", "artifacts", "macos_requirements"})

    def test_new_build_creates_the_tag_then_a_draft_prerelease_then_publishes_without_latest(self):
        self.api.latest = {"tag_name": "unknown"}
        self.publish()
        tag_object, reference = self.api.created
        self.assertEqual((tag_object["tag"], tag_object["object"], tag_object["type"]), (TAG, SHA, "commit"))
        for line in (NOTES, "Channel: test", "Branch: " + BRANCH, "Commit: " + SHA, "Build: 42", "Run: " + RUN_URL):
            self.assertIn(line, tag_object["message"])
        self.assertEqual(tag_object["tagger"]["name"], "github-actions[bot]")
        self.assertRegex(tag_object["tagger"]["date"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(reference, {"ref": "refs/tags/" + TAG, "sha": "f" * 40})
        kinds = [(method, path.rsplit("/", 1)[1].split("?")[0]) for method, path in self.api.writes]
        self.assertEqual(kinds[:3], [("POST", "tags"), ("POST", "refs"), ("POST", "releases")])
        self.assertEqual([method for method, _ in self.api.writes[3:]], ["POST"] * 4 + ["PATCH"])
        release = self.api.release
        self.assertFalse(release["draft"])
        self.assertIs(release["prerelease"], True)
        self.assertNotIn("make_latest", release)
        self.assertIn(MARKER, release["body"])
        self.assertIn(tag_object["message"].strip(), release["body"])
        self.assertEqual(len(self.api.assets), 4)
        self.assertFalse([path for _, path in self.api.requests if path.endswith("/releases/latest")])

    def test_completed_publish_rerun_is_read_only(self):
        self.publish()
        self.api.writes.clear()
        self.publish()
        self.assertEqual(self.api.writes, [])

    def test_interrupted_draft_recovers_only_missing_assets_and_never_retags(self):
        self.api.interrupt_after = 1
        with self.assertRaisesRegex(RuntimeError, "connection loss"):
            self.publish()
        self.assertTrue(self.api.release["draft"])
        first = self.api.assets[0].copy()
        self.api.add("operator-note.txt", b"keep me")
        self.api.hide_draft_from_tag = True
        self.api.interrupt_after = None
        self.api.writes.clear()
        self.publish()
        self.assertEqual(self.api.assets[0], first)
        self.assertEqual(len(self.api.assets), 5)
        self.assertFalse(self.api.release["draft"])
        self.assertFalse([item for item in self.api.writes if item[1].endswith(("/git/tags", "/git/refs", "/releases"))])

    def test_existing_tag_keeps_its_own_message(self):
        self.api.tag = {"type": "tag", "sha": "d" * 40}
        self.api.annotation = {"tag": TAG, "object": {"type": "commit", "sha": SHA}, "message": "Notes from the first attempt\n\nChannel: test"}
        self.publish(notes="Different notes on the rerun")
        self.assertEqual(self.api.created, [])
        self.assertIn("Notes from the first attempt", self.api.release["body"])
        self.assertNotIn("Different notes", self.api.release["body"])

    def test_remote_tag_for_another_commit_aborts_before_writes(self):
        self.api.tag = {"type": "tag", "sha": "d" * 40}
        self.api.annotation = {"tag": TAG, "object": {"type": "commit", "sha": "c" * 40}, "message": "Moved"}
        with self.assertRaisesRegex(ValueError, "tag no longer"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_remote_lightweight_or_nested_or_renamed_tag_aborts_before_writes(self):
        cases = (({"type": "commit", "sha": SHA}, None, "annotated tag"),
                 ({"type": "tag", "sha": "d" * 40}, {"tag": TAG, "object": {"type": "tag", "sha": "e" * 40}, "message": "Nested"}, "tag no longer"),
                 ({"type": "tag", "sha": "d" * 40}, {"tag": "v0.2.1-test.41", "object": {"type": "commit", "sha": SHA}, "message": "Renamed"}, "identity mismatch"),
                 ({"type": "tag", "sha": "d" * 40}, {"tag": TAG, "object": {"type": "commit", "sha": SHA}, "message": "  \n"}, "release notes"))
        for tag, annotation, expected in cases:
            with self.subTest(expected=expected):
                self.api = FakeTestChannelGitHub()
                self.api.tag, self.api.annotation = tag, annotation
                with self.assertRaisesRegex(ValueError, expected):
                    self.publish()
                self.assertEqual(self.api.writes, [])

    def test_another_builds_draft_blocks_before_the_tag_is_created(self):
        self.api.release = {"id": 9, "draft": True, "body": "human-created", "tag_name": TAG, "prerelease": True}
        with self.assertRaisesRegex(ValueError, "another build"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_a_stable_release_on_the_same_tag_is_a_channel_mismatch(self):
        self.api.release = {"id": 9, "draft": False, "body": MARKER, "tag_name": TAG, "prerelease": False}
        with self.assertRaisesRegex(ValueError, "test channel mismatch"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_blank_notes_are_rejected_before_any_request(self):
        for notes in ("", " \n\t"):
            with self.subTest(notes=notes), self.assertRaisesRegex(ValueError, "release notes"):
                self.publish(notes=notes)
        self.assertEqual(self.api.requests, [])

    def test_changed_artifact_is_rejected_before_any_request(self):
        next(self.root.glob("*.apk")).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.publish()
        self.assertEqual(self.api.requests, [])

    def test_published_release_missing_an_asset_is_never_repaired_silently(self):
        self.publish()
        self.api.assets.pop()
        self.api.writes.clear()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_channel_and_tag_must_agree(self):
        with self.assertRaisesRegex(ValueError, "does not belong to the stable channel"):
            self.publish(channel="stable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_bundle(root, "1.2.3", "v1.2.3", BUILD)
            with self.assertRaisesRegex(ValueError, "does not belong to the test channel"):
                release_bundle.publish(self.api, root, "v1.2.3", SHA, BUILD, "test", NOTES, BRANCH, RUN_URL)
        self.assertEqual(self.api.requests, [])

    def test_a_test_serial_must_equal_the_build_number(self):
        with self.assertRaisesRegex(ValueError, "serial"):
            release_bundle.prepare(self.root, TAG, SHA, BUILD + 1, BRANCH)
        with self.assertRaisesRegex(ValueError, "serial"):
            build_bundle(self.root, VERSION, TAG, BUILD + 1)

    def test_a_test_build_must_use_the_exact_package_names_the_app_accepts(self):
        for macos_name in ("macos.dmg", "macos-universal.dmg"):
            with self.subTest(macos_name=macos_name), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaisesRegex(ValueError, "Unexpected artifact name"):
                build_bundle(Path(directory), VERSION, TAG, BUILD, BRANCH, macos_name=macos_name)

    def test_the_branch_is_printable_ascii_text_of_bounded_length(self):
        cleaned = release_bundle.clean_branch("feat/中文-" + "x" * 200)
        self.assertLessEqual(len(cleaned), 100)
        self.assertTrue(cleaned.startswith("feat/??-"))
        self.assertTrue(all(" " <= char <= "~" for char in cleaned))
        for value in ("", "   ", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "source branch"):
                release_bundle.clean_branch(value)
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "source branch"):
            build_bundle(Path(directory), VERSION, TAG, BUILD, "")

    def test_annotation_message_lists_the_source_without_the_run_when_unknown(self):
        message = release_bundle.annotation_message(" notes \n", BRANCH, SHA, "42", "")
        self.assertEqual(message, "notes\n\nChannel: test\nBranch: %s\nCommit: %s\nBuild: 42\n" % (BRANCH, SHA))

    def test_publish_cli_rejects_local_and_self_hosted_runs_for_the_test_channel(self):
        for github_actions, runner in (("", ""), ("true", "self-hosted")):
            with self.subTest(runner=runner):
                env = dict(os.environ, GITHUB_ACTIONS=github_actions, RUNNER_ENVIRONMENT=runner,
                           GITHUB_TOKEN="unused-fixture-token", RELEASE_NOTES=NOTES, PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run([
                    sys.executable, str(HERE.parent / "release_bundle.py"), "publish", "--directory", str(self.root / "does-not-exist"),
                    "--tag", TAG, "--channel", "test", "--branch", BRANCH, "--source-sha", SHA, "--build-number", str(BUILD),
                ], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only supported in GitHub-hosted Actions", result.stderr)

    def test_prepare_cli_writes_the_test_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = {name: (self.root / name).read_bytes() for name in os.listdir(self.root)}
            for name, data in shared.items():
                (root / name).write_bytes(data)
            (root / "manifest.json").unlink()
            result = subprocess.run([
                sys.executable, str(HERE.parent / "release_bundle.py"), "prepare", "--directory", str(root), "--tag", TAG,
                "--channel", "test", "--branch", BRANCH, "--source-sha", SHA, "--build-number", str(BUILD),
            ], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "manifest.json").read_text()), self.manifest)
            self.assertEqual((root / "SHA256SUMS").read_text(), (self.root / "SHA256SUMS").read_text())
            self.assertTrue(re.fullmatch(r"(?:[0-9a-f]{64}  .+\n){3}", (root / "SHA256SUMS").read_text()))


if __name__ == "__main__":
    unittest.main()
