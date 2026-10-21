"""Codex strict verdicts over isolated SQLite, Git and native evidence files."""
import copy
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_engine_verdict as fixtures
from kanpilot import auto_monitor, cmd_read, db, execution_contract, round_store, stage_contract, util
from kanpilot.engine import verdict


class CodexVerdictIntegration(unittest.TestCase):
    def setUp(self):
        self.read_report = cmd_read.read_goal_report
        self.case = fixtures.TestEngineVerdictCommand()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def prepare(self, label, mode="legacy"):
        c = self.case
        tid, sha = c._running_task_with_repo(label, commands=["exit 91"])
        c._set_mode(tid, mode)
        conn = db.connect(c.db_path)
        worker_pid = 1073741823
        with db.write_txn(conn):
            request, _ = db.create_request(conn, util.now_iso(), "dispatch", label + "-dispatch", task_id=tid)
            request_id = request["request_id"]
            conn.execute("UPDATE requests SET status='completed',run_id=? WHERE request_id=?", (c.spec["run_id"], request_id))
            config = {"backend": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "high", "sandbox": "workspace-write"}
            conn.execute("UPDATE execution_rounds SET backend='codex',config_json=? WHERE task_id=?", (util.jdump(config), tid))
            round_store.mark_started(conn, tid, c.spec["run_id"], util.now_iso(), pid=worker_pid)
        c.spec.update(backend="codex", config=config, request_id=request_id, scope_id="codex-execution:" + label,
                      instruction="Implement only the frozen plan", required_verification_commands=c.commands)
        c.job.update(backend="codex", config=config, runs_dir=str(c.directory), pid=worker_pid,
                     worker_birth={"started_seconds": 1, "started_microseconds": 0},
                     turn_id="codex:" + c.spec["run_id"] + ":1",
                     cli_terminal=True, ended_at=util.now_iso(), report_id="codex-report:" + label)
        self.contract = execution_contract.freeze_spec(c.spec, requirements_sha256=c._row(tid)["requirements_sha256"])
        execution_contract.attach_to_job(c.spec, c.job)
        self.report = {"status": "completed", "summary": "done", "checks": [{"command": "exit 91", "exit_code": 0}], "questions": []}
        self.native = [{"type": "thread.started", "thread_id": "session"},
            {"type": "item.started", "item": {"id": "command-1", "type": "command_execution", "command": "exit 91", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "command-1", "type": "command_execution", "command": "exit 91", "status": "completed", "exit_code": 0}},
            {"type": "turn.completed"}]
        c.job["turns"] = [{"turn_id": c.job["turn_id"], "source": "codex_adapter", "session_id": "session", "pid": 1073741822,
            "birth": {"started_seconds": 1, "started_microseconds": 0}, "status": "completed", "exit_code": 0,
            "cli_terminal": True, "ended_at": util.now_iso(), "events_file": "codex-turn-1.jsonl", "output_file": "codex-output-1.json"}]
        c.events[0]["turn_id"] = c.job["turn_id"]
        self.persist()
        conn.close()
        return tid, sha

    def persist(self):
        c = self.case
        turn = c.job["turns"][-1]
        native_path, output_path = c.directory / turn["events_file"], c.directory / turn["output_file"]
        native_path.write_text("".join(util.jdump({"seq": i, "turn_id": c.job["turn_id"], "event": event,
            "source_sha256": c.job["verification_source_sha256"]}) + "\n" for i, event in enumerate(self.native, 1)))
        output_path.write_text(util.jdump(self.report))
        turn.update(native_sha256=util.sha256_file(native_path), output_sha256=util.sha256_file(output_path))
        envelope = {key: c.job[key] for key in ("task_id", "run_id", "session_id", "turn_id", "plan_sha256", "verification_source_sha256", "report_id")}
        envelope.update(report=self.report, native_sha256=turn["native_sha256"])
        (c.directory / "codex-report.json").write_text(util.jdump(envelope))
        c.write_evidence()
        c.job["spec_sha256"] = util.sha256_file(c.directory / "spec.json")
        c.write_evidence()

    def test_all_modes_use_strict_report_and_never_plus_or_local_verify(self):
        c = self.case
        for mode in ("legacy", "shadow", "live"):
            with self.subTest(mode=mode):
                tid, sha = self.prepare("codex-" + mode, mode)
                with patch.object(verdict, "evaluate_task", side_effect=AssertionError("legacy verdict")), \
                     patch.object(verdict, "evaluate_goal_task", side_effect=AssertionError("Plus verdict")), \
                     patch.object(verdict, "run_verify_commands", side_effect=AssertionError("local verify")):
                    receipt = c.verdict(tid, sha, operation_id="same")
                self.assertTrue(receipt["verification_ok"], receipt)
                self.assertEqual(receipt["evidence_source"], "codex_report")
                self.assertEqual(stage_contract.validate_result(receipt["stage_result"], self.contract)["outcome"], "succeeded")
                self.assertNotIn("goal_report_comparison", receipt)
                self.assertTrue(c.verdict(tid, sha, operation_id="same")["deduplicated"])
                self.assertEqual(db.task_extra(c._row(tid))["engine_verdict"]["evidence_source"], "codex_report")

    def test_persisted_worker_and_observer_identity_mismatch_rejects_prior_receipt(self):
        for mismatch in ("missing-worker", "different-worker", "different-observer"):
            with self.subTest(mismatch=mismatch):
                tid, sha = self.prepare(mismatch)
                c = self.case
                # A historical observer PID is valid only when it is the same
                # registered worker; a finished fixture still binds that PID.
                c.job["observe_pid"] = c.job["pid"]
                self.persist()
                self.assertTrue(c.verdict(tid, sha, operation_id="same")["verification_ok"])
                if mismatch == "different-observer":
                    c.job["observe_pid"] = c.job["pid"] - 1
                    self.persist()
                else:
                    conn = db.connect(c.db_path)
                    try:
                        with db.write_txn(conn):
                            conn.execute("UPDATE execution_rounds SET pid=? WHERE task_id=? AND run_id=?",
                                         (None if mismatch == "missing-worker" else c.job["pid"] - 1,
                                          tid, c.spec["run_id"]))
                    finally:
                        conn.close()
                rejected = c.verdict(tid, sha, operation_id="same")
                self.assertFalse(rejected["verification_ok"])
                self.assertFalse(rejected.get("deduplicated", False))
                self.assertEqual(rejected["failure_code"], "verification_identity_unconfirmed")
                self.assertIn("Codex worker 进程登记与持久记录不符", rejected["detail"])

    def test_native_missing_duplicate_boolean_failure_and_model_missing_reject(self):
        mutations = {"missing": lambda: self.native.pop(2),
            "duplicate": lambda: self.native.insert(3, copy.deepcopy(self.native[2])),
            "boolean": lambda: self.native[2]["item"].update(exit_code=True),
            "failure": lambda: self.native[2]["item"].update(exit_code=7),
            "model-missing": lambda: self.report.update(checks=[])}
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                tid, sha = self.prepare("reject-" + label)
                mutate()
                self.persist()
                result = self.case.verdict(tid, sha)
                self.assertFalse(result["verification_ok"], result)
                self.assertEqual(result["kind"], "verify_failed" if label == "failure" else "unconfirmed")

    def test_same_operation_rechecks_report_and_source(self):
        tid, sha = self.prepare("receipt-drift")
        self.assertTrue(self.case.verdict(tid, sha, operation_id="same")["verification_ok"])
        self.report["checks"] = []
        self.persist()
        self.assertFalse(self.case.verdict(tid, sha, operation_id="same")["verification_ok"])
        tid, sha = self.prepare("source-drift")
        self.assertTrue(self.case.verdict(tid, sha)["verification_ok"])
        (self.case.worktree / "after.py").write_text("drift")
        self.assertFalse(self.case.verdict(tid, sha)["verification_ok"])

    def test_committed_same_source_receipt_recovers_and_changed_evidence_rejects(self):
        tid, sha = self.prepare("codex-commit")
        c = self.case
        subprocess.run(["git", "-C", str(c.worktree), "switch", "-c", "codex-execution"], check=True, capture_output=True)
        conn = db.connect(c.db_path)
        self.addCleanup(conn.close)
        base = subprocess.check_output(["git", "-C", str(c.worktree), "rev-parse", "HEAD"]).decode().strip()
        db.set_extra(conn, db.require_task(conn, tid), util.now_iso(), delivery={"mode": "manual", "workspace_version": 1,
            "worktree_path": str(c.worktree), "branch": "codex-execution", "head_sha": base})
        receipt = c.verdict(tid, sha)
        self.assertTrue(receipt["verification_ok"], receipt)
        for args in (("add", "."), ("commit", "-m", "verified source\n\nKaneoPilot-Run: run-codex-commit")):
            subprocess.run(["git", "-C", str(c.worktree), *args], check=True, capture_output=True)
        self.assertTrue(verdict.valid_committed_receipt(conn, db.require_task(conn, tid), receipt))
        self.assertTrue(c.verdict(tid, sha)["deduplicated"])
        self.report["checks"] = []
        self.persist()
        self.assertFalse(verdict.valid_committed_receipt(conn, db.require_task(conn, tid), receipt))

    def test_report_reader_exposes_native_report_without_plus_goal(self):
        tid, _ = self.prepare("native-read")
        conn = db.connect(self.case.db_path)
        self.addCleanup(conn.close)
        with patch.object(cmd_read, "read_goal_report", self.read_report):
            result = cmd_read.cmd_get_goal_report(SimpleNamespace(conn=conn), {"task_id": tid, "run_id": "run-native-read"})
        self.assertEqual(result["backend"], "codex")
        self.assertIsNone(result["goal_approval"])
        self.assertIsNone(result["proposal_id"])
        self.assertIsNone(result["verdict"])
        self.assertEqual(result["report"], self.report)
        self.assertTrue(result["checks_confirmed"])


