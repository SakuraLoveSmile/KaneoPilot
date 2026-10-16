"""Plus-evidence verdict tests with isolated files, Git worktrees and SQLite.

Run: ``cd core && python3 -m pytest tests/test_engine_verdict.py -q``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import db, util, round_store, harness_plus_adapter  # noqa: E402
from kanpilot.engine import verdict  # noqa: E402


def _git_repo():
    root = Path(tempfile.mkdtemp(prefix="verdict-repo-"))
    for args in (("init", "-b", "main"),
                 ("config", "user.name", "t"),
                 ("config", "user.email", "t@e.invalid"),
                 ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, timeout=15)
    return root


class TestVerdictSource(unittest.TestCase):
    def test_only_real_source_changes_count(self):
        repo = _git_repo()
        self.addCleanup(shutil.rmtree, repo)
        self.assertEqual(verdict.effective_changes(repo), [])
        (repo / ".pi").mkdir()
        (repo / ".pi" / "goal.md").write_text("bookkeeping")
        (repo / "module.pyc").write_bytes(b"cache")
        self.assertEqual(verdict.effective_changes(repo), [])
        (repo / "src.py").write_text("source")
        self.assertEqual(verdict.effective_changes(repo), ["src.py"])

    def test_retired_local_runners_fail_closed(self):
        for runner in (verdict.run_command, verdict.run_verify_commands):
            with self.assertRaises(util.KanpilotError) as caught:
                runner("echo should-never-run", cwd="/tmp")
            self.assertEqual(caught.exception.code, "local_verification_disabled")


class TestEngineVerdictCommand(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.plus_state = {"session_id": "session", "is_running": False,
                           "current_turn_id": "execution", "pending_permission": None}
        self.plus_turn = {"session_id": "session", "turn_id": "execution", "status": "completed"}
        status_patch = patch.object(harness_plus_adapter, "plus_status",
            side_effect=lambda *a, **k: dict(self.plus_state))
        turn_patch = patch.object(harness_plus_adapter, "plus_turn_get",
            side_effect=lambda *a, **k: dict(self.plus_turn))
        self.plus_status_mock = status_patch.start()
        self.plus_turn_mock = turn_patch.start()
        self.addCleanup(status_patch.stop)
        self.addCleanup(turn_patch.stop)

    def _running_task_with_repo(self, task_id, *, commands, change=True):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        repo = self.repos_root / "Feedback"
        worktree = self.root / ("verdict-tree-" + task_id)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree)],
                       check=True, capture_output=True)
        if change:
            (worktree / f"change-{task_id}.py").write_text("x = 1\n", encoding="utf-8")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                plan = Path(task["plan_path"])
                plan.write_text(plan.read_text() + "\n```verify\n" + "\n".join(commands) + "\n```\n")
                sha = util.sha256_file(plan)
                db.update_task(conn, tid, util.now_iso(), plan_sha256=sha)
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(),
                             plan_verify={"plan_sha256": task["plan_sha256"],
                                          "commands": commands})
                conn.execute("UPDATE tasks SET status='running', run_id=? WHERE task_id=?",
                             ("run-" + task_id, tid))
                round_store.upsert_on_claim(conn, tid, "run-" + task_id,
                    {"connection_file": str(self.root / "fake-plus.json")}, util.now_iso())
                round_store.mark_started(conn, tid, "run-" + task_id, util.now_iso(), worktree_path=str(worktree))
        finally:
            conn.close()
        self.directory = self.runs_root / tid / ("run-" + task_id)
        self.directory.mkdir(parents=True)
        self.commands = commands
        self.worktree = worktree
        self.job = {"task_id": tid, "run_id": "run-" + task_id, "backend": "pi_desktop",
                    "config": {"connection_file": str(self.root / "fake-plus.json")}, "status": "finished", "exit_code": 0, "session_id": "session",
                    "turn_id": "execution", "send_state": "sent", "plan_sha256": sha,
                    "completion_policy": "command_verification", "verification_evidence_version": 1,
                    "verification_source_sha256": verdict.git_delivery.source_state_sha256(worktree)}
        self.spec = {**self.job, "plan_path": str(plan), "repository": str(repo), "cwd": str(worktree),
                     "runs_dir": str(self.directory)}
        self.events = [{"seq": 1, "type": "session", "session_id": "session", "turn_id": "execution"},
            *[{"seq": n + 2, "type": "command_output", "turn_id": "execution", "name": "exec",
               "input": json.dumps({"command": command}), "exit_code": 0, "output": "passed"}
              for n, command in enumerate(commands)],
            {"seq": len(commands) + 2, "type": "terminal", "outcome": "completed"}]
        self.write_evidence()
        conn = db.connect(self.db_path)
        db.update_task(conn, tid, util.now_iso(), session_id="session", sent=1, send_state="sent")
        conn.execute("UPDATE execution_rounds SET backend='pi_desktop', session_id='session' WHERE task_id=?", (tid,))
        conn.close()
        view = self.task(tid)
        return tid, view["plan_sha256"]

    def write_evidence(self):
        for name, data in (("job.json", self.job), ("spec.json", self.spec)):
            (self.directory / name).write_text(json.dumps(data))
        (self.directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in self.events))

    def verdict(self, task_id, sha, **fields):
        return self.call("engine-verdict", {"task_id": task_id, "expected_execution_run_id": "run-" + task_id,
                         "expected_plan_sha256": sha, **fields})

    def _row(self, tid):
        conn = db.connect(self.db_path)
        try:
            return db.require_task(conn, tid)
        finally:
            conn.close()

    def test_plus_success_never_runs_pilot_verification_in_live_or_shadow(self):
        for mode in ("live", "shadow"):
            tid, sha = self._running_task_with_repo("pass-" + mode, commands=["exit 91"])
            row = self._row(tid)
            self.call("set-project-mapping", {"project_id": row["project_id"], "repository": row["repository"], "enabled": True,
                "expected_revision": self.call("get-project-mappings")["revision"]})
            self.call("set-project-engine-mode", {"project_id": row["project_id"], "engine_mode": mode})
            with patch.object(verdict, "run_verify_commands", side_effect=AssertionError("Pilot must not verify")), \
                    patch.object(verdict, "project_verify_env", side_effect=AssertionError("Pilot env must not be read")):
                result = self.verdict(tid, sha)
            self.assertTrue(result["verification_ok"])
            self.assertEqual(result["evidence_source"], "plus_tool_results")
            self.assertEqual(result["checks"][0]["exit_code"], 0)

    def test_real_failure_unknown_missing_and_wrong_turn_cannot_pass(self):
        for label, fields, kind in (("failure", {"exit_code": 7}, "verify_failed"),
                                    ("unknown", {"exit_code": None}, "unconfirmed"),
                                    ("oldturn", {"turn_id": "planning"}, "unconfirmed"),
                                    ("prose", {"type": "assistant_message"}, "unconfirmed")):
            tid, sha = self._running_task_with_repo(label, commands=["pytest"])
            self.events[1].update(fields)
            self.write_evidence()
            result = self.verdict(tid, sha)
            self.assertFalse(result["verification_ok"])
            self.assertEqual(result["kind"], kind)

    def test_missing_snapshot_old_core_receipt_and_payload_checks_cannot_pass(self):
        tid, sha = self._running_task_with_repo("historical", commands=["pytest"])
        self.job.pop("verification_source_sha256")
        self.write_evidence()
        conn = db.connect(self.db_path)
        db.set_extra(conn, db.require_task(conn, tid), util.now_iso(), engine_verdict={
            "operation_id": "old", "verification_ok": True, "execution_run_id": "run-" + tid,
            "plan_sha256": sha, "binding": verdict.binding(conn, db.require_task(conn, tid))})
        conn.close()
        error = self.call_err("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
            "expected_plan_sha256": sha, "checks": [{"exit_code": 0}]})
        self.assertEqual(error.code, "bad_request")
        result = self.verdict(tid, sha, operation_id="old")
        self.assertEqual(result["kind"], "unconfirmed")
        self.assertFalse(result["verification_ok"])

    def test_snapshot_changed_after_plus_finished_cannot_pass(self):
        tid, sha = self._running_task_with_repo("changed", commands=["pytest"])
        (self.worktree / "later.py").write_text("later")
        self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")

    def test_evidence_mutation_and_same_operation_revalidate(self):
        tid, sha = self._running_task_with_repo("repeat", commands=["pytest"])
        self.assertTrue(self.verdict(tid, sha, operation_id="same")["verification_ok"])
        self.assertTrue(self.verdict(tid, sha, operation_id="same")["deduplicated"])
        self.events[1]["exit_code"] = 1
        self.write_evidence()
        result = self.verdict(tid, sha, operation_id="same")
        self.assertFalse(result["verification_ok"])

    def test_wrong_tool_cwd_and_cd_prefix_cannot_pass(self):
        for label, arguments in (("cwd", {"command": "pytest", "cwd": "/tmp"}),
                                 ("workdir", {"command": "pytest", "workdir": ".."}),
                                 ("cd", {"command": "cd /tmp && pytest"})):
            tid, sha = self._running_task_with_repo(label, commands=["pytest"])
            self.events[1]["input"] = json.dumps(arguments)
            self.write_evidence()
            self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")

    def test_conflicting_cwd_and_workdir_rejected_in_both_orders(self):
        for label, reverse in (("cwd-root-first", False), ("cwd-child-first", True)):
            tid, sha = self._running_task_with_repo(label, commands=["pytest"])
            root, child = str(self.worktree), str(self.worktree / "core")
            self.events[1]["input"] = json.dumps({"command": "pytest",
                "cwd": child if reverse else root, "workdir": root if reverse else child})
            self.write_evidence()
            result = self.verdict(tid, sha)
            self.assertEqual(result["kind"], "unconfirmed")
            self.assertEqual(result["failure_code"], "verification_identity_unconfirmed")

    def test_live_plus_session_must_remain_idle_on_original_completed_turn(self):
        tid, sha = self._running_task_with_repo("live-identity", commands=["pytest"])
        receipt = self.verdict(tid, sha)
        self.assertTrue(receipt["verification_ok"])
        conn = db.connect(self.db_path)
        try:
            self.assertTrue(verdict.receipt_evidence_current(conn, db.require_task(conn, tid), receipt))
            self.assertFalse(verdict.receipt_evidence_current(conn, db.require_task(conn, tid), receipt, deadline=time.monotonic() - 1))
            for change in ({"current_turn_id": "new-turn"}, {"is_running": True},
                           {"pending_permission": {"id": "permission"}}):
                original = dict(self.plus_state)
                self.plus_state.update(change)
                result = self.verdict(tid, sha)
                self.assertEqual(result["kind"], "unconfirmed")
                self.assertEqual(result["failure_code"], "verification_identity_unconfirmed")
                self.assertFalse(verdict.receipt_evidence_current(conn, db.require_task(conn, tid), receipt))
                self.plus_state = original
        finally:
            conn.close()
        self.assertTrue(self.plus_status_mock.called)
        self.assertTrue(self.plus_turn_mock.called)
        self.assertTrue(all(call.kwargs["timeout"] == 2 for call in self.plus_status_mock.call_args_list))

    def test_live_plus_turn_failure_or_status_change_during_read_is_rejected(self):
        tid, sha = self._running_task_with_repo("live-race", commands=["pytest"])
        self.plus_turn["status"] = "running"
        self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")
        self.plus_turn["status"] = "completed"
        self.plus_status_mock.side_effect = [dict(self.plus_state), dict(self.plus_state, current_turn_id="new-turn")]
        self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")

    def test_cd_separator_allows_inside_worktree_but_rejects_escape(self):
        for label, directory, passed in (("cd-safe", "-- core", True), ("cd-escape", "-- /tmp", False)):
            tid, sha = self._running_task_with_repo(label, commands=["pytest"])
            self.events[1]["input"] = json.dumps({"command": "cd " + directory + " && pytest"})
            self.write_evidence()
            result = self.verdict(tid, sha)
            self.assertEqual(result["verification_ok"], passed)
            if not passed:
                self.assertEqual(result["failure_code"], "verification_identity_unconfirmed")

    def test_empty_diff_and_environment_diagnostics(self):
        tid, sha = self._running_task_with_repo("empty", commands=["pytest"], change=False)
        self.assertEqual(self.verdict(tid, sha)["kind"], "empty_diff")
        tid, sha = self._running_task_with_repo("java", commands=["pytest"])
        self.events[1].update(exit_code=1, output="This build uses a Java 8 JVM")
        self.write_evidence()
        result = self.verdict(tid, sha)
        self.assertEqual(result["kind"], "environment_failure")
        self.assertIn("Plus", result["detail"])
        self.assertNotIn("verify_env", result["detail"])

    def test_committed_same_source_receipt_survives_but_changed_evidence_does_not(self):
        tid, sha = self._running_task_with_repo("commit", commands=["pytest"])
        subprocess.run(["git", "-C", str(self.worktree), "switch", "-c", "execution-branch"], check=True, capture_output=True)
        conn = db.connect(self.db_path)
        base_head = subprocess.check_output(["git", "-C", str(self.worktree), "rev-parse", "HEAD"]).decode().strip()
        db.set_extra(conn, db.require_task(conn, tid), util.now_iso(), delivery={"mode": "manual", "workspace_version": 1,
            "worktree_path": str(self.worktree), "branch": "execution-branch", "head_sha": base_head})
        conn.close()
        row = self._row(tid)
        self.call("set-project-mapping", {"project_id": row["project_id"], "repository": row["repository"], "enabled": True,
            "expected_revision": self.call("get-project-mappings")["revision"]})
        self.call("set-project-engine-mode", {"project_id": row["project_id"], "engine_mode": "live"})
        receipt = self.verdict(tid, sha)
        self.assertTrue(receipt["verification_ok"])
        for args in (("add", "."), ("commit", "-m", "verified source\n\nKaneoPilot-Run: run-commit")):
            subprocess.run(["git", "-C", str(self.worktree), *args], check=True, capture_output=True)
        conn = db.connect(self.db_path)
        try:
            self.assertTrue(verdict.valid_committed_receipt(conn, db.require_task(conn, tid), receipt))
            self.assertTrue(self.verdict(tid, sha)["deduplicated"])
            self.events[1]["exit_code"] = 1
            self.write_evidence()
            self.assertFalse(verdict.valid_committed_receipt(conn, db.require_task(conn, tid), receipt))
        finally:
            conn.close()

    def test_source_or_evidence_mutation_during_check_is_unconfirmed(self):
        for label, mutate in (("source-race", lambda: (self.worktree / "raced.py").write_text("new")),
                              ("evidence-race", lambda: (self.directory / "events.jsonl").write_text("changed"))):
            tid, sha = self._running_task_with_repo(label, commands=["pytest"])
            original = verdict._plus_checks
            def changing(*args, **kwargs):
                result = original(*args, **kwargs)
                mutate()
                return result
            with patch.object(verdict, "_plus_checks", side_effect=changing):
                self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")

    def test_changed_plan_missing_registered_worktree_and_exhausted_budget_are_unconfirmed(self):
        tid, sha = self._running_task_with_repo("plan-race", commands=["pytest"])
        Path(self._row(tid)["plan_path"]).write_text("changed plan")
        self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")
        tid, sha = self._running_task_with_repo("missing-tree", commands=["pytest"])
        conn = db.connect(self.db_path)
        conn.execute("UPDATE execution_rounds SET worktree_path=NULL WHERE task_id=?", (tid,))
        conn.close()
        self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")
        tid, sha = self._running_task_with_repo("budget", commands=["pytest"])
        original = verdict.git_delivery.source_state_sha256
        def exhausted(path, **kwargs):
            return original(path, **dict(kwargs, deadline=time.monotonic() - 1))
        with patch.object(verdict.git_delivery, "source_state_sha256", side_effect=exhausted):
            self.assertEqual(self.verdict(tid, sha, budget_seconds=1)["kind"], "unconfirmed")

    def test_incomplete_terminal_and_events_cannot_pass(self):
        for label, fields in (("running", {"status": "running"}), ("exitunknown", {"exit_code": None})):
            tid, sha = self._running_task_with_repo(label, commands=["pytest"])
            self.job.update(fields)
            self.write_evidence()
            self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")
        tid, sha = self._running_task_with_repo("truncated", commands=["pytest"])
        with (self.directory / "events.jsonl").open("a") as file:
            file.write('{"seq":')
        self.assertEqual(self.verdict(tid, sha)["kind"], "unconfirmed")

class TestEnvironmentClassification(unittest.TestCase):
    def test_controlled_persisted_reason_survives_redaction_with_legacy_fallback(self):
        check = {"exit_code": 1, "timed_out": False, "environment_reason": "android_sdk_missing",
                 "output_tail": "SDK loc«redacted»tion not found."}
        self.assertEqual(verdict.environment_reason(check), "android_sdk_missing")
        self.assertIsNone(verdict.environment_reason(dict(check, exit_code=0)))
        self.assertIsNone(verdict.environment_reason(dict(check, timed_out=True)))
        self.assertIsNone(verdict.environment_reason(dict(check, exit_code=None)))
        self.assertIsNone(verdict.environment_reason(dict(check, environment_reason="unknown")))
        self.assertEqual(verdict.environment_reason({"exit_code": 1, "output_tail": "SDK location not found."}),
                         "android_sdk_missing")

    def test_explicit_failures_prioritize_environment_signatures(self):
        cases = [(127, "Unsupported class file major version 61", "command_not_found"),
                 (1, "command not found: gradle", "command_not_found"),
                 (1, "This build uses a Java 8 JVM", "jdk_version"),
                 (1, "Dependency requires at least JVM runtime version 17", "jdk_version"),
                 (1, "Unsupported class file major version 61", "jdk_version"),
                 (1, "SDK location not found", "android_sdk_missing"),
                 (1, "No connected devices!", "no_connected_device")]
        for code, output, reason in cases:
            with self.subTest(output=output):
                check = {"exit_code": code, "output_tail": output, "timed_out": False}
                self.assertEqual(verdict.environment_reason(check), reason)
                self.assertIsNone(verdict.environment_reason(dict(check, exit_code=0)))
                self.assertIsNone(verdict.environment_reason(dict(check, timed_out=True)))
                self.assertIsNone(verdict.environment_reason(dict(check, exit_code=None)))
        self.assertEqual(verdict.environment_reason({"command_not_found": True, "exit_code": None}), "command_not_found")
        self.assertIsNone(verdict.environment_reason({"command_not_found": True, "exit_code": 127, "timed_out": True}))



class TestCapturedEnvironmentSignatures(unittest.TestCase):
    def test_real_isolated_gradle_failure_fragments(self):
        import json
        captured = json.loads((Path(__file__).parent / "fixtures" / "verify_environment_signatures.json").read_text())
        self.assertEqual({item["environment_reason"] for item in captured}, {"jdk_version", "android_sdk_missing", "no_connected_device"})
        for record in captured:
            with self.subTest(name=record["name"]):
                self.assertEqual(record["exit_code"], 1)
                self.assertFalse(record["timed_out"])
                self.assertEqual(verdict.environment_reason(record), record["environment_reason"])
                self.assertIsNone(verdict.environment_reason(dict(record, exit_code=0)))
                self.assertIsNone(verdict.environment_reason(dict(record, timed_out=True)))
                self.assertEqual(record["source_head"], "a97165af685f48ff2214ec1d5047d110c6caa8fa")
