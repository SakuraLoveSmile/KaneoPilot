"""Same-branch remediation metadata and real resolve boundary."""
from unittest.mock import patch

from kanpilot import core, db, derive
from kanpilot.engine import loop
import test_engine_recovery as fixture


class TestFixBranchGuard(fixture.TestEngineRecovery):
    def detached(self):
        self.git(self.worktree, "switch", "--detach")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery={"mode": "manual"})

    def test_detached_quality_blocks_before_budget_without_consuming(self):
        self.detached()
        payload = self.quality_failure(empty=False)
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                     engine_fix_rounds=[{"execution_run_id": "old1"}, {"execution_run_id": "old2"}])
        self.call("resolve", payload)
        self.assertEqual(self.task()["failure_code"], "fix_branch_unavailable")
        self.assertEqual(self.task()["failure_stage"], "monitor")
        from kanpilot.engine import attention, ladder
        self.assertEqual(attention.reason_code_for("fix_branch_unavailable"), "needs_user_other")
        self.assertEqual(attention.reason_code_for("verification_environment_failure"), "needs_user_other")
        self.assertEqual(ladder.classify("fix_branch_unavailable"), ladder.LADDER_NEEDS_YOU)
        self.assertEqual(self.task()["failure_reason"], core.FIX_BRANCH_UNAVAILABLE_DETAIL)
        self.assertEqual(len(db.task_extra(self.task())["engine_fix_rounds"]), 2)
        result = self.call("resolve", {"task_id": self.task_id, "action": "resume"})
        self.assertEqual((result["task_status"], result["request"]["kind"]), ("running", "monitor"))
        self.assertEqual(self.task()["run_id"], self.run_id)

    def test_loop_detached_quality_blocks_without_replan(self):
        self.detached()
        self.quality_failure(empty=False)
        loop.Reconciler(self.db_path).reconcile_once()
        self.assertEqual(self.task()["failure_code"], "fix_branch_unavailable")
        self.assertFalse(db.task_extra(self.task()).get("engine_fix_rounds"))
        self.assertFalse(core.automation_of(self.task()).get("plan_wanted"))

    def test_worktree_mismatch_blocks_without_git_probe(self):
        payload = self.quality_failure(empty=False)
        receipt = db.task_extra(self.task())["engine_verdict"]
        receipt["binding"]["worktree_path"] = "/different/registered/tree"
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, engine_verdict=receipt)
        from kanpilot.engine import verdict
        with patch.object(verdict, "binding", side_effect=AssertionError("metadata rejection must not scan")):
            self.call("resolve", payload)
        self.assertEqual(self.task()["failure_code"], "fix_branch_unavailable")
        self.assertFalse(db.task_extra(self.task()).get("engine_fix_rounds"))

    def test_detached_empty_diff_keeps_same_plan_retry(self):
        self.detached()
        payload = self.quality_failure(empty=True)
        self.assertEqual(self.call("resolve", payload)["request"]["kind"], "dispatch")

    def test_review_stage_never_exposes_or_accepts_resume(self):
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       failure_code="fix_branch_unavailable", failure_stage="review", stage="review")
        self.assertNotIn("resume", derive.allowed_actions(self.ctx.conn, self.task(), self.ctx.now))
        self.assertEqual(self.call_err("resolve", {"task_id": self.task_id, "action": "resume"}).code, "conflict")

    def test_unmanaged_environment_resume_preserves_automation_and_original_run(self):
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso, manual_takeover=True)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       failure_code="verification_environment_failure", failure_stage="monitor")
        old = core.automation_of(self.task())
        result = self.call("resolve", {"task_id": self.task_id, "action": "resume"})
        self.assertEqual((result["task_status"], result["request"]["kind"]), ("running", "monitor"))
        self.assertEqual(core.automation_of(self.task()), old)
        self.assertEqual(self.task()["run_id"], self.run_id)

    def test_manual_delivery_residual_branch_cannot_authorize_same_branch_fix(self):
        delivery = core.delivery_of(self.task())
        delivery["mode"] = "manual"
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=delivery)
        self.assertEqual(core.same_branch_fix_ready(self.task()), (False, "detached"))
        delivery["workspace_version"] = 1
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=delivery)
        self.assertTrue(core.same_branch_fix_ready(self.task())[0])
        delivery.pop("branch")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=delivery)
        self.assertEqual(core.same_branch_fix_ready(self.task())[1], "no_branch_worktree")

    def test_shared_helper_reasons_are_metadata_only(self):
        self.assertTrue(core.same_branch_fix_ready(self.task())[0])
        delivery = core.delivery_of(self.task())
        delivery.pop("worktree_path")
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=delivery)
        self.assertEqual(core.same_branch_fix_ready(self.task())[1], "no_branch_worktree")


for _name in fixture.TestEngineRecovery.__dict__:
    if _name.startswith("test_") and _name not in TestFixBranchGuard.__dict__:
        setattr(TestFixBranchGuard, _name, None)
