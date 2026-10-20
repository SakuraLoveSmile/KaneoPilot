"""Table-driven unit tests for the engine decision core (no DB, no I/O).

state-model §7 (phase×condition) and §8 (failure ladder) — every table row is
asserted ("每行一条"). Run: ``cd core && python3 -m pytest tests/test_engine_decide.py -q``.
"""
from __future__ import annotations

import unittest

from kanpilot.engine import attention, decide, ladder


def _snap(**kw):
    base = {"automation_managed": True, "budget_ok": True, "capacity_ok": True}
    base.update(kw)
    return base


class TestPhaseConditionTable(unittest.TestCase):
    """Every (phase, condition) row: pin base_action and decide()'s intent."""

    # Expected steady-state intent per state-model §7 / §9 golden path.
    EXPECTED = {
        ("intake", "idle"): decide.NOOP,
        ("intake", "pending"): decide.ADVANCE_INTAKE,
        ("plan", "wanted"): decide.ENQUEUE_PLAN,
        ("plan", "drafting"): decide.AWAIT,
        ("plan", "ready"): decide.CONFIRM_PLAN,
        ("dispatch", "confirmed"): decide.ENQUEUE_DISPATCH,
        ("dispatch", "submitting"): decide.ADVANCE_DISPATCH,
        ("observe", "running"): decide.AWAIT,
        ("observe", "observing"): decide.RESUME_OBSERVE,
        ("verdict", "checking"): decide.RUN_VERDICT,
        ("verdict", "unconfirmed"): decide.RUN_VERDICT,
        ("commit", "committing"): decide.COMMIT_DELIVERY,
        ("review", "reviewing"): decide.ENQUEUE_REVIEW,
        ("review", "repairing"): decide.START_FIX_ROUND,
        ("delivery", "syncing"): decide.SYNC_DELIVERY,
        ("done", "merged"): decide.NOOP,
        ("needs-you", "blocked"): decide.BLOCK,
    }

    def test_table_has_no_missing_or_extra_rows(self):
        self.assertEqual(set(self.EXPECTED), set(decide._PHASE_ACTION))

    def test_every_row_base_action(self):
        for (phase, condition), expected in self.EXPECTED.items():
            with self.subTest(phase=phase, condition=condition):
                self.assertEqual(decide.base_action(phase, condition), expected)

    def test_every_row_decide_intent(self):
        for (phase, condition), expected in self.EXPECTED.items():
            with self.subTest(phase=phase, condition=condition):
                status = "needs_user" if (phase, condition) == ("needs-you", "blocked") else "ready"
                snap = _snap(phase=phase, condition=condition, status=status,
                             failure_code="pr_closed")
                decision = decide.decide(snap)
                self.assertEqual(decision.action, expected)
                if expected == decide.BLOCK:
                    self.assertTrue(decision.attention)


class TestStatusDerivation(unittest.TestCase):
    """task.status → (phase, condition) fallback rows (state-model §7)."""

    EXPECTED = {
        "needs_plan": ("plan", "wanted"),
        "planning": ("plan", "drafting"),
        "ready": ("plan", "ready"),
        "dispatching": ("dispatch", "confirmed"),
        "prepared": ("dispatch", "submitting"),
        "submitting": ("dispatch", "submitting"),
        "running": ("observe", "running"),
        "in_review": ("review", "reviewing"),
        "needs_user": ("needs-you", "blocked"),
        "done": ("done", "merged"),
    }

    def test_every_status_row(self):
        for status, expected in self.EXPECTED.items():
            with self.subTest(status=status):
                self.assertEqual(decide.phase_condition(status), expected)

    def test_in_review_repairing_when_fix_open(self):
        self.assertEqual(
            decide.phase_condition("in_review", fix_open=True), ("review", "repairing"))
        self.assertEqual(
            decide.phase_condition("in_review", fix_round=1), ("review", "reviewing"))

    def test_unknown_status_is_none(self):
        self.assertIsNone(decide.phase_condition("bogus"))


