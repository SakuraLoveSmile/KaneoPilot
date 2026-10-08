"""Explicit reconnect: retained history, no automatic resurrection or remote writes."""

from datetime import timedelta
import os
import json
from pathlib import Path
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import constants, core, db, project_config, remote, util


class TestReconnectTask(KanpilotTestCase):
    task_id = "taskreconnect000000001"

    def removed(self, save_state="purged"):
        fixture = self.make_ready_task(task_id=self.task_id)
        # A previous dispatch request is cancelled by moving the task out.
        self.enqueue_dispatch(self.task_id)
        self.call("trash-task", {"task_id": self.task_id})
        conn = db.connect(self.db_path)
        try:
            now = util.now_iso()
            conn.execute(
                "INSERT INTO execution_rounds(task_id,run_id,save_state,created_at,updated_at) "
                "VALUES (?,?,'trashed',?,?)",
                (self.task_id, "old-run", now, now),
            )
            db.update_task(conn, self.task_id, now, run_id="old-run", session_id="old-session",
                           failure_code="execution_failed", failure_reason="old failure",
                           failure_stage="running", blocked_summary="old failure")
            db.set_extra(conn, db.require_task(conn, self.task_id), now,
                         automation={"plan_wanted": True, "cost_usd": 2.5, "replan_requested_at": now},
                         review_history=[{"conclusion": "fail"}])
        finally:
            conn.close()
        if save_state == "purged":
            self.call("purge-task", {"task_id": self.task_id})
        elif save_state == "archived":
            self.call("archive-task", {"task_id": self.task_id})
        return fixture

    def snapshot(self):
        conn = db.connect(self.db_path)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    def raw_task(self):
        conn = db.connect(self.db_path)
        try:
            return dict(db.require_task(conn, self.task_id))
        finally:
            conn.close()

    def reconnect(self, save_state="purged"):
        return self.call("reconnect-task", {"task_id": self.task_id, "expected_save_state": save_state})

    def sent_evidence(self, status="finished", outcome="completed"):
        self.set_task_status(self.task_id, "needs_user", sent=1, send_state="sent")
        directory = self.runs_root / self.task_id / "old-run"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "job.json").write_text(json.dumps({
            "task_id": self.task_id, "run_id": "old-run", "backend": "pi",
            "status": status, "started_at": util.now_iso(),
        }), encoding="utf-8")
        (directory / "events.jsonl").write_text(json.dumps({
            "seq": 1, "type": "terminal", "outcome": outcome, "ts": util.now_iso(),
        }) + "\n", encoding="utf-8")

    def test_sent_old_run_requires_explicit_terminal_evidence(self):
        self.removed()
        self.set_task_status(self.task_id, "needs_user", sent=1, send_state="sent", run_id=None)
        before = self.raw_task()
        self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
        self.assertEqual(self.raw_task(), before)
        self.set_task_status(self.task_id, "needs_user", run_id="old-run")
        for status, outcome in (("running", "completed"), ("finished", "unknown"), ("finished", None)):
            self.sent_evidence(status, outcome)
            before = self.raw_task()
            item = next(x for x in self.call("fetch-todos", {})["items"] if x["task_id"] == self.task_id)
            self.assertFalse(item["reconnectable"])
            self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
            self.assertEqual(self.raw_task(), before)
        self.sent_evidence("finished", "completed")
        before = self.snapshot()
        item = next(x for x in self.call("fetch-todos", {})["items"] if x["task_id"] == self.task_id)
        self.assertTrue(item["reconnectable"])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.reconnect()["ok"])
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT save_state FROM execution_rounds WHERE run_id='old-run'").fetchone()[0], "purged")
        finally:
            conn.close()

    def test_sent_task_and_round_changes_during_verification_block(self):
        self.removed()
        self.sent_evidence()
        read = remote.get_task
        for target in ("task", "round"):
            def raced(client, task_id):
                live = read(client, task_id)
                conn = db.connect(self.db_path)
                try:
                    if target == "task":
                        db.update_task(conn, task_id, util.now_iso(), run_id="replacement-run")
                    else:
                        conn.execute("UPDATE execution_rounds SET run_status='running' WHERE task_id=? AND run_id='old-run'", (task_id,))
                finally:
                    conn.close()
                return live
            with patch.object(remote, "get_task", side_effect=raced):
                self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
            self.assertEqual(self.raw_task()["save_state"], "purged")
            self.set_task_status(self.task_id, "needs_user", run_id="old-run")

    def test_failed_new_plan_cannot_fall_back_to_old_plan_after_dismiss(self):
        self.removed()
        self.reconnect()
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        started = self.call("start-plans", {"items": [{"input_id": prepared["input_id"], "input_sha256": prepared["input_sha256"]}]})
        request_id = started["results"][0]["request_id"]
        claim = self.call("claim", {"stage": "plan", "executor": constants.PLANNER_EXECUTOR_ID, "request_id": request_id})
        self.assertTrue(claim["claimed"])
        self.call("report", {"request_id": request_id, "run_id": claim["run_id"], "outcome": "needs_user", "note": "new plan unavailable"})
        self.call("resolve", {"task_id": self.task_id, "action": "dismiss"})
        self.assertEqual(self.raw_task()["status"], "ready")
        self.assertIsNone(self.raw_task()["plan_path"])
        self.assertIsNone(self.raw_task()["plan_sha256"])
        for extra in ({}, {"confirm_plan": True}):
            error = self.call_err("enqueue", {"task_id": self.task_id, "kind": "dispatch", **extra})
            self.assertEqual(error.code, "conflict")
        self.assertFalse(self.call("claim", {"stage": "dispatch", "executor": "test-dispatch"})["claimed"])

    def test_fetch_does_not_resurrect_removed_tasks(self):
        for state in ("purged", "trashed", "archived"):
            with self.subTest(state=state):
                if state == "purged":
                    self.removed(state)
                else:
                    self.set_task_status(self.task_id, "needs_user", save_state=state)
                before = self.snapshot()
                item = next(x for x in self.call("fetch-todos", {})["items"] if x["task_id"] == self.task_id)
                self.assertFalse(item["selectable"])
                self.assertTrue(item["reconnectable"])
                self.assertNotIn("已已", item["blocked_reason"])
                self.assertEqual(before, self.snapshot())

    def test_reconnect_preserves_plan_source_rounds_and_history(self):
        fixture = self.removed()
        old_plan = fixture["plan_path"].read_bytes()
        old_source = Path(fixture["source_path"]).read_bytes()
        before = self.raw_task()
        conn = db.connect(self.db_path)
        try:
            old_requests = [dict(r) for r in conn.execute("SELECT * FROM requests WHERE task_id=?", (self.task_id,))]
        finally:
            conn.close()
        self.fake.requests_log.clear()
        result = self.reconnect()
        self.assertTrue(result["ok"])
        task = self.raw_task()
        self.assertEqual((task["save_state"], task["status"], task["stage"]), ("active", "needs_plan", "plan"))
        self.assertEqual(task["auto_advance_paused"], 1)
        self.assertIsNone(task["saved_at"])
        for key in ("run_id", "session_id", "send_state", "failure_code", "failure_reason", "failure_stage", "blocked_summary"):
            self.assertIsNone(task[key], key)
        for key in ("plan_path", "plan_sha256", "plan_kind", "plan_generated_at", "planner_model", "reasoning_effort"):
            self.assertIsNone(task[key], key)
        for key in ("source_path", "source_sha256"):
            self.assertEqual(task[key], before[key])
        self.assertEqual(fixture["plan_path"].read_bytes(), old_plan)
        self.assertEqual(Path(fixture["source_path"]).read_bytes(), old_source)
        self.assertTrue(all(method == "GET" for method, _, _ in self.fake.requests_log))
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT save_state FROM execution_rounds WHERE run_id='old-run'").fetchone()[0], "purged")
            requests = [dict(r) for r in conn.execute("SELECT * FROM requests WHERE task_id=?", (self.task_id,))]
            self.assertEqual(requests, old_requests)
            fresh = db.require_task(conn, self.task_id)
            self.assertFalse(core.is_automation_managed(fresh))
            self.assertFalse(core.automation_of(fresh)["plan_wanted"])
            self.assertEqual(core.automation_of(fresh)["cost_usd"], 2.5)
            self.assertEqual(db.task_extra(fresh)["review_history"], [{"conclusion": "fail"}])
            events = conn.execute("SELECT kind FROM events WHERE task_id=?", (self.task_id,)).fetchall()
            self.assertIn("task_purged", [r[0] for r in events])
            self.assertIn("task_reconnected", [r[0] for r in events])
        finally:
            conn.close()

    def test_trashed_and_archived_can_reconnect(self):
        self.removed("trashed")
        self.assertTrue(self.reconnect("trashed")["ok"])
        self.call("archive-task", {"task_id": self.task_id})
        self.assertTrue(self.reconnect("archived")["ok"])

    def test_ordinary_restore_still_rejects_purged(self):
        self.removed()
        before = self.snapshot()
        self.assertEqual(self.call_err("restore-task", {"task_id": self.task_id}).code, "conflict")
        self.assertEqual(before, self.snapshot())
        self.sync_task(self.fake.tasks[self.task_id], project_name="Feedback")
        self.assertEqual(before, self.snapshot())

    def test_expected_save_state_required_valid_and_exact(self):
        self.removed()
        for payload, code in (({"task_id": self.task_id}, "bad_request"),
                              ({"task_id": self.task_id, "expected_save_state": "active"}, "bad_request"),
                              ({"task_id": self.task_id, "expected_save_state": "trashed"}, "conflict")):
            before = self.snapshot()
            self.assertEqual(self.call_err("reconnect-task", payload).code, code)
            self.assertEqual(before, self.snapshot())
        self.reconnect()
        self.set_task_status(self.task_id, "ready")
        before = self.snapshot()
        self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
        self.assertEqual(before, self.snapshot())

    def test_remote_changes_and_unavailable_fail_without_writes(self):
        self.removed()
        for changes in ({"status": "in-progress"}, {"projectId": "different-project"}, {"id": "different-task"}):
            with self.subTest(changes=changes):
                live = remote.get_task(self.fake, self.task_id)
                live["task"] = {**live["task"], **changes}
                before = self.snapshot()
                with patch.object(remote, "get_task", return_value=live):
                    error = self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"})
                self.assertEqual(error.code, "conflict")
                self.assertEqual(before, self.snapshot())
        self.fake.fail_next = RuntimeError("unavailable")
        before = self.snapshot()
        self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "remote_unavailable")
        self.assertEqual(before, self.snapshot())

    def test_safety_conflicts_and_fetch_eligibility(self):
        self.removed()
        for fields in ({"send_state": "unknown"}, *({"status": status} for status in constants.ARCHIVE_BLOCKED_STATUSES)):
            with self.subTest(fields=fields):
                self.set_task_status(self.task_id, "needs_user", send_state=None)
                self.set_task_status(self.task_id, fields.get("status", "needs_user"), **{k: v for k, v in fields.items() if k != "status"})
                before = self.snapshot()
                item = next(x for x in self.call("fetch-todos", {})["items"] if x["task_id"] == self.task_id)
                self.assertFalse(item["reconnectable"])
                self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
                self.assertEqual(before, self.snapshot())

    def test_queued_claimed_and_lost_requests_block(self):
        self.removed()
        conn = db.connect(self.db_path)
        try:
            req, _ = db.create_request(conn, util.now_iso(), "plan", "new-plan", task_id=self.task_id)
            for status, lease in (("queued", None), ("claimed", util.plus_seconds(600, util.now_dt())),
                                  ("claimed", util.to_iso(util.now_dt() - timedelta(seconds=60)))):
                db.update_request(conn, req["request_id"], util.now_iso(), status=status, lease_expires_at=lease)
                before = self.snapshot()
                item = next(x for x in self.call("fetch-todos", {})["items"] if x["task_id"] == self.task_id)
                self.assertFalse(item["reconnectable"])
                self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
                self.assertEqual(before, self.snapshot())
        finally:
            conn.close()

    def test_unusable_mapping_blocks_and_mapping_race_is_rechecked(self):
        self.removed()
        pid = self.fake.tasks[self.task_id]["projectId"]
        conn = db.connect(self.db_path)
        try:
            valid = project_config.get_effective_mapping(conn, pid)
        finally:
            conn.close()
        for unavailable in ({**valid, "mapping_state": "disabled"}, {**valid, "mapping_state": "missing"}, None):
            with patch.object(project_config, "get_effective_mapping", return_value=unavailable):
                before = self.snapshot()
                self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
                self.assertEqual(before, self.snapshot())
        with patch.object(project_config, "get_effective_mapping", side_effect=[valid, {**valid, "revision": valid["revision"] + 1}]):
            before = self.snapshot()
            self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
            self.assertEqual(before, self.snapshot())

    def test_state_changed_during_remote_read_is_rechecked(self):
        self.removed()
        read = remote.get_task
        def raced(client, task_id):
            live = read(client, task_id)
            self.set_task_status(task_id, "needs_plan", save_state="active")
            return live
        with patch.object(remote, "get_task", side_effect=raced):
            self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
        self.assertEqual(self.raw_task()["save_state"], "active")
        self.assertEqual(self.raw_task()["run_id"], "old-run")

    def test_request_created_during_remote_read_is_rechecked(self):
        self.removed()
        read = remote.get_task
        def raced(client, task_id):
            live = read(client, task_id)
            conn = db.connect(self.db_path)
            try:
                db.create_request(conn, util.now_iso(), "plan", "raced-plan", task_id=task_id)
            finally:
                conn.close()
            return live
        with patch.object(remote, "get_task", side_effect=raced):
            self.assertEqual(self.call_err("reconnect-task", {"task_id": self.task_id, "expected_save_state": "purged"}).code, "conflict")
        self.assertEqual(self.raw_task()["save_state"], "purged")
        self.assertEqual(self.raw_task()["run_id"], "old-run")

    def test_new_manual_plan_can_be_claimed_and_failure_keeps_pause(self):
        self.removed()
        self.reconnect()
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        bad = self.call("start-plans", {"items": [{"input_id": prepared["input_id"], "input_sha256": "stale"}]})
        self.assertEqual(bad["started"], 0)
        self.assertEqual(self.raw_task()["auto_advance_paused"], 1)
        self.assertTrue(prepared["can_start"])
        started = self.call("start-plans", {"items": [{"input_id": prepared["input_id"], "input_sha256": prepared["input_sha256"]}]})
        self.assertEqual(started["started"], 1, started)
        self.assertEqual(self.raw_task()["auto_advance_paused"], 0)
        claim = self.call("claim", {"stage": "plan", "executor": constants.PLANNER_EXECUTOR_ID,
                                   "request_id": started["results"][0]["request_id"]})
        self.assertTrue(claim["claimed"], claim)
        self.assertEqual(self.raw_task()["status"], "planning")
        self.assertNotEqual(self.raw_task()["run_id"], "old-run")

    def test_new_requirements_publish_with_retained_old_files_and_execution_hold(self):
        fixture = self.removed()
        old_plan = fixture["plan_path"].read_bytes()
        old_source = Path(fixture["source_path"]).read_bytes()
        missing_worktree = str(self.root / "removed-old-worktree")
        old_delivery = {"mode": "pr", "branch": "merged-old-branch", "worktree_path": missing_worktree, "head_sha": "a" * 40}
        conn = db.connect(self.db_path)
        try:
            db.set_extra(conn, db.require_task(conn, self.task_id), util.now_iso(), delivery=old_delivery)
        finally:
            conn.close()
        self.fake.tasks[self.task_id]["description"] = "New requirement after reconnect"
        self.reconnect()
        self.assertEqual(core.delivery_of(self.raw_task()), {"mode": "manual"})
        conn = db.connect(self.db_path)
        try:
            detail = json.loads(conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='task_reconnected'", (self.task_id,)).fetchone()[0])
            self.assertEqual(detail["previous"]["delivery"], old_delivery)
        finally:
            conn.close()
        prepared = self.call("prepare-plan-input", {"task_id": self.task_id})
        started = self.call("start-plans", {"items": [{"input_id": prepared["input_id"], "input_sha256": prepared["input_sha256"]}]})
        self.assertEqual(started["started"], 1, started)
        from kanpilot import planner_runner
        with patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}):
            self.assertEqual(planner_runner.run_loop(self.db_path, None, once=True), 0)
        task = self.raw_task()
        self.assertEqual(task["status"], "ready")
        self.assertNotEqual(task["plan_path"], str(fixture["plan_path"]))
        self.assertNotEqual(task["requirements_sha256"], fixture["requirements_sha256"])
        self.assertEqual(fixture["plan_path"].read_bytes(), old_plan)
        self.assertEqual(Path(fixture["source_path"]).read_bytes(), old_source)
        self.assertTrue(db.task_extra(task)["plan_execution_hold"])
        self.assertEqual(self.call_err("enqueue", {"task_id": self.task_id, "kind": "dispatch"}).code, "conflict")
        self.assertFalse(self.call("claim", {"stage": "dispatch", "executor": "test-dispatch"})["claimed"])
        self.assertFalse(Path(missing_worktree).exists())
