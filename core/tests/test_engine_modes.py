"""KP-E01: saved historical modes migrate once; ordinary scheduling is live."""
from __future__ import annotations

import copy
from contextlib import closing
import unittest
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import core, db, engine_claim_scope, project_config, util
from kanpilot.engine import health, loop


class TestEngineModes(KanpilotTestCase):
    REPO = "Feedback"

    def setUp(self):
        super().setUp()
        mocked = patch("kanpilot.automation_policy.automation_preflight", return_value=[])
        mocked.start()
        self.addCleanup(mocked.stop)

    def _setup(self, project_id, task_id, engine_mode=None, *, nested=False):
        info = self.make_ready_task(task_id=task_id, project_name=self.REPO, project_id=project_id)
        tid = info["remote"]["id"]
        self.call("set-project-mapping", {"project_id": project_id,
            "repository": str(self.repos_root / self.REPO), "enabled": True,
            "expected_revision": self.call("get-project-mappings")["revision"]})
        if engine_mode is not None:
            self.seed_engine_mode(project_id, engine_mode, nested=nested)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                core.update_automation(conn, db.require_task(conn, tid), util.now_iso(), adopted_at=util.now_iso())
        finally:
            conn.close()
        self.seed_auto_confirmation(tid)
        return tid

    def _active_dispatch(self, task_id):
        with closing(self._conn()) as conn:
            return conn.execute("SELECT request_id FROM requests WHERE task_id=? AND kind='dispatch' "
                "AND status IN ('queued','claimed')", (task_id,)).fetchall()

    def _entry_of(self, project_id):
        with closing(self._conn()) as conn:
            return project_config.load_raw_mappings(conn)[2].get(project_id) or {}

    def test_historical_modes_are_all_driven_by_default_live(self):
        tasks = [self._setup("project-" + mode, "taskmode-" + mode, mode)
                 for mode in ("legacy", "shadow", "live")]
        summary = loop.Reconciler(self.db_path).reconcile_once()
        self.assertTrue(all(a["engine_mode"] == "live" for a in summary["actions"]))
        for task in tasks:
            self.assertEqual(len(self._active_dispatch(task)), 1)
        self.assertEqual(self._entry_of("project-legacy")["engine_mode"], "legacy")

    def test_missing_and_nested_modes_read_live_without_writes(self):
        self._setup("nested", "taskmode-nested", "shadow", nested=True)
        with closing(self._conn()) as conn:
            before = db.get_setting(conn, "project_mappings")
        items = self.call("get-project-mappings")["items"]
        self.assertTrue(all(item["engine_mode"] == item["effective_engine_mode"] == "live" for item in items))
        with closing(self._conn()) as conn:
            self.assertEqual(db.get_setting(conn, "project_mappings"), before)

    def test_malformed_engine_configuration_fails_explicitly(self):
        for entry in (None, [], False, {"automation": []}, {"automation": False},
                      {"automation": "invalid"}, {"engine_mode": "unknown"},
                      {"engine_mode": "live", "automation": {"engine_mode": "unknown"}}):
            with self.subTest(entry=entry):
                with self.assertRaises(util.KanpilotError) as error:
                    engine_claim_scope.project_mode(entry, "invalid-project")
                self.assertEqual(error.exception.code, "validation_failed")

    def test_explicit_shadow_is_process_wide_without_business_writes(self):
        tasks = [self._setup("diag-" + mode, "taskdiag-" + mode, mode)
                 for mode in ("legacy", "shadow", "live")]
        with closing(self._conn()) as conn:
            before = db.get_setting(conn, "project_mappings")
        rec = loop.Reconciler(self.db_path, mode="shadow", force_mode=True,
            enable_intake=True, enable_spawn=True, enable_tick=True,
            spawn=lambda *a, **k: self.fail("diagnostic spawned a worker"))
        summary = rec.reconcile_once()
        for task in tasks:
            actions = [a for a in summary["actions"] if a.get("task_id") == task]
            self.assertTrue(actions)
            self.assertTrue(all(a["engine_mode"] == "shadow" and not a.get("executed") for a in actions))
            self.assertEqual(self._active_dispatch(task), [])
        rec.run(instance_id="diagnostic-migration", max_ticks=0)
        with closing(self._conn()) as conn:
            self.assertEqual(db.get_setting(conn, "project_mappings"), before)
            self.assertIsNone(db.get_setting(conn, "engine_mode_migration"))

    def test_retired_and_incomplete_process_modes_are_rejected(self):
        for mode, force in (("legacy", False), ("legacy", True), ("shadow", False), ("bogus", True)):
            with self.subTest(mode=mode, force=force):
                with self.assertRaises(util.KanpilotError):
                    loop.Reconciler(self.db_path, mode=mode, force_mode=force)
                self.assertEqual(self.call_err("engine-run", {"mode": mode, "force": force}).code, "bad_request")

    def test_mode_setter_only_accepts_live_and_keeps_revision_cas(self):
        pid = "mode-setter"
        self._setup(pid, "task-mode-setter", "legacy")
        for mode in ("legacy", "shadow", "bogus"):
            self.assertEqual(self.call_err("set-project-engine-mode", {"project_id": pid, "engine_mode": mode}).code,
                             "bad_request")
        revision = self.call("get-project-mappings")["revision"]
        result = self.call("set-project-engine-mode", {"project_id": pid, "expected_revision": revision})
        self.assertEqual(result["engine_mode"], "live")
        self.assertEqual(self._entry_of(pid)["engine_mode"], "live")
        self.assertEqual(self.call_err("set-project-engine-mode", {"project_id": pid,
            "engine_mode": "live", "expected_revision": revision}).code, "conflict")

    def test_live_startup_migrates_once_and_preserves_policy_tasks_and_requests(self):
        self._setup("entry", "task-migrate-entry", "legacy")
        self._setup("nested", "task-migrate-nested", "shadow", nested=True)
        with closing(self._conn()) as conn:
            mappings = db.get_setting(conn, "project_mappings")
            mappings["items"]["entry"]["automation"] = {"enabled": False, "revision": 8,
                "auto_confirm_plan": False, "auto_merge_pr": True, "max_fix_rounds": 1,
                "max_minutes": 12, "max_cost_usd": 4, "release": {"enabled": False}}
            db.set_setting(conn, "project_mappings", mappings)
            db.set_setting(conn, "paused_global", True)
            before = copy.deepcopy(mappings)
            tasks = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY task_id")]
            requests = [tuple(row) for row in conn.execute("SELECT * FROM requests ORDER BY request_id")]
        rec = loop.Reconciler(self.db_path)
        rec.run(instance_id="migrate-first", max_ticks=0)
        with closing(self._conn()) as conn:
            after = db.get_setting(conn, "project_mappings")
            receipt = db.get_setting(conn, "engine_mode_migration")
            self.assertEqual(after["revision"], before["revision"] + 1)
            self.assertEqual(after["items"]["entry"]["automation"], before["items"]["entry"]["automation"])
            self.assertTrue(db.get_setting(conn, "paused_global"))
            self.assertTrue(all(item["engine_mode"] == "live" and "engine_mode" not in (item.get("automation") or {})
                                for item in after["items"].values()))
            self.assertEqual(receipt["from_revision"], before["revision"])
            self.assertEqual(receipt["projects"]["entry"]["engine_mode"], "legacy")
            self.assertEqual(receipt["projects"]["nested"]["automation_engine_mode"], "shadow")
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY task_id")], tasks)
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM requests ORDER BY request_id")], requests)
        rec.run(instance_id="migrate-second", max_ticks=0)
        with closing(self._conn()) as conn:
            self.assertEqual(db.get_setting(conn, "project_mappings"), after)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='project_engine_modes_migrated'").fetchone()[0], 1)
        self.assertEqual(self.call_err("set-project-engine-mode", {"project_id": "entry",
            "engine_mode": "live", "expected_revision": before["revision"]}).code, "conflict")

    def test_invalid_saved_mode_aborts_whole_migration_and_releases_lease(self):
        self._setup("valid", "task-migrate-valid", "legacy")
        self._setup("invalid", "task-migrate-invalid", "bogus")
        with closing(self._conn()) as conn:
            before = db.get_setting(conn, "project_mappings")
        with self.assertRaises(util.KanpilotError):
            loop.Reconciler(self.db_path).run(instance_id="migration-failed", max_ticks=0)
        with closing(self._conn()) as conn:
            self.assertEqual(db.get_setting(conn, "project_mappings"), before)
            self.assertIsNone(db.get_setting(conn, "engine_mode_migration"))
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 0)
        self.assertEqual(self.call_err("get-project-mappings").code, "validation_failed")

    def test_migration_failure_rolls_back_and_releases_lease(self):
        self._setup("rollback", "task-migrate-rollback", "legacy")
        with closing(self._conn()) as conn:
            before = db.get_setting(conn, "project_mappings")
        original = db.set_setting
        def fail_receipt(conn, key, value):
            if key == "engine_mode_migration":
                raise OSError("receipt unavailable")
            return original(conn, key, value)
        with patch.object(db, "set_setting", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                loop.Reconciler(self.db_path).run(instance_id="migration-rollback", max_ticks=0)
        with closing(self._conn()) as conn:
            self.assertEqual(db.get_setting(conn, "project_mappings"), before)
            self.assertEqual(health.active_instance_count(conn, util.now_dt()), 0)

    def test_engine_mode_survives_set_project_automation(self):
        self._setup("policy", "task-migrate-policy", "live")
        self.call("set-project-automation", {"project_id": "policy", "automation": {"enabled": True}})
        self.assertEqual(self._entry_of("policy")["engine_mode"], "live")

    def test_set_project_verify_env_persists_and_rejects_credentials(self):
        pid = "projverifyenv0000000a"
        self._setup(pid, "taskmodevenv0000000001")
        self.call("set-project-verify-env",
                  {"project_id": pid, "verify_env": {"PATH": "/opt/bin", "FOO": "bar"}})
        self.assertEqual(self._entry_of(pid).get("verify_env"),
                         {"PATH": "/opt/bin", "FOO": "bar"})
        # set-project-automation must not drop it (same entry-level reason).
        self.call("set-project-automation",
                  {"project_id": pid, "automation": {"enabled": True}})
        self.assertEqual(self._entry_of(pid).get("verify_env"),
                         {"PATH": "/opt/bin", "FOO": "bar"})
        # credential-ish keys are rejected: credentials live in credential files.
        for key in ("KANEO_TOKEN", "Authorization", "api_key", "SECRET"):
            exc = self.call_err("set-project-verify-env",
                                {"project_id": pid, "verify_env": {key: "x"}})
            self.assertEqual(exc.code, "bad_request", key)
        # non-string values and non-dict payloads are rejected.
        self.assertEqual(
            self.call_err("set-project-verify-env",
                          {"project_id": pid, "verify_env": {"A": 1}}).code,
            "bad_request")
        self.assertEqual(
            self.call_err("set-project-verify-env",
                          {"project_id": pid, "verify_env": ["A"]}).code,
            "bad_request")
        # unknown project -> not_found.
        self.assertEqual(
            self.call_err("set-project-verify-env",
                          {"project_id": "nope", "verify_env": {}}).code,
            "not_found")
        # verdict reads the entry-level value.
        from kanpilot.engine import verdict
        self.assertEqual(verdict.project_verify_env(self._conn(), pid),
                         {"PATH": "/opt/bin", "FOO": "bar"})

    def _conn(self):
        return db.connect(self.db_path)


if __name__ == "__main__":
    unittest.main()
