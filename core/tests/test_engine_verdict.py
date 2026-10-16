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

    def test_short_sensitive_env_redacts_command_detail_and_output_but_keeps_ready_flag(self):
        environment = {"PASSWORD": "123456", "TOKEN": "abcdef", "SECRET": "uvwxyz",
                       "API_KEY": "987654", "AUTHORIZATION": "auth56", "KP_VERIFY_READY": "1"}
        result = verdict.run_command('printf "%4093s" ""; printf "%s|%s|%s|%s|%s|ready=%s" '
                                     '"$PASSWORD" "$TOKEN" "$SECRET" "$API_KEY" "$AUTHORIZATION" "$KP_VERIFY_READY"',
                                     cwd=self._repo(), env=environment)
        for key, value in environment.items():
            if key != "KP_VERIFY_READY":
                self.assertNotIn(value, util.jdump(result), key)
        self.assertIn("ready=1", result["output_tail"])
        with patch.object(verdict.subprocess, "Popen", side_effect=OSError("123456")):
            failed = verdict.run_command("printf 123456", cwd=None, env=environment)
        self.assertNotIn("123456", util.jdump(failed))

    def test_sensitive_environment_values_have_no_minimum_length(self):
        for value in ("a", "1", "pw"):
            with self.subTest(value=value):
                result = verdict.run_command('printf %s "$PASSWORD"', cwd=None, env={"PASSWORD": value})
                self.assertEqual(result["output_tail"], "«redacted»")
        with patch.dict(os.environ, {"TOKEN": "short"}):
            result = verdict.run_command('printf %s "$TOKEN"', cwd=None)
        self.assertEqual(result["output_tail"], "«redacted»")

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

    def test_live_and_shadow_receipts_redact_short_password_values(self):
        for mode in ("live", "shadow"):
            with self.subTest(mode=mode):
                tid, sha = self._running_task_with_repo("shortsecret" + mode, commands=['printf "%s|ready=%s" "$PASSWORD" "$KP_VERIFY_READY"'])
                task = self._row(tid)
                self.call("set-project-mapping", {"project_id": task["project_id"], "repository": task["repository"], "enabled": True,
                                                 "expected_revision": self.call("get-project-mappings")["revision"]})
                self.call("set-project-engine-mode", {"project_id": task["project_id"], "engine_mode": mode})
                with patch.object(verdict, "project_verify_env", return_value={"PASSWORD": "123456", "KP_VERIFY_READY": "1"}):
                    result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                                         "expected_plan_sha256": sha})
                self.assertTrue(result["verification_ok"])
                key = "engine_verdict_shadow" if mode == "shadow" else "engine_verdict"
                receipt = db.task_extra(self._row(tid))[key]
                self.assertNotIn("123456", util.jdump(receipt))
                self.assertIn("ready=1", receipt["checks"][0]["output_tail"])
                conn = db.connect(self.db_path)
                try:
                    for row in conn.execute("SELECT detail_json FROM events WHERE task_id=?", (tid,)):
                        self.assertNotIn("123456", row[0] or "")
                finally:
                    conn.close()

    def test_live_and_shadow_classify_raw_failure_before_redacting_receipts(self):
        for mode in ("live", "shadow"):
            with self.subTest(mode=mode):
                tid, sha = self._running_task_with_repo("rawclassify" + mode,
                    commands=["printf 'SDK location not found.\\nsecret=%s' \"$PASSWORD\"; exit 1"])
                task = self._row(tid)
                self.call("set-project-mapping", {"project_id": task["project_id"], "repository": task["repository"], "enabled": True,
                                                 "expected_revision": self.call("get-project-mappings")["revision"]})
                self.call("set-project-engine-mode", {"project_id": task["project_id"], "engine_mode": mode})
                with patch.object(verdict, "project_verify_env", return_value={"PASSWORD": "a"}):
                    result = self.call("engine-verdict", {"task_id": tid, "expected_execution_run_id": "run-" + tid,
                                                         "expected_plan_sha256": sha})
                self.assertEqual(result["kind"], "environment_failure")
                self.assertTrue(result["environment_failure"])
                self.assertEqual(result["environment_reasons"], ["android_sdk_missing"])
                key = "engine_verdict_shadow" if mode == "shadow" else "engine_verdict"
                receipt = db.task_extra(self._row(tid))[key]
                self.assertEqual(receipt["checks"][0]["output_tail"], "SDK loc«redacted»tion not found.\nsecret=«redacted»")
                self.assertEqual(verdict.environment_reasons(receipt["checks"]), ["android_sdk_missing"])
                self.assertNotIn("secret=a", util.jdump(receipt))
                conn = db.connect(self.db_path)
                try:
                    for row in conn.execute("SELECT detail_json FROM events WHERE task_id=?", (tid,)):
                        self.assertNotIn("secret=a", row[0] or "")
                finally:
                    conn.close()

    def _row(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return db.require_task(conn, task_id)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


class TestEnvironmentClassification(unittest.TestCase):
    def test_sensitive_value_redaction_cannot_erase_environment_failure_classification(self):
        cases = [("SDK location not found.", "a", "android_sdk_missing"),
                 ("Dependency requires at least JVM runtime version 17", "a", "jdk_version"),
                 ("No connected devices!", "o", "no_connected_device"),
                 ("command not found: gradle", "o", "command_not_found")]
        for output, password, reason in cases:
            with self.subTest(reason=reason):
                command = "printf '%s' '" + output + "'; exit 1"
                check = verdict.run_command(command, cwd=None, env={"PASSWORD": password})
                self.assertEqual(check["environment_reason"], reason)
                self.assertEqual(verdict.environment_reasons([check]), [reason])
                self.assertIn("«redacted»", check["output_tail"])
                self.assertNotIn(output, check["output_tail"])
                self.assertIsNone(verdict.environment_reason(dict(check, exit_code=0)))
                self.assertIsNone(verdict.environment_reason(dict(check, timed_out=True)))
        success = verdict.run_command("printf 'SDK location not found.'", cwd=None, env={"PASSWORD": "a"})
        self.assertIsNone(success["environment_reason"])
        timeout = verdict.run_command("printf 'SDK location not found.'; sleep 5", cwd=None,
                                      timeout=0.05, env={"PASSWORD": "a"})
        self.assertTrue(timeout["timed_out"])
        self.assertIsNone(timeout["environment_reason"])

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

    def test_real_shell_nonzero_success_timeout_and_failed_launch(self):
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as root:
            failed = verdict.run_command("printf 'SDK location not found'; exit 1", cwd=root)
            self.assertEqual(failed["environment_reason"], "android_sdk_missing")
            self.assertIsNone(verdict.run_command("printf 'SDK location not found'; exit 0", cwd=root)["environment_reason"])
            timed = verdict.run_command("printf 'This build uses a Java 8 JVM'; sleep 5", cwd=root, timeout=0.05)
            self.assertTrue(timed["timed_out"])
            self.assertIsNone(timed["environment_reason"])
            with patch.object(verdict, "verify_shell", return_value=["/missing/verify-shell", "-l", "-c"]):
                launch = verdict.run_command("true", cwd=root)
            self.assertTrue(launch["command_not_found"])
            self.assertEqual(launch["environment_reason"], "command_not_found")

    def test_group_environment_wins_and_reasons_deduplicate_in_order(self):
        from unittest.mock import patch
        checks = [{"exit_code": 1, "timed_out": False, "output_tail": "test assertion failed"},
                  {"exit_code": 1, "timed_out": False, "output_tail": "SDK location not found"},
                  {"exit_code": 1, "timed_out": False, "output_tail": "SDK location not found"},
                  {"exit_code": 1, "timed_out": False, "output_tail": "No connected devices"}]
        with patch.object(verdict, "effective_changes", return_value=["source.py"]), patch.object(verdict, "run_verify_commands", return_value=checks):
            result = verdict.evaluate(["test", "sdk", "sdk", "device"], "/unused")
        self.assertTrue(result["environment_failure"])
        self.assertEqual(result["environment_reasons"], ["android_sdk_missing", "no_connected_device"])
        self.assertEqual(result["kind"], "environment_failure")
        self.assertIn("ANDROID_HOME", result["detail"])
        with patch.object(verdict, "login_shell_path", return_value="/bin"):
            import time
            exhausted = verdict.run_verify_commands(["false"], cwd="/unused", deadline=time.monotonic()-1)
        self.assertIsNone(exhausted[0]["environment_reason"])


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
