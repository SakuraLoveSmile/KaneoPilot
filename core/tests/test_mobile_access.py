"""Isolated mobile security, current-identity and operation-receipt regression tests."""
import base64
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from kanpilot import cli, constants, db, mobile_access as mobile, util


@pytest.fixture
def mobile_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(tmp_path / "support"))
    monkeypatch.setenv("KANPILOT_REPORTS_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("KANEO_CREDENTIALS_PATH", str(tmp_path / "absent-credentials.json"))
    path = tmp_path / "state.db"
    monkeypatch.setenv("KANPILOT_DB", str(path))
    conn = db.connect(path)
    conn.close()
    return path


def run(path, name, data=None):
    return cli.run(name, data or {}, path)


def enable(path):
    return run(path, "set-mobile-access", {"enabled": True, "bind_host": "127.0.0.1", "port": 47831})


def pairing(path):
    result = run(path, "create-mobile-pairing")
    raw = parse_qs(urlparse(result["pairing_uri"]).query)["data"][0]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def pair(path):
    content = pairing(path)
    return request(path, "pair", {"pairing_token": content["pairing_token"], "device_name": "测试手机"})


def request(path, kind, data, token=None):
    value = {"kind": kind, "data": data}
    if token is not None:
        value["token"] = token
    return run(path, "mobile-request", value)


def query(path, device, name="overview", args=None):
    return request(path, "query", {"type": name, "args": args or {}}, device["device_token"])


def action(path, device, operation="op-1", paused=True, expected=False):
    return request(path, "action", {"action": "set-paused", "operation_id": operation,
                                    "args": {"paused": paused, "expected_paused": expected}}, device["device_token"])


def test_missing_and_old_database_never_created_or_migrated(tmp_path):
    missing = tmp_path / "missing" / "state.db"
    with pytest.raises(util.KanpilotError, match="已初始化"):
        run(missing, "mobile-status")
    assert not missing.parent.exists()
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_meta(version INTEGER)")
    connection.execute("INSERT INTO schema_meta VALUES(1)")
    connection.commit()
    connection.close()
    with pytest.raises(util.KanpilotError, match="版本"):
        run(path, "mobile-status")
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("schema_meta",)]
    connection.close()


def test_disabled_by_default_and_status_has_no_credentials(mobile_env):
    initial = run(mobile_env, "mobile-status")
    assert initial["enabled"] is False and initial["server_id"] is None
    enable(mobile_env)
    phone = pair(mobile_env)
    status = run(mobile_env, "mobile-status")
    encoded = json.dumps(status)
    assert phone["device_token"] not in encoded and "token_sha256" not in encoded
    assert status["devices"][0]["name"] == "测试手机"
    assert query(mobile_env, phone)["data"]["paused"]["global"] is False


def test_pairing_is_atomic_single_use_and_only_hashes_are_stored(mobile_env):
    enable(mobile_env)
    content = pairing(mobile_env)
    payload = {"pairing_token": content["pairing_token"], "device_name": "手机"}
    barrier = threading.Barrier(2)
    def attempt():
        barrier.wait()
        try:
            return request(mobile_env, "pair", payload)
        except util.KanpilotError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    accepted = [result for result in results if isinstance(result, dict)]
    assert len(accepted) == 1 and "unauthorized" in results
    conn = db.connect(mobile_env)
    stored = json.dumps([dict(row) for row in conn.execute("SELECT * FROM settings WHERE key LIKE 'mobile.%'")])
    conn.close()
    assert content["pairing_token"] not in stored
    assert accepted[0]["device_token"] not in stored


def test_expired_pairing_and_replaced_qr_cannot_pair(mobile_env):
    enable(mobile_env)
    old = pairing(mobile_env)
    current = pairing(mobile_env)
    with pytest.raises(util.KanpilotError) as exc:
        request(mobile_env, "pair", {"pairing_token": old["pairing_token"], "device_name": "手机"})
    assert exc.value.code == "unauthorized"
    conn = db.connect(mobile_env)
    data = db.get_setting(conn, "mobile.pairing")
    data["expires_at"] = util.plus_seconds(-1)
    db.set_setting(conn, "mobile.pairing", data)
    conn.close()
    with pytest.raises(util.KanpilotError) as exc:
        request(mobile_env, "pair", {"pairing_token": current["pairing_token"], "device_name": "手机"})
    assert exc.value.code == "unauthorized"


