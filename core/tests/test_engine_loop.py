"""Integration tests for the engine lease/status (health) and reconciler (loop).

Run: ``cd core && python3 -m pytest tests/test_engine_loop.py -q``.
"""
from __future__ import annotations

import json
import unittest
import uuid
from datetime import timedelta

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import health, loop  # noqa: E402


def _iid():
    return str(uuid.uuid4())


class TestEngineLease(KanpilotTestCase):
    def _conn(self):
        return db.connect(self.db_path)

    def test_acquire_then_status_shape(self):
        iid = _iid()
        conn = self._conn()
        try:
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=iid, stage="dispatch",
                pid=4242, lease_seconds=60)
        finally:
            conn.close()
        conn = self._conn()
        try:
            status = health.engine_status(conn, util.now_dt())
        finally:
            conn.close()
        self.assertEqual(len(status["instances"]), 1)
        inst = status["instances"][0]
        self.assertEqual(inst["instance_id"], iid)
        self.assertEqual(inst["stage"], "dispatch")
        self.assertEqual(inst["pid"], 4242)
        self.assertIn("lease_expires_at", inst)
        self.assertEqual(inst["owned_workers"], [])

    def test_single_instance_refuses_second_active(self):
        a, b = _iid(), _iid()
        conn = self._conn()
        try:
            health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=a,
                                          stage=None, pid=1, lease_seconds=60)
            with self.assertRaises(util.KanpilotError) as ctx:
                health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=b,
                                              stage=None, pid=2, lease_seconds=60)
            self.assertEqual(ctx.exception.code, "conflict")
        finally:
            conn.close()

    def test_expired_lease_is_taken_over(self):
        a, b = _iid(), _iid()
        conn = self._conn()
        try:
            health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=a,
                                          stage=None, pid=1, lease_seconds=60)
            past = (util.now_dt() - timedelta(seconds=120)).isoformat()
            with db.write_txn(conn):
                instances = db.get_setting(conn, "executor_instances")
                instances[a]["lease_expires_at"] = past
                db.set_setting(conn, "executor_instances", instances)
            health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=b,
                                          stage=None, pid=2, lease_seconds=60)
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 1)
        finally:
            conn.close()

    def test_engine_status_command_empty(self):
        self.assertEqual(self.call("engine-status", {}), {"instances": []})

    def test_engine_status_command_lists_lease(self):
        iid = _iid()
        conn = self._conn()
        try:
            health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=iid,
                                          stage="monitor", pid=7, lease_seconds=60)
        finally:
            conn.close()
        result = self.call("engine-status", {})
        self.assertEqual(len(result["instances"]), 1)
        self.assertEqual(result["instances"][0]["instance_id"], iid)

    def test_engine_two_token_status(self):
        code, out, _ = self.call_main(["engine", "status", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"instances": []})

    def test_engine_requires_subcommand(self):
        code, _, _ = self.call_main(["engine", "--db", str(self.db_path)])
        self.assertEqual(code, 1)
        code, _, _ = self.call_main(["engine", "bogus", "--db", str(self.db_path)])
        self.assertEqual(code, 1)


class TestReconcile(KanpilotTestCase):
    def _automate(self, task_id, status=None, **automation):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso(), **automation)
                if status:
                    conn.execute("UPDATE tasks SET status=? WHERE task_id=?",
                                 (status, tid))
        finally:
            conn.close()
        return tid

    def test_ready_task_confirms_dispatch(self):
        tid = self._automate("taskengrdy00000000001")
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        executed = [a for a in summary["actions"] if a.get("executed")]
        self.assertTrue(any(a["task_id"] == tid and a["command"] == "enqueue"
                            for a in executed))
        queued = [r for r in self.requests(status="queued")
                  if r["task_id"] == tid and r["kind"] == "dispatch"]
        self.assertEqual(len(queued), 1)

    def test_second_tick_is_idempotent(self):
        tid = self._automate("taskengidem00000000001")
        rec = loop.Reconciler(self.db_path, mode="live")
        rec.reconcile_once()
        summary = rec.reconcile_once()
        # Second tick performs no new write; the single dispatch request stays.
        self.assertFalse(any(a.get("executed") for a in summary["actions"]))
        queued = [r for r in self.requests(status="queued")
                  if r["task_id"] == tid and r["kind"] == "dispatch"]
        self.assertEqual(len(queued), 1)

    def test_shadow_mode_writes_nothing(self):
        tid = self._automate("taskengshdw00000000001")
        summary = loop.Reconciler(self.db_path, mode="shadow").reconcile_once()
        self.assertTrue(any(a["task_id"] == tid and a["executed"] is False
                            for a in summary["actions"]))
        self.assertEqual([r for r in self.requests(status="queued")
                          if r["task_id"] == tid], [])

    def test_paused_no_action(self):
        self._automate("taskengpaus00000000001")
        self.call("set-paused", {"stage": None, "paused": True})
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        self.assertEqual([a for a in summary["actions"] if a.get("executed")], [])

    def test_needs_user_has_no_auto_action(self):
        tid = self._automate("taskengneedy0000000001", status="needs_user")
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        self.assertEqual([a for a in summary["actions"] if a.get("executed")], [])

    def test_manual_takeover_is_skipped(self):
        self._automate("taskengmanu00000000001", manual_takeover=True)
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        # A taken-over task is not an engine candidate: nothing is scheduled for it.
        self.assertEqual(
            [a for a in summary["actions"]
             if a["task_id"] == "taskengmanu00000000001"], [])


