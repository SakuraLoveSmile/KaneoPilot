"""A14 P0: the engine must not start lifecycle-serial work while the serial is held.

Sandbox evidence §4.1 (``docs/evidence/sandbox-live-20261010.md``): with several
live tasks, the engine spawned a dispatch worker while another task's
``advance-task`` held the global lifecycle serial (``lifecycle_owner``). The
dispatch worker lost the race, and because the protocol mandates
``send_state=unknown`` once begin-send has been attempted, the task was pushed to
needs_user for a send that never happened — tasks 2/4/5 stalled there.

``loop.plan_spawns`` now defers the stages whose workers take that serial
(dispatch via ``dispatch-begin-send``; monitor and delivery via
``advance-task``) while it is held, and ``spawn_step`` records what it deferred.
``plan`` and ``review`` do not take the serial and keep spawning.

Run: ``cd core && python3 -m pytest tests/test_engine_lifecycle_spawn.py -q``.
"""
from __future__ import annotations

import os
import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import loop  # noqa: E402

PROJECT = "example-project-05"


class TestPlanSpawnsLifecycle(unittest.TestCase):
    def test_serial_stages_deferred_while_busy(self):
        queued = {"dispatch": 1, "monitor": 1, "delivery": 1, "plan": 1, "review": 1}
        stages = loop.plan_spawns(queued, set(), foreign_active=False,
                                  lifecycle_busy=True)
        # 持串行位的三个 stage 让行；不持的照旧。
        self.assertEqual(stages, ["review", "plan"])

    def test_default_behaviour_unchanged(self):
        queued = {"dispatch": 1, "monitor": 1, "delivery": 1, "plan": 1, "review": 1}
        self.assertEqual(
            loop.plan_spawns(queued, set(), foreign_active=False),
            ["dispatch", "monitor", "review", "delivery", "plan"])
        self.assertEqual(
            loop.plan_spawns(queued, set(), foreign_active=False, lifecycle_busy=False),
            ["dispatch", "monitor", "review", "delivery", "plan"])

    def test_busy_does_not_override_other_gates(self):
        # 已有 live runner 的 stage 本来就不起；foreign scheduler 仍然全禁。
        self.assertEqual(
            loop.plan_spawns({"review": 1}, {"review"}, foreign_active=False,
                             lifecycle_busy=True), [])
        self.assertEqual(
            loop.plan_spawns({"dispatch": 1}, set(), foreign_active=True,
                             lifecycle_busy=True), [])

    def test_serial_stage_set_is_exactly_the_remote_writers(self):
        self.assertEqual(loop.LIFECYCLE_SERIAL_STAGES,
                         frozenset({"dispatch", "monitor", "delivery"}))
        for stage in loop.LIFECYCLE_SERIAL_STAGES:
            self.assertIn(stage, loop.STAGES_WITH_WORKERS)


class TestLifecycleBusy(KanpilotTestCase):
    def _conn(self):
        return db.connect(self.db_path)

    def test_reads_the_setting(self):
        conn = self._conn()
        try:
            self.assertFalse(loop.Reconciler.lifecycle_busy(conn))
            with db.write_txn(conn):
                db.set_setting(conn, "lifecycle_owner", "advance-task:abc")
            self.assertTrue(loop.Reconciler.lifecycle_busy(conn))
            with db.write_txn(conn):
                db.set_setting(conn, "lifecycle_owner", "")
            self.assertFalse(loop.Reconciler.lifecycle_busy(conn))
        finally:
            conn.close()

    def test_release_clears_it(self):
        conn = self._conn()
        try:
            with db.write_txn(conn):
                core.acquire_lifecycle(conn, "begin-send:run-1")
            self.assertTrue(loop.Reconciler.lifecycle_busy(conn))
            with db.write_txn(conn):
                core.release_lifecycle(conn, "begin-send:run-1")
            self.assertFalse(loop.Reconciler.lifecycle_busy(conn))
        finally:
            conn.close()


