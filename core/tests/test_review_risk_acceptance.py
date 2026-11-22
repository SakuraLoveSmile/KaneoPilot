"""Accepting the residual Review risk continues the manual first delivery (KP-MG03 #4)."""
from unittest import TestCase

import test_review_delivery as base
from kanpilot import cli, core, db, derive, review_repair, review_risk, util


class RiskAcceptanceTest(TestCase):
    def setUp(self):
        self.base = base.ReviewDeliveryTest()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.conn, self.ctx, self.fixture = self.base.conn, self.base.ctx, self.base.fixture
        task = self.task()
        review = dict(db.task_extra(task)["review_history"][0], conclusion="changes_requested")
        db.set_extra(self.conn, task, self.ctx.now_iso,
                     review_conclusion="changes_requested", review_history=[review],
                     review_repair_round=0)
        fix = review_repair.candidate(self.task())
        self.assertIsNotNone(fix)
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, fix_request=fix)
        task, delivery = self.task(), core.delivery_of(self.task())
        self.accept_payload = dict(task_id="parent", expected_review_key=task["review_key"],
            expected_report_sha256=task["review_report_sha256"],
            expected_head_sha=delivery["head_sha"],
            expected_plan_sha256=task["plan_sha256"], confirm=True, note="只剩可选加固")

    def task(self):
        return db.require_task(self.conn, "parent")

    def accept(self, **overrides):
        return cli.run("accept-review-risk", dict(self.accept_payload, **overrides), self.fixture.db_path)

    def resolve(self, **overrides):
        return cli.run("resolve", dict(self.base.payload, **overrides), self.fixture.db_path)

    def test_accept_allows_continue_delivery_of_the_reviewed_head(self):
        result = self.accept()
        self.assertTrue(result["ok"])
        task, extra = self.task(), db.task_extra(self.task())
        self.assertIsNone(extra.get("fix_request"))
        self.assertEqual(extra["review_conclusion"], "changes_requested")
        self.assertIsNotNone(review_risk.identity(task))
        self.assertIn("continue_delivery", derive.allowed_actions(self.conn, task, self.ctx.now))
        result = self.resolve()
        self.assertEqual(result["request"]["kind"], "delivery")
        self.assertEqual(core.delivery_of(self.task())["mode"], "pr")

    def test_accept_requires_confirm(self):
        with self.assertRaises(util.KanpilotError):
            self.accept(confirm=False)

    def test_accept_rejects_ai_action_material(self):
        fix = dict(db.task_extra(self.task())["fix_request"], source_kind="check_failure")
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, fix_request=fix)
        with self.assertRaises(util.KanpilotError):
            self.accept()

    def test_accept_rejects_automation_managed_task(self):
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, automation={"enabled": True})
        with self.assertRaises(util.KanpilotError):
            self.accept()

    def test_accept_rejects_an_active_request(self):
        db.create_request(self.conn, self.ctx.now_iso, "dispatch", "busy", task_id="parent")
        with self.assertRaises(util.KanpilotError):
            self.accept()

    def test_head_drift_invalidates_the_acceptance(self):
        self.accept()
        delivery = dict(core.delivery_of(self.task()), head_sha="e" * 40)
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, delivery=delivery)
        self.assertIsNone(review_risk.identity(self.task()))
        with self.assertRaises(util.KanpilotError):
            self.resolve()

    def test_review_report_drift_invalidates_the_acceptance(self):
        self.accept()
        db.update_task(self.conn, "parent", self.ctx.now_iso, review_report_sha256="f" * 64)
        self.assertIsNone(review_risk.identity(self.task()))

    def test_revoke_restores_the_blocked_repair_state(self):
        self.accept()
        cli.run("revoke-review-risk-acceptance",
                dict(task_id="parent", expected_head_sha=core.delivery_of(self.task())["head_sha"],
                     confirm=True), self.fixture.db_path)
        self.assertIsNone(db.task_extra(self.task()).get(review_risk.KEY))
        self.assertIsNone(review_risk.identity(self.task()))
        self.assertNotIn("continue_delivery", derive.allowed_actions(self.conn, self.task(), self.ctx.now))

    def test_second_acceptance_is_deduplicated(self):
        self.accept()
        self.assertTrue(self.accept().get("deduplicated"))
