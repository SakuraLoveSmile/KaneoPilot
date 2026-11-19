"""Check repairs run in place: same branch, single parent, new review, delivery continuation."""
from unittest.mock import Mock, patch

import pytest

from helpers import goal_checks, goal_report_read
import test_task_ai_action as fixtures
from kanpilot import (cmd_dispatch, cmd_read, core, db, derive, exec_config, harness_plus_adapter,
                      mcp_client, review_repair, task_ai_action, task_ai_check_failure as kind,
                      util, verify_evidence)
from kanpilot.engine import verdict
from test_git_delivery import git

CHECK = {"__typename": "CheckRun", "name": "analyze-test", "workflowName": "app-ci",
         "status": "COMPLETED", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/owner/repo/actions/runs/44211608370/job/134491096466"}
GATE_REASON = "检查未通过：app-ci/analyze-test，修复或重跑后会自动合并"


class CheckFailureActionFlow(fixtures.ConflictActionFixture):
    """The merge gate named one failed check; one authorized repair must run end to end."""

    def setUp(self):
        super().setUp()
        self.pr.update(mergeable="MERGEABLE", statusCheckRollup=[CHECK])
        core.update_delivery(self.conn, self.task(), self.now, pr_state="OPEN", publish_failures=0,
                             auto_merge={"state": "blocked", "reason": GATE_REASON, "head_sha": self.head})
        for name, value in (("run", {"head_sha": self.head, "status": "completed",
                                     "run_attempt": 1, "conclusion": "failure"}),
                            ("failure_logs", [{"job_id": 134491096466, "name": "analyze-test",
                                               "conclusion": "failure",
                                               "content": "PathNotFoundException: index.json.tmp"}])):
            mock = patch.object(kind.release_github, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    # --- action helpers ----------------------------------------------------
    def prepare(self):
        return task_ai_action.cmd_prepare_task_ai_action(
            self.ctx, {"task_id": self.task_id, "kind": kind.KIND})["candidate"]

    def start(self, candidate=None):
        candidate = candidate or self.prepare()
        return task_ai_action.cmd_start_task_ai_action(
            self.ctx, {"task_id": self.task_id, "kind": kind.KIND, "confirm": True,
                       "expected_context_sha256": candidate["context_sha256"],
                       "expected_config_sha256": candidate["config_sha256"]})

    def identity(self, frozen):
        return {"task_id": self.task_id,
                **{key: frozen[value] for key, value in review_repair.IDENTITY.items()}}

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
        self.run, self.dispatch = claimed["run_id"], claimed
        row = self.conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
                                (self.task_id, self.run)).fetchone()
        assert util.jload(row["config_json"]) == frozen
        return claimed

    def open_worktree(self):
        path, mode = cmd_dispatch._ensure_worktree(self.task(), self.task_id, self.run, self.conn)
        assert (path, mode) == (self.worktree, "branch")
        self.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id=? AND run_id=?",
                          (str(self.worktree), self.task_id, self.run))
        plan = self.fixture["plan_path"].read_text()
        verification = verify_evidence.parse_verify_block(plan)
        db.set_extra(self.conn, self.task(), self.now,
                     plan_verify={"plan_sha256": self.task()["plan_sha256"],
                                  "commands": verification["commands"]})
        return path

    def goal_evidence(self, payload):
        task = self.task()
        config = core.review_repair_of(task)["action_config"]
        commands = db.task_extra(task)["plan_verify"]["commands"]
        self.conn.execute("UPDATE execution_rounds SET run_status='finished',session_id='session' "
                          "WHERE task_id=? AND run_id=?", (self.task_id, self.run))
        directory = self.runs_root / self.task_id / self.run
        directory.mkdir(parents=True, exist_ok=True)
        job = dict(task_id=self.task_id, run_id=self.run, backend="pi_desktop", config=config,
                   plan_sha256=task["plan_sha256"], completion_policy="command_verification",
                   verification_evidence_version=1,
                   verification_source_sha256=verdict.git_delivery.source_state_sha256(self.worktree),
                   status="finished", exit_code=0, session_id="session", turn_id="action-turn",
                   send_state="sent")
        spec = dict(job, plan_path=task["plan_path"], repository=task["repository"],
                    cwd=str(self.worktree), runs_dir=str(directory))
        for name, content in (("job.json", job), ("spec.json", spec)):
            (directory / name).write_text(util.jdump(content))
        events = [{"seq": 1, "type": "session", "session_id": "session", "turn_id": "action-turn"},
                  {"seq": 2, "type": "terminal", "outcome": "completed"}]
        (directory / "events.jsonl").write_text("".join(util.jdump(e) + "\n" for e in events))
        goal = goal_report_read(goal_checks(commands), turn_id="action-turn")
        for module, name, value in (
                (harness_plus_adapter, "plus_status",
                 {"session_id": "session", "current_turn_id": "action-turn", "is_running": False}),
                (harness_plus_adapter, "plus_turn_get",
                 {"session_id": "session", "turn_id": "action-turn", "status": "completed"})):
            mock = patch.object(module, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(cmd_read, "read_goal_report", side_effect=lambda *a, **k: goal)
        mock.start()
        self.addCleanup(mock.stop)
        with patch.object(verdict, "run_verify_commands",
                          side_effect=AssertionError("Pilot must not run verification")):
            result = self.call("engine-verdict", dict(payload, expected_plan_sha256=task["plan_sha256"],
                                                      operation_id="action-verdict"))
        assert result["verification_ok"] is True, result
        assert result["evidence_source"] == "goal_report"
        return result

    # --- scenario helpers --------------------------------------------------
    def commit_fix(self):
        self.claim_action()
        self.open_worktree()
        (self.worktree / "tracked.txt").write_text("task implementation\nfixed check\n")
        git(self.worktree, "add", "tracked.txt")
        db.update_task(self.conn, self.task_id, self.now, status="running", stage="monitor",
                       sent=1, send_state="sent", session_id="session")
        self.call("report", {"request_id": self.dispatch["request_id"], "run_id": self.run,
                             "outcome": "succeeded"})
        self.fake.tasks[self.task_id]["status"] = "in-progress"
        monitor = self.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        assert monitor["claimed"], monitor
        payload = dict(task_id=self.task_id, request_id=monitor["request_id"], run_id=monitor["run_id"],
                       expected_execution_run_id=self.run)
        self.goal_evidence(payload)
        result = self.call("delivery-commit", payload)
        assert result["outcome"] == "committed", result
        return payload

    def open_review(self, payload):
        self.call("advance-task", dict(task_id=self.task_id, run_id=payload["run_id"],
                                       target="in-review", expected_current="in-progress"))
        self.call("report", {"request_id": payload["request_id"], "run_id": payload["run_id"],
                             "outcome": "succeeded"})
        review = self.conn.execute(
            "SELECT * FROM requests WHERE task_id=? AND kind='review' AND status='queued'",
            (self.task_id,)).fetchone()
        assert review is not None
        return review

    def register_review_pass(self, review):
        db.merge_request_payload(self.conn, review["request_id"], self.now,
                                 review_branch_snapshot=core.branch_review_snapshot(self.task()))
        db.update_request(self.conn, review["request_id"], self.now, status="claimed",
                          run_id="check-review", claimed_by="kaneo-review",
                          lease_expires_at=util.plus_seconds(600))
        report = self.reports_root / "check-repair-review.md"
        report.write_text("# Review\nThe failing check was repaired in place.\n")
        self.call("register-review", dict(task_id=self.task_id, request_id=review["request_id"],
                                          run_id="check-review", expected_execution_run_id=self.run,
                                          review_key="check-review-key", conclusion="pass",
                                          report_path=str(report)))
        return db.require_task(self.conn, self.task_id)

    # --- tests -------------------------------------------------------------
    def test_prepare_freezes_the_failing_check_and_offers_one_repair(self):
        before = self.conn.total_changes, dict(self.task())
        candidate = self.prepare()
        assert candidate["kind"] == kind.KIND
        assert candidate["base_sha"] == self.original_base
        assert candidate["head_sha"] == self.head
        assert [item["name"] for item in candidate["check_runs"]] == ["analyze-test"]
        assert candidate["model_summary"]
        assert before == (self.conn.total_changes, dict(self.task()))

    def test_start_claims_the_frozen_config_and_runs_in_place(self):
        self.claim_action()
        assert self.task()["status"] == "dispatching"
        self.open_worktree()
        assert git(self.worktree, "status", "--porcelain") == ""
        assert git(self.worktree, "rev-parse", "HEAD") == self.head
        frozen = core.review_repair_of(self.task())
        assert frozen["source_kind"] == kind.KIND and frozen["state"] == "running"
        assert frozen["repair_execution_run_id"] == self.run
        merges = self.conn.execute(
            "SELECT count(*) FROM runtime_operations WHERE operation_id LIKE 'merge:%'").fetchone()[0]
        assert merges == 0, "a check repair must not prepare a conflict merge"

    def test_fix_commit_is_single_parent_and_review_then_delivery_continues(self):
        payload = self.commit_fix()
        delivery = core.delivery_of(self.task())
        assert git(self.worktree, "show", "-s", "--format=%P", "HEAD").split() == [self.head]
        assert delivery["base_sha"] == self.original_base, "in-place repair must not move the reviewed base"
        assert delivery["head_sha"] != self.head
        assert core.review_repair_of(self.task())["state"] == "running"
        review = self.open_review(payload)
        assert core.review_repair_of(self.task())["state"] == "awaiting_review"
        task = self.register_review_pass(review)
        assert core.review_repair_of(task) == {}
        receipt = task_ai_action.delivery_continuation(task, allow_paused=True)
        assert receipt and receipt["action"]["source_kind"] == kind.KIND
        assert receipt["head_sha"] == core.delivery_of(task)["head_sha"]
        projection = task_ai_action.projection(self.conn, task, self.ctx.now)
        assert projection["history"][-1]["state"] == "completed"
        assert projection["history"][-1]["kind"] == kind.KIND

    def test_second_repair_of_the_same_head_is_refused(self):
        self.start()
        assert "已安排过一次检查修复" in (kind.gate(self.conn, self.task()) or "")
        assert task_ai_action.projection(self.conn, self.task(), self.ctx.now)["can_prepare"] is False

    def test_ai_action_cannot_be_started_as_a_generic_review_repair(self):
        self.start()
        frozen = core.review_repair_of(self.task())
        assert review_repair.view(self.conn, self.task(), self.ctx.now) is None
        with pytest.raises(util.KanpilotError):
            review_repair.cmd_start_review_repair(self.ctx, self.identity(frozen))
        review_repair.try_auto_start(self.ctx, self.task_id)
        assert core.review_repair_of(self.task())["state"] == "queued"

    def test_display_follows_the_action_instead_of_a_plain_repair(self):
        self.start()
        summary = derive.task_summary(self.conn, self.task(), self.ctx.now)
        assert summary["display_status"] == "AI 处理待启动"
        assert summary["review_repair"] is None
