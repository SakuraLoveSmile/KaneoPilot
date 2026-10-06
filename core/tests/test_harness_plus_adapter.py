"""Unit tests for harness_plus_adapter.py (protocol §5.25 / T7.5)."""

import json
from pathlib import Path
import sys

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
from unittest.mock import MagicMock, patch

import pytest
from kanpilot import constants, harness_plus_adapter, mcp_scopes, runtime_client, util


def test_plus_capabilities_offline(tmp_path: Path):
    non_existent = str(tmp_path / "nonexistent.json")
    res = harness_plus_adapter.plus_capabilities(non_existent)
    assert res["available"] is False
    assert res["connected"] is False
    assert res["tools_ok"] is False
    assert "不存在" in res["detail"]


def test_plus_capabilities_mocked_client(tmp_path: Path):
    mock_client = MagicMock()
    mock_client.server_info = {"version": "0.18.0", "name": "pi-desktop"}
    mock_client.protocol_version = "2025-06-18"
    mock_client.tools_list.return_value = {
        t: {} for t in constants.PI_DESKTOP_REQUIRED_TOOLS
    }

    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(mock_client, {})):
        res = harness_plus_adapter.plus_capabilities("/fake/conn.json")
        assert res["connected"] is True
        assert res["tools_ok"] is True
        assert res["version"] == "0.18.0"


def test_runtime_connect_and_prompt_without_session_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "app_support", lambda: tmp_path)
    facade = runtime_client.RuntimeClient(
        role="planner", connection_file=str(tmp_path / "plus.json"),
        project_path=str(tmp_path), runtime_config={}, invocation_id="no_attach",
    )
    client = MagicMock()
    client.initialize.return_value = {
        "serverInfo": {"name": constants.MCP_SERVER_NAME},
    }
    client.list_tools.return_value = [
        {"name": name} for name in mcp_scopes.RUNTIME_ROLE_TOOLS["planner"]
    ]
    client.call_tool.side_effect = [
        {"available": True, "tools_ok": True,
         "supported_operations": list(constants.PI_DESKTOP_PLAN_TOOLS)},
        {"session_id": "sess-1", "accepted": True, "turn_id": "turn-1"},
    ]
    with patch.object(runtime_client, "_read_admin_server", return_value=(
            {"url": "http://127.0.0.1:8765/mcp"}, "test-admin")), \
            patch.object(runtime_client, "_admin_register", return_value={
                "runtime_grant_file": str(tmp_path / "runtime.json")}), \
            patch.object(runtime_client.mcp_client.MCPClient, "from_grant_file",
                         return_value=client):
        facade.connect(required_tools=constants.PI_DESKTOP_PLAN_TOOLS)
        result = facade.agent_prompt("sess-1", "规划输入")
    assert result["accepted"] is True
    assert result["turnId"] == "turn-1"
    assert [call.args[0] for call in client.call_tool.call_args_list] == [
        "kp_runtime_capabilities", "kp_runtime_prompt",
    ]


@pytest.mark.parametrize("inactive", [False, True])
def test_runtime_without_ready_project_mcp_still_blocks(tmp_path, monkeypatch, inactive):
    monkeypatch.setattr(constants, "app_support", lambda: tmp_path)
    if inactive:
        server_file = tmp_path / "mcp" / "server.json"
        server_file.parent.mkdir()
        server_file.write_text(json.dumps({"active": False}), encoding="utf-8")
        server_file.chmod(0o600)
    facade = runtime_client.RuntimeClient(
        role="planner", connection_file=str(tmp_path / "plus.json"),
        project_path=str(tmp_path), runtime_config={}, invocation_id="not_ready",
    )
    with patch.object(runtime_client, "_admin_register") as register:
        with pytest.raises(util.KanpilotError) as exc:
            facade.connect(required_tools=constants.PI_DESKTOP_PLAN_TOOLS)
    assert exc.value.code == ("remote_unavailable" if inactive else "not_found")
    register.assert_not_called()


def test_readonly_runtime_prompt_remains_forbidden(tmp_path):
    facade = runtime_client.RuntimeClient(
        role="planner", connection_file=str(tmp_path / "plus.json"),
        project_path=str(tmp_path), runtime_config={}, invocation_id="readonly",
        read_only=True,
    )
    with pytest.raises(util.KanpilotError) as exc:
        facade.agent_prompt("sess-1", "规划输入")
    assert exc.value.code == "forbidden"