class CodexMonitorTests(unittest.TestCase):
    def test_every_mode_requires_strict_verdict_and_does_not_fallback(self):
        for mode in ("legacy", "shadow", "live"):
            for passed in (True, False):
                with self.subTest(mode=mode, passed=passed):
                    controller = SimpleNamespace(claim={"request_id": "monitor", "context": {"lease_seconds": 180}},
                        deadline=1000, clock=lambda: 0, heartbeat=lambda **kw: None,
                        identity=lambda: {"run_id": "monitor"}, call=lambda *a, **kw: {"verification_ok": passed},
                        report=lambda *a, **kw: None)
                    with patch.object(auto_monitor, "_engine_mode", return_value=mode), \
                         patch.object(auto_monitor, "_verification", side_effect=AssertionError("transcript fallback")), \
                         patch.object(auto_monitor, "_finish_complete") as finished:
                        auto_monitor._complete(controller, {"task_id": "task", "run_id": "run", "plan_sha256": "plan"},
                            {"backend": "codex"}, "in-progress", [])
                    self.assertEqual(finished.called, passed)

    def test_monitor_reconciles_normal_stop_and_never_reads_plus_messages(self):
        task = {"task_id": "task", "run_id": "run", "extra": {}}
        detail = {"backend": "codex", "observer_alive": False, "run_status": "finished",
                  "terminal": {"outcome": "completed"}, "exit_code": 0, "send_state": "sent"}
        for status in ("observed", "stopped"):
            with self.subTest(status=status):
                calls = []
                controller = SimpleNamespace(call=lambda name, payload: {"status": status},
                    report=lambda *a, **kw: calls.append((a, kw)))
                with patch.object(auto_monitor, "_get_task", return_value=task), \
                     patch.object(auto_monitor, "_recheck", return_value={"kaneo_status": "in-progress"}), \
                     patch.object(auto_monitor, "_get_session", return_value={"round_detail": detail}), \
                     patch.object(auto_monitor, "_observe", return_value=None), \
                     patch.object(auto_monitor, "_read_messages", side_effect=AssertionError("Plus transcript read")), \
                     patch.object(auto_monitor, "_complete") as complete:
                    auto_monitor._monitor(controller)
                self.assertEqual(complete.called, status == "observed")
                if status == "stopped":
                    self.assertEqual(calls[0][0], ("needs_user",))

    def test_observation_is_explicitly_read_only_and_cost_source_is_codex(self):
        calls = []
        controller = SimpleNamespace(call=lambda name, payload: calls.append((name, payload)))
        task = {"task_id": "task", "run_id": "run", "extra": {"automation": {"enabled": True}},
                "attempts": [{"stage": "dispatch", "run_id": "run", "request_id": "dispatch"}]}
        auto_monitor._observe(controller, task, {"backend": "codex", "observer_alive": False})
        self.assertFalse(calls[0][1]["continue_goal"])
        auto_monitor._record_cost(controller, task, {"backend": "codex"})
        self.assertEqual(calls[1][1]["source"], "codex")
        self.assertIsNone(calls[1][1]["cost_usd"])
