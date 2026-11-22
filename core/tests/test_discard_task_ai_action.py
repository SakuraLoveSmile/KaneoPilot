"""Discarding an unpushed one-shot AI action (KP-MG02): back to the reviewed head, one fresh review.

The stuck task is built with the real commands (action start and claim, monitor and delivery commit,
review registration, direct repair start). Only the executor, GitHub and Plus are simulated.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kanpilot import (cli, cmd_delivery_pr, cmd_rerun_checks, core, db, derive, engine_claim_scope, gh_client,
                      harness_plus_adapter, mcp_client, review_delivery, review_repair, review_resolution,
                      review_settle, runtime_operations, task_ai_discard as discard, util)
from kanpilot.engine import commands, verdict
from test_check_failure_action_flow import CheckFailureFixture
from test_git_delivery import git

FIX = {"title": "修复评审发现", "description": "只处理报告列出的阻断项", "priority": "high"}
CONFIRMED = {"state": "confirmed", "detail": None}
DROPPED_EXTRA = ("fix_request", "review_conclusion", "review_registered_at", "registered_review_identity",
                 "verification_override", "plan_execution_hold", "execution_retry", "engine_verdict")


class Crash(Exception):
    """Stands in for the process dying between two steps of the command."""


class DiscardFixture(CheckFailureFixture):
    """A manual PR task reviewed `pass` at ``self.head`` (pushed), then one check repair and N direct
    repair rounds whose reviews all came back `changes_requested` — the MediaPlayer shape."""

    def setUp(self):
        super().setUp()
        # Real PR tasks carry the v1 branch workspace; the manual-delivery gates read it.
        core.update_delivery(self.conn, self.task(), self.now, pushed_sha=self.head, workspace_version=1)
        self.reviews, self.number, self.action_id = [], 0, None
        # Starting a repair re-reads the finished source turn from Plus; every turn here is completed.
        completed = lambda session, turn: {"turn": {"sessionId": session, "turnId": turn, "status": "completed"}}  # noqa: E731
        client = patch.object(harness_plus_adapter, "_get_client", return_value=(SimpleNamespace(turn_get=completed), {}))
        client.start()
        self.addCleanup(client.stop)

    # --- building the stuck task ------------------------------------------
    def register_review(self, review, conclusion, fix=None):
        self.number += 1
        run = "discard-review-%d" % self.number
        db.merge_request_payload(self.conn, review["request_id"], self.now,
                                 review_branch_snapshot=core.branch_review_snapshot(self.task()))
        db.update_request(self.conn, review["request_id"], self.now, status="claimed", run_id=run,
                          claimed_by="kaneo-review", lease_expires_at=util.plus_seconds(600))
        report = self.reports_root / (run + ".md")
        report.write_text("# Review %d\n%s\n" % (self.number, "a finding" if fix else "nothing to fix"))
        self.call("register-review", dict(
            task_id=self.task_id, request_id=review["request_id"], run_id=run, review_key="discard-key-%d" % self.number,
            expected_execution_run_id=self.task()["run_id"], conclusion=conclusion, report_path=str(report),
            **({"fix_mode": "same_task", "fix": dict(fix)} if fix else {})))
        self.call("report", {"request_id": review["request_id"], "run_id": run, "outcome": "succeeded"})
        self.reviews.append(report)
        return report

    def claim_repair(self):
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        with patch.object(mcp_client.MCPClient, "from_server_file", return_value=service):
            claimed = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        assert claimed["claimed"], claimed
        self.run, self.dispatch = claimed["run_id"], claimed

    def stuck(self, rounds=2):
        """The action commit, its review, then ``rounds`` direct repairs each followed by a review."""
        payload = self.commit_fix()
        self.action_id = db.task_extra(self.task())["ai_action_history"][0]["action_id"]
        self.register_review(self.open_review(payload), "changes_requested", FIX)
        for index in range(rounds):
            review_repair.cmd_start_review_repair(self.ctx, self.identity(core.review_repair_of(self.task())))
            self.claim_repair()
            payload = self.commit_round("repair %d" % (index + 1))
            self.register_review(self.open_review(payload), "changes_requested", FIX)
        task = self.task()
        assert task["status"] == "in_review" and db.task_extra(task)["review_conclusion"] == "changes_requested"
        assert db.task_extra(task).get("review_repair_round", 0) == rounds
        self.dropped = [row["sha"] for row in core.delivery_of(task)["commits"][1:]]
        assert len(self.dropped) == rounds + 1 and self.dropped[-1] == core.delivery_of(task)["head_sha"]
        return self.args()

    # --- driving the command ----------------------------------------------
    def args(self, **over):
        value = dict(task_id=self.task_id, action_id=self.action_id,
                     expected_head_sha=core.delivery_of(self.task())["head_sha"],
                     expected_restore_head_sha=self.head, expected_pr_head_sha=self.head, confirm=True)
        return dict(value, **over)

    def discard(self, args=None):
        # Its own context, like a CLI call: requests it queues carry the real time, not the fixture's frozen one.
        ctx = cli.Context(self.db_path)
        self.addCleanup(ctx.close)
        return discard.cmd_discard_task_ai_action(ctx, args or self.args())

    def confirmed(self):
        return patch.object(discard, "rederive_verdict", return_value=dict(CONFIRMED))

    def crash(self, target, name, *, after):
        """Raise ``Crash`` right before (or right after) the named step runs."""
        real = getattr(target, name)

        def step(*args, **kwargs):
            if after:
                real(*args, **kwargs)
            raise Crash(name)
        return patch.object(target, name, side_effect=step)

    # --- changing the task the way a refusal case needs --------------------
    def set_extra(self, **fields):
        db.set_extra(self.conn, self.task(), self.now, **fields)

    def set_delivery(self, **fields):
        core.update_delivery(self.conn, self.task(), self.now, **fields)

    def set_action(self, **fields):
        history = [dict(item, **fields) for item in db.task_extra(self.task())["ai_action_history"]]
        self.set_extra(ai_action_history=history)

    def merge_receipt(self, status=None):
        scope = "pr-merge:" + self.task_id
        with db.write_txn(self.conn):
            if status is None:
                runtime_operations.begin_operation(self.conn, scope, "merge-1", "github", "pr_merge", {"pr": 7}, self.now)
            else:
                runtime_operations.complete_operation(self.conn, scope, "merge-1", status=status)

    def other_request(self, close=False):
        with db.write_txn(self.conn):
            if close:
                db.update_request(self.conn, self.other["request_id"], self.now, status="succeeded")
            else:
                self.other = db.create_request(self.conn, self.now, "review", "review:other", task_id=self.task_id)[0]

    def commit_in_worktree(self):
        (self.worktree / "tracked.txt").write_text("someone else\n")
        git(self.worktree, "add", "tracked.txt")
        git(self.worktree, "commit", "-m", "unregistered commit")
        return git(self.worktree, "rev-parse", "HEAD")

    # --- observing --------------------------------------------------------
    def queued_reviews(self):
        return self.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='review' AND status='queued'",
                                 (self.task_id,)).fetchall()

    def event_count(self, kind):
        return self.conn.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]

    def preserved(self):
        return git(self.repo, "for-each-ref", "refs/kaneopilot")

    def receipt(self):
        return db.get_runtime_operation(self.conn, discard._scope(self.task_id), discard._operation(self.action_id))

    def fingerprint(self):
        rows = lambda sql: [tuple(row) for row in self.conn.execute(sql)]  # noqa: E731
        return {"task": dict(self.task()), "requests": rows("SELECT * FROM requests ORDER BY rowid"),
                "operations": rows("SELECT * FROM runtime_operations ORDER BY rowid"),
                "events": rows("SELECT id FROM events ORDER BY id"), "refs": git(self.repo, "for-each-ref"),
                "head": git(self.worktree, "rev-parse", "HEAD"), "status": git(self.worktree, "status", "--porcelain")}

    def assert_discarded(self, args, *, verdict_state="confirmed"):
        """The end state every path (direct, resumed after a crash) must reach."""
        task = self.task()
        delivery, extra = core.delivery_of(task), db.task_extra(task)
        assert (delivery["head_sha"], delivery["pushed_sha"]) == (self.head, self.head)
        assert delivery["commits"] == [{"execution_run_id": "exec1", "sha": self.head}]
        assert git(self.worktree, "rev-parse", "HEAD") == self.head
        assert git(self.worktree, "status", "--porcelain") == ""
        assert git(self.repo, "rev-parse", "refs/heads/" + self.branch) == self.head
        ref = discard.preserved_ref(self.task_id, args["action_id"])
        assert git(self.repo, "rev-parse", ref) == self.dropped[-1]
        for sha in self.dropped:
            git(self.repo, "merge-base", "--is-ancestor", sha, ref)
        assert (task["status"], task["stage"], task["run_id"]) == ("in_review", "review", "exec1")
        assert task["review_report_path"] is None and task["review_report_sha256"] is None and task["review_key"] is None
        assert task["sent"] == 1 and task["send_state"] == "sent" and task["session_marker"] == self.task_id + "/exec1"
        for key in DROPPED_EXTRA:
            if key != "engine_verdict" or verdict_state == "confirmed":
                assert key not in extra, key
        assert core.review_repair_of(task) == {}
        entry = [item for item in extra["ai_action_history"] if item["action_id"] == args["action_id"]][0]
        assert (entry["state"], entry["state_title"]) == ("discarded", "已放弃")
        assert (entry["discarded_head_sha"], entry["restore_head_sha"], entry["preserved_ref"]) == (
            self.dropped[-1], self.head, ref)
        assert len(self.queued_reviews()) == 1
        assert self.event_count("task_ai_action_discarded") == 1
        assert self.receipt()["status"] == "accepted"
        assert len(self.preserved().splitlines()) == 1


class DiscardTaskAIActionTest(DiscardFixture):
    def test_discard_returns_to_the_reviewed_head_and_queues_one_review(self):
        args = self.stuck()
        reports = {path: path.read_bytes() for path in self.reviews}
        assert db.task_extra(self.task())["review_repair_round"] == 2
        # The dead end this command removes: the local head is not the PR head, so a rerun is refused.
        with pytest.raises(util.KanpilotError) as refused:
            cmd_rerun_checks.cmd_rerun_checks(self.ctx, {"task_id": self.task_id})
        # KP-MG03 #1: the stale notice is refused before any GitHub read.
        assert "旧提交" in refused.value.message

        with self.confirmed():
            result = self.discard(args)
        assert result["ok"] and result["already_discarded"] is False and result["verdict"] == CONFIRMED
        assert result["restore_head_sha"] == self.head and result["discarded_head_sha"] == self.dropped[-1]
        self.assert_discarded(args)
        assert result["request_id"] == self.queued_reviews()[0]["request_id"]

        extra = db.task_extra(self.task())
        assert extra["review_repair_round"] == 0, "the discarded rounds are given back"
        history = extra["review_history"]
        assert [item["conclusion"] for item in history] == ["pass"] + ["changes_requested"] * 3
        assert "discarded" not in history[0]
        assert all(item["discarded"]["action_id"] == args["action_id"] for item in history[1:])
        for path, content in reports.items():
            assert path.read_bytes() == content
        assert all(util.sha256_file(Path(item["report_path"])) == item["report_sha256"] for item in history)

        # Replaying the same confirmation changes nothing.
        refs, review = self.preserved(), self.queued_reviews()[0]["request_id"]
        with self.confirmed():
            again = self.discard(args)
        assert again["already_discarded"] is True and again["request_id"] == review
        assert self.preserved() == refs and len(self.queued_reviews()) == 1
        assert self.event_count("task_ai_action_discarded") == 1

        # The original dead end is gone: the failed checks can be rerun against the PR head.
        with patch.object(gh_client, "run_rerun_failed") as rerun:
            rerun_result = cmd_rerun_checks.cmd_rerun_checks(self.ctx, {"task_id": self.task_id})
        assert rerun_result["runs"] == [{"run_id": 44211608370, "status": "accepted"}]
        rerun.assert_called_once()

    def test_a_new_changes_requested_review_can_still_start_a_repair_round(self):
        args = self.stuck()
        assert review_repair._round_count(self.task()) == 2
        with self.confirmed():
            self.discard(args)
        assert review_repair._round_count(self.task()) == 0
        self.register_review(self.queued_reviews()[0], "changes_requested", FIX)
        view = review_repair.view(self.conn, self.task(), util.now_dt())
        assert view["can_start"] is True and view["round"] == 0
        review_repair.cmd_start_review_repair(self.ctx, self.identity(core.review_repair_of(self.task())))
        assert db.task_extra(self.task())["review_repair_round"] == 1

    def test_quota_is_untouched_when_no_round_followed_the_action(self):
        args = self.stuck(rounds=0)
        self.set_extra(review_repair_round=1)
        with self.confirmed():
            self.discard(args)
        assert db.task_extra(self.task())["review_repair_round"] == 1

    def test_refusals_change_nothing(self):
        args = self.stuck(rounds=1)
        local_head = core.delivery_of(self.task())["head_sha"]
        original = db.task_extra(self.task())["ai_action_history"]
        stray = self.worktree / "stray.txt"
        no_change = (lambda: None, lambda: None)
        cases = [
            ("no confirmation", *no_change, dict(confirm=False), "bad_request"),
            ("unknown action", *no_change, dict(action_id="missing"), "conflict"),
            ("local head changed since the confirmation", *no_change, dict(expected_head_sha=self.head), "conflict"),
            ("restore head is not the frozen reviewed head", *no_change,
             dict(expected_restore_head_sha=self.dropped[-1]), "conflict"),
            ("PR head differs from the confirmation", *no_change, dict(expected_pr_head_sha="0" * 40), "conflict"),
            ("dirty worktree", lambda: stray.write_text("x"), stray.unlink, {}, "conflict"),
            ("task paused", lambda: db.update_task(self.conn, self.task_id, self.now, auto_advance_paused=1),
             lambda: db.update_task(self.conn, self.task_id, self.now, auto_advance_paused=0), {}, "conflict"),
            ("review stage paused", lambda: db.set_setting(self.conn, "paused_stages", ["review"]),
             lambda: db.set_setting(self.conn, "paused_stages", []), {}, "conflict"),
            ("automation managed", lambda: self.set_extra(automation={"fix_round": 0, "segment_started_at": self.now}),
             lambda: self.set_extra(automation=None), {}, "conflict"),
            ("request in flight", self.other_request, lambda: self.other_request(close=True), {}, "conflict"),
            ("unconfirmed merge receipt", self.merge_receipt, lambda: self.merge_receipt("rejected"), {}, "conflict"),
            ("PR no longer open", lambda: self.pr.update(state="MERGED"), lambda: self.pr.update(state="OPEN"),
             {}, "conflict"),
            ("PR head moved", lambda: self.pr.update(headRefOid="f" * 40),
             lambda: self.pr.update(headRefOid=self.head), {}, "conflict"),
            ("PR from another repository", lambda: self.pr.update(isCrossRepository=True),
             lambda: self.pr.update(isCrossRepository=False), {}, "conflict"),
            ("local head moved", self.commit_in_worktree, lambda: git(self.worktree, "reset", "--hard", local_head),
             {}, "conflict"),
            ("unregistered commit on the chain", lambda: self.set_delivery(head_sha=self.commit_in_worktree()),
             lambda: (git(self.worktree, "reset", "--hard", local_head), self.set_delivery(head_sha=local_head)),
             {}, "conflict"),
            ("action still running", lambda: self.set_action(state="running"),
             lambda: self.set_extra(ai_action_history=original), {}, "conflict"),
            ("action awaiting review", lambda: self.set_action(state="awaiting_review"),
             lambda: self.set_extra(ai_action_history=original), {}, "conflict"),
            ("another kind of action", lambda: self.set_action(kind="merge_conflict"),
             lambda: self.set_extra(ai_action_history=original), {}, "conflict"),
            ("action commit already pushed", lambda: self.set_delivery(pushed_sha=local_head),
             lambda: self.set_delivery(pushed_sha=self.head), {}, "conflict"),
            ("review already passed", lambda: self.set_extra(review_conclusion="pass"),
             lambda: self.set_extra(review_conclusion="changes_requested"), {}, "conflict"),
            ("not a PR delivery", lambda: self.set_delivery(mode="manual"), lambda: self.set_delivery(mode="pr"),
             {}, "conflict"),
        ]
        for label, mutate, revert, over, code in cases:
            with self.subTest(label):
                try:
                    mutate()
                    before = self.fingerprint()
                    with pytest.raises(util.KanpilotError) as caught:
                        self.discard(self.args(**over))
                    assert caught.value.code == code, (label, caught.value.message)
                    assert self.fingerprint() == before, label
                finally:
                    revert()
        # Every refusal left the task as it was, so it can still be discarded once nothing is in the way.
        assert core.delivery_of(self.task())["head_sha"] == local_head and not self.preserved()
        with self.confirmed():
            assert self.discard(args)["ok"]
        self.assert_discarded(args)

    def interrupted(self, target, name, *, after, branch_moved, ref_exists):
        args = self.stuck(rounds=0)
        with self.crash(target, name, after=after), self.confirmed(), pytest.raises(Crash):
            self.discard(args)
        # What the interruption left behind: the old identity in the database, a receipt to resume from.
        assert core.delivery_of(self.task())["head_sha"] == self.dropped[-1] and len(self.queued_reviews()) == 0
        assert self.event_count("task_ai_action_discarded") == 0
        assert git(self.worktree, "rev-parse", "HEAD") == (self.head if branch_moved else self.dropped[-1])
        assert bool(self.preserved()) is ref_exists
        assert self.receipt()["status"] == "unknown"
        with self.confirmed():
            result = self.discard(args)
        assert result["ok"] and result["already_discarded"] is False
        self.assert_discarded(args)

    def test_interrupted_right_after_the_receipt_resumes(self):
        self.interrupted(discard, "_preserve", after=False, branch_moved=False, ref_exists=False)

    def test_interrupted_after_the_preserving_ref_resumes(self):
        self.interrupted(discard, "_preserve", after=True, branch_moved=False, ref_exists=True)

    def test_interrupted_after_the_branch_rollback_resumes(self):
        self.interrupted(discard, "_rollback", after=True, branch_moved=True, ref_exists=True)

    def test_interrupted_before_the_database_commit_resumes(self):
        self.interrupted(runtime_operations, "complete_operation", after=False, branch_moved=True, ref_exists=True)

    def test_crash_after_the_commit_resumes_without_a_second_review(self):
        args = self.stuck(rounds=0)
        with patch.object(discard, "rederive_verdict", side_effect=Crash("verdict")), pytest.raises(Crash):
            self.discard(args)
        self.assert_discarded(args, verdict_state="pending")
        with self.confirmed():
            result = self.discard(args)
        assert result["already_discarded"] is True and result["verdict"] == CONFIRMED
        self.assert_discarded(args)

    def test_verdict_outcomes_are_reported_and_a_replay_retries_them(self):
        args = self.stuck(rounds=0)
        # The restored run has no Plus evidence in this fixture, so the real derivation cannot confirm it.
        result = self.discard(args)
        assert result["ok"] and result["verdict"]["state"] in ("failed", "unconfirmed"), result
        self.assert_discarded(args, verdict_state="pending")

        def write_receipt(ok, detail=None):
            def command(ctx, payload):
                task = db.require_task(ctx.conn, payload["task_id"])
                db.set_extra(ctx.conn, task, ctx.now_iso, engine_verdict={
                    "verification_ok": ok, "detail": detail, "execution_run_id": task["run_id"],
                    "plan_sha256": task["plan_sha256"]})
                return {"ok": True}
            return command

        def broken(ctx, payload):
            raise util.KanpilotError("conflict", "token=secret-value 本机记录不符")

        def surprising(ctx, payload):
            raise RuntimeError("token=secret-value 意外错误")

        # The discard is committed by now: even an unexpected error type is reported, not raised.
        outcomes = (("failed", broken, True), ("failed", surprising, True),
                    ("unconfirmed", write_receipt(False, "no evidence"), True),
                    ("unconfirmed", write_receipt(True), False), ("confirmed", write_receipt(True), True))
        for expected, command, valid in outcomes:
            with self.subTest("%s (receipt valid: %s)" % (expected, valid)):
                with patch.object(commands, "cmd_engine_verdict", side_effect=command), \
                        patch.object(verdict, "valid_committed_receipt", return_value=valid):
                    outcome = discard.rederive_verdict(self.ctx, self.task_id, args["action_id"])
                assert outcome["state"] == expected, outcome
                assert "secret-value" not in str(outcome["detail"])
                if command is surprising:
                    assert "RuntimeError" in outcome["detail"], outcome
        # A replay whose derivation now succeeds settles it, still without a second review.
        self.set_extra(engine_verdict=None)
        with patch.object(commands, "cmd_engine_verdict", side_effect=write_receipt(True)), \
                patch.object(verdict, "valid_committed_receipt", return_value=True):
            replay = self.discard(args)
        assert replay["already_discarded"] is True and replay["verdict"] == CONFIRMED
        assert len(self.queued_reviews()) == 1 and self.event_count("task_ai_action_discarded") == 1


class DiscardedDeliveryTest(DiscardFixture):
    """The PR of a discarded action stays open; its fresh Review pass waits for one explicit continue_delivery.

    Without this the discard would only restore the review, and a manual PR task has no other way to deliver:
    settlement would try to close it locally while the PR is still unmerged.
    """

    def discard_and_pass(self, rounds=1):
        args = self.stuck(rounds=rounds)
        with self.confirmed():
            self.discard(args)
        self.register_review(self.queued_reviews()[0], "pass")
        return args

    def candidate(self):
        return review_delivery.candidate(self.conn, self.task())

    def actions(self):
        return derive.allowed_actions(self.conn, self.task(), self.ctx.now)

    def settlement_waits(self):
        """What review settlement decides from the get-task DTO: wait for the user (True) or close the task."""
        dto = self.call("get-task", {"task_id": self.task_id})["task"]
        return review_delivery.manual_release_review(review_settle.raw_task(dto))

    def continue_delivery(self, key=None):
        payload = dict(task_id=self.task_id, action="continue_delivery", expected_execution_run_id="exec1",
                       expected_review_key=key or self.task()["review_key"])
        # The formal verification coverage and the Kaneo remote read have their own tests; both are simulated here.
        with patch.object(review_delivery, "_exact_execution_coverage", return_value=True), \
                patch.object(review_resolution, "_remote"):
            return self.call("resolve", payload)

    def test_fresh_pass_offers_one_explicit_continuation_of_the_open_pr(self):
        args = self.stuck(rounds=1)
        # The task as it was stuck, and as it is while the fresh review is still pending: nothing to continue.
        assert self.candidate() is None and not self.settlement_waits()
        with self.confirmed():
            self.discard(args)
        assert self.candidate() is None and "continue_delivery" not in self.actions() and not self.settlement_waits()

        self.register_review(self.queued_reviews()[0], "pass")
        task = self.task()
        key, extra = task["review_key"], db.task_extra(task)
        assert review_delivery.discarded_pr_delivery(task) and self.settlement_waits()
        assert self.candidate() == {"execution_run_id": "exec1", "review_key": key}
        actions = self.actions()
        assert actions[0] == "continue_delivery" and not {"retry", "enqueue", "recover_review", "mark_done"} & set(actions)
        assert derive.task_display_status(self.conn, task, self.ctx.now) == "待 PR 交付"
        assert derive.task_exception(self.conn, task, self.ctx.now) is None
        reason = review_delivery.attention_fields(self.conn, task)["failure_reason"]
        assert "已有 PR" in reason and "创建 PR" not in reason
        # The authorization given before the action cannot be reused for this pass.
        assert review_delivery.continuation(task) is None

        result = self.continue_delivery(key)
        assert result["task_status"] == "in_review" and result["request"]["kind"] == "delivery"
        task = self.task()
        receipt = db.task_extra(task)["manual_review_delivery"]
        assert receipt["authorized_by"] == "user" and receipt["review_identity"] == extra["registered_review_identity"]
        assert receipt["branch"]["head_sha"] == self.head and core.delivery_of(task)["mode"] == "pr"
        assert (task["run_id"], task["review_key"]) == ("exec1", key) and core.delivery_of(task)["pr_number"] == 7
        assert git(self.worktree, "rev-parse", "HEAD") == self.head
        queued = self.conn.execute("SELECT * FROM requests WHERE kind='delivery' AND status='queued'").fetchall()
        assert len(queued) == 1
        assert review_delivery.allowed(self.conn, task, queued[0])
        assert engine_claim_scope.action_delivery_allowed(self.conn, task, queued[0])
        assert self.candidate() is None and "continue_delivery" not in self.actions()
        assert self.continue_delivery(key)["deduplicated"] is True
        assert len(self.conn.execute("SELECT 1 FROM requests WHERE kind='delivery'").fetchall()) == 1

        # The delivery worker accepts exactly this request, and the offer does not come back once it ends.
        claimed = self.call("claim", {"stage": "delivery", "executor": "kaneo-delivery",
                                      "request_id": queued[0]["request_id"]})
        assert claimed["claimed"], claimed
        cmd_delivery_pr._identity(self.ctx, dict(task_id=self.task_id, request_id=claimed["request_id"],
                                                 run_id=claimed["run_id"], expected_execution_run_id="exec1"))
        self.call("report", {"request_id": claimed["request_id"], "run_id": claimed["run_id"], "outcome": "succeeded"})
        assert review_delivery.continuation(self.task()) is not None and self.candidate() is None
        assert "continue_delivery" not in self.actions()

    def test_the_offer_closes_when_the_authority_changes(self):
        self.discard_and_pass()
        assert self.candidate() is not None
        task, extra = self.task(), db.task_extra(self.task())
        history, actions = extra["review_history"], extra["ai_action_history"]
        restore_extra = lambda: self.set_extra(review_history=history, ai_action_history=actions)  # noqa: E731
        cases = [
            ("action not discarded", lambda: self.set_action(state="needs_followup"), restore_extra),
            ("a later action took over", lambda: self.set_extra(ai_action_history=actions + [
                dict(actions[-1], action_id="later", state="awaiting_review")]), restore_extra),
            ("action delivery receipt", lambda: self.set_extra(task_ai_action_delivery={"action": {"action_id": "x"}}),
             lambda: self.set_extra(task_ai_action_delivery=None)),
            ("newest review is a discarded one", lambda: self.set_extra(review_history=history[:-1] + [
                dict(history[-1], discarded=history[1]["discarded"])]), restore_extra),
            ("newest review is not a pass", lambda: self.set_extra(review_history=history[:-1] + [
                dict(history[-1], conclusion="changes_requested")]), restore_extra),
            ("no review was discarded", lambda: self.set_extra(review_history=[
                {key: value for key, value in item.items() if key != "discarded"} for item in history]), restore_extra),
            ("PR already merged", lambda: self.set_delivery(merged_at=self.now), lambda: self.set_delivery(merged_at=None)),
            ("pushed head differs", lambda: self.set_delivery(pushed_sha="0" * 40),
             lambda: self.set_delivery(pushed_sha=self.head)),
            ("no PR", lambda: self.set_delivery(pr_number=None), lambda: self.set_delivery(pr_number=7)),
            ("automation managed", lambda: self.set_extra(automation={"fix_round": 0, "segment_started_at": self.now}),
             lambda: self.set_extra(automation=None)),
            ("task paused", lambda: db.update_task(self.conn, self.task_id, self.now, auto_advance_paused=1),
             lambda: db.update_task(self.conn, self.task_id, self.now, auto_advance_paused=0)),
            ("delivery stage paused", lambda: db.set_setting(self.conn, "paused_stages", ["delivery"]),
             lambda: db.set_setting(self.conn, "paused_stages", [])),
            ("request in flight", self.other_request, lambda: self.other_request(close=True)),
            ("review request replaced", lambda: self.conn.execute("UPDATE requests SET status='failed' WHERE kind='review'"),
             lambda: self.conn.execute("UPDATE requests SET status='succeeded' WHERE kind='review'")),
        ]
        for label, mutate, revert in cases:
            with self.subTest(label):
                try:
                    mutate()
                    assert self.candidate() is None, label
                    assert "continue_delivery" not in self.actions(), label
                    with pytest.raises(util.KanpilotError):
                        self.continue_delivery(task["review_key"])
                finally:
                    revert()
        assert self.candidate() == {"execution_run_id": "exec1", "review_key": task["review_key"]}
        assert not self.conn.execute("SELECT 1 FROM requests WHERE kind='delivery'").fetchone()

    def test_a_changes_requested_fresh_review_is_not_a_delivery_candidate(self):
        args = self.stuck(rounds=1)
        with self.confirmed():
            self.discard(args)
        self.register_review(self.queued_reviews()[0], "changes_requested", FIX)
        assert self.candidate() is None and not self.settlement_waits()
        assert "continue_delivery" not in self.actions()