class TestFailureLadderTable(unittest.TestCase):
    """Every failure_code → ladder row, plus the unknown default."""

    def test_every_code_row(self):
        for code, expected in ladder._FAILURE_CODE_LADDER.items():
            with self.subTest(code=code):
                self.assertEqual(ladder.classify(code), expected)

    def test_unknown_and_empty_default_to_needs_you(self):
        self.assertEqual(ladder.classify("no_such_code"), ladder.LADDER_NEEDS_YOU)
        self.assertEqual(ladder.classify(None), ladder.LADDER_NEEDS_YOU)
        self.assertEqual(ladder.classify(""), ladder.LADDER_NEEDS_YOU)

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual(ladder.backoff_seconds(0), 1.0)
        self.assertEqual(ladder.backoff_seconds(1), 2.0)
        self.assertEqual(ladder.backoff_seconds(2), 4.0)
        self.assertEqual(ladder.backoff_seconds(-3), 1.0)
        self.assertEqual(ladder.backoff_seconds(100), ladder.BACKOFF_CAP_SECONDS)

    def test_ladder_policy_lookups(self):
        self.assertTrue(ladder.auto_retry_allowed(ladder.LADDER_TRANSIENT))
        self.assertFalse(ladder.auto_retry_allowed(ladder.LADDER_ENVIRONMENTAL))
        self.assertEqual(ladder.max_attempts(ladder.LADDER_TRANSIENT), 2)
        self.assertEqual(ladder.max_attempts(ladder.LADDER_QUALITY), 2)
        self.assertEqual(ladder.escalate(ladder.LADDER_TRANSIENT), ladder.LADDER_ENVIRONMENTAL)
        self.assertEqual(ladder.escalate(ladder.LADDER_QUALITY), ladder.LADDER_NEEDS_YOU)
        self.assertIsNone(ladder.escalate(ladder.LADDER_ENVIRONMENTAL))
        self.assertFalse(ladder.retries_exhausted(ladder.LADDER_TRANSIENT, 1))
        self.assertTrue(ladder.retries_exhausted(ladder.LADDER_TRANSIENT, 2))


class TestFailureDecisionRows(unittest.TestCase):
    """failure_decision per ladder row (retry / escalate / fix / block)."""

    def test_transient_retries_then_escalates_to_environmental(self):
        d0 = decide.failure_decision("remote_unavailable", attempt_no=0)
        self.assertEqual((d0.action, d0.ladder), (decide.RETRY, ladder.LADDER_TRANSIENT))
        self.assertEqual(d0.params["backoff_seconds"], 1.0)
        self.assertEqual(d0.params["attempt_no"], 1)
        d1 = decide.failure_decision("remote_unavailable", attempt_no=1)
        self.assertEqual(d1.action, decide.RETRY)
        self.assertEqual(d1.params["backoff_seconds"], 2.0)
        d2 = decide.failure_decision("remote_unavailable", attempt_no=2)
        self.assertTrue(d2.is_block)
        self.assertEqual(d2.ladder, ladder.LADDER_ENVIRONMENTAL)  # escalate 1 → 2
        self.assertTrue(d2.attention)

    def test_environmental_blocks_without_retry(self):
        for code in ("repository_missing", "gh_missing", "gh_not_logged_in"):
            with self.subTest(code=code):
                d = decide.failure_decision(code)
                self.assertTrue(d.is_block)
                self.assertEqual(d.ladder, ladder.LADDER_ENVIRONMENTAL)
                self.assertTrue(d.attention)

    def test_quality_bounded_fix_rounds_then_needs_you(self):
        d0 = decide.failure_decision("verification_unconfirmed", fix_round=0)
        self.assertEqual((d0.action, d0.params["fix_round"]), (decide.START_FIX_ROUND, 1))
        d1 = decide.failure_decision("verification_unconfirmed", fix_round=1)
        self.assertEqual(d1.action, decide.START_FIX_ROUND)
        d2 = decide.failure_decision("verification_unconfirmed", fix_round=2)
        self.assertTrue(d2.is_block)
        self.assertEqual(d2.reason, "fix_rounds_exhausted")
        self.assertEqual(d2.ladder, ladder.LADDER_NEEDS_YOU)  # escalate 3 → 4

    def test_quality_budget_exceeded_blocks(self):
        d = decide.failure_decision("review_blocked", fix_round=0, budget_ok=False)
        self.assertTrue(d.is_block)
        self.assertEqual(d.reason, "auto_budget_exceeded")

    def test_needs_you_blocks(self):
        d = decide.failure_decision("pr_closed")
        self.assertTrue(d.is_block)
        self.assertEqual(d.ladder, ladder.LADDER_NEEDS_YOU)


