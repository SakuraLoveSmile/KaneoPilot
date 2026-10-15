"""Integration tests for the engine lease/status (health) and reconciler (loop).

Run: ``cd core && python3 -m pytest tests/test_engine_loop.py -q``.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, process_owner, util  # noqa: E402
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
        self.assertEqual(self.call("engine-status", {}), {"instances": [], "business_tick": {"last_tick_at": None, "age_seconds": None}})

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
        self.assertEqual(json.loads(out), {"instances": [], "business_tick": {"last_tick_at": None, "age_seconds": None}})

    def test_engine_requires_subcommand(self):
        code, _, _ = self.call_main(["engine", "--db", str(self.db_path)])
        self.assertEqual(code, 1)
        code, _, _ = self.call_main(["engine", "bogus", "--db", str(self.db_path)])
        self.assertEqual(code, 1)


class TestLeaseHolderTakeover(KanpilotTestCase):
    """失联持有者接管（protocol §5.27.1）：pid 经 birth 核验已死 → 放行接管。

    形状：引擎被 SIGKILL（``finally`` 未执行）→ 租约条目停在 active 且
    ``lease_expires_at`` 未到 → 旧口径下任何新引擎都被拒绝，直到租约自然
    过期（沙盒实测 7200 秒堵死验收）。新口径按「持有进程是否经 birth 核验
    存活」判定，并允许把失联条目标记 stopped 后接管。
    """

    def _conn(self):
        return db.connect(self.db_path)

    def _instances(self, conn):
        return db.get_setting(conn, process_owner.SETTING, {})

    def _dead_holder_pid(self):
        """A real pid that has already exited (and its recorded birth)."""
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.kill)
        birth = process_owner.process_birth(proc.pid)
        proc.kill()
        proc.wait()
        return proc.pid, birth

    def _seed_entry(self, conn, instance_id, *, pid, birth=None, lease_seconds=600):
        now = util.now_dt()
        entry = {"state": "active", "instance_id": instance_id, "pid": pid,
                 "stage": None, "started_at": util.to_iso(now),
                 "lease_expires_at": util.plus_seconds(lease_seconds, now),
                 "workers": []}
        if birth:
            entry.update(birth)
        with db.write_txn(conn):
            instances = self._instances(conn)
            instances[instance_id] = entry
            db.set_setting(conn, process_owner.SETTING, instances)
        return entry

    def test_dead_holder_lease_is_taken_over_and_marked_stopped(self):
        pid, birth = self._dead_holder_pid()
        self.assertIsNotNone(birth, "无法读取子进程 birth，测试前提不成立")
        dead, live = _iid(), _iid()
        conn = self._conn()
        try:
            self._seed_entry(conn, dead, pid=pid, birth=birth)
            # 未过期的活跃租约，但持有进程已死 → 放行接管。
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=live, stage=None,
                pid=os.getpid(), lease_seconds=600)
            instances = self._instances(conn)
            self.assertEqual(instances[dead]["state"], "stopped")
            self.assertEqual(instances[dead]["stopped_reason"], "holder_dead")
            self.assertIsNotNone(instances[dead]["stopped_at"])
            # 原 active/未过期字段不被改写（只追加停止标记）。
            self.assertEqual(instances[live]["state"], "active")
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 1)
        finally:
            conn.close()

    def test_live_holder_still_refuses_a_second_engine(self):
        live_a, live_b = _iid(), _iid()
        conn = self._conn()
        try:
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=live_a, stage=None,
                pid=os.getpid(), lease_seconds=600)
            with self.assertRaises(util.KanpilotError) as ctx:
                health.acquire_or_renew_lease(
                    conn, util.now_iso(), instance_id=live_b, stage=None,
                    pid=os.getpid(), lease_seconds=600)
            self.assertEqual(ctx.exception.code, "conflict")
            # 被拒不改写任何条目。
            self.assertEqual(self._instances(conn)[live_a]["state"], "active")
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 1)
        finally:
            conn.close()

    def test_reused_pid_with_foreign_birth_is_not_mistaken_for_alive(self):
        """pid 仍在（本测试进程），但 birth 与记录不符 → 原持有者已死。"""
        stale = _iid()
        conn = self._conn()
        try:
            self._seed_entry(conn, stale, pid=os.getpid(),
                             birth={"started_seconds": 1,
                                    "started_microseconds": 2})
            fresh = _iid()
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=fresh, stage=None,
                pid=os.getpid(), lease_seconds=600)
            self.assertEqual(self._instances(conn)[stale]["stopped_reason"],
                             "holder_dead")
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 1)
        finally:
            conn.close()

    def test_legacy_entry_without_birth_stays_conservative(self):
        """历史条目无 birth → 无法核验存活 → 按未过期拒绝接管（含死 pid）。"""
        pid, _birth = self._dead_holder_pid()
        legacy, fresh = _iid(), _iid()
        conn = self._conn()
        try:
            self._seed_entry(conn, legacy, pid=pid)          # 无 birth
            with self.assertRaises(util.KanpilotError) as ctx:
                health.acquire_or_renew_lease(
                    conn, util.now_iso(), instance_id=fresh, stage=None,
                    pid=os.getpid(), lease_seconds=600)
            self.assertEqual(ctx.exception.code, "conflict")
            self.assertEqual(self._instances(conn)[legacy]["state"], "active")
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 1)
        finally:
            conn.close()

    def test_expired_lease_is_still_taken_over(self):
        """过期租约接管（既有行为，不因 birth 规则回归）。"""
        stale, fresh = _iid(), _iid()
        conn = self._conn()
        try:
            entry = self._seed_entry(conn, stale, pid=os.getpid())
            with db.write_txn(conn):
                instances = self._instances(conn)
                instances[stale] = dict(
                    entry, lease_expires_at=util.to_iso(
                        util.now_dt() - timedelta(seconds=120)))
                db.set_setting(conn, process_owner.SETTING, instances)
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=fresh, stage=None,
                pid=os.getpid(), lease_seconds=600)
            # 过期条目按既有口径不改写（只「跳过」）；只有失联持有者才会被
            # 标记 stopped/holder_dead，两者是可区分的审计事实。
            self.assertEqual(self._instances(conn)[stale]["state"], "active")
            self.assertNotIn("stopped_reason", self._instances(conn)[stale])
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 1)
        finally:
            conn.close()

    def test_status_reports_dead_holder_without_counting_it_active(self):
        pid, birth = self._dead_holder_pid()
        self.assertIsNotNone(birth, "无法读取子进程 birth，测试前提不成立")
        dead = _iid()
        conn = self._conn()
        try:
            self._seed_entry(conn, dead, pid=pid, birth=birth)
            status = health.engine_status(conn, util.now_dt())
            self.assertEqual(len(status["instances"]), 1)
            view = status["instances"][0]
            # 失联条目仍可见（诊断需要），但不计为活动实例。
            self.assertEqual(view["instance_id"], dead)
            self.assertEqual(view["state"], "active")
            self.assertFalse(view["holder_alive"])
            self.assertFalse(view["active"])
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 0)
        finally:
            conn.close()


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

    def test_intake_no_live_projects_is_noop(self):
        rec = loop.Reconciler(self.db_path, mode="live", enable_intake=True)
        summary = rec.reconcile_once()
        intake = [a for a in summary["actions"] if a.get("action") == "intake"]
        self.assertEqual(len(intake), 1)
        # No enabled+live project in this fixture -> no adoption, no writes (A12).
        self.assertFalse(intake[0]["executed"])
        self.assertEqual(intake[0]["reason"], "no_live_projects")
        self.assertEqual(intake[0]["adopted"], 0)

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
            result = rec.spawn_step(conn, {"ryryo1reph1xfemqtwkjyiiz": "live"})
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
            result = rec.spawn_step(conn, {"ryryo1reph1xfemqtwkjyiiz": "live"})
        finally:
            conn.close()
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "app_scheduler_active")
        self.assertEqual(spawned, [])

    def test_dead_app_scheduler_does_not_block_manual_plan(self):
        info = self.make_ready_task(task_id="taskstale000000000001")
        tid = info["remote"]["id"]
        conn = self._conn()
        try:
            with db.write_txn(conn):
                db.create_request(conn, util.now_iso(), "plan", f"plan:{tid}",
                                  task_id=tid, payload={"manual_authorized": True}, requested_by="user")
                db.set_setting(conn, "executor_instances", {
                    _iid(): {"state": "active", "scheduler_pid": 12345,
                             "scheduler_started_seconds": 100,
                             "scheduler_started_microseconds": 200, "workers": []}})
            spawned = []
            rec = loop.Reconciler(self.db_path, mode="legacy",
                                  spawn=lambda stage, *_: spawned.append(stage))
            # Both a dead PID and a reused PID must unblock this exact manual request.
            for birth in (None, {"started_seconds": 101, "started_microseconds": 200}):
                with self.subTest(birth=birth), patch.object(process_owner, "process_birth", return_value=birth), \
                        patch.object(util, "pid_alive", return_value=False):
                    result = rec.spawn_step(conn, {})
                    self.assertEqual(result["spawned"], ["plan"])
            self.assertEqual(spawned, ["plan", "plan"])
        finally:
            conn.close()

    def test_live_idle_app_scheduler_still_blocks_spawn(self):
        conn = self._conn()
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "executor_instances", {
                    _iid(): {"state": "active", "scheduler_pid": 12345,
                             "scheduler_started_seconds": 100,
                             "scheduler_started_microseconds": 200, "workers": []}})
            with patch.object(process_owner, "process_birth", return_value={
                    "started_seconds": 100, "started_microseconds": 200}):
                self.assertTrue(loop.Reconciler.foreign_scheduler_active(conn))
        finally:
            conn.close()

    def test_dead_app_with_live_detached_worker_still_blocks_spawn(self):
        conn = self._conn()
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "executor_instances", {
                    _iid(): {"state": "active", "scheduler_pid": 12345,
                             "scheduler_started_seconds": 100,
                             "scheduler_started_microseconds": 200,
                             "workers": [{"pid": os.getpid(), **process_owner.process_birth(os.getpid())}]}})
            actual_birth = process_owner.process_birth
            with patch.object(process_owner, "process_birth", side_effect=lambda pid:
                              None if pid == 12345 else actual_birth(pid)):
                self.assertTrue(loop.Reconciler.foreign_scheduler_active(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
