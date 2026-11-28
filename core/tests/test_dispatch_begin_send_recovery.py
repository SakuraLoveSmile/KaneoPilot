"""begin-send 幂等、失败分流与派发恢复回归（kp-nx01）。

所有远端访问都经内存 FakeKaneoClient；执行器边界全部 mock。测试数据库、计划、
仓库与运行材料均位于 KanpilotTestCase 的临时目录，不接触真实调度台或 Kaneo。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import KanpilotTestCase
from kanpilot import (
    auto_dispatch,
    bridge,
    cmd_dispatch,
    cmd_exec,
    cmd_queue,
    codex_environment,
    constants,
    core,
    db,
    derive,
    dispatch_validate,
    exec_config,
    process_owner,
    remote,
    round_store,
    util,
)


class _Endpoint:
    """在 FakeKaneoClient 外面注入连接层故障，只影响请求的传输层。"""

    def __init__(self, inner):
        self.inner = inner
        self.fail_writes = False
        self.fail_reads_after_write = False
        self.read_limit = 0
        self.read_failures = 0
        self.written = False

    def request(self, path, method="GET", body=None):
        if method == "PUT" and self.fail_writes:
            raise bridge.BridgeError(
                f"Kaneo 请求失败: URLError(Connection reset by peer) PUT {path}")
        payload = self.inner.request(path, method, body)
        if method == "PUT":
            self.written = True
        elif (self.written and self.fail_reads_after_write
              and self.read_failures < self.read_limit):
            self.read_failures += 1
            raise bridge.BridgeError(
                f"Kaneo 请求失败: URLError(timed out) GET {path}")
        return payload


class BeginSendRecovery(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.configure_codex_execution()
        self.endpoint = _Endpoint(self.fake)
        remote.set_client_factory(lambda: self.endpoint)
        available = patch.object(exec_config, "check_available", return_value=(True, None))
        available.start()
        self.addCleanup(available.stop)
        sleep = patch.object(cmd_dispatch.time, "sleep", lambda *_: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def write_plan(self, project_name, task, meta_overrides=None, body="# plan\n"):
        return super().write_plan(
            project_name, task, meta_overrides,
            body + "\n```verify\npython3 -m unittest\n```\n")

    # ------------------------------------------------------------ fixtures/assertions

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _task_row(self, task_id):
        conn = self._connect()
        try:
            return dict(db.require_task(conn, task_id))
        finally:
            conn.close()

    def _puts(self):
        return [entry for entry in self.fake.requests_log if entry[0] == "PUT"]

    def _dispatch_requests(self, task_id):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM requests WHERE task_id=? AND kind='dispatch' ORDER BY rowid",
                (task_id,),
            ).fetchall()
            return [dict(row, result=util.jload(row["result_json"])) for row in rows]
        finally:
            conn.close()

    def _request(self, request_id):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            return dict(row, result=util.jload(row["result_json"]))
        finally:
            conn.close()

    def _attempts(self, task_id=None, request_id=None, dispatch_only=False):
        clauses, params = [], []
        if dispatch_only:
            clauses.append("kind='dispatch'")
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM attempts" + where + " ORDER BY id", params
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _settings(self):
        conn = self._connect()
        try:
            return {
                row["key"]: util.jload(row["value"])
                for row in conn.execute(
                    "SELECT key, value FROM settings WHERE key IN "
                    "('dispatch_serial','lifecycle_owner')")
            }
        finally:
            conn.close()

    def _events(self, task_id, kind=None):
        sql = "SELECT * FROM events WHERE task_id=?"
        params = [task_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id"
        conn = self._connect()
        try:
            return [
                dict(row, detail=util.jload(row["detail_json"]))
                for row in conn.execute(sql, params)
            ]
        finally:
            conn.close()

    def _recovery(self, task_id):
        value = self._task_row(task_id)["resolution"]
        resolution = value if isinstance(value, dict) else util.jload(value)
        return (resolution or {}).get("dispatch_recovery")

    def _assert_terminal_failure(self, request_id, run_id, send_state):
        request = self._request(request_id)
        self.assertEqual(request["status"], "failed")
        self.assertEqual(request["run_id"], run_id)
        self.assertEqual(request["attempt_no"], 1)
        self.assertIsNotNone(request["finished_at"])
        self.assertEqual(request["result"]["outcome"], "failed")
        self.assertEqual(request["result"]["stage"], "begin-send")
        self.assertEqual(request["result"]["run_id"], run_id)
        self.assertIn(request["result"]["failure_stage"], ("prepared", "submitting"))
        self.assertEqual(request["result"]["send_state"], send_state)
        attempts = self._attempts(request_id=request_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["task_id"], request["task_id"])
        self.assertEqual(attempts[0]["run_id"], run_id)
        self.assertEqual(attempts[0]["stage"], "dispatch")
        self.assertEqual(attempts[0]["kind"], "dispatch")
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertEqual(attempts[0]["send_state"], send_state)
        self.assertIsNotNone(attempts[0]["finished_at"])
        settings = self._settings()
        self.assertEqual(settings["dispatch_serial"], "")
        self.assertEqual(settings["lifecycle_owner"], "")

    def _assert_unknown_block(self, task_id):
        view = self._task_row(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["stage"], "dispatch")
        self.assertEqual(view["failure_code"], "remote_conflict")
        self.assertEqual(view["send_state"], "unknown")
        self.assertEqual(view["sent"], 0)
        self.assertEqual(view["lock_retained"], 1)
        self.assertEqual(view["manual_recovery_required"], 1)
        self.assertEqual(self.fake.tasks[task_id]["status"], "in-progress")

    def _assert_not_sent_block(self, task_id):
        view = self._task_row(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["stage"], "dispatch")
        self.assertEqual(view["failure_code"], "remote_unavailable")
        self.assertEqual(view["send_state"], "not_sent")
        self.assertEqual(view["sent"], 0)
        self.assertEqual(view["lock_retained"], 0)
        self.assertEqual(view["manual_recovery_required"], 0)
        self.assertEqual(self.fake.tasks[task_id]["status"], "to-do")

    # ------------------------------------------------------------ real command chains

    def _prepared(self, *, automation=False, task_id=None, backend="codex"):
        if backend == "pi_desktop":
            connection = self.root / "pi-desktop-connection.json"
            connection.write_text("{}\n", encoding="utf-8")
            self.call("set-exec-config", {
                "backend": "pi_desktop",
                "pi_desktop": {
                    "connection_file": str(connection),
                    "provider_id": "fixture-provider",
                    "model_id": "fixture-model",
                    "thinking_level": "low",
                    "permission_mode": "ask",
                },
            })
        kwargs = {} if task_id is None else {"task_id": task_id}
        fixture = self.make_ready_task(**kwargs)
        tid = fixture["remote"]["id"]
        if automation:
            self.seed_auto_confirmation(tid)
        self.enqueue_dispatch(tid)
        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        service.ping.return_value = None
        with patch.object(
                dispatch_validate.mcp_client.MCPClient, "from_server_file",
                return_value=service), patch.object(
                exec_config, "check_available", return_value=(True, None)):
            claim = self.call(
                "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"], claim)
        self._prepare_claim(claim, fixture["plan_path"])
        return {
            **fixture,
            "task_id": tid,
            "run_id": claim["run_id"],
            "request_id": claim["request_id"],
            "claim": claim,
            "backend": backend,
        }

    def _prepare_claim(self, claim, plan_path):
        context = claim["context"]
        if context["backend"] == "codex":
            evidence = self.valid_evidence(self.task(context["task_id"]), plan_path)
        else:
            evidence = {
                "session_marker": context["session_marker"],
                "repository": context["repository"],
                "backend": context["backend"],
                "config": context["exec_config"],
            }
        return self.call("dispatch-prepare", {
            "task_id": context["task_id"],
            "run_id": claim["run_id"],
            "evidence": evidence,
        })

    def _confirm_codex_without_worker(self, claim, plan_path, session="fixture-session"):
        evidence = self.valid_evidence(
            self.task(claim["context"]["task_id"]), plan_path,
            message_visible=True, session_id=session, session_title="Fixture session")
        return self.call("dispatch-confirm", {
            "task_id": claim["context"]["task_id"],
            "run_id": claim["run_id"],
            "evidence": evidence,
        })

    def _start_and_confirm_mocked_codex(self, claim, request_id):
        task_id = claim["context"]["task_id"]
        specs = []
        worker_calls = []
        real_popen = cmd_dispatch.subprocess.Popen

        def spawn(argv, **kwargs):
            if "kanpilot.codex_worker" not in argv:
                return real_popen(argv, **kwargs)
            worker_calls.append(argv)
            spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
            specs.append(spec)
            (Path(spec["runs_dir"]) / "events.jsonl").write_text(
                json.dumps({
                    "v": 1, "seq": 1, "ts": util.now_iso(), "type": "session",
                    "session_id": "recovery-session", "turn_id": "recovery-turn",
                    "session_title": "Recovery session",
                }) + "\n", encoding="utf-8")
            return Mock(pid=42424)

        with patch.object(exec_config, "check_available", return_value=(True, None)) as available, \
             patch.object(codex_environment, "freeze"), \
             patch.object(process_owner, "bind_spec"), \
             patch.object(cmd_dispatch, "_register_started_worker"), \
             patch.object(cmd_dispatch, "_ensure_worktree",
                          return_value=(Path(self.task(task_id)["repository"]), "plain")), \
             patch.object(cmd_dispatch.subprocess, "Popen", side_effect=spawn):
            started = self.call("dispatch-start", {
                "task_id": task_id, "run_id": claim["run_id"]})
        available.assert_called_once()
        self.assertEqual(len(worker_calls), 1)
        self.assertEqual(started["status"], "started")
        self.assertEqual(started["pid"], 42424)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["request_id"], request_id)
        confirmed = self.call("dispatch-confirm", {
            "task_id": task_id, "run_id": claim["run_id"],
            "evidence": {
                "session_marker": claim["context"]["session_marker"],
                "repository": claim["context"]["repository"],
                "backend": "codex",
                "config": claim["context"]["exec_config"],
                "protocol_seen": True,
                "session_id": "recovery-session",
                "turn_id": "recovery-turn",
            },
        })
        self.assertEqual(confirmed["status"], "running")
        return confirmed

    def _blocked_unknown(self, task_id=None):
        self.endpoint.fail_writes = False
        self.endpoint.fail_reads_after_write = False
        self.endpoint.read_failures = 0
        self.endpoint.read_limit = 0
        self.endpoint.written = False
        puts_before = len(self._puts())
        info = self._prepared(automation=True, task_id=task_id)
        self.endpoint.fail_reads_after_write = True
        self.endpoint.read_limit = 3
        exc = self.call_err("dispatch-begin-send", {
            "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(exc.code, "remote_conflict")
        self.endpoint.fail_reads_after_write = False
        self._assert_unknown_block(info["task_id"])
        self._assert_terminal_failure(info["request_id"], info["run_id"], "unknown")
        self.assertEqual(len(self._puts()), puts_before + 1)
        return info

    def _legacy_block(self, info, send_state):
        conn = self._connect()
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, info["task_id"])
                unknown = send_state == "unknown"
                core.block_task(
                    conn, task, util.now_iso(), "legacy begin-send failure",
                    "remote_conflict" if unknown else "remote_unavailable",
                    "kaneo-dispatch", stage=task["status"],
                    sent=0, send_state=send_state,
                    lock_retained=1 if unknown else 0,
                    manual_recovery_required=1 if unknown else 0,
                    transition_started_at=util.now_iso())
        finally:
            conn.close()

    def _clear_prepare_evidence(self, task_id):
        conn = self._connect()
        try:
            with db.write_txn(conn):
                db.update_task(
                    conn, task_id, util.now_iso(),
                    ui_evidence=None, send_evidence=None,
                    transition_started_at=None, transition_verified_at=None)
        finally:
            conn.close()

    def _seed_queued_recovery(self, source):
        """隔离授权校验：构造与 resolve 相同的 queued 绑定，不绕过 claim。"""
        source_result = self._request(source["request_id"])["result"]
        conn = self._connect()
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, source["task_id"])
                now = util.now_iso()
                fields = core.clear_block_fields()
                fields.update(
                    sent=0, send_state="not_sent", sent_at=None, lock_retained=0,
                    ui_evidence=None, send_evidence=None,
                    transition_started_at=None, transition_verified_at=None,
                    dispatch_lease_expires_at=None)
                core.transition_task(conn, task, "ready", now, "user", **fields)
                request, created = db.create_request(
                    conn, now, "dispatch", "dispatch:" + source["task_id"],
                    task_id=source["task_id"], requested_by="user")
                self.assertTrue(created)
                recovery = {
                    "identity": core.dispatch_recovery_identity(task),
                    "source_run_id": source["run_id"],
                    "source_request_id": source["request_id"],
                    "source_request_result": source_result,
                    "request_id": request["request_id"],
                    "state": "queued",
                }
                resolution = {
                    "action": "resume", "actor": "user", "at": now,
                    "dispatch_recovery": recovery,
                }
                db.update_task(
                    conn, source["task_id"], now, resolution=util.jdump(resolution))
                return request["request_id"]
        finally:
            conn.close()

    def _pending_recovery_after_remote_precheck_failure(self):
        source = self._blocked_unknown()
        resumed = self.call("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(resumed["task_status"], "ready")
        request_id = resumed["request"]["request_id"]
        with patch.object(dispatch_validate.remote, "get_task",
                          side_effect=bridge.BridgeError("fixture remote precheck unavailable")):
            claim = self.call("claim", {
                "stage": "dispatch", "executor": "kaneo-dispatch",
                "request_id": request_id})
        self.assertFalse(claim["claimed"], claim)
        self.assertEqual(claim["code"], "remote_unavailable")
        failed = self._request(request_id)
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["run_id"])
        self.assertEqual(failed["attempt_no"], 0)
        self.assertEqual(failed["result"]["outcome"], "failed")
        attempts = self._attempts(request_id=request_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertIsNone(attempts[0]["started_at"])
        self.assertIsNone(attempts[0]["run_id"])
        self.assertIsNotNone(attempts[0]["finished_at"])
        view = self._task_row(source["task_id"])
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["send_state"], "not_sent")
        recovery = self._recovery(source["task_id"])
        self.assertEqual(recovery["state"], "queued")
        self.assertEqual(recovery["request_id"], request_id)
        self.assertEqual(recovery["retry_pending_request_id"], request_id)
        self.assertEqual(recovery["retry_request_result"], failed["result"])
        conn = self._connect()
        try:
            self.assertFalse(core.dispatch_recovery_matches(
                conn, view, recovery, request_id))
        finally:
            conn.close()
        self.assertEqual(self._settings()["dispatch_serial"], "")
        events = self._events(source["task_id"], "dispatch_recovery_retry_pending")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["request_id"], request_id)
        return source, request_id, recovery

    def _tamper_recovery(self, task_id, *, identity_index=None, request_id=None,
                         run_id=None, state=None):
        conn = self._connect()
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                resolution = util.jload(task["resolution"])
                recovery = dict(resolution["dispatch_recovery"])
                if identity_index is not None:
                    recovery["identity"] = list(recovery["identity"])
                    recovery["identity"][identity_index] = "tampered"
                if request_id is not None:
                    recovery["request_id"] = request_id
                if run_id is not None:
                    recovery["run_id"] = run_id
                if state is not None:
                    recovery["state"] = state
                resolution["dispatch_recovery"] = recovery
                db.update_task(
                    conn, task_id, util.now_iso(), resolution=util.jdump(resolution))
        finally:
            conn.close()

    def _reset_ready_preserving_resolution(self, task_id):
        conn = self._connect()
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, task_id)
                fields = core.clear_block_fields()
                fields.update(
                    sent=0, send_state="not_sent", sent_at=None, lock_retained=0,
                    run_id=None, session_marker=None, session_id=None,
                    session_title=None, send_evidence=None, ui_evidence=None,
                    transition_started_at=None, transition_verified_at=None,
                    dispatch_lease_expires_at=None, original_todo_slug=None)
                core.transition_task(conn, task, "ready", util.now_iso(), "user", **fields)
        finally:
            conn.close()

    def _complete_normal_dispatch(self, task_id, request_id):
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertTrue(claim["claimed"], claim)
        self.assertEqual(claim["request_id"], request_id)
        plan_path = self.task(task_id)["plan_path"]
        self._prepare_claim(claim, plan_path)
        sent = self.call("dispatch-begin-send", {
            "task_id": task_id, "run_id": claim["run_id"]})
        self.assertEqual(sent["status"], "submitting")
        confirmed = self._confirm_codex_without_worker(claim, plan_path)
        self.assertEqual(confirmed["status"], "running")
        return claim

    # ------------------------------------------------------------ begin-send outcomes

    def test_remote_already_in_progress_skips_second_write(self):
        info = self._prepared()
        self.fake.tasks[info["task_id"]]["status"] = "in-progress"
        sent = self.call("dispatch-begin-send", {
            "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(self._puts(), [])
        view = self._task_row(info["task_id"])
        self.assertEqual(view["status"], "submitting")
        self.assertIsNotNone(view["transition_verified_at"])

    def test_single_read_blip_is_absorbed(self):
        info = self._prepared()
        self.endpoint.fail_reads_after_write = True
        self.endpoint.read_limit = 1
        sent = self.call("dispatch-begin-send", {
            "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(len(self._puts()), 1)
        self.assertEqual(self.task(info["task_id"])["status"], "submitting")

    def test_failed_write_with_untouched_remote_is_not_sent(self):
        info = self._prepared()
        self.endpoint.fail_writes = True
        exc = self.call_err("dispatch-begin-send", {
            "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(exc.code, "remote_unavailable")
        self.endpoint.fail_writes = False
        self._assert_not_sent_block(info["task_id"])
        self._assert_terminal_failure(info["request_id"], info["run_id"], "not_sent")
        self.assertEqual(self._puts(), [])
        self.assertIn("retry", self.task(info["task_id"])["allowed_actions"])

    def test_unreadable_verify_stays_unknown(self):
        info = self._blocked_unknown()
        view = self.task(info["task_id"])
        self.assertIn("restore", view["allowed_actions"])
        self.assertIn("mark_sent", view["allowed_actions"])
        failed = [event for event in self._events(info["task_id"], "dispatch_failed")
                  if event["request_id"] == info["request_id"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["detail"]["send_state"], "unknown")

    def test_begin_send_block_without_dispatch_fail_does_not_block_other_task(self):
        source = self._blocked_unknown()
        other = self.make_ready_task(task_id="taskotherdispatch0000001")
        other_id = other["remote"]["id"]
        self.enqueue_dispatch(other_id)
        other_request = self._dispatch_requests(other_id)[0]["request_id"]
        self._complete_normal_dispatch(other_id, other_request)
        self.assertEqual(self._request(other_request)["status"], "succeeded")
        self.assertEqual(self._request(source["request_id"])["status"], "failed")
        self.assertEqual(self.task(source["task_id"])["status"], "needs_user")

    # ------------------------------------------------------------ explicit recovery

    def test_explicit_resume_binds_source_and_reuses_remote_without_second_put(self):
        source = self._blocked_unknown()
        source_before = self._request(source["request_id"])
        resumed = self.call("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(resumed["task_status"], "ready")
        view = self._task_row(source["task_id"])
        self.assertEqual(view["send_state"], "not_sent")
        recovery = self._recovery(source["task_id"])
        self.assertEqual(recovery["state"], "queued")
        self.assertEqual(recovery["source_run_id"], source["run_id"])
        self.assertEqual(recovery["source_request_id"], source["request_id"])
        self.assertEqual(recovery["source_request_result"]["outcome"], "failed")
        self.assertEqual(recovery["source_request_result"]["send_state"], "unknown")
        self.assertNotEqual(recovery["request_id"], source["request_id"])
        self.assertEqual(recovery["identity"], core.dispatch_recovery_identity(view))
        queued = self._request(recovery["request_id"])
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["run_id"])
        self.assertEqual(self._request(source["request_id"]), source_before)
        self.assertEqual(len(self._puts()), 1)
        events = self._events(source["task_id"], "dispatch_recovery_queued")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["source_request_id"], source["request_id"])

        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": recovery["request_id"],
        })
        self.assertTrue(claim["claimed"], claim)
        self.assertNotEqual(claim["run_id"], source["run_id"])
        self.assertEqual(self._recovery(source["task_id"])["state"], "claimed")
        self.assertEqual(self._recovery(source["task_id"])["run_id"], claim["run_id"])
        self._prepare_claim(claim, source["plan_path"])
        sent = self.call("dispatch-begin-send", {
            "task_id": source["task_id"], "run_id": claim["run_id"]})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(len(self._puts()), 1)
        self.assertEqual(self._recovery(source["task_id"])["state"], "transitioned")
        self.assertEqual(self._request(source["request_id"]), source_before)
        self.assertEqual(self._request(recovery["request_id"])["status"], "claimed")
        self.assertEqual(len(self._attempts(source["task_id"], dispatch_only=True)), 2)
        self.assertEqual(self._settings()["dispatch_serial"], claim["run_id"])

        self._start_and_confirm_mocked_codex(claim, recovery["request_id"])
        self.assertEqual(self._recovery(source["task_id"])["state"], "completed")
        self.assertEqual(self._request(source["request_id"]), source_before)
        self.assertEqual(self._request(recovery["request_id"])["status"], "succeeded")
        attempts = self._attempts(source["task_id"], dispatch_only=True)
        self.assertEqual([row["outcome"] for row in attempts], ["failed", "succeeded"])
        self.assertEqual(self._settings()["dispatch_serial"], "")

    def test_explicit_resume_closes_legacy_claimed_source_and_reuses_remote(self):
        source = self._prepared(automation=True)
        self._legacy_block(source, "unknown")
        self.fake.tasks[source["task_id"]]["status"] = "in-progress"
        resumed = self.call("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(resumed["task_status"], "ready")
        old = self._request(source["request_id"])
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(old["result"]["via"], "resolve resume")
        self.assertEqual(self._attempts(request_id=source["request_id"])[0]["outcome"], "cancelled")
        recovery = self._recovery(source["task_id"])
        self.assertEqual(recovery["state"], "queued")
        self.assertEqual(recovery["source_request_id"], source["request_id"])
        self.assertNotEqual(recovery["request_id"], source["request_id"])
        conn = self._connect()
        try:
            self.assertTrue(core.dispatch_recovery_source_matches(
                conn, db.require_task(conn, source["task_id"]), recovery))
        finally:
            conn.close()

        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": recovery["request_id"],
        })
        self.assertTrue(claim["claimed"], claim)
        self.assertNotEqual(claim["run_id"], source["run_id"])
        self._prepare_claim(claim, source["plan_path"])
        sent = self.call("dispatch-begin-send", {
            "task_id": source["task_id"], "run_id": claim["run_id"]})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(len(self._puts()), 0)
        self.assertEqual(self._recovery(source["task_id"])["state"], "transitioned")
        self.assertEqual(self._settings()["dispatch_serial"], claim["run_id"])

    def test_remote_precheck_failure_preserves_authorization_for_explicit_rebind(self):
        source, failed_request_id, _ = self._pending_recovery_after_remote_precheck_failure()
        task_id = source["task_id"]
        failed_before = self._request(failed_request_id)
        requests_before = self._dispatch_requests(task_id)
        recovery_before = self._recovery(task_id)

        with patch("kanpilot.automation_policy.automation_preflight", return_value=None), \
             patch("kanpilot.automation_policy.auto_confirm_skip_reason", return_value=None):
            automation = self.call_err("enqueue", {
                "task_id": task_id, "kind": "dispatch", "confirm_plan": True,
                "actor": "automation"})
        self.assertEqual(automation.code, "conflict")
        self.assertIn("用户显式确认", automation.message)
        self.assertEqual(self._dispatch_requests(task_id), requests_before)
        self.assertEqual(self._recovery(task_id), recovery_before)

        retry = self.enqueue_dispatch(task_id)
        self.assertFalse(retry["deduplicated"])
        request_id = retry["request"]["request_id"]
        self.assertNotEqual(request_id, failed_request_id)
        recovery = self._recovery(task_id)
        self.assertEqual(recovery["state"], "queued")
        self.assertEqual(recovery["request_id"], request_id)
        self.assertNotIn("retry_pending_request_id", recovery)
        self.assertNotIn("retry_request_result", recovery)
        conn = self._connect()
        try:
            self.assertTrue(core.dispatch_recovery_matches(
                conn, self._task_row(task_id), recovery, request_id))
        finally:
            conn.close()
        self.assertEqual(self._request(failed_request_id), failed_before)
        events = self._events(task_id, "dispatch_recovery_rebound")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["previous_request_id"], failed_request_id)

        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id})
        self.assertTrue(claim["claimed"], claim)
        self.assertNotEqual(claim["run_id"], source["run_id"])
        self._prepare_claim(claim, source["plan_path"])
        sent = self.call("dispatch-begin-send", {
            "task_id": task_id, "run_id": claim["run_id"]})
        self.assertEqual(sent["status"], "submitting")
        self.assertEqual(len(self._puts()), 1)
        self.assertEqual(self._recovery(task_id)["state"], "transitioned")
        self.assertEqual(self._request(failed_request_id), failed_before)

    def test_recovery_rebind_rejects_changed_source_result(self):
        source, failed_request_id, recovery = self._pending_recovery_after_remote_precheck_failure()
        conn = self._connect()
        try:
            with db.write_txn(conn):
                changed = dict(recovery["source_request_result"], reason="changed after authorization")
                conn.execute(
                    "UPDATE requests SET result_json=? WHERE request_id=?",
                    (util.jdump(changed), source["request_id"]))
        finally:
            conn.close()
        requests_before = self._dispatch_requests(source["task_id"])
        exc = self.call_err("enqueue", {
            "task_id": source["task_id"], "kind": "dispatch", "confirm_plan": True})
        self.assertEqual(exc.code, "conflict")
        self.assertEqual(self._dispatch_requests(source["task_id"]), requests_before)
        self.assertEqual(self._recovery(source["task_id"]), recovery)
        self.assertEqual(self._request(failed_request_id)["status"], "failed")

    def test_recovery_rebind_rejects_original_round_start(self):
        source, failed_request_id, recovery = self._pending_recovery_after_remote_precheck_failure()
        conn = self._connect()
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE execution_rounds SET started_at=? WHERE task_id=? AND run_id=?",
                    (util.now_iso(), source["task_id"], source["run_id"]))
        finally:
            conn.close()
        requests_before = self._dispatch_requests(source["task_id"])
        exc = self.call_err("enqueue", {
            "task_id": source["task_id"], "kind": "dispatch", "confirm_plan": True})
        self.assertEqual(exc.code, "conflict")
        self.assertEqual(self._dispatch_requests(source["task_id"]), requests_before)
        self.assertEqual(self._recovery(source["task_id"]), recovery)
        self.assertEqual(self._request(failed_request_id)["status"], "failed")

    def test_recovery_rebind_rejects_unreadable_ui_evidence(self):
        source, failed_request_id, recovery = self._pending_recovery_after_remote_precheck_failure()
        conn = self._connect()
        try:
            with db.write_txn(conn):
                db.update_task(
                    conn, source["task_id"], util.now_iso(), ui_evidence="{not-json")
        finally:
            conn.close()
        requests_before = self._dispatch_requests(source["task_id"])
        exc = self.call_err("enqueue", {
            "task_id": source["task_id"], "kind": "dispatch", "confirm_plan": True})
        self.assertEqual(exc.code, "conflict")
        self.assertEqual(self._dispatch_requests(source["task_id"]), requests_before)
        self.assertEqual(self._recovery(source["task_id"]), recovery)
        self.assertEqual(self._request(failed_request_id)["status"], "failed")

    def test_mocked_recovery_start_and_confirm_completes_authorization(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertTrue(claim["claimed"], claim)
        self._prepare_claim(claim, source["plan_path"])
        self.call("dispatch-begin-send", {
            "task_id": source["task_id"], "run_id": claim["run_id"]})
        self.assertEqual(len(self._puts()), 1)
        self.assertEqual(self._recovery(source["task_id"])["state"], "transitioned")

        self._start_and_confirm_mocked_codex(claim, request_id)
        self.assertEqual(self._recovery(source["task_id"])["state"], "completed")
        self.assertEqual(self._request(request_id)["status"], "succeeded")
        self.assertEqual(self._request(source["request_id"])["status"], "failed")
        attempts = self._attempts(source["task_id"], dispatch_only=True)
        self.assertEqual([row["outcome"] for row in attempts], ["failed", "succeeded"])
        self.assertEqual(self._settings()["dispatch_serial"], "")

    # ------------------------------------------------------------ not-sent automation/retry

    def test_auto_dispatch_unknown_fail_preserves_not_sent_and_retry_releases_stage(self):
        fixture = self.make_ready_task(task_id="taskautonotsent00000001")
        task_id = fixture["remote"]["id"]
        self.seed_auto_confirmation(task_id)
        self.enqueue_dispatch(task_id)
        self.endpoint.fail_writes = True
        records = []
        fail_payloads = []
        real_fail = auto_dispatch._fail

        def record_fail(controller, send_state, reason):
            fail_payloads.append((send_state, reason))
            return real_fail(controller, send_state, reason)

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        with patch.object(auto_dispatch, "_fail", side_effect=record_fail):
            exit_code = auto_dispatch.run_stage(
                "dispatch", conn=conn, db_path=self.db_path, timeout=600,
                log_path=self.root / "auto-dispatch.jsonl",
                record_run=lambda **fields: records.append(fields),
                sleep=lambda _seconds: None)
        self.endpoint.fail_writes = False
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fail_payloads), 1)
        self.assertEqual(fail_payloads[0][0], "unknown")
        self.assertTrue(fail_payloads[0][1].startswith("remote_unavailable:"))
        self.assertEqual([row["exit_code"] for row in records if "exit_code" in row], [0])
        self._assert_not_sent_block(task_id)
        requests = self._dispatch_requests(task_id)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "failed")
        self._assert_terminal_failure(requests[0]["request_id"], requests[0]["run_id"], "not_sent")
        run_id = requests[0]["run_id"]
        before_attempts = self._attempts(task_id, dispatch_only=True)
        before_events = self._events(task_id, "dispatch_failed")
        for _ in range(2):
            duplicate = self.call("dispatch-fail", {
                "task_id": task_id, "run_id": run_id,
                "send_state": "unknown", "reason": "duplicate unknown report",
            })
            self.assertTrue(duplicate["already_recorded"])
            self.assertEqual(duplicate["send_state"], "not_sent")
        self.assertEqual(self._attempts(task_id, dispatch_only=True), before_attempts)
        self.assertEqual(self._events(task_id, "dispatch_failed"), before_events)
        self.assertEqual(self._request(requests[0]["request_id"]), requests[0])
        self.assertEqual(self.task(task_id)["failure_reason"], requests[0]["result"]["reason"])

        self.call("resolve", {"task_id": task_id, "action": "retry", "actor": "user"})
        retry = self._dispatch_requests(task_id)[1]
        self.assertEqual(retry["status"], "queued")
        self.assertIsNone(self._recovery(task_id))
        other = self.make_ready_task(task_id="taskautonotsent00000002")
        other_id = other["remote"]["id"]
        self.enqueue_dispatch(other_id)
        other_request = self._dispatch_requests(other_id)[0]["request_id"]
        retry_claim = self._complete_normal_dispatch(task_id, retry["request_id"])
        self.assertEqual(self._request(retry["request_id"])["status"], "succeeded")
        self.assertEqual(self._settings()["dispatch_serial"], "")
        other_claim = self._complete_normal_dispatch(other_id, other_request)
        self.assertNotEqual(other_claim["request_id"], retry_claim["request_id"])
        self.assertEqual(self._request(other_request)["status"], "succeeded")
        self.assertEqual(len(self._puts()), 2)
        self.assertEqual(len(self._attempts(task_id, dispatch_only=True)), 2)
        self.assertEqual(len(self._attempts(other_id, dispatch_only=True)), 1)

    def test_duplicate_fail_closes_legacy_claimed_request_without_releasing_other_serial(self):
        info = self._prepared()
        self._legacy_block(info, "unknown")
        conn = self._connect()
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "dispatch_serial", "other-run")
        finally:
            conn.close()
        before_attempts = self._attempts(request_id=info["request_id"])
        self.assertEqual(before_attempts[0]["outcome"], "claimed")
        result = self.call("dispatch-fail", {
            "task_id": info["task_id"], "run_id": info["run_id"],
            "send_state": "not_sent", "reason": "late conflicting report"})
        self.assertTrue(result["already_recorded"])
        self.assertEqual(result["send_state"], "unknown")
        request = self._request(info["request_id"])
        self.assertEqual(request["status"], "failed")
        self.assertEqual(request["result"]["send_state"], "unknown")
        self.assertEqual(request["result"]["run_id"], info["run_id"])
        self.assertEqual(request["result"]["stage"], "prepared")
        self.assertEqual(request["result"]["failure_stage"], "prepared")
        attempts = self._attempts(request_id=info["request_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertEqual(attempts[0]["send_state"], "unknown")
        self.assertIsNotNone(attempts[0]["finished_at"])
        self.assertEqual(self._settings()["dispatch_serial"], "other-run")
        self.call("dispatch-fail", {
            "task_id": info["task_id"], "run_id": info["run_id"],
            "send_state": "unknown", "reason": "duplicate"})
        self.assertEqual(self._request(info["request_id"]), request)
        self.assertEqual(self._attempts(request_id=info["request_id"]), attempts)
        self.assertEqual(self._settings()["dispatch_serial"], "other-run")

    def test_retry_closes_legacy_claimed_not_sent_request_before_new_claim(self):
        info = self._prepared()
        self._legacy_block(info, "not_sent")
        self.assertEqual(self._request(info["request_id"])["status"], "claimed")
        self.assertEqual(self._settings()["dispatch_serial"], info["run_id"])
        self.call("resolve", {
            "task_id": info["task_id"], "action": "retry", "actor": "user"})
        old = self._request(info["request_id"])
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual(old["result"]["via"], "resolve retry")
        self.assertEqual(self._attempts(request_id=info["request_id"])[0]["outcome"], "cancelled")
        self.assertEqual(self._settings()["dispatch_serial"], "")
        requests = self._dispatch_requests(info["task_id"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["status"], "queued")
        self._complete_normal_dispatch(info["task_id"], requests[1]["request_id"])
        self.assertEqual(self._request(requests[1]["request_id"])["status"], "succeeded")

    # ------------------------------------------------------------ authorization guards

    def test_ordinary_in_progress_without_recovery_is_rejected(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.fake.tasks[task_id]["status"] = "in-progress"
        self.enqueue_dispatch(task_id)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["reason"], "guard_failed")
        self.assertEqual(claim["code"], "validation_failed")
        request = self._dispatch_requests(task_id)[0]
        self.assertEqual(request["status"], "failed")
        self.assertEqual(request["result"]["outcome"], "failed")
        attempts = self._attempts(request_id=request["request_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertIsNone(attempts[0]["run_id"])
        self.assertEqual(self.task(task_id)["status"], "needs_user")
        self.assertEqual(self._puts(), [])

    def test_tampered_identity_cannot_authorize_or_poison_later_normal_cycle(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        self._tamper_recovery(source["task_id"], identity_index=0)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(self._request(source["request_id"])["status"], "failed")
        self.assertEqual(self._attempts(task_id=source["task_id"], request_id=request_id), [])
        self.assertEqual(len(self._puts()), 1)

        self.fake.tasks[source["task_id"]]["status"] = "to-do"
        self._reset_ready_preserving_resolution(source["task_id"])
        self.enqueue_dispatch(source["task_id"])
        normal = self._dispatch_requests(source["task_id"])[-1]
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        claim = self._complete_normal_dispatch(source["task_id"], normal["request_id"])
        self.assertEqual(self._request(normal["request_id"])["status"], "succeeded")
        self.assertEqual(self.task(source["task_id"])["status"], "running")
        self.assertEqual(len(self._puts()), 2)
        self.assertEqual(claim["context"]["backend"], "codex")

    def test_wrong_recovery_request_cannot_authorize(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        self._tamper_recovery(source["task_id"], request_id=source["request_id"])
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(self._request(source["request_id"])["status"], "failed")
        failed_events = self._events(source["task_id"], "dispatch_recovery_failed")
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["request_id"], source["request_id"])
        self.assertEqual(len(self._puts()), 1)

    def test_recovery_identity_binds_every_registered_field(self):
        source = self._blocked_unknown()
        source_view = self._task_row(source["task_id"])
        request_id = self._seed_queued_recovery(source)
        conn = self._connect()
        try:
            task = db.require_task(conn, source["task_id"])
            recovery = core.dispatch_recovery(task)
            self.assertTrue(core.dispatch_recovery_matches(
                conn, task, recovery, request_id))
            for field in core._DISPATCH_RECOVERY_IDENTITY:
                with self.subTest(field=field):
                    changed = dict(task)
                    changed[field] = "tampered-" + field
                    self.assertFalse(core.dispatch_recovery_matches(
                        conn, changed, recovery, request_id))
            self.assertTrue(cmd_queue.dispatch_recovery_candidate(conn, source_view))
            for field in (
                    "project_id", "repository", "plan_path", "plan_sha256",
                    "plan_generated_at", "source_path", "source_sha256",
                    "requirements_sha256"):
                with self.subTest(missing=field):
                    changed = dict(source_view)
                    changed[field] = None
                    self.assertFalse(cmd_queue.dispatch_recovery_candidate(conn, changed))
        finally:
            conn.close()

    def test_guard_failure_does_not_clobber_recovery_resolution_changed_during_check(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)

        def guard_then_replace(task, kind):
            conn = self._connect()
            try:
                with db.write_txn(conn):
                    db.update_task(conn, source["task_id"], util.now_iso(),
                                   resolution=util.jdump({"marker": "newer-resolution"}))
            finally:
                conn.close()
            return "guard changed during check"

        with patch.object(core, "guard_check", side_effect=guard_then_replace):
            claim = self.call("claim", {
                "stage": "dispatch", "executor": "kaneo-dispatch",
                "request_id": request_id})
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["reason"], "guard_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(util.jload(self._task_row(source["task_id"])["resolution"]),
                         {"marker": "newer-resolution"})

    def test_queued_recovery_rejects_source_run_change(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        conn = self._connect()
        try:
            task = db.require_task(conn, source["task_id"])
            recovery = core.dispatch_recovery(task)
            self.assertTrue(core.dispatch_recovery_matches(
                conn, task, recovery, request_id))
            changed = dict(task, run_id="replacement-source-run")
            self.assertFalse(core.dispatch_recovery_matches(
                conn, changed, recovery, request_id))
            with db.write_txn(conn):
                db.update_task(conn, source["task_id"], util.now_iso(),
                               run_id="replacement-source-run")
        finally:
            conn.close()
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id})
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(len(self._puts()), 1)

    def test_queued_recovery_rejects_source_request_change(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        conn = self._connect()
        try:
            task = db.require_task(conn, source["task_id"])
            recovery = core.dispatch_recovery(task)
            self.assertTrue(core.dispatch_recovery_matches(
                conn, task, recovery, request_id))
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE requests SET run_id=? WHERE request_id=?",
                    ("replacement-source-request-run", source["request_id"]),
                )
            self.assertFalse(core.dispatch_recovery_matches(
                conn, task, recovery, request_id))
        finally:
            conn.close()
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id})
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(len(self._puts()), 1)

    def test_active_recovery_rejects_remote_status_drift_instead_of_normal_dispatch(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        self.fake.tasks[source["task_id"]]["status"] = "to-do"
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id})
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(self.task(source["task_id"])["status"], "needs_user")
        self.assertEqual(len(self._puts()), 1)

    def _assert_resume_rejects_remote_change(self, field, value, expected_code="conflict"):
        source = self._blocked_unknown()
        real_get = remote.get_task

        def changed_remote(client, task_id):
            live = real_get(client, task_id)
            if field == "requirements_sha256":
                live[field] = value
            else:
                live["task"][field] = value
            return live

        with patch.object(remote, "get_task", side_effect=changed_remote):
            exc = self.call_err("resolve", {
                "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(exc.code, expected_code)
        self.assertEqual(self.task(source["task_id"])["status"], "needs_user")
        self.assertEqual(len(self._dispatch_requests(source["task_id"])), 1)
        self.assertEqual(self._events(source["task_id"], "dispatch_recovery_queued"), [])

    def test_resume_rejects_remote_task_identity_mismatch(self):
        self._assert_resume_rejects_remote_change("id", "foreign-task")

    def test_resume_rejects_remote_project_identity_mismatch(self):
        self._assert_resume_rejects_remote_change(
            "projectId", "foreign-project", expected_code="remote_conflict")

    def test_resume_rejects_remote_requirements_change(self):
        self._assert_resume_rejects_remote_change("requirements_sha256", "foreign-requirements")

    def test_recovery_claim_rejects_remote_task_identity_mismatch(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        real_get = remote.get_task

        def foreign_task(client, task_id):
            live = real_get(client, task_id)
            live["task"]["id"] = "foreign-task"
            return live

        with patch.object(remote, "get_task", side_effect=foreign_task):
            claim = self.call("claim", {
                "stage": "dispatch", "executor": "kaneo-dispatch",
                "request_id": request_id,
            })
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        attempts = self._attempts(request_id=request_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertIsNone(attempts[0]["run_id"])
        self.assertEqual(self._settings()["dispatch_serial"], "")
        self.assertEqual(len(self._puts()), 1)

    def test_authorization_removed_after_validation_cannot_claim(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        real_validate = dispatch_validate.validate_dispatch

        def validate_then_remove(conn, task, now):
            checked = real_validate(conn, task, now)
            race_conn = self._connect()
            try:
                with db.write_txn(race_conn):
                    current = db.require_task(race_conn, task["task_id"])
                    resolution = util.jload(current["resolution"])
                    resolution.pop("dispatch_recovery", None)
                    db.update_task(
                        race_conn, task["task_id"], util.now_iso(),
                        resolution=util.jdump(resolution))
            finally:
                race_conn.close()
            return checked

        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        service.ping.return_value = None
        with patch.object(
                dispatch_validate.mcp_client.MCPClient, "from_server_file",
                return_value=service), patch.object(
                dispatch_validate, "validate_dispatch", side_effect=validate_then_remove):
            claim = self.call("claim", {
                "stage": "dispatch", "executor": "kaneo-dispatch",
                "request_id": request_id,
            })
        self.assertFalse(claim["claimed"])
        self.assertEqual(claim["code"], "validation_failed")
        self.assertIn("授权已变化", claim["detail"])
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertIsNone(self._recovery(source["task_id"]))
        attempts = self._attempts(request_id=request_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertIsNone(attempts[0]["run_id"])
        self.assertEqual(self._settings()["dispatch_serial"], "")

    def _assert_historical_recovery_rejected_then_normal_cycle(self, state, run_id=None):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        self._tamper_recovery(source["task_id"], state=state, run_id=run_id)
        rejected = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertFalse(rejected["claimed"])
        self.assertEqual(rejected["code"], "validation_failed")
        self.assertEqual(self._request(request_id)["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(self._request(source["request_id"])["status"], "failed")
        self.assertEqual(len(self._puts()), 1)

        self.fake.tasks[source["task_id"]]["status"] = "to-do"
        self._reset_ready_preserving_resolution(source["task_id"])
        self.enqueue_dispatch(source["task_id"])
        normal = self._dispatch_requests(source["task_id"])[-1]
        self._complete_normal_dispatch(source["task_id"], normal["request_id"])
        self.assertEqual(self._request(normal["request_id"])["status"], "succeeded")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(len(self._puts()), 2)
        attempts = self._attempts(source["task_id"], dispatch_only=True)
        self.assertEqual([row["outcome"] for row in attempts], ["failed", "succeeded"])
        self.assertEqual(self._settings()["dispatch_serial"], "")

    def test_failed_historical_recovery_cannot_authorize_or_poison_normal_cycle(self):
        self._assert_historical_recovery_rejected_then_normal_cycle("failed")

    def test_consumed_historical_recovery_cannot_authorize_or_poison_normal_cycle(self):
        self._assert_historical_recovery_rejected_then_normal_cycle(
            "claimed", run_id="consumed-source-run")

    def test_wrong_recovery_run_cannot_write_transition(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertTrue(claim["claimed"], claim)
        self._prepare_claim(claim, source["plan_path"])
        self._tamper_recovery(source["task_id"], run_id="wrong-run")
        exc = self.call_err("dispatch-begin-send", {
            "task_id": source["task_id"], "run_id": claim["run_id"]})
        self.assertEqual(exc.code, "conflict")
        view = self._task_row(source["task_id"])
        self.assertEqual(view["status"], "prepared")
        self.assertIsNone(view["transition_started_at"])
        self.assertIsNone(view["transition_verified_at"])
        self.assertEqual(self._request(request_id)["status"], "claimed")
        self.assertEqual(self._attempts(request_id=request_id)[0]["outcome"], "claimed")
        self.assertEqual(self._settings()["dispatch_serial"], claim["run_id"])
        self.assertEqual(len(self._puts()), 1)
        self.assertEqual(self._events(source["task_id"], "kaneo_transition"), [])

    def test_completed_historical_recovery_neither_authorizes_nor_blocks_normal_cycle(self):
        source = self._blocked_unknown()
        request_id = self._seed_queued_recovery(source)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertTrue(claim["claimed"], claim)
        self._prepare_claim(claim, source["plan_path"])
        self.call("dispatch-begin-send", {
            "task_id": source["task_id"], "run_id": claim["run_id"]})
        self._confirm_codex_without_worker(claim, source["plan_path"], "recovery-session")
        self.assertEqual(self._recovery(source["task_id"])["state"], "completed")
        self.assertEqual(self._request(request_id)["status"], "succeeded")
        self.assertEqual(len(self._puts()), 1)

        self._reset_ready_preserving_resolution(source["task_id"])
        self.enqueue_dispatch(source["task_id"])
        historical = self._dispatch_requests(source["task_id"])[-1]
        rejected = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": historical["request_id"],
        })
        self.assertFalse(rejected["claimed"])
        self.assertEqual(rejected["code"], "validation_failed")
        self.assertEqual(self._request(historical["request_id"])["status"], "failed")
        self.assertEqual(self._recovery(source["task_id"])["state"], "completed")

        self.fake.tasks[source["task_id"]]["status"] = "to-do"
        self._reset_ready_preserving_resolution(source["task_id"])
        self.enqueue_dispatch(source["task_id"])
        normal = self._dispatch_requests(source["task_id"])[-1]
        self._complete_normal_dispatch(source["task_id"], normal["request_id"])
        self.assertEqual(self._request(normal["request_id"])["status"], "succeeded")
        self.assertEqual(self._recovery(source["task_id"])["state"], "completed")
        self.assertEqual(len(self._puts()), 2)

    # ------------------------------------------------------------ resume evidence gates

    def test_resume_action_tracks_dispatch_stage_and_unstarted_evidence(self):
        source = self._blocked_unknown()
        self.assertEqual(self._task_row(source["task_id"])["failure_stage"], "submitting")
        conn = self._connect()
        try:
            self.assertIn("resume", derive.allowed_actions(
                conn, db.require_task(conn, source["task_id"]), util.now_dt()))
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE execution_rounds SET started_at=? WHERE task_id=? AND run_id=?",
                    (util.now_iso(), source["task_id"], source["run_id"]))
            self.assertNotIn("resume", derive.allowed_actions(
                conn, db.require_task(conn, source["task_id"]), util.now_dt()))
        finally:
            conn.close()

    def test_resume_rejected_for_original_round_start_evidence(self):
        fields = (
            ("started_at", util.now_iso()), ("pid", 42424),
            ("session_id", "already-started"), ("session_title", "Already started"),
            ("save_state", "archived"))
        for index, (field, value) in enumerate(fields, 1):
            with self.subTest(field=field):
                source = self._blocked_unknown(task_id=f"taskresumestart{index:05d}")
                self._clear_prepare_evidence(source["task_id"])
                conn = self._connect()
                try:
                    with db.write_txn(conn):
                        conn.execute(
                            "UPDATE execution_rounds SET started_at=NULL,pid=NULL,"
                            "session_id=NULL,session_title=NULL WHERE task_id=? AND run_id=?",
                            (source["task_id"], source["run_id"]))
                        conn.execute(
                            f"UPDATE execution_rounds SET {field}=? WHERE task_id=? AND run_id=?",
                            (value, source["task_id"], source["run_id"]))
                finally:
                    conn.close()
                exc = self.call_err("resolve", {
                    "task_id": source["task_id"], "action": "resume", "actor": "user"})
                self.assertEqual(exc.code, "conflict")
                self.assertEqual(self.task(source["task_id"])["status"], "needs_user")
                self.assertEqual(len(self._dispatch_requests(source["task_id"])), 1)
                self.assertEqual(self._events(source["task_id"], "dispatch_recovery_queued"), [])

    def _assert_resume_rejected_for_task_evidence(self, field, value, task_id=None):
        source = self._blocked_unknown(task_id=task_id)
        self._clear_prepare_evidence(source["task_id"])
        conn = self._connect()
        try:
            with db.write_txn(conn):
                db.update_task(
                    conn, source["task_id"], util.now_iso(), **{field: value})
        finally:
            conn.close()
        exc = self.call_err("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(exc.code, "conflict")
        self.assertEqual(self.task(source["task_id"])["status"], "needs_user")
        self.assertEqual(len(self._dispatch_requests(source["task_id"])), 1)
        self.assertEqual(self._events(source["task_id"], "dispatch_recovery_queued"), [])

    def test_resume_rejected_for_send_evidence(self):
        for index, (field, value) in enumerate((
                ("send_evidence", {"message_visible": True}),
                ("sent_at", util.now_iso())), 1):
            with self.subTest(field=field):
                self._assert_resume_rejected_for_task_evidence(
                    field, value, task_id=f"taskresumeevidence{index:05d}")

    def test_resume_rejected_when_unknown_lock_evidence_is_missing(self):
        for index, field in enumerate(
                ("lock_retained", "manual_recovery_required"), 1):
            with self.subTest(field=field):
                self._assert_resume_rejected_for_task_evidence(
                    field, 0, task_id=f"taskresumelock{index:05d}")

    def test_resume_rejected_for_visible_ui_evidence(self):
        self._assert_resume_rejected_for_task_evidence(
            "ui_evidence", {"message_visible": True})

    def test_resume_rejected_for_unreadable_ui_evidence(self):
        values = ("{not-json", "null", "[]", "false", "0", "\"\"")
        for index, value in enumerate(values, 1):
            with self.subTest(value=value):
                self._assert_resume_rejected_for_task_evidence(
                    "ui_evidence", value, task_id=f"taskresumeuibad{index:05d}")

    def test_resume_rejected_for_task_session_title(self):
        self._assert_resume_rejected_for_task_evidence(
            "session_title", "Unverified session")

    def test_resume_allows_unverified_transition_started_at(self):
        source = self._blocked_unknown()
        self.assertIsNotNone(self._task_row(source["task_id"])["transition_started_at"])
        resumed = self.call("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(resumed["task_status"], "ready")
        self.assertEqual(self._recovery(source["task_id"])["state"], "queued")

    def test_resume_rejected_for_transition_verified_at(self):
        self._assert_resume_rejected_for_task_evidence(
            "transition_verified_at", util.now_iso())

    def test_resume_rejected_when_source_request_evidence_is_missing(self):
        source = self._blocked_unknown()
        conn = self._connect()
        try:
            with db.write_txn(conn):
                db.update_request(conn, source["request_id"], util.now_iso(), run_id=None)
        finally:
            conn.close()
        exc = self.call_err("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(exc.code, "conflict")
        self.assertEqual(self.task(source["task_id"])["status"], "needs_user")
        self.assertEqual(len(self._dispatch_requests(source["task_id"])), 1)
        self.assertEqual(self._events(source["task_id"], "dispatch_recovery_queued"), [])

    def test_resume_rejected_when_remote_still_in_original_column(self):
        source = self._blocked_unknown()
        self._clear_prepare_evidence(source["task_id"])
        self.fake.tasks[source["task_id"]]["status"] = "to-do"
        exc = self.call_err("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        self.assertEqual(exc.code, "conflict")
        self.assertIn("In Progress", str(exc))
        self.assertEqual(self.task(source["task_id"])["status"], "needs_user")
        self.assertEqual(len(self._dispatch_requests(source["task_id"])), 1)

    # ------------------------------------------------------------ startup-intent races

    def _inject_transitioned_recovery(self, info):
        conn = self._connect()
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, info["task_id"])
                recovery = {
                    "identity": core.dispatch_recovery_identity(task),
                    "source_run_id": "source-run",
                    "source_request_id": "source-request",
                    "source_request_result": {"outcome": "failed", "send_state": "unknown"},
                    "request_id": info["request_id"],
                    "run_id": info["run_id"],
                    "state": "transitioned",
                }
                db.update_task(conn, info["task_id"], util.now_iso(), resolution=util.jdump({
                    "action": "resume", "actor": "user", "dispatch_recovery": recovery,
                }))
        finally:
            conn.close()

    def _assert_startup_race(self, backend, mutation):
        info = self._prepared(task_id="taskstartrace" + backend.replace("_", "") + "0001",
                              backend=backend)
        self.call("dispatch-begin-send", {
            "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(self.task(info["task_id"])["status"], "submitting")
        if mutation == "recovery":
            self._inject_transitioned_recovery(info)

        def check_then_mutate(conn, timeout=10, *, config_snapshot=None):
            race_conn = self._connect()
            try:
                with db.write_txn(race_conn):
                    if mutation == "status":
                        db.update_task(
                            race_conn, info["task_id"], util.now_iso(), status="prepared")
                    elif mutation == "transition":
                        db.update_task(
                            race_conn, info["task_id"], util.now_iso(),
                            transition_verified_at=None)
                    else:
                        task = db.require_task(race_conn, info["task_id"])
                        resolution = util.jload(task["resolution"])
                        recovery = dict(resolution["dispatch_recovery"])
                        recovery["identity"] = list(recovery["identity"])
                        recovery["identity"][0] = "changed-after-preflight"
                        resolution["dispatch_recovery"] = recovery
                        db.update_task(
                            race_conn, info["task_id"], util.now_iso(),
                            resolution=util.jdump(resolution))
            finally:
                race_conn.close()
            return True, None

        with patch.object(exec_config, "check_available", side_effect=check_then_mutate) as available, \
             patch.object(cmd_dispatch, "_require_session_naming_capability"), \
             patch.object(cmd_dispatch, "_ensure_worktree",
                          side_effect=AssertionError("worktree must not be reached")) as worktree, \
             patch.object(round_store, "mark_intent", wraps=round_store.mark_intent) as intent, \
             patch.object(cmd_dispatch.subprocess, "Popen") as popen:
            exc = self.call_err("dispatch-start", {
                "task_id": info["task_id"], "run_id": info["run_id"]})
        available.assert_called_once()
        intent.assert_not_called()
        popen.assert_not_called()
        worktree.assert_not_called()
        self.assertEqual(getattr(exc, "code", None), "conflict")
        round_conn = self._connect()
        try:
            row = round_store.require_round(
                round_conn, info["task_id"], info["run_id"])
            self.assertIsNone(row["started_at"])
            self.assertIsNone(row["pid"])
        finally:
            round_conn.close()
        self.assertEqual(self._settings()["dispatch_serial"], info["run_id"])

    def test_codex_start_rechecks_task_status_after_environment_check(self):
        self._assert_startup_race("codex", "status")

    def test_codex_start_rechecks_recovery_after_environment_check(self):
        self._assert_startup_race("codex", "recovery")

    def test_pi_desktop_start_rechecks_task_status_after_environment_check(self):
        self._assert_startup_race("pi_desktop", "status")

    def test_pi_desktop_start_rechecks_recovery_after_environment_check(self):
        self._assert_startup_race("pi_desktop", "recovery")

    def test_codex_start_rechecks_transition_after_environment_check(self):
        self._assert_startup_race("codex", "transition")

    def test_pi_desktop_start_rechecks_transition_after_environment_check(self):
        self._assert_startup_race("pi_desktop", "transition")

    # ------------------------------------------------------------ validation/I/O races

    def _install_newer_dispatch_owner(self, task_id, old_request_id):
        """模拟校验或远端 I/O 期间，另一写入者已失败旧请求并领取新请求。"""
        conn = self._connect()
        try:
            with db.write_txn(conn):
                now = util.now_iso()
                old = db.get_request(conn, old_request_id)
                core.finish_request(
                    conn, old, now, "failed", "failed",
                    result={"outcome": "failed", "reason": "superseded by newer owner"},
                    note="superseded by newer owner")
                new, created = db.create_request(
                    conn, now, "dispatch", "dispatch:" + task_id,
                    task_id=task_id, requested_by="kaneo-dispatch")
                self.assertTrue(created)
                run_id = cmd_exec._claim_row(
                    conn, new, db.require_task(conn, task_id),
                    "kaneo-dispatch", 180, util.now_dt())
                round_store.upsert_on_claim(
                    conn, task_id, run_id, {"backend": "codex"}, now, backend="codex")
                fields = core.clear_block_fields()
                fields.update(
                    status="dispatching", stage="dispatch", run_id=run_id,
                    session_marker=task_id + "/" + run_id, sent=0,
                    send_state="not_sent", original_todo_slug="to-do",
                    kaneo_status="to-do", ui_evidence=None, send_evidence=None,
                    transition_started_at=None, transition_verified_at=None,
                    sent_at=None, session_id=None, session_title=None,
                    dispatch_lease_expires_at=util.plus_seconds(180, util.now_dt()))
                db.update_task(conn, task_id, now, **fields)
                db.set_setting(conn, "dispatch_serial", run_id)
                return new["request_id"], run_id
        finally:
            conn.close()

    def _assert_newer_owner_untouched(self, task_id, old_request_id, new_request_id, new_run_id,
                                      remote_status, put_count):
        view = self._task_row(task_id)
        self.assertEqual(view["status"], "dispatching")
        self.assertEqual(view["run_id"], new_run_id)
        self.assertIsNone(view["transition_started_at"])
        self.assertIsNone(view["transition_verified_at"])
        self.assertEqual(self._request(old_request_id)["status"], "failed")
        self.assertEqual(self._request(new_request_id)["status"], "claimed")
        self.assertEqual(self._request(new_request_id)["run_id"], new_run_id)
        newer_attempts = self._attempts(request_id=new_request_id)
        self.assertEqual(len(newer_attempts), 1)
        self.assertEqual(newer_attempts[0]["outcome"], "claimed")
        self.assertEqual(newer_attempts[0]["run_id"], new_run_id)
        self.assertEqual(self._settings()["dispatch_serial"], new_run_id)
        self.assertEqual(self._settings()["lifecycle_owner"], "")
        self.assertEqual(self._events(task_id, "kaneo_transition"), [])
        self.assertEqual(self.fake.tasks[task_id]["status"], remote_status)
        self.assertEqual(len(self._puts()), put_count)

    def test_validation_to_claim_race_cannot_overwrite_or_close_newer_request(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.enqueue_dispatch(task_id)
        old_request_id = self._dispatch_requests(task_id)[0]["request_id"]
        real_validate = dispatch_validate.validate_dispatch
        replacement = {}

        def validate_then_replace(conn, task, now):
            checked = real_validate(conn, task, now)
            replacement["request_id"], replacement["run_id"] = \
                self._install_newer_dispatch_owner(task_id, old_request_id)
            return checked

        service = Mock()
        service.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}
        service.ping.return_value = None
        with patch.object(
                dispatch_validate.mcp_client.MCPClient, "from_server_file",
                return_value=service), patch.object(
                dispatch_validate, "validate_dispatch", side_effect=validate_then_replace):
            exc = self.call_err("claim", {
                "stage": "dispatch", "executor": "kaneo-dispatch",
                "request_id": old_request_id,
            })
        self.assertEqual(exc.code, "conflict")
        self._assert_newer_owner_untouched(
            task_id, old_request_id, replacement["request_id"], replacement["run_id"],
            "to-do", 0)

    def test_remote_readback_identity_mismatch_stays_unknown_and_closes_request(self):
        source = self._blocked_unknown()
        resumed = self.call("resolve", {
            "task_id": source["task_id"], "action": "resume", "actor": "user"})
        request_id = resumed["request"]["request_id"]
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch",
            "request_id": request_id,
        })
        self.assertTrue(claim["claimed"], claim)
        self._prepare_claim(claim, source["plan_path"])
        real_get = remote.get_task
        calls = []

        def foreign_readback(client, task_id):
            live = real_get(client, task_id)
            calls.append(task_id)
            if len(calls) == 2:
                live["task"]["id"] = "foreign-task"
            return live

        with patch.object(remote, "get_task", side_effect=foreign_readback):
            exc = self.call_err("dispatch-begin-send", {
                "task_id": source["task_id"], "run_id": claim["run_id"]})
        self.assertEqual(exc.code, "remote_conflict", str(exc))
        self.assertEqual(len(calls), 2)
        self._assert_unknown_block(source["task_id"])
        self._assert_terminal_failure(request_id, claim["run_id"], "unknown")
        self.assertEqual(self._recovery(source["task_id"])["state"], "failed")
        self.assertEqual(len(self._puts()), 1)

    def test_remote_precheck_race_cannot_write_transition_or_close_newer_request(self):
        info = self._prepared()
        real_get = remote.get_task
        replacement = {}

        def get_then_replace(client, task_id):
            live = real_get(client, task_id)
            replacement["request_id"], replacement["run_id"] = \
                self._install_newer_dispatch_owner(task_id, info["request_id"])
            return live

        with patch.object(remote, "get_task", side_effect=get_then_replace):
            exc = self.call_err("dispatch-begin-send", {
                "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(exc.code, "stale_run")
        self._assert_newer_owner_untouched(
            info["task_id"], info["request_id"], replacement["request_id"],
            replacement["run_id"], "to-do", 0)

    def test_remote_readback_race_cannot_write_transition_or_close_newer_request(self):
        info = self._prepared()
        real_get = remote.get_task
        calls = []
        replacement = {}

        def get_then_replace_on_readback(client, task_id):
            live = real_get(client, task_id)
            calls.append(task_id)
            if len(calls) == 2:
                replacement["request_id"], replacement["run_id"] = \
                    self._install_newer_dispatch_owner(task_id, info["request_id"])
            return live

        with patch.object(remote, "get_task", side_effect=get_then_replace_on_readback):
            exc = self.call_err("dispatch-begin-send", {
                "task_id": info["task_id"], "run_id": info["run_id"]})
        self.assertEqual(exc.code, "stale_run")
        self.assertEqual(len(calls), 2)
        self._assert_newer_owner_untouched(
            info["task_id"], info["request_id"], replacement["request_id"],
            replacement["run_id"], "in-progress", 1)


if __name__ == "__main__":
    unittest.main()
