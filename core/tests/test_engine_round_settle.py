"""Round terminal-state registration fallback (sandbox §13.2).

Shape under test: the worker exited and ``job.json`` recorded
``ended_at``/``exit_code``, but the monitor's observation window had already
closed, so ``execution_rounds.run_status`` stayed ``running`` and the task
waited in observe/running forever (``decide`` can only AWAIT).

Run: ``cd core && python3 -m pytest tests/test_engine_round_settle.py -q``.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import cmd_read, core, db, dsh_events, round_store, util  # noqa: E402
from kanpilot.engine import decide, loop, rounds  # noqa: E402

# A pid that is certainly not alive on this machine (never recycled by the
# test process itself); pid 1 is excluded by util.pid_alive on purpose.
DEAD_PID = 2 ** 22 + 7


class RoundExitViewTest(KanpilotTestCase):
    """``rounds.round_exit`` is a read-only world view (never writes)."""

    def _task(self, task_id="taskroundvw00000000001"):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root", str(self.runs_root))
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
                conn.execute("UPDATE tasks SET status='running', run_id=? "
                             "WHERE task_id=?", ("runvw0001", tid))
                round_store.upsert_on_claim(conn, tid, "runvw0001", {},
                                            util.now_iso(), backend="pi")
                round_store.mark_started(conn, tid, "runvw0001",
                                         util.now_iso(), pid=DEAD_PID)
        finally:
            conn.close()
        return tid

    def _write_job(self, tid, run_id="runvw0001", *, status="finished",
                   exit_code=0, ended=True, pid=DEAD_PID, task_id=None,
                   events=()):
        directory = self.runs_root / tid / run_id
        directory.mkdir(parents=True, exist_ok=True)
        job = {"task_id": task_id or tid, "run_id": run_id, "backend": "pi",
               "status": status, "session_id": "sess-vw-1",
               "started_at": "2026-10-11T20:13:00+00:00", "pid": pid,
               "ended_at": "2026-10-11T20:14:30+00:00" if ended else None,
               "exit_code": exit_code if ended else None}
        (directory / "job.json").write_text(json.dumps(job), encoding="utf-8")
        if events:
            lines = [json.dumps({"seq": index + 1, "type": "terminal",
                                 "ts": "2026-10-11T20:14:30+00:00", **event},
                                ensure_ascii=False)
                     for index, event in enumerate(events)]
            (directory / dsh_events.EVENT_LOG_NAME).write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        return directory

    def _view(self, tid, run_id="runvw0001"):
        conn = db.connect(self.db_path)
        try:
            return rounds.round_exit(conn, tid, run_id)
        finally:
            conn.close()

    def test_clean_exit_is_provable(self):
        tid = self._task()
        self._write_job(tid)
        view = self._view(tid)
        self.assertTrue(view["available"])
        self.assertTrue(view["exited"])
        self.assertEqual(view["exit_code"], 0)
        self.assertEqual(view["ended_at"], "2026-10-11T20:14:30+00:00")
        self.assertFalse(view["pid_alive"])
        self.assertFalse(view["terminal_recorded"])
        self.assertEqual(view["run_status"], "running")

    def test_live_process_is_not_exited(self):
        tid = self._task("taskroundvwlive00000001")
        self._write_job(tid, pid=os.getpid())  # this very test process
        view = self._view(tid)
        self.assertTrue(view["available"])
        self.assertTrue(view["pid_alive"])
        self.assertFalse(view["exited"])

    def test_missing_process_result_is_not_exited(self):
        tid = self._task("taskroundvwnoend00000")
        self._write_job(tid, status="running", ended=False)
        view = self._view(tid)
        self.assertTrue(view["available"])
        self.assertIsNone(view["ended_at"])
        self.assertIsNone(view["exit_code"])
        self.assertFalse(view["exited"])

    def test_registered_terminal_outcome_is_flagged(self):
        tid = self._task("taskroundvwdone00000001")
        self._write_job(tid)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute("UPDATE execution_rounds SET run_status='finished' "
                             "WHERE task_id=? AND run_id=?", (tid, "runvw0001"))
        finally:
            conn.close()
        view = self._view(tid)
        self.assertTrue(view["terminal_recorded"])
        self.assertEqual(view["run_status"], "finished")

    def test_identity_mismatch_is_not_trusted(self):
        tid = self._task("taskroundvwbadid0000001")
        self._write_job(tid, task_id="someothertask00000001")
        view = self._view(tid)
        self.assertFalse(view["available"])
        self.assertEqual(view["reason"], "job_unreadable")
        self.assertFalse(view["exited"])

    def test_missing_run_dir_is_not_trusted(self):
        tid = self._task("taskroundvwnojob0000001")
        view = self._view(tid)
        self.assertFalse(view["available"])
        self.assertEqual(view["reason"], "run_dir_missing")
        self.assertFalse(view["exited"])

    def test_missing_job_file_is_not_trusted(self):
        tid = self._task("taskroundvwnojobf0000001")
        (self.runs_root / tid / "runvw0001").mkdir(parents=True, exist_ok=True)
        view = self._view(tid)
        self.assertFalse(view["available"])
        self.assertEqual(view["reason"], "job_unreadable")
        self.assertFalse(view["exited"])

    def test_no_round_row_is_not_trusted(self):
        tid = self._task("taskroundvwnorow0000001")
        view = self._view(tid, "runvw9999")
        self.assertFalse(view["available"])
        self.assertEqual(view["reason"], "no_round_row")

    def test_terminal_event_is_exposed(self):
        tid = self._task("taskroundvwevnt00000001")
        self._write_job(tid, events=[{"outcome": "completed",
                                      "reason": "control_turn_completed"}])
        view = self._view(tid)
        self.assertEqual(view["terminal"],
                         {"outcome": "completed",
                          "reason": "control_turn_completed"})


class RoundSettleReconcileTest(KanpilotTestCase):
    """``loop.Reconciler`` settles the missed terminal registration."""

    def _stuck(self, task_id, *, run_id="runsettle0001", run_status="running",
               status="finished", exit_code=0, ended=True, pid=DEAD_PID,
               active_request=False, settles=None):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root", str(self.runs_root))
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
                conn.execute("UPDATE tasks SET status='running', run_id=? "
                             "WHERE task_id=?", (run_id, tid))
                round_store.upsert_on_claim(conn, tid, run_id, {},
                                            util.now_iso(), backend="pi")
                round_store.mark_started(conn, tid, run_id, util.now_iso(),
                                         pid=pid)
                if run_status != "running":
                    conn.execute("UPDATE execution_rounds SET run_status=? "
                                 "WHERE task_id=? AND run_id=?",
                                 (run_status, tid, run_id))
                if settles is not None:
                    db.set_extra(conn, db.require_task(conn, tid),
                                 util.now_iso(),
                                 **{loop.ROUND_SETTLE_KEY: settles})
                if active_request:
                    db.create_request(conn, util.now_iso(), "monitor",
                                      f"monitor:{tid}", task_id=tid,
                                      payload={}, requested_by="engine")
        finally:
            conn.close()
        directory = self.runs_root / tid / run_id
        directory.mkdir(parents=True, exist_ok=True)
        job = {"task_id": tid, "run_id": run_id, "backend": "pi",
               "status": status, "session_id": "sess-settle-1",
               "started_at": "2026-10-11T20:13:00+00:00", "pid": pid,
               "ended_at": "2026-10-11T20:14:30+00:00" if ended else None,
               "exit_code": exit_code if ended else None}
        (directory / "job.json").write_text(json.dumps(job), encoding="utf-8")
        return tid

    def _monitor_requests(self, tid):
        return [r for r in self.requests(status="queued")
                if r["task_id"] == tid and r["kind"] == "monitor"]

    def _actions(self, tid, summary):
        return [a for a in summary["actions"] if a.get("task_id") == tid]

    def _failed_monitor(self, tid, spent=0, code="remote_unavailable"):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                now = util.now_iso()
                row, _ = db.create_request(conn, now, "monitor", f"monitor:{tid}",
                    task_id=tid, payload={"retry_base": spent}, requested_by="automation")
                db.update_request(conn, row["request_id"], now, status="failed",
                    finished_at=now, result_json=util.jdump({"failure_code": code}))
                return row["request_id"]
        finally:
            conn.close()

    def _due_tick(self, tid):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                return cmd_read.tick_inline(conn, util.now_dt() + timedelta(days=1))
        finally:
            conn.close()

    def test_transient_monitor_cap_cannot_reopen_via_settle_or_tick(self):
        tid = self._stuck("tasksettleretrycap001")
        self._failed_monitor(tid, spent=2)
        rec = loop.Reconciler(self.db_path, mode="live", enable_tick=True)
        for _ in range(2):
            summary = rec.reconcile_once()
            action = self._actions(tid, summary)[0]
            self.assertEqual((action["action"], action["reason"], action["ladder"]),
                             (decide.BLOCK, "transient_exhausted", 2))
            self.assertTrue(action["attention"])
            self.assertEqual(self._monitor_requests(tid), [])
            self.assertNotIn(loop.ROUND_SETTLE_KEY, self.task(tid)["extra"])
        tick = self._due_tick(tid)
        self.assertIn({"kind": "monitor", "task_id": tid,
                       "reason": "transient_exhausted"}, tick["skipped"])
        self.assertEqual(self._monitor_requests(tid), [])

    def test_transient_monitor_retry_owns_backoff_before_settle_and_tick(self):
        tid = self._stuck("tasksettleretrydue001")
        self._failed_monitor(tid, spent=1)
        tick = self._due_tick(tid)
        self.assertIn({"kind": "monitor", "task_id": tid,
                       "reason": "transient_retry_pending"}, tick["skipped"])
        summary = loop.Reconciler(self.db_path, mode="live", enable_tick=True).reconcile_once()
        action = self._actions(tid, summary)[0]
        self.assertEqual(action["action"], decide.RETRY)
        self.assertTrue(action["executed"])
        requests = self._monitor_requests(tid)
        self.assertEqual(len(requests), 1)
        self.assertTrue(util.ts_before(util.now_iso(), requests[0]["not_before"]))
        conn = db.connect(self.db_path)
        try:
            row = db.get_request(conn, requests[0]["request_id"])
            self.assertEqual(db.request_payload(row)["retry_base"], 2)
        finally:
            conn.close()
        self.assertNotIn(loop.ROUND_SETTLE_KEY, self.task(tid)["extra"])

    def test_automatic_enqueue_cannot_reset_monitor_retry_chain(self):
        tid = self._stuck("tasksettleretryenq001")
        self._failed_monitor(tid, spent=1)
        for extra in ({}, {"retry_base": 0}, {"retry_base": 1}, {"retry_base": 2},
                      {"retry_base": 2, "not_before": "2020-01-01T00:00:00+00:00"}):
            payload = {"task_id": tid, "kind": "monitor", "actor": "automation", **extra}
            self.assertEqual(self.call_err("enqueue", payload).code, "conflict")
        self.assertEqual(self.call_err("enqueue", {"task_id": tid, "kind": "monitor",
            "actor": "automation", "retry_base": True}).code, "bad_request")
        # An explicit user observation remains available for recovery.
        self.call("enqueue", {"task_id": tid, "kind": "monitor"})
        self.assertEqual(len(self._monitor_requests(tid)), 1)

    def test_cancelled_lost_retry_does_not_restore_monitor_allowance(self):
        tid = self._stuck("tasksettleretrylost01")
        self._failed_monitor(tid, spent=1)
        retry = self.call("enqueue", {"task_id": tid, "kind": "monitor",
            "actor": "automation", "retry_base": 2,
            "not_before": util.plus_seconds(60)})["request"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                # A claimed retry whose owner vanished is released by the
                # ordinary lost-request recovery as cancelled.
                db.update_request(conn, retry["request_id"], util.now_iso(),
                    status="claimed", claimed_at=util.now_iso(), lease_expires_at="2020-01-01T00:00:00+00:00")
        finally:
            conn.close()
        self._due_tick(tid)  # lost claim is cancelled after the monitor top-up pass
        self._due_tick(tid)
        self.assertEqual(self._monitor_requests(tid), [])
        summary = loop.Reconciler(self.db_path, mode="live", enable_tick=True).reconcile_once()
        self.assertEqual(self._actions(tid, summary)[0]["reason"], "transient_exhausted")

    def test_late_round_discovery_does_not_supersede_current_failure(self):
        tid = self._stuck("tasksettleretrylate01")
        self._failed_monitor(tid, spent=2)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET sent_at=? WHERE task_id=?",
                    ("2020-01-01T00:00:00+00:00", tid))
                conn.execute("UPDATE execution_rounds SET created_at=? WHERE task_id=?",
                    ("2099-01-01T00:00:00+00:00", tid))
        finally:
            conn.close()
        summary = loop.Reconciler(self.db_path, mode="live", enable_tick=True).reconcile_once()
        self.assertEqual(self._actions(tid, summary)[0]["reason"], "transient_exhausted")
        self.assertEqual(self._monitor_requests(tid), [])

    def test_new_execution_and_successful_monitor_supersede_old_failure(self):
        for later in ("execution", "success"):
            tid = self._stuck("tasksettleretry" + later)
            failed = self._failed_monitor(tid, spent=2)
            conn = db.connect(self.db_path)
            try:
                with db.write_txn(conn):
                    if later == "execution":
                        # The previous monitor predates this execution's identity.
                        conn.execute("UPDATE requests SET created_at=? WHERE request_id=?",
                            ("2020-01-01T00:00:00+00:00", failed))
                    else:
                        now = util.now_iso()
                        row, _ = db.create_request(conn, now, "monitor", f"monitor:{tid}", task_id=tid)
                        db.update_request(conn, row["request_id"], now, status="succeeded", finished_at=now)
                self.assertIsNone(core.monitor_retry_state(conn, db.require_task(conn, tid)))
            finally:
                conn.close()
            summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
            self.assertEqual(self._actions(tid, summary)[0]["action"], decide.SETTLE_ROUND)
            self.assertEqual(len(self._monitor_requests(tid)), 1)

    def test_nontransient_failure_and_manual_takeover_keep_observation_topups(self):
        for kind in ("unknown", "verification_unconfirmed", "manual"):
            tid = self._stuck("tasksettleretry" + kind)
            self._failed_monitor(tid, spent=2,
                code="remote_unavailable" if kind == "manual" else kind)
            if kind == "manual":
                conn = db.connect(self.db_path)
                try:
                    with db.write_txn(conn):
                        core.update_automation(conn, db.require_task(conn, tid),
                                               util.now_iso(), manual_takeover=True)
                finally:
                    conn.close()
            self._due_tick(tid)
            self.assertEqual(len(self._monitor_requests(tid)), 1)

    def test_clean_exit_pushes_the_round_to_verdict(self):
        tid = self._stuck("tasksettleclean000001")
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        action = self._actions(tid, summary)[0]
        self.assertEqual(action["action"], decide.SETTLE_ROUND)
        self.assertEqual(action["reason"], "round_exited_zero")
        self.assertTrue(action["executed"])
        self.assertEqual(action["command"], "enqueue")
        # One monitor request is queued: the monitor stage reads the now
        # complete round and runs the core-owned verdict path.
        self.assertEqual(len(self._monitor_requests(tid)), 1)
        # The bounded-budget receipt is recorded durably.
        extra = self.task(tid)["extra"]
        self.assertEqual(len(extra[loop.ROUND_SETTLE_KEY]), 1)
        self.assertEqual(extra[loop.ROUND_SETTLE_KEY][0]["run_id"],
                         "runsettle0001")
        self.assertEqual(extra[loop.ROUND_SETTLE_KEY][0]["exit_code"], 0)

    def test_nonzero_exit_is_attributed_to_the_failure_ladder(self):
        tid = self._stuck("tasksettlefail000001", status="failed",
                          exit_code=1)
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        action = self._actions(tid, summary)[0]
        self.assertEqual(action["action"], decide.SETTLE_ROUND)
        self.assertEqual(action["reason"], "execution_failed")
        self.assertEqual(action["ladder"], 4)  # needs-you (阶梯 4)
        self.assertTrue(action["executed"])
        # Still exactly one monitor request: the monitor stage reports
        # needs_user/execution_failed (→ attention inbox), the engine does not
        # forge the failure itself.
        self.assertEqual(len(self._monitor_requests(tid)), 1)

    def test_active_request_skips_the_probe(self):
        tid = self._stuck("tasksettlebusy000001", active_request=True)
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        # The existing monitor request stays; the engine neither probes nor
        # enqueues a second one.
        self.assertEqual(len(self._monitor_requests(tid)), 1)
        actions = self._actions(tid, summary)
        self.assertTrue(actions)
        self.assertEqual(actions[0]["action"], decide.AWAIT)
        extra = self.task(tid)["extra"]
        self.assertNotIn(loop.ROUND_SETTLE_KEY, extra)

    def test_backfilled_terminal_status_still_settles_running_task(self):
        tid = self._stuck("tasksettledone000001", run_status="finished")
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        actions = self._actions(tid, summary)
        self.assertTrue(actions)
        self.assertEqual(actions[0]["action"], decide.SETTLE_ROUND)
        self.assertEqual(len(self._monitor_requests(tid)), 1)
        extra = self.task(tid)["extra"]
        self.assertEqual(len(extra[loop.ROUND_SETTLE_KEY]), 1)

    def test_task_that_left_running_is_not_settled(self):
        for index, status in enumerate(("in_review", "needs_user")):
            with self.subTest(status=status):
                tid = self._stuck("tasksettlemoved" + str(index).zfill(6),
                                  run_status="finished")
                conn = db.connect(self.db_path)
                try:
                    with db.write_txn(conn):
                        db.update_task(conn, tid, util.now_iso(), status=status)
                finally:
                    conn.close()
                summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
                actions = self._actions(tid, summary)
                self.assertFalse(any(a["action"] == decide.SETTLE_ROUND for a in actions))
                self.assertNotIn(loop.ROUND_SETTLE_KEY, self.task(tid)["extra"])

    def test_settle_and_business_tick_deduplicate_within_one_loop(self):
        tid = self._stuck("tasksettletopup00001", run_status="finished")
        summary = loop.Reconciler(self.db_path, mode="live", enable_tick=True).reconcile_once()
        self.assertEqual(len(self._monitor_requests(tid)), 1)
        self.assertEqual(self._actions(tid, summary)[0]["action"], decide.SETTLE_ROUND)
        tick = next(a for a in summary["actions"] if a["action"] == "business_tick")
        self.assertEqual(tick["created"], 0)
        self.assertEqual(tick["skipped_requests"],
                         [{"kind": "monitor", "task_id": tid, "reason": "active"}])
        self.assertEqual(len(self.task(tid)["extra"][loop.ROUND_SETTLE_KEY]), 1)

    def test_still_running_round_keeps_waiting(self):
        tid = self._stuck("tasksettlerun0000001", status="running",
                          ended=False, pid=os.getpid())
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        actions = self._actions(tid, summary)
        self.assertEqual(actions[0]["action"], decide.AWAIT)
        # The steady decision carries no reason: the round is simply awaited.
        self.assertEqual(actions[0]["reason"], "")
        self.assertEqual(self._monitor_requests(tid), [])

    def test_budget_exhaustion_blocks_without_reenqueue(self):
        tid = self._stuck("tasksettlecap0000001", settles=[
            {"run_id": "runsettle0001", "exit_code": 0,
             "at": "2026-10-11T20:15:00+00:00"},
            {"run_id": "runsettle0001", "exit_code": 0,
             "at": "2026-10-11T20:16:00+00:00"},
        ])
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        action = self._actions(tid, summary)[0]
        self.assertEqual(action["action"], decide.BLOCK)
        self.assertEqual(action["reason"], "round_settle_exhausted")
        self.assertTrue(action["attention"])
        self.assertEqual(action["ladder"], 4)
        # No further automatic enqueue: the task cannot spin forever.
        self.assertEqual(self._monitor_requests(tid), [])
        extra = self.task(tid)["extra"]
        self.assertEqual(len(extra[loop.ROUND_SETTLE_KEY]), 2)

    def test_second_tick_dedups_the_settle_request(self):
        tid = self._stuck("tasksettlededup00001")
        rec = loop.Reconciler(self.db_path, mode="live")
        rec.reconcile_once()
        summary = rec.reconcile_once()
        self.assertEqual(len(self._monitor_requests(tid)), 1)
        actions = self._actions(tid, summary)
        self.assertEqual(actions[0]["action"], decide.AWAIT)

    def test_shadow_mode_computes_but_writes_nothing(self):
        tid = self._stuck("tasksettleshdw000001")
        summary = loop.Reconciler(self.db_path, mode="shadow", force_mode=True).reconcile_once()
        action = self._actions(tid, summary)[0]
        self.assertEqual(action["action"], decide.SETTLE_ROUND)
        self.assertIs(action["executed"], False)
        self.assertEqual(action["command"], "enqueue")
        self.assertEqual(self._monitor_requests(tid), [])
        extra = self.task(tid)["extra"]
        self.assertNotIn(loop.ROUND_SETTLE_KEY, extra)


if __name__ == "__main__":
    unittest.main()
