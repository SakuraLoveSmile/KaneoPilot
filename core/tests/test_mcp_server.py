"""Unit tests for mcp_server.py and mcp_stdio.py (protocol §5.25 / T7.4)."""

import datetime
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from pathlib import Path
import sys

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
import pytest
from kanpilot import constants, db, mcp_client, mcp_server, mcp_stdio, util


@pytest.fixture
def mcp_env(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    conn.close()

    app_support = tmp_path / "app_support"
    srv = mcp_server.MCPServer(
        db_path=db_path,
        app_support_dir=app_support,
        host="127.0.0.1",
        port=0,
    )
    srv.start()
    time.sleep(0.05)
    yield srv
    srv.stop()


def _post_json(url: str, data: dict, headers: dict = None) -> tuple[int, dict, dict]:
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=hdrs,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp_body = resp.read().decode("utf-8")
            resp_hdrs = dict(resp.headers)
            parsed = json.loads(resp_body) if resp_body else {}
            return resp.status, parsed, resp_hdrs
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"error": body}
        return exc.code, parsed, dict(exc.headers)


def test_mcp_server_manifest(mcp_env: mcp_server.MCPServer):
    manifest_file = mcp_env.connection_file
    assert manifest_file.is_file()
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert data["server_name"] == constants.MCP_SERVER_NAME
    assert data["active"] is True
    assert data["protocol_version"] == constants.MCP_PROTOCOL_VERSION
    assert "127.0.0.1" in data["url"]


def test_mcp_http_get_method_not_allowed(mcp_env: mcp_server.MCPServer):
    req = urllib.request.Request(mcp_env.server_url, method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5.0)
    assert exc_info.value.code == 405
    assert "POST" in exc_info.value.headers.get("Allow", "")


def test_mcp_requires_bearer_and_binds_session_to_grant(mcp_env: mcp_server.MCPServer):
    exp = util.to_iso(util.now_dt() + datetime.timedelta(hours=1))
    first = mcp_env.scope_manager.register_scope(
        {"scope_id": "session-a", "invocation_id": "inv-a", "role": "planner", "expires_at": exp},
        mcp_env.server_url,
    )
    second = mcp_env.scope_manager.register_scope(
        {"scope_id": "session-b", "invocation_id": "inv-b", "role": "planner", "expires_at": exp},
        mcp_env.server_url,
    )
    first_grant = json.loads(Path(first["runtime_grant_file"]).read_text(encoding="utf-8"))
    second_grant = json.loads(Path(second["runtime_grant_file"]).read_text(encoding="utf-8"))
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    status, _, headers = _post_json(
        mcp_env.server_url, initialize,
        headers={"Authorization": f"Bearer {first_grant['token']}"},
    )
    assert status == 200
    session_id = headers["Mcp-Session-Id"]

    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    status, _, _ = _post_json(mcp_env.server_url, ping, headers={"Mcp-Session-Id": session_id})
    assert status == 401
    status, _, _ = _post_json(
        mcp_env.server_url, ping,
        headers={"Authorization": f"Bearer {second_grant['token']}", "Mcp-Session-Id": session_id},
    )
    assert status == 401
    status, _, _ = _post_json(
        mcp_env.server_url, ping,
        headers={"Authorization": f"Bearer {first_grant['token']}", "Mcp-Session-Id": session_id},
    )
    assert status == 200

    mcp_env.scope_manager.revoke_scope("session-a")
    status, _, _ = _post_json(
        mcp_env.server_url, ping,
        headers={"Authorization": f"Bearer {first_grant['token']}", "Mcp-Session-Id": session_id},
    )
    assert status == 401


def test_admin_credential_is_limited_to_mcp_transport_health(mcp_env: mcp_server.MCPServer):
    token = mcp_env.scope_manager.admin_token
    auth_headers = {"Authorization": f"Bearer {token}"}
    status, _, headers = _post_json(
        mcp_env.server_url,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=auth_headers,
    )
    assert status == 200
    session_id = headers["Mcp-Session-Id"]
    session_headers = {**auth_headers, "Mcp-Session-Id": session_id}

    status, _, _ = _post_json(
        mcp_env.server_url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=session_headers,
    )
    assert status == 202
    status, _, _ = _post_json(
        mcp_env.server_url,
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers=session_headers,
    )
    assert status == 200

    for rpc_id, method, params in (
        (3, "tools/list", {}),
        (4, "tools/call", {"name": "kp_runtime_capabilities", "arguments": {}}),
    ):
        status, result, _ = _post_json(
            mcp_env.server_url,
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
            headers=session_headers,
        )
        assert status == 403
        assert "result" not in result

    delete_req = urllib.request.Request(
        mcp_env.server_url,
        headers=session_headers,
        method="DELETE",
    )
    with urllib.request.urlopen(delete_req, timeout=5.0) as resp:
        assert resp.status == 204


