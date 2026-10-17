"""Live verification can advance an unchanged manual detached execution."""
from pathlib import Path
from unittest.mock import patch

from helpers import use_simulated_plus_goal_reports
from kanpilot import core, db, git_delivery, harness_plus_adapter, remote, util
import test_execution_retry as retry_fixture
from test_session import pi_event, write_json


class TestLiveManualReview(retry_fixture.TestExecutionRetry):
    def setUp(self):
        super().setUp()
        # Live verdicts read Plus's Goal report; the double mirrors this round's command results.
        use_simulated_plus_goal_reports(self)
        plan = Path(self.task()["plan_path"])
        plan.write_text(plan.read_text() + "\n# Frozen manual execution\n```verify\ntrue\n```\n")
        sha = util.sha256_file(plan)
        db.update_task(self.ctx.conn, self.task_id, self.ctx.now_iso, status="running", stage="monitor",
                       plan_sha256=sha, **core.clear_block_fields())
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                     plan_verify={"plan_sha256": sha, "commands": ["true"]})
        self.call("set-project-mapping", {"project_id": self.task()["project_id"],
                                         "repository": str(self.repo), "enabled": True,
                                         "expected_revision": self.call("get-project-mappings")["revision"]})
        self.call("set-project-engine-mode", {"project_id": self.task()["project_id"], "engine_mode": "live"})
        self.job.update(status="finished", exit_code=0, plan_sha256=sha,
                        completion_policy="command_verification", verification_evidence_version=1,
                        verification_source_sha256=git_delivery.source_state_sha256(self.worktree))
        write_json(self.directory / "job.json", self.job)
        write_json(self.directory / "spec.json", {
            "task_id": self.task_id, "run_id": self.run_id, "backend": "pi_desktop",
            "config": self.job["config"], "plan_sha256": sha,
            "completion_policy": "command_verification", "plan_path": str(plan),
            "repository": self.task()["repository"], "cwd": str(self.worktree), "runs_dir": str(self.directory)})
        (self.directory / "events.jsonl").write_text(
            util.jdump(pi_event(1, "session", session_id="failed-session", turn_id="failed-turn")) + "\n"
            + util.jdump(pi_event(2, "command_output", name="exec", turn_id="failed-turn",
                                  input=util.jdump({"command": "true", "cwd": str(self.worktree)}),
                                  exit_code=0, output="passed")) + "\n"
            + util.jdump(pi_event(3, "terminal", outcome="completed")) + "\n")
        self.plus_status["session_id"] = "failed-session"
        self.plus_turn["status"] = "completed"
        def read_turn(*args, **kwargs):
            self.assertFalse(self.ctx.conn.in_transaction)
            return {"session_id": self.plus_turn["sessionId"], "turn_id": self.plus_turn["turnId"],
                    "status": self.plus_turn["status"]}
        turn_patch = patch.object(harness_plus_adapter, "plus_turn_get", side_effect=read_turn)
        turn_patch.start()
        self.addCleanup(turn_patch.stop)
        self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id, "status_only": True})
        self.call("enqueue", {"task_id": self.task_id, "kind": "monitor"})
        claim = self.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        self.assertTrue(claim["claimed"])
        result = self.call("engine-verdict", {"task_id": self.task_id,
                           "expected_execution_run_id": self.run_id, "expected_plan_sha256": sha})
        self.assertTrue(result["verification_ok"], result)
        self.assertEqual(result["evidence_source"], "goal_report")
        self.advance ={"task_id": self.task_id, "run_id": claim["run_id"],
                        "target": "in-review", "expected_current": "in-progress"}

    def test_verified_manual_detached_dirty_results_enter_review_without_commit(self):
        head = self.git(self.worktree, "rev-parse", "HEAD")
        self.assertFalse(core.requires_branch_review(self.task()))
        self.assertEqual(self.git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.call("advance-task", self.advance)
        self.assertEqual(self.task()["status"], "in_review")
        self.assertEqual(self.fake.tasks[self.task_id]["status"], "in-review")
        self.assertEqual(self.git(self.worktree, "rev-parse", "HEAD"), head)
        self.assertEqual((self.worktree / "tracked.txt").read_text(), "partial implementation\n")
        self.assertEqual((self.worktree / "new.txt").read_text(), "keep this untracked file\n")

    def test_source_tamper_after_pass_rejects_before_local_or_remote_writes(self):
        for source in ("tracked.txt", "new.txt"):
            with self.subTest(source=source):
                path = self.worktree / source
                original = path.read_text()
                path.write_text("changed after verification\n")
                before = dict(self.task())
                events = self.ctx.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                with patch.object(remote, "update_status") as update:
                    error = self.call_err("advance-task", self.advance)
                self.assertEqual(error.code, "conflict")
                update.assert_not_called()
                self.assertEqual(dict(self.task()), before)
                self.assertEqual(self.ctx.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
                self.assertEqual(self.fake.tasks[self.task_id]["status"], "in-progress")
                path.write_text(original)

    def test_head_only_change_after_pass_rejects_without_writes(self):
        self.git(self.worktree, "commit", "--allow-empty", "-m", "unverified HEAD change")
        before = dict(self.task())
        events = self.ctx.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with patch.object(remote, "update_status") as update:
            error = self.call_err("advance-task", self.advance)
        self.assertEqual(error.code, "conflict")
        update.assert_not_called()
        self.assertEqual(dict(self.task()), before)
        self.assertEqual(self.ctx.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertEqual(self.fake.tasks[self.task_id]["status"], "in-progress")


for _name in retry_fixture.TestExecutionRetry.__dict__:
    if _name.startswith("test_") and _name not in TestLiveManualReview.__dict__:
        setattr(TestLiveManualReview, _name, None)
