"""Manual test-build identity against temporary Git fixtures and a fake release API. No network."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "release_bundle.py"
SPEC = importlib.util.spec_from_file_location("release_bundle", SCRIPT)
release_bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_bundle)
STABLE = {"tag_name": "v0.2.0", "draft": False, "prerelease": False}


def release(tag, draft=False, prerelease=None):
    return {"tag_name": tag, "draft": draft, "prerelease": "-test." in tag if prerelease is None else prerelease}


class FakeReleases:
    """Answers only the two read-only queries the identity step is allowed to make."""
    repository = "test/private"

    def __init__(self, latest=None, releases=()):
        self.latest, self.releases, self.requests = latest, list(releases), []

    def request(self, method, path, data=None, binary=False, upload=None):
        self.requests.append((method, path))
        if method == "GET" and path == "/repos/test/private/releases/latest":
            return self.latest
        if method == "GET" and path == "/repos/test/private/releases?per_page=100":
            return self.releases
        raise AssertionError((method, path))


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "github-output"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GIT_", "GITHUB_", "RELEASE_", "RUNNER_"))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Channel identity fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.git("config", "commit.gpgsign", "false")
        (self.root / "source.txt").write_text("initial source\n")
        self.git("add", "source.txt")
        self.git("commit", "-m", "fixture base")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, stderr=subprocess.PIPE, text=True)

    def environment(self, notes="Try the new update channel.", base="", run="70", **extra):
        values = dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF_TYPE="branch",
                      GITHUB_REF_NAME="feat/test-channel", GITHUB_OUTPUT=str(self.output),
                      GITHUB_RUN_NUMBER=run, RELEASE_CHANNEL="test", RELEASE_BASE_VERSION=base,
                      RELEASE_NOTES=notes)
        values.update(extra)
        return values

    def written(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines()) if self.output.exists() else {}


class ManualTestIdentityTests(RepoCase):
    def resolve(self, api, **options):
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch.dict(os.environ, self.environment(**options), clear=True):
                return release_bundle.identity(api)
        finally:
            os.chdir(previous)

    def assert_rejected(self, expected, api, **options):
        with self.assertRaisesRegex(ValueError, expected):
            self.resolve(api, **options)
        self.assertEqual(self.written(), {})

    def test_default_base_is_the_next_patch_above_the_latest_stable(self):
        api = FakeReleases(STABLE)
        result = self.resolve(api)
        expected = {"release": "true", "version": "0.2.1-test.70", "sha": self.sha,
                    "channel": "test", "tag": "v0.2.1-test.70"}
        self.assertEqual(result, expected)
        self.assertEqual(self.written(), expected)
        self.assertEqual({method for method, _ in api.requests}, {"GET"})
        self.assertEqual(len(api.requests), 2)

    def test_default_base_follows_the_highest_published_test_base(self):
        api = FakeReleases(STABLE, [release("v0.2.1-test.9"), release("v0.3.0-test.5"), release("v0.2.5-test.99")])
        self.assertEqual(self.resolve(api, run="71")["version"], "0.3.0-test.71")

    def test_an_obsolete_test_base_does_not_hold_the_default_back(self):
        api = FakeReleases(STABLE, [release("v0.1.5-test.3"), release("v0.2.0-test.8")])
        self.assertEqual(self.resolve(api)["version"], "0.2.1-test.70")

    def test_drafts_prereleases_and_foreign_tags_are_ignored(self):
        api = FakeReleases(STABLE, [
            release("v9.9.9-test.1", draft=True), release("v8.0.0-test.1", prerelease=False),
            release("v7.0.0-rc.1", prerelease=True), release("v6.0.0", prerelease=True),
            {"tag_name": None, "draft": False, "prerelease": True}, release("v0.1.0"), release("nightly", prerelease=True)])
        self.assertEqual(self.resolve(api)["version"], "0.2.1-test.70")

    def test_explicit_base_may_repeat_the_highest_published_test_base(self):
        api = FakeReleases(STABLE, [release("v0.3.0-test.5")])
        self.assertEqual(self.resolve(api, base="0.3.0")["version"], "0.3.0-test.70")
        self.assertEqual(self.resolve(FakeReleases(STABLE, [release("v0.3.0-test.5")]), base=" 0.4.0 ")["version"], "0.4.0-test.70")

    def test_base_must_be_above_the_latest_stable_release(self):
        for base in ("0.2.0", "0.1.9", "0.0.1"):
            with self.subTest(base=base):
                self.assert_rejected("above the latest stable", FakeReleases(STABLE), base=base)

    def test_base_must_not_be_below_the_highest_published_test_base(self):
        self.assert_rejected("below the highest", FakeReleases(STABLE, [release("v0.3.0-test.5")]), base="0.2.5")

    def test_malformed_base_is_rejected_before_any_api_call(self):
        for base in ("0.3", "0.3.0-test.1", "v0.3.0", "01.2.3", "0.3.0.1", "1.2.x", "1.2.3-rc.1", "1000000000.0.0"):
            with self.subTest(base=base):
                api = FakeReleases(STABLE)
                self.assert_rejected("stable X.Y.Z", api, base=base)
                self.assertEqual(api.requests, [])

    def test_without_any_published_release_the_base_must_be_explicit_and_above_zero(self):
        self.assert_rejected("Cannot determine", FakeReleases())
        self.assert_rejected("above 0.0.0", FakeReleases(), base="0.0.0")
        self.assertEqual(self.resolve(FakeReleases(), base="0.1.0")["version"], "0.1.0-test.70")

    def test_notes_are_required_and_bounded_before_any_api_call(self):
        for notes, expected in (("", "release notes"), (" \n\t ", "release notes"), ("x" * 4001, "at most 4000")):
            with self.subTest(length=len(notes)):
                api = FakeReleases(STABLE)
                self.assert_rejected(expected, api, notes=notes)
                self.assertEqual(api.requests, [])
        self.assertEqual(self.resolve(FakeReleases(STABLE), notes="x" * 4000)["channel"], "test")

    def test_run_number_must_be_a_build_number(self):
        for run in ("", "0", "01", "-1", "1000000000", "7a"):
            with self.subTest(run=run):
                api = FakeReleases(STABLE)
                self.assert_rejected("build number", api, run=run)
                self.assertEqual(api.requests, [])

    def test_an_unrecognizable_latest_release_aborts(self):
        for tag in ("nightly", "v0.3.0-rc.1", "v01.3.0", "v0.3.0-test.4"):
            with self.subTest(tag=tag):
                self.assert_rejected("recognizable stable", FakeReleases({"tag_name": tag, "draft": False, "prerelease": False}))
        self.assert_rejected("recognizable stable", FakeReleases({"tag_name": "v0.2.0", "draft": False, "prerelease": True}))

    def test_the_commit_does_not_need_to_be_in_main(self):
        (self.root / "source.txt").write_text("task branch source\n")
        self.git("commit", "-am", "task branch commit")
        contained = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=self.root, env=self.env)
        self.assertNotEqual(contained.returncode, 0)
        result = self.resolve(FakeReleases(STABLE))
        self.assertEqual(result["sha"], self.git("rev-parse", "HEAD").strip())

    def test_a_manual_test_build_cannot_name_a_tag(self):
        self.assert_rejected("Manual builds require a branch", FakeReleases(STABLE), GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="v1.2.3")

    def test_missing_token_is_reported_before_any_request(self):
        self.assert_rejected("GITHUB_TOKEN is required", None)


class IdentityRejectionTests(RepoCase):
    def run_identity(self, **options):
        env = self.environment(**options)
        result = subprocess.run([sys.executable, str(SCRIPT), "identity"], cwd=self.root, env=env, capture_output=True, text=True)
        return result

    def assert_rejected(self, expected, **options):
        result = self.run_identity(**options)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        self.assertEqual(self.written(), {})

    def test_a_pushed_test_tag_is_never_a_release(self):
        self.git("tag", "-a", "v1.2.3-test.5", "-m", "Pushed by hand")
        self.assert_rejected("manual channel=test", GITHUB_EVENT_NAME="push", GITHUB_REF_TYPE="tag",
                             GITHUB_REF_NAME="v1.2.3-test.5", RELEASE_CHANNEL="", RELEASE_NOTES="")

    def test_a_channel_can_only_be_selected_for_a_manual_build(self):
        for event in ("push", "pull_request"):
            with self.subTest(event=event):
                self.assert_rejected("manual build", GITHUB_EVENT_NAME=event, GITHUB_REF_NAME="main")

    def test_unknown_channels_are_rejected(self):
        for channel in ("nightly", "TEST", "stable", " test"):
            with self.subTest(channel=channel):
                self.assert_rejected("Unknown release channel", RELEASE_CHANNEL=channel)

    def test_test_inputs_on_a_preview_build_are_rejected(self):
        for options in ({"base": "0.3.0", "notes": ""}, {"base": "", "notes": "Looks like a test."}):
            with self.subTest(options=options):
                self.assert_rejected("only used by channel=test", RELEASE_CHANNEL="preview",
                                     RELEASE_BASE_VERSION=options["base"], RELEASE_NOTES=options["notes"])

    def test_explicit_preview_is_still_the_zero_version_without_a_release(self):
        for channel in ("", "preview"):
            with self.subTest(channel=channel):
                self.output.unlink(missing_ok=True)
                result = self.run_identity(RELEASE_CHANNEL=channel, RELEASE_NOTES="", RELEASE_BASE_VERSION="")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.written(), {"release": "false", "version": "0.0.0", "sha": self.sha})

    def test_a_test_build_without_a_token_fails_before_any_network_use(self):
        self.assert_rejected("GITHUB_TOKEN is required")

    def test_a_test_build_without_notes_fails_before_any_network_use(self):
        self.assert_rejected("release notes", RELEASE_NOTES="", GITHUB_TOKEN="unused-fixture-token")


if __name__ == "__main__":
    unittest.main()