class TestRoundSettledDecision(unittest.TestCase):
    """round_settled_decision: a probed exit code → the settle intent (§13.2)."""

    def test_clean_exit_settles_toward_verdict(self):
        d = decide.round_settled_decision(0)
        self.assertEqual(d.action, decide.SETTLE_ROUND)
        self.assertEqual(d.reason, "round_exited_zero")
        self.assertEqual(d.params["exit_code"], 0)
        self.assertEqual(d.params["terminal"], "completed")
        self.assertEqual(d.ladder, 0)  # success path: no failure ladder
        self.assertFalse(d.attention)

    def test_nonzero_exit_attributes_execution_failed(self):
        d = decide.round_settled_decision(1)
        self.assertEqual(d.action, decide.SETTLE_ROUND)
        self.assertEqual(d.reason, "execution_failed")
        self.assertEqual(d.params["failure_code"], "execution_failed")
        self.assertEqual(d.params["exit_code"], 1)
        self.assertEqual(d.ladder, ladder.LADDER_NEEDS_YOU)  # 阶梯 4

    def test_negative_exit_code_is_also_a_failure(self):
        d = decide.round_settled_decision(-2)  # pi_desktop timeout stop
        self.assertEqual(d.action, decide.SETTLE_ROUND)
        self.assertEqual(d.reason, "execution_failed")
        self.assertEqual(d.params["exit_code"], -2)

class TestDecideGuards(unittest.TestCase):
    def test_manual_takeover_is_noop(self):
        d = decide.decide(_snap(status="ready", manual_takeover=True))
        self.assertTrue(d.is_noop)
        self.assertEqual(d.reason, "not_automation_managed")

    def test_unmanaged_is_noop(self):
        d = decide.decide(_snap(status="ready", automation_managed=False))
        self.assertTrue(d.is_noop)

    def test_paused_awaits(self):
        d = decide.decide(_snap(status="ready", paused=True))
        self.assertEqual((d.action, d.reason), (decide.AWAIT, "paused"))

    def test_budget_gate_blocks_confirm(self):
        d = decide.decide(_snap(status="ready", phase="plan", condition="ready", budget_ok=False))
        self.assertTrue(d.is_block)
        self.assertEqual(d.reason, "auto_budget_exceeded")

    def test_capacity_gate_awaits_dispatch(self):
        d = decide.decide(_snap(status="dispatching", phase="dispatch",
                                condition="confirmed", capacity_ok=False))
        self.assertEqual((d.action, d.reason), (decide.AWAIT, "capacity_full"))

    def test_plan_wanted_not_capacity_gated(self):
        # scenario #18: plan_wanted replans ignore max_active_tasks.
        d = decide.decide(_snap(status="needs_plan", phase="plan",
                                condition="wanted", capacity_ok=False))
        self.assertEqual(d.action, decide.ENQUEUE_PLAN)

    def test_needs_user_surfaces_attention_with_ladder(self):
        d = decide.decide(_snap(status="needs_user", failure_code="verification_unconfirmed"))
        self.assertTrue(d.is_block)
        self.assertEqual(d.ladder, ladder.LADDER_QUALITY)
        self.assertTrue(d.attention)


