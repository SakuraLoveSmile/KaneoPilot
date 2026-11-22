import json
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import bridge, cli, db, followup_task, util


class FollowupTask(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.make_ready_task(task_id="task1234")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.calls = []

    def fake_create(self):
        def create(client, project_id, dedupe_key, payload, *,
                   marker_prefix="codex-kaneo-review"):
            self.calls.append({"project_id": project_id, "dedupe_key": dedupe_key,
                               "payload": payload, "marker_prefix": marker_prefix})
            return {"task": {"id": "follow01", "projectId": project_id},
                    "comments": [], "relations": [], "activity": [],
                    "created": True, "todo_slug": "slug-follow01",
                    "requirements_sha256": "a" * 64, "source_sha256": "b" * 64}
        return create

    def payload(self, **overrides):
        value = {"source_task_id": "task1234", "dedupe_key": "offline-index-race",
                 "title": "修复离线索引竞态",
                 "description": "analyze-test 在 push 事件稳定失败",
                 "confirm": True}
        value.update(overrides)
        return value

    def test_creates_linked_task_with_marker_and_receipt(self):
        with patch.object(followup_task.remote, "get_client", lambda: object()), \
                patch.object(followup_task.remote, "create_todo", self.fake_create()):
            result = followup_task.cmd_create_followup_task(self.ctx, self.payload())

        self.assertTrue(result["ok"])
        self.assertEqual(result["followup_task_id"], "follow01")
        self.assertTrue(result["created"])
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["marker_prefix"], "kaneopilot-followup")
        self.assertEqual(call["project_id"], "fixture-project-feedback")
        self.assertEqual(call["dedupe_key"], "offline-index-race")
        self.assertEqual(call["payload"], {
            "title": "修复离线索引竞态",
            "description": "analyze-test 在 push 事件稳定失败",
            "priority": "high"})

        row = db.get_task(self.ctx.conn, "follow01")
        self.assertEqual(row["status"], "needs_plan")
        self.assertEqual(row["stage"], "plan")
        self.assertEqual(row["plan_kind"], "standard")
        self.assertEqual(row["review_source_task_id"], "task1234")
        self.assertEqual(row["title"], "修复离线索引竞态")
        self.assertEqual(db.task_extra(row).get("followup_key"), "offline-index-race")
        snapshot = json.loads(open(row["source_path"], encoding="utf-8").read())
        self.assertEqual(snapshot["task"]["id"], "follow01")

        operation = db.get_runtime_operation(
            self.ctx.conn, "followup-task:task1234",
            followup_task._operation_id("task1234", "offline-index-race"))
        self.assertEqual(operation["status"], "accepted")
        self.assertEqual(operation["harness_id"], "kaneo")
        self.assertEqual(operation["tool"], "kaneo_create_followup")

        event = self.ctx.conn.execute(
            "SELECT * FROM events WHERE task_id=? AND kind='task_created'",
            ("follow01",)).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event["detail_json"]),
                         {"via": "create-followup-task", "source_task_id": "task1234"})

    def test_same_dedupe_key_reuses_without_remote_call(self):
        with patch.object(followup_task.remote, "get_client", lambda: object()), \
                patch.object(followup_task.remote, "create_todo", self.fake_create()):
            first = followup_task.cmd_create_followup_task(self.ctx, self.payload())
            second = followup_task.cmd_create_followup_task(self.ctx, self.payload())

        self.assertEqual(first["followup_task_id"], second["followup_task_id"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.calls), 1)
        count = self.ctx.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE review_source_task_id='task1234'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_remote_failure_marks_unknown_and_creates_nothing(self):
        def broken(*args, **kwargs):
            raise bridge.BridgeError("kaneo down")

        with patch.object(followup_task.remote, "get_client", lambda: object()), \
                patch.object(followup_task.remote, "create_todo", broken):
            with self.assertRaises(util.KanpilotError) as caught:
                followup_task.cmd_create_followup_task(self.ctx, self.payload())

        self.assertEqual(caught.exception.code, "remote_unavailable")
        operation = db.get_runtime_operation(
            self.ctx.conn, "followup-task:task1234",
            followup_task._operation_id("task1234", "offline-index-race"))
        self.assertEqual(operation["status"], "unknown")
        self.assertIsNone(db.get_task(self.ctx.conn, "follow01"))
        self.assertEqual(self.ctx.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE plan_kind='standard' AND review_source_task_id='task1234'"
        ).fetchone()[0], 0)

    def test_requires_confirmation_and_safe_dedupe_key(self):
        with patch.object(followup_task.remote, "get_client", lambda: object()), \
                patch.object(followup_task.remote, "create_todo", self.fake_create()):
            for payload in (self.payload(confirm=False),
                            self.payload(dedupe_key="bad-->marker"),
                            self.payload(title="")):
                with self.assertRaises(util.KanpilotError) as caught:
                    followup_task.cmd_create_followup_task(self.ctx, payload)
                self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(self.calls, [])

    def test_removed_task_is_not_silently_reused(self):
        with patch.object(followup_task.remote, "get_client", lambda: object()), \
                patch.object(followup_task.remote, "create_todo", self.fake_create()):
            followup_task.cmd_create_followup_task(self.ctx, self.payload())
            db.update_task(self.ctx.conn, "follow01", self.ctx.now_iso, save_state="purged")
            with self.assertRaises(util.KanpilotError) as caught:
                followup_task.cmd_create_followup_task(self.ctx, self.payload())
            self.assertEqual(caught.exception.code, "conflict")
            self.assertEqual(len(self.calls), 1)
            self.ctx.conn.execute("DELETE FROM tasks WHERE task_id='follow01'")
            with self.assertRaises(util.KanpilotError) as caught:
                followup_task.cmd_create_followup_task(self.ctx, self.payload())
            self.assertEqual(caught.exception.code, "conflict")
            self.assertIn("已不存在或已永久移除", caught.exception.message)
            self.assertEqual(len(self.calls), 1)