def test_revoke_blocks_queries_actions_and_receipts(mobile_env):
    enable(mobile_env)
    phone = pair(mobile_env)
    action(mobile_env, phone)
    run(mobile_env, "revoke-mobile-device", {"device_id": phone["device_id"]})
    for kind, data in [("query", {"type": "overview", "args": {}}),
                       ("operation", {"operation_id": "op-1"}),
                       ("action", {"action": "set-paused", "operation_id": "op-2", "args": {"paused": False, "expected_paused": True}})]:
        with pytest.raises(util.KanpilotError) as exc:
            request(mobile_env, kind, data, phone["device_token"])
        assert exc.value.code == "unauthorized"


def test_revocation_during_write_wait_is_checked_inside_transaction(mobile_env, monkeypatch):
    enable(mobile_env)
    phone = pair(mobile_env)
    spec, original = cli.COMMANDS["set-paused"]
    def revoke_before_handler(ctx, payload):
        connection = db.connect(mobile_env)
        devices = db.get_setting(connection, "mobile.devices")
        devices[phone["device_id"]]["revoked_at"] = util.now_iso()
        with db.write_txn(connection):
            db.set_setting(connection, "mobile.devices", devices)
        connection.close()
        return original(ctx, payload)
    monkeypatch.setitem(cli.COMMANDS, "set-paused", (spec, revoke_before_handler))
    with pytest.raises(util.KanpilotError) as exc:
        action(mobile_env, phone)
    assert exc.value.code == "unauthorized"
    assert run(mobile_env, "overview")["paused"]["global"] is False


@pytest.mark.parametrize("name,args", [("set-config", {}), ("overview", {"db": "/tmp/else.db"}),
    ("list-sessions", {"task_id": "t", "session_runs_root": "/tmp"}),
    ("get-plan", {"task_id": "t"}), ("list-events", {}),
    ("list-events", {"task_id": "t", "limit": 201}),
    ("list-tasks", {"save_state": "all"}), ("attention-list", {"include_historical": True})])
def test_query_whitelist_rejects_privilege_and_path_injection(mobile_env, name, args):
    enable(mobile_env)
    phone = pair(mobile_env)
    with pytest.raises(util.KanpilotError) as exc:
        query(mobile_env, phone, name, args)
    assert exc.value.code in {"bad_request", "forbidden"}


def test_projection_keeps_reading_text_but_removes_local_config():
    value = {"extra": {"provider": {"apiKey": "SECRET"}}, "config": {"token": "SECRET"},
             "title": "/tmp is mentioned in user title", "markdown": "Read /local/path.md", "content": "message",
             "tool": {"input": {"path": "/local"}, "output": "hello"},
             "source_path": "/local", "links": {"release_url": "https://github.com/a/b/releases/1", "plan_file": "/local"},
             "review_key": "review-key", "allowed_actions": ["trash", "confirm_dispatch", "answer_execution_questions"]}
    result = mobile.project(value)
    assert result["markdown"] == value["markdown"] and result["title"] == value["title"]
    assert result["tool"]["output"] == "hello"
    assert result["review_key"] == "review-key"
    assert result["links"] == {"release_url": "https://github.com/a/b/releases/1"}
    assert "SECRET" not in json.dumps(result) and "source_path" not in result
    assert result["allowed_actions"] == ["confirm-dispatch", "save-execution-answers", "submit-execution-answers"]


