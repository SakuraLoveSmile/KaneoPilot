"""request-continuation：同一 Plus 会话发送一次继续指令，不改 job/任务状态。"""

import json
from pathlib import Path
from unittest.mock import patch

import test_execution_retry as retry_fixtures
from kanpilot import (cmd_request_continuation as cont, db, round_store, runtime_operations, util)


class FakeClient:
    def __init__(self, calls, fail=False):
        self.calls = calls
        self.fail = fail

    def agent_status(self, session_id):
        return {"status": {"currentTurnId": "failed-turn", "isRunning": False}}

    def turn_get(self, session_id, turn_id, require_identity=False):
        return {"sessionId": "failed-session", "turnId": "failed-turn", "status": "error"}

    def agent_prompt(self, session_id, content):
        self.calls.append((session_id, content))
        if self.fail:
            raise util.KanpilotError("remote_unavailable", "network lost")
        return {"turnId": "continued-turn", "accepted": True}


class RequestContinuationTests:
    def setup_fixture(self):
        self.f = retry_fixtures.TestExecutionRetry(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        now = f.ctx.now_iso
        config = {"connection_file": str(f.root / "fake-plus.json")}
        task = f.task()
        spec = {
            "task_id": f.task_id, "run_id": f.run_id,
            "cwd": str(f.worktree), "plan_path": task["plan_path"],
            "plan_sha256": task["plan_sha256"], "config": config,
            "timeout_seconds": 3600,
        }
        (f.directory / "spec.json").write_text(util.jdump(spec))
        with db.write_txn(f.ctx.conn):
            f.ctx.conn.execute(
                "UPDATE execution_rounds SET config_json=?, session_id=?, run_status='failed', "
                "updated_at=? WHERE task_id=? AND run_id=?",
                (util.jdump(config), "failed-session", now, f.task_id, f.run_id))
        self.calls = []
        patcher = patch.object(cont, "_client", side_effect=lambda *a, **k: FakeClient(self.calls))
        patcher.start()
        self.addCleanup(patcher.stop)
        observe = patch.object(cont.cmd_dispatch, "cmd_round_observe",
                               return_value={"status": "observing", "pid": 1})
        observe.start()
        self.addCleanup(observe.stop)
        self.before_job = (f.directory / "job.json").read_bytes()
        self.before_task = dict(f.task())

    def test_send_once_records_request_and_keeps_state(self):
        self.setup_fixture()
        f = self.f
        result = cont.cmd_request_continuation(
            f.ctx, {"task_id": f.task_id, "run_id": f.run_id, "confirm": True})
        self.assertEqual(result["status"], "continued")
        self.assertEqual(result["turn_id"], "continued-turn")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "failed-session")
        self.assertIn("继续执行当前 Goal", self.calls[0][1])
        extra = db.task_extra(f.task()).get(cont.REQUEST_KEY) or {}
        self.assertEqual(extra.get("new_turn"), "continued-turn")
        self.assertEqual(extra.get("from_turn"), "failed-turn")
        self.assertEqual(extra.get("status"), "sent")
        self.assertEqual(f.task()["status"], "running")
        self.assertIsNone(f.task()["failure_code"])
        job = json.loads((f.directory / "job.json").read_text())
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["turn_id"], "continued-turn")
        self.assertEqual(job["continue_previous"]["turn_id"], "failed-turn")
        self.assertEqual(job["turn_adopted"][-1]["to"], "continued-turn")
        scope = cont.runtime_client.stable_scope_id(
            "execution", task_id=f.task_id, run_id=f.run_id)
        op = runtime_operations.get_operation(f.ctx.conn, scope, result.get("operation_id", "")) if result.get("operation_id") else None
        if op is not None:
            self.assertEqual(op["status"], "accepted")

    def test_second_click_is_deduplicated_without_resend(self):
        self.setup_fixture()
        f = self.f
        first = cont.cmd_request_continuation(
            f.ctx, {"task_id": f.task_id, "run_id": f.run_id, "confirm": True})
        second = cont.cmd_request_continuation(
            f.ctx, {"task_id": f.task_id, "run_id": f.run_id, "confirm": True})
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertTrue(second.get("deduplicated"))
        self.assertEqual(len(self.calls), 1)

    def test_confirm_required(self):
        self.setup_fixture()
        f = self.f
        with self.assertRaises(util.KanpilotError) as caught:
            cont.cmd_request_continuation(f.ctx, {"task_id": f.task_id, "run_id": f.run_id})
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(len(self.calls), 0)

    def test_settled_continuation_can_continue_again_with_a_new_receipt(self):
        self.setup_fixture()
        f = self.f
        payload = {"task_id": f.task_id, "run_id": f.run_id, "confirm": True}
        cont.cmd_request_continuation(f.ctx, payload)
        job_path = f.directory / "job.json"
        job = json.loads(job_path.read_text())
        job.update(status="finished", exit_code=0, ended_at=f.ctx.now_iso)
        job_path.write_text(util.jdump(job))
        with db.write_txn(f.ctx.conn):
            db.update_task(f.ctx.conn, f.task_id, f.ctx.now_iso, status="needs_user", failure_code="verification_unconfirmed")
            f.ctx.conn.execute("UPDATE execution_rounds SET run_status='finished' WHERE task_id=?", (f.task_id,))
        client = FakeClient(self.calls)
        client.agent_status = lambda sid: {"currentTurnId": "continued-turn", "isRunning": False}
        client.turn_get = lambda *a, **kw: {"status": "completed"}
        def send(sid, content):
            self.calls.append((sid, content))
            return {"turnId": "second-continued-turn"}
        client.agent_prompt = send
        with patch.object(cont, "_client", return_value=client):
            second = cont.cmd_request_continuation(f.ctx, payload)
        self.assertEqual(second['turn_id'], 'second-continued-turn')
        self.assertEqual(len(self.calls), 2)
        updated = json.loads(job_path.read_text())
        self.assertTrue(cont.authorized_observation(f.ctx.conn, updated, 'continued-turn'))
        self.assertFalse(cont.authorized_observation(f.ctx.conn, updated, 'failed-turn'))

    def test_lost_response_marks_unknown_and_never_resends(self):
        self.setup_fixture()
        f = self.f
        with patch.object(cont, "_client", side_effect=lambda *a, **k: FakeClient(self.calls, fail=True)):
            with self.assertRaises(util.KanpilotError) as caught:
                cont.cmd_request_continuation(
                    f.ctx, {"task_id": f.task_id, "run_id": f.run_id, "confirm": True})
        self.assertEqual(caught.exception.code, "remote_unavailable")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(cont.REQUEST_KEY, db.task_extra(f.task()))
        self.assertEqual(f.task()["status"], "needs_user")
        with self.assertRaises(util.KanpilotError) as second:
            cont.cmd_request_continuation(
                f.ctx, {"task_id": f.task_id, "run_id": f.run_id, "confirm": True})
        self.assertEqual(second.exception.code, "conflict")
        self.assertEqual(len(self.calls), 1)


import unittest


class RequestContinuationTest(RequestContinuationTests, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()


if __name__ == "__main__":
    unittest.main()
