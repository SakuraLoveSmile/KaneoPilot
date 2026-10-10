"""Integration tests for abandon-task command."""
from __future__ import annotations

import json
import sqlite3
import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import util  # noqa: E402


class TestAbandonTask(KanpilotTestCase):
    def test_abandon_active_task_sets_archived_and_resolution(self):
        task_id = self.make_ready_task()["remote"]["id"]
        result = self.call("abandon-task", {"task_id": task_id})
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("deduplicated", False))
        task = result["task"]
        self.assertEqual(task["save_state"], "archived")
        self.assertEqual(task["status"], "ready")
        extra = json.loads(
            sqlite3.connect(str(self.db_path))
            .execute("SELECT extra_json FROM tasks WHERE task_id=?", (task_id,))
            .fetchone()[0]
        )
        self.assertEqual(extra.get("resolution", {}).get("action"), "abandoned")

    def test_abandon_with_save_state_trashed(self):
        task_id = self.make_ready_task()["remote"]["id"]
        result = self.call(
            "abandon-task", {"task_id": task_id, "save_state": "trashed"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["save_state"], "trashed")

    def test_abandon_again_is_deduplicated(self):
        task_id = self.make_ready_task()["remote"]["id"]
        self.call("abandon-task", {"task_id": task_id})
        result = self.call("abandon-task", {"task_id": task_id, "note": "again"})
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("deduplicated", False))
        self.assertEqual(result["task"]["save_state"], "archived")

    def test_abandon_purged_task_conflicts(self):
        task_id = self.make_ready_task()["remote"]["id"]
        self.call("archive-task", {"task_id": task_id})
        self.call("trash-task", {"task_id": task_id})
        self.call("purge-task", {"task_id": task_id})
        exc = self.call_err("abandon-task", {"task_id": task_id})
        self.assertEqual(exc.code, "conflict")

    def test_abandon_purged_task_with_allow_purged_records_resolution(self):
        task_id = self.make_ready_task()["remote"]["id"]
        self.call("archive-task", {"task_id": task_id})
        self.call("trash-task", {"task_id": task_id})
        self.call("purge-task", {"task_id": task_id})
        result = self.call(
            "abandon-task",
            {"task_id": task_id, "allow_purged": True, "note": "legacy leftover"},
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("deduplicated", False))
        self.assertEqual(result["task"]["save_state"], "purged")
        extra = json.loads(
            sqlite3.connect(str(self.db_path))
            .execute("SELECT extra_json FROM tasks WHERE task_id=?", (task_id,))
            .fetchone()[0]
        )
        resolution = extra.get("resolution", {})
        self.assertEqual(resolution.get("action"), "abandoned")
        self.assertTrue(resolution.get("purged"))

    def test_abandon_purged_task_without_flag_still_conflicts(self):
        task_id = self.make_ready_task()["remote"]["id"]
        self.call("archive-task", {"task_id": task_id})
        self.call("trash-task", {"task_id": task_id})
        self.call("purge-task", {"task_id": task_id})
        exc = self.call_err("abandon-task", {"task_id": task_id})
        self.assertEqual(exc.code, "conflict")
