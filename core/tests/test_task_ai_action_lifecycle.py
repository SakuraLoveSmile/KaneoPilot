"""Real isolated Git/core lifecycle; executor transport is seeded, never billed."""
from unittest.mock import Mock, patch

from helpers import goal_checks, goal_report_read
import test_task_ai_action as fixtures
from kanpilot import (cmd_dispatch, cmd_queue, cmd_read, core, db, derive, exec_config,
                      harness_plus_adapter, repair_prompt, task_ai_action, util, verify_evidence)
from kanpilot import mcp_client
from kanpilot.engine import decide, loop, verdict
from test_git_delivery import git


class TaskAIActionLifecycle(fixtures.ConflictActionFixture):
    def claim_action(self):
        self.before_plans = self.conn.execute("SELECT count(*) FROM requests WHERE kind='plan'").fetchone()[0]
        self.start()
        frozen = core.review_repair_of(self.task())["action_config"]
        config = exec_config.load(self.conn)
        config["pi_desktop"]["model_id"] = "later-setting"
        db.set_setting(self.conn, "exec_config", config)
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert claimed["claimed"], claimed
        self.run = claimed["run_id"]
        self.dispatch = claimed
        row = self.conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
                                (self.task_id, self.run)).fetchone()
        assert util.jload(row["config_json"]) == frozen
        return claimed

    def prepare_merge(self):
        self.claim_action()
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, self.run, self.conn)
        assert path == self.worktree and mode == "branch"
        self.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
                          (str(self.worktree), self.task_id, self.run))
        plan = self.fixture["plan_path"].read_text()
        verification = verify_evidence.parse_verify_block(plan)
        db.set_extra(self.conn, self.task(), self.now,
                     plan_verify={"plan_sha256": self.task()["plan_sha256"], "commands": verification["commands"]})
        return path

    def finish_files_and_claim_monitor(self):
        self.prepare_merge()
        # Simulate only the executor's resolved files and accepted transport.
        (self.worktree / "tracked.txt").write_text("task implementation\nnew base implementation\n")
        git(self.worktree, "add", "tracked.txt")
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor",
                       sent=1, send_state="sent", session_id="session")
        self.call("report", {"request_id": self.dispatch["request_id"], "run_id": self.run, "outcome": "succeeded"})
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        monitor = self.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        assert monitor["claimed"], monitor
        return dict(task_id=self.task_id, request_id=monitor["request_id"], run_id=monitor["run_id"],
                    expected_execution_run_id=self.run)

    def goal_evidence(self, payload, mode="legacy", *, checks=None, expected_ok=True):
        mappings = db.get_setting(self.conn, "project_mappings")
        mappings["items"][self.task()["project_id"]]["engine_mode"] = mode
        db.set_setting(self.conn, "project_mappings", mappings)
        task = self.task()
        config = core.review_repair_of(task)["action_config"]
        commands = db.task_extra(task)["plan_verify"]["commands"]
        self.conn.execute("UPDATE execution_rounds SET run_status='finished',session_id='session' WHERE task_id=? AND run_id=?",
                          (self.task_id, self.run))
        directory = self.runs_root / self.task_id / self.run
        directory.mkdir(parents=True, exist_ok=True)
        job = dict(task_id=self.task_id, run_id=self.run, backend="pi_desktop", config=config,
                   plan_sha256=task["plan_sha256"], completion_policy="command_verification",
                   verification_evidence_version=1, verification_source_sha256=verdict.git_delivery.source_state_sha256(self.worktree),
                   status="finished", exit_code=0, session_id="session", turn_id="action-turn", send_state="sent")
        spec = dict(job, plan_path=task["plan_path"], repository=task["repository"], cwd=str(self.worktree), runs_dir=str(directory))
        for name, content in (("job.json", job), ("spec.json", spec)):
            (directory / name).write_text(util.jdump(content))
        events = [{"seq": 1, "type": "session", "session_id": "session", "turn_id": "action-turn"},
                  {"seq": 2, "type": "terminal", "outcome": "completed"}]
        (directory / "events.jsonl").write_text("".join(util.jdump(e) + "\n" for e in events))
        # Only the Plus structured report supplies checks, never local execution.
        self.goal = goal_report_read(goal_checks(commands) if checks is None else checks, turn_id="action-turn")
        for module, name, value in ((harness_plus_adapter, "plus_status", {"session_id": "session", "current_turn_id": "action-turn", "is_running": False}),
                                    (harness_plus_adapter, "plus_turn_get", {"session_id": "session", "turn_id": "action-turn", "status": "completed"})):
            mock = patch.object(module, name, return_value=value)
            mock.start(); self.addCleanup(mock.stop)
        mock = patch.object(cmd_read, "read_goal_report", side_effect=lambda *a, **k: self.goal)
        mock.start(); self.addCleanup(mock.stop)
        with patch.object(verdict, "run_verify_commands", side_effect=AssertionError("Pilot must not run verification")):
            result = self.call("engine-verdict", dict(payload, expected_plan_sha256=task["plan_sha256"], operation_id="action-verdict"))
        assert result["verification_ok"] is expected_ok, result
        assert result["evidence_source"] == "goal_report"
        if expected_ok:
            assert result["index_source_sha256"]
        assert db.task_extra(self.task())["engine_verdict"] == {k: v for k, v in result.items() if k != "ok"}
        return result

    def test_claim_keeps_frozen_general_config_and_prompt_is_bounded(self):
        self.prepare_merge()
        task = self.task()
        assert task["run_id"] != "exec1"
        assert task_ai_action.projection(self.conn, task, self.ctx.now)["current"]["state"] == "queued"
        assert derive.task_summary(self.conn, task, self.ctx.now)["display_status"] == "AI 处理待启动"
        assert derive.task_summary(self.conn, task, self.ctx.now)["review_repair"] is None
        spec = dict(task_id=self.task_id, repository=task["repository"], cwd=str(self.worktree),
                    run_id=self.run, plan_sha256=task["plan_sha256"], plan_path=task["plan_path"])
        spec.update(repair_prompt.freeze(task))
        body = repair_prompt.execution_body(spec)
        assert "python3 -c 'assert True'" in body
        assert "## 本轮一次性 AI 处理" in body
        assert self.fixture["plan_path"].read_text() not in body
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='plan'").fetchone()[0] == self.before_plans

    def test_merge_commit_requires_new_review_and_keeps_action_history(self):
        payload = self.finish_files_and_claim_monitor()
        self.goal_evidence(payload)
        result = self.call("delivery-commit", payload)
        assert result["outcome"] == "committed", result
        committed = core.delivery_of(self.task())
        assert git(self.worktree, "show", "-s", "--format=%P", "HEAD").split() == [self.head, self.base]
        assert committed["base_sha"] == self.base
        assert self.call("delivery-commit", payload)["outcome"] == "already_committed"
        self.call("advance-task", dict(task_id=self.task_id, run_id=payload["run_id"],
                    target="in-review", expected_current="in-progress"))
        self.call("report", {"request_id": payload["request_id"], "run_id": payload["run_id"], "outcome": "succeeded"})
        task = self.task()
        assert core.review_repair_of(task)["state"] == "awaiting_review"
        assert db.task_extra(task).get("review_conclusion") is None
        assert derive.task_summary(self.conn, task, self.ctx.now)["phase"] == "review"
        snapshot = loop.Reconciler(db_path=str(self.db_path))._snapshot(self.conn, task, self.ctx.now)
        assert snapshot["phase"] == "review" and snapshot["condition"] == "reviewing"
        snapshot["automation_managed"] = True
        assert decide.decide(snapshot).action == decide.ENQUEUE_REVIEW
        review = self.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='review' AND status='queued'",
                                   (self.task_id,)).fetchone()
        assert review is not None
        db.merge_request_payload(self.conn, review["request_id"], self.now,
                                 review_branch_snapshot=core.branch_review_snapshot(task))
        db.update_request(self.conn, review["request_id"], self.now, status="claimed", run_id="new-review",
                          claimed_by="kaneo-review", lease_expires_at=util.plus_seconds(600))
        report = self.reports_root / "after-conflict-review.md"
        report.write_text("# Review\nMerged behavior verified.\n")
        self.call("register-review", dict(task_id=self.task_id, request_id=review["request_id"], run_id="new-review",
                    expected_execution_run_id=self.run, review_key="new-review-key", conclusion="pass", report_path=str(report)))
        assert core.review_repair_of(self.task()) == {}
        history = task_ai_action.projection(self.conn, self.task(), self.ctx.now)["history"]
        assert len(history) == 1 and history[0]["state"] == "completed"
        assert history[0]["execution_run_id"] == self.run
        assert len(db.task_extra(self.task())["review_history"]) == 2
        assert db.task_extra(self.task())["review_conclusion"] == "pass"

    def test_live_commit_rejects_missing_execution_verification(self):
        payload = self.finish_files_and_claim_monitor()
        with patch("kanpilot.engine_claim_scope.effective_mode", return_value="live"):
            result = self.call("delivery-commit", payload)
        assert result["outcome"] == "blocked"
        assert result['failure_code'] == 'delivery_commit_failed'
        assert "与当前源码绑定的执行验证证据" in result["message"]
        assert git(self.worktree, "rev-parse", "HEAD") == self.head

    def test_live_binding_recognizes_only_same_run_double_parent_recovery(self):
        payload = self.finish_files_and_claim_monitor()
        receipt = self.goal_evidence(payload, "live")
        before = verdict.binding(self.conn, self.task())
        from kanpilot import git_delivery
        delivery = dict(core.delivery_of(self.task()), repository=self.task()["repository"])
        git_delivery.commit_worktree(delivery, task_id=self.task_id, title="resolve", execution_run_id=self.run,
                                    fix_round=0, merge_parent_sha=self.base,
                                    verified_source_sha256=receipt["binding"]["source_sha256"],
                                    verified_index_sha256=receipt["index_source_sha256"])
        after = verdict.binding(self.conn, self.task(), allow_core_commit=True)
        assert after["head_sha"] != before["head_sha"]
        assert dict(after, head_sha=before["head_sha"]) == before
        recovered = self.call("engine-verdict", dict(payload, expected_plan_sha256=self.task()["plan_sha256"]))
        assert recovered["deduplicated"]
        assert self.call("delivery-commit", payload)["outcome"] == "committed"
        git(self.worktree, "commit", "--allow-empty", "-m", "unrelated third commit")
        with self.assertRaises(util.KanpilotError):
            verdict.binding(self.conn, self.task(), allow_core_commit=True)

    def test_shadow_action_uses_enforced_goal_receipt_and_does_not_auto_retry(self):
        payload = self.finish_files_and_claim_monitor()
        self.goal_evidence(payload, "shadow")
        assert self.call("delivery-commit", payload)["outcome"] == "committed"
        self.call("advance-task", dict(task_id=self.task_id, run_id=payload["run_id"],
                  target="in-review", expected_current="in-progress"))
        db.update_task(self.conn, self.task_id, self.now, status="needs_user", stage="monitor",
                       failure_stage="monitor", failure_code="verification_unconfirmed")
        reconciler = loop.Reconciler(db_path=str(self.db_path))
        assert reconciler._quality_retry(self.conn, self.task(), self.now, {}, "live") is None
        assert not cmd_queue.verification_retry_candidate(self.conn, self.task())
        with self.assertRaises(util.KanpilotError):
            cmd_queue._engine_fix_guard(self.ctx, {"task_id": self.task_id, "actor": "automation", "engine_fix": {}})

    def test_legacy_without_goal_cannot_commit_or_advance_even_with_manual_override(self):
        payload = self.finish_files_and_claim_monitor()
        db.set_extra(self.conn, self.task(), self.now, verification_override={"execution_run_id": self.run})
        with self.assertRaises(util.KanpilotError):
            self.call("advance-task", dict(task_id=self.task_id, run_id=payload["run_id"],
                      target="in-review", expected_current="in-progress"))
        result = self.call("delivery-commit", payload)
        assert result["outcome"] == "blocked"
        assert git(self.worktree, "rev-parse", "HEAD") == self.head
