"""Project auto-fix: bounded policy-authorized repairs (protocol §5.27.20)."""
from datetime import timedelta
from unittest.mock import patch

import pytest

import test_check_failure_action_flow as flow
from kanpilot import (auto_fix, automation_policy, core, db, gh_client, runtime_operations,
                      task_ai_action, task_ai_check_failure, util)

GATE_REASON = flow.GATE_REASON
RUN_URL = "https://github.com/owner/repo/actions/runs/44211608370/job/134491096466"


class AutoFixCase(flow.CheckFailureFixture):
    """A hosted PR task with automation enabled; auto-fix runs against fake GitHub."""

    def setUp(self):
        super().setUp()
        self.pid = self.task()["project_id"]
        self.auto_merge_updated = self.now
        self.run_info = {"head_sha": self.head, "status": "completed",
                         "run_attempt": 1, "conclusion": "failure"}
        core.update_automation(self.conn, self.task(), self.now,
                               segment_started_at=self.now, cost_usd=0.0,
                               segment_cost_base_usd=0.0)
        self.observe(self.now, head=self.head)
        run = patch.object(auto_fix, "_run", side_effect=lambda repo, run_id: dict(self.run_info))
        run.start(); self.addCleanup(run.stop)
        self.reruns = []
        rerun = patch.object(gh_client, "run_rerun_failed",
                             side_effect=lambda repo, run_id: self.reruns.append((repo, run_id)))
        rerun.start(); self.addCleanup(rerun.stop)

    # --- helpers ----------------------------------------------------------
    def enable_auto_fix(self, **overrides):
        current = automation_policy.policy_for(self.conn, self.pid) or {}
        raw = {"enabled": True, "auto_fix_pr": True}
        raw.update(overrides)
        return automation_policy.cmd_set_project_automation(self.ctx, {
            "project_id": self.pid, "automation": raw,
            "expected_policy_revision": current.get("policy_revision", 0)})

    def observe(self, at, *, head=None):
        notice = {"state": "blocked", "reason": GATE_REASON,
                  "head_sha": head or self.head, "updated_at": at}
        core.update_delivery(self.conn, self.task(), at, auto_merge=notice, last_sync_at=at,
                             pr_state="OPEN", publish_failures=0,
                             pushed_sha=head or self.head)

    def next_observation(self):
        at = util.to_iso(util.parse_ts(self.auto_merge_updated) + timedelta(seconds=300))
        self.auto_merge_updated = at
        self.observe(at)
        return at

    def auto(self):
        policy = automation_policy.policy_for(self.conn, self.pid) or {}
        return auto_fix.cmd_auto_fix_pr(self.ctx, {
            "task_id": self.task_id,
            "expected_head_sha": core.delivery_of(self.task())["head_sha"],
            "expected_policy_revision": policy.get("policy_revision", 0)})

    def requests(self, kind):
        return self.conn.execute("SELECT * FROM requests WHERE kind=?", (kind,)).fetchall()

    def stored(self):
        return (core.delivery_of(self.task()) or {}).get("auto_fix") or {}

    def dispatch_count(self):
        return self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch'").fetchone()[0]


