"""Release delivery gates at the public command and read-model boundaries.

All state is temporary; GitHub/Kaneo calls are replaced at their adapters.
"""
from unittest.mock import patch
import pytest

from helpers import KanpilotTestCase
from kanpilot import automation_policy, cli, core, db, derive, remote, util
from test_delivery_pr import DeliveryPR as _DeliveryPR


POLICY = dict(enabled=True, channel="prerelease",
              workflow_path=".github/workflows/pilot-release.yml", workflow_ref="main",
              version_base="0.3.0", required_assets=["Music.zip"], required_jobs=["Test"])
PENDING = dict(code="release_required", message="安装包尚未发布，不能完成任务")


class ReleaseIntegration(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.make_ready_task(task_id="task1234")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso,
                           status="in_review", stage="review", run_id="exec1")
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, review_conclusion="pass")

    def task(self):
        return db.require_task(self.ctx.conn, "task1234")

    def test_policy_keeps_release_configuration_and_defaults_to_preview(self):
        value = automation_policy.validate_automation(dict(release=POLICY))
        self.assertEqual(value["release"], POLICY)
        old = automation_policy.validate_automation({})["release"]
        self.assertFalse(old["enabled"])
        self.assertEqual(old["channel"], "prerelease")

    def test_invalid_release_configuration_is_not_silently_discarded(self):
        for value in (True, dict(POLICY, channel="latest"),
                      dict(POLICY, required_assets=[]),
                      dict(POLICY, workflow_path="../release.yml")):
            with self.subTest(value=value), self.assertRaises(util.KanpilotError):
                automation_policy.validate_automation(dict(release=value))

    def test_release_public_commands_have_fixed_schemas(self):
        for name, valid in (
            ("prepare-task-release", dict(task_id="task1234")),
            ("start-task-release", dict(task_id="task1234", expected_context_sha256="context")),
            ("sync-release-batch", dict(batch_id="batch")),
            ("sync-release-batches", dict(limit=3)),
            ("abandon-unsent-release", dict(batch_id="batch", expected_context_sha256="context")),
        ):
            self.assertIn(name, cli.COMMANDS)
            cli.validate_payload(name, valid)
            with self.assertRaises(util.KanpilotError):
                cli.validate_payload(name, dict(valid, override=True))
        with self.assertRaises(util.KanpilotError):
            cli.validate_payload("start-task-release", dict(task_id="task1234"))

    def test_older_settings_client_preserves_enabled_release_and_revision_guard(self):
        pid = self.task()["project_id"]
        first = self.call("set-project-automation", dict(project_id=pid,
                          automation=dict(release=POLICY), expected_policy_revision=0))
        second = self.call("set-project-automation", dict(project_id=pid,
                           automation=dict(auto_merge_pr=True), expected_policy_revision=1))
        self.assertEqual(first["automation"]["release"], second["automation"]["release"])
        self.assertEqual(second["policy_revision"], 2)
        with self.assertRaises(util.KanpilotError):
            self.call("set-project-automation", dict(project_id=pid,
                automation=dict(release=dict(POLICY, enabled=False)), expected_policy_revision=1))
        self.assertTrue(automation_policy.policy_for(self.ctx.conn, pid)["release"]["enabled"])

    def test_advance_cannot_write_kaneo_done_before_release(self):
        with patch("kanpilot.release_delivery.completion_gate", return_value=PENDING), \
                patch.object(remote, "get_client") as client, \
                patch.object(remote, "update_status") as update:
            with self.assertRaises(util.KanpilotError):
                self.call("advance-task", dict(task_id="task1234", target="done",
                    expected_current="in-review"))
        client.assert_not_called()
        update.assert_not_called()
        self.assertEqual(self.task()["status"], "in_review")
        self.assertFalse(db.get_setting(self.ctx.conn, "lifecycle_owner"))

    def test_manual_mark_done_cannot_bypass_release(self):
        with patch("kanpilot.release_delivery.completion_gate", return_value=PENDING):
            with self.assertRaises(util.KanpilotError):
                self.call("resolve", dict(task_id="task1234", action="mark_done", note="manual"))
        self.assertEqual(self.task()["status"], "in_review")

    def test_remote_done_is_observed_without_bypassing_release(self):
        with patch("kanpilot.release_delivery.completion_gate", return_value=PENDING):
            self.call("sync-tasks", dict(executor="isolated-sync", projects=[dict(
                project_id=self.task()["project_id"], project_name=self.task()["project_name"],
                tasks=[dict(task_id="task1234", title=self.task()["title"], kaneo_status="done",
                            repository=self.task()["repository"])])]))
        self.assertEqual(self.task()["status"], "in_review")
        self.assertEqual(self.task()["kaneo_status"], "done")

    def test_projection_distinguishes_failed_blocked_unknown_and_published(self):
        for state, severity in (("building", None), ("failed", "failed"),
                                ("blocked", "blocked"), ("unknown", "blocked"),
                                ("published", None)):
            value = dict(state=state, title="发布" + state, reason="发布结果说明", required=True,
                channel="prerelease", root_task_id="task1234", member_task_ids=["task1234"],
                assets=[], blockers=[], can_start=False, can_reconcile=True, batch_id="batch")
            with self.subTest(state=state), \
                    patch("kanpilot.release_delivery.projection", return_value=value), \
                    patch("kanpilot.release_delivery.completion_gate", return_value=PENDING):
                before = list(self.ctx.conn.iterdump())
                view = derive.task_detail(self.ctx.conn, self.task(), self.ctx.now)
                self.assertEqual(view["release"], value)
                self.assertEqual(view["board_column"], "in_review")
                self.assertEqual(view["board_substatus"], value["title"])
                self.assertEqual(view["display_status"], value["title"])
                self.assertEqual((view["phase"], view["condition"]), ("delivery", "syncing"))
                self.assertEqual(view["exception"]["severity"] if view["exception"] else None, severity)
                self.assertNotIn("mark_done", view["allowed_actions"])
                self.assertNotIn("enqueue", view["allowed_actions"])
                self.assertEqual(list(self.ctx.conn.iterdump()), before)

    def test_historical_done_remains_done_even_when_release_is_ready(self):
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="done", stage="done")
        value = dict(state="ready", title="可补发版本", reason="历史完成任务", required=False,
                     channel="prerelease", root_task_id="task1234", member_task_ids=["task1234"],
                     assets=[], blockers=[], can_start=True, can_reconcile=False)
        with patch("kanpilot.release_delivery.projection", return_value=value):
            view = derive.task_summary(self.ctx.conn, self.task(), self.ctx.now)
        self.assertEqual(view["release"], value)
        self.assertEqual(view["display_status"], "已完成")
        self.assertEqual(view["board_column"], "done")

    def test_tick_polls_existing_releases_outside_write_transaction_and_in_scope(self):
        pid = self.task()["project_id"]
        def poll(ctx, *, project_ids, limit):
            self.assertFalse(ctx.conn.in_transaction)
            self.assertEqual(project_ids, [pid])
            self.assertEqual(limit, 3)
            return dict(results=[dict(batch_id="existing", outcome="waiting")])
        with patch("kanpilot.release_delivery.sync_existing", side_effect=poll) as sync:
            result = self.call("tick", dict(project_ids=[pid]))
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(result["release_batches"][0]["batch_id"], "existing")

    def test_engine_polls_historical_release_but_respects_shadow_and_legacy(self):
        from kanpilot import release_delivery, runtime_operations
        from kanpilot.engine.loop import Reconciler
        from unittest.mock import Mock
        pid = self.task()["project_id"]
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="done", stage="done")
            runtime_operations.begin_operation(self.ctx.conn, "release-project:" + pid,
                "batch:history", "core", release_delivery.BATCH_TOOL, {}, now=self.ctx.now_iso)
            runtime_operations.complete_operation(self.ctx.conn, "release-project:" + pid,
                "batch:history", status="prepared", result=dict(project_id=pid, state="building"),
                now=self.ctx.now_iso)
        command = Mock(return_value=dict(created=[], skipped=[]))
        rec = Reconciler(self.db_path, mode="live", run_command=command)
        result = rec.business_tick_step(self.ctx.conn, {pid: "live"})
        self.assertTrue(result["executed"])
        command.assert_called_once_with("tick", dict(project_ids=[pid]))
        for mode in ("shadow", "legacy"):
            command.reset_mock()
            result = rec.business_tick_step(self.ctx.conn, {pid: mode})
            self.assertEqual(result["reason"], "no_live_projects")
            command.assert_not_called()


