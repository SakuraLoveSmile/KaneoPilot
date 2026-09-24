"""Dispatch pipeline: claim validation chain, prepare/begin-send/confirm,
fail semantics, serial bit."""

from __future__ import annotations

import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import bridge, util  # noqa: E402


class DispatchFlow(KanpilotTestCase):
    def _queue(self):
        fixture = self.make_ready_task()
        # v2 §5.22.4：发布后 plan_execution_hold → 安排执行须 confirm_plan。
        self.enqueue_dispatch(fixture["remote"]["id"])
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
        # v2 §5.6：publish-plan 也复验远端状态——先在 to-do 发布，再把远端
        # 拨到 in-progress，验证 claim 校验捕获非 To Do。
        fixture = self.make_ready_task(remote_status="to-do")
        self.fake.tasks[fixture["remote"]["id"]]["status"] = "in-progress"
        self.enqueue_dispatch(fixture["remote"]["id"])
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "guard_failed")
        view = self.task(fixture["remote"]["id"])
        self.assertEqual(view["status"], "needs_user")

    def test_claim_validation_remote_unavailable_keeps_task(self):
        fixture = self.make_ready_task()
        self.enqueue_dispatch(fixture["remote"]["id"])
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
        self.enqueue_dispatch(fixture["remote"]["id"])
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
        self.enqueue_dispatch(fixture["remote"]["id"])
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
        self.enqueue_dispatch(fixture["remote"]["id"])
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
        self.enqueue_dispatch(fixture["remote"]["id"])
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

    def _setup_review_fix(self, strip_marker=False):
        """Source task reviewed -> fix task + registered repair plan."""
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.set_task_status(task_id, "in_review")
        report_dir = self.reports_root / task_id
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / "report.md"
        report.write_text("# review report\n", encoding="utf-8")
        report_sha = util.sha256_file(report)
        dedupe = "cd" * 32
        fix = self.call(
            "create-review-fix",
            {"source_task_id": task_id, "title": "fix it",
             "description": "repair the thing", "priority": "high",
             "dedupe_key": dedupe},
        )
        fix_id = fix["fix_task_id"]
        fix_remote = self.fake.tasks[fix_id]
        if strip_marker:
            # Remove the dedupe marker before fingerprints are taken, so the
            # §4.5-5 marker check is the actual failure point.
            fix_remote["description"] = fix_remote["description"].replace(
                f"<!-- codex-kaneo-review:{dedupe} -->", ""
            ).strip()
        source_sha, req_sha, source_path = self.make_source_snapshot(fix_remote)
        self.sync_task(
            fix_remote, project_name="Feedback",
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path,
        )
        plan = self.write_plan(
            "Feedback", fix_remote,
            {"source_sha256": source_sha,
             "requirements_sha256": req_sha,
             "planner_model": "gpt-5.6-sol",
             "reasoning_effort": "xhigh",
             "plan_kind": "review_fix",
             "review_report_path": str(report),
             "review_report_sha256": report_sha,
             "review_source_task_id": task_id,
             "review_key": dedupe},
        )
        self.call(
            "register-review",
            {"task_id": task_id, "report_path": str(report),
             "conclusion": "changes_requested",
             "review_key": dedupe, "fix_task_id": fix_id,
             "fix_task_source_sha256": source_sha,
             "repair_plan_path": str(plan)},
        )
        self.publish(
            fix_id, plan, plan_kind="review_fix",
            source_sha256=source_sha, requirements_sha256=req_sha,
            planner_model="gpt-5.6-sol", reasoning_effort="xhigh",
        )
        return fix_id, plan

    def test_claim_review_fix_dispatch(self):
        fix_id, _plan = self._setup_review_fix()
        self.enqueue_dispatch(fix_id)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["context"]["plan_kind"], "review_fix")
        self.assertEqual(claim["context"]["task_id"], fix_id)

    def test_claim_review_fix_missing_marker(self):
        fix_id, _plan = self._setup_review_fix(strip_marker=True)
        self.enqueue_dispatch(fix_id)
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertFalse(result["claimed"])
        self.assertIn("marker", result["detail"])
        self.assertEqual(self.task(fix_id)["status"], "needs_user")

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