class TestAttention(unittest.TestCase):
    def _task(self, **kw):
        base = {"task_id": "t1", "project_name": "Feedback", "title": "do it",
                "status": "needs_user", "save_state": "active",
                "failure_code": "pr_closed", "failure_stage": "delivery",
                "failure_reason": "closed", "blocked_summary": "closed unmerged",
                "allowed_actions": ["resume", "mark_done", "dismiss"],
                "blocked_at": "2026-10-10T00:00:00+00:00"}
        base.update(kw)
        return base

    def test_item_has_exact_contract_fields(self):
        item = attention.build_item(self._task())
        self.assertEqual(set(item), set(attention.ITEM_FIELDS))
        self.assertEqual(item["attention_state"], attention.ACTIONABLE)

    def test_non_needs_user_has_no_item(self):
        self.assertIsNone(attention.build_item(self._task(status="running")))

    def test_collect_dedups_one_item_per_task(self):
        items = attention.collect([self._task(), self._task()])
        self.assertEqual(len(items), 1)

    def test_collect_orders_by_ladder_priority(self):
        hi = self._task(task_id="hi", failure_code="pr_closed")       # ladder 4
        lo = self._task(task_id="lo", failure_code="verification_unconfirmed")  # ladder 3
        items = attention.collect([lo, hi])
        self.assertEqual([it["task_id"] for it in items], ["hi", "lo"])

    def test_include_historical_gate(self):
        archived = self._task(task_id="a", save_state="archived")
        self.assertEqual(attention.collect([archived]), [])
        self.assertEqual(len(attention.collect([archived], include_historical=True)), 1)
        self.assertEqual(
            attention.build_item(archived)["attention_state"], attention.HISTORICAL)

    def test_reconcile_autoclose(self):
        item = attention.build_item(self._task())
        still, reason = attention.reconcile(item, self._task())
        self.assertTrue(still)
        self.assertIsNone(reason)
        still, reason = attention.reconcile(item, self._task(status="done"))
        self.assertFalse(still)
        self.assertEqual(reason, "resolved")
        still, reason = attention.reconcile(item, self._task(status="running"))
        self.assertFalse(still)
        self.assertEqual(reason, "moved")

    def test_recommendation_splits_allowed_actions(self):
        rec, alts = attention.recommendation(attention.build_item(self._task()))
        self.assertEqual(rec, "resume")
        self.assertEqual(alts, ["mark_done", "dismiss"])

    def test_counts(self):
        items = attention.collect([
            self._task(task_id="a", failure_code="pr_closed"),
            self._task(task_id="b", failure_code="verification_unconfirmed"),
        ])
        self.assertEqual(attention.counts(items),
                         {"actionable": 2, "historical": 0, "informational": 0})


class TestExplain(unittest.TestCase):
    def test_explain_includes_phase_and_reason(self):
        snap = _snap(status="ready", phase="plan", condition="ready")
        text = decide.explain(snap, decide.decide(snap))
        self.assertIn("[plan/ready]", text)
        self.assertIn("确认", text)

    def test_explain_block_has_ladder(self):
        snap = _snap(status="needs_user", failure_code="pr_closed")
        text = decide.explain(snap, decide.decide(snap))
        self.assertIn("需人工", text)
        self.assertIn("阶梯 4", text)

    def test_explain_noop(self):
        snap = _snap(status="ready", manual_takeover=True)
        text = decide.explain(snap, decide.decide(snap))
        self.assertIn("无需动作", text)


