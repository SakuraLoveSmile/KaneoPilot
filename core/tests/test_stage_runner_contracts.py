"""Isolated runner entrypoints: no real model, production DB, or network service."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from kanpilot import (claude_planner, cli_review, db, executor_runner, pi_desktop_planner, pi_desktop_review,
                      planner_config, planner_runner, review_settle, stage_contract, util)
from test_plus_planner import PlusPlannerCase
from test_plus_review import ReviewCase


REVIEW = {"conclusion": "pass", "report_markdown": "# Review\n固定证据", "fix": None}


class PlanEntrypointContracts(PlusPlannerCase):
    def test_codex_rejects_wrong_manifest_before_process_start(self):
        scene = self.plus_request()
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, scene["request_id"]))
            payload.update(planner_backend="codex", planner_input_sha256="0" * 64)
            with db.write_txn(conn):
                db.update_request(conn, scene["request_id"], util.now_iso(), payload_json=payload)
        with mock.patch.object(planner_runner.subprocess, "Popen") as process:
            self.run_round()
        process.assert_not_called()
        self.assertIsNone(self.server.created)
        self.assertIn("指纹", self.task(scene["task_id"])["failure_reason"])

    def test_plus_proposal_is_bound_to_claimed_contract(self):
        scene = self.plus_request()
        markdown = "# 实施计划\n\n检查变更\n"
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", markdown)
        self.server.add_plan_proposal(markdown=markdown)
        with self.fast():
            self.run_round()
        _, job = self.job_for(scene["task_id"], scene["request_id"])
        contract = stage_contract.validate_contract(job["stage_contract"])
        result = stage_contract.validate_result(job["stage_result"], contract)
        self.assertEqual(result["payload"]["plan_markdown"], markdown)
        self.assertEqual(result["evidence"][0]["mechanism"], "plus_plan_proposal")
        self.assertEqual(contract["inputs"]["input_id"], scene["info"]["input_id"])
        self.assertEqual(contract["workspace"], job["worktree_path"])
        self.assertNotEqual(contract["workspace"], str(scene["repo"]))
        identity = contract["inputs"]["worktree_identity"]
        self.assertEqual(identity["pool"]["slot_id"], job["pool_lease"]["slot_id"])
        self.assertEqual(len(identity["source_sha256"]), 64)
        self.assertTrue(self.server.resolve_calls)

    def _contracted_parent(self, *, ignore_materials=False):
        scene = self.plus_request()
        if ignore_materials:
            ignore = self.root / "git-global-ignore"
            ignore.write_text(".kaneopilot-plan-material/\n")
            subprocess.run(["git", "-C", str(scene["repo"]), "config", "core.excludesfile", str(ignore)],
                           check=True, capture_output=True)
        claim = self.call("claim", {"stage": "plan", "executor": "fixture",
                                    "request_id": scene["request_id"]})
        directory = self.run_dir(scene["task_id"], claim["run_id"])
        directory.mkdir(parents=True, exist_ok=True)
        old = pi_desktop_planner._Round(self.db_path, scene["request_id"], claim["run_id"],
            context=claim["context"], task=claim["context"]["task"], task_id=scene["task_id"],
            run_dir=directory, timeout=30, heartbeat_every=1)
        worktree, _, materials = old._prepare_materials()
        old.job.update(worktree_path=str(worktree), session_id="session", turn_id="turn",
            managed_material_paths=[str(materials.manifest_path)] + [item["path"] for item in materials.files])
        prepared = planner_runner._prepare_stage_inputs(old.context, old.task_id,
            old.request_id, old.run_id, "pi_desktop")
        planner_runner._freeze_plan_contract(old.job, prepared, worktree)
        current_context = copy.deepcopy(claim["context"])
        current_context["payload"]["runtime_config"]["model_id"] = "changed-current-model"
        current_dir = self.root / "current-round"
        current_dir.mkdir()
        current = pi_desktop_planner._Round(self.db_path, scene["request_id"], "new-run",
            context=current_context, task=current_context["task"], task_id=scene["task_id"],
            run_dir=current_dir, timeout=30, heartbeat_every=1)
        return old, current

    def test_finished_parent_missing_result_rejects_before_connect_or_publish(self):
        old, current = self._contracted_parent()
        old.job["status"] = "finished"
        with mock.patch.object(current, "_connect") as connect, \
             mock.patch.object(current, "_publish") as publish, \
             mock.patch.object(current, "fail", return_value=True) as fail:
            self.assertTrue(current._resume(old.job))
        connect.assert_not_called()
        publish.assert_not_called()
        self.assertIn("缺少结果包", fail.call_args.args[0])

    def test_active_parent_resume_binds_original_workspace_and_runtime(self):
        old, current = self._contracted_parent()
        with mock.patch.object(current, "_connect", return_value="fixture stop") as connect, \
             mock.patch.object(current, "defer", return_value=True), \
             mock.patch.object(current, "fail", return_value=True) as fail:
            self.assertTrue(current._resume(old.job))
        fail.assert_not_called()
        connect.assert_called_once()
        contract = stage_contract.validate_contract(current.job["stage_contract"])
        self.assertEqual(contract["workspace"], old.job["stage_contract"]["workspace"])
        self.assertEqual(contract["config"]["runtime"], old.job["config"])
        self.assertNotEqual(contract["config"]["runtime"]["model_id"], "changed-current-model")
        self.assertEqual(contract["inputs"]["resume_from"]["contract_sha256"],
                         old.job["stage_contract"]["contract_sha256"])

    def test_changed_parent_source_rejects_before_connect(self):
        old, current = self._contracted_parent()
        (Path(old.job["worktree_path"]) / "AGENTS.md").write_text("changed source\n")
        with mock.patch.object(current, "_connect") as connect, \
             mock.patch.object(current, "fail", return_value=True) as fail:
            self.assertTrue(current._resume(old.job))
        connect.assert_not_called()
        self.assertIn("源码或槽位身份", fail.call_args.args[0])

    def test_ignored_material_changes_reject_before_connect(self):
        old, current = self._contracted_parent(ignore_materials=True)
        material = Path(old.job["managed_material_paths"][0])
        ignored = subprocess.run(["git", "-C", old.job["worktree_path"], "check-ignore", str(material)],
                                 capture_output=True)
        self.assertEqual(ignored.returncode, 0)
        material.write_text("changed ignored material\n")
        with mock.patch.object(current, "_connect") as connect, \
             mock.patch.object(current, "fail", return_value=True) as fail:
            self.assertTrue(current._resume(old.job))
        connect.assert_not_called()
        self.assertIn("源码或槽位身份", fail.call_args.args[0])

    def test_ignored_material_symlink_rejects_before_connect(self):
        old, current = self._contracted_parent(ignore_materials=True)
        material = Path(old.job["managed_material_paths"][0])
        material.unlink()
        material.symlink_to(Path(old.job["worktree_path"]) / "AGENTS.md")
        with mock.patch.object(current, "_connect") as connect, \
             mock.patch.object(current, "fail", return_value=True) as fail:
            self.assertTrue(current._resume(old.job))
        connect.assert_not_called()
        self.assertIn("不是普通文件", fail.call_args.args[0])

    def test_fake_codex_uses_same_plan_payload_gate(self):
        scene = self.plus_request()
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, scene["request_id"]))
            payload["planner_backend"] = "codex"
            payload["model_config"] = dict(planner_config.DEFAULTS["standard"])
            payload.pop("runtime_config", None)
            with db.write_txn(conn):
                db.update_request(conn, scene["request_id"], util.now_iso(), payload_json=payload)
        with mock.patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}):
            self.run_round()
        _, job = self.job_for(scene["task_id"], scene["request_id"])
        result = stage_contract.validate_result(job["stage_result"], job["stage_contract"])
        self.assertEqual(result["harness_id"], "codex")
        self.assertEqual(set(result["payload"]), {"status", "plan_markdown", "questions", "evidence"})

    def _codex_process(self, mutate=False):
        scene = self.plus_request()
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, scene["request_id"]))
            payload.update(planner_backend="codex", model_config=dict(planner_config.DEFAULTS["standard"]))
            payload.pop("runtime_config", None)
            with db.write_txn(conn):
                db.update_request(conn, scene["request_id"], util.now_iso(), payload_json=payload)
        output = {"status": "ready", "plan_markdown": "# 实施计划\n\n检查入口\n",
                  "questions": [], "evidence": []}
        def launcher(_job, _schema, out_path, _images, **_kwargs):
            script = "import sys; from pathlib import Path; sys.stdin.read(); "
            if mutate:
                script += "Path('AGENTS.md').write_text('changed source'); "
            script += "Path(" + repr(str(out_path)) + ").write_text(" + repr(json.dumps(output)) + ")"
            return [sys.executable, "-c", script]
        with mock.patch.object(planner_runner.codex_planner, "launcher_argv", side_effect=launcher), \
             mock.patch.object(planner_runner.codex_planner.codex_provider, "resolve_provider", return_value=None):
            self.run_round()
        return scene, self.job_for(scene["task_id"], scene["request_id"])[1]

    def test_codex_process_binds_actual_slot_and_source(self):
        scene, job = self._codex_process()
        contract = stage_contract.validate_contract(job["stage_contract"])
        stage_contract.validate_result(job["stage_result"], contract)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(contract["workspace"], job["worktree_path"])
        self.assertNotEqual(contract["workspace"], str(scene["repo"]))
        self.assertEqual(contract["inputs"]["worktree_identity"]["pool"]["slot_id"],
                         job["pool_lease"]["slot_id"])

    def test_codex_process_changed_source_cannot_publish(self):
        scene, job = self._codex_process(mutate=True)
        self.assertEqual(job["status"], "failed")
        self.assertIn("源码或槽位身份", job["detail"])
        self.assertFalse(self.task(scene["task_id"]).get("plan_path"))

    def test_claude_adapter_binds_actual_slot_and_source(self):
        scene = self.plus_request()
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, scene["request_id"]))
            payload.update(planner_backend="claude", model_config={
                "planner_model": "fixture-model", "reasoning_effort": "high"})
            payload.pop("runtime_config", None)
            with db.write_txn(conn):
                db.update_request(conn, scene["request_id"], util.now_iso(), payload_json=payload)
        with mock.patch.object(claude_planner.claude_cli, "run", return_value={
                "is_error": False, "exit_code": 0, "total_cost_usd": 0,
                "structured_output": {"status": "ready", "plan_markdown": "# 实施计划\n\n检查入口\n",
                                      "questions": [], "evidence": []}}):
            self.run_round()
        _, job = self.job_for(scene["task_id"], scene["request_id"])
        contract = stage_contract.validate_contract(job["stage_contract"])
        stage_contract.validate_result(job["stage_result"], contract)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(contract["workspace"], job["worktree_path"])
        self.assertNotEqual(contract["workspace"], str(scene["repo"]))
        self.assertEqual(contract["inputs"]["worktree_identity"]["pool"]["slot_id"],
                         job["pool_lease"]["slot_id"])


class PlanOutputContracts(unittest.TestCase):
    def test_cli_decode_rejects_missing_fields_instead_of_repairing(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "output.txt"
            for payload in ({"status": "ready", "plan_markdown": "# Plan"},
                            {"status": "needs_input", "questions": ["目标？"]},
                            {"status": "ready", "plan_markdown": "# Plan", "questions": [], "evidence": [42]}):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(util.KanpilotError):
                        planner_runner.parse_output(path)


class ReviewEntrypointContracts(ReviewCase):
    def test_plus_result_uses_common_review_contract(self):
        self.make_scene()
        self.record_core_calls()
        self.review_result(**{"conclusion": REVIEW["conclusion"], "markdown": REVIEW["report_markdown"]})
        self.assertEqual(self.run_review(), 0)
        job = self.review_job()
        result = stage_contract.validate_result(job["stage_result"], job["stage_contract"])
        self.assertEqual(result["payload"], REVIEW)
        self.assertEqual(result["evidence"][0]["mechanism"], "plus_turn")
        self.assertIn("register-review", self.commands())

    def test_cli_result_uses_same_review_contract_and_settlement(self):
        self.make_scene()
        self.record_core_calls()
        config = {"backend": "claude", "model": "fixture-model", "effort": "high"}
        with db.connect(self.db_path) as conn:
            with mock.patch.object(cli_review.review_config, "snapshot", return_value=config), \
                 mock.patch.object(cli_review.claude_cli, "run", return_value={
                     "is_error": False, "exit_code": 0, "structured_output": REVIEW,
                     "total_cost_usd": 0}):
                code = cli_review.run_stage("review", conn=conn, db_path=self.db_path,
                    timeout=3, log_path=self.root / "review.log", config_override=config,
                    record_run=lambda **fields: executor_runner._record_run(conn, "review", **fields))
        self.assertEqual(code, 0)
        job = self.review_job()
        result = stage_contract.validate_result(job["stage_result"], job["stage_contract"])
        self.assertEqual(result["payload"], REVIEW)
        self.assertEqual(result["evidence"][0]["mechanism"], "cli_process")
        self.assertIn("register-review", self.commands())


class ReviewSettlementContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.job = {"task_id": "task", "request_id": "request", "run_id": "review-run",
                    "execution_run_id": "exec-run", "backend": "codex", "config": {},
                    "review_root": str(root), "execution_worktree": str(root),
                    "materials": {}, "material_hashes": {}, **REVIEW}
        self.task = {"task_id": "task", "run_id": "exec-run", "extra": {}}
        self.request = {"request_id": "request", "run_id": "review-run", "stage": "review",
                        "status": "claimed", "lease_expires_at": util.to_iso(util.now_dt() + timedelta(minutes=5))}
        self.facts = {"task": self.task, "request": self.request}
        review_settle.freeze_stage_contract(self.job, self.task)
        review_settle.record_stage_result(self.job, REVIEW, facts=self.facts)

    def settle(self):
        with mock.patch.object(pi_desktop_review, "_facts", return_value=self.facts), \
             mock.patch.object(pi_desktop_review, "_stop_no_write", return_value=19) as stop, \
             mock.patch.object(pi_desktop_review, "core_call") as call:
            code = review_settle.settle_job(driver=pi_desktop_review, db_path="isolated",
                job=self.job, job_path=Path(self.temp.name) / "job.json",
                log=mock.Mock(), record_run=mock.Mock())
            self.assertEqual(code, 19)
            stop.assert_called_once()
            call.assert_not_called()

    def test_foreign_contract_result_never_registers(self):
        other = copy.deepcopy(self.job)
        other["run_id"] = "older-run"
        other = {k: v for k, v in other.items() if k not in ("stage_contract", "stage_result")}
        review_settle.freeze_stage_contract(other, self.task)
        self.job["stage_result"] = stage_contract.make_result(other["stage_contract"], REVIEW)
        self.settle()

    def test_current_claim_rejects_old_run_even_with_valid_result(self):
        self.request["run_id"] = "new-run"
        self.settle()

    def test_result_body_tampering_is_rejected_before_report(self):
        self.job["report_markdown"] = "# Different body"
        self.settle()

    def test_missing_result_cannot_fall_back_to_legacy(self):
        del self.job["stage_result"]
        self.settle()

    def test_missing_contract_on_new_job_cannot_fall_back_to_legacy(self):
        del self.job["stage_contract"]
        self.settle()

    def test_cli_resume_rejects_foreign_result_without_resending(self):
        self.job.update(stage="review", status="finished", exit_code=0)
        self.job["stage_result"]["run_id"] = "foreign-run"
        with mock.patch.object(cli_review, "_persisted", return_value=(self.job, Path(self.temp.name))), \
             mock.patch.object(cli_review.claude_cli, "run") as process:
            code = cli_review.run_stage("review", conn=mock.Mock(), db_path="isolated",
                timeout=1, log_path=Path(self.temp.name) / "log", record_run=mock.Mock(), resume=True)
        self.assertEqual(code, 1)
        process.assert_not_called()

    def test_plus_resume_rejects_foreign_result_without_observing_or_sending(self):
        self.job["stage_result"]["run_id"] = "foreign-run"
        with mock.patch.object(pi_desktop_review, "_persisted_round", return_value={"job": self.job}), \
             mock.patch.object(pi_desktop_review.pi_desktop_controller, "_open_client") as connect:
            code = pi_desktop_review._resume_round(conn=mock.Mock(), db_path="isolated",
                block={}, prompt="unused", timeout=1, log_path=Path(self.temp.name) / "log",
                record_run=mock.Mock())
        self.assertEqual(code, 1)
        connect.assert_not_called()

    def test_result_cannot_be_created_after_claim_changes(self):
        self.request["run_id"] = "new-run"
        with self.assertRaises(util.KanpilotError) as caught:
            review_settle.record_stage_result(self.job, REVIEW, facts=self.facts)
        self.assertEqual(caught.exception.code, "stale_run")


if __name__ == "__main__":
    unittest.main()
