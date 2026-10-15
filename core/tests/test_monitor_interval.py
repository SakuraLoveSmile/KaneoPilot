"""Legacy seeded interval is interpreted consistently, without DB writes."""

from datetime import timedelta

from helpers import KanpilotTestCase
from kanpilot import cmd_read, db, util


class MonitorIntervalTests(KanpilotTestCase):
    def test_read_interprets_only_legacy_value_without_mutating(self):
        for stored, expected in ((900, 120), (300, 300), (120, 120)):
            with self.subTest(stored=stored):
                conn = db.connect(self.db_path)
                try:
                    db.set_setting(conn, "monitor_interval_seconds", stored)
                    conn.commit()
                    before = conn.total_changes
                    self.assertEqual(util.monitor_interval(conn), expected)
                    self.assertEqual(conn.total_changes, before)
                    self.assertEqual(db.get_setting(conn, "monitor_interval_seconds"), stored)
                finally:
                    conn.close()
                self.assertEqual(self.call("overview", {})["settings"]["monitor_interval_seconds"], expected)

    def test_first_monitor_uses_legacy_interpretation(self):
        conn = db.connect(self.db_path)
        try:
            db.set_setting(conn, "monitor_interval_seconds", 900)
            conn.commit()
        finally:
            conn.close()
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.enqueue_dispatch(task_id)
        claim = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        evidence = self.valid_evidence(self.task(task_id), fixture["plan_path"])
        self.call("dispatch-prepare", {"task_id": task_id, "run_id": claim["run_id"], "evidence": evidence})
        self.call("dispatch-begin-send", {"task_id": task_id, "run_id": claim["run_id"]})
        evidence.update(message_visible=True, session_id="session", session_title="test")
        self.call("dispatch-confirm", {"task_id": task_id, "run_id": claim["run_id"], "evidence": evidence})
        req = next(r for r in self.requests(status="queued") if r["kind"] == "monitor" and r["task_id"] == task_id)
        conn = db.connect(self.db_path)
        try:
            sent_at = db.require_task(conn, task_id)["sent_at"]
        finally:
            conn.close()
        delta = util.parse_ts(req["not_before"]) - util.parse_ts(sent_at)
        self.assertEqual(delta, timedelta(seconds=120))

    def test_tick_respects_effective_interval_for_next_monitor(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        now = util.now_dt()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "monitor_interval_seconds", 900)
                db.update_task(conn, task_id, util.to_iso(now), status="running")
                previous, _ = db.create_request(conn, util.to_iso(now), "monitor",
                    f"monitor:{task_id}", task_id=task_id, requested_by="scheduler")
                db.update_request(conn, previous["request_id"], util.to_iso(now),
                    status="succeeded", finished_at=util.to_iso(now))
            with db.write_txn(conn):
                before = cmd_read.tick_inline(conn, now + timedelta(seconds=119))
            self.assertFalse(any(r["kind"] == "monitor" for r in before["created"]))
            with db.write_txn(conn):
                due = cmd_read.tick_inline(conn, now + timedelta(seconds=120))
            self.assertTrue(any(r["kind"] == "monitor" and r["task_id"] == task_id
                                for r in due["created"]))
            self.assertEqual(db.get_setting(conn, "monitor_interval_seconds"), 900)
        finally:
            conn.close()

    def test_project_filter_limits_monitor_review_and_automation_segments(self):
        from unittest.mock import patch
        from kanpilot import core
        tasks = {}
        for project in ("filteredproject00001", "outsideproject000001"):
            for status in ("running", "in_review", "ready"):
                tid = "taskfilter" + str(len(tasks)).zfill(10)
                self.make_ready_task(task_id=tid, project_id=project)
                tasks[(project, status)] = tid
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                for (project, status), tid in tasks.items():
                    db.update_task(conn, tid, util.now_iso(), status=status)
                    if status == "ready":
                        core.update_automation(conn, db.require_task(conn, tid),
                            util.now_iso(), adopted_at=util.now_iso())
                        db.set_extra(conn, db.require_task(conn, tid), util.now_iso(),
                                     plan_execution_hold=True)
            with patch("kanpilot.automation_policy.automation_preflight", return_value=[]) as preflight:
                result = self.call("tick", {"project_ids": ["filteredproject00001"] * 2})
            self.assertEqual(preflight.call_count, 1)
            self.assertEqual(preflight.call_args.args[1], "filteredproject00001")
            self.assertEqual({(r["kind"], r["task_id"]) for r in result["created"]}, {
                ("monitor", tasks[("filteredproject00001", "running")]),
                ("review", tasks[("filteredproject00001", "in_review")])})
            selected = db.require_task(conn, tasks[("filteredproject00001", "ready")])
            outside = db.require_task(conn, tasks[("outsideproject000001", "ready")])
            self.assertIn("last_skip", core.automation_of(selected))
            self.assertNotIn("last_skip", core.automation_of(outside))
            self.assertIsNotNone(db.get_setting(conn, "last_tick_at"))
        finally:
            conn.close()

    def test_project_filter_applies_to_lost_managed_lease_release(self):
        from kanpilot import core
        tids = []
        for index in range(2):
            tid = "taskfilterlost" + str(index).zfill(6)
            self.make_ready_task(task_id=tid, project_id=f"projectlost{index}")
            tids.append(tid)
        conn = db.connect(self.db_path)
        try:
            requests = []
            with db.write_txn(conn):
                for tid in tids:
                    db.update_task(conn, tid, util.now_iso(), status="running")
                    core.update_automation(conn, db.require_task(conn, tid),
                        util.now_iso(), adopted_at=util.now_iso())
                    req, _ = db.create_request(conn, util.now_iso(), "monitor",
                        f"monitor:{tid}", task_id=tid, requested_by="scheduler")
                    db.update_request(conn, req["request_id"], util.now_iso(),
                        status="claimed", lease_expires_at="2020-01-01T00:00:00Z")
                    requests.append(req["request_id"])
            self.call("tick", {"project_ids": ["projectlost0"]})
            statuses = [conn.execute("SELECT status FROM requests WHERE request_id=?",
                                     (rid,)).fetchone()[0] for rid in requests]
            self.assertEqual(statuses, ["cancelled", "claimed"])
        finally:
            conn.close()

    def test_project_filter_validation_and_unknown_fields(self):
        for values in ([], [1], [None], [True], [""], ["  "], "project", None):
            with self.subTest(project_ids=values):
                self.assertEqual(self.call_err("tick", {"project_ids": values}).code,
                                 "bad_request")
        self.assertEqual(self.call_err("tick", {"unknown": True}).code, "bad_request")

    def test_default_tick_still_covers_all_projects(self):
        tids = []
        for index in range(2):
            tid = "taskfilterdefault" + str(index).zfill(5)
            self.make_ready_task(task_id=tid, project_id=f"projectdefault{index}")
            tids.append(tid)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                for tid in tids:
                    db.update_task(conn, tid, util.now_iso(), status="running")
            result = self.call("tick", {})
            self.assertEqual({r["task_id"] for r in result["created"]}, set(tids))
        finally:
            conn.close()
