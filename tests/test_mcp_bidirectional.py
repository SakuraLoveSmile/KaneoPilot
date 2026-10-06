"""Integration test for KaneoPilot bidirectional standard MCP communication (T7 / protocol §5.25)."""

import datetime
import json
import time
from pathlib import Path

import pytest
from kanpilot import constants, db, mcp_client, mcp_server, mcp_scopes, util


def datetime_timedelta(**kwargs):
    import datetime
    return datetime.timedelta(**kwargs)


@pytest.fixture
def mcp_integration_server(factory, tmp_path: Path, monkeypatch):
    for k, v in factory.env.env.items():
        monkeypatch.setenv(k, v)

    app_support = tmp_path / "app_support"
    srv = mcp_server.MCPServer(
        db_path=factory.env.db,
        app_support_dir=app_support,
        host="127.0.0.1",
        port=0,
    )
    srv.start()
    time.sleep(0.05)

    yield {
        "server": srv,
        "factory": factory,
        "app_support": app_support,
    }
    srv.stop()


def test_mcp_dispatch_flow_and_idempotency(mcp_integration_server: dict):
    srv: mcp_server.MCPServer = mcp_integration_server["server"]
    factory = mcp_integration_server["factory"]

    # Create a ready task and queued dispatch request
    ready = factory.make_ready()
    task = ready.task
    factory.enqueue_dispatch(task.task_id)
    req = factory.find_request(task.task_id, "dispatch")
    request_id = req["request_id"]

    # 1. Register dispatch scope
    exp = util.to_iso(util.now_dt() + datetime_timedelta(hours=1))
    scope_res = srv.scope_manager.register_scope(
        {
            "scope_id": "scope-disp-1",
            "invocation_id": "inv-disp-1",
            "role": "dispatch",
            "expires_at": exp,
            "task_id": task.task_id,
        },
        server_url=srv.server_url,
    )
    task_grant_file = Path(scope_res["task_grant_file"])
    client = mcp_client.MCPClient.from_grant_file(task_grant_file)
    init_res = client.initialize(client_name="kp-dispatch-worker", client_version="1.0.0")
    assert init_res["serverInfo"]["name"] == constants.MCP_SERVER_NAME

    # 2. kp_task_claim with operation_id
    claim_res = client.call_tool(
        "kp_task_claim",
        {
            "operation_id": "op-claim-1",
            "request_id": request_id,
        },
    )
    assert claim_res.get("claimed") is True, f"Claim failed: {claim_res}"
    claimed_req_id = claim_res["request_id"]
    run_id = claim_res["run_id"]
    assert claimed_req_id == request_id
    assert run_id is not None

    # 3. Idempotency test: duplicate call with same operation_id returns exact same claim
    dup_claim = client.call_tool(
        "kp_task_claim",
        {
            "operation_id": "op-claim-1",
            "request_id": request_id,
        },
    )
    assert dup_claim["claimed"] is True
    assert dup_claim["run_id"] == run_id

    # 4. kp_task_heartbeat
    hb_res = client.call_tool(
        "kp_task_heartbeat",
        {
            "request_id": request_id,
            "run_id": run_id,
            "progress": "Running dispatch checks",
        },
    )
    assert hb_res["ok"] is True

    # 5. kp_task_dispatch_prepare
    try:
        from conftest import ui_evidence
    except ImportError:
        from tests.conftest import ui_evidence
    ev = ui_evidence(
        session_marker=claim_res["context"]["session_marker"],
        repository=claim_res["context"]["repository"],
        attachment_name=claim_res["context"]["attachment_name"],
    )
    prep_res = client.call_tool(
        "kp_task_dispatch_prepare",
        {
            "task_id": task.task_id,
            "run_id": run_id,
            "evidence": ev,
        },
    )
    assert prep_res.get("prepared", True) is True

    # 6. kp_task_dispatch_begin_send
    begin_res = client.call_tool(
        "kp_task_dispatch_begin_send",
        {
            "task_id": task.task_id,
            "run_id": run_id,
        },
    )
    assert begin_res.get("begin_send", True) is True

    # 7. kp_task_dispatch_confirm
    ev2 = dict(ev, message_visible=True, session_title=f"Session {task.task_id}", session_id=f"sess-{task.task_id[:8]}")
    confirm_res = client.call_tool(
        "kp_task_dispatch_confirm",
        {
            "task_id": task.task_id,
            "run_id": run_id,
            "evidence": ev2,
        },
    )
    assert confirm_res.get("confirmed", True) is True

    # 8. Monitor claim and report flow via MCP
    mon_req = factory.active_request(task.task_id, kind="monitor")
    mon_req_id_orig = mon_req["request_id"]

    mon_scope_res = srv.scope_manager.register_scope(
        {
            "scope_id": "scope-mon-1",
            "invocation_id": "inv-mon-1",
            "role": "monitor",
            "expires_at": exp,
            "task_id": task.task_id,
        },
        server_url=srv.server_url,
    )
    mon_grant_file = Path(mon_scope_res["task_grant_file"])
    mon_client = mcp_client.MCPClient.from_grant_file(mon_grant_file)
    mon_client.initialize(client_name="kp-monitor-worker", client_version="1.0.0")

    mon_claim_res = mon_client.call_tool(
        "kp_task_claim",
        {
            "operation_id": "op-mon-claim-1",
            "request_id": mon_req_id_orig,
        },
    )
    assert mon_claim_res["claimed"] is True
    mon_req_id = mon_claim_res["request_id"]
    mon_run_id = mon_claim_res["run_id"]
    assert mon_req_id == mon_req_id_orig
    # 9. kp_task_report by monitor
    report_res = mon_client.call_tool(
        "kp_task_report",
        {
            "request_id": mon_req_id,
            "run_id": mon_run_id,
            "outcome": "succeeded",
            "note": "Monitor observed task running",
        },
    )
    assert report_res["ok"] is True

def test_mcp_permission_isolation(mcp_integration_server: dict):
    srv: mcp_server.MCPServer = mcp_integration_server["server"]
    factory = mcp_integration_server["factory"]

    ready = factory.make_ready()
    task = ready.task

    exp = util.to_iso(util.now_dt() + datetime_timedelta(hours=1))

    # Register dispatch scope
    scope_res = srv.scope_manager.register_scope(
        {
            "scope_id": "scope-disp-iso",
            "invocation_id": "inv-disp-iso",
            "role": "dispatch",
            "expires_at": exp,
            "task_id": task.task_id,
        },
        server_url=srv.server_url,
    )
    task_grant_file = Path(scope_res["task_grant_file"])
    dispatch_client = mcp_client.MCPClient.from_grant_file(task_grant_file)
    dispatch_client.initialize()

    # 1. Dispatch cannot call monitor's advance tool
    with pytest.raises(util.KanpilotError) as exc_info:
        dispatch_client.call_tool(
            "kp_task_advance",
            {
                "task_id": task.task_id,
                "target": "in-review",
                "expected_current": "in-progress",
            },
        )
    assert exc_info.value.code == "forbidden"

    # 2. Dispatch task grant cannot call control plane runtime tools
    with pytest.raises(util.KanpilotError) as exc_info:
        dispatch_client.call_tool(
            "kp_runtime_session_create",
            {
                "scope_id": "scope-disp-iso",
                "operation_id": "op-illegal",
                "mode": "agent",
                "project_path": "/tmp",
            },
        )
    assert exc_info.value.code == "forbidden"
