"""Isolated execution admission and core verdict contracts; no model or verify."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_engine_verdict as verdict_fixtures

from kanpilot import (db, execution_contract, harnesses, pi_desktop_worker,
                      stage_contract, stage_evidence, util, verification_recovery)
from kanpilot.engine import verdict


class ExecutionStageContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="kp-execution-contract-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-b", "main"), ("config", "user.name", "test"),
                     ("config", "user.email", "test@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        (self.repo / "source.py").write_text("value = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "fixture"],
                       check=True, capture_output=True)
        plan = self.root / "plan.md"
        plan.write_text("# Plan\n```verify\npytest -q\n```\n")
        self.spec = {
            "v": 1, "task_id": "task", "request_id": "dispatch-request", "run_id": "run",
            "backend": "pi_desktop", "cwd": str(self.repo), "repository": str(self.repo),
            "runs_dir": str(self.root / "runs"), "plan_path": str(plan),
            "plan_sha256": util.sha256_file(plan), "completion_policy": "command_verification",
            "required_verification_commands": ["pytest -q"],
            "config": {"connection_file": str(self.root / "fake-plus.json"),
                       "permission_mode": "auto", "model_id": "fixture"},
        }
        self.contract = execution_contract.freeze_spec(self.spec, requirements_sha256="a" * 64)

    def job(self):
        job = pi_desktop_worker._base_job(self.spec)
        job.update(session_id="session", turn_id="turn", send_state="sent",
                   verification_source_sha256=self.contract["inputs"]["initial_source_sha256"])
        return job

    def report(self, command="pytest -q", code=0, result="passed"):
        return {"report": {"checks": [{"id": "check-1", "command": command,
                                        "exitCode": code, "result": result}]}}

    def coverage(self, report):
        rows = harnesses.get_runtime_adapter("pi_desktop").verification_checks(
            report, source_sha256=self.job()["verification_source_sha256"])
        return stage_evidence.match_checks(self.contract["verification_commands"], rows,
            expected_source_sha256=self.job()["verification_source_sha256"])

    def test_new_job_persists_exact_frozen_identity(self):
        job = self.job()
        self.assertEqual(job["stage_contract"], self.contract)
        self.assertEqual(job["request_id"], self.spec["request_id"])
        self.assertEqual(execution_contract.validate_spec(self.spec, job=job), self.contract)

    def test_legacy_absence_is_distinct_from_missing_required_contract(self):
        legacy = {key: value for key, value in self.spec.items()
                  if key not in ("stage_contract", "stage_contract_required")}
        self.assertIsNone(execution_contract.validate_spec(legacy))
        legacy["stage_contract_required"] = True
        with self.assertRaises(util.KanpilotError):
            execution_contract.validate_spec(legacy)
        with self.assertRaises(util.KanpilotError):
            execution_contract.validate_spec(self.spec, job={})

    def test_config_plan_and_command_drift_cannot_start_native_session(self):
        for key, changed in (("config", {"permission_mode": "ask"}),
                             ("plan_sha256", "b" * 64),
                             ("instruction", "Use a different task scope"),
                             ("required_verification_commands", ["echo fabricated"])):
            spec = copy.deepcopy(self.spec)
            spec[key] = changed
            with self.subTest(key=key), patch.object(pi_desktop_worker.runtime_client, "RuntimeClient") as native:
                with self.assertRaises(util.KanpilotError):
                    pi_desktop_worker._run_real(spec, False, SimpleNamespace(check=lambda: None))
                native.assert_not_called()

    def test_prompt_identity_binds_the_body_and_repair_lineage(self):
        with patch("kanpilot.repair_prompt.execution_body", return_value="frozen repair body"):
            repair = copy.deepcopy(self.spec)
            repair["instruction"] = "Repair only the frozen review"
            repair["repair_prompt"] = {"identity_sha256": "e" * 64}
            contract = execution_contract.freeze_spec(repair, requirements_sha256="a" * 64)
            self.assertEqual(contract["inputs"]["prompt_sha256"], util.sha256_text(
                repair["instruction"] + "\n\nfrozen repair body"))
            changed = copy.deepcopy(repair)
            changed["repair_prompt"]["identity_sha256"] = "f" * 64
            with self.assertRaises(util.KanpilotError):
                execution_contract.validate_spec(changed)
        with patch("kanpilot.repair_prompt.execution_body", return_value="changed repair body"):
            with self.assertRaises(util.KanpilotError):
                execution_contract.validate_spec(repair)

    def test_source_drift_before_start_is_rejected(self):
        (self.repo / "source.py").write_text("value = 2\n")
        with self.assertRaises(util.KanpilotError) as caught:
            execution_contract.validate_spec(self.spec, starting=True)
        self.assertIn("源码已变化", caught.exception.message)

    def test_resume_rejects_old_or_missing_contract_without_overwriting_job(self):
        directory = self.root / "runs"
        directory.mkdir()
        path = directory / "job.json"
        job = self.job()
        job["stage_contract"]["run_id"] = "old-run"
        raw = json.dumps(job)
        path.write_text(raw)
        recovered, error = pi_desktop_worker._resume_job(path, self.spec)
        self.assertIsNone(recovered)
        self.assertIn("阶段契约", error)
        self.assertEqual(path.read_text(), raw)

    def test_adapter_keeps_model_claim_provenance_and_exact_commands(self):
        coverage = self.coverage(self.report())
        self.assertTrue(coverage["ok"])
        self.assertEqual(coverage["rows"][0]["provenance"], "model_declared")
        for text in ("PATH=/fake pytest -q", "pytest  -q", "pytest -q（通过）"):
            with self.subTest(command=text):
                self.assertFalse(self.coverage(self.report(text))["ok"])

    def test_duplicate_and_contradictory_reports_remain_unconfirmed(self):
        report = self.report()
        report["report"]["checks"] *= 2
        coverage = self.coverage(report)
        self.assertEqual(coverage["rows"][0]["unconfirmed_reason"], "duplicate_records")
        self.assertEqual(coverage["failed"], [])
        coverage = self.coverage(self.report(code=7, result="passed"))
        self.assertEqual(coverage["rows"][0]["unconfirmed_reason"], "contradictory_result")
        self.assertEqual(coverage["failed"], [])
        self.assertTrue(self.coverage(self.report(code=7, result="failed"))["failed"])

    def evaluate(self, report):
        job = self.job()
        source = job["verification_source_sha256"]
        report = dict(report, turn_id="turn", report_id="report", report_sha256="c" * 64)
        frozen = {"task_id": "task", "execution_run_id": "run", "source_sha256": source,
                  "plan_sha256": self.spec["plan_sha256"], "worktree_path": str(self.repo)}
        evidence = {"job": job, "commands": ["pytest -q"], "coverage": self.coverage(report),
                    "report": report, "hashes": {"job.json": "d" * 64}}
        task = {"task_id": "task", "run_id": "run"}
        with patch.object(verdict, "binding", return_value=frozen), \
             patch.object(verdict, "effective_changes", return_value=["source.py"]), \
             patch.object(verdict, "_evidence_unchanged", return_value=True), \
             patch.object(verdict.db, "require_task", return_value=task), \
             patch.object(verification_recovery, "read_goal_evidence", return_value=evidence), \
             patch.object(verdict, "run_verify_commands", side_effect=AssertionError("Pilot must not verify")):
            result = verdict.evaluate_goal_task(None, task, isolated=True)
        return result

    def test_core_packages_only_verified_success_with_bound_source(self):
        result = self.evaluate(self.report())
        self.assertTrue(result["verification_ok"])
        envelope = stage_contract.validate_result(result["stage_result"], self.contract)
        self.assertEqual(envelope["outcome"], "succeeded")
        self.assertEqual(envelope["payload"]["source_sha256"], result["binding"]["source_sha256"])
        self.assertEqual(envelope["payload"]["checks"][0]["provenance"], "model_declared")

    def test_core_distinguishes_missing_evidence_from_real_failure(self):
        unknown = self.evaluate(self.report(command="echo fabricated"))
        failed = self.evaluate(self.report(code=7, result="failed"))
        self.assertEqual(unknown["kind"], "unconfirmed")
        self.assertEqual(unknown["stage_result"]["outcome"], "blocked")
        self.assertEqual(failed["kind"], "verify_failed")
        self.assertEqual(failed["stage_result"]["outcome"], "failed")


class ExecutionStageCommandIntegration(unittest.TestCase):
    def test_cli_verdict_binds_real_request_and_frozen_prompt_without_running_verify(self):
        # Compose the existing fixture without inheriting/recollecting its suite.
        case = verdict_fixtures.TestEngineVerdictCommand()
        case.setUp()
        self.addCleanup(case.doCleanups)
        tid, sha = case._running_task_with_repo("contracted", commands=["exit 91"])
        case._set_mode(tid, "live")
        conn = db.connect(case.db_path)
        self.addCleanup(conn.close)
        with db.write_txn(conn):
            request, _ = db.create_request(conn, util.now_iso(), "dispatch", "contracted-dispatch", task_id=tid)
            request_id = request["request_id"]
            conn.execute("UPDATE requests SET status='completed',run_id=? WHERE request_id=?",
                         (case.spec["run_id"], request_id))
        case.spec.update(request_id=request_id, instruction="Implement only the frozen plan",
                         required_verification_commands=case.commands)
        contract = execution_contract.freeze_spec(case.spec,
            requirements_sha256=case._row(tid)["requirements_sha256"])
        execution_contract.attach_to_job(case.spec, case.job)
        case.write_evidence()
        with patch.object(verdict, "run_verify_commands", side_effect=AssertionError("Pilot must not verify")):
            result = case.verdict(tid, sha)
            envelope = stage_contract.validate_result(result["stage_result"], contract)
            self.assertTrue(result["verification_ok"])
            self.assertEqual(envelope["outcome"], "succeeded")
            for column, changed, original in (("stage", "review", "dispatch"),
                                               ("run_id", "other-run", case.spec["run_id"])):
                with self.subTest(column=column):
                    conn.execute("UPDATE requests SET " + column + "=? WHERE request_id=?", (changed, request_id))
                    rejected = case.call("engine-verdict", {"task_id": tid,
                        "expected_execution_run_id": case.spec["run_id"], "expected_plan_sha256": sha})
                    self.assertFalse(rejected["verification_ok"])
                    self.assertEqual(rejected["failure_code"], "verification_identity_unconfirmed")
                    conn.execute("UPDATE requests SET " + column + "=? WHERE request_id=?", (original, request_id))
            case.spec["instruction"] = "A different task scope after dispatch"
            case.write_evidence()
            rejected = case.call("engine-verdict", {"task_id": tid,
                "expected_execution_run_id": case.spec["run_id"], "expected_plan_sha256": sha})
            self.assertFalse(rejected["verification_ok"])
            self.assertEqual(rejected["failure_code"], "verification_identity_unconfirmed")


if __name__ == "__main__":
    unittest.main()
