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
        self.assertEqual(self._automation_of(pid).get("engine_mode"), "shadow")
        # CAS: stale revision -> conflict.
        exc = self.call_err("set-project-engine-mode",
                            {"project_id": pid, "engine_mode": "live",
                             "expected_revision": 0})
        self.assertEqual(exc.code, "conflict")


if __name__ == "__main__":
    unittest.main()
