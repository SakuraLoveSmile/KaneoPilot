"""Authorized one-shot actions continue through real report/claim in local fixtures."""
import os
from datetime import timedelta
from unittest.mock import patch

from kanpilot import (core, db, derive, engine_claim_scope, gh_client, git_delivery, process_owner,
                      remote, review_settle, task_ai_action, util)
from kanpilot.engine import health, loop
import test_task_ai_action as fixtures
import test_task_ai_action_lifecycle as lifecycle
from test_git_delivery import git


class TaskAIActionScheduler(fixtures.ConflictActionFixture):
    claim_action = lifecycle.TaskAIActionLifecycle.claim_action
    prepare_merge = lifecycle.TaskAIActionLifecycle.prepare_merge
    finish_files_and_claim_monitor = lifecycle.TaskAIActionLifecycle.finish_files_and_claim_monitor
    goal_evidence = lifecycle.TaskAIActionLifecycle.goal_evidence

    def engine_env(self, mode="legacy"):
        health.acquire_or_renew_lease(self.conn, util.now_iso(), instance_id="action-engine",
                                     stage=None, pid=os.getpid(), lease_seconds=1200)
        return {engine_claim_scope.WORKER_ENV: "1", engine_claim_scope.MODE_ENV: mode,
                engine_claim_scope.FORCE_ENV: "0", process_owner.INSTANCE_ENV: "action-engine"}

    def prepare_review(self, mode="legacy"):
        monitor = self.finish_files_and_claim_monitor()
        self.goal_evidence(monitor, mode)
        assert self.call("delivery-commit", monitor)["outcome"] == "committed"
        self.call("advance-task", dict(task_id=self.task_id, run_id=monitor["run_id"], target="in-review",
                                      expected_current="in-progress"))
        self.call("report", dict(request_id=monitor["request_id"], run_id=monitor["run_id"], outcome="succeeded"))
        with patch.dict(os.environ, self.engine_env(mode)):
            review = self.call("claim", {"stage": "review", "executor": "kaneo-review"})
        assert review["claimed"], review
        return review

    def pass_review(self, mode="legacy", *, settle=False, pause_kind=None):
        review = self.prepare_review(mode)
        self.review = review
        if pause_kind:
            self.set_pause(pause_kind, True)
        if settle:
            task = self.call("get-task", {"task_id": self.task_id})["task"]
            request = self.call("get-request", {"request_id": review["request_id"]})["request"]
            result = review_settle.settle(db_path=self.db_path, task=task, request=request, run_id=review["run_id"],
                result={"conclusion": "pass", "report_markdown": "# Review\n合并冲突已解决且验证通过。", "fix": None},
                backend="pi", cost_usd=None)
            assert result["exit_code"] == 0, result
        else:
            report = self.reports_root / "action-pass.md"; report.write_text("# Review\n处理后通过\n")
            self.call("register-review", dict(task_id=self.task_id, request_id=review["request_id"], run_id=review["run_id"],
                    expected_execution_run_id=self.run, review_key="after-conflict-pass", conclusion="pass", report_path=str(report)))
            assert not core.review_repair_of(self.task())
            self.call("report", dict(request_id=review["request_id"], run_id=review["run_id"],
                                     outcome="succeeded"))
        return review

    def queued_delivery(self):
        return self.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='delivery' AND status='queued'", (self.task_id,)).fetchone()

    def set_pause(self, kind, paused):
        if kind == "task":
            db.update_task(self.conn, self.task_id, util.now_iso(), auto_advance_paused=int(paused))
        else:
            self.call("set-paused", {"stage": "delivery", "paused": paused})

    def assert_paused_request_resumes(self, kind, request):
        request_id = request["request_id"]
        assert engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request, allow_paused=True)
        assert not engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request)
        assert not engine_claim_scope.action_delivery_allowed(self.conn, dict(self.task(), run_id="unrelated-run"), request, allow_paused=True)
        with patch.dict(os.environ, self.engine_env()):
            blocked = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery", "request_id": request_id})
        assert not blocked["claimed"], blocked
        assert db.get_request(self.conn, request_id)["status"] == "queued"
        if kind == "task":
            assert self.task()["auto_advance_paused"] == 1
        else:
            assert db.paused(self.conn, "delivery")
        self.set_pause(kind, False)
        with patch.dict(os.environ, self.engine_env()):
            resumed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery", "request_id": request_id})
        assert resumed["claimed"] and resumed["request_id"] == request_id, resumed

    def test_task_paused_review_still_registers_and_queues_same_delivery(self):
        self.pass_review(settle=True, pause_kind="task")
        self.assert_paused_request_resumes("task", self.queued_delivery())

    def test_delivery_paused_review_still_registers_and_queues_same_delivery(self):
        self.pass_review(settle=True, pause_kind="delivery")
        self.assert_paused_request_resumes("delivery", self.queued_delivery())

    def delivery_report_paused(self, kind):
        self.pass_review()
        with patch.dict(os.environ, self.engine_env()):
            claimed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery"})
        assert claimed["claimed"]
        self.set_pause(kind, True)
        self.call("report", dict(request_id=claimed["request_id"], run_id=claimed["run_id"], outcome="succeeded"))
        queued = self.queued_delivery()
        assert queued is not None and queued["request_id"] != claimed["request_id"]
        self.assert_paused_request_resumes(kind, queued)

    def test_task_pause_keeps_delivery_report_continuation(self):
        self.delivery_report_paused("task")

    def test_delivery_pause_keeps_delivery_report_continuation(self):
        self.delivery_report_paused("delivery")

    def publish_original_pr(self, claimed):
        head = core.delivery_of(self.task())["head_sha"]
        self.pr["headRefOid"] = head
        self.pr["mergeable"] = "MERGEABLE"
        actual_parser = git_delivery.github_repository
        def local_repo(url, **kwargs):
            return "owner/repo" if url == str(self.bare) else actual_parser(url, **kwargs)
        marker = "[kaneopilot:delivery:" + self.task_id + "]"
        with patch.object(git_delivery, "github_repository", side_effect=local_repo), patch.object(
                gh_client, "pr_list", return_value=[dict(self.pr)]), patch.object(
                remote, "find_marker_comment", return_value={"id": "existing-comment", "content": marker + " 已创建 PR：https://github.com/owner/repo/pull/7"}), patch.object(
                gh_client, "pr_create", side_effect=AssertionError("must reuse original PR")):
            result = self.call("delivery-publish", dict(task_id=self.task_id, request_id=claimed["request_id"], run_id=claimed["run_id"], expected_execution_run_id=self.run))
        assert result["outcome"] == "published", result
        assert core.delivery_of(self.task())["pr_number"] == 7
        assert git(self.bare, "rev-parse", "refs/heads/" + self.branch) == head

    def test_legacy_review_settles_to_delivery_and_engine_pushes_existing_pr(self):
        self.pass_review(settle=True)
        assert self.task()["status"] == "in_review"  # No premature advance(done).
        request = self.queued_delivery()
        assert request is not None
        assert engine_claim_scope.manual_review_repair(self.conn, request)
        assert loop.Reconciler(self.db_path, mode="legacy")._queued_stages(self.conn, {}) == {"delivery": 1}
        with patch.dict(os.environ, self.engine_env()):
            claimed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery"})
        assert claimed["claimed"], claimed
        self.publish_original_pr(claimed)

    def test_only_current_pass_action_identity_grants_delivery(self):
        self.pass_review()
        request = self.queued_delivery()
        assert engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request)
        original = dict(self.task())
        for fields in ({"run_id": "unrelated-run"}, {"plan_sha256": "f" * 64}, {"repository": str(self.root / "other")},
                       {"requirements_sha256": "e" * 64}, {"task_id": "other-task"}):
            altered = dict(original, **fields)
            assert not engine_claim_scope.action_delivery_allowed(self.conn, altered, request)
        delivery = core.delivery_of(self.task())
        core.update_delivery(self.conn, self.task(), self.now, head_sha=self.head)
        assert not engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request)
        core.update_delivery(self.conn, self.task(), self.now, **delivery)
        extra = db.task_extra(self.task())
        old_history = extra["ai_action_history"]
        db.set_extra(self.conn, self.task(), self.now, ai_action_history=[dict(old_history[0], state="awaiting_review")])
        assert not engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request)
        db.set_extra(self.conn, self.task(), self.now, ai_action_history=old_history)
        saved_receipt = db.task_extra(self.task())["task_ai_action_delivery"]
        db.set_extra(self.conn, self.task(), self.now, task_ai_action_delivery=None)
        assert not engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request)
        db.set_extra(self.conn, self.task(), self.now, task_ai_action_delivery=saved_receipt)
        payload = db.request_payload(request)
        for key, value in (("task_ai_action_id", "old-action"), ("expected_execution_run_id", "old-run"),
                           ("expected_head_sha", self.head), ("expected_plan_sha256", "e" * 64)):
            changed = dict(request, payload_json=util.jdump(dict(payload, **{key: value})))
            assert not engine_claim_scope.action_delivery_allowed(self.conn, self.task(), changed)

    def test_live_report_and_business_tick_deduplicate_same_delivery(self):
        self.pass_review("live")
        request = self.queued_delivery()
        core.update_automation(self.conn, self.task(), self.now, fix_round=0)
        for _ in range(2):
            self.call("tick", {})
        rows = self.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='delivery' AND status IN ('queued','claimed')", (self.task_id,)).fetchall()
        assert len(rows) == 1 and rows[0]["request_id"] == request["request_id"]

    def test_blocked_review_has_no_delivery_continuation(self):
        review = self.prepare_review("shadow")
        report = self.reports_root / "blocked-after-conflict.md"; report.write_text("# Review\n需要补充证据\n")
        self.call("register-review", dict(task_id=self.task_id, request_id=review["request_id"], run_id=review["run_id"],
                    expected_execution_run_id=self.run, review_key="blocked-review", conclusion="blocked", report_path=str(report)))
        self.call("report", dict(request_id=review["request_id"], run_id=review["run_id"], outcome="needs_user"))
        assert self.queued_delivery() is None
        assert task_ai_action.delivery_continuation(self.task()) is None
        assert not engine_claim_scope.action_delivery_allowed(self.conn, self.task())

    def test_manual_takeover_rejected_before_queue_or_model_cost(self):
        core.update_automation(self.conn, self.task(), self.now, manual_takeover=True)
        before = self.conn.total_changes
        with self.assertRaisesRegex(util.KanpilotError, "人工处理"):
            self.prepare()
        assert self.conn.total_changes == before
        assert self.conn.execute("SELECT count(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 0

    def test_successful_delivery_report_schedules_only_its_next_sync(self):
        self.pass_review("shadow")
        with patch.dict(os.environ, self.engine_env("shadow")):
            claimed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery"})
        assert claimed["claimed"]
        self.publish_original_pr(claimed)
        result = self.call("delivery-sync", dict(task_id=self.task_id, request_id=claimed["request_id"], run_id=claimed["run_id"], expected_execution_run_id=self.run))
        assert result["outcome"] == "waiting"
        next_sync = result["next_sync_at"]
        self.call("report", dict(request_id=claimed["request_id"], run_id=claimed["run_id"], outcome="succeeded"))
        queued = self.queued_delivery()
        assert queued is not None and queued["not_before"] == next_sync
        assert engine_claim_scope.action_delivery_allowed(self.conn, self.task(), queued)
        self.pr.update(state="MERGED", mergedAt=next_sync)
        later = util.parse_ts(next_sync) + timedelta(seconds=1)
        with patch.object(util, "now_dt", return_value=later), patch.dict(os.environ, self.engine_env("shadow")):
            resumed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery"})
            assert resumed["claimed"], resumed
            identity = dict(task_id=self.task_id, request_id=resumed["request_id"], run_id=resumed["run_id"], expected_execution_run_id=self.run)
            assert self.call("delivery-sync", identity)["outcome"] == "merged"
            self.call("advance-task", dict(identity, target="done", expected_current="in-review"))
            self.call("report", dict(request_id=resumed["request_id"], run_id=resumed["run_id"], outcome="succeeded"))
            assert self.task()["status"] == "done"
            assert derive.task_summary(self.conn, self.task(), later)["display_status"] == "已完成"
            assert self.queued_delivery() is None
            assert self.fake.tasks[self.task_id]["status"] == "done"
