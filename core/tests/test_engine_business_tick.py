"""Business-cycle top-up on an engine loop, using isolated DB and fake workers."""
from datetime import timedelta
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import core, db, process_owner, util
from kanpilot.engine import health, loop


class EngineBusinessTickTests(KanpilotTestCase):
    def _task(self, task_id="taskbusinesstick00001", *, status="running",
              project_id="businessproject00001", managed=False, manual=False,
              save_state="active"):
        info = self.make_ready_task(task_id=task_id, project_id=project_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.update_task(conn, tid, util.now_iso(), status=status,
                               save_state=save_state)
                if managed or manual:
                    core.update_automation(conn, db.require_task(conn, tid),
                        util.now_iso(), adopted_at=util.now_iso(), manual_takeover=manual)
        finally:
            conn.close()
        return tid

    def test_running_queue_is_topped_up_and_spawned_same_loop(self):
        tid = self._task()
        spawned = []
        rec = loop.Reconciler(self.db_path, mode="live", enable_spawn=True,
            spawn=lambda stage, path, iid: spawned.append(stage))
        rec.enable_tick = True  # Also runs against the unfixed baseline.
        summary = rec.reconcile_once()
        monitors = [r for r in self.requests(status="queued")
                    if r["kind"] == "monitor" and r["task_id"] == tid]
        self.assertEqual(len(monitors), 1)
        self.assertEqual(spawned, ["monitor"])
        action = next(a for a in summary["actions"] if a["action"] == "business_tick")
        self.assertTrue(action["executed"])
        self.assertEqual(action["created"], 1)
        self.assertEqual(action["created_requests"][0]["task_id"], tid)

    def _mapping(self, modes):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "project_mappings", {"version": 1,
                    "revision": 1, "items": {pid: {"engine_mode": mode,
                    "enabled": False} for pid, mode in modes.items()}})
        finally:
            conn.close()

    def _rec(self, **options):
        return loop.Reconciler(self.db_path, enable_tick=True, **options)

    def _tick_action(self, summary):
        return next(a for a in summary["actions"] if a["action"] == "business_tick")

    def _clear_observations(self):
        # Publishing later fixtures claims a planning request, whose unchanged
        # inline tick can create observations for earlier running fixtures.
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute("DELETE FROM requests WHERE kind IN ('monitor','review')")
        finally:
            conn.close()

    def test_interval_is_applied_using_the_core_command_clock(self):
        tid = self._task()
        now = util.now_dt()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "monitor_interval_seconds", 900)
                previous, _ = db.create_request(conn, util.to_iso(now), "monitor",
                    f"monitor:{tid}", task_id=tid, requested_by="scheduler")
                db.update_request(conn, previous["request_id"], util.to_iso(now),
                    status="succeeded", finished_at=util.to_iso(now))
        finally:
            conn.close()
        rec = self._rec(mode="live")
        with patch("kanpilot.util.now_dt", return_value=now + timedelta(seconds=119)):
            early = self._tick_action(rec.reconcile_once())
        self.assertEqual(early["created"], 0)
        self.assertEqual(early["skipped_requests"][0]["reason"], "interval")
        with patch("kanpilot.util.now_dt", return_value=now + timedelta(seconds=120)):
            due = self._tick_action(rec.reconcile_once())
        self.assertEqual(due["created"], 1)

    def test_three_modes_only_live_is_topped_up(self):
        tids = {}
        for mode in ("legacy", "shadow", "live"):
            tids[mode] = self._task("tasktick" + mode + "000001",
                project_id="projecttick" + mode, managed=True)
        self._clear_observations()
        self._mapping({"projecttick" + mode: mode for mode in tids})
        summary = self._rec(mode="legacy").reconcile_once()
        action = self._tick_action(summary)
        self.assertEqual(action["project_ids"], ["projectticklive"])
        self.assertEqual([r["task_id"] for r in action["created_requests"]], [tids["live"]])
        shadow = [a for a in summary["actions"] if a.get("task_id") == tids["shadow"]]
        self.assertTrue(shadow)
        self.assertTrue(all(a["engine_mode"] == "shadow" and not a.get("executed")
                            for a in shadow))
        self.assertFalse(any(a.get("task_id") == tids["legacy"] for a in summary["actions"]))

    def test_manual_takeover_keeps_observations_but_not_confirmation(self):
        running = self._task("tasktickmanualrun001", manual=True)
        review = self._task("tasktickmanualrev001", status="in_review", manual=True)
        ready = self._task("tasktickmanualrdy001", status="ready", manual=True)
        self._clear_observations()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_extra(conn, db.require_task(conn, ready), util.now_iso(),
                             plan_execution_hold=True)
        finally:
            conn.close()
        summary = self._rec(mode="live").reconcile_once()
        created = {(r["kind"], r["task_id"]) for r in self._tick_action(summary)["created_requests"]}
        self.assertEqual(created, {("monitor", running), ("review", review)})
        self.assertFalse(any(a.get("task_id") in (ready, running, review)
                             for a in summary["actions"]))
        self.assertTrue(self.task(ready)["extra"]["plan_execution_hold"])
        self.assertFalse(any(r["kind"] == "dispatch" for r in self.requests(status="queued")))

    def test_errors_do_not_block_spawn_and_are_redacted(self):
        tid = self._task(status="in_review")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.create_request(conn, util.now_iso(), "review", f"review:{tid}",
                                  task_id=tid, requested_by="scheduler")
        finally:
            conn.close()
        for error in (RuntimeError("Authorization: Bearer tick-secret-12345"),
                      util.KanpilotError("busy", "token=tick-secret-12345")):
            with self.subTest(error=type(error).__name__):
                spawned, logs = [], []
                def failing(command, payload):
                    raise error
                summary = self._rec(mode="live", run_command=failing,
                    enable_spawn=True, log=logs.append,
                    spawn=lambda stage, path, iid: spawned.append(stage)).reconcile_once()
                action = self._tick_action(summary)
                self.assertFalse(action["executed"])
                self.assertIn("error", action)
                self.assertNotIn("tick-secret-12345", action["detail"])
                self.assertNotIn("tick-secret-12345", str(logs))
                self.assertIn("业务 tick", logs[0])
                self.assertEqual(spawned, ["review"])

    def test_scope_query_and_malformed_result_are_caught(self):
        from unittest.mock import Mock
        rec = self._rec(mode="live")
        broken = Mock()
        broken.execute.side_effect = RuntimeError("scope failed")
        action = rec.business_tick_step(broken, {})
        self.assertEqual(action["error"], "internal_error")
        tid = self._task()
        spawned = []
        summary = self._rec(mode="live", enable_spawn=True,
            run_command=lambda command, payload: {"unexpected": True},
            spawn=lambda stage, path, iid: spawned.append(stage)).reconcile_once()
        self.assertEqual(self._tick_action(summary)["error"], "internal_error")
        self.assertEqual(next(a for a in summary["actions"] if a["action"] == "spawn")["spawned"], [])
        self.assertEqual(self.task(tid)["status"], "running")

    def test_consecutive_loops_deduplicate_active_monitor(self):
        self._task()
        rec = self._rec(mode="live")
        self.assertEqual(self._tick_action(rec.reconcile_once())["created"], 1)
        second = self._tick_action(rec.reconcile_once())
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped_requests"][0]["reason"], "active")

    def test_default_constructor_does_not_tick(self):
        self._task()
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        self.assertFalse(any(a["action"] == "business_tick" for a in summary["actions"]))
        self.assertEqual(self.requests(status="queued"), [])

    def test_engine_run_tick_defaults_to_spawn_with_explicit_override(self):
        for payload, expected in (({}, True), ({"spawn": False}, False),
            ({"spawn": True}, True), ({"spawn": False, "tick": True}, True),
            ({"spawn": True, "tick": False}, False)):
            with self.subTest(payload=payload):
                with patch("kanpilot.engine.loop.Reconciler") as constructor:
                    constructor.return_value.run.return_value = {"ticks": 0}
                    self.call("engine-run", payload)
                self.assertIs(constructor.call_args.kwargs["enable_tick"], expected)

    def test_no_live_projects_skips_core_command(self):
        self._task()
        for mode in ("legacy", "shadow"):
            with self.subTest(mode=mode):
                from unittest.mock import Mock
                command = Mock()
                summary = self._rec(mode=mode, run_command=command).reconcile_once()
                self.assertEqual(self._tick_action(summary)["reason"], "no_live_projects")
                command.assert_not_called()

    def test_archived_tasks_do_not_provide_scope_or_get_observations(self):
        archived = self._task(save_state="archived")
        self.assertEqual(self._tick_action(self._rec(mode="live").reconcile_once())["reason"],
                         "no_live_projects")
        self._task("tasktickactive000001")
        self._clear_observations()
        action = self._tick_action(self._rec(mode="live").reconcile_once())
        self.assertEqual(action["created"], 1)
        self.assertFalse(any(r["task_id"] == archived for r in action["created_requests"]))

    def test_process_default_and_force_match_claim_scope(self):
        self._task()
        # Unmapped projects use the process default; no preflight gates observation.
        self.assertEqual(self._tick_action(self._rec(mode="live").reconcile_once())["created"], 1)
        self._clear_observations()
        self._mapping({"businessproject00001": "legacy"})
        self.assertEqual(self._tick_action(self._rec(mode="live").reconcile_once())["reason"],
                         "no_live_projects")
        self.assertEqual(self._tick_action(self._rec(mode="live", force_mode=True).reconcile_once())["created"], 1)
        self._clear_observations()
        self._mapping({"businessproject00001": "live"})
        self.assertEqual(self._tick_action(self._rec(mode="legacy", force_mode=True).reconcile_once())["reason"],
                         "no_live_projects")

    def test_failed_tick_still_renews_lease_without_backoff(self):
        self._task()
        sleeps = []
        def failing(command, payload):
            raise RuntimeError("tick unavailable")
        rec = self._rec(mode="live", force_mode=True, run_command=failing)
        with patch("kanpilot.engine.health.renew_lease", wraps=health.renew_lease) as renew:
            result = rec.run(instance_id="tick-test-engine", max_ticks=2, sleeper=sleeps.append)
        self.assertEqual(result["failures"], 0)
        self.assertEqual(renew.call_count, 2)
        self.assertEqual(sleeps, [60, 60])
        conn = db.connect(self.db_path)
        try:
            entry = db.get_setting(conn, process_owner.SETTING)["tick-test-engine"]
        finally:
            conn.close()
        self.assertEqual(entry["mode"], "live")
        self.assertIs(entry["force"], True)
        self.assertEqual(entry["state"], "stopped")


