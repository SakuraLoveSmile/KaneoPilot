"""Desktop Plus control: isolated DB/files, no real Plus or model calls."""

import errno
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from helpers import CORE, KanpilotTestCase
from kanpilot import constants, db, planner_inputs, session_control, util


class SessionControlTests(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.task_id = "control-task"
        self.runtime = {"connection_file": str(self.root / "fake-plus.json"),
                        "provider_id": "test", "model_id": "test-model",
                        "thinking_level": "high", "permission_mode": "ask"}
        self.control_calls = []
        calls = self.control_calls
        class ReadonlyPlus(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = request["method"]
                calls.append(method)
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "isolated-plus", "version": "fixture"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": name} for name in constants.PI_DESKTOP_PLAN_TOOLS]}
                else:
                    raise AssertionError("The control test must never send or create: " + method)
                body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReadonlyPlus)
        self.server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)
        connection = self.root / "fake-plus.json"
        connection.write_text(json.dumps({"active": True, "pid": os.getpid(), "token": "isolated-fake-token",
                                          "url": "http://127.0.0.1:%d/mcp" % self.server.server_port}))
        connection.chmod(0o600)
        self.manifest = {"input_id": "control-input", "task": {"task_id": self.task_id}, "files": []}
        self.input_sha = planner_inputs._manifest_sha(self.manifest)
        self.input_dir = self.app_support / "planner_inputs" / "control-input"
        self.input_dir.mkdir(parents=True)
        (self.input_dir / "manifest.json").write_text(json.dumps(self.manifest))
        conn = db.connect(self.db_path)
        now = util.now_iso()
        with db.write_txn(conn):
            conn.execute("INSERT INTO tasks(task_id,project_id,project_name,stage,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                         (self.task_id, "test-project", "Test", "plan", "needs_plan", now, now))
            db.insert_planner_input(conn, "control-input", self.task_id, self.input_sha, None, None, self.manifest, now)
            row, _ = db.create_request(conn, now, "plan", "control-plan", self.task_id,
                                     payload={"manual_authorized": True, "confirmed_at": now,
                                              "planner_backend": "pi_desktop", "planner_method": "builtin",
                                              "runtime_config": self.runtime, "input_id": "control-input",
                                              "planner_input_sha256": self.input_sha, "manifest": self.manifest})
            self.request_id = row["request_id"]
        conn.close()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": self.request_id})
        self.run_id = claim["run_id"]
        # A real reaped child proves the positive recovery path is not merely
        # a mocked "pid_alive=False" for an arbitrary or reused PID.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=5)
        self.dead_pid = child.pid
        self.job = {"task_id": self.task_id, "run_id": self.run_id, "request_id": self.request_id,
                    "backend": "pi_desktop", "pid": self.dead_pid, "status": "running",
                    "config": self.runtime, "input_id": "control-input",
                    "planner_input_sha256": self.input_sha, "session_id": None,
                    "turn_id": None, "send_state": None}
        self.directory = self.runs_root / self.task_id / self.run_id
        self.directory.mkdir(parents=True)
        self.write_job()
        self.change_request(lease_expires_at=util.plus_seconds(-60))
        self.payload = {"task_id": self.task_id, "run_id": self.run_id, "request_id": self.request_id}
        self.status_payload = {"task_id": self.task_id, "run_id": self.run_id}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def write_job(self):
        (self.directory / "job.json").write_text(json.dumps(self.job), encoding="utf-8")

    def change_request(self, **fields):
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            db.update_request(conn, self.request_id, util.now_iso(), **fields)
        conn.close()

    def change_task(self, **fields):
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            db.update_task(conn, self.task_id, util.now_iso(), **fields)
        conn.close()

    def snapshot(self):
        conn = db.connect(self.db_path)
        result = {table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                  for table in ("tasks", "requests", "attempts", "events", "execution_rounds")}
        conn.close()
        return result

    def blocked(self):
        before = self.snapshot()
        error = self.call_err("session-request-start", self.payload)
        self.assertIn(error.code, ("conflict", "stale_run", "validation_failed"))
        self.assertEqual(before, self.snapshot())

    def test_requeue_preserves_original_request_payload_attempts_and_job(self):
        before = self.snapshot()
        original_job = (self.directory / "job.json").read_bytes()
        response = self.call("session-request-start", self.payload)
        self.assertEqual(response, {"request_id": self.request_id, "stage": "plan", "queued": True, "deduplicated": False})
        after = self.snapshot()
        self.assertEqual(before["attempts"], after["attempts"])
        conn = db.connect(self.db_path)
        request = db.get_request(conn, self.request_id)
        self.assertEqual(request["status"], "queued")
        self.assertEqual(request["run_id"], self.run_id)
        self.assertEqual(request["attempt_no"], 1)
        self.assertIsNone(request["lease_expires_at"])
        self.assertEqual(db.get_task(conn, self.task_id)["status"], "needs_plan")
        self.assertEqual(request["payload_json"], before["requests"][0][8])
        conn.close()
        self.assertEqual(original_job, (self.directory / "job.json").read_bytes())
        self.assertTrue(set(self.control_calls).issubset({"initialize", "notifications/initialized", "tools/list"}))
        repeated = self.call("session-request-start", self.payload)
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(after, self.snapshot())
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": self.request_id})
        self.assertNotEqual(claim["run_id"], self.run_id)
        self.blocked()

    def test_auto_snapshot_requeues_without_changing_permission(self):
        self.runtime["permission_mode"] = "auto"
        conn = db.connect(self.db_path)
        try:
            payload = db.request_payload(db.get_request(conn, self.request_id))
        finally:
            conn.close()
        payload["runtime_config"] = dict(self.runtime)
        self.change_request(payload_json=payload)
        self.write_job()
        self.assertTrue(self.call("session-control-status", self.status_payload)["can_start"])
        original_job = (self.directory / "job.json").read_bytes()
        self.assertTrue(self.call("session-request-start", self.payload)["queued"])
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.request_payload(db.get_request(conn, self.request_id)), payload)
        finally:
            conn.close()
        self.assertEqual((self.directory / "job.json").read_bytes(), original_job)

    def test_unsupported_permission_snapshot_cannot_requeue(self):
        self.runtime["permission_mode"] = "accept-edits"
        conn = db.connect(self.db_path)
        try:
            payload = db.request_payload(db.get_request(conn, self.request_id))
        finally:
            conn.close()
        payload["runtime_config"] = dict(self.runtime)
        self.change_request(payload_json=payload)
        self.write_job()
        self.blocked()

    def test_separate_cli_process_requeues_and_deduplicates(self):
        env = dict(os.environ, KANPILOT_DB=str(self.db_path),
                   KANEO_CREDENTIALS_PATH=str(self.root / "fake-credentials.json"))
        def invoke():
            result = subprocess.run([sys.executable, str(CORE / "kanpilot.py"), "session-request-start", "--json", json.dumps(self.payload)],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertFalse(invoke()["deduplicated"])
        self.assertTrue(invoke()["deduplicated"])

    def test_live_pid_invalid_pid_and_unknown_process_state_block(self):
        for pid in (os.getpid(), None, 0, True, "123"):
            with self.subTest(pid=pid):
                self.job["pid"] = pid
                self.write_job()
                self.blocked()
        self.job["pid"] = self.dead_pid
        self.write_job()
        with patch("kanpilot.session_control.os.kill", side_effect=PermissionError(errno.EPERM, "private-token")):
            self.blocked()

    def test_active_or_missing_lease_blocks(self):
        for lease in (util.plus_seconds(3600), None, "bad-date"):
            with self.subTest(lease=lease):
                self.change_request(lease_expires_at=lease)
                self.blocked()

    def test_session_turn_and_any_current_send_state_block(self):
        for field, value in (("session_id", "session-1"), ("turn_id", "turn-1"), ("send_state", "sent"),
                             ("send_state", "unknown"), ("send_state", "not_sent")):
            with self.subTest(field=field, value=value):
                self.job[field] = value
                self.write_job()
                self.blocked()
                self.job[field] = None

    def test_paused_saved_and_changed_task_status_block(self):
        for fields, reset in (({"save_state": "trashed"}, {"save_state": "active"}),
                              ({"auto_advance_paused": 1}, {"auto_advance_paused": 0}),
                              ({"status": "needs_user"}, {"status": "planning"}),
                              ({"send_state": "unknown"}, {"send_state": None})):
            self.change_task(**fields)
            self.blocked()
            self.change_task(**reset)
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            db.set_setting(conn, "paused_stages", ["plan"])
        conn.close()
        self.blocked()

    def test_identity_backend_old_run_and_different_request_block(self):
        for key, value in (("task_id", "other"), ("run_id", "other"), ("request_id", "other"), ("backend", "codex")):
            original = self.job[key]
            self.job[key] = value
            self.write_job()
            self.blocked()
            self.job[key] = original
        self.write_job()
        self.payload["request_id"] = "different-request"
        self.blocked()

    def test_authorization_and_material_fingerprint_block(self):
        conn = db.connect(self.db_path)
        original = db.request_payload(db.get_request(conn, self.request_id))
        conn.close()
        for key, value in (("manual_authorized", False), ("planner_backend", "codex"),
                           ("task_status_at_claim", "running"), ("planner_input_sha256", "changed")):
            self.change_request(payload_json=dict(original, **{key: value}))
            self.blocked()
        self.change_request(payload_json=original)
        (self.input_dir / "manifest.json").write_text("{}")
        self.blocked()

    def test_historical_attempt_send_and_event_evidence_block(self):
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE attempts SET send_state='unknown' WHERE request_id=?", (self.request_id,))
        conn.close()
        self.blocked()
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE attempts SET send_state=NULL WHERE request_id=?", (self.request_id,))
            db.add_event(conn, util.now_iso(), "send", request_id=self.request_id,
                         detail={"evidence": {"turnId": "historical-turn"}})
        conn.close()
        self.blocked()

    def test_old_round_missing_or_sent_blocks(self):
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("INSERT INTO attempts(request_id,task_id,run_id,stage,kind) VALUES (?,?,?,?,?)",
                         (self.request_id, self.task_id, "old-run", "plan", "plan"))
            # Current attempt remains latest for the independent history check.
            db.add_attempt(conn, self.request_id, self.task_id, self.run_id, "plan", "plan", util.now_iso(), "claimed")
        conn.close()
        self.blocked()
        old_dir = self.runs_root / self.task_id / "old-run"
        old_dir.mkdir()
        (old_dir / "job.json").write_text(json.dumps(dict(self.job, run_id="old-run", session_id="old-session")))
        self.blocked()

    def test_status_readonly_uses_round_snapshot_and_no_prompt(self):
        before = self.snapshot()
        with patch.object(session_control.harness_plus_adapter, "plus_capabilities", return_value={"connected": True, "available": True, "supported_operations": list(constants.PI_DESKTOP_PLAN_TOOLS)}) as capabilities, \
                patch.object(session_control.harness_plus_adapter, "plus_prompt") as prompt:
            response = self.call("session-control-status", self.status_payload)
        self.assertEqual(response["state"], "executor_lost")
        self.assertTrue(response["can_start"])
        capabilities.assert_called_once_with(self.runtime["connection_file"], timeout=2)
        prompt.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_status_running_permission_idle_and_no_sensitive_raw(self):
        self.job["session_id"] = "test-session"
        self.write_job()
        for view, state in (({"is_running": True}, "running"),
                            ({"is_running": True, "pending_permission": {"body": "SECRET-REQUEST"}}, "waiting_permission"),
                            ({"is_running": False}, "idle")):
            view = dict(view, session_id="test-session", raw_status={"token": "SECRET-TOKEN", "isRunning": view["is_running"]})
            with patch.object(session_control.harness_plus_adapter, "plus_status", return_value=view) as status:
                response = self.call("session-control-status", self.status_payload)
            self.assertEqual(response["state"], state)
            self.assertTrue(response["connected"])
            self.assertFalse(response["can_start"])
            self.assertNotIn("SECRET", json.dumps(response))
            status.assert_called_once_with(self.runtime["connection_file"], session_id="test-session", timeout=2)

    def test_incomplete_plus_status_cannot_claim_idle_or_connected(self):
        self.job["session_id"] = "test-session"
        self.write_job()
        with patch.object(session_control.harness_plus_adapter, "plus_status", return_value={
                "session_id": "test-session", "is_running": False, "raw_status": {}}):
            response = self.call("session-control-status", self.status_payload)
        self.assertEqual(response["state"], "unavailable")
        self.assertFalse(response["connected"])

    def test_nested_plus_status_contract_through_adapter(self):
        self.job["session_id"] = "test-session"
        self.write_job()
        before = self.snapshot()
        for running, pending, state in ((True, [], "running"), (False, [], "idle"),
                                        (True, [{"body": "SECRET-REQUEST"}], "waiting_permission")):
            with self.subTest(state=state):
                client = MagicMock()
                client.call_tool.return_value = {"status": {
                    "isRunning": running, "currentTurnId": "test-turn",
                    "pendingToolConfirmations": pending}, "isRunning": "invalid"}
                with patch.object(session_control.harness_plus_adapter, "_get_client",
                                  return_value=(client, {})):
                    response = self.call("session-control-status", self.status_payload)
                self.assertEqual(response["state"], state)
                self.assertTrue(response["connected"])
                self.assertEqual(response["turn_id"], "test-turn")
                self.assertFalse(response["can_start"])
                self.assertNotIn("SECRET", json.dumps(response))
                client.call_tool.assert_called_once_with("pi_agent_status", {"sessionId": "test-session"})
        self.assertEqual(before, self.snapshot())

    def test_start_label_matches_round_type_and_keeps_disabled_reason(self):
        with patch.object(session_control.harness_plus_adapter, "plus_capabilities", return_value={
                "connected": True, "available": True,
                "supported_operations": list(constants.PI_DESKTOP_PLAN_TOOLS)}):
            planning = self.call("session-control-status", self.status_payload)
            self.assertEqual(planning["start_label"], "请求开始规划")
            self.change_request(kind="dispatch", stage="dispatch")
            execution = self.call("session-control-status", self.status_payload)
        self.assertEqual(execution["start_label"], "请求开始")
        self.assertFalse(execution["can_start"])
        self.assertIn("不是当前规划请求", execution["blocked_reason"])

    def test_durable_runtime_receipt_without_job_scope_blocks(self):
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("INSERT INTO runtime_operations(scope_id,operation_id,harness_id,tool,request_sha256,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         ("planner_" + self.task_id + "_" + self.run_id, "create", "pi_desktop",
                          "kp_runtime_session_create", "fake", "in_flight", util.now_iso(), util.now_iso()))
        conn.close()
        self.blocked()

    def test_concurrent_cli_start_only_requeues_once(self):
        env = dict(os.environ, KANPILOT_DB=str(self.db_path),
                   KANEO_CREDENTIALS_PATH=str(self.root / "fake-credentials.json"))
        argv = [sys.executable, str(CORE / "kanpilot.py"), "session-request-start", "--json", json.dumps(self.payload)]
        processes = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        responses = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            responses.append(json.loads(stdout))
        self.assertEqual(sorted(row["deduplicated"] for row in responses), [False, True])

    def test_log_evidence_and_malformed_partial_history_block(self):
        for content in ('{"v":1,"seq":1,"type":"session","session_id":"session"}\n',
                        '{"v":1,"seq":1,"type":"user","content":"sent prompt"}\n',
                        '{"v":1,"seq":1,"type":"terminal","outcome":"completed"}\n',
                        '{"v":1,"seq":1,"type":"terminal","outcome":"failed","reason":"send_unknown: interrupted"}\n',
                        '{"v":1,"seq":1,"type":"system"}\n{"seq":2,"type":"session",',
                        '{"seq":1,"type":"unknown_activity"}\n', 'not-json\n'):
            with self.subTest(content=content):
                (self.directory / "events.jsonl").write_text(content)
                self.blocked()

    def test_empty_or_presend_system_log_does_not_prevent_recovery(self):
        (self.directory / "events.jsonl").write_text('{"v":1,"seq":1,"type":"system","content":"checking configuration"}\n')
        self.assertTrue(self.call("session-request-start", self.payload)["queued"])

    def test_round_index_send_evidence_and_saved_state_block(self):
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE execution_rounds SET session_id='durable-session' WHERE task_id=? AND run_id=?", (self.task_id, self.run_id))
        conn.close()
        self.blocked()
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE execution_rounds SET session_id=NULL,save_state='trashed' WHERE task_id=? AND run_id=?", (self.task_id, self.run_id))
        conn.close()
        self.blocked()

    def test_changed_actual_material_file_blocks(self):
        content = b"approved material"
        (self.input_dir / "files").mkdir()
        (self.input_dir / "files" / "input.txt").write_bytes(content)
        manifest = dict(self.manifest, files=[{"path": "files/input.txt", "sha256": util.sha256_bytes(content)}])
        input_sha = planner_inputs._manifest_sha(manifest)
        (self.input_dir / "manifest.json").write_text(json.dumps(manifest))
        conn = db.connect(self.db_path)
        payload = db.request_payload(db.get_request(conn, self.request_id))
        payload.update(manifest=manifest, planner_input_sha256=input_sha)
        with db.write_txn(conn):
            conn.execute("UPDATE planner_inputs SET manifest_json=?,input_sha256=? WHERE input_id='control-input'", (util.jdump(manifest), input_sha))
            db.update_request(conn, self.request_id, util.now_iso(), payload_json=payload)
        conn.close()
        self.job["planner_input_sha256"] = input_sha
        self.write_job()
        (self.input_dir / "files" / "input.txt").write_bytes(b"changed")
        self.blocked()

    def test_missing_plan_tools_blocks_status_and_start(self):
        before = self.snapshot()
        with patch.object(session_control.harness_plus_adapter, "plus_capabilities", return_value={
                "connected": True, "available": True, "supported_operations": list(constants.PI_DESKTOP_REQUIRED_TOOLS)}):
            response = self.call("session-control-status", self.status_payload)
            self.blocked()
        self.assertEqual(response["state"], "unsupported")
        self.assertFalse(response["can_start"])
        self.assertEqual(before, self.snapshot())

    def test_disconnected_start_never_mutates_queue(self):
        with patch.object(session_control.harness_plus_adapter, "plus_capabilities", return_value={"connected": False}):
            self.blocked()

    def test_status_disconnected_unsupported_and_unavailable(self):
        for view, state in (({"connected": False, "available": False, "detail": "SECRET"}, "disconnected"),
                            ({"connected": True, "available": False}, "unsupported")):
            with patch.object(session_control.harness_plus_adapter, "plus_capabilities", return_value=view):
                response = self.call("session-control-status", self.status_payload)
            self.assertEqual(response["state"], state)
            self.assertFalse(response["can_start"])
            self.assertNotIn("SECRET", json.dumps(response))
        with patch.object(session_control.harness_plus_adapter, "plus_capabilities", side_effect=RuntimeError("SECRET")):
            response = self.call("session-control-status", self.status_payload)
            self.assertEqual(response["state"], "disconnected")
            self.assertNotIn("SECRET", json.dumps(response))
        self.job["task_id"] = "wrong-task"
        self.write_job()
        self.assertEqual(self.call("session-control-status", self.status_payload)["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