class AutoFixTest(AutoFixCase):
    def test_default_off_never_evaluates(self):
        result = self.auto()
        self.assertEqual(result["outcome"], "skipped")
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.reruns, [])

    def test_blank_notice_does_not_trigger(self):
        self.enable_auto_fix(auto_fix_kinds=["check_failure"])
        core.update_delivery(self.conn, self.task(), self.now,
                             auto_merge={"state": "waiting_checks", "reason": "检查仍在运行：ci",
                                         "head_sha": self.head, "updated_at": self.now})
        self.assertEqual(auto_fix.candidate(self.conn, self.task()), False)

    def test_check_failure_reruns_once_then_starts_one_policy_action(self):
        self.enable_auto_fix()
        first = self.auto()
        self.assertEqual(first["state"], "waiting_rerun")
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(len(self.reruns), 1)
        self.assertEqual(self.stored()["state"], "waiting_rerun")
        self.assertFalse(self.conn.in_transaction)

        self.run_info = {"head_sha": self.head, "status": "completed",
                         "run_attempt": 2, "conclusion": "failure"}
        self.next_observation()
        second = self.auto()
        self.assertEqual(second["outcome"], "started")
        self.assertEqual(second["kind"], "check_failure")
        self.assertEqual(self.dispatch_count(), 1)
        self.assertEqual(len(self.reruns), 1, "only one model-free rerun per head")
        frozen = core.review_repair_of(self.task())
        self.assertEqual(frozen["source_kind"], "check_failure")
        self.assertEqual(frozen["authorized_by"], "policy")
        self.assertIsInstance(frozen["policy_revision"], int)
        history = db.task_extra(self.task()).get("ai_action_history") or []
        self.assertTrue(any(item.get("authorized_by") == "policy" and item.get("kind") == "check_failure"
                            for item in history), history)
        event = self.conn.execute("SELECT * FROM events WHERE kind='task_ai_action_started' "
                                  "ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(event["actor"], "automation")
        request = self.requests("dispatch")[0]
        self.assertEqual(request["requested_by"], "automation")

    def test_started_projection_says_queued_not_running(self):
        self.enable_auto_fix()
        self.auto()
        self.run_info = {"head_sha": self.head, "status": "completed",
                         "run_attempt": 2, "conclusion": "failure"}
        self.next_observation()
        self.auto()
        from kanpilot import derive
        notice = derive.task_delivery_view(self.task())["auto_fix"]
        self.assertEqual(notice["state"], "started")
        self.assertIn("执行端领取", notice["reason"])
        self.assertEqual(notice["attempt"], 1)
        self.assertEqual(notice["max"], 1)

    def test_replaying_the_same_fix_does_not_start_again(self):
        self.enable_auto_fix()
        self.auto()
        self.run_info = {"head_sha": self.head, "status": "completed",
                         "run_attempt": 2, "conclusion": "failure"}
        self.next_observation()
        self.auto()
        with pytest.raises(util.KanpilotError) as error:
            self.auto()
        self.assertEqual(error.value.code, "conflict")
        self.assertEqual(self.dispatch_count(), 1)

    def test_attempt_cap_suspends_without_new_request(self):
        self.enable_auto_fix()
        db.set_extra(self.conn, self.task(), self.now, ai_action_history=[{
            "action_id": "old-action", "kind": "check_failure", "state": "completed",
            "authorized_by": "policy", "authorized_at": self.now, "policy_revision": 1,
            "head_sha": "0" * 40}])
        result = self.auto()
        self.assertEqual(result["state"], "suspended")
        self.assertIn("次数已用尽", result["reason"])
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.reruns, [])
        self.assertEqual(self.stored()["attempt"], 1)
        self.assertEqual(self.stored()["max"], 1)

    def test_paused_project_suspends(self):
        self.enable_auto_fix()
        db.set_setting(self.conn, "paused_global", True)
        result = self.auto()
        self.assertEqual(result["state"], "suspended")
        self.assertIn("暂停", result["reason"])
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.reruns, [])

    def test_exhausted_budget_suspends(self):
        self.enable_auto_fix(max_task_minutes=30)
        core.update_automation(self.conn, self.task(), self.now,
                               segment_started_at=util.to_iso(util.parse_ts(self.now) - timedelta(minutes=90)))
        result = self.auto()
        self.assertEqual(result["state"], "suspended")
        self.assertIn("额度", result["reason"])
        self.assertEqual(self.dispatch_count(), 0)

    def test_merge_conflict_is_never_auto_fixed(self):
        self.enable_auto_fix()
        self.pr.update(mergeable="CONFLICTING")
        result = self.auto()
        self.assertEqual(result["state"], "suspended")
        self.assertIn("合并冲突", result["reason"])
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.reruns, [])

    def test_unknown_rerun_receipt_is_not_replayed(self):
        self.enable_auto_fix()
        calls = []
        def broken(repo, run_id):
            calls.append(run_id)
            raise gh_client.GhError("transient", "GitHub 调用失败：EOF")
        with patch.object(gh_client, "run_rerun_failed", side_effect=broken):
            with pytest.raises(util.KanpilotError):
                self.auto()
        row = self.conn.execute("SELECT * FROM runtime_operations WHERE tool='github_run_rerun'").fetchone()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(len(calls), 1)
        self.next_observation()
        result = self.auto()
        self.assertEqual(result["state"], "waiting_rerun")
        self.assertEqual(len(calls), 1, "an unknown rerun receipt is never sent again")

    def test_policy_revision_change_between_prepare_and_start_conflicts(self):
        self.enable_auto_fix()
        value = task_ai_check_failure.prepare(self.ctx, self.task())
        revision = automation_policy.policy_for(self.conn, self.pid)["policy_revision"]
        self.enable_auto_fix(auto_fix_max_attempts=2)
        with pytest.raises(util.KanpilotError) as error:
            task_ai_action.start_policy(self.ctx, self.task(), value, policy_revision=revision)
        self.assertEqual(error.value.code, "conflict")
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(core.review_repair_of(self.task()), {})

    def test_public_start_keeps_confirm_and_user_identity(self):
        with pytest.raises(util.KanpilotError) as error:
            task_ai_action.cmd_start_task_ai_action(self.ctx, {
                "task_id": self.task_id, "kind": "check_failure",
                "expected_context_sha256": "x", "expected_config_sha256": "y"})
        self.assertEqual(error.value.code, "bad_request")
        with pytest.raises(util.KanpilotError) as error:
            task_ai_action.cmd_start_task_ai_action(self.ctx, {
                "task_id": self.task_id, "kind": "check_failure", "confirm": True,
                "authorized_by": "policy",
                "expected_context_sha256": "x", "expected_config_sha256": "y"})
        self.assertEqual(error.value.code, "bad_request")

    def test_unrelated_request_in_flight_defers(self):
        self.enable_auto_fix()
        db.create_request(self.conn, self.now, "delivery", "delivery:" + self.task_id,
                          task_id=self.task_id, requested_by="scheduler")
        result = self.auto()
        self.assertEqual(result["outcome"], "skipped")
        self.assertIn("在途", result["reason"])
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.reruns, [])

    def test_engine_step_calls_auto_fix_with_the_observation_identity(self):
        from kanpilot.engine import loop as engine_loop
        self.enable_auto_fix(auto_fix_kinds=["check_failure"])
        calls = []
        def run(command, payload):
            calls.append((command, payload))
            if command == "tick":
                return {"created": [], "skipped": []}
            return {"ok": True, "outcome": "recorded", "state": "waiting_rerun", "reason": "x"}
        recon = engine_loop.Reconciler(self.ctx.db_path, mode="live", run_command=run,
                                       enable_tick=True, enable_spawn=False)
        summary = recon.reconcile_once()
        action = next(item for item in summary["actions"] if item.get("action") == "auto_fix")
        self.assertTrue(action["executed"])
        payload = dict(calls)["auto-fix-pr"]
        self.assertEqual(payload["task_id"], self.task_id)
        self.assertEqual(payload["expected_head_sha"], self.head)
        self.assertEqual(payload["expected_policy_revision"],
                         automation_policy.policy_for(self.conn, self.pid)["policy_revision"])

    def test_engine_shadow_mode_only_logs_the_decision(self):
        from kanpilot.engine import loop as engine_loop
        self.enable_auto_fix()
        calls = []
        def run(command, payload):
            calls.append(command)
            return {}
        recon = engine_loop.Reconciler(self.ctx.db_path, mode="shadow", force_mode=True,
                                       run_command=run, enable_tick=True, enable_spawn=False)
        summary = recon.reconcile_once()
        action = next(item for item in summary["actions"] if item.get("action") == "auto_fix")
        self.assertFalse(action["executed"])
        self.assertEqual(action["reason"], "shadow_diagnostic")
        self.assertNotIn("auto-fix-pr", calls)

    def test_one_evaluation_per_delivery_observation(self):
        self.enable_auto_fix(auto_fix_kinds=["check_failure"])
        reads = []
        def counting(*args, **kwargs):
            reads.append(1)
            return dict(self.pr)
        with patch.object(gh_client, "pr_view", side_effect=counting):
            self.auto()
            first = len(reads)
            result = self.auto()
        self.assertEqual(result["outcome"], "skipped")
        self.assertEqual(len(reads), first, "the same observation is not re-read")

    def test_waiting_for_the_new_attempt_consumes_the_observation(self):
        self.enable_auto_fix(auto_fix_kinds=["check_failure"])
        self.auto()
        self.next_observation()
        reads = []
        def counting(*args, **kwargs):
            reads.append(1)
            return dict(self.pr)
        with patch.object(gh_client, "pr_view", side_effect=counting):
            first = self.auto()
            second = self.auto()
        self.assertEqual(first["state"], "waiting_rerun")
        self.assertEqual(second["outcome"], "skipped")
        self.assertEqual(len(reads), 1)

    def test_check_only_observation_triggers_without_auto_merge(self):
        self.enable_auto_fix(auto_fix_kinds=["check_failure"])
        core.update_delivery(self.conn, self.task(), self.now, auto_merge=None,
                             auto_check={"state": "blocked", "reason": GATE_REASON,
                                         "head_sha": self.head, "updated_at": self.now})
        first = self.auto()
        self.assertEqual(first["state"], "waiting_rerun")
        self.assertEqual(len(self.reruns), 1)
        self.run_info = {"head_sha": self.head, "status": "completed",
                         "run_attempt": 2, "conclusion": "failure"}
        at = util.to_iso(util.parse_ts(self.auto_merge_updated) + timedelta(seconds=300))
        self.auto_merge_updated = at
        core.update_delivery(self.conn, self.task(), at, last_sync_at=at, auto_merge=None,
                             auto_check={"state": "blocked", "reason": GATE_REASON,
                                         "head_sha": self.head, "updated_at": at})
        second = self.auto()
        self.assertEqual(second["outcome"], "started")
        self.assertEqual(self.dispatch_count(), 1)
        self.assertEqual(core.review_repair_of(self.task())["authorized_by"], "policy")

    def test_suspension_is_idempotent(self):
        self.enable_auto_fix()
        db.set_setting(self.conn, "paused_global", True)
        first = self.auto()
        second = self.auto()
        self.assertEqual(first["outcome"], "recorded")
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='auto_fix_state_changed'")
                         .fetchone()[0], 1)

