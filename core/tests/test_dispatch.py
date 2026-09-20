"""Dispatch pipeline: claim validation chain, prepare/begin-send/confirm,
fail semantics, serial bit."""

from __future__ import annotations

import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import bridge  # noqa: E402


class DispatchFlow(KanpilotTestCase):
    def _queue(self):
        fixture = self.make_ready_task()
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        return fixture

    def test_full_dispatch_flow(self):
        fixture = self._queue()
        task_id = fixture["remote"]["id"]
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"])
        ctx = claim["context"]
        self.assertEqual(ctx["task_id"], task_id)
        self.assertEqual(ctx["attachment_name"], fixture["plan_path"].name)
        self.assertEqual(ctx["plan_kind"], "standard")
        self.assertEqual(ctx["original_todo_slug"], "to-do")
        self.assertEqual(
            ctx["session_marker"], f"{task_id}/{claim['run_id']}"
        )
        self.assertIn("任务标识", ctx["instruction"])
        view = self.task(task_id)
        self.assertEqual(view["status"], "dispatching")
        self.assertEqual(view["display_status"], "派发中")

        # prepare
        evidence = self.valid_evidence(view, fixture["plan_path"])
        prepared = self.call(
            "dispatch-prepare",
            {"task_id": task_id, "run_id": claim["run_id"],
             "evidence": evidence},
        )
        self.assertEqual(prepared["status"], "prepared")

        # begin-send flips remote to in-progress
        sent = self.call(
            "dispatch-begin-send",
            {"task_id": task_id, "run_id": claim["run_id"]},
        )
        self.assertEqual(sent["status"], "submitting")
        self.assertIn("Send", sent["action"])
        self.assertEqual(
            self.fake.tasks[task_id]["status"], "in-progress"
        )

        # confirm requires message_visible
        bad = self.call_err(
            "dispatch-confirm",
            {"task_id": task_id, "run_id": claim["run_id"],
             "evidence": evidence},
        )
        self.assertEqual(bad.code, "evidence_invalid")
        evidence.update(
            {"message_visible": True, "session_id": "sess-1",
             "session_title": "Feedback fix"}
        )
        confirmed = self.call(
            "dispatch-confirm",
            {"task_id": task_id, "run_id": claim["run_id"],
             "evidence": evidence},
        )
        self.assertEqual(confirmed["status"], "running")
        view = self.task(task_id)
        self.assertEqual(view["status"], "running")
        self.assertEqual(view["sent"], 1)
        self.assertEqual(view["send_state"], "sent")
        self.assertEqual(view["session_id"], "sess-1")
        reqs = self.requests(status="queued")
        self.assertTrue(any(r["kind"] == "monitor" for r in reqs))
        # dispatch request finished
        reqs = self.requests(status="succeeded")
        self.assertTrue(any(r["kind"] == "dispatch" for r in reqs))

    def test_claim_validation_remote_not_todo(self):
        fixture = self.make_ready_task(remote_status="in-progress")
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "guard_failed")
        view = self.task(fixture["remote"]["id"])
        self.assertEqual(view["status"], "needs_user")

    def test_claim_validation_remote_unavailable_keeps_task(self):
        fixture = self.make_ready_task()
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        self.fake.fail_next = bridge.BridgeError("connection refused")
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        self.assertEqual(result["code"], "remote_unavailable")
        view = self.task(fixture["remote"]["id"])
        self.assertEqual(view["status"], "ready")  # unchanged, not blocked

    def test_claim_validation_plan_changed(self):
        fixture = self.make_ready_task()
        fixture["plan_path"].write_text(
            fixture["plan_path"].read_text() + "\nextra\n", encoding="utf-8"
        )
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        view = self.task(fixture["remote"]["id"])
        self.assertEqual(view["status"], "needs_user")

    def test_claim_validation_source_mismatch(self):
        fixture = self.make_ready_task()
        # corrupt the snapshot file after registration
        import json
        snap = json.loads(Path(fixture["source_path"]).read_text())
        snap["task"]["title"] = "changed"
        Path(fixture["source_path"]).write_text(json.dumps(snap))
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        self.assertEqual(self.task(fixture["remote"]["id"])["status"],
                         "needs_user")

    def test_claim_validation_requirements_mismatch(self):
        fixture = self.make_ready_task()
        # remote description change alters requirements hash
        self.fake.tasks[fixture["remote"]["id"]]["description"] = "edited"
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        self.assertEqual(self.task(fixture["remote"]["id"])["status"],
                         "needs_user")

    def test_claim_standard_rejects_review_marker(self):
        marker = "<!-- codex-kaneo-review:" + "ab" * 32 + " -->"
        fixture = self.make_ready_task(description=f"body\n\n{marker}")
        # plan+snapshot were built before description existed; rebuild:
        # description is part of requirements hash, so register again.
        source_sha, req_sha, source_path = self.make_source_snapshot(
            fixture["remote"]
        )
        self.sync_task(
            fixture["remote"], project_name="Feedback",
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path,
        )
        self.call("enqueue", {"task_id": fixture["remote"]["id"]})
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        # requirements also changed remotely -> deterministic needs_user.
        self.assertFalse(result["claimed"])
        self.assertEqual(self.task(fixture["remote"]["id"])["status"],
                         "needs_user")

    def test_serial_bit_blocks_second_dispatch(self):
        fixture = self._queue()
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"])
        again = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(again["claimed"])
        self.assertEqual(again["reason"], "stage_busy")

    def test_dispatch_fail_not_sent_and_unknown(self):
        fixture = self._queue()
        task_id = fixture["remote"]["id"]
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        failed = self.call(
            "dispatch-fail",
            {"task_id": task_id, "run_id": claim["run_id"],
             "send_state": "not_sent", "reason": "ui blocked"},
        )
        self.assertEqual(failed["send_state"], "not_sent")
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["send_state"], "not_sent")
        self.assertEqual(view["sent"], 0)
        self.assertIn("retry", view["allowed_actions"])
        # serial released -> a fresh dispatch can be claimed after retry
        self.call("resolve", {"task_id": task_id, "action": "retry"})
        self.assertEqual(self.task(task_id)["status"], "ready")
        claim2 = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim2["claimed"])
        failed2 = self.call(
            "dispatch-fail",
            {"task_id": task_id, "run_id": claim2["run_id"],
             "send_state": "unknown", "reason": "screen locked"},
        )
        self.assertEqual(failed2["send_state"], "unknown")
        view = self.task(task_id)
        self.assertEqual(view["send_state"], "unknown")
        self.assertIsNone(view["sent"])
        actions = view["allowed_actions"]
        self.assertIn("restore", actions)
        self.assertIn("accept_duplicate", actions)
        self.assertIn("mark_sent", actions)
        self.assertNotIn("retry", actions)

    def test_begin_send_remote_conflict(self):
        fixture = self._queue()
        task_id = fixture["remote"]["id"]
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        view = self.task(task_id)
        self.call(
            "dispatch-prepare",
            {"task_id": task_id, "run_id": claim["run_id"],
             "evidence": self.valid_evidence(view, fixture["plan_path"])},
        )
        # remote moved away before the transition
        self.fake.tasks[task_id]["status"] = "done"
        exc = self.call_err(
            "dispatch-begin-send",
            {"task_id": task_id, "run_id": claim["run_id"]},
        )
        self.assertEqual(exc.code, "remote_conflict")
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["send_state"], "unknown")

    def test_evidence_rejects_wrong_fields(self):
        fixture = self._queue()
        task_id = fixture["remote"]["id"]
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        view = self.task(task_id)
        evidence = self.valid_evidence(view, fixture["plan_path"],
                                       model="wrong-model")
        exc = self.call_err(
            "dispatch-prepare",
            {"task_id": task_id, "run_id": claim["run_id"],
             "evidence": evidence},
        )
        self.assertEqual(exc.code, "evidence_invalid")


if __name__ == "__main__":
    unittest.main()
