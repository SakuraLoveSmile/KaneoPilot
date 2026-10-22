"""migrate: backup + manifest import + idempotent re-run."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402


def _manifest(tasks):
    return {"version": 1, "tasks": tasks}


class TestMigrate(KanpilotTestCase):
    def _legacy(self):
        """Write fake legacy manifests into the temp legacy root."""
        exe = {
            "readytask000000000001": {
                "task_id": "readytask000000000001",
                "project_id": "fixture-project-feedback",
                "project_name": "Feedback",
                "status": "ready",
                "plan_path": "/plans/f.md", "plan_sha256": "aa",
                "requirements_sha256": "req", "source_sha256": "src",
                "send_state": "not_sent", "sent": False,
                "repository": str(self.repos_root / "Feedback"),
            },
            "runtask00000000000001": {
                "task_id": "runtask00000000000001",
                "project_id": "fixture-project-feedback",
                "project_name": "Feedback",
                "status": "running",
                "send_state": "sent", "sent": True,
                "session_marker": "runtask00000000000001/run-1",
                "repository": str(self.repos_root / "Feedback"),
            },
            "disptask0000000000001": {
                "task_id": "disptask0000000000001",
                "project_id": "fixture-project-feedback",
                "project_name": "Feedback",
                "status": "submitting",
                "run_id": "run-x", "send_state": "unknown", "sent": None,
                "lock_retained": True,
                "repository": str(self.repos_root / "Feedback"),
            },
            "blocktask000000000001": {
                "task_id": "blocktask000000000001",
                "project_id": "fixture-project-feedback",
                "project_name": "Feedback",
                "status": "needs_user",
                "send_state": "unknown", "failure_code": "computer_locked",
                "failure_reason": "screen locked", "lock_retained": True,
                "repository": str(self.repos_root / "Feedback"),
                "attempt_history": [
                    {"run_id": "old-run", "status": "needs_user",
                     "failure_reason": "earlier failure",
                     "created_at": "2026-09-19T00:00:00+00:00",
                     "failed_at": "2026-09-19T01:00:00+00:00"},
                ],
            },
        }
        pln = {
            "planonly0000000000001": {
                "task_id": "planonly0000000000001",
                "project_id": "fixture-project-library",
                "project_name": "Library",
                "plan_path": "/plans/lib.md", "plan_sha256": "bb",
                "source_sha256": "ss", "source_snapshot": "/snap/1.json",
                "source_status": "to-do", "implementation_status": "未开始",
                "repository": str(self.repos_root / "Library"),
                "generated_at": "2026-09-20T00:00:00+00:00",
                "planner_model": "gpt-6-astra", "reasoning_effort": "medium",
            },
        }
        rev = {
            "revtask00000000000001": {
                "task_id": "revtask00000000000001",
                "project_id": "fixture-project-studytool",
                "project_name": "StudyTool",
                "status": "completed", "conclusion": "pass",
                "kaneo_status": "done",
                "report_path": "/reports/r.md", "report_sha256": "cc",
                "repository": str(self.repos_root / "StudyTool"),
                "reviewed_at": "2026-09-19T15:16:35Z",
            },
        }
        for name, data in (
            ("KaneoExecutor", exe), ("KaneoPlanner", pln),
            ("KaneoReviewer", rev),
        ):
            directory = self.legacy_root / name / "state"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "manifest.json").write_text(json.dumps(_manifest(data)))
        # a legacy lock with live owner
        lock = self.legacy_root / "KaneoExecutor" / "locks" / "runtask00000000000001.lock"
        lock.mkdir(parents=True, exist_ok=True)
        (lock / "owner.json").write_text(json.dumps(
            {"owner_run_id": "run-1", "task_id": "runtask00000000000001"}
        ))

    def test_migrate_imports_and_idempotent(self):
        self._legacy()
        result = self.call("migrate", {})
        self.assertTrue(result["ok"])
        self.assertTrue(Path(result["backup_path"]).is_dir())
        # backup holds copies, originals untouched
        self.assertTrue(
            (Path(result["backup_path"]) / "KaneoExecutor" / "state"
             / "manifest.json").is_file()
        )
        self.assertEqual(result["imported"]["tasks"], 6)

        statuses = {
            t["task_id"]: t["status"] for t in self.call("list-tasks")["tasks"]
        }
        self.assertEqual(statuses["readytask000000000001"], "ready")
        self.assertEqual(statuses["runtask00000000000001"], "running")
        self.assertEqual(statuses["disptask0000000000001"], "needs_user")
        self.assertEqual(statuses["blocktask000000000001"], "needs_user")
        self.assertEqual(statuses["planonly0000000000001"], "ready")
        self.assertEqual(statuses["revtask00000000000001"], "done")

        # running -> monitor request; needs_user keeps failure fields
        reqs = [r for r in self.requests(status="queued")
                if r["kind"] == "monitor"
                and r["task_id"] == "runtask00000000000001"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["requested_by"], "migration")
        view = self.task("disptask0000000000001")
        self.assertEqual(view["send_state"], "unknown")
        view = self.task("blocktask000000000001")
        self.assertEqual(view["send_state"], "unknown")
        # attempt history imported
        self.assertTrue(view["attempts"])
        # lock recorded but not revived
        run_view = self.task("runtask00000000000001")
        self.assertEqual(run_view["status"], "running")

        # second run: manifests unchanged -> nothing new imported
        before = len(self.call("list-events")["events"])
        again = self.call("migrate", {})
        self.assertTrue(again["ok"])
        tasks_after = self.call("list-tasks")["tasks"]
        self.assertEqual(len(tasks_after), 6)
        self.assertEqual(again["imported"]["tasks"], 0)
        # migration setting persisted
        conn = sqlite3.connect(str(self.db_path))
        setting = conn.execute(
            "SELECT value FROM settings WHERE key='migration'"
        ).fetchone()[0]
        self.assertIn("manifests_sha256", setting)
        conn.close()

    def test_migrate_dry_run_writes_nothing(self):
        self._legacy()
        result = self.call("migrate", {"dry_run": True})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["backup_path"])
        self.assertEqual(self.call("list-tasks")["tasks"], [])


if __name__ == "__main__":
    unittest.main()
