"""A12: per-project engine_mode (legacy|shadow|live) in one engine process.

Three projects configured legacy/shadow/live share one DB; the reconciler must
drive only live, log-only shadow, and never touch legacy — plus the safe
default (unset → process default; process default legacy leaves old projects
alone) and the force override.

Run: ``cd core && python3 -m pytest tests/test_engine_modes.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import loop  # noqa: E402


class TestEngineModes(KanpilotTestCase):
    # All projects share the "Feedback" repo (make_ready_task's plan/task repo
    # pairing); they differ by project_id, which is what engine_mode keys on.
    REPO = "Feedback"

    def _setup(self, project_id, task_id, engine_mode=None):
        info = self.make_ready_task(task_id=task_id, project_name=self.REPO,
                                    project_id=project_id)
        tid = info["remote"]["id"]
        rev = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": project_id, "repository": str(self.repos_root / self.REPO),
            "enabled": True, "expected_revision": rev})
        if engine_mode is not None:
            self.call("set-project-engine-mode",
                      {"project_id": project_id, "engine_mode": engine_mode})
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(), adopted_at=util.now_iso())
        finally:
            conn.close()
        return tid

    def _active_dispatch(self, task_id):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT request_id FROM requests WHERE task_id=? AND kind='dispatch' "
                "AND status IN ('queued','claimed')", (task_id,)).fetchall()
        finally:
            conn.close()

    def _automation_of(self, project_id):
        for item in self.call("get-project-mappings", {})["items"]:
            if item["project_id"] == project_id:
                return item.get("automation") or {}
        return {}

    def test_three_modes_one_db(self):
        legacy = self._setup("projlegacy00000000000a", "taskmodeleg000000000001", engine_mode="legacy")
        shadow = self._setup("projshadow00000000000a", "taskmodeshd000000000001", engine_mode="shadow")
        live = self._setup("projlive000000000000a", "taskmodeliv000000000001", engine_mode="live")
        summary = loop.Reconciler(self.db_path, mode="live", force_mode=False).reconcile_once()
        actions = summary["actions"]
        acted = {a["task_id"] for a in actions}

        # legacy: filtered before decide -> never acted, zero writes.
        self.assertNotIn(legacy, acted)
        self.assertEqual(self._active_dispatch(legacy), [])

        # shadow: decision logged (engine_mode=shadow), zero writes.
        shadow_actions = [a for a in actions if a["task_id"] == shadow]
        self.assertTrue(shadow_actions)
        self.assertTrue(all(a["engine_mode"] == "shadow" and not a.get("executed")
                            for a in shadow_actions))
        self.assertEqual(self._active_dispatch(shadow), [])

        # live: driven -> dispatch request created.
        self.assertEqual(len(self._active_dispatch(live)), 1)

    def test_default_legacy_preserves_old_projects(self):
        # old project: mapped + automation-managed, but engine_mode never set.
        old = self._setup("projold0000000000000a", "taskmodeold000000000001", engine_mode=None)
        # process default legacy -> the old project is not driven.
        loop.Reconciler(self.db_path, mode="legacy").reconcile_once()
        self.assertEqual(self._active_dispatch(old), [])
        # process default live -> the old project falls back to live and is driven.
        loop.Reconciler(self.db_path, mode="live").reconcile_once()
        self.assertEqual(len(self._active_dispatch(old)), 1)

    def test_force_mode_overrides_project_config(self):
        tid = self._setup("projforce00000000000a", "taskmodefrc000000000001", engine_mode="legacy")
        # force live drives even a legacy-configured project (drills/debug).
        loop.Reconciler(self.db_path, mode="live", force_mode=True).reconcile_once()
        self.assertEqual(len(self._active_dispatch(tid)), 1)

    def test_set_project_engine_mode_persists_and_validates(self):
        pid = "projpersist000000000a"
        self._setup(pid, "taskmodeprs000000000001")
        exc = self.call_err("set-project-engine-mode",
                            {"project_id": pid, "engine_mode": "bogus"})
        self.assertEqual(exc.code, "bad_request")
        rev = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-engine-mode",
                  {"project_id": pid, "engine_mode": "shadow", "expected_revision": rev})
        self.assertEqual(self._entry_of(pid).get("engine_mode"), "shadow")
        # CAS: stale revision -> conflict.
        exc = self.call_err("set-project-engine-mode",
                            {"project_id": pid, "engine_mode": "live",
                             "expected_revision": 0})
        self.assertEqual(exc.code, "conflict")

    def _entry_of(self, project_id):
        import json
        import sqlite3
        row = sqlite3.connect(str(self.db_path)).execute(
            "SELECT value FROM settings WHERE key='project_mappings'").fetchone()
        items = json.loads(row[0])["items"] if row else {}
        return items.get(project_id) or {}

    def test_engine_mode_survives_set_project_automation(self):
        """engine_mode must live at the entry level, not inside ``automation``.

        ``validate_automation`` rebuilds the automation sub-dict from a fixed
        key whitelist, so an engine_mode stored inside it is silently dropped
        by the next ``set-project-automation``. Regression guard.
        """
        pid = "projsurvive000000000a"
        self._setup(pid, "taskmodesrv000000000001")
        self.call("set-project-engine-mode",
                  {"project_id": pid, "engine_mode": "live"})
        self.assertEqual(self._entry_of(pid).get("engine_mode"), "live")
        self.call("set-project-automation",
                  {"project_id": pid, "automation": {"enabled": True}})
        entry = self._entry_of(pid)
        self.assertEqual(entry.get("engine_mode"), "live")
        self.assertNotIn("engine_mode", entry.get("automation") or {})
        self.assertEqual(loop.Reconciler(self.db_path, mode="legacy")._project_modes(
            self._conn())[pid], "live")

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
