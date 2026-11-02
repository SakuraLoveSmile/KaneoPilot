"""Static contract checks for the Packages workflow: release channels and the job graph. No network, no compiler."""
import json
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "_vendor"))
try:
    import yaml  # vendored PyYAML
finally:
    sys.path.remove(str(ROOT / "core" / "_vendor"))

WORKFLOW_PATH = ROOT / ".github/workflows/packages.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
TRIGGERS = WORKFLOW["on"] if "on" in WORKFLOW else WORKFLOW[True]  # YAML 1.1 reads `on:` as True
JOBS = WORKFLOW["jobs"]

UPDATE_FIXTURES = (
    "run-software-update-fixtures", "run-software-update-termination-fixtures", "run-software-update-ui-fixtures",
)
WIRED_FIXTURES = (
    "run-fixtures", "run-engine-fixtures", "run-plan-answer-fixtures", "run-execution-answer-fixtures",
    "run-owner-fixtures", "run-mobile-fixtures", "run-mobile-real-core-fixtures", "run-retrospective-fixtures",
    "run-completion-display-fixtures", "run-build-ai-action-fixtures", "run-external-build-repair-fixtures",
    "run-ai-verification-recovery-fixtures", "run-planning-input-fixtures", "run-recovery-supervisor-fixtures",
    "run-review-resolution-fixtures", "run-scheduler-fixtures", "run-session-reader-fixtures",
    "run-session-search-input-fixtures", "run-verification-check-fixtures", "run-engine-fix-revert-fixtures",
    "run-python-runtime-fixtures",
) + UPDATE_FIXTURES
# Manual probe only (workflow_dispatch input): its main-thread latency assertion fails on hosted runners.
UNGATED_FIXTURES = ("run-session-fulltext-fixtures",)
UPLOAD = "actions/upload-artifact"


def steps_by_name(job):
    return {step["name"]: step for step in JOBS[job]["steps"] if "name" in step}


def needs_of(job):
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def shard_fixtures(job):
    return [name for entry in job["strategy"]["matrix"]["include"] for name in entry["fixtures"].split()]


def uploads(job):
    return [step["with"]["name"] for step in JOBS[job].get("steps", []) if step.get("uses", "").startswith(UPLOAD)]