class BusinessTickFreshnessTests(KanpilotTestCase):
    NOW = util.parse_ts("2026-10-15T02:00:00Z")

    def _seed_task(self, *, status="running", save_state="active"):
        tid = self.make_ready_task(task_id="tasktickhealth000001",
                                  project_id="projecttickhealth001")["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.update_task(conn, tid, util.to_iso(self.NOW), status=status,
                               save_state=save_state)
                db.set_setting(conn, "last_tick_at", None)
        finally:
            conn.close()
        return tid

    def _lease(self, *, mode="live", force=False):
        import os
        conn = db.connect(self.db_path)
        try:
            health.acquire_or_renew_lease(conn, util.to_iso(self.NOW),
                instance_id="tick-health", stage=None, pid=os.getpid(),
                lease_seconds=1200, mode=mode, force=force)
        finally:
            conn.close()

    def _check(self, last_tick=None):
        from types import SimpleNamespace
        from kanpilot import cmd_read
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "last_tick_at", last_tick)
            with patch("kanpilot.util.now_dt", return_value=self.NOW):
                return cmd_read._doctor_business_tick(SimpleNamespace(conn=conn, now=self.NOW))
        finally:
            conn.close()

    def _mapping(self, mode):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "project_mappings", {"revision": 1, "items":
                    {"projecttickhealth001": {"engine_mode": mode, "enabled": False}}})
        finally:
            conn.close()

    def test_status_reports_tick_age_and_preserves_invalid_clock(self):
        conn = db.connect(self.db_path)
        try:
            for value, expected in ((None, None), ("invalid", None),
                (util.to_iso(self.NOW - timedelta(seconds=50)), 50),
                (util.to_iso(self.NOW + timedelta(seconds=50)), 0)):
                with self.subTest(value=value):
                    with db.write_txn(conn):
                        db.set_setting(conn, "last_tick_at", value)
                    with patch("kanpilot.util.now_dt", return_value=self.NOW):
                        status = health.engine_status(conn, self.NOW)
                    self.assertEqual(status["business_tick"],
                        {"last_tick_at": value, "age_seconds": expected})
        finally:
            conn.close()

    def test_freshness_boundary_and_missing_invalid_time(self):
        self._seed_task()
        self._lease()
        for age, expected in ((0, True), (300, True), (301, False), (-5, True)):
            with self.subTest(age=age):
                check = self._check(util.to_iso(self.NOW - timedelta(seconds=age)))
                self.assertIs(check["ok"], expected)
                if not expected:
                    self.assertIn("301", check["detail"])
                    self.assertIn("business_tick", check["detail"])
        for invalid in (None, "", "not-a-timestamp", 42, {}):
            with self.subTest(invalid=invalid):
                self.assertFalse(self._check(invalid)["ok"])

    def test_no_engine_or_no_observation_tasks_are_not_failures(self):
        self.assertTrue(self._check()["ok"])
        self._lease()
        check = self._check()
        self.assertTrue(check["ok"])
        self.assertIn("没有需观察", check["detail"])

    def test_only_active_saved_running_or_review_tasks_require_clock(self):
        tid = self._seed_task(save_state="archived")
        self._lease()
        self.assertTrue(self._check()["ok"])
        conn = db.connect(self.db_path)
        try:
            for status, expected in (("ready", True), ("in_review", False),
                                     ("running", False), ("done", True)):
                with self.subTest(status=status):
                    with db.write_txn(conn):
                        db.update_task(conn, tid, util.to_iso(self.NOW),
                                       status=status, save_state="active")
                    self.assertIs(self._check()["ok"], expected)
        finally:
            conn.close()

    def test_effective_mode_uses_instance_default_mapping_and_force(self):
        self._seed_task()
        scenarios = ((None, "legacy", False, True),
                     (None, "live", False, False),
                     ("live", "legacy", False, False),
                     ("legacy", "live", False, True),
                     ("legacy", "live", True, False),
                     ("live", "legacy", True, True),
                     ("shadow", "live", False, True))
        for mapping, mode, force, expected in scenarios:
            with self.subTest(mapping=mapping, mode=mode, force=force):
                self._mapping(mapping)
                self._lease(mode=mode, force=force)
                self.assertIs(self._check()["ok"], expected)

    def test_legacy_lease_defaults_to_legacy_without_force(self):
        self._seed_task()
        self._lease()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                instances = db.get_setting(conn, process_owner.SETTING)
                instances["tick-health"].pop("mode")
                instances["tick-health"].pop("force")
                db.set_setting(conn, process_owner.SETTING, instances)
            self.assertTrue(self._check()["ok"])
            self._mapping("live")
            self.assertFalse(self._check()["ok"])
        finally:
            conn.close()

    def test_expired_dead_and_stopped_engines_do_not_require_clock(self):
        self._seed_task()
        for state in ("expired", "dead", "stopped"):
            with self.subTest(state=state):
                self._lease()
                conn = db.connect(self.db_path)
                try:
                    with db.write_txn(conn):
                        instances = db.get_setting(conn, process_owner.SETTING)
                        entry = instances["tick-health"]
                        if state == "expired":
                            entry["lease_expires_at"] = util.to_iso(self.NOW - timedelta(seconds=1))
                        elif state == "dead":
                            entry.update(started_seconds=1, started_microseconds=1)
                        else:
                            entry["state"] = "stopped"
                        db.set_setting(conn, process_owner.SETTING, instances)
                    self.assertTrue(self._check()["ok"])
                finally:
                    conn.close()

    def test_renew_preserves_instance_mode_and_force(self):
        self._lease(mode="shadow", force=True)
        conn = db.connect(self.db_path)
        try:
            health.renew_lease(conn, util.to_iso(self.NOW + timedelta(seconds=60)),
                              instance_id="tick-health", lease_seconds=1200)
            entry = db.get_setting(conn, process_owner.SETTING)["tick-health"]
            self.assertEqual(entry["mode"], "shadow")
            self.assertIs(entry["force"], True)
        finally:
            conn.close()

    def test_doctor_includes_business_tick_check(self):
        self._seed_task()
        self._lease()
        with patch("kanpilot.util.now_dt", return_value=self.NOW):
            result = self.call("doctor", {})
        check = next(c for c in result["checks"] if c["name"] == "business_tick_fresh")
        self.assertFalse(check["ok"])
        self.assertIn("business_tick", check["detail"])