class TestRunLoop(KanpilotTestCase):
    def test_lease_lifecycle_and_release(self):
        rec = loop.Reconciler(self.db_path, mode="shadow", log=lambda m: None)
        result = rec.run(instance_id=_iid(), max_ticks=2, sleeper=lambda s: None)
        self.assertEqual(result["ticks"], 2)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 0)
        finally:
            conn.close()

    def test_backoff_on_tick_failure(self):
        class Boom(loop.Reconciler):
            def reconcile_once(self, now=None):
                raise RuntimeError("boom")

        rec = Boom(self.db_path, mode="shadow", log=lambda m: None)
        sleeps = []
        result = rec.run(instance_id=_iid(), max_ticks=2, sleeper=sleeps.append)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(sleeps, [1.0, 2.0])  # exponential 1s → 2s


class TestIntake(KanpilotTestCase):
    """Intake adoption reuses auto_intake.run_once (live adopts / shadow previews)."""

    def test_intake_disabled_by_default(self):
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        self.assertEqual([a for a in summary["actions"] if a.get("action") == "intake"], [])

    def test_intake_runs_when_enabled_live(self):
        rec = loop.Reconciler(self.db_path, mode="live", enable_intake=True)
        summary = rec.reconcile_once()
        intake = [a for a in summary["actions"] if a.get("action") == "intake"]
        self.assertEqual(len(intake), 1)
        self.assertTrue(intake[0]["executed"])    # live => real pass
        self.assertEqual(intake[0]["adopted"], 0)  # no enabled project in this fixture

    def test_intake_shadow_is_preview_only(self):
        rec = loop.Reconciler(self.db_path, mode="shadow", enable_intake=True)
        summary = rec.reconcile_once()
        intake = [a for a in summary["actions"] if a.get("action") == "intake"]
        self.assertEqual(len(intake), 1)
        self.assertFalse(intake[0]["executed"])   # shadow => dry preview

    def test_intake_throttled_within_interval(self):
        rec = loop.Reconciler(self.db_path, mode="shadow", enable_intake=True,
                              intake_interval_seconds=300)
        first = rec.reconcile_once()
        second = rec.reconcile_once()  # immediate second tick
        self.assertEqual(len([a for a in first["actions"] if a.get("action") == "intake"]), 1)
        self.assertEqual([a for a in second["actions"] if a.get("action") == "intake"], [])


class TestSpawn(KanpilotTestCase):
    """Optional worker spawn (live): queued stages, no double-spawn with the App."""

    def _conn(self):
        return db.connect(self.db_path)

    def test_plan_spawns_pure(self):
        self.assertEqual(loop.plan_spawns({"dispatch": 1}, set(), foreign_active=False), ["dispatch"])
        self.assertEqual(loop.plan_spawns({"dispatch": 1}, {"dispatch"}, foreign_active=False), [])
        self.assertEqual(loop.plan_spawns({"dispatch": 1}, set(), foreign_active=True), [])
        self.assertEqual(loop.plan_spawns({}, set(), foreign_active=False), [])
        self.assertEqual(
            loop.plan_spawns({"delivery": 1, "dispatch": 2}, set(), foreign_active=False),
            ["dispatch", "delivery"])  # order follows STAGES_WITH_WORKERS

    def test_foreign_scheduler_active_distinguishes_app_vs_engine(self):
        conn = self._conn()
        try:
            health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=_iid(),
                                          stage=None, pid=1, lease_seconds=60)
            self.assertFalse(loop.Reconciler.foreign_scheduler_active(conn))  # engine lease
            with db.write_txn(conn):
                inst = db.get_setting(conn, "executor_instances")
                inst["app-instance"] = {"state": "active", "started_at": util.now_iso(), "workers": []}
                db.set_setting(conn, "executor_instances", inst)
            self.assertTrue(loop.Reconciler.foreign_scheduler_active(conn))   # App instance
        finally:
            conn.close()

    def test_spawn_step_spawns_queued_stage(self):
        info = self.make_ready_task(task_id="taskspnA0000000000001")
        tid = info["remote"]["id"]
        conn = self._conn()
        try:
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running', run_id=? WHERE task_id=?", ("r1", tid))
                db.create_request(conn, util.now_iso(), "monitor", f"monitor:{tid}",
                                  task_id=tid, payload={}, requested_by="engine")
        finally:
            conn.close()
        spawned = []
        rec = loop.Reconciler(self.db_path, mode="live", enable_spawn=True,
                              spawn=lambda stage, db_path, iid: spawned.append(stage))
        conn = self._conn()
        try:
            result = rec.spawn_step(conn)
        finally:
            conn.close()
        self.assertTrue(result["executed"])
        self.assertEqual(result["spawned"], ["monitor"])
        self.assertEqual(spawned, ["monitor"])

    def test_spawn_step_skipped_when_app_active(self):
        conn = self._conn()
        try:
            with db.write_txn(conn):
                inst = db.get_setting(conn, "executor_instances") or {}
                inst["app-x"] = {"state": "active", "started_at": util.now_iso(), "workers": []}
                db.set_setting(conn, "executor_instances", inst)
        finally:
            conn.close()
        spawned = []
        rec = loop.Reconciler(self.db_path, mode="live", enable_spawn=True,
                              spawn=lambda stage, db_path, iid: spawned.append(stage))
        conn = self._conn()
        try:
            result = rec.spawn_step(conn)
        finally:
            conn.close()
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "app_scheduler_active")
        self.assertEqual(spawned, [])


if __name__ == "__main__":
    unittest.main()
