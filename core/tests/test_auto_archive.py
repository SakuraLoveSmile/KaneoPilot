"""Completed-task retention: isolated SQLite, synthetic clocks and fake Kaneo."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import auto_archive, core, db, release_delivery, util


PROJECT = "fixture-project-feedback"
BASE = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)


class AutoArchiveTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        # The retention integration exercises only tick's local work. Release
        # polling belongs to its own suite and must never reach GitHub here.
        sync = patch.object(release_delivery, "sync_existing", return_value={"results": []})
        sync.start()
        self.addCleanup(sync.stop)

    @contextmanager
    def connection(self):
        conn = db.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def call_at(self, at, command, payload=None):
        with patch.object(util, "now_dt", return_value=at):
            return self.call(command, payload)

    def add_done(self, task_id, completed_at=None, *, project=PROJECT):
        remote = self.fake.add_task(task_id, project, status="done")
        self.make_repo("Feedback")
        self.call_at(BASE, "sync-tasks", {"executor": "test", "projects": [{
            "project_id": project, "project_name": "Feedback",
            "repository": str(self.repos_root / "Feedback"),
            "tasks": [{"task_id": task_id, "title": task_id, "kaneo_status": "done"}],
        }]})
        if completed_at is not None:
            with self.connection() as conn:
                db.update_task(conn, task_id, util.to_iso(completed_at), status="running")
                core.transition_task(conn, db.require_task(conn, task_id), "done",
                                     util.to_iso(completed_at), "fixture")
        return remote

    def configure(self, enabled=True, after_days=7):
        return self.call_at(BASE, "set-auto-archive-config", {"enabled": enabled, "after_days": after_days})

    def tick(self, at=BASE, project_ids=None):
        payload = {} if project_ids is None else {"project_ids": project_ids}
        return self.call_at(at, "tick", payload)

    def row(self, task_id):
        with self.connection() as conn:
            return dict(db.require_task(conn, task_id))

    def extra(self, task_id):
        return db.task_extra(self.row(task_id))

    def edit(self, task_id, **fields):
        with self.connection() as conn:
            db.update_task(conn, task_id, util.to_iso(BASE), **fields)

    def setting(self, key, value):
        with self.connection() as conn:
            db.set_setting(conn, key, value)

    def test_default_get_is_read_only_and_disabled(self):
        self.add_done("default", BASE - timedelta(days=90))
        with self.connection() as conn:
            before = conn.total_changes
            ctx = type("Context", (), {"conn": conn})()
            self.assertEqual(auto_archive.cmd_get_auto_archive_config(ctx, {}),
                             {"config": {"enabled": False, "after_days": 7}})
            self.assertEqual(conn.total_changes, before)
            self.assertIsNone(db.get_setting(conn, auto_archive.SETTING_KEY))
        self.assertEqual(self.tick()["auto_archived"], [])
        self.assertEqual(self.row("default")["save_state"], "active")
        self.assertNotIn("auto_archive_retention", self.extra("default"))

    def test_config_validation_persistence_and_no_immediate_archive(self):
        self.add_done("overdue", BASE - timedelta(days=30))
        for payload in [{}, {"enabled": True}, {"enabled": 1, "after_days": 7},
                        {"enabled": True, "after_days": True}, {"enabled": True, "after_days": 1.5},
                        {"enabled": True, "after_days": "7"}, {"enabled": True, "after_days": 0},
                        {"enabled": True, "after_days": 3651}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.call_err("set-auto-archive-config", payload).code, "bad_request")
        self.assertEqual(self.configure(after_days=1), {"config": {"enabled": True, "after_days": 1}})
        self.assertEqual(self.call("get-auto-archive-config"), {"config": {"enabled": True, "after_days": 1}})
        self.assertEqual(self.row("overdue")["save_state"], "active")
        self.assertEqual(self.tick()["auto_archived"], ["overdue"])

    def test_exact_deadline_and_repeated_tick_are_idempotent(self):
        self.add_done("boundary", BASE - timedelta(days=7))
        self.configure()
        self.assertEqual(self.tick(BASE - timedelta(microseconds=1))["auto_archived"], [])
        self.assertEqual(self.tick()["auto_archived"], ["boundary"])
        self.assertEqual(self.row("boundary")["saved_at"], util.to_iso(BASE))
        self.assertEqual(self.tick(BASE + timedelta(days=1))["auto_archived"], [])
        with self.connection() as conn:
            events = conn.execute("SELECT * FROM events WHERE task_id=? AND kind='task_archived'", ("boundary",)).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "scheduler")
        self.assertEqual(util.jload(events[0]["detail_json"])["source"], "auto_archive")
        self.assertEqual(self.call("list-tasks")["tasks"], [])
        self.assertEqual(self.call("list-tasks", {"save_state": "archived"})["tasks"][0]["task_id"], "boundary")

    def test_sync_metadata_does_not_restart_completion_clock(self):
        remote = self.add_done("metadata", BASE - timedelta(days=8))
        self.configure(after_days=10)
        self.tick()
        with patch.object(util, "now_dt", return_value=BASE + timedelta(days=1)):
            self.sync_task(remote, project_name="Feedback")
        self.assertEqual(self.row("metadata")["updated_at"], util.to_iso(BASE + timedelta(days=1)))
        self.assertEqual(self.tick(BASE + timedelta(days=2))["auto_archived"], ["metadata"])

    def test_historical_unknown_completion_gets_full_persisted_retention(self):
        self.add_done("historical")
        self.edit("historical", created_at=util.to_iso(BASE - timedelta(days=300)),
                  updated_at=util.to_iso(BASE - timedelta(days=300)))
        self.configure()
        self.assertEqual(self.tick()["auto_archived"], [])
        self.assertEqual(self.extra("historical")["auto_archive_retention"]["started_at"], util.to_iso(BASE))
        self.assertEqual(self.tick(BASE + timedelta(days=7, microseconds=-1))["auto_archived"], [])
        self.assertEqual(self.tick(BASE + timedelta(days=7))["auto_archived"], ["historical"])

    def test_new_completion_restarts_clock_even_without_intermediate_tick(self):
        self.add_done("again")
        self.configure()
        self.tick()
        with self.connection() as conn:
            for status, days in [("running", 1), ("done", 2)]:
                core.transition_task(conn, db.require_task(conn, "again"), status,
                                     util.to_iso(BASE + timedelta(days=days)), "fixture")
        self.assertEqual(self.tick(BASE + timedelta(days=7))["auto_archived"], [])
        self.assertEqual(self.tick(BASE + timedelta(days=9))["auto_archived"], ["again"])

    def test_same_status_done_event_does_not_restart_clock(self):
        self.add_done("same", BASE - timedelta(days=8))
        with self.connection() as conn:
            core.transition_task(conn, db.require_task(conn, "same"), "done", util.to_iso(BASE), "fixture")
        self.configure()
        self.assertEqual(self.tick()["auto_archived"], ["same"])

    def test_restore_restarts_retention_but_active_restore_does_not(self):
        for state, command in [("archived", "archive-task"), ("trashed", "trash-task")]:
            self.add_done(state, BASE - timedelta(days=30))
            self.call_at(BASE, command, {"task_id": state})
            self.call_at(BASE + timedelta(days=1), "restore-task", {"task_id": state})
        self.configure()
        self.assertEqual(self.tick(BASE + timedelta(days=7))["auto_archived"], [])
        # Explicit resume emits an active→active task_restored event; neither
        # it nor an ordinary idempotent restore grants another retention window.
        self.call_at(BASE + timedelta(days=7), "restore-task", {"task_id": "archived", "resume_auto": True})
        self.call_at(BASE + timedelta(days=7), "restore-task", {"task_id": "trashed"})
        self.assertEqual(set(self.tick(BASE + timedelta(days=8))["auto_archived"]), {"archived", "trashed"})

    def test_only_active_completed_tasks_are_candidates(self):
        for status in ["needs_plan", "ready", "planning", "running", "in_review", "needs_user"]:
            self.add_done(status, BASE - timedelta(days=30))
            self.edit(status, status=status)
        for state in ["archived", "trashed", "purged"]:
            task_id = "saved-" + state
            self.add_done(task_id, BASE - timedelta(days=30))
            self.edit(task_id, save_state=state)
        self.add_done("paused-task", BASE - timedelta(days=30))
        self.edit("paused-task", auto_advance_paused=1)
        self.configure()
        self.assertEqual(self.tick()["auto_archived"], ["paused-task"])

    def test_claimed_unknown_and_verification_busy_do_not_block_other_tasks(self):
        for task_id in ["claimed", "lost", "unknown", "verify", "z-ready"]:
            self.add_done(task_id, BASE - timedelta(days=30))
        self.edit("unknown", send_state="unknown")
        with self.connection() as conn:
            for task_id, lease in [("claimed", BASE + timedelta(hours=1)), ("lost", BASE - timedelta(hours=1))]:
                request, _ = db.create_request(conn, util.to_iso(BASE), "monitor", "fixture:" + task_id, task_id=task_id)
                db.update_request(conn, request["request_id"], util.to_iso(BASE), status="claimed",
                                  lease_expires_at=util.to_iso(lease))
            db.set_extra(conn, db.require_task(conn, "verify"), util.to_iso(BASE),
                         verification_checks={"state": "running", "operation_id": "fixture-verification", "owner_pid": os.getpid()})
        self.configure()
        result = self.tick()
        self.assertEqual(result["auto_archived"], ["z-ready"])
        for task_id in ["claimed", "lost", "unknown", "verify"]:
            self.assertEqual(self.row(task_id)["save_state"], "active", task_id)
        self.assertNotIn("auto_archive_retention", self.extra("verify"))

    def test_due_archive_cancels_queued_requests(self):
        self.add_done("queued", BASE - timedelta(days=30))
        with self.connection() as conn:
            request, _ = db.create_request(conn, util.to_iso(BASE), "monitor", "fixture:queued", task_id="queued")
            request_id = request["request_id"]
        self.configure()
        self.assertEqual(self.tick()["auto_archived"], ["queued"])
        with self.connection() as conn:
            self.assertEqual(db.get_request(conn, request_id)["status"], "cancelled")
            event = conn.execute(
                "SELECT actor FROM events WHERE request_id=? AND kind='request_cancelled'",
                (request_id,),
            ).fetchone()
            self.assertEqual(event["actor"], "scheduler")

    def attach_release(self, task_id, state):
        with self.connection() as conn:
            task = db.require_task(conn, task_id)
            batch_id = "fixture-release-" + task_id
            batch = {"batch_id": batch_id, "project_id": PROJECT, "members": [release_delivery._identity(task)],
                     "state": state, "policy": {"channel": "prerelease"}, "root_task_id": task_id,
                     "target_sha": "a" * 40, "context_sha256": "b" * 64, "version": "0.1.0-test.1",
                     "tag": "v0.1.0-test.1", "build_number": 1, "dispatch_status": "sent_or_unknown",
                     "next_check_at": util.to_iso(BASE + timedelta(days=40))}
            db.insert_runtime_operation(conn, release_delivery._scope(PROJECT), "batch:" + batch_id,
                                        "fixture", release_delivery.BATCH_TOOL, "c" * 64,
                                        "accepted" if state == "published" else "prepared", util.to_iso(BASE), result_json=util.jdump(batch))
            db.set_extra(conn, task, util.to_iso(BASE), release_ref={"batch_id": batch_id, "required": True})

    def test_current_release_and_delivery_are_kept_but_published_history_can_archive(self):
        for state in ["building", "unknown", "published"]:
            self.add_done(state, BASE - timedelta(days=30))
            self.attach_release(state, state)
        self.add_done("open-pr", BASE - timedelta(days=30))
        self.edit("open-pr", extra_json={"delivery": {"mode": "pr", "pr_number": 3, "pr_state": "OPEN"}})
        self.add_done("delivery-queued", BASE - timedelta(days=30))
        with self.connection() as conn:
            db.create_request(conn, util.to_iso(BASE), "delivery", "fixture:delivery", task_id="delivery-queued")
        self.configure()
        self.assertEqual(self.tick()["auto_archived"], ["published"])
        for task_id in ["building", "unknown", "open-pr", "delivery-queued"]:
            self.assertEqual(self.row(task_id)["save_state"], "active", task_id)

    def test_global_pause_disable_and_restart(self):
        self.add_done("paused", BASE - timedelta(days=30))
        self.configure()
        self.setting("paused_global", True)
        self.assertEqual(self.tick()["auto_archived"], [])
        self.setting("paused_global", False)
        self.configure(enabled=False)
        self.assertEqual(self.tick()["auto_archived"], [])
        self.configure()
        self.assertEqual(self.tick(BASE + timedelta(days=4))["auto_archived"], ["paused"])
        self.configure(enabled=False)
        self.assertEqual(self.row("paused")["save_state"], "archived")

    def test_all_engine_modes_and_project_scoping(self):
        for mode in ["legacy", "shadow", "live"]:
            project = "fixture-project-" + mode
            self.add_done(mode, BASE - timedelta(days=30), project=project)
        with self.connection() as conn:
            mappings = db.get_setting(conn, "project_mappings")
            for mode in ["legacy", "shadow", "live"]:
                mappings["items"]["fixture-project-" + mode] = {"engine_mode": mode}
            db.set_setting(conn, "project_mappings", mappings)
        self.configure()
        self.assertEqual(self.tick(project_ids=["fixture-project-live"])["auto_archived"], ["live"])
        self.assertEqual(set(self.tick()["auto_archived"]), {"legacy", "shadow"})
