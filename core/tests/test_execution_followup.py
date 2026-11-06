"""A user can correct one stopped report without rewriting evidence or restarting it."""
import copy
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from kanpilot import (cli, cmd_queue, codex_execution, db, derive, execution_followup,
                      git_delivery, process_owner, remote, round_store, util)
from test_codex_preflight_retry import seed_preflight
from test_session import HeadlessCase, pi_event, write_json, write_jsonl


class ExecutionFollowupTests(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.make_ready_task(task_id="t")
        self.repo = self.repos_root / "Feedback"
        for args in (("init", "--initial-branch=main"), ("config", "user.name", "Test"),
                     ("config", "user.email", "test@example.invalid")):
            self.git(*args)
        (self.repo / "source.txt").write_text("base\n")
        self.git("add", "."); self.git("commit", "-m", "initial")
        self.worktree = self.root / "execution"
        self.git("worktree", "add", "--detach", str(self.worktree), "HEAD")
        (self.worktree / "source.txt").write_text("implementation remains\n")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        db.update_task(self.ctx.conn, "t", self.ctx.now_iso, run_id="r")
        self.directory = self.runs_root / "t" / "r"
        self.spec, self.job = seed_preflight(self.ctx.conn, self.task(), self.directory,
                                            self.worktree, self.ctx.now_iso)
        self.session, self.turn_id = "session", "codex:r:1"
        source = git_delivery.source_state_sha256(self.worktree)
        self.report = {"status": "failed", "summary": "缺少隔离设备与授权 Emby 会话，未完成真实播放验收",
                       "checks": [{"command": "python3 -V", "exit_code": 0}], "questions": []}
        self.native = [{"type": "thread.started", "thread_id": self.session},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "check", "type": "command_execution",
                                              "command": "python3 -V", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "check", "type": "command_execution",
                "command": "python3 -V", "status": "completed", "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": ""}},
            {"type": "turn.completed"}]
        self.job.update(status="failed", send_state="sent", session_id=self.session, turn_id=self.turn_id,
                        detail="Codex 报告执行失败\n" + self.report["summary"], pending_input=None,
                        failure_report={"source_sha256": source, "summary": self.report["summary"], "checks": []})
        self.job["turns"] = [{"turn_id": self.turn_id, "session_id": self.session, "source": "codex_adapter",
            "status": "completed", "exit_code": 0, "pid": 99999997,
            "birth": {"started_seconds": 0, "started_microseconds": 0}, "cli_terminal": True,
            "ended_at": self.ctx.now_iso, "events_file": "codex-turn-1.jsonl", "output_file": "codex-final-1.json"}]
        db.update_task(self.ctx.conn, "t", self.ctx.now_iso, status="needs_user", stage="monitor",
                       failure_code="execution_failed", failure_stage="running", sent=1, send_state="sent",
                       session_id=self.session, failure_reason=self.job["detail"], blocked_summary=self.job["detail"],
                       blocked_at=self.ctx.now_iso)
        self.ctx.conn.execute("UPDATE execution_rounds SET session_id=?,run_status='failed' WHERE task_id='t' AND run_id='r'",
                              (self.session,))
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, plan_execution_hold=False)
        self.persist()
        write_jsonl(self.directory / "events.jsonl", [pi_event(1, "session", session_id=self.session, turn_id=self.turn_id),
            pi_event(2, "terminal", outcome="failed", reason=self.job["detail"])])
        self.payload = {"task_id": "t", "action": "await_input", "expected_execution_run_id": "r",
                        "note": "待补充隔离测试设备及授权 Emby 测试会话，然后完成原生播放验收。"}
        self.bind_job()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                              env=git_delivery.git_env()).stdout.decode().strip()

    def task(self):
        return db.require_task(self.ctx.conn, "t")

    def bind_job(self):
        self.payload["expected_job_sha256"] = util.sha256_file(self.directory / "job.json")

    def persist(self):
        self.native[-2]["item"]["text"] = util.jdump(self.report)
        turn = self.job["turns"][-1]
        source = self.job["failure_report"]["source_sha256"]
        native_path = self.directory / turn["events_file"]
        native_path.write_text("".join(util.jdump({"seq": index, "turn_id": self.turn_id, "event": event,
                    "source_sha256": source}) + "\n" for index, event in enumerate(self.native, 1)))
        output_path = self.directory / turn["output_file"]
        write_json(output_path, self.report)
        turn.update(native_sha256=util.sha256_file(native_path), output_sha256=util.sha256_file(output_path))
        write_json(self.directory / "job.json", self.job)

    def snapshot(self):
        return {"task": dict(self.task()), "round": dict(round_store.get_round(self.ctx.conn, "t", "r")),
                "requests": [dict(row) for row in self.ctx.conn.execute("SELECT * FROM requests ORDER BY rowid")],
                "events": [dict(row) for row in self.ctx.conn.execute("SELECT * FROM events ORDER BY id")]}

    def reject(self, payload=None):
        before = self.snapshot()
        with self.assertRaises(util.KanpilotError) as caught:
            self.call("resolve", payload or self.payload)
        self.assertEqual(before, self.snapshot())
        return caught.exception

    def test_correction_is_blocked_preserves_original_evidence_and_is_idempotent(self):
        before = self.snapshot()
        evidence = {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no model or command")), \
             mock.patch.object(remote, "get_task", side_effect=AssertionError("no remote")):
            result = self.call("resolve", self.payload)
        self.assertIsNone(result["request"])
        view = derive.task_summary(self.ctx.conn, self.task(), self.ctx.now)
        self.assertEqual(view["exception"]["severity"], "blocked")
        self.assertEqual(view["exception"]["label"], "待补充材料或决定")
        self.assertEqual(view["exception"]["detail"], self.payload["note"])
        self.assertEqual((self.task()["status"], self.task()["stage"]), ("needs_user", "monitor"))
        self.assertIsNone(view.get("execution_question"))
        self.assertNotIn("answer_execution_questions", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        self.assertEqual(before["round"], self.snapshot()["round"])
        self.assertEqual(before["requests"], self.snapshot()["requests"])
        self.assertEqual(evidence, {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()})
        correction = util.jload(self.ctx.conn.execute("SELECT detail_json FROM events WHERE kind='execution_classification_corrected'").fetchone()[0])
        self.assertEqual(correction["previous"]["failure_reason"], before["task"]["failure_reason"])
        self.assertEqual(correction["previous"]["blocked_at"], before["task"]["blocked_at"])
        self.assertEqual(correction["job_sha256"], self.payload["expected_job_sha256"])
        changed = self.snapshot()
        self.assertTrue(self.call("resolve", self.payload)["deduplicated"])
        self.assertEqual(changed, self.snapshot())
        self.assertTrue(cmd_queue.execution_retry_candidate(self.ctx.conn, self.task()))

    def test_normal_read_does_not_automatically_reclassify_failed_report(self):
        before = self.snapshot()
        view = derive.task_summary(self.ctx.conn, self.task(), self.ctx.now)
        self.assertEqual(view["exception"]["severity"], "failed")
        self.assertEqual(before, self.snapshot())

    def test_stale_run_hash_actor_or_missing_reason_is_rejected(self):
        for values in ({"expected_execution_run_id": "old"}, {"expected_job_sha256": "0" * 64},
                       {"expected_job_sha256": ""}, {"actor": "automation"}, {"note": " "}, {"note": "x" * 4097}):
            with self.subTest(values=values):
                self.reject(dict(self.payload, **values))

    def test_active_request_live_process_unknown_send_or_other_backend_is_rejected(self):
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "monitor", "active", task_id="t")
        self.reject()
        db.update_request(self.ctx.conn, request["request_id"], self.ctx.now_iso, status="failed")
        for fields in ({"send_state": "unknown"}, {"sent": 0}, {"failure_stage": "dispatch"},
                       {"failure_code": "review_blocked"}, {"save_state": "trashed"}):
            original = dict(self.task())
            db.update_task(self.ctx.conn, "t", self.ctx.now_iso, **fields)
            self.reject()
            db.update_task(self.ctx.conn, "t", self.ctx.now_iso, **{key: original[key] for key in fields})
        self.ctx.conn.execute("UPDATE execution_rounds SET backend='pi_desktop' WHERE task_id='t'")
        self.reject()
        self.ctx.conn.execute("UPDATE execution_rounds SET backend='codex' WHERE task_id='t'")
        self.job.update(pid=os.getpid(), worker_birth=process_owner.process_birth(os.getpid()))
        self.persist(); self.bind_job()
        self.ctx.conn.execute("UPDATE execution_rounds SET pid=? WHERE task_id='t'", (os.getpid(),))
        self.reject()

    def test_nonzero_missing_or_unmatched_checks_and_missing_report_are_rejected(self):
        original = copy.deepcopy(self.report)
        for checks in ([], [{"command": "python3 -V", "exit_code": 7}],
                       [{"command": "different", "exit_code": 0}],
                       original["checks"] + original["checks"]):
            with self.subTest(checks=checks):
                self.report = dict(original, checks=checks)
                self.persist(); self.bind_job(); self.reject()
        self.report = original
        self.persist(); self.bind_job()
        self.native[3]["item"]["exit_code"] = 1
        self.persist(); self.bind_job(); self.reject()
        self.native[3]["item"]["exit_code"] = 0
        self.persist(); self.bind_job()
        (self.directory / self.job["turns"][-1]["output_file"]).unlink()
        self.reject()

    def test_earlier_failure_and_later_tools_do_not_turn_classification_into_a_success_verdict(self):
        earlier = [copy.deepcopy(self.native[2]), copy.deepcopy(self.native[3])]
        for event in earlier:
            event["item"]["id"] = "earlier-check"
        earlier[1]["item"]["exit_code"] = 1
        self.native[2:2] = earlier
        self.native[-2:-2] = [
            {"type": "item.started", "item": {"id": "status", "type": "command_execution",
                "command": "git status --short", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "status", "type": "command_execution",
                "command": "git status --short", "status": "completed", "exit_code": 0}}]
        self.persist(); self.bind_job()
        observed = codex_execution._native_checks(self.directory, self.job["turns"][-1], {},
                                                 commands=self.spec["required_verification_commands"])
        self.assertTrue(all(check["exit_code"] is None for check in observed))
        self.call("resolve", self.payload)
        self.assertEqual(self.task()["failure_code"], execution_followup.CODE)
        self.assertFalse((self.directory / codex_execution.REPORT_NAME).exists())
        with self.assertRaises(util.KanpilotError):
            codex_execution.require_terminal(self.job)

    def test_last_native_call_needs_its_start_integer_zero_and_same_final_source(self):
        original = copy.deepcopy(self.native)
        for mutate in (lambda: self.native.pop(2),
                       lambda: self.native[3]["item"].update(exit_code=True),
                       lambda: self.native[3]["item"].update(exit_code=9)):
            self.native = copy.deepcopy(original)
            mutate(); self.persist(); self.bind_job(); self.reject()
        self.native = original
        self.persist()
        path = self.directory / self.job["turns"][-1]["events_file"]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[2]["source_sha256"] = "0" * 64
        write_jsonl(path, rows)
        self.job["turns"][-1]["native_sha256"] = util.sha256_file(path)
        write_json(self.directory / "job.json", self.job)
        self.bind_job(); self.reject()

    def test_failed_identity_and_changed_evidence_during_write_are_rejected(self):
        original = copy.deepcopy(self.job)
        for fields in ({"session_id": "wrong"}, {"scope_id": "wrong"}, {"spec_sha256": "0" * 64}, {"status": "running"},
                       {"cli_terminal": False}, {"worker_birth": None}):
            with self.subTest(fields=fields):
                self.job = dict(copy.deepcopy(original), **fields)
                self.persist(); self.bind_job(); self.reject()
        self.job = original
        self.persist(); self.bind_job()
        original_unchanged = execution_followup._unchanged
        calls = []
        def mutate(hashes):
            calls.append(True)
            if len(calls) == 2:
                (self.directory / "codex-final-1.json").write_text("changed")
            return original_unchanged(hashes)
        with mock.patch.object(execution_followup, "_unchanged", side_effect=mutate):
            self.reject()

    def test_final_message_mismatch_and_request_arriving_during_proof_are_rejected(self):
        self.native[-2]["item"]["text"] = util.jdump(dict(self.report, summary="different message"))
        path = self.directory / self.job["turns"][-1]["events_file"]
        source = self.job["failure_report"]["source_sha256"]
        write_jsonl(path, [{"seq": index, "turn_id": self.turn_id, "event": event, "source_sha256": source}
                          for index, event in enumerate(self.native, 1)])
        self.job["turns"][-1]["native_sha256"] = util.sha256_file(path)
        write_json(self.directory / "job.json", self.job)
        self.bind_job(); self.reject()
        self.persist(); self.bind_job()
        before_task = dict(self.task())
        original_proof = execution_followup._proof
        def queued(*args):
            proof = original_proof(*args)
            db.create_request(self.ctx.conn, self.ctx.now_iso, "monitor", "concurrent", task_id="t")
            return proof
        with mock.patch.object(execution_followup, "_proof", side_effect=queued), self.assertRaises(util.KanpilotError):
            self.call("resolve", self.payload)
        self.assertEqual(before_task, dict(self.task()))
        self.assertIsNotNone(self.ctx.conn.execute("SELECT 1 FROM requests WHERE idempotency_key='concurrent'").fetchone())
        self.assertIsNone(self.ctx.conn.execute("SELECT 1 FROM events WHERE kind='execution_classification_corrected'").fetchone())

    def test_followup_resume_requires_exact_old_round_and_existing_retry_guards(self):
        self.call("resolve", self.payload)
        self.reject({"task_id": "t", "action": "resume", "expected_execution_run_id": "old"})
        with mock.patch.object(cmd_queue, "_execution_retry_materials", side_effect=util.KanpilotError("conflict", "still gated")) as prepare:
            error = self.reject({"task_id": "t", "action": "resume", "expected_execution_run_id": "r"})
        self.assertIn("still gated", error.message)
        prepare.assert_called_once()
        self.assertEqual(self.task()["failure_code"], execution_followup.CODE)

    def test_followup_automation_cannot_resume_or_prepare_retry_materials(self):
        self.call("resolve", self.payload)
        with mock.patch.object(cmd_queue, "_execution_retry_materials") as prepare, \
             mock.patch.object(remote, "get_task") as read_remote:
            error = self.reject({"task_id": "t", "action": "resume",
                                 "expected_execution_run_id": "r", "actor": "automation"})
        self.assertIn("仅允许用户", error.message)
        prepare.assert_not_called()
        read_remote.assert_not_called()
