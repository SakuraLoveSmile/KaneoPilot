"""Plus Harness Adapter for KaneoPilot MCP Server (protocol §5.25 / T7.5).

Maps kp_runtime_* control domain tools to the local PI-Desktop-Plus MCP control plane.
Handles session creation, configuration, prompt, status, turn inspection, pagination,
proposals, and session grant attachment.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional, Tuple

from . import constants, mcp_scopes, pi_desktop_backend, util


_SENSITIVE_CATALOG_KEYS = frozenset({
    "apiKey", "api_key", "apikey", "accessToken", "access_token",
    "token", "secret", "password", "credential", "credentials",
})


def _redact_catalog(value):
    if isinstance(value, dict):
        sensitive = {key.lower() for key in _SENSITIVE_CATALOG_KEYS}
        return {key: _redact_catalog(item) for key, item in value.items()
                if key.lower() not in sensitive}
    if isinstance(value, list):
        return [_redact_catalog(item) for item in value]
    return value


def _redact_output(value, token):
    if isinstance(value, str):
        return pi_desktop_backend._strip_secret(value, token)
    if isinstance(value, dict):
        return {key: _redact_output(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_output(item, token) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_output(item, token) for item in value)
    return value


def _get_client(connection_file: str, timeout: int = 10) -> Tuple[pi_desktop_backend.MCPClient, dict]:
    conn_path = Path(connection_file).expanduser()
    if not conn_path.is_file():
        raise util.KanpilotError("not_found", f"Plus 连接文件不存在：{connection_file}")
    record = pi_desktop_backend.load_connection(str(conn_path))
    client = pi_desktop_backend.MCPClient(record["url"], record["token"], timeout=timeout)
    client.connect()
    if client.protocol_version not in pi_desktop_backend.MCP_COMPATIBLE_VERSIONS:
        raise util.KanpilotError(
            "validation_failed",
            f"Plus MCP 协议版本不受支持：{client.protocol_version or '未知'}",
        )
    return client, record


def plus_capabilities(connection_file: str, *, timeout: int = 10) -> dict:
    """Check capabilities and readiness of local Plus MCP control interface."""
    record = {}
    try:
        client, record = _get_client(connection_file, timeout=timeout)
        tools = client.tools_list()
        missing = [t for t in constants.PI_DESKTOP_REQUIRED_TOOLS if t not in tools]
        tools_ok = len(missing) == 0
        server_name = (client.server_info or {}).get("name", "pi-desktop")
        version = (client.server_info or {}).get("version", client.protocol_version)
        detail = (
            f"PI-Desktop 控制面就绪（{server_name}，{version or '未知版本'}）"
            if tools_ok
            else f"PI-Desktop 控制面缺少必需工具：{'、'.join(missing)}"
        )
        return _redact_output({
            "available": tools_ok,
            "connected": True,
            "pid": record.get("pid"),
            "detail": detail,
            "tools_ok": tools_ok,
            "attachment_supported": "pi_session_attach_mcp" in tools,
            "version": version,
            "supported_operations": sorted(tools),
        }, record.get("token"))
    except util.KanpilotError as exc:
        return _redact_output({
            "available": False,
            "connected": False,
            "detail": exc.message,
            "tools_ok": False,
            "attachment_supported": False,
            "version": None,
            "supported_operations": [],
        }, record.get("token"))
    except Exception as exc:
        return _redact_output({
            "available": False,
            "connected": False,
            "detail": f"Plus 连接失败：{exc}",
            "tools_ok": False,
            "attachment_supported": False,
            "version": None,
            "supported_operations": [],
        }, record.get("token"))


def plus_catalog(connection_file: str, *, provider_id: Optional[str] = None, timeout: int = 10) -> dict:
    """Read provider/model catalog from local Plus cache/fallback."""
    client, record = _get_client(connection_file, timeout=timeout)
    tools = client.tools_list()
    if "pi_desktop_invoke" not in tools:
        raise util.KanpilotError("not_found", "PI-Desktop 控制面缺少 pi_desktop_invoke 工具")

    providers_res = client.call_tool("pi_desktop_invoke", {"operation": "providers/list", "args": []})
    providers_res = pi_desktop_backend._unwrap_invoke_result(providers_res)
    rows = providers_res.get("providers")
    if not isinstance(rows, list):
        raise util.KanpilotError("internal", "PI-Desktop provider 目录格式无效")

    settings_res = client.call_tool("pi_desktop_invoke", {"operation": "settings/get", "args": []})
    settings_res = pi_desktop_backend._unwrap_invoke_result(settings_res)
    if provider_id:
        source_settings = settings_res.get("settings", settings_res) if isinstance(settings_res, dict) else {}
        selected_settings = dict(source_settings) if isinstance(source_settings, dict) else {}
        selected_settings["defaultProviderId"] = provider_id
        selected_settings.pop("defaultModelId", None)
        selected_settings.pop("default_model_id", None)
        settings_res = {"settings": selected_settings}

    def fetch_models(pid: str):
        try:
            res = client.call_tool(
                "pi_desktop_invoke",
                {"operation": "providers/listModels", "args": [{"providerId": pid, "source": "cache"}]},
            )
            return res, None
        except Exception as e:
            return None, str(e)

    resolved, resolve_err = pi_desktop_backend.resolve_default_from_catalog_data(
        connection_file,
        rows,
        settings_res or {},
        models_fetcher=fetch_models,
    )

    resolved_default = None
    if resolved is not None:
        resolved_default = {
            "provider_id": resolved["provider_id"],
            "model_id": resolved["model_id"],
            "thinking_level": resolved["thinking_level"],
        }

    return _redact_output({
        "connection_file": str(Path(connection_file).expanduser()),
        "providers": _redact_catalog(rows),
        "resolved_default": resolved_default,
        "default_detail": resolve_err,
    }, record.get("token"))


def plus_session_create(
    connection_file: str,
    *,
    project_path: str,
    mode: str,
    runtime_config: dict,
    title: Optional[str] = None,
    project_name: Optional[str] = None,
    timeout: int = 10,
) -> dict:
    """Create a new session in Plus with authorized runtime config."""
    client, record = _get_client(connection_file, timeout=timeout)
    params = {
        "projectPath": str(Path(project_path).expanduser()),
        "mode": mode,
    }
    if title is not None:
        params["title"] = title
    if project_name is not None:
        params["projectName"] = project_name
    if runtime_config.get("provider_id"):
        params["providerId"] = runtime_config["provider_id"]
    if runtime_config.get("model_id"):
        params["modelId"] = runtime_config["model_id"]
    if runtime_config.get("thinking_level"):
        params["thinkingLevel"] = runtime_config["thinking_level"]

    res = client.call_tool("pi_session_create", params)
    session = res.get("session") if isinstance(res, dict) else None
    session_id = (session.get("id") if isinstance(session, dict) else None) or res.get("sessionId") or res.get("id")
    if not session_id or not isinstance(session_id, str):
        raise util.KanpilotError("internal", f"Plus session_create 未返回有效 sessionId: {res}")
    result = {"session_id": session_id}
    # 名称用于 worker 核对 Host 实际创建结果；缺失时绝不回显请求值。
    returned = session if isinstance(session, dict) else res
    for target, names in (("title", ("title", "name")),
                          ("project_name", ("projectName", "project_name"))):
        for name in names:
            if name in returned:
                result[target] = returned[name]
                break
    return _redact_output(result, record.get("token"))


def plus_session_configure(
    connection_file: str,
    *,
    session_id: str,
    mode: str,
    permission_mode: str,
    timeout: int = 10,
) -> dict:
    """Configure session mode and permission mode in Plus."""
    client, record = _get_client(connection_file, timeout=timeout)
    params = {
        "id": session_id,
        "mode": mode,
        "permissionMode": permission_mode,
        "confirm": True,
    }
    res = client.call_tool("pi_session_configure", params)
    return _redact_output({"session_id": session_id, "configured": True, "detail": res}, record.get("token"))


def plus_prompt(
    connection_file: str,
    *,
    session_id: str,
    content: str,
    attachments: Optional[list] = None,
    timeout: int = 10,
) -> dict:
    """Send a prompt to the Plus session."""
    if len(content.encode("utf-8")) > constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES:
        raise util.bad_request(
            f"prompt 正文超限（> {constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES // (1024*1024)} MiB），在发送前拒绝"
        )
    client, record = _get_client(connection_file, timeout=timeout)
    params = {
        "sessionId": session_id,
        "content": content,
    }
    if attachments:
        params["attachments"] = attachments

    res = client.call_tool("pi_agent_prompt", params)
    turn_id = res.get("turnId")
    if not turn_id:
        # Some versions return {accepted: true, turnId: ...}
        turn_id = res.get("turn_id")
    return _redact_output({
        "session_id": session_id,
        "turn_id": str(turn_id) if turn_id is not None else None,
        "accepted": bool(res.get("accepted", True)),
    }, record.get("token"))


def plus_status(connection_file: str, *, session_id: str, timeout: int = 10) -> dict:
    """Get active status of session in Plus."""
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_agent_status", {"sessionId": session_id})
    return _redact_output({
        "session_id": session_id,
        "is_running": bool(res.get("isRunning")),
        "current_turn_id": str(res.get("currentTurnId")) if res.get("currentTurnId") is not None else None,
        "pending_permission": res.get("pendingPermission"),
        "raw_status": res,
    }, record.get("token"))


def plus_turn_get(
    connection_file: str,
    *,
    session_id: str,
    turn_id: str,
    timeout: int = 10,
) -> dict:
    """Get persistent turn outcome from Plus."""
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_turn_get", {"sessionId": session_id, "turnId": turn_id})
    # Structure: {"sessionId": str, "turnId": str, "status": "running"|"completed"|"error"|"aborted", "endedAt": ...}
    ret_session_id = res.get("sessionId") or res.get("session_id")
    ret_turn_id = res.get("turnId") or res.get("turn_id")
    status = res.get("status", "unknown")
    if ret_session_id and ret_session_id != session_id:
        raise util.KanpilotError("conflict", f"Plus turn_get 返回 sessionId 不匹配：{ret_session_id} != {session_id}")
    if ret_turn_id and ret_turn_id != turn_id:
        raise util.KanpilotError("conflict", f"Plus turn_get 返回 turnId 不匹配：{ret_turn_id} != {turn_id}")
    return _redact_output({
        "session_id": session_id,
        "turn_id": turn_id,
        "status": status,
        "ended_at": res.get("endedAt") or res.get("ended_at"),
        "error_code": res.get("errorCode") or res.get("error_code"),
    }, record.get("token"))


def plus_messages(
    connection_file: str,
    *,
    session_id: str,
    before: Optional[int] = None,
    limit: Optional[int] = None,
    timeout: int = 10,
) -> dict:
    """Read physical paginated transcript messages from Plus."""
    client, record = _get_client(connection_file, timeout=timeout)
    params: dict = {"id": session_id}
    if before is not None:
        params["messageBefore"] = before
    if limit is not None:
        params["messageLimit"] = limit

    res = client.call_tool("pi_session_get", params)
    if res.get("truncated"):
        # The control plane's preview is incomplete and may contain transcript
        # content. Preserve only the signal needed for the reader to shrink pages.
        return _redact_output({
            "session_id": session_id,
            "messages": [],
            "truncated": True,
            "reason": str(res.get("reason") or "MCP_RESULT_LIMIT"),
        }, record.get("token"))
    session_data = res.get("session", res)
    messages = session_data.get("messages", [])
    return _redact_output({
        "session_id": session_id,
        "messages": messages,
        "message_start": session_data.get("messageStart"),
        "message_end": session_data.get("messageEnd"),
        "has_more_before": bool(session_data.get("hasMoreBefore")),
        "has_more_after": bool(session_data.get("hasMoreAfter")),
    }, record.get("token"))


def plus_goal_report_list(connection_file: str, *, session_id: str,
                          timeout: int = 10) -> dict:
    """Read Plus report candidates without changing report state or identity."""
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_goal_report_list", {"sessionId": session_id})
    return _redact_output(res, record.get("token"))


def plus_goal_report_get(connection_file: str, *, session_id: str,
                         execution_id: Optional[str] = None,
                         report_id: Optional[str] = None,
                         timeout: int = 10) -> dict:
    """Read the selected Plus report, preserving its body, state and hash."""
    if not (execution_id or report_id):
        raise util.bad_request("Goal report get requires execution_id or report_id")
    params = {"sessionId": session_id}
    if execution_id is not None:
        params["executionId"] = execution_id
    if report_id is not None:
        params["reportId"] = report_id
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_goal_report_get", params)
    return _redact_output(res, record.get("token"))


def plus_stop(connection_file: str, *, session_id: str, timeout: int = 10) -> dict:
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_agent_stop", {"sessionId": session_id})
    return _redact_output({"session_id": session_id, "requested": True, "detail": res}, record.get("token"))


def plus_abort(connection_file: str, *, session_id: str, timeout: int = 10) -> dict:
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_agent_abort", {"sessionId": session_id})
    return _redact_output({"session_id": session_id, "requested": True, "detail": res}, record.get("token"))


def plus_proposals(
    connection_file: str,
    *,
    session_id: str,
    turn_id: Optional[str] = None,
    timeout: int = 10,
) -> dict:
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_plans_pending", {"sessionId": session_id})
    plans = res.get("plans", res) if isinstance(res, dict) else []
    if not isinstance(plans, list):
        plans = []
    if turn_id:
        plans = [p for p in plans if isinstance(p, dict) and p.get("turnId") == turn_id]
    history = res.get("history", []) if isinstance(res, dict) else []
    if not isinstance(history, list):
        history = []
    if turn_id:
        history = [p for p in history if isinstance(p, dict) and p.get("turnId") == turn_id]
    return _redact_output({"session_id": session_id, "proposals": plans, "history": history}, record.get("token"))


def plus_proposal_resolve(
    connection_file: str,
    *,
    session_id: str,
    turn_id: str,
    proposal_id: str,
    tool_call_id: str,
    version: int,
    action: str,
    target_permission_mode: Optional[str] = None,
    timeout: int = 10,
) -> dict:
    if target_permission_mode is not None and target_permission_mode not in constants.EXEC_PERMISSION_MODES:
        raise util.bad_request("target_permission_mode 必须为 ask、accept-edits 或 auto")
    client, record = _get_client(connection_file, timeout=timeout)
    pending = client.call_tool("pi_plans_pending", {"sessionId": session_id})
    proposals = pending.get("plans", []) if isinstance(pending, dict) else []
    proposal = next((item for item in proposals if isinstance(item, dict)
                     and item.get("id") == proposal_id), None)
    if proposal is None:
        raise util.KanpilotError("not_found", f"Plus proposal 不存在：{proposal_id}")
    proposal_version = proposal.get("version")
    if (proposal.get("status") != "pending"
            or not isinstance(version, int) or isinstance(version, bool) or version <= 0
            or not isinstance(proposal_version, int) or isinstance(proposal_version, bool)
            or proposal.get("sessionId") != session_id or proposal.get("turnId") != turn_id
            or proposal.get("toolCallId") != tool_call_id
            or proposal_version != version):
        raise util.KanpilotError("conflict", "Plus proposal 的 session/turn/version/toolCallId 与请求不匹配")
    args = {
        "sessionId": session_id,
        "turnId": turn_id,
        "proposalId": proposal_id,
        "toolCallId": tool_call_id,
        "action": "approve" if action == "accept" else action,
        "confirm": True,
        "version": version,
    }
    if target_permission_mode is not None:
        args["targetPermissionMode"] = target_permission_mode
    res = client.call_tool("pi_plans_resolve", args)
    return _redact_output({
        "session_id": session_id, "proposal_id": proposal_id,
        "action": action, "resolved": True, "detail": res,
    }, record.get("token"))


def plus_attach_mcp(
    connection_file: str,
    *,
    session_id: str,
    bridge_file: str,
    timeout: int = 10,
) -> dict:
    """Attach session-scoped KaneoPilot MCP grant to Plus session."""
    client, record = _get_client(connection_file, timeout=timeout)
    tools = client.tools_list()
    if "pi_session_attach_mcp" not in tools:
        raise util.KanpilotError(
            "validation_failed",
            "Plus 控制面缺少 pi_session_attach_mcp 工具（需升级 Plus 以支持会话级 MCP 挂接）",
        )
    bridge_path = Path(bridge_file).expanduser()
    if bridge_path.is_symlink():
        raise util.KanpilotError("validation_failed", f"MCP grant bridge 文件不能是符号链接：{bridge_file}")
    if not bridge_path.is_file():
        raise util.KanpilotError("not_found", f"MCP grant bridge 文件不存在：{bridge_file}")

    st = bridge_path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise util.KanpilotError("validation_failed", f"MCP grant bridge 路径必须是普通文件：{bridge_file}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise util.KanpilotError("validation_failed", f"MCP grant bridge 文件所有者不符（必须为当前用户）：{bridge_file}")
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
        raise util.KanpilotError("validation_failed", f"MCP grant bridge 文件权限必须为 0600 owner-only：{bridge_file}")

    try:
        grant_content = json.loads(bridge_path.read_text(encoding="utf-8"))
        if not isinstance(grant_content, dict):
            raise ValueError("grant 必须为 JSON 对象")
        parsed_url = urlsplit(grant_content.get("url", ""))
        if (parsed_url.scheme != "http" or not parsed_url.hostname
                or parsed_url.username or parsed_url.password
                or not pi_desktop_backend._is_loopback_host(parsed_url.hostname)):
            raise util.KanpilotError("validation_failed", "MCP grant URL 必须是无凭据的 HTTP loopback 地址")
        if grant_content.get("domain") != "task":
            raise util.KanpilotError("validation_failed", "Plus 仅能挂接 task 域 grant")
        role = grant_content.get("role")
        if role not in mcp_scopes.TASK_ROLE_TOOLS:
            raise util.KanpilotError("validation_failed", "MCP grant role 无效")
        if grant_content.get("server_name") != constants.MCP_SERVER_NAME:
            raise util.KanpilotError("validation_failed", "MCP grant server_name 不匹配")
        if grant_content.get("protocol_version") not in constants.MCP_COMPATIBLE_VERSIONS:
            raise util.KanpilotError("validation_failed", "MCP grant protocol_version 不受支持")
        if not grant_content.get("scope_id") or not grant_content.get("token") or not grant_content.get("epoch"):
            raise util.KanpilotError("validation_failed", "MCP grant 缺少 scope_id、epoch 或 token")
    except Exception as exc:
        if isinstance(exc, util.KanpilotError):
            raise
        raise util.KanpilotError("validation_failed", f"MCP grant bridge 文件解析失败：{exc}") from exc

    res = client.call_tool(
        "pi_session_attach_mcp",
        {
            "sessionId": session_id,
            "bridgeFile": str(bridge_path),
            "confirm": True,
        },
    )
    if not isinstance(res, dict):
        raise util.KanpilotError("validation_failed", f"pi_session_attach_mcp 返回类型无效：{type(res)}")
    res = _redact_output(res, record.get("token"))
    if res.get("attached") is not True:
        raise util.KanpilotError("validation_failed", f"pi_session_attach_mcp 挂接失败或未返回 attached: true：{res}")
    if res.get("sessionId") != session_id:
        raise util.KanpilotError("validation_failed", "pi_session_attach_mcp 返回 sessionId 不匹配")
    server_name = res.get("serverName")
    if server_name != constants.MCP_SERVER_NAME:
        raise util.KanpilotError("validation_failed", f"pi_session_attach_mcp serverName 不匹配：{server_name}")
    tool_names = res.get("toolNames")
    if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names):
        raise util.KanpilotError("validation_failed", f"pi_session_attach_mcp 缺少有效 toolNames 列表：{res}")
    if res.get("role") != role:
        raise util.KanpilotError("validation_failed", "pi_session_attach_mcp 返回 role 与 grant 不匹配")
    expected_tools = mcp_scopes.TASK_ROLE_TOOLS[role]
    if set(tool_names) != expected_tools:
        missing = expected_tools.difference(tool_names)
        extra = set(tool_names).difference(expected_tools)
        detail = []
        if missing:
            detail.append(f"缺少：{'、'.join(sorted(missing))}")
        if extra:
            detail.append(f"多出：{'、'.join(sorted(extra))}")
        raise util.KanpilotError("validation_failed", f"Plus 挂接的 {role} grant 工具清单不匹配（{'；'.join(detail)}）")

    return _redact_output({
        "session_id": session_id,
        "attached": True,
        "server_name": server_name,
        "role": role,
        "tool_names": tool_names,
    }, record.get("token"))