def test_plus_session_attach_mcp_mocked(tmp_path: Path):
    grant_file = tmp_path / "grant.json"
    grant_file.write_text(json.dumps({
        "server_name": "kaneopilot", "url": "http://127.0.0.1:8765/mcp",
        "token": "grant_tok", "scope_id": "scope-1", "epoch": 1,
        "protocol_version": constants.MCP_PROTOCOL_VERSION,
        "role": "planner", "domain": "task",
    }), encoding="utf-8")
    grant_file.chmod(0o600)

    mock_client = MagicMock()
    mock_client.tools_list.return_value = {"pi_session_attach_mcp": {}}
    mock_client.call_tool.return_value = {
        "sessionId": "sess-123",
        "attached": True,
        "serverName": "kaneopilot",
        "role": "planner",
        "toolNames": sorted(mcp_scopes.TASK_ROLE_TOOLS["planner"]),
    }

    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(mock_client, {})):
        res = harness_plus_adapter.plus_attach_mcp(
            connection_file="/fake/conn.json",
            session_id="sess-123",
            bridge_file=str(grant_file),
        )
        assert res["attached"] is True
        assert res["session_id"] == "sess-123"
        assert res["server_name"] == "kaneopilot"
        assert set(res["tool_names"]) == mcp_scopes.TASK_ROLE_TOOLS["planner"]


def test_plus_session_attach_mcp_symlink_rejected(tmp_path: Path):
    real_file = tmp_path / "real_grant.json"
    real_file.write_text(json.dumps({"server_name": "kaneopilot", "url": "http://127.0.0.1:8765/mcp", "token": "grant_tok"}), encoding="utf-8")
    real_file.chmod(0o600)
    symlink_file = tmp_path / "sym_grant.json"
    symlink_file.symlink_to(real_file)

    mock_client = MagicMock()
    mock_client.tools_list.return_value = {"pi_session_attach_mcp": {}}

    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(mock_client, {})):
        with pytest.raises(util.KanpilotError) as exc_info:
            harness_plus_adapter.plus_attach_mcp(
                connection_file="/fake/conn.json",
                session_id="sess-123",
                bridge_file=str(symlink_file),
            )
        assert exc_info.value.code == "validation_failed"
        assert "符号链接" in exc_info.value.message


def test_plus_session_attach_mcp_missing_fields_rejected(tmp_path: Path):
    grant_file = tmp_path / "grant.json"
    grant_file.write_text(json.dumps({"server_name": "kaneopilot", "url": "http://127.0.0.1:8765/mcp", "token": "grant_tok"}), encoding="utf-8")
    grant_file.chmod(0o600)

    mock_client = MagicMock()
    mock_client.tools_list.return_value = {"pi_session_attach_mcp": {}}
    mock_client.call_tool.return_value = {
        "sessionId": "sess-123",
        "serverName": "kaneopilot",
    }

    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(mock_client, {})):
        with pytest.raises(util.KanpilotError) as exc_info:
            harness_plus_adapter.plus_attach_mcp(
                connection_file="/fake/conn.json",
                session_id="sess-123",
                bridge_file=str(grant_file),
            )
        assert exc_info.value.code == "validation_failed"


def test_plus_session_dtos_match_plus_api():
    client = MagicMock()
    client.call_tool.side_effect = [
        {"session": {"id": "sess-1"}},
        {"ok": True},
        {"accepted": True, "turnId": "turn-1"},
        {"session": {"id": "sess-1", "messages": []}},
    ]
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        assert harness_plus_adapter.plus_session_create(
            "/fake", project_path="/project", mode="agent", runtime_config={}
        )["session_id"] == "sess-1"
        harness_plus_adapter.plus_session_configure(
            "/fake", session_id="sess-1", mode="agent", permission_mode="ask"
        )
        harness_plus_adapter.plus_prompt("/fake", session_id="sess-1", content="hello")
        harness_plus_adapter.plus_messages("/fake", session_id="sess-1", before=8, limit=4)
    assert client.call_tool.call_args_list[0].args[1]["projectPath"] == "/project"
    assert client.call_tool.call_args_list[1].args[1] == {
        "id": "sess-1", "mode": "agent", "permissionMode": "ask", "confirm": True
    }
    assert client.call_tool.call_args_list[2].args[1] == {"sessionId": "sess-1", "content": "hello"}
    assert client.call_tool.call_args_list[3].args[1] == {
        "id": "sess-1", "messageBefore": 8, "messageLimit": 4
    }


def test_plus_catalog_provider_override_and_redaction():
    client = MagicMock()
    client.tools_list.return_value = {"pi_desktop_invoke": {}}
    client.call_tool.side_effect = [
        {"providers": [{"id": "p1", "apiKey": "secret", "models": [{"api_key": "nested"}]}]},
        {"settings": {"defaultProviderId": "other", "defaultModelId": "wrong"}},
        {"models": [{"modelId": "m1", "supportedThinkingLevels": ["low"]}], "source": "cache"},
    ]
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        result = harness_plus_adapter.plus_catalog("/fake", provider_id="p1")
    assert result["resolved_default"]["provider_id"] == "p1"
    assert "apiKey" not in result["providers"][0]
    assert "api_key" not in result["providers"][0]["models"][0]