@pytest.fixture
def merged_case():
    case = _DeliveryPR()
    case.setUp()
    try:
        case.update(pr_number=7, pushed_sha=case.delivery["head_sha"])
        yield case
    finally:
        case.doCleanups()


def test_merge_waits_for_release_after_persisting_merge_outside_transaction(merged_case):
    case = merged_case
    def released(ctx, task, payload):
        assert not ctx.conn.in_transaction
        assert core.delivery_of(task)["pr_state"] == "MERGED"
        assert core.delivery_of(task)["merged_at"]
        assert payload["request_id"] == case.payload["request_id"]
        return dict(outcome="waiting", release=dict(state="building", reason="正在构建"))
    with patch("kanpilot.gh_client.pr_view", return_value=case.pr("MERGED")), \
            patch("kanpilot.release_delivery.after_merge", side_effect=released) as after:
        result = case.sync()
    assert result["outcome"] == "waiting"
    assert result["release"]["state"] == "building"
    assert case.task()["status"] == "in_review"
    assert after.call_count == 1


def test_remote_done_still_verifies_merge_and_release_when_completion_is_gated(merged_case):
    case = merged_case
    with patch.object(remote, "get_task", return_value={"task": {"status": "done"}}), \
            patch("kanpilot.gh_client.pr_view", return_value=case.pr("MERGED")) as pr, \
            patch("kanpilot.release_delivery.completion_gate", return_value=PENDING), \
            patch("kanpilot.release_delivery.after_merge", return_value=dict(
                outcome="blocked", release=dict(state="failed", reason="构建失败"))):
        result = case.sync()
    assert pr.call_count == 1
    assert result["outcome"] == "waiting"
    assert result["release"]["state"] == "failed"
    assert case.task()["status"] == "in_review"


def test_runner_does_not_advance_while_release_is_building():
    from kanpilot import delivery_runner
    from unittest.mock import Mock
    controller = Mock()
    controller.claim = dict(context=dict(task_id="isolated"))
    controller.identity.return_value = dict(request_id="request", run_id="delivery")
    def call(command, payload):
        if command == "get-task":
            return dict(task=dict(run_id="execution", delivery=dict(
                pr_number=1, pushed_sha="head", head_sha="head")))
        if command == "delivery-sync":
            return dict(outcome="waiting", release=dict(state="building", reason="安装包构建中"))
        raise AssertionError("unexpected command: " + command)
    controller.call.side_effect = call
    delivery_runner._delivery(controller)
    controller.report.assert_called_once_with("succeeded", progress="安装包构建中")

