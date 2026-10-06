"""Queued partial copies must remain authentic before every planner backend."""
import json
import os
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
import test_planner_inputs_automation as fixtures
from kanpilot import (claude_planner, db, dsh_events, pi_desktop_planner,
                       planner_config, planner_runner, util)


class TestPartialPlannerPreflight(KanpilotTestCase):
    prepare = fixtures.TestUserReplanMaterial.prepare
    dirty_replan = fixtures.TestUserReplanMaterial.dirty_replan

    def queued_partial(self, backend):
        task_id, item, worktree, _sha = self.dirty_replan()
        prepared = self.call("prepare-plan-input", {"task_id": task_id})
        block = {"connection_file": str(self.root / "fake-connection.json"),
                 "provider_id": "test", "model_id": "test", "thinking_level": "medium",
                 "permission_mode": "ask"}
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                planner_config.save(conn, {"backend": backend, "pi_desktop": block,
                    "claude": {"model": "test-sonnet", "effort": "high"}}, util.now_iso())
            manifest = db.planner_input_manifest(db.get_planner_input(conn, prepared["input_id"]))
        finally:
            conn.close()
        item.update(input_id=prepared["input_id"], input_sha256=prepared["input_sha256"],
                    planner_backend=backend)
        # Only resolves a frozen fake config for submission; no runtime/model call.
        with mock.patch.object(planner_config, "resolve_runtime_config", return_value=(block, None)):
            result = self.call("start-plans", {"items": [item]})["results"][0]
        self.assertIn("request_id", result)
        entry = next(f for f in manifest["files"] if f.get("repository_path") == "partial.py")
        return task_id, result["request_id"], Path(prepared["input_dir"]), entry, worktree

    def assert_rejected(self, backend, tamper):
        task_id, request_id, input_dir, entry, worktree = self.queued_partial(backend)
        copy = input_dir / entry["path"]
        if tamper == "bytes":
            copy.write_text("queued file was replaced\n")
        elif tamper == "symlink":
            original = copy.read_bytes()
            outside = self.root / "outside-copy.txt"
            outside.write_bytes(original)
            copy.unlink()
            copy.symlink_to(outside)
        elif tamper == "directory_symlink":
            outside = self.root / "outside-files"
            copy.parent.rename(outside)
            copy.parent.symlink_to(outside, target_is_directory=True)
        elif tamper == "escape":
            outside = input_dir.parent / "escaped-copy.txt"
            outside.write_bytes(copy.read_bytes())
            conn = db.connect(self.db_path)
            try:
                payload = db.request_payload(db.get_request(conn, request_id))
                partial = next(f for f in payload["manifest"]["files"] if f.get("repository_path") == "partial.py")
                partial["path"] = "../escaped-copy.txt"
                db.update_request(conn, request_id, util.now_iso(), payload_json=payload)
                conn.commit()
            finally:
                conn.close()
        with mock.patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}), \
                mock.patch.object(planner_runner.subprocess, "Popen") as process, \
                mock.patch.object(planner_runner, "_run_fake") as fake, \
                mock.patch.object(claude_planner, "run_one") as claude, \
                mock.patch.object(pi_desktop_planner, "run_one") as plus:
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        for call in (process, fake, claude, plus):
            call.assert_not_called()
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, request_id)
            self.assertEqual(request["status"], "failed")
            run_id = request["run_id"]
            round_row = conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
                                     (task_id, run_id)).fetchone()
            self.assertEqual(round_row["run_status"], "failed")
            task = db.require_task(conn, task_id)
            self.assertFalse(task["plan_path"])
            self.assertIn("重新准备材料", task["failure_reason"])
        finally:
            conn.close()
        run_dir = self.runs_root / task_id / run_id
        job = json.loads((run_dir / dsh_events.JOB_NAME).read_text())
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["backend"], backend)
        events, _ = dsh_events.iter_events(run_dir / dsh_events.EVENT_LOG_NAME)
        self.assertEqual(dsh_events.terminal_event(events)["reason"], "plan_input_mismatch")
        self.assertEqual((worktree / "partial.py").read_text(), "partial_result = 42\n")

    def test_codex_changed_bytes(self):
        self.assert_rejected("codex", "bytes")

    def test_claude_changed_bytes(self):
        self.assert_rejected("claude", "bytes")

    def test_plus_changed_bytes(self):
        self.assert_rejected("pi_desktop", "bytes")

    def test_codex_leaf_symlink(self):
        self.assert_rejected("codex", "symlink")

    def test_claude_leaf_symlink(self):
        self.assert_rejected("claude", "symlink")

    def test_plus_leaf_symlink(self):
        self.assert_rejected("pi_desktop", "symlink")

    def test_codex_parent_symlink(self):
        self.assert_rejected("codex", "directory_symlink")

    def test_claude_parent_symlink(self):
        self.assert_rejected("claude", "directory_symlink")

    def test_plus_parent_symlink(self):
        self.assert_rejected("pi_desktop", "directory_symlink")

    def test_codex_escaping_path(self):
        self.assert_rejected("codex", "escape")

    def test_claude_escaping_path(self):
        self.assert_rejected("claude", "escape")

    def test_plus_escaping_path(self):
        self.assert_rejected("pi_desktop", "escape")

    def test_valid_partial_still_publishes_codex_fake(self):
        task_id, request_id, _input_dir, _entry, _worktree = self.queued_partial("codex")
        with mock.patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}):
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        self.assertEqual(self.call("get-task", {"task_id": task_id})["task"]["status"], "ready")
