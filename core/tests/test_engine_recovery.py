"""Actual automation resolve/queue boundary, disposable Git + fake Kaneo/Plus."""
from pathlib import Path
import os
import time
from unittest.mock import patch

from kanpilot import cmd_queue, core, db, remote, util
from kanpilot.engine import ladder, loop
import test_execution_retry as retry_fixture
from test_session import pi_event, write_json


class TestEngineRecovery(retry_fixture.TestExecutionRetry):
    def setUp(self):
        super().setUp()
        self.git(self.worktree, "switch", "-c", "kaneo/retry-task")
        core.update_automation(self.ctx.conn, self.task(), self.ctx.now_iso,
                               manual_takeover=False, adopted_at=self.ctx.now_iso,
                               segment_started_at=self.ctx.now_iso, segment_cost_base_usd=0)
        core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, mode="pr", branch="kaneo/retry-task",
                             worktree_path=str(self.worktree), head_sha=self.git(self.worktree, "rev-parse", "HEAD"))
        pid = self.task()["project_id"]
        self.call("set-project-mapping", {"project_id": pid, "repository": str(self.repo), "enabled": True,
                                         "expected_revision": self.call("get-project-mappings")["revision"]})
        self.call("set-project-engine-mode", {"project_id": pid, "engine_mode": "live"})
        self.call("set-project-automation", {"project_id": pid, "automation": {"enabled": True}})
        self.job.update(status="finished", exit_code=0)
        write_json(self.directory / "job.json", self.job)
        (self.directory / "events.jsonl").write_text(util.jdump(pi_event(1, "session", session_id="failed-session", turn_id="failed-turn")) + "\n"
            + util.jdump(pi_event(2, "terminal", outcome="completed")) + "\n")
        self.plus_turn["status"] = "completed"
        self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id, "status_only": True})

    def quality_failure(self, *, empty=True):
        command = "true" if empty else "false"
        if empty:
            (self.worktree / "tracked.txt").write_text("base\n")
            (self.worktree / "new.txt").unlink(missing_ok=True)
        plan = Path(self.task()["plan_path"])
        plan.write_text("# Frozen\n```verify\n" + command + "\n```\n")
        sha = util.sha256_file(plan)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, plan_sha256=sha)
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                     plan_verify={"plan_sha256": sha, "commands": [command]})
        self.call("engine-verdict", {"task_id": self.task_id, "expected_execution_run_id": self.run_id,
                                     "expected_plan_sha256": sha})
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso,
                       failure_code="verification_unconfirmed", failure_stage="monitor")
        return {"task_id": self.task_id, "action": "resume" if empty else "replan", "actor": "automation",
                "expected_execution_run_id": self.run_id,
                "engine_fix": {"execution_run_id": self.run_id, "plan_sha256": sha,
                               "reason": "empty_diff" if empty else "verify_failed"}}

    def test_empty_retry_creates_one_new_request_and_preserves_plan(self):
        payload = self.quality_failure()
        first = self.call("resolve", payload)
        self.assertEqual(first["task_status"], "ready")
        self.assertEqual(first["request"]["kind"], "dispatch")
        request = db.get_request(self.ctx.conn, first["request"]["request_id"])
        self.assertEqual(request["requested_by"], "automation")
        self.assertIsNone(self.task()["run_id"])
        self.assertEqual(self.task()["plan_sha256"], payload["engine_fix"]["plan_sha256"])
        self.assertEqual(len(db.task_extra(self.task())["engine_fix_rounds"]), 1)
        self.assertTrue(self.call("resolve", payload)["deduplicated"])
        rows = self.ctx.conn.execute("SELECT actor FROM events WHERE kind IN ('resolved','execution_retry_requested','engine_fix_started')").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["actor"] == "automation" for row in rows))

    def test_reconciler_replans_verify_failure_without_user_resolve(self):
        self.quality_failure(empty=False)
        initial = core.automation_of(self.task())["segment_started_at"]
        summary = loop.Reconciler(self.db_path).reconcile_once()
        action = next(a for a in summary["actions"] if a.get("task_id") == self.task_id)
        self.assertTrue(action.get("executed"), action)
        self.assertEqual(self.task()["status"], "needs_plan")
        self.assertTrue(core.automation_of(self.task())["plan_wanted"])
        self.assertEqual(core.automation_of(self.task())["segment_started_at"], initial)
        self.assertEqual(len(db.task_extra(self.task())["engine_fix_rounds"]), 1)
        event = self.ctx.conn.execute("SELECT detail_json FROM events WHERE kind='resolved' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(util.jload(event["detail_json"])["actor"], "automation")

    def test_task_wide_limit_survives_other_plan_hashes(self):
        payload = self.quality_failure(empty=False)
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, engine_fix_rounds=[
            {"execution_run_id": "old1", "plan_sha256": "different1", "reason": "verify_failed"},
            {"execution_run_id": "old2", "plan_sha256": "different2", "reason": "verify_failed"}])
        self.call("resolve", payload)
        self.assertEqual(self.task()["failure_code"], "fix_rounds_exhausted")
        self.assertEqual(len(db.task_extra(self.task())["engine_fix_rounds"]), 2)

    def test_prior_empty_on_other_plan_does_not_consume_new_plan_retry(self):
        payload = self.quality_failure()
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, engine_fix_rounds=[
            {"execution_run_id": "old1", "plan_sha256": "other-plan", "reason": "empty_diff"}])
        self.assertEqual(self.call("resolve", payload)["task_status"], "ready")
        self.assertEqual(len(db.task_extra(self.task())["engine_fix_rounds"]), 2)

    def test_forged_model_and_events_are_ignored(self):
        payload = self.quality_failure()
        payload["engine_fix"]["plan"] = {"snapshot": {"provider_id": "forged", "model_id": "forged"},
                                           "events": [{"kind": "forged", "detail": {}}]}
        self.call("resolve", payload)
        self.assertIsNone(db.task_extra(self.task()).get("engine_model_override"))
        self.assertFalse(self.ctx.conn.execute("SELECT 1 FROM events WHERE kind='forged'").fetchone())

    def test_remote_failure_never_consumes_fix_round(self):
        payload = self.quality_failure()
        with patch.object(remote, "get_task", side_effect=util.KanpilotError("remote_unavailable", "offline")):
            self.assertIsInstance(self.call_err("resolve", payload), Exception)
        self.assertNotIn("engine_fix_rounds", db.task_extra(self.task()))
        self.assertEqual(self.task()["status"], "needs_user")

    def test_shadow_and_stale_receipts_cannot_authorize_retry(self):
        payload = self.quality_failure()
        self.call("set-project-engine-mode", {"project_id": self.task()["project_id"], "engine_mode": "shadow"})
        self.assertEqual(self.call_err("resolve", payload).code, "conflict")
        self.call("set-project-engine-mode", {"project_id": self.task()["project_id"], "engine_mode": "live"})
        payload["engine_fix"]["execution_run_id"] = "stale"
        self.assertEqual(self.call_err("resolve", payload).code, "conflict")

    def test_explicit_execution_failure_never_automatically_retries(self):
        self.quality_failure()
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, failure_code="execution_failed")
        actions = loop.Reconciler(self.db_path).reconcile_once()["actions"]
        self.assertFalse(any(a.get("command") == "resolve" for a in actions))
        self.assertNotIn("engine_fix_rounds", db.task_extra(self.task()))

    def test_force_live_resolves_quality_failure_on_legacy_mapping(self):
        self.quality_failure()
        self.call("set-project-engine-mode", {"project_id": self.task()["project_id"], "engine_mode": "legacy"})
        actions = loop.Reconciler(self.db_path, mode="live", force_mode=True).reconcile_once()["actions"]
        self.assertTrue(any(a.get("command") == "resolve" and a.get("executed") for a in actions), actions)
        self.assertEqual(self.task()["status"], "ready")

    def test_configuration_race_cannot_apply_a_stale_model_plan(self):
        payload = self.quality_failure()
        original = remote.get_task
        def changed(client, task_id):
            result = original(client, task_id)
            self.call("set-project-fallback-model", {"project_id": self.task()["project_id"],
                                                      "fallback_model": {"provider_id": "new", "model_id": "new"}})
            return result
        with patch.object(remote, "get_task", side_effect=changed):
            self.assertEqual(self.call_err("resolve", payload).code, "conflict")
        self.assertNotIn("engine_fix_rounds", db.task_extra(self.task()))

    def test_fixed_verify_environment_resumes_original_execution_without_prompt(self):
        from kanpilot import auto_dispatch, auto_monitor
        command = "kp_fixture_environment_verify"
        plan = Path(self.task()["plan_path"])
        plan.write_text("# Frozen environment verification\n```verify\n" + command + "\n```\n")
        sha = util.sha256_file(plan)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, status="running", stage="monitor",
                       plan_sha256=sha, **core.clear_block_fields())
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                     plan_verify={"plan_sha256": sha, "commands": [command]})
        # This disposable fixture uses a local hyphenated task ID; its branch
        # workspace follows the existing versioned local-task contract.
        core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, workspace_version=1,
                             base_branch="main", base_sha=self.git(self.worktree, "rev-parse", "HEAD"))
        self.job.update(plan_sha256=sha, completion_policy="command_verification")
        write_json(self.directory / "job.json", self.job)
        dispatch_count = self.ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='dispatch'").fetchone()[0]
        original_rounds = self.ctx.conn.execute("SELECT COUNT(*) FROM execution_rounds").fetchone()[0]

        def complete_existing_monitor():
            controller = auto_dispatch.Controller("monitor", self.db_path, 600, self.root / "environment-monitor.jsonl",
                                                   time.sleep, time.monotonic)
            self.assertTrue(controller.claim_next())
            view = self.call("get-task", {"task_id": self.task_id})["task"]
            # This fixture has no real execution/model call. Cost sampling is
            # separate from the core verification/report/commit/advance path.
            with patch.object(auto_monitor, "_record_cost"):
                auto_monitor._complete(controller, view, {"backend": "pi_desktop"}, "in-progress", [])

        self.call("enqueue", {"task_id": self.task_id, "kind": "monitor", "actor": "automation"})
        complete_existing_monitor()
        self.assertEqual(self.task()["failure_code"], "verification_environment_failure")
        self.assertEqual(ladder.classify(self.task()["failure_code"]), ladder.LADDER_ENVIRONMENTAL)
        self.assertEqual(self.task()["run_id"], self.run_id)
        self.assertIn("resume", self.call("get-task", {"task_id": self.task_id})["task"]["allowed_actions"])

        tools = self.root / "fixed-verify-tools"
        tools.mkdir()
        binary = tools / command
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self.call("set-project-verify-env", {"project_id": self.task()["project_id"],
                                            "verify_env": {"PATH": str(tools) + os.pathsep + os.environ["PATH"]}})
        result = self.call("resolve", {"task_id": self.task_id, "action": "resume", "actor": "user"})
        self.assertEqual(result["task_status"], "running")
        self.assertEqual(result["request"]["kind"], "monitor")
        self.assertEqual(self.task()["run_id"], self.run_id)
        self.assertEqual(self.task()["plan_sha256"], sha)
        self.assertEqual(core.delivery_of(self.task())["worktree_path"], str(self.worktree))
        complete_existing_monitor()
        self.assertEqual(self.task()["status"], "in_review", util.jdump(db.task_extra(self.task()).get("engine_verdict")))
        self.assertTrue(db.task_extra(self.task())["engine_verdict"]["verification_ok"])
        self.assertFalse(db.task_extra(self.task()).get("engine_fix_rounds"))
        self.assertEqual(self.ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='dispatch'").fetchone()[0], dispatch_count)
        self.assertEqual(self.ctx.conn.execute("SELECT COUNT(*) FROM execution_rounds").fetchone()[0], original_rounds)


# Reuse the disposable execution fixture without collecting its independent
# manual-retry suite a second time under a different automation configuration.
for _name in retry_fixture.TestExecutionRetry.__dict__:
    if _name.startswith("test_") and _name not in TestEngineRecovery.__dict__:
        setattr(TestEngineRecovery, _name, None)
