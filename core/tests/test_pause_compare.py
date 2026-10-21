"""Global pause compare-and-set remains atomic across desktop/mobile callers."""
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kanpilot import cli, util


class PauseCompareTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kp-pause-compare-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "pilot.sqlite3"
        env = patch.dict(os.environ, {"KANPILOT_APP_SUPPORT": str(self.root),
                                     "KANPILOT_DB": str(self.database)})
        env.start()
        self.addCleanup(env.stop)
        cli.run("overview", {}, self.database)

    def paused_events(self):
        with sqlite3.connect(self.database) as conn:
            return conn.execute("SELECT COUNT(*) FROM events WHERE kind IN ('paused','unpaused')").fetchone()[0]

    def test_stale_state_rejected_without_event_or_change(self):
        with self.assertRaises(util.KanpilotError) as caught:
            cli.run("set-paused", {"stage": None, "paused": False,
                                   "expected_paused": True}, self.database)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.paused_events(), 0)
        self.assertFalse(cli.run("overview", {}, self.database)["paused"]["global"])

    def test_existing_desktop_call_without_expected_value_works(self):
        result = cli.run("set-paused", {"stage": None, "paused": True}, self.database)
        self.assertTrue(result["paused"]["global"])

    def test_only_one_concurrent_stale_snapshot_is_accepted(self):
        def write(_):
            try:
                cli.run("set-paused", {"stage": None, "paused": True,
                                       "expected_paused": False}, self.database)
                return "accepted"
            except util.KanpilotError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, range(2)))
        self.assertCountEqual(results, ["accepted", "conflict"])
        self.assertEqual(self.paused_events(), 1)

    def test_expected_global_state_cannot_guard_a_stage_setting(self):
        with self.assertRaises(util.KanpilotError) as caught:
            cli.run("set-paused", {"stage": "plan", "paused": True,
                                   "expected_paused": False}, self.database)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(self.paused_events(), 0)
