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