class PackagesWorkflowTests(unittest.TestCase):
    def test_pushed_test_tags_never_start_a_run(self):
        self.assertEqual(TRIGGERS["push"]["tags"], ["v*", "!v*-test.*"])

    def test_only_branch_pushes_skip_plan_only_changes(self):
        # GitHub does not evaluate path filters for tag pushes; a manual run must never be skipped by one.
        self.assertEqual(TRIGGERS["push"]["paths-ignore"], ["docs/plans/**"])
        self.assertNotIn("paths-ignore", TRIGGERS["workflow_dispatch"])
        self.assertNotIn("paths-ignore", TRIGGERS["pull_request"] or {})

    def test_manual_inputs_select_a_preview_or_a_test_release(self):
        inputs = TRIGGERS["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"channel", "base_version", "notes", "probe_session_fulltext"})
        self.assertEqual((inputs["channel"]["type"], inputs["channel"]["options"], inputs["channel"]["default"]),
                         ("choice", ["preview", "test"], "preview"))
        for name in ("base_version", "notes"):
            self.assertEqual((inputs[name]["type"], inputs[name]["default"], inputs[name]["required"]), ("string", "", False))

    def test_concurrency_separates_event_kinds_and_never_cancels_manual_or_tag_runs(self):
        concurrency = WORKFLOW["concurrency"]
        self.assertIn("github.event_name", concurrency["group"])
        self.assertIn("github.ref", concurrency["group"])
        # A superseded branch push or pull request is cancelled; a manual run may be publishing and a tag run is a release.
        self.assertEqual(concurrency["cancel-in-progress"],
                         "${{ github.event_name != 'workflow_dispatch' && !startsWith(github.ref, 'refs/tags/') }}")

    def test_identity_exposes_the_channel_and_tag_and_reads_inputs_from_the_environment(self):
        self.assertEqual(set(JOBS["identity"]["outputs"]), {"release", "version", "sha", "channel", "tag"})
        env = steps_by_name("identity")["Resolve immutable build identity"]["env"]
        self.assertEqual(env, {"GITHUB_TOKEN": "${{ github.token }}", "RELEASE_CHANNEL": "${{ inputs.channel }}",
                               "RELEASE_BASE_VERSION": "${{ inputs.base_version }}", "RELEASE_NOTES": "${{ inputs.notes }}"})

    def test_free_text_inputs_never_reach_a_shell_script_or_a_condition(self):
        def scrub(text):
            return text.replace("inputs.probe_session_fulltext", "")  # a boolean opt-in, not free text

        for name, job in JOBS.items():
            self.assertNotIn("inputs.", scrub(json.dumps({key: value for key, value in job.items() if key != "steps"})), name)
            for step in job.get("steps", []):
                outside_env = scrub(json.dumps({key: value for key, value in step.items() if key != "env"}))
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
        # The only ref check allowed is the run-cancelling rule; release decisions come from the identity job.
        text = WORKFLOW_PATH.read_text(encoding="utf-8").replace(WORKFLOW["concurrency"]["cancel-in-progress"], "")
        self.assertNotIn("refs/tags", text)

    def test_release_artifacts_are_named_from_the_identity_version(self):
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('name="KaneoPilot-$VERSION_NAME-android.apk"', text)
        self.assertIn('export DMG_NAME="KaneoPilot-$VERSION_NAME-macos-arm64.dmg"', text)
        self.assertIn("VERSION_NAME: ${{ needs.identity.outputs.version }}", text)

    def test_the_mac_package_is_apple_silicon_only(self):
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("universal", text.lower())
        self.assertNotIn("x86_64", text)
        step = steps_by_name("macos-package")["Build and verify Apple Silicon DMG"]
        self.assertEqual(step["env"]["ARCHS"], "arm64")
        self.assertIn('[ "$(uname -m)" = arm64 ]', step["run"])
        self.assertIn("--architectures arm64 --output", step["run"])
        self.assertIn('KaneoPilot-ci-$BUILD_NUMBER-macos-arm64-local-signed.dmg', step["run"])

    def test_the_mac_package_fetches_the_pinned_runtime_first_and_smokes_the_dmg_it_built(self):
        steps = steps_by_name("macos-package")
        order = [step.get("name") for step in JOBS["macos-package"]["steps"]]
        fetch, build, smoke = ("Fetch the pinned Python runtime", "Build and verify Apple Silicon DMG",
                               "Smoke the bundled Python in the built DMG")
        self.assertLess(order.index(fetch), order.index(build))
        self.assertLess(order.index(build), order.index(smoke))
        self.assertIn('scripts/fetch_python_runtime.py --output "$RUNNER_TEMP/python-runtime"', steps[fetch]["run"])
        # The fetch step exports PYTHON_RUNTIME_DIR; the build step must not point it anywhere else.
        self.assertNotIn("PYTHON_RUNTIME_DIR", json.dumps(steps[build]))
        # The smoke runs after an earlier failure too (one run, all signals), but only on a package that was built.
        self.assertEqual(steps[smoke]["if"], "${{ !cancelled() && steps.macos-package.outcome == 'success' }}")
        self.assertIn('bash scripts/smoke_bundled_python.sh "${dmgs[0]}"', steps[smoke]["run"])
        # Its logs travel with the packaging reports.
        self.assertIn("macos-reports/bundled-python-smoke.log", steps[smoke]["run"])
        self.assertIn("macos-reports/python-runtime-fetch.log", steps[fetch]["run"])

    def test_the_syntax_check_reads_each_shell_script_of_the_mac_package_on_its_own(self):
        run = steps_by_name("checks")["Release recovery and packaging checks"]["run"]
        loop = re.search(r"for script in ([^;]+); do\n\s+bash -n \"\$script\"", run)
        self.assertIsNotNone(loop, "bash -n takes one script; a list after it is only arguments")
        checked = set(loop.group(1).split())
        self.assertEqual(checked, {"scripts/require_actions.sh", "scripts/build_app.sh", "scripts/build_dmg.sh",
                                   "scripts/smoke_bundled_python.sh"})
        for script in checked:
            self.assertTrue((ROOT / script).is_file(), script)

    def test_core_tests_run_on_the_bundled_python_and_on_the_oldest_supported_one(self):
        job = JOBS["checks"]
        matrix = job["strategy"]["matrix"]
        self.assertEqual(matrix["python"], ["3.11", "3.13"])
        setups = [step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python")]
        self.assertEqual([step["with"]["python-version"] for step in setups], ["${{ matrix.python }}"])
        # Shards are split by the shard count; the job count also multiplies by the Python versions.
        self.assertNotIn("strategy.job-total", WORKFLOW_PATH.read_text(encoding="utf-8"))
        for name in ("Core isolated tests", "Integration fixture tests"):
            self.assertEqual(int(steps_by_name("checks")[name]["env"]["SHARDS"]), len(matrix["shard"]), name)
        # Packaging checks run per Python (they cover the tools on both); every cell keeps its own report.
        self.assertEqual(steps_by_name("checks")["Release recovery and packaging checks"]["if"],
                         "${{ !cancelled() && matrix.shard == 1 }}")
        self.assertIn("-py${{ matrix.python }}", uploads("checks")[0])

    def test_desktop_fixtures_name_the_interpreter_they_run_the_core_with(self):
        for name in ("macos-fixtures", "macos-session-fulltext-probe"):
            steps = JOBS[name]["steps"]
            names = [step.get("name") for step in steps]
            setup = next(index for index, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python"))
            export = names.index("Name the interpreter the fixtures run the core with")
            running = next(index for index, step in enumerate(steps)
                           if "matrix.fixtures" in step.get("run", "") or "macos/tests/run-" in step.get("run", ""))
            self.assertEqual(steps[setup]["with"]["python-version"], "3.13", name)
            self.assertLess(setup, export, name)
            self.assertLess(export, running, name)
            self.assertIn('echo "KANPILOT_PYTHON=$python" >> "$GITHUB_ENV"', steps[export]["run"], name)

    def test_publish_waits_for_every_job_its_condition_names(self):
        publish = JOBS["publish"]
        gates = set(re.findall(r"needs\.([\w-]+)\.result", publish["if"]))
        self.assertTrue(gates <= set(needs_of(publish)), gates)
        self.assertTrue(set(needs_of(publish)) <= set(JOBS), needs_of(publish))
        # Every shard of a matrix job reports through that job's single aggregate result.
        self.assertEqual(gates, {"checks", "android", "macos-fixtures", "macos-package"})

    def test_publish_waits_for_the_jobs_that_feed_release_artifacts(self):
        needs = needs_of(JOBS["publish"])
        self.assertIn("android", needs)
        self.assertIn("macos-package", needs)
        self.assertIn("macos-arm64-local-signed-${{ github.run_number }}", uploads("macos-package"))
        downloads = [step["with"]["name"] for step in JOBS["publish"]["steps"] if step.get("uses", "").startswith("actions/download-artifact")]
        self.assertIn("macos-arm64-local-signed-${{ github.run_number }}", downloads)

    def test_fixtures_and_packaging_start_together_after_identity(self):
        for job in ("android", "macos-fixtures", "macos-package"):
            self.assertEqual(needs_of(JOBS[job]), ["identity"], job)

    def test_wired_fixtures_run_once_each_across_two_shards(self):
        job = JOBS["macos-fixtures"]
        self.assertEqual(len(job["strategy"]["matrix"]["include"]), 2)
        self.assertIs(job["strategy"]["fail-fast"], False)
        self.assertEqual(sorted(shard_fixtures(job)), sorted(WIRED_FIXTURES))

    def test_every_desktop_fixture_script_is_wired_or_probed(self):
        scripts = sorted(path.stem for path in (ROOT / "macos" / "tests").glob("run-*fixtures.sh"))
        self.assertEqual(scripts, sorted(WIRED_FIXTURES + UNGATED_FIXTURES))

    def test_session_fulltext_probe_is_manual_opt_in_and_never_gates_publish(self):
        probe = JOBS["macos-session-fulltext-probe"]
        self.assertIn("workflow_dispatch", probe["if"])
        self.assertIn("probe_session_fulltext", probe["if"])
        self.assertIs(TRIGGERS["workflow_dispatch"]["inputs"]["probe_session_fulltext"]["default"], False)
        self.assertIs(probe["continue-on-error"], True)
        self.assertNotIn("macos-session-fulltext-probe", needs_of(JOBS["publish"]))
        runs = "\n".join(step.get("run", "") for step in probe["steps"])
        for name in UNGATED_FIXTURES:
            self.assertIn(f"macos/tests/{name}.sh", runs)
        self.assertFalse(set(UNGATED_FIXTURES) & set(shard_fixtures(JOBS["macos-fixtures"])))

    def test_artifact_names_are_unique_within_a_run(self):
        names = [name for job in JOBS for name in uploads(job)]
        self.assertEqual(len(names), len(set(names)), names)

    def test_step_references_stay_inside_their_job(self):
        # A step reading another job's step output evaluates to nothing and silently never runs.
        for name, job in JOBS.items():
            ids = {step["id"] for step in job.get("steps", []) if "id" in step}
            for reference in set(re.findall(r"\bsteps\.([\w-]+)\.", json.dumps(job.get("steps", [])))):
                self.assertIn(reference, ids, f"{name} reads steps.{reference} from another job")

    def test_update_fixture_evidence_is_uploaded_by_the_shard_that_runs_the_update_fixtures(self):
        job = JOBS["macos-fixtures"]
        shards = [entry for entry in job["strategy"]["matrix"]["include"] if set(UPDATE_FIXTURES) <= set(entry["fixtures"].split())]
        self.assertEqual(len(shards), 1, "the three update fixtures share one shard")
        evidence = [step for step in job["steps"] if step.get("with", {}).get("name") == "macos-update-fixture-${{ github.run_number }}"]
        self.assertEqual(len(evidence), 1)
        for condition in (f"matrix.group == '{shards[0]['group']}'", "needs.identity.outputs.release == 'false'",
                          "steps.desktop-fixtures.outcome == 'success'"):
            self.assertIn(condition, evidence[0]["if"])
        self.assertNotIn("macos-update-fixture-${{ github.run_number }}", uploads("macos-package"))


if __name__ == "__main__":
    unittest.main()
