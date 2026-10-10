"""Desktop publish/terminal-export crash window, isolated files and fake Kaneo."""
from datetime import timedelta
from unittest.mock import patch

from kanpilot import db, dsh_events, util
from test_session import HeadlessCase, pi_job, simple_events, write_json


class TestDesktopPublishedSession(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("desktop-published-task", None, status="ready")
        self.run_id = "desktop-plan-run"
        self.request_id = "desktop-plan-request"
        self.now = util.now_dt()
        self.binding = {"role": "plan", "run_id": self.run_id,
                        "operation_id": "publish-operation", "token_hash": "b" * 64}
        self.payload = {"planner_backend": "codex_desktop", "desktop_claim": self.binding}
        self.job = pi_job(self.task_id, self.run_id, backend="codex_desktop",
                          status="running", request_id=self.request_id,
                          started_at=util.to_iso(self.now), ended_at=None, exit_code=None)
        self.directory = self.make_run(self.task_id, self.run_id,
                                       simple_events("规划正文已准备", terminal=False), job=self.job)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,"
                "run_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.request_id, self.request_id, self.task_id, "plan", "plan", "claimed",
                 self.run_id, util.jdump(self.payload), util.to_iso(self.now), util.to_iso(self.now)),
            )

    def connection(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def full(self):
        return self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id})

    def status(self):
        return self.call("get-session", {"task_id": self.task_id, "run_id": self.run_id,
                                         "status_only": True})

    def listed(self):
        rows = self.call("list-sessions", {"task_id": self.task_id})["sessions"]
        return next(row for row in rows if row["run_id"] == self.run_id)

    def published(self, **changes):
        with self.connection() as conn:
            db.update_request(conn, self.request_id, util.to_iso(self.now),
                              status="succeeded", result_json={"outcome": "succeeded", "plan_sha256": "a" * 64},
                              finished_at=util.to_iso(self.now), **changes)

    def assert_all_finished(self):
        self.assertEqual(self.listed()["run_status"], "finished")
        for result in (self.full(), self.status()):
            self.assertEqual(result["session"]["run_status"], "finished")
            self.assertEqual(result["round_detail"]["run_status"], "finished")

    def test_published_request_overrides_running_job_without_mutating_records(self):
        self.assertEqual(self.listed()["run_status"], "running")
        self.assertEqual(self.full()["session"]["run_status"], "running")
        self.assertEqual(self.status()["session"]["run_status"], "running")
        self.published()
        with self.connection() as conn:
            baseline = {table: [dict(row) for row in conn.execute("SELECT * FROM " + table)]
                        for table in ("tasks", "requests", "attempts", "execution_rounds")}
        files = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        self.assert_all_finished()
        with patch.object(util, "now_dt", return_value=self.now + timedelta(days=7)):
            self.assert_all_finished()
        with self.connection() as conn:
            for table, original in baseline.items():
                self.assertEqual([dict(row) for row in conn.execute("SELECT * FROM " + table)], original)
        self.assertEqual({path: path.read_bytes() for path in files}, files)

    def test_idle_status_cache_observes_core_commit_without_rereading_transcript(self):
        self.status()
        self.published()
        with patch.object(dsh_events, "_read_events", side_effect=AssertionError("idle log reread")):
            result = self.status()
        self.assertEqual(result["session"]["run_status"], "finished")
        self.assertEqual(result["round_detail"]["run_status"], "finished")

    def test_missing_terminal_and_empty_events_keep_core_finished_proof(self):
        (self.directory / "events.jsonl").write_text("", encoding="utf-8")
        self.assertEqual(self.status()["session"]["run_status"], "unknown")
        self.published()
        self.assert_all_finished()

    def test_unrelated_or_unbound_requests_do_not_override_runtime(self):
        cases = (
            {"run_id": "other-run"},
            {"kind": "review", "stage": "review"},
            {"payload_json": {**self.payload, "planner_backend": "codex"}},
            {"payload_json": {**self.payload, "desktop_claim": {**self.binding, "run_id": "other-run"}}},
            {"payload_json": {**self.payload, "desktop_claim": {**self.binding, "role": "review"}}},
            {"payload_json": {"planner_backend": "codex_desktop"}},
            {"result_json": {"outcome": "succeeded"}},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.connection() as conn:
                    db.update_request(conn, self.request_id, util.to_iso(self.now),
                                      status="succeeded", kind="plan", stage="plan", run_id=self.run_id,
                                      payload_json=self.payload,
                                      result_json={"outcome": "succeeded", "plan_sha256": "a" * 64})
                    db.update_request(conn, self.request_id, util.to_iso(self.now), **changes)
                self.assertEqual(self.listed()["run_status"], "running")
                self.assertEqual(self.full()["session"]["run_status"], "running")
                self.assertEqual(self.status()["session"]["run_status"], "running")

    def test_other_backend_running_round_retains_timeout_semantics(self):
        self.published()
        write_json(self.directory / "job.json", {**self.job, "backend": "codex"})
        self.assertEqual(self.listed()["run_status"], "running")
        self.assertEqual(self.full()["session"]["run_status"], "running")
        self.assertEqual(self.status()["session"]["run_status"], "running")
        with patch.object(util, "now_dt", return_value=self.now + timedelta(days=7)):
            self.assertEqual(self.full()["session"]["run_status"], "failed")
            self.assertEqual(self.status()["session"]["run_status"], "failed")
