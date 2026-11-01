"""Static contract checks for the Packages workflow's release channels. No network, no compiler."""
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "_vendor"))
try:
    import yaml
finally:
    sys.path.remove(str(ROOT / "core" / "_vendor"))

WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/packages.yml").read_text(encoding="utf-8"))
TRIGGERS = WORKFLOW["on"] if "on" in WORKFLOW else WORKFLOW[True]
JOBS = WORKFLOW["jobs"]


def steps_by_name(job):
    return {step["name"]: step for step in JOBS[job]["steps"] if "name" in step}


class PackagesWorkflowTests(unittest.TestCase):
    def test_pushed_test_tags_never_start_a_run(self):
        self.assertEqual(TRIGGERS["push"]["tags"], ["v*", "!v*-test.*"])

    def test_manual_inputs_select_a_preview_or_a_test_release(self):
        inputs = TRIGGERS["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"channel", "base_version", "notes"})
        self.assertEqual((inputs["channel"]["type"], inputs["channel"]["options"], inputs["channel"]["default"]),
                         ("choice", ["preview", "test"], "preview"))
        for name in ("base_version", "notes"):
            self.assertEqual((inputs[name]["type"], inputs[name]["default"], inputs[name]["required"]), ("string", "", False))

    def test_concurrency_separates_event_kinds_and_never_cancels(self):
        group = WORKFLOW["concurrency"]["group"]
        self.assertIn("github.event_name", group)
        self.assertIn("github.ref", group)
        self.assertIs(WORKFLOW["concurrency"]["cancel-in-progress"], False)

    def test_identity_exposes_the_channel_and_tag_and_reads_inputs_from_the_environment(self):
        self.assertEqual(set(JOBS["identity"]["outputs"]), {"release", "version", "sha", "channel", "tag"})
        env = steps_by_name("identity")["Resolve immutable build identity"]["env"]
        self.assertEqual(env, {"GITHUB_TOKEN": "${{ github.token }}", "RELEASE_CHANNEL": "${{ inputs.channel }}",
                               "RELEASE_BASE_VERSION": "${{ inputs.base_version }}", "RELEASE_NOTES": "${{ inputs.notes }}"})

    def test_free_text_inputs_never_reach_a_shell_script_or_a_condition(self):
        for name, job in JOBS.items():
            self.assertNotIn("inputs.", json.dumps({key: value for key, value in job.items() if key != "steps"}), name)
            for step in job.get("steps", []):
                outside_env = json.dumps({key: value for key, value in step.items() if key != "env"})
                self.assertNotIn("inputs.", outside_env, name)
                self.assertNotIn("github.event.inputs", outside_env, name)

    def test_publish_uses_the_identity_tag_and_channel(self):
        steps = steps_by_name("publish")
        for name in ("Assemble checksums and immutable manifest", "Publish or verify this build's release"):
            with self.subTest(step=name):
                step = steps[name]
                self.assertEqual(step["env"]["RELEASE_TAG"], "${{ needs.identity.outputs.tag || github.ref_name }}")
                self.assertEqual(step["env"]["RELEASE_CHANNEL"], "${{ needs.identity.outputs.channel || 'stable' }}")
                self.assertEqual(step["env"]["RELEASE_BRANCH"], "${{ github.ref_name }}")
                for argument in ('--tag "$RELEASE_TAG"', '--channel "$RELEASE_CHANNEL"', '--branch "$RELEASE_BRANCH"'):
                    self.assertIn(argument, step["run"])
                self.assertNotIn("GITHUB_REF_NAME", step["run"])
        self.assertEqual(steps["Publish or verify this build's release"]["env"]["RELEASE_NOTES"], "${{ inputs.notes }}")

    def test_publish_keeps_the_shared_lock_and_is_the_only_job_that_can_write(self):
        publish = JOBS["publish"]
        self.assertEqual(publish["concurrency"], {"group": "kaneopilot-stable-release", "cancel-in-progress": False})
        self.assertEqual(publish["permissions"], {"contents": "write"})
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        for name, job in JOBS.items():
            if name != "publish":
                self.assertNotIn("permissions", job, name)

    def test_signing_and_publishing_follow_the_release_flag_not_the_ref(self):
        android = steps_by_name("android")
        for name in ("Restore release signing key", "Signed release APK"):
            self.assertIn("needs.identity.outputs.release == 'true'", android[name]["if"], name)
        self.assertIn("needs.identity.outputs.release == 'true'", JOBS["publish"]["if"])
        self.assertIn("needs.identity.outputs.release == 'false'", android["Build debug APK and isolated instrumentation APK"]["if"])
        self.assertNotIn("refs/tags", (ROOT / ".github/workflows/packages.yml").read_text(encoding="utf-8"))

    def test_release_artifacts_are_named_from_the_identity_version(self):
        text = (ROOT / ".github/workflows/packages.yml").read_text(encoding="utf-8")
        self.assertIn('name="KaneoPilot-$VERSION_NAME-android.apk"', text)
        self.assertIn('export DMG_NAME="KaneoPilot-$VERSION_NAME-macos-universal.dmg"', text)
        self.assertIn("VERSION_NAME: ${{ needs.identity.outputs.version }}", text)


if __name__ == "__main__":
    unittest.main()
