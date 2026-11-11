"""Isolated FakeCore Goal preview contract; never compile or connect to a real app."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


FAKE = Path(__file__).resolve().parents[1] / "FakeCore" / "kanpilot-fake.py"
TASK = "auto-verification_unconfirmed"
RUN = "run-auto-verification_unconfirmed"
MARKER = TASK + "/" + RUN


class GoalBlockerFakeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.state = Path(self.scratch.name) / "state.json"
        self.env = dict(os.environ, KANPILOT_FAKE_STATE=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("KANPILOT_FAKE_GOAL_BLOCKER_SCENARIO", None)

    def call(self, command, payload=None, ok=True):
        body = {"task_id": TASK, "run_id": RUN}
        body.update(payload or {})
        result = subprocess.run([sys.executable, str(FAKE), command, "--json", json.dumps(body)],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def decision(self, action, **extra):
        current = self.call("get-goal-blocker")
        payload = {"action": action, "confirm": True, "operation_id": "fixture-operation-1",
                   "execution_id": current["execution_id"], "expected_version": current["version"]}
        payload.update(extra)
        return payload

    def test_blocked_material_and_identity_are_canned(self):
        result = self.call("get-goal-blocker")
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["actions"], ["continue", "accept"])
        self.assertEqual((result["task_id"], result["run_id"]), (TASK, RUN))
        self.assertEqual(result["session_id"], self.call("get-task")["task"]["session_id"])
        self.assertEqual(len(result["limitations"]), 8)
        self.assertTrue(any(item["status"] == "blocked" for item in result["progress"]))
        self.assertEqual(self.call("get-goal-blocker", {"run_id": "stale"}, ok=False)["code"], "conflict")
        self.assertFalse(self.state.exists(), "reads must not create preview state")

    def test_accept_retains_evidence_without_passing_verification(self):
        initial = self.call("get-goal-blocker")
        payload = self.decision("accept")
        self.assertEqual(self.call("resolve-goal-blocker", payload)["status"], "accepted")
        self.assertTrue(self.call("resolve-goal-blocker", payload)["deduplicated"])
        accepted = self.call("get-goal-blocker")
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["actions"], [])
        self.assertEqual(accepted["limitations"], initial["limitations"])
        task = self.call("get-task")["task"]
        self.assertEqual((task["status"], task["stage"]), ("in_review", "review"))
        self.assertFalse(task["engine_verdict"]["verification_ok"])
        self.assertTrue(task["engine_verdict"]["review_authorized"])
        stale = dict(payload, operation_id="another-operation")
        self.assertEqual(self.call("resolve-goal-blocker", stale, ok=False)["code"], "conflict")

    def test_continue_uses_same_fixture_session_and_turn(self):
        payload = self.decision("continue", message="本地示例曲目引用")
        result = self.call("resolve-goal-blocker", payload)
        self.assertEqual(result["status"], "continued")
        self.assertEqual(self.call("get-goal-blocker")["state"], "none")
        task = self.call("get-task")["task"]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["run_id"], RUN)
        self.assertEqual(task["execution_round"]["session_id"], result["session_id"])
        self.assertEqual(task["execution_round"]["turn_id"], result["turn_id"])
        self.assertFalse(task["engine_verdict"]["verification_ok"])

    def test_unknown_accept_reconciles_without_sending_again(self):
        self.state.write_text(json.dumps({"goal_blocker_fixtures": {MARKER: {"lose_resolve_response": True}}}))
        payload = self.decision("accept")
        self.assertEqual(self.call("resolve-goal-blocker", payload, ok=False)["code"], "remote_unavailable")
        pending = self.call("get-goal-blocker")
        self.assertEqual((pending["state"], pending["actions"]), ("accepted", ["reconcile"]))
        self.assertEqual(self.call("get-task")["task"]["status"], "needs_user")
        self.assertEqual(self.call("reconcile-goal-blocker")["status"], "accepted")
        self.assertTrue(self.call("reconcile-goal-blocker")["deduplicated"])
        self.assertEqual(self.call("get-goal-blocker")["actions"], [])
        record = json.loads(self.state.read_text())["goal_blocker_results"][MARKER]
        self.assertEqual((record["resolve_sends"], record["reconcile_calls"]), (1, 1))
        self.assertFalse(self.call("get-task")["task"]["engine_verdict"]["verification_ok"])

    def test_environment_scenarios_are_isolated_and_reconcile_once(self):
        self.env["KANPILOT_FAKE_GOAL_BLOCKER_SCENARIO"] = "unknown_continue"
        self.assertEqual(self.call("get-goal-blocker")["actions"], ["reconcile"])
        self.assertEqual(self.call("reconcile-goal-blocker")["status"], "continued")
        self.assertEqual(self.call("get-goal-blocker")["actions"], [])
        self.env["KANPILOT_FAKE_GOAL_BLOCKER_SCENARIO"] = "unavailable"
        self.assertEqual(self.call("get-goal-blocker")["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
