"""Read-only recovery: isolated MCP fixtures and temporary task data only."""
import copy
import json
from pathlib import Path
from unittest import mock

import test_pi_desktop_backend as backend
from test_pi_desktop_backend import PiDesktopCase, SESSION_ID, TURN_ID, FOLLOW_UP_TURN_ID
from kanpilot import dsh_events, pi_desktop_worker as worker, runtime_client, util


class RecoveryCase(PiDesktopCase):
    _base = backend.TestRunReal._base

    def recovery_scene(self, *, appended=False, current=FOLLOW_UP_TURN_ID):
        spec, run_dir = self._base()
        job = self.write_lost_observer_scene(spec, run_dir, detail="turn aborted: TURN_ABORTED")
        scope = runtime_client.stable_scope_id("execution", task_id=spec["task_id"], run_id=spec["run_id"])
        job["scope_id"] = scope
        (run_dir / dsh_events.JOB_NAME).write_text(json.dumps(job), encoding="utf-8")
        old = {"sessionId": SESSION_ID, "turnId": TURN_ID, "status": "aborted",
               "errorCode": "TURN_ABORTED", "startedAt": "2026-10-16T15:00:00Z",
               "endedAt": "2026-10-16T15:58:00Z"}
        new = {"sessionId": SESSION_ID, "turnId": FOLLOW_UP_TURN_ID, "status": "completed",
               "startedAt": "2026-10-16T16:00:00Z", "endedAt": "2026-10-16T16:24:00Z"}
        messages = [{"id": "initial", "role": "user", "content": self.expected_prompt(spec),
                     "turnId": TURN_ID, "timestamp": old["startedAt"]}]
        if appended:
            messages.append({"id": "continue", "role": "user", "content": "继续当前任务",
                             "turnId": FOLLOW_UP_TURN_ID, "timestamp": new["startedAt"]})
        messages.append({"id": "final", "role": "assistant", "content": "完成验证",
                         "turnId": FOLLOW_UP_TURN_ID, "timestamp": new["endedAt"]})
        client = mock.Mock(scope_id=scope)
        client.agent_status.return_value = {"sessionId": SESSION_ID, "isRunning": False,
                                            "currentTurnId": current, "planningState": "inactive"}
        client.turn_get.side_effect = lambda sid, tid, **kw: copy.deepcopy(old if tid == TURN_ID else new)
        client.session_get.return_value = {"session": {"id": SESSION_ID, "messages": messages,
            "messageStart": 0, "messageEnd": len(messages), "hasMoreBefore": False}}
        return spec, job, client, old, new, messages


class TestRecoveryIdentity(RecoveryCase):
    def test_completed_follow_up_is_read_only_and_bound(self):
        spec, job, client, *_ = self.recovery_scene()
        result = worker.prepare_turn_recovery(spec, job, client)
        self.assertEqual(result["turn_id"], FOLLOW_UP_TURN_ID)
        self.assertEqual(result["original_turn_id"], TURN_ID)
        self.assertEqual(result["scope_id"], job["scope_id"])
        self.assertFalse(client.agent_prompt.called)
        self.assertFalse(client.plans_resolve.called)

    def test_appended_instruction_requires_explicit_turn_binding(self):
        spec, job, client, *_ = self.recovery_scene(appended=True)
        with self.assertRaises(util.KanpilotError):
            worker.prepare_turn_recovery(spec, job, client)
        result = worker.prepare_turn_recovery(spec, job, client, turn_id=FOLLOW_UP_TURN_ID)
        self.assertEqual(result["turn_id"], FOLLOW_UP_TURN_ID)

    def test_no_different_candidate_returns_none(self):
        spec, job, client, *_ = self.recovery_scene(current=TURN_ID)
        self.assertIsNone(worker.prepare_turn_recovery(spec, job, client))

    def test_running_follow_up_is_bound_without_prompt_or_approval(self):
        spec, job, client, old, new, messages = self.recovery_scene()
        new.update(status="running", endedAt=None)
        client.agent_status.return_value["isRunning"] = True
        result = worker.prepare_turn_recovery(spec, job, client)
        self.assertEqual(result["turn_id"], FOLLOW_UP_TURN_ID)
        self.assertFalse(client.agent_prompt.called)
        self.assertFalse(client.plans_resolve.called)

    def test_host_without_started_at_uses_identified_transcript_messages(self):
        spec, job, client, old, new, messages = self.recovery_scene()
        old.pop("startedAt")
        new.pop("startedAt")
        result = worker.prepare_turn_recovery(spec, job, client)
        self.assertEqual(result["turn_id"], FOLLOW_UP_TURN_ID)
        self.assertEqual(result["time_basis"], "transcript_turn_messages")
        self.assertEqual(result["started_at"], messages[-1]["timestamp"])

    def test_unproven_identity_anchor_time_or_transcript_is_rejected(self):
        changes = ("scope", "identity", "time", "anchor", "candidate_message", "truncated", "changed", "turn_changed", "pending")
        for change in changes:
            with self.subTest(change=change):
                spec, job, client, old, new, messages = self.recovery_scene()
                if change == "scope": job["scope_id"] = "other"
                if change == "identity": new["sessionId"] = "foreign"
                if change == "time": new["startedAt"] = old["startedAt"]
                if change == "anchor": messages[0]["content"] = "different task"
                if change == "candidate_message": messages[-1]["turnId"] = "other-turn"
                if change == "truncated": client.session_get.return_value["session"]["hasMoreBefore"] = True
                if change == "changed":
                    first = client.agent_status.return_value
                    client.agent_status.side_effect = [first, dict(first, currentTurnId="other-turn")]
                if change == "turn_changed":
                    client.turn_get.side_effect = [old, new, dict(new, status="aborted", errorCode="TURN_ABORTED")]
                if change == "pending": client.agent_status.return_value["planningState"] = "awaiting_approval"
                with self.assertRaises(util.KanpilotError):
                    worker.prepare_turn_recovery(spec, job, client)


