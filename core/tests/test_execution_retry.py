"""Manual execution retry against disposable Git worktrees and fake Plus/Kaneo."""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier
from unittest.mock import patch

from kanpilot import (cli, cmd_dispatch, cmd_exec, cmd_queue, core, db, derive,
                      git_delivery, harness_plus_adapter, remote, round_store, util)
from test_session import HeadlessCase, pi_event, pi_job, write_json


class TestExecutionRetry(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id, self.run_id = "retry-task", "failed-run"
        fixture = self.make_ready_task(task_id=self.task_id)
        self.repo = Path(self.repos_root / "Feedback")
        self.git(self.repo, "init", "--initial-branch=main")
        self.git(self.repo, "config", "user.name", "Retry Test")
        self.git(self.repo, "config", "user.email", "retry@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        self.git(self.repo, "add", "tracked.txt")
        self.git(self.repo, "commit", "-m", "base")
        self.worktree = self.root / "original-execution"
        self.git(self.repo, "worktree", "add", "--detach", str(self.worktree), "HEAD")
        (self.worktree / "tracked.txt").write_text("partial implementation\n")
        (self.worktree / "new.txt").write_text("keep this untracked file\n")
        (self.repo / "primary.txt").write_text("preserve primary work\n")
        self.primary_state = self.git(self.repo, "status", "--porcelain")
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        now = self.ctx.now_iso
        db.update_task(self.ctx.conn, self.task_id, now, status="needs_user", stage="monitor",
                       failure_code="execution_failed", failure_stage="running", run_id=self.run_id,
                       sent=1, send_state="sent", sent_at=now, lock_retained=1,
                       session_marker=f"{self.task_id}/{self.run_id}", session_id="failed-session")
        core.update_automation(self.ctx.conn, self.task(), now, manual_takeover=True)
        # Legacy detached cases keep the old unversioned delivery contract.
        db.set_extra(self.ctx.conn, self.task(), now, delivery={"mode": "manual"})
        db.set_extra(self.ctx.conn, self.task(), now, plan_execution_hold=False)
        config = {"connection_file": str(self.root / "fake-plus.json")}
        round_store.upsert_on_claim(self.ctx.conn, self.task_id, self.run_id, config, now, backend="pi_desktop")
        round_store.mark_started(self.ctx.conn, self.task_id, self.run_id, now, worktree_path=str(self.worktree))
        self.job = pi_job(self.task_id, self.run_id, backend="pi_desktop", status="failed", exit_code=1,
                          pid=None, observe_pid=None, config=config, session_id="failed-session",
                          turn_id="failed-turn", send_state="sent", worktree_path=str(self.worktree))
        self.directory = self.make_run(self.task_id, self.run_id, job=self.job, events=[
            pi_event(1, "session", session_id="failed-session", turn_id="failed-turn"),
            pi_event(2, "terminal", outcome="failed", reason="NETWORK_ERROR"),
        ])
        self.plus_status = {"is_running": False, "current_turn_id": "failed-turn", "pending_permission": None}
        self.plus_turn = {"sessionId": "failed-session", "turnId": "failed-turn", "status": "error"}
        def status(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return dict(self.plus_status)
        def turn(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return dict(self.plus_turn)
        for name, value in (("plus_status", status), ("_get_client", lambda *a, **k:
                            (SimpleNamespace(turn_get=turn), {}))):
            patcher = patch.object(harness_plus_adapter, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Refresh the real status-only reader before snapshots in rejection tests.
        self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id, "status_only": True})
        self.payload = {"task_id": self.task_id, "action": "resume", "expected_execution_run_id": self.run_id}
        self.plan_sha = fixture["plan_path"] and self.task()["plan_sha256"]
        self.addCleanup(lambda: self.assertEqual(self.primary_state, self.git(self.repo, "status", "--porcelain")))

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                              env=git_delivery.git_env()).stdout.decode().strip()

    def task(self):
        return db.require_task(self.ctx.conn, self.task_id)

    def reject(self, payload=None):
        before = dict(self.task())
        count = self.ctx.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        with self.assertRaises(util.KanpilotError) as caught:
            cmd_queue.cmd_resolve(self.ctx, payload or self.payload)
        self.assertEqual(before, dict(self.task()))
        self.assertEqual(count, self.ctx.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        return caught.exception

    def test_versioned_manual_retry_preserves_named_branch_and_dirty_results(self):
        branch = "kaneo/retry-task"
        self.git(self.worktree, "switch", "-c", branch)
        head = self.git(self.worktree, "rev-parse", "HEAD")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, automation={}, delivery={
            "mode": "manual", "workspace_version": 1, "branch": branch,
            "worktree_path": str(self.worktree), "base_branch": "main", "base_sha": head, "head_sha": head})
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual(result["task_status"], "ready")
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "retry-test",
                                              "request_id": result["request"]["request_id"]})
        self.assertTrue(claimed["claimed"])
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"],
                                                  self.ctx.conn, require_isolated=True)
        self.assertEqual((path, mode), (self.worktree, "branch"))
        self.assertEqual(self.git(path, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertEqual((path / "tracked.txt").read_text(), "partial implementation\n")
        self.assertEqual((path / "new.txt").read_text(), "keep this untracked file\n")
        self.assertEqual(core.delivery_of(self.task())["head_sha"], head)

    def test_manual_takeover_retry_preserves_dirty_worktree_and_claims_new_round(self):
        self.assertIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        old_round = dict(round_store.get_round(self.ctx.conn, self.task_id, self.run_id))
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual("ready", result["task_status"])
        self.assertEqual("dispatch", result["request"]["kind"])
        self.assertIsNone(self.task()["run_id"])
        self.assertEqual(0, self.task()["sent"])
        self.assertIsNone(self.task()["send_state"])
        self.assertEqual(self.plan_sha, self.task()["plan_sha256"])
        self.assertFalse(core.is_automation_managed(self.task()))
        self.assertTrue(core.automation_of(self.task())["manual_takeover"])
        self.assertEqual(old_round, dict(round_store.get_round(self.ctx.conn, self.task_id, self.run_id)))
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "retry-test",
                                              "request_id": result["request"]["request_id"]})
        self.assertTrue(claimed["claimed"], claimed)
        self.assertNotEqual(self.run_id, claimed["run_id"])
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, claimed["run_id"], self.ctx.conn,
                                                  require_isolated=True)
        self.assertEqual(self.worktree, path)
        self.assertEqual("reuse", mode)
        self.assertIn("不要另建 worktree", cmd_dispatch._execution_instruction(self.task(), path, desktop=True))
        self.assertEqual("partial implementation\n", (path / "tracked.txt").read_text())
        self.assertEqual("keep this untracked file\n", (path / "new.txt").read_text())
        self.reject()

    def test_cli_cas_requires_current_failed_round_and_failure_kind(self):
        self.reject({"task_id": self.task_id, "action": "resume"})
        self.assertEqual("stale_run", self.reject(dict(self.payload, expected_execution_run_id="historical-run")).code)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, failure_code="delivery_commit_failed")
        self.assertIn("原因已变化", self.reject().message)

    def test_unknown_send_active_request_and_trashed_round_are_not_candidates(self):
        for fields in ({"send_state": "unknown"}, {"sent": 0}, {"failure_stage": "dispatch"}):
            original = dict(self.task())
            db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, **fields)
            self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
            self.reject()
            db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, **{k: original[k] for k in fields})
        request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "monitor", "active-monitor", task_id=self.task_id)
        self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        self.reject()
        db.update_request(self.ctx.conn, request["request_id"], self.ctx.now_iso, status="failed")
        self.ctx.conn.execute("UPDATE execution_rounds SET save_state='trashed' WHERE task_id=?", (self.task_id,))
        self.reject()

    def use_manual_continuation(self, status="aborted", ended="2026-10-17T02:00:00+00:00", running=False,
                                planning=None, proposals=None, **raw_fields):
        """Install a later Plus turn with the same evidence that control-status reads."""
        raw = {"sessionId": "failed-session", "currentTurnId": "adopted-turn", "isRunning": running,
               "pendingPermission": None, "pendingToolConfirmations": 0, "planningState": planning,
               "pendingPlanId": None}
        raw.update(raw_fields)
        self.plus_status = {"session_id": "failed-session", "is_running": running, "current_turn_id": "adopted-turn",
                            "pending_permission": None, "raw_status": raw}
        turns = {"failed-turn": {"session_id": "failed-session", "turn_id": "failed-turn", "status": "error",
                                 "ended_at": "2026-10-17T01:00:00+00:00"},
                 "adopted-turn": {"session_id": "failed-session", "turn_id": "adopted-turn", "status": status,
                                  "ended_at": ended}}
        patcher = patch.object(harness_plus_adapter, "plus_turn_get",
                               side_effect=lambda _file, *, session_id, turn_id, **_: dict(turns[turn_id]))
        patcher.start()
        self.addCleanup(patcher.stop)
        proposal_patcher = patch.object(harness_plus_adapter, "plus_proposals",
                                        return_value=proposals or {"proposals": [], "history": []})
        proposal_patcher.start()
        self.addCleanup(proposal_patcher.stop)

    def test_live_plus_running_or_pending_permission_blocks_retry(self):
        for update in ({"is_running": True}, {"pending_permission": True}):
            original = dict(self.plus_status)
            self.plus_status.update(update)
            self.assertIn("仍有运行或等待权限", self.reject().message)
            self.plus_status = original

    def test_original_turn_must_remain_settled_before_retry(self):
        for update in ({"status": "running"}, {"sessionId": "other-session"}, {"turnId": None}):
            original = dict(self.plus_turn)
            self.plus_turn.update(update)
            self.reject()
            self.plus_turn = original

    def test_unverifiable_manual_continuation_blocks_retry(self):
        self.plus_status = {"is_running": False, "current_turn_id": "adopted-turn", "pending_permission": None}
        self.assertIn("无法核验", self.reject().message)
        for kwargs in ({"status": "running"}, {"sessionId": "other-session"}, {"ended": "2026-10-17T00:30:00+00:00"}):
            self.use_manual_continuation(**kwargs)
            self.assertIn("无法核验", self.reject().message, kwargs)

    def test_running_or_waiting_manual_continuation_blocks_retry(self):
        self.use_manual_continuation(status="running", running=True)
        self.assertIn("仍有运行或等待权限", self.reject().message)
        self.use_manual_continuation(status="completed", planning="awaiting_approval", pendingPlanId="goal-1",
                                     proposals={"proposals": [{"kind": "goal", "sessionId": "failed-session",
                                                               "turnId": "adopted-turn", "id": "goal-1",
                                                               "status": "pending"}], "history": []})
        self.assertIn("仍在等待确认", self.reject().message)

    def test_settled_manual_continuation_allows_retry_and_claim(self):
        self.use_manual_continuation(status="aborted")
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual("ready", result["task_status"])
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "retry-test",
                                              "request_id": result["request"]["request_id"]})
        self.assertTrue(claimed["claimed"], claimed)

    def test_settled_completed_manual_continuation_allows_retry(self):
        self.use_manual_continuation(status="completed")
        self.assertEqual("ready", cmd_queue.cmd_resolve(self.ctx, self.payload)["task_status"])

    def test_expired_goal_contract_never_entered_execution_allows_retry(self):
        self.use_manual_continuation(status="completed", planning="awaiting_approval", pendingPlanId="goal-1",
                                     proposals={"proposals": [], "history": [{"kind": "goal",
                                                                             "sessionId": "failed-session",
                                                                             "turnId": "adopted-turn",
                                                                             "id": "goal-1", "status": "expired"}]})
        self.assertEqual("ready", cmd_queue.cmd_resolve(self.ctx, self.payload)["task_status"])

    def test_finished_round_with_later_turn_is_not_settled_evidence(self):
        self.job.update(status="finished", exit_code=0)
        write_json(self.directory / "job.json", self.job)
        self.write_events(self.task_id, self.run_id, [pi_event(1, "session", session_id="failed-session"),
                                                    pi_event(2, "terminal", outcome="completed")])
        self.use_manual_continuation(status="aborted")
        self.assertIn("不满足手动继续的核验条件", self.reject().message)

    def test_review_remediation_keeps_refusing_any_later_turn(self):
        self.use_manual_continuation(status="aborted")
        with self.assertRaises(util.KanpilotError) as caught:
            cmd_queue._require_execution_terminal(self.ctx, self.task(), self.run_id)
        self.assertIn("存在后续轮次", caught.exception.message)

    def test_running_observer_or_local_job_never_allows_duplicate_execution(self):
        for update in ({"status": "running", "started_at": self.ctx.now_iso}, {"observe_pid": os.getpid()}):
            original = dict(self.job)
            self.job.update(update)
            write_json(self.directory / "job.json", self.job)
            self.reject()
            self.job = original
            write_json(self.directory / "job.json", self.job)

    def test_changed_requirements_plan_or_remote_review_status_need_reconfirmation(self):
        for status in ("in-review", "done", "to-do"):
            self.fake.tasks[self.task_id]["status"] = status
            self.assertEqual("remote_conflict", self.reject().code)
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        self.fake.tasks[self.task_id]["description"] = "changed requirements"
        self.assertEqual("remote_conflict", self.reject().code)
        self.fake.tasks[self.task_id]["description"] = ""
        Path(self.task()["plan_path"]).write_text("changed plan")
        self.reject()

    def test_remote_io_race_with_new_request_or_task_state_does_not_queue_retry(self):
        original = remote.get_task
        def changed(client, task_id):
            result = original(client, task_id)
            db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, send_state="unknown")
            return result
        with patch.object(remote, "get_task", side_effect=changed), self.assertRaises(util.KanpilotError):
            cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual("unknown", self.task()["send_state"])
        self.assertIsNone(self.ctx.conn.execute("SELECT 1 FROM requests WHERE kind='dispatch' AND status='queued'").fetchone())

    def test_post_queue_worktree_mutation_or_plus_revival_blocks_claim(self):
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        (self.worktree / "new.txt").write_text("changed after confirmation")
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "retry-test",
                                              "request_id": result["request"]["request_id"]})
        self.assertFalse(claimed["claimed"])
        self.assertIn("工作树内容已变化", claimed["detail"])

    def test_plus_revival_after_queue_blocks_claim_and_send(self):
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.plus_status["is_running"] = True
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "retry-test",
                                              "request_id": result["request"]["request_id"]})
        self.assertFalse(claimed["claimed"])
        self.assertIn("仍有运行", claimed["detail"])

    def test_plus_revival_after_claim_blocks_send_before_remote_mutation(self):
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        claimed = cmd_exec.cmd_claim(self.ctx, {"stage": "dispatch", "executor": "retry-test",
                                              "request_id": result["request"]["request_id"]})
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, status="prepared")
        self.plus_status["is_running"] = True
        with patch.object(remote, "update_status", side_effect=AssertionError("must not write remote")):
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch.cmd_dispatch_begin_send(self.ctx, {"task_id": self.task_id, "run_id": claimed["run_id"]})
        self.assertEqual("prepared", self.task()["status"])
        self.assertEqual("not_sent", self.task()["send_state"])

    def test_double_click_concurrent_resolves_create_exactly_one_dispatch(self):
        barrier = Barrier(2)
        original = remote.get_task
        def simultaneous(client, task_id):
            result = original(client, task_id)
            barrier.wait(timeout=5)
            return result
        def resolve():
            try:
                return self.call("resolve", dict(self.payload))
            except util.KanpilotError as exc:
                return exc
        with patch.object(remote, "get_task", side_effect=simultaneous), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resolve(), range(2)))
        self.assertEqual(1, sum(isinstance(result, dict) and result["ok"] for result in results), results)
        self.assertEqual(1, sum(isinstance(result, util.KanpilotError) for result in results), results)
        self.assertEqual(1, self.ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='dispatch' AND status='queued'").fetchone()[0])

    def test_terminal_file_changes_during_remote_read_do_not_queue_retry(self):
        original = remote.get_task
        def changed(client, task_id):
            result = original(client, task_id)
            self.job["status"] = "running"
            write_json(self.directory / "job.json", self.job)
            return result
        with patch.object(remote, "get_task", side_effect=changed):
            self.reject()

    def test_primary_checkout_and_missing_old_workspace_never_substitute_for_original(self):
        self.ctx.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=?",
                              (str(self.repo), self.task_id))
        self.reject()
        self.ctx.conn.execute("UPDATE execution_rounds SET worktree_path=NULL WHERE task_id=?", (self.task_id,))
        self.reject()

    def test_completed_but_task_failed_can_retry_when_plus_has_settled(self):
        self.job.update(status="finished", exit_code=0)
        write_json(self.directory / "job.json", self.job)
        self.write_events(self.task_id, self.run_id, [pi_event(1, "session", session_id="failed-session"),
                                                    pi_event(2, "terminal", outcome="completed")])
        self.plus_turn["status"] = "completed"
        self.assertEqual("ready", cmd_queue.cmd_resolve(self.ctx, self.payload)["task_status"])

    def test_managed_pr_retry_preserves_branch_changes(self):
        self.git(self.worktree, "switch", "-c", "kaneo/retry-task")
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso, manual_takeover=False)
        core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, mode="pr", branch="kaneo/retry-task",
                             worktree_path=str(self.worktree), head_sha=self.git(self.worktree, "rev-parse", "HEAD"))
        self.assertEqual("ready", cmd_queue.cmd_resolve(self.ctx, self.payload)["task_status"])
        self.assertTrue(core.is_automation_managed(self.task()))
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, "new-run", self.ctx.conn)
        self.assertEqual((self.worktree, "branch"), (path, mode))

    def test_verification_retry_preserves_original_worktree_and_records_explicit_action(self):
        for code in ("verification_unconfirmed", "verification_environment_failure"):
            db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                           failure_code=code, failure_stage="monitor")
            self.assertIn("retry_verification", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        payload = dict(self.payload, action="retry_verification")
        result = cmd_queue.cmd_resolve(self.ctx, payload)
        self.assertEqual(result["request"]["kind"], "dispatch")
        self.assertEqual(core.execution_retry_of(self.task())["worktree"]["worktree_path"], str(self.worktree))
        self.assertEqual(self.task()["plan_sha256"], self.plan_sha)
        self.assertEqual(util.jload(self.task()["resolution"])["action"], "retry_verification")
        self.assertEqual((self.worktree / "new.txt").read_text(), "keep this untracked file\n")

    def test_verification_retry_rejects_unknown_identity_and_plus_running(self):
        payload = dict(self.payload, action="retry_verification")
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       failure_code="verification_identity_unconfirmed", failure_stage="monitor")
        self.assertNotIn("retry_verification", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        self.reject(payload)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, failure_code="verification_unconfirmed")
        self.plus_status["is_running"] = True
        self.reject(payload)
        self.plus_status["is_running"] = False
        self.reject(dict(payload, expected_execution_run_id="stale"))
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, send_state="unknown")
        self.assertNotIn("retry_verification", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        self.reject(payload)


    def test_verification_retry_requires_explicit_user_action(self):
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       failure_code="verification_unconfirmed", failure_stage="monitor")
        self.reject(dict(self.payload, action="retry_verification", actor="automation"))
        self.reject(dict(self.payload, verification_retry=True))
