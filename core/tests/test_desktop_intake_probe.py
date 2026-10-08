"""Isolated fixed queue / stdio checks. No daemon, model or remote service."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
import json
import os
import threading
from types import SimpleNamespace

import pytest

from kanpilot import cli, db, desktop_intake_probe as probe, util


@pytest.fixture
def call(tmp_env, monkeypatch):
    # Support the parallel integration window before the lead registers commands.
    specs = {
        "desktop-probe-create": ({}, probe.cmd_desktop_probe_create),
        "desktop-probe-status": ({}, probe.cmd_desktop_probe_status),
        "desktop-probe-claim": ({"role": (True, "str")}, probe.cmd_desktop_probe_claim),
        "desktop-probe-report": ({key: (key in ("request_id", "claim_token", "output"), "str")
                                  for key in ("request_id", "claim_token", "output", "thread_id", "model")},
                                 probe.cmd_desktop_probe_report),
    }
    for command, entry in specs.items():
        if command not in cli.COMMANDS:
            monkeypatch.setitem(cli.COMMANDS, command, entry)

    def invoke(command, **payload):
        return cli.run("desktop-probe-" + command, payload, tmp_env.db)
    return invoke


def _report(call, claim, output="中文固定验证结果", **metadata):
    return call("report", request_id=claim["request_id"], claim_token=claim["claim_token"],
                output=output, **metadata)


def _setting(tmp_env):
    conn = db.connect(tmp_env.db)
    try:
        return db.get_setting(conn, probe.SETTING_KEY)
    finally:
        conn.close()


def test_empty_status_and_claim_do_not_create_batch(call, tmp_env):
    assert call("status")["batch_id"] is None
    assert call("status")["requests"] == []
    assert call("claim", role="plan") == {"available": False, "reason": "no_batch"}
    assert _setting(tmp_env) is None


def test_create_is_idempotent_and_only_changes_own_setting(call, tmp_env):
    call("status")
    conn = db.connect(tmp_env.db)
    try:
        before = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM settings")}
        counts = {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("tasks", "requests", "events")}
    finally:
        conn.close()
    batch = call("create")
    assert batch == call("create")
    assert [row["role"] for row in batch["requests"]] == ["plan", "review"]
    assert {row["status"] for row in batch["requests"]} == {"queued"}
    plan = call("claim", role="plan")
    _report(call, plan)
    review = call("claim", role="review")
    _report(call, review)
    conn = db.connect(tmp_env.db)
    try:
        after = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM settings")}
        assert {key: value for key, value in after.items() if key != probe.SETTING_KEY} == before
        assert {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in counts} == counts
    finally:
        conn.close()
    next_batch = call("create")
    assert next_batch["batch_id"] != batch["batch_id"]
    assert all(row["status"] == "queued" for row in next_batch["requests"])


def test_plan_then_review_complete_with_process_evidence(call):
    call("create")
    assert call("claim", role="review")["reason"] == "plan_pending"
    plan = call("claim", role="plan")
    assert plan["available"] and "1 到 10" in plan["instructions"]
    assert call("claim", role="plan")["reason"] == "already_claimed"
    assert call("claim", role="review")["reason"] == "plan_pending"
    _report(call, plan, "上游计划", thread_id="desktop-thread", model="fixture-model")
    review = call("claim", role="review")
    assert review["upstream_plan"] == "上游计划"
    assert "Number(raw)" in review["instructions"] and "1.5" in review["instructions"] and "NaN" in review["instructions"]
    _report(call, review, "1.5 通过范围检查；非数字产生 NaN。")
    status = call("status")
    assert [row["status"] for row in status["requests"]] == ["completed", "completed"]
    assert status["requests"][0]["thread_id"] == "desktop-thread"
    for row in status["requests"]:
        assert row["execution_evidence"] == {"pid": os.getpid(), "ppid": os.getppid()}
    assert plan["claim_token"] not in json.dumps(status)
    assert review["claim_token"] not in json.dumps(status)
    assert call("claim", role="plan")["reason"] == "completed"
    assert call("claim", role="review")["reason"] == "completed"


def test_identity_denial_and_no_token_disclosure(call):
    batch = call("create")
    unclaimed = batch["requests"][0]
    with pytest.raises(util.KanpilotError, match="身份不匹配") as exc:
        call("report", request_id=unclaimed["request_id"], claim_token="secret-foreign", output="结果")
    assert exc.value.code == "claim_denied"
    claim = call("claim", role="plan")
    for token, request_id in (("secret-foreign", claim["request_id"]), ("非 ASCII", claim["request_id"]),
                              (claim["claim_token"], batch["requests"][1]["request_id"])):
        with pytest.raises(util.KanpilotError) as exc:
            call("report", request_id=request_id, claim_token=token, output="结果")
        assert exc.value.code == "claim_denied"
        assert token not in str(exc.value)
    with pytest.raises(util.KanpilotError):
        _report(call, claim, "凭据 " + claim["claim_token"])
    status = call("status")
    assert status["requests"][0]["status"] == "claimed"
    assert "claim_token" not in json.dumps(status)


def test_report_exact_idempotence_and_conflict(call):
    call("create")
    claim = call("claim", role="plan")
    result = _report(call, claim, "计划", thread_id="thread")
    assert _report(call, claim, "计划", thread_id="thread") == result
    for output, metadata in (("不同计划", {"thread_id": "thread"}), ("计划", {"thread_id": "other"})):
        with pytest.raises(util.KanpilotError) as exc:
            _report(call, claim, output, **metadata)
        assert exc.value.code == "conflict"
    assert call("status")["requests"][0]["output"] == "计划"


def test_strict_lease_expiry_reclaim_invalidates_old_token(call, monkeypatch):
    now = util.now_dt()
    monkeypatch.setattr(util, "now_dt", lambda: now)
    call("create")
    old = call("claim", role="plan")
    now += timedelta(seconds=probe.LEASE_SECONDS)
    with pytest.raises(util.KanpilotError) as exc:
        _report(call, old)
    assert exc.value.code == "lease_expired"
    current = call("claim", role="plan")
    assert current["request_id"] == old["request_id"]
    assert current["claim_token"] != old["claim_token"]
    with pytest.raises(util.KanpilotError) as exc:
        _report(call, old)
    assert exc.value.code == "claim_denied"
    _report(call, current)
    now += timedelta(seconds=probe.LEASE_SECONDS + 1)
    assert _report(call, current)["ok"]  # Completed report retry no longer needs a lease.


def test_concurrent_claim_has_exactly_one_owner(call):
    call("create")
    barrier = threading.Barrier(8)

    def take(_):
        barrier.wait(timeout=5)
        return call("claim", role="plan")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(take, range(8)))
    assert sum(result["available"] for result in claims) == 1
    assert all(result["reason"] == "already_claimed" for result in claims if not result["available"])
    owner = next(result for result in claims if result["available"])
    _report(call, owner)


@pytest.mark.parametrize("payload", [
    {"role": "execute"}, {"role": "plan", "task_id": "formal-task"},
    {"role": "plan", "source_path": "/private/source"},
])
def test_fixed_inputs_reject_scope_expansion(call, payload):
    call("create")
    with pytest.raises(util.KanpilotError) as exc:
        call("claim", **payload)
    assert exc.value.code == "bad_request"
    assert all(row["status"] == "queued" for row in call("status")["requests"])


@pytest.mark.parametrize("output", ["", " \n", "中" * (probe.MAX_OUTPUT_BYTES // 3 + 1), "\ud800"])
def test_report_output_boundaries(call, output):
    call("create")
    claim = call("claim", role="plan")
    with pytest.raises(util.KanpilotError) as exc:
        _report(call, claim, output)
    assert exc.value.code == "bad_request"
    assert call("status")["requests"][0]["status"] == "claimed"


def test_report_accepts_exact_64k_bytes(call):
    call("create")
    claim = call("claim", role="plan")
    assert _report(call, claim, "a" * probe.MAX_OUTPUT_BYTES)["ok"]


def _stdio(monkeypatch, tmp_env, data):
    output = io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(probe.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    monkeypatch.setattr(probe.sys, "stdout", output)
    monkeypatch.setattr(probe.sys, "stderr", errors)
    assert probe.run_stdio(tmp_env.db) == 0
    assert errors.getvalue() == ""
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _wire(*messages):
    return ("\n".join(json.dumps(item) for item in messages) + "\n").encode()


def test_stdio_initialize_three_tools_status_and_notifications(call, tmp_env, monkeypatch):
    responses = _stdio(monkeypatch, tmp_env, _wire(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": probe.PROTOCOL_VERSION}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kp_desktop_probe_status", "arguments": {}}},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "kp_desktop_probe_claim", "arguments": {"role": "plan"}}},
    ))
    assert [row["id"] for row in responses] == [1, 2, 3]
    assert responses[0]["result"]["protocolVersion"] == probe.PROTOCOL_VERSION
    assert responses[0]["result"]["capabilities"] == {"tools": {}}
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
        "kp_desktop_probe_claim", "kp_desktop_probe_report", "kp_desktop_probe_status"}
    result = responses[2]["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["batch_id"] is None


def test_stdio_claim_report_dispatch_and_safe_errors(call, tmp_env, monkeypatch):
    call("create")
    responses = _stdio(monkeypatch, tmp_env, _wire(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kp_desktop_probe_claim", "arguments": {"role": "plan"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete-task", "arguments": {"claim_token": "SECRET"}}},
    ))
    claim = responses[0]["result"]["structuredContent"]
    assert claim["available"]
    assert responses[1]["result"]["isError"]
    assert "SECRET" not in json.dumps(responses[1])
    report = {"request_id": claim["request_id"], "claim_token": claim["claim_token"], "output": "计划结果"}
    responses = _stdio(monkeypatch, tmp_env, _wire(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kp_desktop_probe_report", "arguments": report}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kp_desktop_probe_report", "arguments": {**report, "claim_token": "SECRET"}}},
    ))
    assert responses[0]["result"]["structuredContent"] == {"ok": True, "status": "completed"}
    assert "claim_token" not in json.dumps(responses)
    assert responses[1]["result"]["structuredContent"]["code"] == "claim_denied"
    assert "SECRET" not in json.dumps(responses)


def test_stdio_malformed_and_oversize_lines_recover(call, tmp_env, monkeypatch):
    data = (b"{ malformed SECRET }\n" + b"x" * (probe.MAX_MESSAGE_BYTES + 5) + b"\n" +
            _wire({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tasks/delete"}))
    responses = _stdio(monkeypatch, tmp_env, data)
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["code"] == -32600
    assert responses[2]["result"] == {}
    assert responses[3]["error"]["code"] == -32601
    assert "SECRET" not in json.dumps(responses)


def test_stdio_invalid_unicode_request_id_does_not_break_transport(call, tmp_env, monkeypatch):
    responses = _stdio(monkeypatch, tmp_env, _wire(
        {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ))
    assert responses[0]["error"]["code"] == -32600
    assert responses[0]["id"] is None
    assert responses[1]["result"] == {}


def test_stdio_oversize_result_and_unexpected_error_are_safe(call, tmp_env, monkeypatch):
    def oversized(*_):
        return {"data": "SECRET" * probe.MAX_MESSAGE_BYTES}
    monkeypatch.setattr(cli, "run", oversized)
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kp_desktop_probe_status"}}
    responses = _stdio(monkeypatch, tmp_env, _wire(message))
    assert responses[0]["error"]["code"] == -32603
    assert "SECRET" not in json.dumps(responses)

    def broken(*_):
        raise RuntimeError("SECRET accidental diagnostic")
    monkeypatch.setattr(cli, "run", broken)
    responses = _stdio(monkeypatch, tmp_env, _wire(message))
    assert responses[0]["result"]["isError"]
    assert responses[0]["result"]["structuredContent"]["code"] == "probe_error"
    assert "SECRET" not in json.dumps(responses)
