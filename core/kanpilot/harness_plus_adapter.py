"""Plus Harness Adapter for KaneoPilot MCP Server (protocol §5.25 / T7.5).

Maps kp_runtime_* control domain tools to the local PI-Desktop-Plus MCP control plane.
Handles session creation, configuration, prompt, status, turn inspection, pagination,
proposals, and session grant attachment.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional, Tuple

from . import constants, harnesses, mcp_scopes, pi_desktop_backend, util


def _semantic_capabilities(tools=None, command_binding=None) -> dict:
    """Describe mechanisms only; a report tool is not independent command evidence."""
    def tool_capability(required, detail):
        state = "unknown" if tools is None else (
            "supported" if set(required).issubset(tools) else "unsupported")
        return harnesses.capability(state, "host", detail)

    return {
        "structured_result": tool_capability(
            ("pi_turn_get", "pi_session_get"), "适配器规范化结构化状态；不保证模型正文正确"),
        "durable_status": tool_capability(("pi_turn_get",), "持久轮次状态查询机制"),
        "verification_report": tool_capability(
            ("pi_goal_report_list", "pi_goal_report_get"), "Goal 结构化验证报告读取机制"),
        "verification_command_binding": harnesses.capability(
            "unknown" if command_binding is None else ("supported" if command_binding else "unsupported"),
            "host", "由 pi_plans_resolve schema 的 requiredVerificationCommands 能力确认"),
        "observed_command_results": harnesses.capability(
            "unsupported", "adapter", "当前适配器没有独立采集命令退出码；模型报告不构成独立执行证据"),
        "input_requests": tool_capability(
            ("pi_asks_pending", "pi_asks_resolve"), "宿主执行问题查询及回应机制"),
        "stop_confirmation": harnesses.capability(
            "unknown" if tools is None else "unsupported", "adapter",
            "停止请求回执与状态读取不证明所有工具及子进程已停止"),
        "read_only_workspace": harnesses.capability(
            "unknown", "host", "需要宿主强制只读保障；plan 模式本身不构成只读证明"),
    }


def _capability_envelope(*, connected, tools=None, command_binding=None) -> dict:
    return {
        "contract_version": harnesses.CONTRACT_VERSION, "harness_id": "pi_desktop",
        "adapter_version": "1", "connected": connected,
        "capabilities": _semantic_capabilities(tools, command_binding),
    }


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
        binding = client.supports_required_verification_commands() if "pi_plans_resolve" in tools else False
        binding = binding if isinstance(binding, bool) else None
        return _redact_output({
            **_capability_envelope(connected=True, tools=tools, command_binding=binding),
            "available": tools_ok,
            "connected": True,
            "pid": record.get("pid"),
            "detail": detail,
            "tools_ok": tools_ok,
            "attachment_supported": "pi_session_attach_mcp" in tools,
            "version": version,
            "supported_operations": sorted(tools),
            "required_verification_commands_supported": binding is True,
        }, record.get("token"))
    except util.KanpilotError as exc:
        return _redact_output({
            **_capability_envelope(connected=False),
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
            **_capability_envelope(connected=False),
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
    interaction_mode: Optional[str] = None,
    timeout: int = 10,
) -> dict:
    """Create a new session in Plus with authorized runtime config."""
    client, record = _get_client(connection_file, timeout=timeout)
    params = {
        "projectPath": str(Path(project_path).expanduser()),
        "mode": mode,
    }
    if interaction_mode is not None:
        params["interactionMode"] = interaction_mode
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
    for key in ("interactionMode", "interactionPolicyVersion"):
        if key in returned:
            result[key] = returned[key]
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
    interaction_mode: Optional[str] = None,
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
    if interaction_mode is not None:
        params["interactionMode"] = interaction_mode
    res = client.call_tool("pi_session_configure", params)
    return _redact_output({"session_id": session_id, "configured": True, "detail": res,
                           **({"session": res["session"]} if isinstance(res.get("session"), dict) else {})}, record.get("token"))


def plus_prompt(
    connection_file: str,
    *,
    session_id: str,
    content: str,
    attachments: Optional[list] = None,
    expected_interaction_mode: Optional[str] = None,
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
    if expected_interaction_mode is not None:
        params["expectedInteractionMode"] = expected_interaction_mode
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
    if not isinstance(res, dict):
        raise util.KanpilotError("validation_failed", "Plus 返回的运行状态格式无效")
    status = res.get("status", res)
    if not isinstance(status, dict) or not isinstance(status.get("isRunning"), bool):
        raise util.KanpilotError("validation_failed", "Plus 返回的运行状态不完整，无法确认当前状态")
    # New hosts wrap the status object; legacy hosts return the same fields flat.
    # Do not combine conflicting envelopes or coerce missing/string booleans.
    fields = ("sessionId", "isRunning", "currentTurnId",
              "pendingPermission", "pendingToolConfirmations", "planningState", "pendingInputs")
    if status is not res and any(key in res and (key not in status or
            type(res[key]) is not type(status[key]) or res[key] != status[key]) for key in fields):
        raise util.KanpilotError("validation_failed", "Plus 返回的内外层运行状态不一致")
    status = dict(status)
    for primary in ("sessionId", "currentTurnId"):
        if primary in status and (status[primary] is not None or primary == "sessionId"):
            if not isinstance(status[primary], str) or not status[primary].strip():
                raise util.KanpilotError("validation_failed", "Plus 返回的会话或轮次身份无效")
    returned_session = status.get("sessionId", session_id)
    if returned_session != session_id:
        raise util.KanpilotError("conflict", "Plus 返回的会话身份不匹配")

    permission = status.get("pendingPermission")
    if permission is not None and not isinstance(permission, (bool, dict, list)):
        raise util.KanpilotError("validation_failed", "Plus 返回的权限等待状态格式无效")
    if "pendingToolConfirmations" in status:
        confirmations = status["pendingToolConfirmations"]
        if (isinstance(confirmations, bool) or not isinstance(confirmations, (int, list))
                or isinstance(confirmations, int) and confirmations < 0):
            raise util.KanpilotError("validation_failed", "Plus 返回的工具确认状态格式无效")
        waiting = bool(confirmations)
        if waiting and not permission:
            permission = True
    return _redact_output({
        "session_id": returned_session,
        "is_running": status["isRunning"],
        "current_turn_id": status.get("currentTurnId"),
        "pending_permission": _redact_catalog(permission) if permission else None,
        "raw_status": _redact_catalog(status),
        **({"pending_inputs":status["pendingInputs"]} if "pendingInputs" in status else {}),
    }, record.get("token"))


def plus_turn_get(
    connection_file: str,
    *,
    session_id: str,
    turn_id: str,
    timeout: int = 10,
    require_identity: bool = False,
) -> dict:
    """Get persistent turn outcome from Plus."""
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_turn_get", {"sessionId": session_id, "turnId": turn_id})
    if require_identity or (isinstance(res, dict) and res.get("errorCode") == "INTERACTION_REQUIRED"):
        if not isinstance(res, dict):
            raise util.KanpilotError("validation_failed", "Plus 返回的轮次状态格式无效")
        if res.get("sessionId") != session_id or res.get("turnId") != turn_id:
            raise util.KanpilotError("conflict", "Plus 返回的轮次身份缺失或不匹配")
        if res.get("status") not in ("running", "completed", "error", "aborted"):
            raise util.KanpilotError("validation_failed", "Plus 返回的轮次状态无效")
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
        "started_at": res.get("startedAt") or res.get("started_at"),
        "ended_at": res.get("endedAt") or res.get("ended_at"),
        "error_code": res.get("errorCode") or res.get("error_code"),
        "error_details": res.get("errorDetails") or res.get("error_details"),
    }, record.get("token"))


_TRANSCRIPT_CHUNK_BYTES = 64 * 1024
_TRANSCRIPT_PAGE_MAX_BYTES = 16 * 1024 * 1024


def _read_transcript_chunks(client, session_id: str, before: Optional[int]) -> dict:
    """Reassemble one oversized physical message without clipping its content."""
    body = bytearray()
    digest = None
    total = None
    # The server emits fixed-size chunks; cap requests as well as total memory.
    for _ in range(_TRANSCRIPT_PAGE_MAX_BYTES // _TRANSCRIPT_CHUNK_BYTES):
        args = {"id": session_id, "offset": len(body)}
        if before is not None:
            args["messageBefore"] = before
        if digest is not None:
            args["expectedSha256"] = digest
        result = client.call_tool("pi_session_get_chunk", args)
        chunk_digest = result.get("sha256")
        chunk_total = result.get("totalBytes")
        if (result.get("sessionId") != session_id
                or type(result.get("offset")) is not int
                or result["offset"] != len(body)
                or result.get("encoding") != "base64"
                or type(chunk_total) is not int
                or not 0 < chunk_total <= _TRANSCRIPT_PAGE_MAX_BYTES
                or not isinstance(chunk_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", chunk_digest) is None):
            raise util.KanpilotError("protocol_error", "转录分片身份或边界无效")
        if digest is not None and (digest != chunk_digest or total != chunk_total):
            raise util.KanpilotError("conflict", "转录在分片读取期间变化，未采用不完整证据")
        digest, total = chunk_digest, chunk_total
        encoded = result.get("chunk")
        if (not isinstance(encoded, str)
                or len(encoded) > ((_TRANSCRIPT_CHUNK_BYTES + 2) // 3) * 4
                or "nextOffset" not in result):
            raise util.KanpilotError("protocol_error", "转录分片大小或终点无效")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise util.KanpilotError("protocol_error", "转录分片编码无效")
        end = len(body) + len(chunk)
        next_offset = result.get("nextOffset")
        if (not 0 < len(chunk) <= _TRANSCRIPT_CHUNK_BYTES or end > total
                or (end == total and next_offset is not None)
                or (end < total and (type(next_offset) is not int or next_offset != end))):
            raise util.KanpilotError("protocol_error", "转录分片链不完整")
        body.extend(chunk)
        if end == total:
            if hashlib.sha256(body).hexdigest() != digest:
                raise util.KanpilotError("protocol_error", "转录分片完整性校验失败")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise util.KanpilotError("protocol_error", "转录分片不是完整 UTF-8 JSON")
            page = payload.get("session") if isinstance(payload, dict) else None
            if not isinstance(page, dict) or page.get("id") != session_id:
                raise util.KanpilotError("conflict", "转录分片会话身份不符")
            rows = page.get("messages")
            start, finish = page.get("messageStart"), page.get("messageEnd")
            if (not isinstance(rows, list) or len(rows) > 1
                    or type(start) is not int or type(finish) is not int
                    or start < 0 or finish - start != len(rows)
                    or (not rows and (start != 0 or finish != 0))
                    or (before is not None and finish != before)
                    or not isinstance(page.get("hasMoreBefore"), bool)
                    or not isinstance(page.get("hasMoreAfter"), bool)
                    or page.get("hasMoreBefore") != (start > 0)
                    or (before is None and page["hasMoreAfter"])):
                raise util.KanpilotError("protocol_error", "转录分片物理分页不完整")
            return payload
    raise util.KanpilotError("protocol_error", "转录分片数量超过上限")


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
    if (res.get("truncated") and limit == 1
            and "pi_session_get_chunk" in client.tools_list()):
        res = _read_transcript_chunks(client, session_id, before)
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
    if not isinstance(session_data, dict):
        raise util.KanpilotError("not_found", "Plus 会话已不存在，无法读取完整转录")
    if session_data.get("id") not in (None, session_id):
        raise util.KanpilotError("conflict", "Plus 转录会话身份不符")
    messages = session_data.get("messages", [])
    return _redact_output({
        "session_id": session_id,
        "messages": messages,
        **{key: session_data[key] for key in ("interactionMode", "interactionPolicyVersion") if key in session_data and session_data.get("id") == session_id},
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
    required_verification_commands: Optional[list] = None,
    timeout: int = 10,
) -> dict:
    pi_desktop_backend.validate_required_verification_commands(required_verification_commands, action)
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
    if required_verification_commands is not None:
        if required_verification_commands and proposal.get("kind") != "goal":
            raise util.bad_request("验证命令要求仅适用于 Goal 提案")
        if required_verification_commands and not client.supports_required_verification_commands():
            raise util.KanpilotError("unsupported_tools", "请升级 Plus：pi_plans_resolve 缺少 requiredVerificationCommands 能力")
        args["requiredVerificationCommands"] = list(required_verification_commands)
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


def plus_asks_pending(connection_file, *, session_id, timeout=10):
    client, record = _get_client(connection_file, timeout=timeout)
    res = client.call_tool("pi_asks_pending", {"sessionId": session_id})
    if (not isinstance(res, dict) or res.get("sessionId") != session_id
            or not isinstance(res.get("asks"), list)):
        raise util.KanpilotError("protocol_error", "Plus pending inputs 身份或格式不符")
    return _redact_output({"session_id":session_id,"asks":res["asks"]},record.get("token"))


def plus_asks_resolve(connection_file, *, session_id, request_id, expected_tool_call_id, answers, timeout=10):
    from .execution_questions import validate_wire_answers
    validate_wire_answers(answers,session_id,request_id,expected_tool_call_id)
    client, record = _get_client(connection_file, timeout=timeout)
    res=client.call_tool("pi_asks_resolve", {"sessionId":session_id,"requestId":request_id,
        "expectedToolCallId":expected_tool_call_id,"answers":answers,"confirm":True})
    if not isinstance(res,dict) or res.get("requestId")!=request_id or res.get("status") not in ("answered","not_pending"):
        raise util.KanpilotError("protocol_error", "Plus execution answer receipt 格式不符")
    return _redact_output({"session_id":session_id,"request_id":request_id,"status":res["status"]},record.get("token"))


def validate_runtime_policy(conn, scope, tool, args):
    """Native Plus mode, permission, proposal, and interaction interpretation."""
    from . import runtime_operations
    session_id = args.get("session_id")
    mode = {"planner": "plan", "review": "plan", "execution": "goal"}.get(scope.role, "agent")
    if tool in ("kp_runtime_session_create", "kp_runtime_session_configure") and scope.role in ("planner", "review"):
        if args.get("interaction_mode") != "unattended":
            raise util.KanpilotError("forbidden", "planner/Review require unattended interaction policy")
    if tool == "kp_runtime_session_create" and args["mode"] != mode:
        raise util.KanpilotError("forbidden", "session mode does not match role")
    if tool == "kp_runtime_proposal_resolve" and args.get("goal_continuation"):
        receipt = runtime_operations.get_operation(conn, scope.scope_id, args["operation_id"])
        if receipt is None or receipt["status"] != "accepted":
            from .pi_desktop_worker import _pending_goal_contract
            status = plus_status(scope.connection_file, session_id=session_id)
            raw = status.get("raw_status") or {}
            inner = raw.get("status") or raw
            proposal = _pending_goal_contract(
                {"plans": plus_proposals(scope.connection_file, session_id=session_id).get("proposals")},
                session_id, args["turn_id"])
            if (status.get("current_turn_id") != args["turn_id"]
                    or inner.get("planningState") not in ("planning", "awaiting_approval")
                    or status.get("pending_permission") or proposal is None
                    or any(proposal.get(source) != args.get(target) for source, target in
                           (("id", "proposal_id"), ("version", "version"), ("toolCallId", "tool_call_id")))):
                raise util.KanpilotError("conflict", "当前目标提案已变化或不再可自动批准")
    if tool == "kp_runtime_session_configure":
        permission = (scope.runtime_config or {}).get("permission_mode")
        if scope.role == "review" and permission not in (
                constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
                constants.PI_DESKTOP_REVIEW_PERMISSION_MODE):
            raise util.KanpilotError("forbidden", "unsupported review permission snapshot")
        elif scope.role == "planner" and permission not in (
                constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
                constants.PI_DESKTOP_PLANNER_PERMISSION_MODE):
            raise util.KanpilotError("forbidden", "unsupported planner permission snapshot")
        if args["mode"] != mode or args["permission_mode"] != permission:
            raise util.KanpilotError("forbidden", "configuration exceeds authorized snapshot")
    if args.get("target_permission_mode") is not None and (scope.role != "execution" or args["target_permission_mode"] != (scope.runtime_config or {}).get("permission_mode")):
        raise util.KanpilotError("forbidden", "proposal permission exceeds authorized snapshot")
    if tool == "kp_runtime_prompt":
        if scope.role in ("planner", "review"):
            if args.get("expected_interaction_mode") != "unattended":
                raise util.KanpilotError("forbidden", "planner/Review prompt require atomic unattended precondition")
            from . import plus_interaction
            connection_file = (scope.connection_file or (scope.runtime_config or {}).get("connection_file")
                               or str(Path(constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE).expanduser()))
            policy = plus_messages(connection_file, session_id=session_id, limit=1)
            session = {"id": policy.get("session_id"), **{
                key: policy[key] for key in ("interactionMode", "interactionPolicyVersion") if key in policy}}
            plus_interaction.require_unattended({"session": session}, session_id)


class PlusAdapter:
    """Runtime interface implemented by the statically registered Plus harness."""
    harness_id = "pi_desktop"
    adapter_version = "1"
    runtime_control = True
    validate_runtime_policy = staticmethod(validate_runtime_policy)
    validate_verification_commands = staticmethod(pi_desktop_backend.validate_required_verification_commands)
    _operations = {
        'capabilities': 'plus_capabilities',
        'catalog': 'plus_catalog',
        'input_pending': 'plus_asks_pending',
        'status': 'plus_status',
        'turn_get': 'plus_turn_get',
        'messages': 'plus_messages',
        'proposals': 'plus_proposals',
        'goal_report_list': 'plus_goal_report_list',
        'goal_report_get': 'plus_goal_report_get',
        'session_create': 'plus_session_create',
        'session_configure': 'plus_session_configure',
        'attach_mcp': 'plus_attach_mcp',
        'prompt': 'plus_prompt',
        'input_respond': 'plus_asks_resolve',
        'stop': 'plus_stop',
        'abort': 'plus_abort',
        'proposal_resolve': 'plus_proposal_resolve',
    }

    def call(self, operation, connection_file, **kwargs):
        function = self._operations.get(operation)
        if function is None:
            raise util.KanpilotError("unsupported_operation", "Plus 适配器不支持该操作")
        return globals()[function](connection_file, **kwargs)

    def capabilities(self, connection_file, **kwargs):
        return self.call("capabilities", connection_file, **kwargs)

    def verification_checks(self, report, *, source_sha256):
        """Normalize report claims without upgrading their evidence provenance."""
        checks = (report.get("report") or {}).get("checks")
        util.require(isinstance(checks, list), "验证报告缺少 checks 列表",
                     "verification_unconfirmed")
        return [{"id": item.get("id"), "command": item.get("command"),
                 "exit_code": item.get("exitCode"), "result": item.get("result"),
                 "detail": util.redact(str(item.get("detail") or "")),
                 "provenance": "model_declared", "source_sha256": source_sha256}
                for item in checks if isinstance(item, dict)]

    def continuation_permission_authorized(self, scope, args):
        return (scope.role == "execution"
                and (scope.runtime_config or {}).get("permission_mode") == "auto"
                and args.get("action") == "accept"
                and args.get("target_permission_mode") == "auto")

    def observe_status(self, connection_file, *, session_id, **kwargs):
        status = self.call("status", connection_file, session_id=session_id, **kwargs)
        return self.observation(status)

    def observation(self, status):
        """Idle is an observation, never a claim that execution completed."""
        return {
            "contract_version": harnesses.CONTRACT_VERSION, "harness_id": self.harness_id,
            "session_id": status["session_id"], "turn_id": status.get("current_turn_id"),
            "state": "running" if status["is_running"] else "idle",
            "waiting_for_permission": bool(status.get("pending_permission")),
            "input_requests": status.get("pending_inputs"),
        }


ADAPTER = PlusAdapter()