def test_lost_response_replay_and_concurrent_submission_execute_once(mobile_env):
    enable(mobile_env)
    phone = pair(mobile_env)
    barrier = threading.Barrier(2)
    def submit():
        barrier.wait()
        return action(mobile_env, phone)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert {result["status"] for result in results} <= {"accepted", "unknown"}
    receipt = request(mobile_env, "operation", {"operation_id": "op-1"}, phone["device_token"])
    assert receipt["status"] == "accepted"
    assert action(mobile_env, phone) == receipt
    conn = db.connect(mobile_env)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='paused'").fetchone()[0] == 1
    conn.close()
    with pytest.raises(util.KanpilotError) as exc:
        action(mobile_env, phone, paused=False, expected=True)
    assert exc.value.code == "conflict"


def test_unknown_operation_is_not_reexecuted_after_process_crash(mobile_env, monkeypatch):
    enable(mobile_env)
    phone = pair(mobile_env)
    spec, original = cli.COMMANDS["set-paused"]
    calls = []
    def died(ctx, payload):
        calls.append(True)
        raise KeyboardInterrupt()
    monkeypatch.setitem(cli.COMMANDS, "set-paused", (spec, died))
    with pytest.raises(KeyboardInterrupt):
        action(mobile_env, phone)
    monkeypatch.setitem(cli.COMMANDS, "set-paused", (spec, original))
    result = action(mobile_env, phone)
    assert result["status"] == "unknown" and len(calls) == 1
    assert run(mobile_env, "overview")["paused"]["global"] is False


def test_receipts_are_device_scoped_and_old_pause_cannot_overwrite(mobile_env):
    enable(mobile_env)
    first, second = pair(mobile_env), pair(mobile_env)
    assert action(mobile_env, first)["status"] == "accepted"
    with pytest.raises(util.KanpilotError) as exc:
        request(mobile_env, "operation", {"operation_id": "op-1"}, second["device_token"])
    assert exc.value.code == "not_found"
    assert action(mobile_env, second)["status"] == "rejected"
    assert run(mobile_env, "overview")["paused"]["global"] is True


def make_review(path, markdown=b"# Review\nall good"):
    root = constants.review_reports_root()
    root.mkdir(parents=True, exist_ok=True)
    report = root / "review.md"
    report.write_bytes(markdown)
    digest = hashlib.sha256(markdown).hexdigest()
    conn = db.connect(path)
    now = util.now_iso()
    conn.execute("INSERT INTO tasks(task_id,project_id,project_name,title,stage,status,run_id,review_key,review_report_path,review_report_sha256,extra_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("t", "p", "test", "test", "review", "in_review", "run-1", "review-1", str(report), digest,
                  util.jdump({"review_history": [{"execution_run_id": "run-1", "review_key": "review-1", "report_path": str(report), "report_sha256": digest, "conclusion": "pass"}]}), now, now))
    conn.close()
    return report, {"task_id": "t", "review_key": "review-1", "expected_execution_run_id": "run-1", "expected_report_sha256": digest}


def test_review_current_identity_hash_utf8_and_symlink_protection(mobile_env):
    report, args = make_review(mobile_env)
    assert run(mobile_env, "get-review-report", args)["markdown"] == "# Review\nall good"
    for key in ("review_key", "expected_execution_run_id", "expected_report_sha256"):
        with pytest.raises(util.KanpilotError) as exc:
            run(mobile_env, "get-review-report", dict(args, **{key: "stale"}))
        assert exc.value.code == "conflict"
    report.write_text("changed")
    with pytest.raises(util.KanpilotError) as exc:
        run(mobile_env, "get-review-report", args)
    assert exc.value.code == "conflict"
    target = report.with_name("target.md")
    report.rename(target)
    report.symlink_to(target)
    with pytest.raises(util.KanpilotError) as exc:
        run(mobile_env, "get-review-report", args)
    assert exc.value.code == "not_found"


