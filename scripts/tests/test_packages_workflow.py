"""Packages job graph contract: publish waits for every job it consumes. No network, no compiler."""
import re
import sys
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "core" / "_vendor"))
import yaml  # noqa: E402  vendored PyYAML

WORKFLOW = REPO / ".github" / "workflows" / "packages.yml"
WIRED_FIXTURES = (
    "run-fixtures", "run-engine-fixtures", "run-plan-answer-fixtures", "run-execution-answer-fixtures",
    "run-owner-fixtures", "run-mobile-fixtures", "run-mobile-real-core-fixtures", "run-retrospective-fixtures",
    "run-completion-display-fixtures", "run-build-ai-action-fixtures", "run-external-build-repair-fixtures",
    "run-ai-verification-recovery-fixtures", "run-planning-input-fixtures", "run-recovery-supervisor-fixtures",
    "run-review-resolution-fixtures", "run-scheduler-fixtures", "run-session-reader-fixtures",
    "run-session-search-input-fixtures", "run-verification-check-fixtures",
)
# Stale compile set or title text: kept out of the gate, measured by the manual probe only.
UNWIRED_FIXTURES = ("run-engine-fix-revert-fixtures", "run-session-fulltext-fixtures")
UPLOAD = "actions/upload-artifact"


def needs_of(job):
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def fixture_list(job, step_name):
    run = next(step["run"] for step in job["steps"] if step.get("name") == step_name)
    return re.search(r"for fixture in (.+?); do", run).group(1).split()


def shard_fixtures(job):
    return [name for entry in job["strategy"]["matrix"]["include"] for name in entry["fixtures"].split()]


class PackagesWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.jobs = document["jobs"]
        cls.dispatch_inputs = document[True]["workflow_dispatch"]["inputs"]  # YAML 1.1 reads `on:` as True

    def uploads(self, job):
        return [step["with"]["name"] for step in self.jobs[job].get("steps", []) if step.get("uses", "").startswith(UPLOAD)]

    def test_publish_waits_for_every_job_its_condition_names(self):
        publish = self.jobs["publish"]
        gates = set(re.findall(r"needs\.([\w-]+)\.result", publish["if"]))
        self.assertTrue(gates <= set(needs_of(publish)), gates)
        self.assertTrue(set(needs_of(publish)) <= set(self.jobs), needs_of(publish))

    def test_publish_waits_for_the_jobs_that_feed_release_artifacts(self):
        needs = needs_of(self.jobs["publish"])
        self.assertIn("android", needs)
        self.assertIn("macos-package", needs)
        self.assertIn("macos-universal-local-signed-${{ github.run_number }}", self.uploads("macos-package"))

    def test_fixtures_and_packaging_start_together_after_identity(self):
        for job in ("android", "macos-fixtures", "macos-package"):
            self.assertEqual(needs_of(self.jobs[job]), ["identity"], job)

    def test_wired_fixtures_run_once_each_across_two_shards(self):
        job = self.jobs["macos-fixtures"]
        self.assertEqual(len(job["strategy"]["matrix"]["include"]), 2)
        self.assertIs(job["strategy"]["fail-fast"], False)
        self.assertEqual(sorted(shard_fixtures(job)), sorted(WIRED_FIXTURES))

    def test_artifact_names_are_unique_within_a_run(self):
        names = [name for job in self.jobs for name in self.uploads(job)]
        self.assertEqual(len(names), len(set(names)), names)

    def test_unwired_probe_is_manual_opt_in_and_never_gates_publish(self):
        probe = self.jobs["macos-unwired-probe"]
        self.assertIn("workflow_dispatch", probe["if"])
        self.assertIn("probe_unwired", probe["if"])
        self.assertNotIn("macos-unwired-probe", needs_of(self.jobs["publish"]))
        self.assertIs(self.dispatch_inputs["probe_unwired"]["default"], False)
        probe_list = fixture_list(probe, "Measure unwired desktop fixtures")
        self.assertEqual(sorted(probe_list), sorted(UNWIRED_FIXTURES))
        self.assertFalse(set(probe_list) & set(WIRED_FIXTURES))


if __name__ == "__main__":
    unittest.main()
