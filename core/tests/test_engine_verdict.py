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
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import db, util  # noqa: E402
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
        if change:
            (repo / f"change-{task_id}.py").write_text("x = 1\n", encoding="utf-8")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(),
                             plan_verify={"plan_sha256": task["plan_sha256"],
                                          "commands": commands})
                conn.execute("UPDATE tasks SET status='running', run_id=? WHERE task_id=?",
                             ("run-" + task_id, tid))
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

    def _row(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return db.require_task(conn, task_id)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
