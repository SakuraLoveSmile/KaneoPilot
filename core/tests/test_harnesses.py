"""Isolated static registry, evidence capability, and runtime routing contracts."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kanpilot import (constants, db, harnesses, harness_plus_adapter, mcp_scopes,
                      mcp_tools, runtime_client, runtime_operations, util)


@pytest.mark.parametrize("harness_id", ["other", "pi", "plugin://custom"])
def test_unknown_harness_is_explicit_and_never_probed(harness_id):
    with patch.object(harness_plus_adapter, "_get_client") as connect:
        with pytest.raises(util.KanpilotError) as caught:
            harnesses.get_adapter(harness_id)
    assert caught.value.code == "unsupported_harness"
    connect.assert_not_called()


@pytest.mark.parametrize("harness_id", ["codex", "claude"])
@pytest.mark.parametrize("role", ["plan", "review"])
def test_cli_describes_existing_readonly_runner_without_claiming_connection(harness_id, role):
    with patch.object(harness_plus_adapter, "_get_client") as connect:
        result = harnesses.describe_cli_harness(harness_id, role=role)
    assert result["contract_version"] == "kp-harness-v1"
    assert result["harness_id"] == harness_id
    assert result["connected"] is None
    assert set(result["capabilities"]) == set(harnesses.CAPABILITY_NAMES)
    assert result["capabilities"]["structured_result"]["state"] == "supported"
    assert result["capabilities"]["read_only_workspace"]["state"] == "supported"
    with pytest.raises(util.KanpilotError):
        harnesses.require_capabilities(result, ["structured_result"])
    connect.assert_not_called()


@pytest.mark.parametrize("harness_id", ["codex", "claude"])
def test_cli_has_no_runtime_control_or_execution_role(harness_id):
    assert harnesses.supports_role(harness_id, "plan")
    assert harnesses.supports_role(harness_id, "review")
    assert not harnesses.supports_role(harness_id, "execution")
    assert not harnesses.supports_role("pi", "execution")
    with pytest.raises(util.KanpilotError) as caught:
        harnesses.get_runtime_adapter(harness_id)
    assert caught.value.code == "unsupported_harness"
    with pytest.raises(util.KanpilotError) as caught:
        harnesses.describe_cli_harness(harness_id, role="execution", connected=True)
    assert caught.value.code == "unsupported_role"


def test_required_role_capabilities_fail_without_fallback():
    description = harnesses.describe_cli_harness("claude", role="review", connected=True)
    harnesses.require_capabilities(description, ["structured_result", "read_only_workspace"])
    with pytest.raises(util.KanpilotError) as caught:
        harnesses.require_capabilities(description, ["verification_report"])
    assert caught.value.code == "unsupported_capabilities"
    assert "verification_report" in caught.value.message


@pytest.mark.parametrize("value", [None, [], {"structured_result": {"state": "supported"}}])
def test_malformed_capability_description_fails_closed(value):
    description = {
        "contract_version": "kp-harness-v1", "connected": True,
        "capabilities": value,
    }
    with pytest.raises(util.KanpilotError) as caught:
        harnesses.require_capabilities(description, ["structured_result"])
    assert caught.value.code == "unsupported_capabilities"


def plus_description(tools, binding=None):
    client = MagicMock()
    client.server_info = {"name": "fixture", "version": "test"}
    client.tools_list.return_value = tools
    client.supports_required_verification_commands.return_value = binding
    with patch.object(harness_plus_adapter, "_get_client", return_value=(client, {})):
        return harnesses.get_runtime_adapter("pi_desktop").capabilities("/fixture")


def test_plus_capabilities_keep_legacy_fields_and_detect_missing_tools():
    result = plus_description({"pi_turn_get": {}})
    assert result["connected"] is True
    assert result["available"] is False
    assert result["tools_ok"] is False
    assert result["supported_operations"] == ["pi_turn_get"]
    caps = result["capabilities"]
    assert caps["durable_status"]["state"] == "supported"
    assert caps["structured_result"]["state"] == "unsupported"
    assert caps["verification_report"]["state"] == "unsupported"
    assert caps["verification_command_binding"]["state"] == "unsupported"
    assert caps["input_requests"]["state"] == "unsupported"


def test_report_and_binding_do_not_claim_independent_command_evidence():
    tools = dict.fromkeys(constants.PI_DESKTOP_PLAN_TOOLS + constants.PI_DESKTOP_GOAL_REPORT_TOOLS)
    result = plus_description(tools, binding=True)
    harnesses.require_capabilities(result, ["verification_report", "verification_command_binding"])
    assert result["required_verification_commands_supported"] is True
    assert result["capabilities"]["observed_command_results"]["state"] == "unsupported"
    assert result["capabilities"]["read_only_workspace"]["state"] == "unknown"
    assert result["capabilities"]["stop_confirmation"]["state"] == "unsupported"
    with pytest.raises(util.KanpilotError):
        harnesses.require_capabilities(result, ["observed_command_results"])


def test_unknown_schema_and_offline_do_not_become_supported():
    online = plus_description({"pi_plans_resolve": {}}, binding=None)
    assert online["capabilities"]["verification_command_binding"]["state"] == "unknown"
    with patch.object(harness_plus_adapter, "_get_client", side_effect=util.KanpilotError("not_found", "offline")):
        offline = harness_plus_adapter.plus_capabilities("/fixture")
    assert offline["connected"] is False
    assert offline["capabilities"]["durable_status"]["state"] == "unknown"
    with pytest.raises(util.KanpilotError):
        harnesses.require_capabilities(offline, ["durable_status"])


def test_scope_identity_cannot_be_changed_by_arguments_or_runtime_config():
    scope = SimpleNamespace(runtime_config={"harness_id": "claude"})
    assert harnesses.runtime_adapter_for_scope(scope, {}).harness_id == "pi_desktop"
    with pytest.raises(util.KanpilotError) as caught:
        harnesses.runtime_adapter_for_scope(scope, {"harness_id": "claude"})
    assert caught.value.code == "forbidden"


def runtime_env(tmp_path, role="execution"):
    manager = mcp_scopes.ScopeManager(app_support_dir=tmp_path / "app")
    scope = mcp_scopes.Scope(
        "scope", "invocation", role, util.to_iso(util.now_dt() + timedelta(hours=1)),
        project_path=str(tmp_path), runtime_config={"permission_mode": "auto"},
        connection_file=str(tmp_path / "fixture.json"))
    manager.scopes[scope.scope_id] = scope
    return manager, scope


def test_mcp_capabilities_uses_authorized_registry_and_preserves_role_boundary(tmp_path):
    manager, scope = runtime_env(tmp_path)
    description = plus_description({})
    with patch.object(harness_plus_adapter, "plus_capabilities", return_value=description) as probe:
        assert mcp_tools.dispatch_tool_call(tmp_path / "test.db", manager, "runtime", scope,
            "kp_runtime_capabilities", {"scope_id": "scope"}) == description
        with pytest.raises(util.KanpilotError) as caught:
            mcp_tools.dispatch_tool_call(tmp_path / "test.db", manager, "runtime", scope,
                "kp_runtime_capabilities", {"scope_id": "scope", "harness_id": "claude"})
        assert caught.value.code == "forbidden"
        with pytest.raises(util.KanpilotError):
            mcp_tools.dispatch_tool_call(tmp_path / "test.db", manager, "task", scope,
                "kp_runtime_capabilities", {"scope_id": "scope"})
    probe.assert_called_once_with(str(tmp_path / "fixture.json"))


def test_registry_writer_preserves_unknown_receipt_and_never_resends(tmp_path):
    manager, scope = runtime_env(tmp_path)
    args = {"scope_id": "scope", "operation_id": "create", "mode": "goal", "project_path": str(tmp_path)}
    with patch.object(harness_plus_adapter, "plus_session_create", side_effect=util.KanpilotError("timeout", "unknown")) as send:
        with pytest.raises(util.KanpilotError) as caught:
            mcp_tools.dispatch_tool_call(tmp_path / "test.db", manager, "runtime", scope,
                                        "kp_runtime_session_create", args)
        assert caught.value.code == "timeout"
        with pytest.raises(util.KanpilotError) as caught:
            mcp_tools.dispatch_tool_call(tmp_path / "test.db", manager, "runtime", scope,
                                        "kp_runtime_session_create", args)
        assert caught.value.code == "conflict"
        assert "do not resend" in caught.value.message
    send.assert_called_once()
    conn = db.connect(tmp_path / "test.db")
    try:
        receipt = runtime_operations.get_operation(conn, "scope", "create")
        assert receipt["status"] == "unknown"
        assert receipt["harness_id"] == "pi_desktop"
    finally:
        conn.close()


def test_neutral_observation_does_not_treat_idle_as_completed(tmp_path):
    facade = runtime_client.RuntimeClient(role="execution", connection_file="/fixture",
        project_path=str(tmp_path), runtime_config={}, invocation_id="observe")
    with patch.object(facade, "_call", return_value={
        "session_id": "session", "is_running": False, "current_turn_id": None,
        "pending_permission": None}) as call:
        result = facade.observe_status("session")
    assert result["state"] == "idle"
    assert result["turn_id"] is None
    assert result["input_requests"] is None
    call.assert_called_once_with("kp_runtime_status", {"session_id": "session"})