def test_mcp_rejects_invalid_host_origin_and_unauthenticated_notification(mcp_env: mcp_server.MCPServer):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    status, _, _ = _post_json(mcp_env.server_url, ping, headers={"Host": "127.0.0.1"})
    assert status == 403
    status, _, _ = _post_json(mcp_env.server_url, ping, headers={"Origin": "http://localhost"})
    assert status == 403
    status, _, _ = _post_json(
        mcp_env.server_url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert status == 401


def test_admin_delete_requires_admin_bearer(mcp_env: mcp_server.MCPServer):
    req = urllib.request.Request(
        f"http://127.0.0.1:{mcp_env.port}/admin/scopes/no-scope",
        method="DELETE",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5.0)
    assert exc_info.value.code == 401


def test_admin_scope_rejects_unknown_task_relationship(mcp_env: mcp_server.MCPServer):
    status, result, _ = _post_json(
        f"http://127.0.0.1:{mcp_env.port}/admin/scopes",
        {
            "scope_id": "bad-task-scope",
            "invocation_id": "inv-bad",
            "role": "dispatch",
            "expires_at": util.to_iso(util.now_dt() + datetime.timedelta(hours=1)),
            "task_id": "missing-task",
        },
        headers={"Authorization": f"Bearer {mcp_env.scope_manager.admin_token}"},
    )
    assert status == 400
    assert result["code"] == "bad_request"


def test_stdio_accepts_only_loopback_mcp_endpoints():
    assert mcp_stdio._valid_loopback_mcp_url("http://127.0.0.1:8765/mcp")
    assert mcp_stdio._valid_loopback_mcp_url("http://[::1]:8765/mcp")
    assert not mcp_stdio._valid_loopback_mcp_url("https://127.0.0.1:8765/mcp")
    assert not mcp_stdio._valid_loopback_mcp_url("http://example.com:8765/mcp")
    assert not mcp_stdio._valid_loopback_mcp_url("http://127.0.0.1:8765/other")


def test_mcp_admin_scopes_lifecycle(mcp_env: mcp_server.MCPServer):
    admin_url = f"http://127.0.0.1:{mcp_env.port}/admin/scopes"
    admin_token = mcp_env.scope_manager.admin_token
    exp = util.to_iso(util.now_dt() + datetime.timedelta(hours=1))

    # 1. Reject without admin token
    status, res, _ = _post_json(admin_url, {"scope_id": "test-sc"})
    assert status == 401

    # 2. Register planner scope
    status, res, _ = _post_json(
        admin_url,
        {
            "scope_id": "test-planner",
            "invocation_id": "inv-1",
            "role": "planner",
            "expires_at": exp,
            "project_path": "/tmp/proj",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert status == 200
    assert res["ok"] is True
    assert res["data"]["scope_id"] == "test-planner"
    assert res["data"]["runtime_grant_file"] is not None

    # 3. Renew scope
    new_exp = util.to_iso(util.now_dt() + datetime.timedelta(minutes=45))
    renew_url = f"{admin_url}/test-planner/renew"
    status, res, _ = _post_json(
        renew_url,
        {"expires_at": new_exp},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert status == 200
    assert res["ok"] is True
    assert res["data"]["expires_at"] == new_exp

    # 4. Delete scope
    del_req = urllib.request.Request(
        f"{admin_url}/test-planner",
        headers={"Authorization": f"Bearer {admin_token}"},
        method="DELETE",
    )
    with urllib.request.urlopen(del_req, timeout=5.0) as resp:
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        assert body["data"]["revoked"] is True


def test_mcp_protocol_handshake_and_tools(mcp_env: mcp_server.MCPServer):
    admin_url = f"http://127.0.0.1:{mcp_env.port}/admin/scopes"
    admin_token = mcp_env.scope_manager.admin_token
    exp = util.to_iso(util.now_dt() + datetime.timedelta(hours=1))

    # Register dispatch scope
    status, scope_res, _ = _post_json(
        admin_url,
        {
            "scope_id": "sc-dispatch-test",
            "invocation_id": "inv-d1",
            "role": "dispatch",
            "expires_at": exp,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    task_grant_file = Path(scope_res["data"]["task_grant_file"])
    task_grant = json.loads(task_grant_file.read_text(encoding="utf-8"))
    task_token = task_grant["token"]

    # Use MCPClient with task grant
    client = mcp_client.MCPClient.from_grant_file(task_grant_file)
    init_res = client.initialize(client_name="test-client", client_version="1.0.0")
    assert init_res["serverInfo"]["name"] == constants.MCP_SERVER_NAME
    assert client.mcp_session_id is not None

    # Ping
    client.ping()

    # List tools - should only see dispatch task tools
    tools = client.list_tools()
    tool_names = {t["name"] for t in tools}
    assert "kp_task_claim" in tool_names
    assert "kp_task_dispatch_prepare" in tool_names
    # Should NOT contain runtime tools or monitor tools
    assert "kp_runtime_session_create" not in tool_names
    assert "kp_task_advance" not in tool_names

    # Test calling an unauthorized tool -> should return forbidden error
    with pytest.raises(util.KanpilotError) as exc_info:
        client.call_tool("kp_runtime_session_create", {"scope_id": "sc-dispatch-test", "operation_id": "op-1"})
    assert exc_info.value.code == "forbidden"