def test_plus_proposal_resolve_uses_current_proposal_identity():
    client = MagicMock()
    client.call_tool.side_effect = [
        {"plans": [{"id": "p-1", "sessionId": "s-1", "turnId": "t-1",
                    "toolCallId": "tc-1", "version": 3, "status": "pending"}]},
        {"ok": True},
    ]
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        result = harness_plus_adapter.plus_proposal_resolve(
            "/fake", session_id="s-1", turn_id="t-1", proposal_id="p-1",
            tool_call_id="tc-1", version=3, action="reject",
            target_permission_mode="accept-edits",
        )
    assert result["resolved"] is True
    assert client.call_tool.call_args_list[1].args[1] == {
        "sessionId": "s-1", "turnId": "t-1", "proposalId": "p-1",
        "toolCallId": "tc-1", "action": "reject", "confirm": True, "version": 3,
        "targetPermissionMode": "accept-edits",
    }


def test_plus_proposal_resolve_omits_unset_target_permission():
    client = MagicMock()
    client.call_tool.side_effect = [
        {"plans": [{"id": "p-1", "sessionId": "s-1", "turnId": "t-1",
                    "toolCallId": "tc-1", "version": 1, "status": "pending"}]},
        {"ok": True},
    ]
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        harness_plus_adapter.plus_proposal_resolve(
            "/fake", session_id="s-1", turn_id="t-1", proposal_id="p-1",
            tool_call_id="tc-1", version=1, action="reject",
        )
    assert "targetPermissionMode" not in client.call_tool.call_args_list[1].args[1]


def test_plus_proposal_resolve_rejects_invalid_target_permission_before_io():
    client = MagicMock()
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        with pytest.raises(util.KanpilotError):
            harness_plus_adapter.plus_proposal_resolve(
                "/fake", session_id="s-1", turn_id="t-1", proposal_id="p-1",
                tool_call_id="tc-1", version=1, action="approve",
                target_permission_mode="dangerously",
            )
    client.call_tool.assert_not_called()


def test_plus_proposal_resolve_rejects_stale_identity_before_write():
    client = MagicMock()
    client.call_tool.return_value = {"plans": [{
        "id": "p-1", "sessionId": "s-other", "turnId": "t-1",
        "toolCallId": "tc-1", "version": 3, "status": "pending",
    }]}
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        with pytest.raises(util.KanpilotError) as error:
            harness_plus_adapter.plus_proposal_resolve(
                "/fake", session_id="s-1", turn_id="t-1", proposal_id="p-1",
                tool_call_id="tc-1", version=3, action="approve",
            )
    assert error.value.code == "conflict"
    assert client.call_tool.call_count == 1


def test_nested_plus_output_strings_redact_connection_token_without_losing_fields():
    token = "plus-token-987654321"
    client = MagicMock()
    client.call_tool.side_effect = [
        {"isRunning": True, "message": f"failure {token}", "nested": [{"text": token}]},
        {"session": {"id": "s-1", "messages": [
            {"role": "assistant", "content": f"token={token}", "sequence": 7}
        ]}, "messageStart": 2},
        {"message": f"request failed {token}", "code": "TEMP"},
    ]
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {"token": token})):
        status = harness_plus_adapter.plus_status("/fake", session_id="s-1")
        messages = harness_plus_adapter.plus_messages("/fake", session_id="s-1")
        stopped = harness_plus_adapter.plus_stop("/fake", session_id="s-1")
    assert status["raw_status"]["message"] == "failure «redacted»"
    assert status["raw_status"]["nested"][0]["text"] == "«redacted»"
    assert messages["messages"][0]["content"] == "token=«redacted»"
    assert messages["messages"][0]["sequence"] == 7
    assert stopped["detail"]["code"] == "TEMP"
    assert stopped["detail"]["message"] == "request failed «redacted»"


def test_runtime_accept_maps_to_plus_approve():
    client = MagicMock()
    client.call_tool.side_effect = [
        {"plans": [{"id": "p-1", "sessionId": "s-1", "turnId": "t-1",
                    "toolCallId": "tc-1", "version": 2, "status": "pending"}]},
        {"proposal": {"id": "p-1", "status": "approved"}},
    ]
    with patch("kanpilot.harness_plus_adapter._get_client", return_value=(client, {})):
        result = harness_plus_adapter.plus_proposal_resolve(
            "/fake", session_id="s-1", turn_id="t-1", proposal_id="p-1",
            tool_call_id="tc-1", version=2, action="accept")
    assert client.call_tool.call_args_list[1].args[1]["action"] == "approve"
    assert client.call_tool.call_args_list[1].args[1]["version"] == 2
    assert result["detail"]["proposal"]["status"] == "approved"
