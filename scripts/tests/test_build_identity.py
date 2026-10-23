"""Resolve build identity against temporary Git fixtures; no network or compilation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "release_bundle.py"
TAG = "v1.2.3"


class BuildIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root / "github-output"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Build identity fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        (self.root / "source.txt").write_text("initial source\n")
        self.git("add", "source.txt")
        self.git("commit", "-m", "fixture base")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, stderr=subprocess.PIPE, text=True)

    def annotated_tag(self, name=TAG, message="Release notes\n\nRepair the workflow."):
        self.git("tag", "-a", name, "-m", message)

    def resolve(self, event="push", ref_type="tag", ref_name=TAG):
        env = dict(self.env, GITHUB_EVENT_NAME=event, GITHUB_REF_TYPE=ref_type,
                   GITHUB_REF_NAME=ref_name, GITHUB_OUTPUT=str(self.output))
        result = subprocess.run([sys.executable, str(SCRIPT), "identity"], cwd=self.root,
                                env=env, capture_output=True, text=True)
        values = dict(line.split("=", 1) for line in self.output.read_text().splitlines()) if self.output.exists() else {}
        return result, values

    def assert_rejected(self, expected, **event):
        result, values = self.resolve(**event)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, result.stderr)
        self.assertEqual(values, {})

    def test_valid_annotated_tag_outputs_immutable_release_identity(self):
        self.annotated_tag()
        result, values = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"release": "true", "version": "1.2.3", "sha": self.sha})

    def test_release_commit_may_be_an_ancestor_of_current_main(self):
        self.annotated_tag()
        (self.root / "source.txt").write_text("newer main source\n")
        self.git("commit", "-am", "newer main")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "--detach", self.sha)
        result, values = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["sha"], self.sha)

    def test_lightweight_tag_rejected(self):
        self.git("tag", TAG)
        self.assert_rejected("annotated tag")

    def test_nested_annotated_tag_rejected(self):
        self.annotated_tag("inner")
        self.git("-c", "advice.nestedTag=false", "tag", "-a", TAG, "inner", "-m", "Release notes")
        self.assert_rejected("directly name the build commit")

    def test_empty_annotation_rejected(self):
        self.annotated_tag(message="")
        self.assert_rejected("release notes")

    def test_tag_outside_main_rejected(self):
        (self.root / "source.txt").write_text("unmerged source\n")
        self.git("commit", "-am", "unmerged change")
        self.annotated_tag()
        self.assert_rejected("contained in origin/main")

    def test_tag_pointing_to_another_commit_rejected(self):
        self.annotated_tag()
        (self.root / "source.txt").write_text("different checked-out source\n")
        self.git("commit", "-am", "different checkout")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assert_rejected("directly name the build commit")

    def test_annotation_with_another_tag_name_rejected(self):
        self.annotated_tag()
        self.git("update-ref", "refs/tags/v1.2.4", "refs/tags/" + TAG)
        self.assert_rejected("directly name the build commit", ref_name="v1.2.4")

    def test_noncanonical_release_tag_rejected(self):
        for tag in ("v1.2.3-rc.1", "v01.2.3", "v1.2", "1.2.3"):
            with self.subTest(tag=tag):
                self.assert_rejected("Only stable", ref_name=tag)

    def test_manual_tag_selection_rejected(self):
        self.annotated_tag()
        self.assert_rejected("Manual builds require a branch", event="workflow_dispatch")

    def test_branches_pull_requests_and_manual_branches_are_preview_builds(self):
        events = (("push", "branch", "main"), ("pull_request", "branch", "123/merge"),
                  ("workflow_dispatch", "branch", "feature/preview"))
        for event, ref_type, ref_name in events:
            with self.subTest(event=event):
                self.output.unlink(missing_ok=True)
                result, values = self.resolve(event, ref_type, ref_name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(values, {"release": "false", "version": "0.0.0", "sha": self.sha})


if __name__ == "__main__":
    unittest.main()
