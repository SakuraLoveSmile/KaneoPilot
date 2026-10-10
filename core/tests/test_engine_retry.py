"""P0: ladder-1 transient RETRY re-enqueue (state-model §8).

commands_for(RETRY) re-enqueues the same request kind with a future not_before
(exponential backoff, ≤2/stage) and never rebuilds a sent/unknown dispatch
prompt. The reconciler's _transient_retry detects a transiently-failed request
and, while the budget lasts, builds the backoff decision; once spent it returns
None (escalate → the failed request surfaces via attention).

Run: ``cd core && python3 -m pytest tests/test_engine_retry.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import decide, loop, steps  # noqa: E402


def _task(**kw):
    base = {"task_id": "t1", "plan_sha256": "sha", "send_state": None}
    base.update(kw)
    return base


def _retry(kind, not_before, attempt_no=1):
    return decide.Decision(decide.RETRY, reason="remote_unavailable",
                           ladder=1, params={"kind": kind, "not_before": not_before,
                                             "attempt_no": attempt_no})


class TestRetrySteps(unittest.TestCase):
    def test_reenqueue_with_not_before(self):
        calls = steps.commands_for(_retry("monitor", "2099-01-01T00:00:00+00:00"), _task())
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.command, "enqueue")
        self.assertEqual(call.payload, {"task_id": "t1", "kind": "monitor",
                                        "not_before": "2099-01-01T00:00:00+00:00",
                                        "actor": "automation"})
        self.assertEqual(call.kind, "monitor")

    def test_dispatch_not_sent_rebuilds(self):
        calls = steps.commands_for(_retry("dispatch", "2099-01-01T00:00:00+00:00"),
                                   _task(send_state="not_sent"))
        self.assertEqual([c.payload["kind"] for c in calls], ["dispatch"])

    def test_dispatch_sent_or_unknown_never_rebuilt(self):
        for send_state in ("sent", "unknown"):
            with self.subTest(send_state=send_state):
                self.assertEqual(
                    steps.commands_for(_retry("dispatch", "2099-01-01T00:00:00+00:00"),
                                       _task(send_state=send_state)), [])

    def test_no_kind_means_no_command(self):
        d = decide.Decision(decide.RETRY, params={"not_before": "x"})
        self.assertEqual(steps.commands_for(d, _task()), [])

    def test_failure_decision_budget_and_backoff(self):
        d0 = decide.failure_decision("remote_unavailable", attempt_no=0)
        d1 = decide.failure_decision("remote_unavailable", attempt_no=1)
        self.assertEqual((d0.action, d0.params["backoff_seconds"]), (decide.RETRY, 1.0))
        self.assertEqual((d1.action, d1.params["backoff_seconds"]), (decide.RETRY, 2.0))
        d2 = decide.failure_decision("remote_unavailable", attempt_no=2)
        self.assertTrue(d2.is_block)  # >2/stage -> escalate (ladder 1 -> 2)


class TestRetryLoop(KanpilotTestCase):
    def _running_task(self, task_id):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(), adopted_at=util.now_iso())
                conn.execute("UPDATE tasks SET status='running', run_id=? WHERE task_id=?",
                             ("r1", tid))
        finally:
            conn.close()
        return tid

    def _failed_request(self, task_id, kind, attempt_no, failure_code):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "INSERT INTO requests(request_id, idempotency_key, task_id, stage, "
                    "kind, status, attempt_no, result_json, created_at, updated_at) "
                    "VALUES (?,?,?,?,?, 'failed', ?, ?, ?, ?)",
                    (util.new_uuid(), f"{kind}:{task_id}", task_id, kind, kind,
                     attempt_no, util.jdump({"failure_code": failure_code}),
                     util.now_iso(), util.now_iso()))
        finally:
            conn.close()

    def _queued(self, task_id, kind):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT not_before FROM requests WHERE task_id=? AND kind=? "
                "AND status='queued'", (task_id, kind)).fetchall()
        finally:
            conn.close()

    def _row(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return db.require_task(conn, task_id)
        finally:
            conn.close()

    def _retry_decision(self, task_id):
        rec = loop.Reconciler(self.db_path, mode="live")
        conn = db.connect(self.db_path)
        try:
            return rec._transient_retry(conn, self._row(task_id), util.now_dt())
        finally:
            conn.close()

    def test_reconcile_applies_backoff_for_transient(self):
        tid = self._running_task("taskretryA000000000001")
        self._failed_request(tid, "monitor", 1, "remote_unavailable")
        loop.Reconciler(self.db_path, mode="live").reconcile_once()
        queued = self._queued(tid, "monitor")
        self.assertEqual(len(queued), 1)
        self.assertTrue(util.ts_before(util.now_iso(), queued[0]["not_before"]))

    def test_transient_retry_builds_backoff_decision(self):
        tid = self._running_task("taskretryB000000000001")
        self._failed_request(tid, "monitor", 1, "remote_unavailable")
        d = self._retry_decision(tid)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, decide.RETRY)
        self.assertTrue(util.ts_before(util.now_iso(), d.params["not_before"]))

    def test_transient_retry_exhausted_returns_none(self):
        tid = self._running_task("taskretryC000000000001")
        self._failed_request(tid, "monitor", 2, "remote_unavailable")
        self.assertIsNone(self._retry_decision(tid))   # escalate, stop

    def test_transient_retry_non_transient_returns_none(self):
        tid = self._running_task("taskretryD000000000001")
        self._failed_request(tid, "monitor", 1, "verification_unconfirmed")
        self.assertIsNone(self._retry_decision(tid))

    def test_transient_retry_dispatch_sent_returns_none(self):
        tid = self._running_task("taskretryE000000000001")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET send_state='sent' WHERE task_id=?", (tid,))
        finally:
            conn.close()
        self._failed_request(tid, "dispatch", 1, "remote_unavailable")
        self.assertIsNone(self._retry_decision(tid))   # never resend


if __name__ == "__main__":
    unittest.main()