def test_plan_hash_and_allowed_action_cannot_be_bypassed(mobile_env):
    enable(mobile_env)
    phone = pair(mobile_env)
    conn = db.connect(mobile_env)
    now = util.now_iso()
    conn.execute("INSERT INTO tasks(task_id,project_id,project_name,title,stage,status,plan_sha256,extra_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ("t", "p", "test", "test", "dispatch", "ready", "current", '{"plan_execution_hold":true}', now, now))
    conn.close()
    result = request(mobile_env, "action", {"action": "confirm-dispatch", "operation_id": "confirm", "args": {"task_id": "t", "expected_plan_sha256": "stale"}}, phone["device_token"])
    assert result["status"] == "rejected"
    conn = db.connect(mobile_env)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    conn.close()
    with pytest.raises(util.KanpilotError) as exc:
        request(mobile_env, "action", {"action": "submit-execution-answers", "operation_id": "bad", "args": {"task_id": "t", "run_id": "r", "request_id": "r", "questions_sha256": "h", "answers": [], "skip_all": True}}, phone["device_token"])
    assert exc.value.code == "bad_request"


def test_can_disable_after_network_address_disappears(mobile_env, monkeypatch):
    enable(mobile_env)
    phone = pair(mobile_env)
    monkeypatch.setattr(mobile, "local_addresses", lambda: [])
    assert run(mobile_env, "set-mobile-access", {"enabled": False})["enabled"] is False
    with pytest.raises(util.KanpilotError) as exc:
        query(mobile_env, phone)
    assert exc.value.code == "unauthorized"


def test_projection_preserves_connection_questions_and_message_protocol():
    raw = {"connection": {"state": "unavailable", "detail": "暂时离线", "poll_seconds": 5, "subscription": False,
                          "token": "SECRET", "path": "/local", "config": {"apiKey": "SECRET"}},
           "task": {"execution_question": {"request_id": "r", "questions_sha256": "qhash",
                 "questions": [{"id": "q", "title": "选择", "options": [{"id": "a", "label": "选项"}]}],
                 "draft": [{"question_id": "q", "option_ids": ["a"], "text": "", "skipped": False}]},
               "plan_clarification": {"id": "clarify", "questions": [{"id": "p", "title": "规划"}]},
               "review_key": "review", "review_report_sha256": "digest", "run_id": "run"},
           "messages": [{"id": "m", "revision": 3, "role": "tool", "content": "普通消息", "tool": {"name": "read", "output": "内容"}}],
           "next_cursor": "cursor", "source_version": "version", "has_more": True}
    result = mobile.project(raw)
    assert result["connection"] == {"state": "unavailable", "detail": "暂时离线", "poll_seconds": 5, "subscription": False}
    assert result["task"] == raw["task"]
    assert result["messages"] == raw["messages"]
    assert result["source_version"] == "version" and result["next_cursor"] == "cursor"
    assert "SECRET" not in json.dumps(result)


def test_existing_authorization_guard_is_composed_and_restored(mobile_env):
    enable(mobile_env)
    phone = pair(mobile_env)
    ctx = cli.Context(mobile_env)
    calls = []
    def preexisting(conn):
        calls.append(conn.in_transaction)
        if conn.in_transaction:
            raise util.KanpilotError("forbidden", "original guard")
    ctx.conn.authorization_guard = preexisting
    try:
        with pytest.raises(util.KanpilotError, match="original guard"):
            mobile.cmd_mobile_request(ctx, {"kind": "action", "token": phone["device_token"],
                "data": {"action": "set-paused", "operation_id": "guard-1", "args": {"paused": True, "expected_paused": False}}})
        assert ctx.conn.authorization_guard is preexisting
        assert True in calls
    finally:
        ctx.close()
    assert run(mobile_env, "overview")["paused"]["global"] is False


def test_host_change_while_disabled_rotates_identity_before_reenable(mobile_env, monkeypatch):
    first = enable(mobile_env)
    phone = pair(mobile_env)
    monkeypatch.setattr(mobile, "local_addresses", lambda: [{"address": "127.0.0.1", "name": "loopback"}, {"address": "127.0.0.2", "name": "loopback"}])
    run(mobile_env, "set-mobile-access", {"enabled": False, "bind_host": "127.0.0.2"})
    second = run(mobile_env, "set-mobile-access", {"enabled": True})
    assert second["certificate_sha256"] != first["certificate_sha256"]
    assert second["server_id"] != first["server_id"]
    with pytest.raises(util.KanpilotError) as exc:
        query(mobile_env, phone)
    assert exc.value.code == "unauthorized"


def test_mobile_review_and_task_projection_keeps_current_identities(mobile_env):
    enable(mobile_env)
    phone = pair(mobile_env)
    report, args = make_review(mobile_env)
    result = query(mobile_env, phone, "get-review-report", args)["data"]
    assert result["markdown"] == "# Review\nall good" and result["execution_run_id"] == "run-1"
    task = query(mobile_env, phone, "get-task", {"task_id": "t"})["data"]["task"]
    assert task["review_key"] == "review-1" and task["review_report_sha256"] == args["expected_report_sha256"]
    assert task["run_id"] == "run-1"
    assert "review_report_path" not in task and "repository" not in task


def test_real_execution_answer_disconnect_remains_unknown_and_never_resends(tmp_path):
    from unittest import mock
    from test_execution_questions import ExecutionQuestionsTests
    from kanpilot import execution_questions
    scene = ExecutionQuestionsTests(methodName="runTest")
    scene.setUp()
    try:
        question = scene.scene()
        enable(scene.db_path)
        phone = pair(scene.db_path)
        client, job = scene.fake_client()
        client.input_respond.side_effect = util.KanpilotError("remote_unavailable", "response lost after send")
        args = scene.answer(question)
        args.pop("operation_id")
        payload = {"action": "submit-execution-answers", "operation_id": "mobile-disconnect", "args": args}
        with mock.patch.object(execution_questions, "_client", return_value=(client, job)), \
             mock.patch.object(execution_questions, "ensure_observer", return_value={"status": "observing"}):
            result = request(scene.db_path, "action", payload, phone["device_token"])
            assert result["status"] == "unknown"
            assert result["data"]["execution_question"]["delivery"] == "unknown"
            assert result["data"]["status"] == "unknown"
            assert client.input_respond.call_count == 1
            assert request(scene.db_path, "action", payload, phone["device_token"]) == result
            assert request(scene.db_path, "operation", {"operation_id": "mobile-disconnect"}, phone["device_token"]) == result
            assert client.input_respond.call_count == 1
    finally:
        scene.doCleanups()


def test_real_execution_answer_nested_delivery_survives_missing_top_status():
    from unittest import mock
    from test_execution_questions import ExecutionQuestionsTests
    from kanpilot import execution_questions
    scene = ExecutionQuestionsTests(methodName="runTest")
    scene.setUp()
    try:
        question = scene.scene()
        enable(scene.db_path)
        phone = pair(scene.db_path)
        client, job = scene.fake_client()
        client.input_respond.side_effect = util.KanpilotError("remote_unavailable", "lost")
        original_result = execution_questions._result
        def without_top_status(*args, **kwargs):
            result = original_result(*args, **kwargs)
            result.pop("status", None)
            return result
        args = scene.answer(question)
        args.pop("operation_id")
        payload = {"action": "submit-execution-answers", "operation_id": "nested-disconnect", "args": args}
        with mock.patch.object(execution_questions, "_client", return_value=(client, job)), \
             mock.patch.object(execution_questions, "ensure_observer", return_value={}), \
             mock.patch.object(execution_questions, "_result", side_effect=without_top_status):
            result = request(scene.db_path, "action", payload, phone["device_token"])
            assert "status" not in result["data"]
            assert result["data"]["execution_question"]["delivery"] == "unknown"
            assert result["status"] == "unknown"
            assert request(scene.db_path, "action", payload, phone["device_token"])["status"] == "unknown"
            assert client.input_respond.call_count == 1
    finally:
        scene.doCleanups()


def test_real_not_pending_response_remains_unknown_and_blocks_other_device_retry():
    from unittest import mock
    from test_execution_questions import ExecutionQuestionsTests
    from kanpilot import execution_questions
    scene = ExecutionQuestionsTests(methodName="runTest")
    scene.setUp()
    try:
        question = scene.scene()
        enable(scene.db_path)
        first, second = pair(scene.db_path), pair(scene.db_path)
        client, job = scene.fake_client()
        client.input_respond.return_value = {"status": "not_pending"}
        client.input_pending.side_effect = util.KanpilotError("remote_unavailable", "cannot reconcile")
        args = scene.answer(question)
        args.pop("operation_id")
        payload = {"action": "submit-execution-answers", "operation_id": "not-pending", "args": args}
        with mock.patch.object(execution_questions, "_client", return_value=(client, job)), \
             mock.patch.object(execution_questions, "ensure_observer", return_value={}):
            result = request(scene.db_path, "action", payload, first["device_token"])
            assert result["data"]["status"] == "not_pending"
            assert result["status"] == "unknown"
            assert request(scene.db_path, "action", payload, first["device_token"]) == result
            # Core uses not_pending as an uncertain send observation too; mobile
            # must not inherit the desktop's explicit-new-operation retry path.
            for phone, operation in ((first, "retry-1"), (second, "retry-2")):
                with pytest.raises(util.KanpilotError) as exc:
                    request(scene.db_path, "action", dict(payload, operation_id=operation), phone["device_token"])
                assert exc.value.code == "conflict"
            assert client.input_respond.call_count == 1
    finally:
        scene.doCleanups()


def test_defensive_guard_failure_after_send_is_unknown_not_rejected():
    from unittest import mock
    from test_execution_questions import ExecutionQuestionsTests
    from kanpilot import execution_questions, verification_checks
    scene = ExecutionQuestionsTests(methodName="runTest")
    scene.setUp()
    try:
        question = scene.scene()
        enable(scene.db_path)
        phone = pair(scene.db_path)
        client, job = scene.fake_client()
        sent = []
        original_guard = verification_checks.require_not_running
        def after_send(*args, **kwargs):
            sent.append(True)
            return {"status": "answered"}
        def injected_guard(conn, task_id):
            if sent:
                raise util.KanpilotError("conflict", "injected post-send guard failure")
            return original_guard(conn, task_id)
        client.input_respond.side_effect = after_send
        args = scene.answer(question)
        args.pop("operation_id")
        payload = {"action": "submit-execution-answers", "operation_id": "post-send", "args": args}
        with mock.patch.object(execution_questions, "_client", return_value=(client, job)), \
             mock.patch.object(execution_questions, "ensure_observer", return_value={}), \
             mock.patch.object(verification_checks, "require_not_running", side_effect=injected_guard):
            result = request(scene.db_path, "action", payload, phone["device_token"])
            assert result["status"] == "unknown"
            assert request(scene.db_path, "action", payload, phone["device_token"]) == result
            assert client.input_respond.call_count == 1
    finally:
        scene.doCleanups()


def test_real_late_execution_receipt_keeps_nested_unknown():
    from unittest import mock
    from test_execution_questions import ExecutionQuestionsTests
    from kanpilot import execution_questions
    scene = ExecutionQuestionsTests(methodName="runTest")
    scene.setUp()
    try:
        question = scene.scene()
        enable(scene.db_path)
        phone = pair(scene.db_path)
        client, job = scene.fake_client()
        def late(*args, **kwargs):
            scene.call("report-execution-question", {
                **{key: value for key, value in scene.payload.items() if key != "questions"}, "state": "closed"})
            return {"status": "answered"}
        client.input_respond.side_effect = late
        args = scene.answer(question)
        args.pop("operation_id")
        payload = {"action": "submit-execution-answers", "operation_id": "late-mobile", "args": args}
        with mock.patch.object(execution_questions, "_client", return_value=(client, job)), \
             mock.patch.object(execution_questions, "ensure_observer", return_value={}):
            result = request(scene.db_path, "action", payload, phone["device_token"])
            assert result["data"]["status"] == "accepted"
            assert result["data"]["execution_question"]["delivery"] == "unknown"
            assert result["status"] == "unknown"
            assert request(scene.db_path, "action", payload, phone["device_token"]) == result
            assert client.input_respond.call_count == 1
    finally:
        scene.doCleanups()
