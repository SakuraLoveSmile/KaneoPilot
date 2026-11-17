"""Reject untransportable Plus verify commands before ready/dispatch side effects."""
from unittest.mock import patch

import pytest

from helpers import KanpilotTestCase
from kanpilot import db, exec_config, pi_desktop_backend, util, verify_evidence


PLUS_CONFIG = {"backend": "pi_desktop", "pi_desktop": {
    "model_source": "custom", "connection_file": "/isolated/plus.json",
    "provider_id": "fixture-provider", "model_id": "fixture-model",
    "thinking_level": "medium", "permission_mode": "auto"}}


@pytest.fixture
def case():
    instance = KanpilotTestCase()
    instance.setUp()
    try:
        yield instance
    finally:
        instance.tearDown()
        instance.doCleanups()


def save_route(case, backend):
    conn = db.connect(case.db_path)
    try:
        db.set_setting(conn, "exec_config", PLUS_CONFIG if backend == "pi_desktop" else {"backend": backend})
    finally:
        conn.close()


def prepare(case, commands=None, *, text=None, backend="pi_desktop", planner_backend="codex"):
    fixture = case.make_ready_task(publish=False)
    task_id = fixture["remote"]["id"]
    if text is None:
        text = "# Plan\n```verify\n" + "\n".join(commands) + "\n```\n"
    path = case.write_plan("Feedback", fixture["remote"], {
        "source_sha256": fixture["source_sha256"],
        "requirements_sha256": fixture["requirements_sha256"]}, body=text)
    request_id, input_sha = case.queue_plan(task_id, source_sha256=fixture["source_sha256"],
                                           requirements_sha256=fixture["requirements_sha256"])
    claim = case.call("claim", {"stage": "plan", "executor": "isolated-planner", "request_id": request_id})
    assert claim["claimed"]
    conn = db.connect(case.db_path)
    try:
        db.merge_request_payload(conn, request_id, util.now_iso(), planner_backend=planner_backend)
    finally:
        conn.close()
    save_route(case, backend)
    return {"task_id": task_id, "plan_path": str(path), "plan_kind": "standard",
            "source_sha256": fixture["source_sha256"], "requirements_sha256": fixture["requirements_sha256"],
            "generated_at": "2026-09-20T00:00:00+00:00", "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium", "request_id": request_id, "run_id": claim["run_id"],
            "planner_input_sha256": input_sha}


@pytest.mark.parametrize("commands,reason", [
    (["x" * 4097], "4096"),
    (["好" * 1366], "4096"),
    (["python  -m pytest", "python -m pytest"], "重复"),
    (["printf 'a\x00b'"], "NUL"),
    (["true"] * 6, "1–5"),
])
def test_plus_publish_rejects_invalid_commands_without_advancing(case, commands, reason):
    payload = prepare(case, commands)
    before = case.task(payload["task_id"])
    # Publication must not resolve inherit defaults, probe the service or start an executor.
    with patch.object(exec_config, "snapshot", side_effect=AssertionError("publish must stay local")), \
            patch.object(exec_config, "check_available", side_effect=AssertionError("publish must not probe")):
        error = case.call_err("publish-plan", payload)
    assert error.code == "validation_failed"
    assert reason in error.message
    after = case.task(payload["task_id"])
    assert after["status"] == before["status"] == "planning"
    assert after["plan_path"] == before["plan_path"] is None
    assert not after.get("plan_execution_hold")
    conn = db.connect(case.db_path)
    try:
        request = db.get_request(conn, payload["request_id"])
        assert request["status"] == "claimed"
        assert request["run_id"] == payload["run_id"]
        assert not conn.execute("SELECT 1 FROM events WHERE kind='plan_published'").fetchone()
        assert not conn.execute("SELECT 1 FROM requests WHERE kind='dispatch'").fetchone()
        assert not conn.execute("SELECT 1 FROM execution_rounds WHERE run_id != ?", (payload["run_id"],)).fetchone()
    finally:
        conn.close()


@pytest.mark.parametrize("text", ["# Missing verify\n", "```verify\n```\n", "```verify\ntrue\n"])
def test_plus_publish_rejects_invalid_verify_declaration(case, text):
    payload = prepare(case, text=text)
    assert case.call_err("publish-plan", payload).code == "validation_failed"
    assert case.task(payload["task_id"])["status"] == "planning"


@pytest.mark.parametrize("command", ["x" * 4096, "好" * 1365 + "x"])
def test_plus_publish_accepts_exact_utf8_byte_boundary_and_is_idempotent(case, command):
    payload = prepare(case, [command])
    result = case.call("publish-plan", payload)
    assert result["task_status"] == "ready"
    assert result["plan_execution_hold"] is True
    again = case.call("publish-plan", payload)
    assert again["deduplicated"] is True
    conn = db.connect(case.db_path)
    try:
        task = db.require_task(conn, payload["task_id"])
        assert db.task_extra(task)["plan_verify"]["commands"] == [command]
        assert db.get_request(conn, payload["request_id"])["status"] == "succeeded"
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='plan_published'").fetchone()[0] == 1
    finally:
        conn.close()


def test_planner_backend_does_not_select_execution_transport(case):
    # A Plus planner can publish for Codex execution without Plus-only size constraints.
    payload = prepare(case, ["x" * 4097], backend="codex", planner_backend="pi_desktop")
    assert case.call("publish-plan", payload)["task_status"] == "ready"


def test_legacy_non_plus_publication_keeps_parse_error_record(case):
    payload = prepare(case, text="# Legacy plan without verify\n", backend="pi")
    assert case.call("publish-plan", payload)["task_status"] == "ready"
    conn = db.connect(case.db_path)
    try:
        assert "error" in db.task_extra(db.require_task(conn, payload["task_id"]))["plan_verify"]
    finally:
        conn.close()


def test_dispatch_rechecks_changed_execution_route_before_any_plus_probe(case):
    payload = prepare(case, ["x" * 4097], backend="codex")
    case.call("publish-plan", payload)
    save_route(case, "pi_desktop")
    case.enqueue_dispatch(payload["task_id"])
    with patch("kanpilot.dispatch_validate.mcp_client.MCPClient.from_server_file",
               side_effect=AssertionError("invalid requirements must fail before Plus probe")):
        result = case.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
    assert result["claimed"] is False
    assert result["code"] == "validation_failed"
    assert "4096" in result["detail"]
    assert case.task(payload["task_id"])["status"] == "needs_user"
    conn = db.connect(case.db_path)
    try:
        assert not conn.execute("SELECT 1 FROM execution_rounds WHERE run_id != ?", (payload["run_id"],)).fetchone()
    finally:
        conn.close()


def test_shared_check_reuses_host_contract_for_all_transport_constraints():
    for commands in (["a\rb"], ["a\nb"], ["\ud800"], [str(i).zfill(4) + "x" * 4092 for i in range(17)]):
        with pytest.raises(util.KanpilotError) as error:
            verify_evidence.validate_execution_requirements({"ok": True, "commands": commands}, "pi_desktop")
        assert error.value.code == "validation_failed"
    with patch.object(pi_desktop_backend, "validate_required_verification_commands", wraps=
                      pi_desktop_backend.validate_required_verification_commands) as host:
        verify_evidence.validate_execution_requirements({"ok": True, "commands": ["true"]}, "pi_desktop")
    host.assert_called_once_with(["true"], "approve")
