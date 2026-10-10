"""Unit tests for the runner adapters (subprocess launch is mocked)."""
from __future__ import annotations

import unittest
from unittest import mock

from kanpilot.engine import runners


class TestRunners(unittest.TestCase):
    def test_spawn_stage_rejects_unknown(self):
        with self.assertRaises(ValueError):
            runners.spawn_stage("plan", "/tmp/x.db")  # plan is not an executor stage

    def test_spawn_dispatch_argv_and_env(self):
        with mock.patch.object(runners.subprocess, "Popen") as popen:
            runners.spawn("dispatch", "/tmp/x.db", instance_id="iid-123")
            argv = popen.call_args.args[0]
            self.assertEqual(argv[:3],
                             [runners.sys.executable, "-m", "kanpilot.executor_runner"])
            self.assertIn("--stage", argv)
            self.assertIn("dispatch", argv)
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["KANPILOT_EXECUTOR_INSTANCE_ID"], "iid-123")
            self.assertEqual(env["KANPILOT_DB"], "/tmp/x.db")

    def test_spawn_planner_argv(self):
        with mock.patch.object(runners.subprocess, "Popen") as popen:
            runners.spawn("plan", "/tmp/x.db")
            argv = popen.call_args.args[0]
            self.assertEqual(argv, [runners.sys.executable, "-m", "kanpilot.planner_runner"])


if __name__ == "__main__":
    unittest.main()
