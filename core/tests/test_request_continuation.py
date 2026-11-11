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


class ReconcileFakeClient(FakeClient):
    """Client whose read-only transcript answers the attribution scan."""

    def __init__(self, calls, pages, *, current_turn="failed-turn", running=False,
                 fail=False):
        super().__init__(calls, fail=fail)
        self.pages = pages
        self.current_turn = current_turn
        self.running = running
        self.reads = []

    def agent_status(self, session_id):
        return {"status": {"sessionId": session_id, "currentTurnId": self.current_turn,
                           "isRunning": self.running}}

    def session_get(self, session_id, **kwargs):
        before = kwargs.get("message_before")
        self.reads.append(before)
        return self.pages(before)


def _page(messages, start=None, more_before=False, more_after=False, truncated=False):
    if truncated:
        return {"truncated": True, "reason": "MCP_RESULT_LIMIT"}
    start = len(messages) if start is None else start
    return {"session": {"id": "failed-session", "messages": messages,
                        "message_start": start, "message_end": start + len(messages),
                        "has_more_before": more_before, "has_more_after": more_after}}


class ReconcileContinuationTests:
    def setup_fixture(self, pages, *, current_turn="failed-turn", running=False):
        self.base = RequestContinuationTests()
        self.base.addCleanup = self.addCleanup
        self.base.setup_fixture()
        self.f = self.base.f
        self.pages = pages
        self.current_turn = current_turn
        self.running = running
        # Reproduce the real failure: the send slot is left unknown.
        lost = ReconcileFakeClient(self.base.calls, pages, fail=True)
        with patch.object(cont, "_client", side_effect=lambda *a, **k: lost):
            with self.assertRaises(util.KanpilotError) as caught:
                cont.cmd_request_continuation(
                    self.f.ctx, {"task_id": self.f.task_id, "run_id": self.f.run_id,
                                 "confirm": True})
        self.assertEqual(caught.exception.code, "remote_unavailable")
        self.assertEqual(len(self.base.calls), 1)

    def reconcile(self, **payload):
        client = ReconcileFakeClient(self.base.calls, self.pages,
                                     current_turn=self.current_turn,
                                     running=self.running)
        with patch.object(cont, "_client", side_effect=lambda *a, **k: client):
            return cont.cmd_reconcile_continuation(
                self.f.ctx, {"task_id": self.f.task_id, "run_id": self.f.run_id, **payload})

    def statuses(self):
        scope = cont.runtime_client.stable_scope_id(
            "execution", task_id=self.f.task_id, run_id=self.f.run_id)
        return [row["status"] for row in self.f.ctx.conn.execute(
            "SELECT status FROM runtime_operations WHERE scope_id=? AND tool=?",
            (scope, cont.SCOPE_TOOL)).fetchall()]

    def job(self):
        return json.loads((self.f.directory / "job.json").read_text())

    def test_dry_run_reports_not_sent_without_touching_state(self):
        self.setup_fixture(lambda before: _page([
            {"role": "assistant", "turnId": "failed-turn", "content": "done"}]))
        before = (self.f.directory / "job.json").read_bytes()
        result = self.reconcile()
        self.assertEqual(result["classification"], "not_sent")
        self.assertFalse(result["applied"])
        self.assertEqual((self.f.directory / "job.json").read_bytes(), before)
        self.assertEqual(self.statuses(), ["unknown"])
        self.assertEqual(self.f.task()["status"], "needs_user")

    def test_confirmed_not_sent_releases_the_send_slot(self):
        self.setup_fixture(lambda before: _page([
            {"role": "assistant", "turnId": "failed-turn", "content": "done"}]))
        result = self.reconcile(dry_run=False, confirm=True)
        self.assertEqual(result["status"], "released")
        self.assertTrue(result["applied"])
        self.assertEqual(self.statuses(), ["rejected"])
        self.assertEqual(self.f.task()["status"], "needs_user")
        self.assertTrue(cont.continue_execution_view(
            self.f.ctx.conn, self.f.task())["eligible"])

    def test_confirmed_delivered_adopts_the_real_turn(self):
        self.setup_fixture(
            lambda before: _page([{"role": "user", "turnId": "continued-turn",
                                   "content": cont.PROMPT + "\n\n后续"}],
                                 start=1, more_before=True),
            current_turn="continued-turn")
        result = self.reconcile(dry_run=False, confirm=True)
        self.assertEqual(result["status"], "continued")
        self.assertEqual(result["turn_id"], "continued-turn")
        job = self.job()
        self.assertEqual(job["turn_id"], "continued-turn")
        self.assertEqual(job["turn_adopted"][-1]["code"], "reconciled_continue")
        self.assertEqual(self.f.task()["status"], "running")

    def test_foreign_newer_turn_stays_unknown(self):
        self.setup_fixture(
            lambda before: _page([{"role": "user", "turnId": "foreign-turn",
                                   "content": "手工消息"}],
                                 start=1, more_before=True),
            current_turn="foreign-turn")
        result = self.reconcile(dry_run=False, confirm=True)
        self.assertEqual(result["status"], "unconfirmed")
        self.assertFalse(result["applied"])
        self.assertEqual(self.statuses(), ["unknown"])
        self.assertEqual(self.f.task()["status"], "needs_user")

    def test_running_session_stays_unknown(self):
        self.setup_fixture(lambda before: _page([]), running=True)
        self.assertEqual(self.reconcile()["classification"], "unknown")

    def test_truncated_transcript_stays_unknown(self):
        self.setup_fixture(lambda before: _page([], truncated=True))
        self.assertEqual(self.reconcile()["classification"], "unknown")

    def test_confirm_is_required_to_release(self):
        self.setup_fixture(lambda before: _page([
            {"role": "assistant", "turnId": "failed-turn", "content": "done"}]))
        with self.assertRaises(util.KanpilotError) as caught:
            self.reconcile(dry_run=False)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(self.statuses(), ["unknown"])

    def test_reconcile_needs_an_unknown_operation(self):
        base = RequestContinuationTests()
        base.addCleanup = self.addCleanup
        base.setup_fixture()
        self.f = base.f
        with self.assertRaises(util.KanpilotError) as caught:
            cont.cmd_reconcile_continuation(
                self.f.ctx, {"task_id": self.f.task_id, "run_id": self.f.run_id})
        self.assertEqual(caught.exception.code, "conflict")


class ReconcileContinuationTest(ReconcileContinuationTests, unittest.TestCase):
    pass