class TestSpawnStepLifecycle(KanpilotTestCase):
    def _adopt_live(self, task_id):
        info = self.make_ready_task(task_id=task_id, project_name="Feedback",
                                    project_id=PROJECT)
        tid = info["remote"]["id"]
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT, "repository": str(self.repos_root / "Feedback"),
            "enabled": True, "expected_revision": revision})
        self.call("set-project-engine-mode",
                  {"project_id": PROJECT, "engine_mode": "live"})
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
        finally:
            conn.close()
        return tid

    def _queue(self, task_id, kind):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.create_request(conn, util.now_iso(), kind, f"{kind}:{task_id}",
                                  task_id=task_id, payload={}, requested_by="engine")
        finally:
            conn.close()

    def _hold_serial(self, owner="advance-task:other-task"):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "lifecycle_owner", owner)
        finally:
            conn.close()

    def _spawn(self, spawned):
        rec = loop.Reconciler(self.db_path, mode="live", enable_spawn=True,
                              spawn=lambda stage, db_path, iid: spawned.append(stage))
        conn = db.connect(self.db_path)
        try:
            return rec.spawn_step(conn, {PROJECT: "live"})
        finally:
            conn.close()

    def test_dispatch_deferred_while_serial_held(self):
        tid = self._adopt_live("tasklifecyc000000001")
        self._queue(tid, "dispatch")
        self._hold_serial()
        spawned = []
        result = self._spawn(spawned)
        self.assertEqual(spawned, [])
        self.assertEqual(result["spawned"], [])
        self.assertEqual(result["deferred"]["reason"], "lifecycle_serial_busy")
        self.assertEqual(result["deferred"]["stages"], ["dispatch"])
        # 请求仍在队列：下一 tick 串行位释放后重试。
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status FROM requests WHERE task_id=? AND kind='dispatch'",
                (tid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "queued")

    def test_spawns_once_serial_released(self):
        tid = self._adopt_live("tasklifecyc000000002")
        self._queue(tid, "dispatch")
        self._hold_serial()
        self.assertEqual(self._spawn([])["spawned"], [])
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                core.release_lifecycle(conn, "advance-task:other-task")
        finally:
            conn.close()
        spawned = []
        result = self._spawn(spawned)
        self.assertEqual(result["spawned"], ["dispatch"])
        self.assertNotIn("deferred", result)

    def test_non_serial_stages_still_spawn_while_busy(self):
        tid = self._adopt_live("tasklifecyc000000003")
        self._queue(tid, "plan")
        self._hold_serial()
        spawned = []
        result = self._spawn(spawned)
        self.assertEqual(result["spawned"], ["plan"])
        self.assertNotIn("deferred", result)

    def test_already_live_stage_is_not_reported_deferred(self):
        tid = self._adopt_live("tasklifecyc000000004")
        self._queue(tid, "dispatch")
        self._hold_serial()
        # 该 stage 已有 live runner：本就不起，也不算「被串行位推迟」。
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                runs = db.get_setting(conn, "executor_runs") or {}
                # 用本进程 pid：_live_runner_stages 只认存活 pid。
                runs["dispatch"] = {"pid": os.getpid()}
                db.set_setting(conn, "executor_runs", runs)
        finally:
            conn.close()
        spawned = []
        result = self._spawn(spawned)
        self.assertEqual(result["spawned"], [])
        self.assertNotIn("deferred", result)

    def test_reconcile_once_reports_deferred_spawn(self):
        tid = self._adopt_live("tasklifecyc000000005")
        self._queue(tid, "dispatch")
        self._hold_serial()
        spawned = []
        rec = loop.Reconciler(self.db_path, mode="live", enable_spawn=True,
                              spawn=lambda stage, db_path, iid: spawned.append(stage))
        summary = rec.reconcile_once()
        spawn_actions = [a for a in summary["actions"] if a.get("action") == "spawn"]
        self.assertTrue(spawn_actions)
        self.assertEqual(spawned, [])
        self.assertEqual(spawn_actions[0]["deferred"]["stages"], ["dispatch"])
        self.assertEqual(spawn_actions[0]["deferred"]["reason"],
                         "lifecycle_serial_busy")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
