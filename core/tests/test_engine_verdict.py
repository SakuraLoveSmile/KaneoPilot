"""Tests for the engine verdict (A10): effective diff (.pi/** excluded) +
login-shell verify with project verify_env.

Run: ``cd core && python3 -m pytest tests/test_engine_verdict.py -q``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import db, util, round_store  # noqa: E402
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


class TestVerdictPure(unittest.TestCase):
    def setUp(self):
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _repo(self):
        root = _git_repo()
        self._dirs.append(root)
        return root

    def test_effective_changes_excludes_pi(self):
        repo = self._repo()
        (repo / "src.py").write_text("x", encoding="utf-8")
        (repo / ".pi").mkdir()
        (repo / ".pi" / "goal.md").write_text("note", encoding="utf-8")
        changes = verdict.effective_changes(str(repo))
        self.assertIn("src.py", changes)
        self.assertFalse(any(c.startswith(".pi/") for c in changes))

    def test_effective_changes_empty(self):
        self.assertEqual(verdict.effective_changes(str(self._repo())), [])

    def test_build_artifacts_are_not_effective_changes(self):
        repo = self._repo()
        (repo / "module.pyc").write_bytes(b"cache")
        self.assertEqual(verdict.effective_changes(repo), [])

    def test_run_command_exit_codes(self):
        repo = self._repo()
        self.assertEqual(verdict.run_command("exit 0", cwd=str(repo))["exit_code"], 0)
        self.assertEqual(verdict.run_command("exit 3", cwd=str(repo))["exit_code"], 3)

    def test_run_command_env_injection(self):
        repo = self._repo()
        result = verdict.run_command('printf %s "$KP_TEST_VAR"', cwd=str(repo),
                                     env={"KP_TEST_VAR": "hello"})
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["output_tail"])

    def test_run_command_timeout(self):
        repo = self._repo()
        result = verdict.run_command("sleep 5", cwd=str(repo), timeout=1)
        self.assertTrue(result["timed_out"])

    def test_timeout_kills_background_process_group_and_redacts_output(self):
        repo = self._repo()
        result = verdict.run_command("sleep 30 & echo $! > child.pyc; printf %s \"$KP_SECRET_TOKEN\"; wait",
                                     cwd=repo, timeout=0.1, env={"KP_SECRET_TOKEN": "private-secret-value"})
        self.assertTrue(result["timed_out"])
        self.assertNotIn("private-secret-value", result["output_tail"])
        pid = int((repo / "child.pyc").read_text())
        # A killed child can briefly remain a zombie; it must never run.
        status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True).stdout.decode()
        self.assertTrue(not status.strip() or status.strip().startswith("Z"), status)

    def test_verify_shell_default_and_override(self):
        # Default: $SHELL -l -c (A4 spike ③), fallback /bin/zsh; override = whole binary.
        saved = {k: os.environ.get(k) for k in ("KANPILOT_VERIFY_SHELL", "SHELL")}
        try:
            os.environ.pop("KANPILOT_VERIFY_SHELL", None)
            os.environ["SHELL"] = "/bin/zsh"
            self.assertEqual(verdict.verify_shell(), ["/bin/zsh", "-l", "-c"])
            os.environ.pop("SHELL", None)
            self.assertEqual(verdict.verify_shell(), ["/bin/zsh", "-l", "-c"])  # fallback
            os.environ["KANPILOT_VERIFY_SHELL"] = "/bin/sh"
            self.assertEqual(verdict.verify_shell(), ["/bin/sh", "-l", "-c"])   # override
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_login_shell_path_probes_or_none(self):
        result = verdict.login_shell_path()
        self.assertTrue(result is None or (isinstance(result, str) and result))

    def test_run_command_command_not_found(self):
        repo = self._repo()
        missing = verdict.run_command("this_command_does_not_exist_zzz", cwd=str(repo))
        self.assertTrue(missing["command_not_found"])          # rc=127 -> env failure
        self.assertFalse(verdict.run_command("exit 0", cwd=str(repo))["command_not_found"])
        self.assertFalse(verdict.run_command("exit 3", cwd=str(repo))["command_not_found"])

    def test_group_deadline_bounds_all_commands(self):
        repo = self._repo()
        started = time.monotonic()
        checks = verdict.run_verify_commands(["sleep 0.1", "sleep 3", "sleep 3"], cwd=repo,
                                             verify_env={"PATH": os.environ["PATH"]}, deadline=started + 0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(any(check["timed_out"] for check in checks))

    def test_evaluate_environment_failure_is_separate(self):
        repo = self._repo()
        (repo / "a.py").write_text("x", encoding="utf-8")
        env_fail = verdict.evaluate(["this_command_does_not_exist_zzz"], str(repo))
        self.assertFalse(env_fail["verification_ok"])
        self.assertTrue(env_fail["environment_failure"])        # ladder 2, not ladder 3
        quality_fail = verdict.evaluate(["false"], str(repo))
        self.assertFalse(quality_fail["verification_ok"])
        self.assertFalse(quality_fail["environment_failure"])   # rc=1 -> ladder 3

    def test_evaluate_pass_fail_empty_no_commands(self):
        repo = self._repo()
        (repo / "a.py").write_text("x", encoding="utf-8")
        passed = verdict.evaluate(["true"], str(repo))
        self.assertTrue(passed["verification_ok"])
        self.assertFalse(passed["empty_diff"])
        failed = verdict.evaluate(["false"], str(repo))
        self.assertFalse(failed["verification_ok"])
        self.assertFalse(failed["environment_failure"])
        # only a .pi change -> empty effective diff -> 空转
        pi_only = self._repo()
        (pi_only / ".pi").mkdir()
        (pi_only / ".pi" / "g.md").write_text("n", encoding="utf-8")
        empty = verdict.evaluate(["true"], str(pi_only))
        self.assertTrue(empty["empty_diff"])
        self.assertFalse(empty["verification_ok"])
        # real change but no declared verify commands
        noc = verdict.evaluate([], str(repo))
        self.assertFalse(noc["verification_ok"])


class TestEngineVerdictCommand(KanpilotTestCase):
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
                plan.write_text("# Frozen plan\n```verify\n" + "\n".join(commands) + "\n```\n")
                sha = util.sha256_file(plan)
                db.update_task(conn, tid, util.now_iso(), plan_sha256=sha)
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(),
                             plan_verify={"plan_sha256": task["plan_sha256"],
                                          "commands": commands})
                conn.execute("UPDATE tasks SET status='running', run_id=? WHERE task_id=?",
                             ("run-" + task_id, tid))
                round_store.upsert_on_claim(conn, tid, "run-" + task_id, {}, util.now_iso())
                round_store.mark_started(conn, tid, "run-" + task_id, util.now_iso(), worktree_path=str(worktree))
        finally:
            conn.close()
        view = self.task(tid)
        return tid, view["plan_sha256"]

    def test_verdict_pass_records_receipt(self):
        tid, sha = self._running_task_with_repo("taskvrdpass00000000001", commands=["true"])
        result = self.call("engine-verdict", {
            "task_id": tid, "expected_execution_run_id": "run-taskvrdpass00000000001",
            "expected_plan_sha256": sha})
        self.assertTrue(result["verification_ok"])
        self.assertFalse(result["empty_diff"])
        self.assertTrue(result["effective_changes"])
        receipt = db.task_extra(self._row(tid)).get("engine_verdict")
        self.assertTrue(receipt["verification_ok"])
        self.assertEqual(receipt["execution_run_id"], "run-taskvrdpass00000000001")

    def test_verdict_fail_on_verify(self):
        tid, sha = self._running_task_with_repo("taskvrdfail00000000001", commands=["false"])
        result = self.call("engine-verdict", {
            "task_id": tid, "expected_execution_run_id": "run-taskvrdfail00000000001",
            "expected_plan_sha256": sha})
        self.assertFalse(result["verification_ok"])
        self.assertFalse(result["empty_diff"])  # real change, but verify failed

    def test_verdict_empty_diff(self):
        tid, sha = self._running_task_with_repo("taskvrdempty0000000001",
                                                commands=["true"], change=False)
        result = self.call("engine-verdict", {
            "task_id": tid, "expected_execution_run_id": "run-taskvrdempty0000000001",
            "expected_plan_sha256": sha})
        self.assertTrue(result["empty_diff"])
        self.assertFalse(result["verification_ok"])

    def test_verdict_identity_conflict(self):
        tid, sha = self._running_task_with_repo("taskvrdidem00000000001", commands=["true"])
        exc = self.call_err("engine-verdict", {
            "task_id": tid, "expected_execution_run_id": "wrong-run",
            "expected_plan_sha256": sha})
        self.assertEqual(exc.code, "conflict")

    def test_verdict_never_falls_back_to_repository(self):
        tid, sha = self._running_task_with_repo("taskvrdmissing000001", commands=["true"])
        conn = db.connect(self.db_path)
        conn.execute("UPDATE execution_rounds SET worktree_path=NULL WHERE task_id=?", (tid,))
        conn.close()
        with patch.object(verdict, "run_verify_commands") as runner:
            result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                                  "expected_plan_sha256": sha})
        self.assertEqual(result["kind"], "unconfirmed")
        self.assertFalse(result["verification_ok"])
        runner.assert_not_called()

    def test_changed_plan_cannot_execute_cached_commands(self):
        tid, sha = self._running_task_with_repo("taskvrdplan000000001", commands=["true"])
        Path(self._row(tid)["plan_path"]).write_text("# changed\n```verify\nfalse\n```\n")
        with patch.object(verdict, "run_verify_commands") as runner:
            result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                                  "expected_plan_sha256": sha})
        self.assertEqual(result["kind"], "unconfirmed")
        runner.assert_not_called()

    def test_source_mutation_during_verify_is_not_a_pass(self):
        tid, sha = self._running_task_with_repo("taskvrdsource0000001", commands=["true"])
        def changed(commands, *, cwd, **kw):
            (Path(cwd) / "source.py").write_text("changed by verify")
            return [{"exit_code": 0, "timed_out": False}]
        with patch.object(verdict, "run_verify_commands", side_effect=changed):
            result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                                  "expected_plan_sha256": sha})
        self.assertEqual(result["kind"], "unconfirmed")
        self.assertFalse(result["verification_ok"])

    def test_total_budget_includes_source_binding(self):
        tid, sha = self._running_task_with_repo("taskvrdbudget000001", commands=["true"])
        original = verdict.git_delivery.source_state_sha256
        def exhausted(path, **kw):
            # The preparation reaches the deadline before any shell command.
            kw["deadline"] = time.monotonic() - 1
            return original(path, **kw)
        with patch.object(verdict.git_delivery, "source_state_sha256", side_effect=exhausted), patch.object(verdict, "run_verify_commands") as runner:
            result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                                  "expected_plan_sha256": sha, "budget_seconds": 1})
        self.assertEqual(result["kind"], "unconfirmed")
        runner.assert_not_called()

    def test_same_operation_reuses_bound_receipt(self):
        tid, sha = self._running_task_with_repo("taskvrdreuse00000001", commands=["true"])
        payload = {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                   "expected_plan_sha256": sha, "operation_id": "one-operation"}
        self.assertTrue(self.call("engine-verdict", payload)["verification_ok"])
        with patch.object(verdict, "run_verify_commands") as runner:
            self.assertTrue(self.call("engine-verdict", payload)["deduplicated"])
        runner.assert_not_called()

    def test_shadow_runs_in_copy_and_keeps_original_source(self):
        tid, sha = self._running_task_with_repo("taskvrdshadow000001", commands=["printf changed > shadow-source.py"])
        task = self._row(tid)
        self.call("set-project-mapping", {"project_id": task["project_id"], "repository": task["repository"], "enabled": True,
                                         "expected_revision": self.call("get-project-mappings")["revision"]})
        self.call("set-project-engine-mode", {"project_id": task["project_id"], "engine_mode": "shadow"})
        self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                     "expected_plan_sha256": sha})
        conn = db.connect(self.db_path)
        row = round_store.get_round(conn, tid, "run-" + tid)
        self.assertFalse((Path(row["worktree_path"]) / "shadow-source.py").exists())
        extra = db.task_extra(db.require_task(conn, tid))
        self.assertIn("engine_verdict_shadow", extra)
        self.assertNotIn("engine_verdict", extra)
        conn.close()

    def test_receipt_never_copies_raw_secret_commands(self):
        secret = "sensitive-private-token"
        tid, sha = self._running_task_with_repo("taskvrdredact000001", commands=["printf '%s' 'Authorization: Bearer " + secret + "'"])
        result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                             "expected_plan_sha256": sha})
        self.assertTrue(result["verification_ok"])
        self.assertNotIn(secret, util.jdump(result))
        self.assertNotIn(secret, util.jdump(db.task_extra(self._row(tid))["engine_verdict"]))

    def _row(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return db.require_task(conn, task_id)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
