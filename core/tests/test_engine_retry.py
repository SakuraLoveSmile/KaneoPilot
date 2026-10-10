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
                                        "actor": "automation", "retry_base": 1})
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

    def _failed_request(self, task_id, kind, attempt_no, failure_code, payload=None):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "INSERT INTO requests(request_id, idempotency_key, task_id, stage, "
                    "kind, status, attempt_no, payload_json, result_json, created_at, updated_at) "
                    "VALUES (?,?,?,?,?, 'failed', ?, ?, ?, ?, ?)",
                    (util.new_uuid(), f"{kind}:{task_id}", task_id, kind, kind,
                     attempt_no, util.jdump(payload) if payload else None,
                     util.jdump({"failure_code": failure_code}),
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
        """封顶按 payload 的 retry_base 判定，不是失败请求自身的 attempt_no。

        重入队的是**新**请求（attempt_no 归零），所以只有随 payload 传递的累计
        计数能让上限在实时链路里生效；旧口径用 attempt_no 永远达不到 2。
        """
        tid = self._running_task("taskretryC000000000001")
        self._failed_request(tid, "monitor", 2, "remote_unavailable",
                             payload={"retry_base": 2})
        self.assertIsNone(self._retry_decision(tid))   # escalate, stop

    def test_retry_cap_accumulates_across_requests(self):
        """同一阶段连续瞬时失败：第 1、2 次退避重试，第 3 次封顶不再重试。"""
        tid = self._running_task("taskretryF000000000001")
        rec = loop.Reconciler(self.db_path, mode="live")
        spent = 0
        for expected_base in (1, 2):
            self._failed_request(tid, "monitor", 1, "remote_unavailable",
                                 payload={"retry_base": spent} if spent else None)
            decision = self._retry_decision(tid)
            self.assertIsNotNone(decision, f"第 {expected_base} 次应仍可重试")
            self.assertEqual(decision.action, decide.RETRY)
            self.assertEqual(decision.params["retry_base"], expected_base)
            # 引擎重入队时把新基数写进 payload
            calls = steps.commands_for(decision, {"task_id": tid,
                                                  "send_state": None})
            self.assertEqual(calls[0].payload["retry_base"], expected_base)
            spent = expected_base
        # 已花费 2 次 → 封顶，交给 attention
        self._failed_request(tid, "monitor", 1, "remote_unavailable",
                             payload={"retry_base": spent})
        self.assertIsNone(self._retry_decision(tid))

    def test_enqueue_persists_retry_base_through_the_db(self):
        """retry_base 必须落库：否则下一轮失败读回 None、封顶累积不了。

        ca06344 只做了「读取侧按 payload 的 retry_base 计预算」，
        cmd_enqueue 落库时却只保留 note，重入队的新请求没有 retry_base，
        _transient_retry 每次读回 None → spent=0 → ≤2/阶段 永远达不到。
        这条用例走真实 enqueue 落库 → 读回，钉住传递闭环。
        """
        tid = self._running_task("taskretryH000000000001")
        result = self.call("enqueue", {"task_id": tid, "kind": "monitor",
                                       "not_before": "2030-01-01T00:00:00+00:00",
                                       "actor": "automation", "retry_base": 2})
        row = result["request"]
        self.assertTrue(row.get("request_id"))
        conn = db.connect(self.db_path)
        try:
            stored = conn.execute(
                "SELECT payload_json FROM requests WHERE request_id=?",
                (row["request_id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(util.jload(stored).get("retry_base"), 2)
        # 落库后可被 _transient_retry 读回并按已花费 2 次封顶
        self._failed_request(tid, "monitor", 1, "remote_unavailable",
                             payload={"retry_base": 2})
        self.assertIsNone(self._retry_decision(tid))

    def test_enqueue_without_retry_base_keeps_legacy_null_payload(self):
        """legacy 语义不变：无 note、无 retry_base 时 payload 仍写 NULL。"""
        tid = self._running_task("taskretryI000000000001")
        result = self.call("enqueue", {"task_id": tid, "kind": "monitor",
                                       "not_before": "2030-01-01T00:00:00+00:00"})
        row = result["request"]
        self.assertTrue(row.get("request_id"))
        conn = db.connect(self.db_path)
        try:
            stored = conn.execute(
                "SELECT payload_json FROM requests WHERE request_id=?",
                (row["request_id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(stored)

    def test_dispatching_enqueue_requires_retry_base_credential(self):
        """G2 反向守卫：dispatching 状态下普通 enqueue 仍被拒。

        cmd_enqueue 放行 dispatching 的退避重入队，凭据是 payload 里的
        retry_base（只有引擎的退避重入队会带）。不带凭据的普通入队必须仍然
        bad_request，否则状态门被悄悄放宽。
        """
        tid = self._running_task("taskretryJ000000000001")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                # 真实链路里计划在派发前已确认（hold 已清），瞬时失败发生在
                # 那之后；这里复现该后置状态，只测状态门本身。
                extra = db.task_extra(db.require_task(conn, tid))
                extra["plan_execution_hold"] = False
                conn.execute(
                    "UPDATE tasks SET status='dispatching', extra_json=? WHERE task_id=?",
                    (util.jdump(extra), tid))
        finally:
            conn.close()
        # 注意：make_ready_task 发布的计划带 plan_execution_hold，dispatch 入队
        # 需 confirm_plan（与引擎 CONFIRM_PLAN 同形状）；这里测的是**状态门**，
        # 所以两条都带 confirm_plan，差别只在 retry_base 凭据。
        exc = self.call_err("enqueue", {"task_id": tid, "kind": "dispatch",
                                        "actor": "automation", "confirm_plan": True})
        self.assertEqual(exc.code, "bad_request")
        # 带上凭据即放行（引擎退避重入队的形状）
        result = self.call("enqueue", {"task_id": tid, "kind": "dispatch",
                                       "actor": "automation", "confirm_plan": True,
                                       "retry_base": 1})
        self.assertTrue(result.get("request", {}).get("request_id"))

    def test_backoff_grows_with_the_cumulative_count(self):
        tid = self._running_task("taskretryG000000000001")
        backoffs = []
        spent = 0
        for _ in range(2):
            self._failed_request(tid, "monitor", 1, "remote_unavailable",
                                 payload={"retry_base": spent} if spent else None)
            decision = self._retry_decision(tid)
            backoffs.append(decision.params["backoff_seconds"])
            spent = decision.params["retry_base"]
        self.assertEqual(backoffs, [1.0, 2.0])   # 1s → 2s，随累计次数指数增长

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
