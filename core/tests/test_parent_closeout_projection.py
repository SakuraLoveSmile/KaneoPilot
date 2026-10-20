"""The core closeout result supersedes old repair advice, without rewriting Review."""
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, db, derive, review_repair, util


class ParentCloseoutProjectionTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.now = util.now_dt()
        extra = dict(review_conclusion="changes_requested", delivery=dict(
            head_sha="original-head", branch="kaneo/original", worktree_path="/isolated/branch"),
            review_history=[dict(review_key="review-key", conclusion="changes_requested",
                execution_run_id="execution", report_sha256="review-sha", fix_task_id="fix",
                fix=dict(title="Fix", description="Repair original issue", priority="high"))])
        values = dict(task_id="original", project_id="music", project_name="Music",
            title="Original", repository="/isolated/Music", status="in_review", stage="review",
            run_id="execution", review_key="review-key", review_report_path="/isolated/review.md",
            review_report_sha256="review-sha", plan_sha256="plan-sha", fix_task_id="fix",
            created_at=util.to_iso(self.now), updated_at=util.to_iso(self.now),
            extra_json=util.jdump(extra))
        self.conn.execute("INSERT INTO tasks (" + ",".join(values) + ") VALUES (" +
                          ",".join("?" for _ in values) + ")", tuple(values.values()))
        self.task_row = db.require_task(self.conn, "original")
        self.value = dict(state="ready", title="待收尾校验", reason="二级任务已完成，待收尾校验",
            context_sha256="context", can_complete=True, required_children=2,
            completed_children=2, blockers=[], acceptance_note="真机和真实服务体验仍未核验")

    def render(self):
        return derive.task_detail(self.conn, db.require_task(self.conn, "original"), self.now)

    def project(self):
        return patch("kanpilot.parent_closeout.projection", return_value=self.value)

    def test_ready_uses_closeout_action_and_keeps_original_review_as_history(self):
        with self.project():
            view = self.render()
            self.assertEqual(view["parent_closeout"], self.value)
            self.assertEqual(view["display_status"], "待收尾校验")
            self.assertEqual(view["board_column"], "in_review")
            self.assertEqual(view["board_substatus"], "待收尾校验")
            self.assertIsNone(view["exception"])
            self.assertIsNone(view["review_repair"])
            self.assertIn("closeout_parent", view["allowed_actions"])
            self.assertNotIn("enqueue", view["allowed_actions"])
            self.assertNotIn("mark_done", view["allowed_actions"])
            self.assertEqual(view["review_summary"]["conclusion"], "changes_requested")
            self.assertEqual((view["phase"], view["condition"]), ("delivery", "syncing"))

    def test_blocked_and_unknown_are_clear_blocks_in_review_column(self):
        for state, label in (("blocked", "收尾校验受阻"), ("unknown", "完成同步待确认")):
            with self.subTest(state=state):
                self.value.update(state=state, title=label, reason="需要核对已有证据")
                with self.project():
                    view = self.render()
                    self.assertEqual(view["display_status"], label)
                    self.assertEqual(view["board_column"], "in_review")
                    self.assertEqual(view["board_substatus"], label)
                    self.assertEqual(view["exception"]["severity"], "blocked")
                    self.assertEqual(view["exception"]["detail"], self.value["reason"])
                    self.assertIn("closeout_parent", view["allowed_actions"])

    def test_waiting_children_does_not_offer_completion_or_another_repair(self):
        self.value.update(state="waiting_children", can_complete=False, completed_children=1)
        with self.project():
            view = self.render()
            self.assertNotIn("closeout_parent", view["allowed_actions"])
            self.assertIsNone(view["review_repair"])
            reason = review_repair._gate(self.conn, self.task_row,
                                         review_repair.candidate(self.task_row), self.now)
            self.assertIn("二级", reason)

    def test_completed_result_keeps_real_done_and_does_not_reopen_history(self):
        db.update_task(self.conn, "original", util.to_iso(self.now), status="done", stage="done")
        self.value.update(state="completed", title="收尾校验完成", can_complete=False)
        with self.project():
            view = self.render()
            self.assertEqual(view["display_status"], "已完成")
            self.assertEqual(view["board_column"], "done")
            self.assertIsNone(view["review_repair"])
            self.assertNotIn("closeout_parent", view["allowed_actions"])

    def test_projection_reads_do_not_modify_persisted_review_or_create_requests(self):
        before = list(self.conn.iterdump())
        self.conn.execute("PRAGMA query_only=ON")
        with self.project():
            self.render()
            derive.task_summary(self.conn, self.task_row, self.now)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_fixed_command_requires_frozen_context(self):
        self.assertIn("closeout-parent-task", cli.COMMANDS)
        with self.assertRaises(util.KanpilotError):
            cli.validate_payload("closeout-parent-task", dict(task_id="original"))

    def test_generic_kaneo_sync_does_not_bypass_pending_closeout_evidence(self):
        self.value.update(state="unknown", title="完成同步待确认")
        with self.project():
            self.call("sync-tasks", dict(executor="isolated-sync", projects=[dict(
                project_id="music", project_name="Music", tasks=[dict(task_id="original",
                    title="Original", kaneo_status="done", repository="/isolated/Music")])]))
        fresh = db.require_task(self.conn, "original")
        self.assertEqual(fresh["kaneo_status"], "done")
        self.assertEqual(fresh["status"], "in_review")
        self.assertEqual(db.task_extra(fresh)["review_conclusion"], "changes_requested")
