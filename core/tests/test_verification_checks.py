"""Historical local verification is readable, but never executable or authoritative."""
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, db, util, verification_checks


class TestVerificationChecks(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.make_ready_task(task_id="historical-verification")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)

    def test_compatibility_runner_refuses_without_process_or_state_change(self):
        before = dict(db.require_task(self.ctx.conn, "historical-verification"))
        payload = {"task_id": "historical-verification", "expected_execution_run_id": "run",
                   "expected_plan_sha256": "plan", "operation_id": "operation"}
        with patch("subprocess.Popen", side_effect=AssertionError("no local execution")):
            with self.assertRaises(util.KanpilotError) as caught:
                verification_checks.cmd_run_verification_checks(self.ctx, payload)
        self.assertIn("Plus", caught.exception.message)
        self.assertEqual(before, dict(db.require_task(self.ctx.conn, "historical-verification")))

    def test_historical_receipt_remains_readable_without_authorizing_progress(self):
        task = db.require_task(self.ctx.conn, "historical-verification")
        receipt = {"verification_ok": True, "checks": [{"command": "old test", "exit_code": 0}]}
        db.set_extra(self.ctx.conn, task, self.ctx.now_iso, verification_checks={
            "operation_id": "history", "state": "completed", "receipt": receipt})
        db.add_event(self.ctx.conn, self.ctx.now_iso, "verification_checks_completed",
            task_id=task["task_id"], detail={"operation_id": "history",
                "receipt_sha256": util.sha256_text(util.jdump(receipt))})
        task = db.require_task(self.ctx.conn, task["task_id"])
        result = verification_checks.cmd_get_verification_check_status(self.ctx,
            {"task_id": task["task_id"], "operation_id": "history"})
        self.assertTrue(result["verification_ok"])
        self.assertFalse(result["recovery_pending"])
        self.assertIn("历史", result["detail"])
        self.assertIsNone(verification_checks.valid_receipt(self.ctx.conn, task))
        self.assertFalse(verification_checks.public_state(task)["recovery_pending"])

    def test_changed_historical_receipt_is_marked_untrusted(self):
        task = db.require_task(self.ctx.conn, "historical-verification")
        db.set_extra(self.ctx.conn, task, self.ctx.now_iso, verification_checks={
            "operation_id": "history", "state": "completed", "receipt": {"verification_ok": True}})
        result = verification_checks.cmd_get_verification_check_status(self.ctx,
            {"task_id": task["task_id"], "operation_id": "history"})
        self.assertFalse(result["verification_ok"])