class TestReasonCode(unittest.TestCase):
    """reason_code table: one row per failure_code, plus ladder fallback."""

    def test_every_table_row(self):
        for code, expected in attention._REASON_TABLE.items():
            with self.subTest(code=code):
                self.assertEqual(attention.reason_code_for(code), expected)

    def test_spec_enum_values(self):
        # protocol §5.27.3 reason_code enum (the 12 categories + fallback).
        self.assertEqual(attention.reason_code_for("planner_failed"), "needs_plan_input")
        self.assertEqual(attention.reason_code_for("verification_unconfirmed"), "verify_unconfirmed")
        self.assertEqual(attention.reason_code_for("review_blocked"), "review_changes_exhausted")
        self.assertEqual(attention.reason_code_for("delivery_commit_failed"), "delivery_blocked")
        self.assertEqual(attention.reason_code_for("remote_unavailable"), "remote_unavailable")
        self.assertEqual(attention.reason_code_for("plus_unreachable"), "plus_unreachable")
        self.assertEqual(attention.reason_code_for("send_state_unknown"), "plus_send_unknown")
        self.assertEqual(attention.reason_code_for("gh_missing"), "gh_environment")
        self.assertEqual(attention.reason_code_for("push_rejected"), "push_rejected")
        self.assertEqual(attention.reason_code_for("pr_closed_unmerged"), "pr_closed_unmerged")
        self.assertEqual(attention.reason_code_for("auto_budget_exceeded"), "budget_exceeded")
        self.assertEqual(attention.reason_code_for("lease_expired"), "concurrency_stalled")

    def test_fallback_is_needs_user_other(self):
        self.assertEqual(attention.reason_code_for("network_timeout"), "needs_user_other")
        self.assertEqual(attention.reason_code_for("repository_missing"), "needs_user_other")
        self.assertEqual(attention.reason_code_for("execution_failed"), "needs_user_other")
        self.assertEqual(attention.reason_code_for(None), "needs_user_other")

    def test_attention_id_is_stable_and_reused(self):
        a1 = attention.attention_id("t1", attention.reason_code_for("pr_closed"))
        a2 = attention.attention_id("t1", attention.reason_code_for("pr_closed"))
        self.assertEqual(a1, a2)                     # deterministic / restart-stable
        self.assertEqual(a1, "attn_t1_pr_closed_unmerged")
        self.assertTrue(a1.startswith("attn_t1_"))
        self.assertNotEqual(
            a1, attention.attention_id("t1", attention.reason_code_for("verification_unconfirmed")))

    def test_build_item_union_fields(self):
        task = {"task_id": "t1", "project_name": "Feedback", "title": "do it",
                "status": "needs_user", "save_state": "active",
                "failure_code": "pr_closed", "failure_stage": "delivery",
                "failure_reason": "closed", "blocked_summary": "closed unmerged",
                "allowed_actions": ["resume", "mark_done", "dismiss"],
                "blocked_at": "2026-10-10T00:00:00+00:00"}
        item = attention.build_item(task)
        self.assertEqual(set(item), set(attention.ITEM_FIELDS))
        self.assertEqual(item["reason_code"], "pr_closed_unmerged")   # pr_closed
        self.assertEqual(item["attention_id"], "attn_t1_pr_closed_unmerged")
        self.assertEqual(item["recommended"], "resume")
        self.assertEqual(item["alternatives"], ["mark_done", "dismiss"])
        self.assertEqual(item["since"], "2026-10-10T00:00:00+00:00")
        self.assertIsInstance(item["evidence"], list)                 # structured array
        self.assertEqual(item["evidence"][0]["detail"], "closed unmerged")
        self.assertIn(item["evidence"][0]["kind"],
                      ("event", "request", "probe", "runtime_operation"))

    def test_all_reason_codes_reachable_with_example(self):
        codes = {
            attention.NEEDS_PLAN_INPUT, attention.VERIFY_UNCONFIRMED,
            attention.REVIEW_CHANGES_EXHAUSTED, attention.DELIVERY_BLOCKED,
            attention.REMOTE_UNAVAILABLE, attention.PLUS_UNREACHABLE,
            attention.PLUS_SEND_UNKNOWN, attention.GH_ENVIRONMENT,
            attention.PUSH_REJECTED, attention.PR_CLOSED_UNMERGED,
            attention.BUDGET_EXCEEDED, attention.CONCURRENCY_STALLED,
            attention.NEEDS_USER_OTHER,
            attention.EXECUTION_QUESTION,
        }
        self.assertEqual(len(codes), 14)
        produced = set(attention._REASON_TABLE.values())
        # every non-fallback code is produced by >=1 failure_code
        self.assertEqual(produced, codes - {attention.NEEDS_USER_OTHER})
        # one concrete example per non-fallback code
        for target in codes - {attention.NEEDS_USER_OTHER}:
            example = next(c for c, r in attention._REASON_TABLE.items() if r == target)
            with self.subTest(reason_code=target, example=example):
                self.assertEqual(attention.reason_code_for(example), target)
        # fallback reachable
        self.assertEqual(attention.reason_code_for("no_such_code"), attention.NEEDS_USER_OTHER)

    def test_empty_failure_code_uses_stage(self):
        # §5.27.3: empty failure_code classifies by failure_stage (values from 13).
        self.assertEqual(attention.reason_code_for(None, failure_stage="plan"), "needs_plan_input")
        self.assertEqual(attention.reason_code_for(None, failure_stage="monitor"), "verify_unconfirmed")
        self.assertEqual(attention.reason_code_for(None, failure_stage="review"), "review_changes_exhausted")
        self.assertEqual(attention.reason_code_for(None, failure_stage="delivery"), "delivery_blocked")
        self.assertEqual(attention.reason_code_for(None, failure_stage="dispatch"), "delivery_blocked")
        # no/unknown stage -> needs_user_other
        self.assertEqual(attention.reason_code_for(None), "needs_user_other")
        self.assertEqual(attention.reason_code_for(None, failure_stage="weird"), "needs_user_other")
        # a real failure_code still wins over stage
        self.assertEqual(
            attention.reason_code_for("verification_unconfirmed", failure_stage="plan"),
            "verify_unconfirmed")


if __name__ == "__main__":
    unittest.main()
