"""Unit tests for engine step→command mapping (pure)."""
from __future__ import annotations

import unittest

from kanpilot.engine import decide, steps


def _task(**kw):
    base = {"task_id": "t1", "plan_sha256": "sha"}
    base.update(kw)
    return base


class TestStepsMapping(unittest.TestCase):
    def test_confirm_plan(self):
        calls = steps.commands_for(decide.Decision(decide.CONFIRM_PLAN), _task())
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.command, "enqueue")
        self.assertEqual(call.payload, {
            "task_id": "t1", "kind": "dispatch", "confirm_plan": True,
            "expected_plan_sha256": "sha"})
        self.assertEqual(call.kind, "dispatch")
        self.assertTrue(call.requires_hold)

    def test_enqueue_dispatch(self):
        calls = steps.commands_for(decide.Decision(decide.ENQUEUE_DISPATCH), _task())
        self.assertEqual(calls[0].payload, {"task_id": "t1", "kind": "dispatch"})
        self.assertEqual(calls[0].kind, "dispatch")
        self.assertFalse(calls[0].requires_hold)

    def test_run_verdict_maps_to_monitor(self):
        calls = steps.commands_for(decide.Decision(decide.RUN_VERDICT), _task())
        self.assertEqual(calls[0].payload, {"task_id": "t1", "kind": "monitor"})

    def test_commit_and_sync_map_to_delivery(self):
        for action in (decide.COMMIT_DELIVERY, decide.SYNC_DELIVERY):
            with self.subTest(action=action):
                calls = steps.commands_for(decide.Decision(action), _task())
                self.assertEqual(calls[0].payload, {"task_id": "t1", "kind": "delivery"})

    def test_enqueue_review(self):
        calls = steps.commands_for(decide.Decision(decide.ENQUEUE_REVIEW), _task())
        self.assertEqual(calls[0].payload, {"task_id": "t1", "kind": "review"})

    def test_no_command_intents(self):
        for action in (decide.AWAIT, decide.BLOCK, decide.NOOP, decide.RETRY,
                       decide.ENQUEUE_PLAN, decide.ADVANCE_INTAKE,
                       decide.ADVANCE_DISPATCH, decide.RESUME_OBSERVE,
                       decide.START_FIX_ROUND):
            with self.subTest(action=action):
                self.assertEqual(steps.commands_for(decide.Decision(action), _task()), [])


if __name__ == "__main__":
    unittest.main()