class TestRecoverySettlement(RecoveryCase):
    def test_explicit_completed_recovery_keeps_goal_approval_and_finishes(self):
        spec, job, client, *_ = self.recovery_scene(appended=True)
        run_dir = Path(spec["runs_dir"])
        job["goal_approval"] = {"status": "approved", "execution_turn_id": TURN_ID,
                                "planning_turn_id": "planning-turn", "proposal_id": "approved-goal"}
        (run_dir / dsh_events.JOB_NAME).write_text(json.dumps(job), encoding="utf-8")
        spec["continue_turn_authorization"] = worker.prepare_turn_recovery(
            spec, job, client, turn_id=FOLLOW_UP_TURN_ID)
        client.harness_pid = None
        written = []
        real_write = worker._write_json
        def capture(path, value):
            if path.name == dsh_events.JOB_NAME: written.append(copy.deepcopy(value))
            return real_write(path, value)
        def settle_report(_client, _job):
            pending = self.read_job(run_dir)
            self.assertEqual(pending["observation_finalizing"], {
                "turn_id": FOLLOW_UP_TURN_ID, "observed_status": "completed"})
            self.assertEqual(pending["status"], "failed")
            self.assertNotIn("observation_resumed_at", pending)
            return None, None
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock(), \
                mock.patch.object(runtime_client, "RuntimeClient", return_value=client), \
                mock.patch.object(worker, "_write_json", capture), \
                mock.patch.object(worker, "_goal_report_settlement", side_effect=settle_report):
            self.assertEqual(worker.run_real(spec, resume=True), 0)
            events = self.read_events(run_dir)
            self.assertEqual(worker.run_real(spec, resume=True), 0)
            self.assertEqual(self.read_events(run_dir), events)
        after = self.read_job(run_dir)
        self.assertEqual(after["status"], "finished")
        self.assertNotIn("observation_finalizing", after)
        self.assertEqual(after["turn_id"], FOLLOW_UP_TURN_ID)
        self.assertEqual(after["goal_approval"], job["goal_approval"])
        self.assertEqual(after["turn_adopted"][0]["recovery_authorization"], spec["continue_turn_authorization"])
        self.assertFalse(any(row["status"] == "running" for row in written))
        self.assertFalse(any(row.get("observation_resumed_at") and row["status"] == "failed" for row in written))
        self.assertFalse(client.agent_prompt.called)
        self.assertFalse(client.plans_resolve.called)

    def test_recovery_rechecks_authorization_before_adopting(self):
        spec, job, client, *_ = self.recovery_scene(appended=True)
        run_dir = Path(spec["runs_dir"])
        spec["continue_turn_authorization"] = worker.prepare_turn_recovery(
            spec, job, client, turn_id=FOLLOW_UP_TURN_ID)
        client.agent_status.return_value["currentTurnId"] = "other-turn"
        client.harness_pid = None
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock(), \
                mock.patch.object(runtime_client, "RuntimeClient", return_value=client):
            self.assertEqual(worker.run_real(spec, resume=True), 3)
        after = self.read_job(run_dir)
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["turn_id"], TURN_ID)
        self.assertNotIn("turn_adopted", after)
        self.assertFalse(client.agent_prompt.called)
        self.assertFalse(client.plans_resolve.called)

    def test_already_bound_recovery_can_resume_after_observer_exit(self):
        spec, job, client, *_ = self.recovery_scene(appended=True)
        run_dir = Path(spec["runs_dir"])
        authorization = worker.prepare_turn_recovery(spec, job, client, turn_id=FOLLOW_UP_TURN_ID)
        spec["continue_turn_authorization"] = authorization
        job["turn_id"] = FOLLOW_UP_TURN_ID
        job["turn_adopted"] = [{"from": TURN_ID, "to": FOLLOW_UP_TURN_ID,
                                "code": "TURN_ABORTED", "recovery_authorization": authorization}]
        (run_dir / dsh_events.JOB_NAME).write_text(json.dumps(job), encoding="utf-8")
        client.harness_pid = None
        client.plans_pending.return_value = {"plans": [], "history": []}
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock(), \
                mock.patch.object(runtime_client, "RuntimeClient", return_value=client):
            self.assertEqual(worker.run_real(spec, resume=True), 0)
        after = self.read_job(run_dir)
        self.assertEqual(after["status"], "finished")
        self.assertEqual(len(after["turn_adopted"]), 1)
        self.assertFalse(client.agent_prompt.called)
        self.assertFalse(client.plans_resolve.called)

    def test_adoption_crash_keeps_authorization_in_the_first_binding_write(self):
        spec, job, client, *_ = self.recovery_scene(appended=True)
        run_dir = Path(spec["runs_dir"])
        authorization = worker.prepare_turn_recovery(spec, job, client, turn_id=FOLLOW_UP_TURN_ID)
        spec["continue_turn_authorization"] = authorization
        client.harness_pid = None
        client.plans_pending.return_value = {"plans": [], "history": []}
        real_write = worker._write_json
        def crash_after_binding(path, value):
            real_write(path, value)
            if path.name == dsh_events.JOB_NAME and value.get("turn_id") == FOLLOW_UP_TURN_ID:
                raise RuntimeError("observer exited immediately after binding")
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock(), \
                mock.patch.object(runtime_client, "RuntimeClient", return_value=client):
            with mock.patch.object(worker, "_write_json", crash_after_binding), self.assertRaises(RuntimeError):
                worker.run_real(spec, resume=True)
            interrupted = self.read_job(run_dir)
            self.assertEqual(interrupted["turn_adopted"][-1]["recovery_authorization"], authorization)
            self.assertEqual(worker.run_real(spec, resume=True), 0)
        self.assertEqual(self.read_job(run_dir)["status"], "finished")
        self.assertEqual(len(self.read_job(run_dir)["turn_adopted"]), 1)

    def test_resume_aborted_never_persists_running(self):
        spec, run_dir = self._base()
        self.write_lost_observer_scene(spec, run_dir, detail="turn aborted: TURN_ABORTED")
        self.server.turn_status = "aborted"
        self.server.turn_error_code = "TURN_ABORTED"
        self.server.current_turn = TURN_ID
        self.server.messages = self.default_messages()
        written = []
        real_write = worker._write_json
        def capture(path, value):
            if path.name == dsh_events.JOB_NAME: written.append(copy.deepcopy(value))
            return real_write(path, value)
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock(), mock.patch.object(worker, "_write_json", capture):
            rc = worker.run_real(spec, resume=True)
            before_events = self.read_events(run_dir)
            self.assertEqual(worker.run_real(spec, resume=True), 1)
            self.assertEqual(self.read_events(run_dir), before_events)
        self.assertEqual(rc, 1)
        self.assertTrue(written)
        self.assertFalse(any(row["status"] == "running" for row in written), written)
        self.assertEqual(self.read_job(run_dir)["status"], "failed")
        self.assertEqual(self.server.prompt_count, 0)

    def test_resumed_turn_error_cannot_use_legacy_unproven_follow_up(self):
        spec, run_dir = self._base()
        self.write_lost_observer_scene(spec, run_dir)
        self.server.turn_statuses = ["running", "error", "completed"]
        self.server.turn_error_code = "EMPTY_MODEL_RESPONSE"
        self.server.follow_up_turn = FOLLOW_UP_TURN_ID
        self.server.messages = [{"id": "initial", "role": "user", "content": self.expected_prompt(spec),
                                 "timestamp": "2026-10-16T15:00:00Z"}]
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(worker.run_real(spec, resume=True), 1)
        self.assertEqual(self.read_job(run_dir)["status"], "failed")
        self.assertFalse(self.read_job(run_dir).get("turn_adopted"))
        self.assertEqual(self.server.prompt_count, 0)
