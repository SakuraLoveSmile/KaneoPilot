"""PI-Desktop 本机 MCP 控制面客户端（protocol.md §5.23.3，v3 T3）。

纯标准库实现（urllib/http.client），对接 PI-Desktop `PI_DESKTOP_MCP_CONTROL`
本机控制面（streamable-http JSON-RPC over POST /mcp）。

- ``load_connection(connection_file)`` → 校验过的连接记录 ``{url, token, pid,
  active, started_at}``：文件存在/非符号链接/权限 0600 或 0400（token 旁文件
  同样要求）/active=true/URL 为 loopback 且路径 /mcp/记录 pid 存活/token 非空。
  所有失败抛 ``KanpilotError``（中文原因）；token、Authorization header、
  连接文件全文**绝不**进入返回值、日志或异常消息。
- ``read_connection(connection_file)`` → ``(url, token)``，供 worker 复用；
  校验规则与 ``load_connection`` 相同。
- ``probe_connection(connection_file, required_tools)`` →
  ``{"ok": bool, "detail": 中文, "tools": set, "version": str|None}``：
  文件层校验 + 内存完成 MCP ``initialize``（协议版本 2025-06-18，兼容
  2025-03-26）+ ``tools/list``，核对必需工具在清单内；任一失败 ok=False。
- ``MCPClient``：initialize/initialized 握手 + ``Mcp-Session-Id`` 续连 +
  ``tools/list`` + ``tools/call``（含 pi_turn_get/pi_session_create/
  pi_session_configure/pi_agent_prompt/pi_agent_status/pi_session_get/
  pi_agent_stop/pi_agent_abort 便捷方法）。JSON-RPC 错误与 tool 级错误映射
  ``not_found``/``internal``/``validation_failed``/``remote_unavailable``；
  请求体超过 ``PI_DESKTOP_MCP_MAX_REQUEST_BYTES``（2 MiB）在发送前拒绝。
  服务端明确要求 Mcp-Session-Id（400/404 unknown session）时自动重新
  initialize 并重试一次——该拒绝发生在请求分发之前，重发不会重复执行。
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from . import constants, util

# MCP 协议版本（PI-Desktop mcp-control.ts 冻结集合）。
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_COMPATIBLE_VERSIONS = frozenset({"2025-06-18", "2025-03-26"})
CANONICAL_THINKING_LEVELS = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)
# 允许的文件权限（owner-only）。
_SECURE_MODES = frozenset({0o600, 0o400})

# JSON-RPC 错误码 → kanpilot 错误码映射。
_RPC_ERROR_CODES = {
    -32601: "not_found",   # method not found
    -32602: "not_found",   # invalid params（含 unknown tool）
    -32603: "internal",
    -32700: "internal",
    -32600: "internal",
}
# tool 级 structuredContent.error.code → kanpilot 错误码。
_TOOL_ERROR_CODES = {
    "NOT_FOUND": "not_found",
    "INVALID_PARAMS": "validation_failed",
    "INVALID_ARGUMENT": "validation_failed",
    "CONFIRMATION_REQUIRED": "validation_failed",
    "INTERACTION_POLICY_MISMATCH": "interaction_policy_unverified",
}


class McpSessionLost(Exception):
    """服务端要求（重新）MCP 会话：400 Mcp-Session-Id required / 404 unknown

    MCP session。该拒绝发生在 JSON-RPC 分发之前——原请求从未执行，重发安全。
    """


def _is_loopback_host(hostname: Optional[str]) -> bool:
    """loopback 判定（与 PI-Desktop isLoopbackBindHost 同口径 + localhost）。"""
    if not hostname:
        return False
    host = hostname.strip().lower().strip("[]")
    if host in ("localhost", "localhost.", "::1"):
        return True
    parts = host.split(".")
    if len(parts) == 4:
        try:
            octets = [int(part) for part in parts]
        except ValueError:
            return False
        return all(0 <= o <= 255 for o in octets) and octets[0] == 127
    return False


def _secure_mode(path: Path) -> Tuple[bool, int]:
    """文件权限是否安全（0600/0400）；返回 (ok, mode)。"""
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode in _SECURE_MODES, mode


def load_connection(connection_file) -> dict:
    """读取并校验 mcp-control.json（文件层全部规则；不发请求）。

    返回 ``{"url", "token", "pid", "active", "started_at"}``；任一校验失败
    抛 ``KanpilotError(validation_failed)``，消息为中文原因且不含 token/
    文件内容。
    """
    path = Path(str(connection_file or "")).expanduser()
    if not path.is_file():
        raise util.KanpilotError(
            "validation_failed", f"PI-Desktop 连接文件不存在：{path}"
        )
    if path.is_symlink():
        raise util.KanpilotError(
            "validation_failed", f"PI-Desktop 连接文件是符号链接：{path}"
        )
    ok, mode = _secure_mode(path)
    if not ok:
        raise util.KanpilotError(
            "validation_failed",
            f"PI-Desktop 连接文件权限不安全：应为 0600/0400"
            f"（当前 {mode:03o}），请先修正权限",
        )
    # token 旁文件（存在时）权限同样要求——凭据文件不可世界可读。
    token_file = path.parent / "mcp-control.token"
    if token_file.is_symlink():
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop token 文件不能是符号链接",
        )
    if token_file.exists():
        ok, tmode = _secure_mode(token_file)
        if not ok:
            raise util.KanpilotError(
                "validation_failed",
                f"PI-Desktop token 文件权限不安全：应为 0600/0400"
                f"（当前 {tmode:03o}），请先修正权限",
            )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件不是有效 JSON"
        )
    except OSError as exc:
        raise util.KanpilotError(
            "validation_failed", f"PI-Desktop 连接文件不可读：{exc}"
        )
    if not isinstance(data, dict):
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件不是 JSON 对象"
        )
    if data.get("active") is not True:
        raise util.KanpilotError(
            "validation_failed",
            "PI-Desktop 控制面未启用（连接文件 active 不是 true）",
        )
    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件缺少 url"
        )
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(url)
    except ValueError:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件 url 无法解析"
        )
    if parsed.scheme != "http":
        raise util.KanpilotError(
            "validation_failed",
            f"PI-Desktop 控制面 URL 协议不是 http：{parsed.scheme or '(空)'}",
        )
    if not _is_loopback_host(parsed.hostname):
        raise util.KanpilotError(
            "validation_failed",
            "PI-Desktop 控制面 URL 不是本机回环地址（拒绝非 loopback）",
        )
    if parsed.port is None:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 控制面 URL 缺少端口"
        )
    if parsed.path.rstrip("/") != "/mcp":
        # 服务器同时接受 /mcp 与 /mcp/；其余路径一律拒绝。
        raise util.KanpilotError(
            "validation_failed",
            f"PI-Desktop 控制面 URL 路径不是 /mcp：{parsed.path}",
        )
    if parsed.query or parsed.fragment:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 控制面 URL 携带非法 query/fragment"
        )
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件缺少有效 pid"
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise util.KanpilotError(
            "validation_failed",
            "PI-Desktop 连接文件记录的进程已不存在（控制面未在运行）",
        )
    except PermissionError:
        # 存在但无权限发信号：按存活处理（macOS 下同用户进程基本不会发生）。
        pass
    except OSError:
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件记录的 pid 无法探测"
        )
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        raise util.KanpilotError(
            "validation_failed", "PI-Desktop 连接文件缺少 token"
        )
    return {
        "url": url,
        "token": token.strip(),
        "pid": pid,
        "active": True,
        "started_at": data.get("startedAt"),
    }


def read_connection(connection_file) -> Tuple[str, str]:
    """worker 复用的入口：校验同 ``load_connection``，返回 ``(url, token)``。"""
    record = load_connection(connection_file)
    return record["url"], record["token"]


class MCPClient:
    """PI-Desktop MCP 控制面 JSON-RPC 客户端（streamable-http POST）。

    用法::

        client = MCPClient(url, token)
        client.connect()               # initialize + notifications/initialized
        tools = client.tools_list()    # {name,...}
        result = client.call_tool("pi_turn_get", {...})
    """

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self._url = url
        self._token = token
        self._timeout = timeout
        self._session_id: Optional[str] = None
        self._next_id = 0
        self.protocol_version: Optional[str] = None
        self.server_info: Optional[dict] = None

    # ------------------------------------------------------------- 传输层

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.protocol_version in MCP_COMPATIBLE_VERSIONS:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, payload: dict) -> Tuple[Optional[dict], int]:
        """POST 一个 JSON-RPC 消息；返回 (body|None, http_status)。

        - 请求体超 PI_DESKTOP_MCP_MAX_REQUEST_BYTES → validation_failed（发送
          前拒绝，不截断）。
        - 202 Accepted（通知类）→ (None, 202)。
        - 400 "Mcp-Session-Id is required" / 404 "unknown MCP session" →
          McpSessionLost（调用方决定是否重连重试）。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES:
            raise util.KanpilotError(
                "validation_failed",
                f"MCP 请求体超过 2 MiB 上限（{len(body)} 字节），发送前拒绝",
            )
        request = urllib.request.Request(
            self._url, data=body, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                self._absorb_session(resp.headers)
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            self._absorb_session(exc.headers)
            if exc.code == 404:
                self._session_id = None
                raise McpSessionLost("MCP session unknown/expired") from exc
            if exc.code == 400:
                self._session_id = None
                raise McpSessionLost("MCP session id required") from exc
            if exc.code == 401:
                raise util.KanpilotError(
                    "remote_unavailable",
                    "PI-Desktop 控制面认证失败（token 已失效或不匹配）",
                ) from exc
            if exc.code == 403:
                raise util.KanpilotError(
                    "remote_unavailable",
                    "PI-Desktop 控制面拒绝了来源（origin/host 校验失败）",
                ) from exc
            raise util.KanpilotError(
                "internal", f"PI-Desktop 控制面 HTTP 错误：{exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise util.KanpilotError(
                "remote_unavailable",
                f"PI-Desktop 控制面不可达：{_safe_net_detail(exc)}",
            ) from exc
        if status == 202 or not raw:
            return None, status
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise util.KanpilotError(
                "internal", "PI-Desktop 控制面返回非 JSON 响应"
            )
        if not isinstance(data, dict):
            raise util.KanpilotError(
                "internal", "PI-Desktop 控制面响应不是 JSON 对象"
            )
        return data, status

    def _absorb_session(self, headers) -> None:
        session = None
        try:
            session = headers.get("Mcp-Session-Id")
        except AttributeError:
            session = None
        if session:
            self._session_id = session

    def _rpc(self, method: str, params: Optional[dict] = None,
             notification: bool = False, retry: bool = True) -> Optional[dict]:
        """发 JSON-RPC 请求/通知，返回 result；错误映射 KanpilotError。"""
        if notification:
            payload = {"jsonrpc": "2.0", "method": method,
                       "params": params or {}}
        else:
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": self._next_id,
                       "method": method, "params": params or {}}
        for attempt in range(2 if retry else 1):
            try:
                data, _status = self._post(payload)
            except McpSessionLost:
                if retry and attempt == 0 and method != "initialize":
                    self._initialize()
                    continue
                raise util.KanpilotError(
                    "remote_unavailable", "PI-Desktop MCP 会话建立失败"
                )
            if notification:
                return None
            if data is None:
                raise util.KanpilotError(
                    "internal", f"MCP {method} 响应为空（应为 JSON-RPC 对象）"
                )
            error = data.get("error")
            if isinstance(error, dict):
                code = _RPC_ERROR_CODES.get(error.get("code"), "internal")
                message = _strip_secret(
                    str(error.get("message") or "JSON-RPC error"), self._token
                )
                raise util.KanpilotError(code, f"MCP {method} 失败：{message}")
            return data.get("result")
        raise util.KanpilotError("internal", "MCP 会话建立失败")  # pragma: no cover

    # ------------------------------------------------------------- 握手

    def _initialize(self) -> dict:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0", "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "kanpilot", "version": "1"},
            },
        }
        data, _status = self._post(payload)
        if data is None:
            raise util.KanpilotError("internal", "MCP initialize 无响应")
        error = data.get("error")
        if isinstance(error, dict):
            raise util.KanpilotError(
                "internal",
                f"MCP initialize 失败：{_strip_secret(str(error.get('message')), self._token)}",
            )
        result = data.get("result")
        if not isinstance(result, dict):
            raise util.KanpilotError("internal", "MCP initialize 结果不是对象")
        self.protocol_version = result.get("protocolVersion")
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else None
        return result

    def connect(self) -> dict:
        """initialize + notifications/initialized 握手，返回 initialize result。"""
        result = self._initialize()
        try:
            self._rpc("notifications/initialized", notification=True)
        except util.KanpilotError:
            # 通知失败不致命：服务端对 notifications/* 恒 202；异常多为瞬态。
            pass
        return result

    # ------------------------------------------------------------- 工具

    def tools_list(self) -> set:
        return set(self.tools_manifest())

    def tools_manifest(self) -> dict:
        """``tools/list`` 原始清单：name → tool entry（T6 能力门读 schema）。"""
        result = self._rpc("tools/list")
        tools: dict = {}
        if isinstance(result, dict):
            for entry in result.get("tools") or []:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    tools[entry["name"]] = entry
        return tools

    def tool_input_properties(self, tool_name: str) -> Optional[dict]:
        """``tools/list`` 中某工具的 ``inputSchema.properties``；不可用 → None。"""
        entry = self.tools_manifest().get(tool_name)
        if not isinstance(entry, dict):
            return None
        schema = entry.get("inputSchema")
        if not isinstance(schema, dict):
            return None
        properties = schema.get("properties")
        return properties if isinstance(properties, dict) else None

    def supports_session_naming(self) -> bool:
        """T6 兼容门（§5.23.10）：`pi_session_create` schema 暴露 `projectName`。"""
        properties = self.tool_input_properties("pi_session_create")
        if properties is None:
            return False
        return (
            constants.PI_SESSION_CREATE_PROJECT_NAME_CAPABILITY in properties
        )

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        """tools/call；返回 structuredContent（或 content[0].text 的 JSON）。

        tool 级错误（isError/structuredContent.error）按 code 映射为
        not_found/validation_failed/internal。"""
        result = self._rpc(
            "tools/call", {"name": name, "arguments": arguments or {}},
            retry=name in {"pi_turn_get", "pi_agent_status", "pi_session_get",
                           "pi_plans_pending"},
        )
        if not isinstance(result, dict):
            raise util.KanpilotError(
                "internal", f"MCP 工具 {name} 响应不是对象"
            )
        structured = result.get("structuredContent")
        value = structured
        if value is None:
            # 无 structuredContent 时解析首个 text 块。
            content = result.get("content") or []
            text = None
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    text = block["text"]
                    break
            if text is not None:
                try:
                    value = json.loads(text)
                except ValueError:
                    value = {"text": text}
        if result.get("isError") or (
            isinstance(value, dict) and isinstance(value.get("error"), dict)
        ):
            err = {}
            if isinstance(value, dict):
                err = value.get("error") or {}
            code = _TOOL_ERROR_CODES.get(str(err.get("code") or ""), "internal")
            message = _strip_secret(
                str(err.get("message") or f"{name} failed"), self._token
            )
            raise util.KanpilotError(code, f"MCP 工具 {name} 失败：{message}")
        return value if isinstance(value, dict) else {"value": value}

    # ------------------------------------------------------------- 便捷方法

    def turn_get(self, session_id: str, turn_id: str) -> dict:
        return self.call_tool(
            "pi_turn_get", {"sessionId": session_id, "turnId": turn_id}
        )

    def session_create(
        self, project_path: str, mode: str = "agent",
        provider_id: Optional[str] = None, model_id: Optional[str] = None,
        thinking_level: Optional[str] = None, title: Optional[str] = None,
        project_name: Optional[str] = None,
        interaction_mode: Optional[str] = None,
    ) -> dict:
        args = {"projectPath": project_path, "mode": mode}
        if interaction_mode is not None:
            args["interactionMode"] = interaction_mode
        if provider_id:
            args["providerId"] = provider_id
        if model_id:
            args["modelId"] = model_id
        if thinking_level:
            args["thinkingLevel"] = thinking_level
        if title:
            args["title"] = title
        if project_name:
            # T6（§5.23.10）：可选的展示名随同一次 create 调用落库。
            args["projectName"] = project_name
        return self.call_tool("pi_session_create", args)

    def session_configure(self, session_id: str, mode: str = "agent",
                          permission_mode: Optional[str] = None,
                          interaction_mode: Optional[str] = None) -> dict:
        """dangerous 操作：必带 confirm=true（PI-Desktop 校验）。"""
        args = {"id": session_id, "mode": mode, "confirm": True}
        if interaction_mode is not None:
            args["interactionMode"] = interaction_mode
        if permission_mode:
            args["permissionMode"] = permission_mode
        return self.call_tool("pi_session_configure", args)

    def agent_prompt(
        self, session_id: str, content: str,
        attachments: Optional[list] = None,
        expected_interaction_mode: Optional[str] = None,
    ) -> dict:
        """发送 prompt（§5.24.3）。``attachments`` 为 Plus ``AgentPromptAttachment``
        的真实形状（``packages/shared/src/types/agent.ts``）：
        ``{"path": <绝对路径，须落在会话 projectPath/scratch/attachments 根内>,
        "name": <显示名>, "kind": "image"|"file", "mimeType"?: str, "size"?: int}``。
        None/空列表时不带该键，保持与旧调用完全一致；不在这里做路径/类型校验
        （由调用方按 manifest 校验哈希后复制、由 Plus 侧再做根内校验）。
        """
        args = {"sessionId": session_id, "content": content}
        if expected_interaction_mode is not None:
            args["expectedInteractionMode"] = expected_interaction_mode
        if attachments:
            args["attachments"] = list(attachments)
        return self.call_tool("pi_agent_prompt", args)

    def agent_status(self, session_id: str) -> dict:
        return self.call_tool("pi_agent_status", {"sessionId": session_id})

    def session_get(
        self, session_id: str, message_before: Optional[int] = None,
        message_limit: Optional[int] = None,
    ) -> dict:
        args = {"id": session_id}
        if message_before is not None:
            # Physical transcript offset; unrelated to opaque UiMessage ids.
            if type(message_before) is not int or message_before < 0:
                raise util.KanpilotError("validation_failed", "messageBefore 必须为非负整数")
            args["messageBefore"] = message_before
        if message_limit is not None:
            args["messageLimit"] = int(message_limit)
        return self.call_tool("pi_session_get", args)

    def plans_pending(self, session_id: str) -> dict:
        """本会话待审批的 Plan/Goal 契约（§5.23.9 只读；`plans` 数组）。"""
        return self.call_tool("pi_plans_pending", {"sessionId": session_id})

    def plans_resolve(
        self, proposal_id: str, session_id: str, turn_id: str,
        tool_call_id: str, action: str,
        target_permission_mode: Optional[str] = None,
        version: Optional[int] = None,
    ) -> dict:
        """dangerous 操作：批准/拒绝目标契约，必带 confirm=true。

        批准成功即由宿主在同会话开启执行轮次（新的 turnId）；KaneoPilot 的
        计划已在本地批准，这里批准的是 PI-Desktop 侧的契约形式（§5.23.9）。
        """
        args = {
            "proposalId": proposal_id, "sessionId": session_id,
            "turnId": turn_id, "toolCallId": tool_call_id,
            "action": action, "confirm": True,
        }
        if target_permission_mode:
            args["targetPermissionMode"] = target_permission_mode
        if isinstance(version, int) and not isinstance(version, bool) and version > 0:
            args["version"] = version
        return self.call_tool("pi_plans_resolve", args)

    def goal_report_list(self, session_id: str) -> dict:
        """只读：本会话的 Goal 报告摘要列表（§5.23.10 T5-A）。"""
        return self.call_tool(
            "pi_goal_report_list", {"sessionId": session_id}
        )

    def goal_report_get(self, session_id: str,
                        execution_id: Optional[str] = None,
                        report_id: Optional[str] = None) -> dict:
        """只读：取回经 Host 完整性核验的完整 Goal 报告（§5.23.10 T5-A）。

        ``pi_goal_report_get {sessionId, executionId}``（``reportId`` 亦可）。
        """
        args = {"sessionId": session_id}
        if execution_id:
            args["executionId"] = execution_id
        if report_id:
            args["reportId"] = report_id
        return self.call_tool("pi_goal_report_get", args)

    def session_list(self, query: Optional[str] = None) -> dict:
        """会话记录列表（只读）：读会话当前 mode/permissionMode 等生效值。"""
        args = {}
        if query:
            args["query"] = query
        return self.call_tool("pi_session_list", args)

    def agent_stop(self, session_id: str) -> dict:
        return self.call_tool("pi_agent_stop", {"sessionId": session_id})

    def agent_abort(self, session_id: str) -> dict:
        return self.call_tool("pi_agent_abort", {"sessionId": session_id})


def probe_connection(connection_file,
                     required_tools=constants.PI_DESKTOP_DISPATCH_TOOLS,
                     *, provider_id=None, model_id=None, timeout=10) -> dict:
    """exec-capabilities/dispatch-start 就绪探测（§5.23.3 + §5.23.10 T6）。

    返回 ``{"ok": bool, "detail": 中文, "tools": set, "version": str|None,
    "session_naming": bool}``；任何失败 ok=False + 中文原因。
    token/Authorization/连接文件全文绝不进入返回值。

    ``session_naming`` 为 T6 兼容门：``pi_session_create.inputSchema.
    properties.projectName`` 是否可用（旧 Plus → False）。
    """
    result = {"ok": False, "detail": None, "tools": set(), "version": None,
              "session_naming": False}
    try:
        record = load_connection(connection_file)
    except util.KanpilotError as exc:
        result["detail"] = exc.message
        return result
    client = MCPClient(record["url"], record["token"], timeout=timeout)
    try:
        init = client.connect()
        version = client.protocol_version or (
            (init.get("serverInfo") or {}).get("version")
        )
        if client.protocol_version not in MCP_COMPATIBLE_VERSIONS:
            result["detail"] = (
                f"PI-Desktop MCP 协议版本不受支持：{client.protocol_version}"
            )
            return result
        result["version"] = client.protocol_version
        manifest = client.tools_manifest()
        tools = set(manifest)
        # T6 兼容门：schema 里必须暴露 pi_session_create.projectName。
        naming = manifest.get("pi_session_create")
        schema = naming.get("inputSchema") if isinstance(naming, dict) else None
        props = schema.get("properties") if isinstance(schema, dict) else None
        result["session_naming"] = bool(
            isinstance(props, dict)
            and constants.PI_SESSION_CREATE_PROJECT_NAME_CAPABILITY in props
        )
    except util.KanpilotError as exc:
        result["detail"] = _strip_secret(exc.message, record["token"])
        return result
    result["tools"] = tools
    missing = [name for name in required_tools if name not in tools]
    if missing:
        result["detail"] = (
            "PI-Desktop 控制面缺少必需工具："
            + "、".join(missing)
            + "（PI-Desktop 版本过旧，请升级）"
        )
        return result
    if (provider_id is None) != (model_id is None):
        result["detail"] = "provider_id 与 model_id 必须同时提供"
        return result
    if provider_id is not None:
        try:
            providers = client.call_tool("pi_desktop_invoke", {
                "operation": "providers/list", "args": [],
            })
            providers = _unwrap_invoke_result(providers)
            rows = providers.get("providers") if isinstance(providers, dict) else None
            provider = next(
                (p for p in rows or [] if isinstance(p, dict)
                 and p.get("id") == provider_id), None
            )
            if provider is None:
                result["detail"] = f"PI-Desktop provider 不存在：{provider_id}"
                return result
            if provider.get("disabled") is True or provider.get("enabled") is False:
                result["detail"] = f"PI-Desktop provider 已禁用：{provider_id}"
                return result
            models = client.call_tool("pi_desktop_invoke", {
                "operation": "providers/listModels",
                "args": [{"providerId": provider_id, "source": "cache"}],
            })
            models = _unwrap_invoke_result(models)
            model_rows = models.get("models") if isinstance(models, dict) else None
            matched = next(
                (m for m in model_rows or [] if isinstance(m, dict)
                 and (m.get("modelId") or m.get("id")) == model_id), None
            )
            if matched is None:
                result["detail"] = f"PI-Desktop model 不存在：{model_id}"
                return result
            source = models.get("source")
            if source not in ("cache", "fallback"):
                result["detail"] = "PI-Desktop model 目录不是允许的本地 cache/fallback 来源"
                return result
        except util.KanpilotError as exc:
            result["detail"] = _strip_secret(exc.message, record["token"])
            return result
    server = (client.server_info or {}).get("name") or "pi-desktop"
    result["ok"] = True
    result["detail"] = f"PI-Desktop 控制面就绪（{server}，{version or '未知版本'}）"
    return result


def _unwrap_invoke_result(value: dict) -> dict:
    """Unwrap Plus ``pi_desktop_invoke``'s ``{operation,result}`` envelope."""
    if not isinstance(value, dict):
        raise util.KanpilotError("internal", "PI-Desktop invoke 结果不是对象")
    inner = value.get("result", value)
    if not isinstance(inner, dict):
        raise util.KanpilotError("internal", "PI-Desktop invoke result 不是对象")
    return inner


# ------------------------------------------------------------ 内部辅助

def _safe_net_detail(exc) -> str:
    """网络层错误的脱敏描述（绝不含 Authorization/token）。"""
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    # URLError.reason 可能包装底层 errno；只保留不含 header 的短描述。
    return text[:200]


def _strip_secret(text: str, token: Optional[str]) -> str:
    """服务端错误消息经脱敏后返回（token ≥8 字符按值替换 + 常见形态脱敏）。"""
    secrets = [token] if token else None
    return util.redact(text, secrets=secrets)

def nearest_thinking_level(target: str, valid_levels: list) -> str:
    """Find nearest supported thinking level in canonical order (search upward, then downward)."""
    if not valid_levels:
        return "off"
    if target in valid_levels:
        return target
    if target not in CANONICAL_THINKING_LEVELS:
        return valid_levels[-1]
    target_idx = CANONICAL_THINKING_LEVELS.index(target)
    for i in range(target_idx + 1, len(CANONICAL_THINKING_LEVELS)):
        lvl = CANONICAL_THINKING_LEVELS[i]
        if lvl in valid_levels:
            return lvl
    for i in range(target_idx - 1, -1, -1):
        lvl = CANONICAL_THINKING_LEVELS[i]
        if lvl in valid_levels:
            return lvl
    return valid_levels[-1]


def resolve_default_from_catalog_data(
    connection_file: str,
    rows: list,
    settings: dict,
    models_fetcher=None,
) -> Tuple[Optional[dict], Optional[str]]:
    """Resolve default (provider, model, thinking) from parsed catalog rows and settings."""
    if not isinstance(rows, list):
        return None, "PI-Desktop provider 目录格式无效"
    enabled = [
        r for r in rows
        if isinstance(r, dict) and r.get("id") and r.get("enabled") is not False and r.get("disabled") is not True
    ]
    if not enabled:
        return None, "PI-Desktop 没有已启用的 provider"

    settings = settings.get("settings", settings) if isinstance(settings, dict) else {}
    settings_default_provider = settings.get("defaultProviderId") or settings.get("default_provider_id")
    settings_default_model = settings.get("defaultModelId") or settings.get("default_model_id")

    # 1. Resolve Provider
    if settings_default_provider:
        selected_provider = next((p for p in enabled if p.get("id") == settings_default_provider), None)
        if selected_provider is None:
            return None, f"Plus 默认 provider ({settings_default_provider}) 不存在或已禁用"
    elif len(enabled) == 1:
        selected_provider = enabled[0]
    else:
        return None, f"Plus 存在多个已启用 provider ({len(enabled)} 个)，但未设置默认 provider，无法自动继承"

    # 2. Resolve Model
    target_model_id = None
    if selected_provider.get("id") == settings_default_provider and settings_default_model:
        target_model_id = settings_default_model
    elif selected_provider.get("defaultModelId") or selected_provider.get("default_model_id"):
        target_model_id = selected_provider.get("defaultModelId") or selected_provider.get("default_model_id")

    if models_fetcher is None:
        return None, "缺少模型获取器"

    models_res, fetch_err = models_fetcher(selected_provider["id"])
    if fetch_err:
        return None, fetch_err
    models_res = _unwrap_invoke_result(models_res)
    source = models_res.get("source")
    if source not in ("cache", "fallback"):
        return None, "PI-Desktop model 目录不是允许的本地 cache/fallback 来源"
    model_rows = models_res.get("models")
    if not isinstance(model_rows, list) or not model_rows:
        return None, f"Plus provider '{selected_provider['id']}' 的本地缓存模型列表为空"

    if target_model_id is None:
        if len(model_rows) == 1:
            target_model_id = model_rows[0].get("modelId") or model_rows[0].get("id")
        else:
            return None, f"Plus provider '{selected_provider['id']}' 包含多个模型 ({len(model_rows)} 个)，但未设置默认模型，无法自动继承"

    matched_row = next(
        (m for m in model_rows if isinstance(m, dict) and (m.get("modelId") or m.get("id")) == target_model_id),
        None,
    )
    if matched_row is None:
        return None, f"Plus 默认模型 '{target_model_id}' 未在 provider '{selected_provider['id']}' 的本地缓存目录中找到"

    # 3. Resolve Thinking Level
    configured = next(
        (m for m in (selected_provider.get("models") or []) if isinstance(m, dict) and (m.get("modelId") or m.get("id")) == target_model_id),
        None,
    )
    if configured is None and target_model_id == (selected_provider.get("defaultModelId") or selected_provider.get("default_model_id")):
        configured = selected_provider

    raw_levels = None
    if configured is not None and ("thinkingLevels" in configured or "thinking_levels" in configured):
        raw_levels = configured.get("thinkingLevels") if "thinkingLevels" in configured else configured.get("thinking_levels")
    elif matched_row is not None and ("supportedThinkingLevels" in matched_row or "supported_thinking_levels" in matched_row):
        raw_levels = matched_row.get("supportedThinkingLevels") if "supportedThinkingLevels" in matched_row else matched_row.get("supported_thinking_levels")
    elif selected_provider is not None and ("supportedThinkingLevels" in selected_provider or "supported_thinking_levels" in selected_provider):
        raw_levels = selected_provider.get("supportedThinkingLevels") if "supportedThinkingLevels" in selected_provider else selected_provider.get("supported_thinking_levels")

    raw_default_thinking = None
    if configured is not None:
        raw_default_thinking = configured.get("defaultThinkingLevel") or configured.get("default_thinking_level")

    if raw_default_thinking == "omit":
        return None, "当前 Plus MCP 不支持此默认思考设置（omit）"

    if raw_levels is not None and (not isinstance(raw_levels, list) or any(not isinstance(level, str) or level not in CANONICAL_THINKING_LEVELS for level in raw_levels)):
        return None, "Plus 思考等级元数据非法，不能继承默认配置"

    if raw_levels is not None and isinstance(raw_levels, list) and len(raw_levels) == 0:
        resolved_thinking = "off"
    else:
        valid_levels = [l for l in (raw_levels or []) if isinstance(l, str) and l in CANONICAL_THINKING_LEVELS]
        if raw_default_thinking is not None:
            if raw_default_thinking not in CANONICAL_THINKING_LEVELS:
                return None, f"Plus 默认思考设置非法：{raw_default_thinking}"
            if raw_default_thinking in valid_levels:
                resolved_thinking = raw_default_thinking
            elif valid_levels:
                resolved_thinking = nearest_thinking_level(raw_default_thinking, valid_levels)
            else:
                resolved_thinking = raw_default_thinking
        else:
            if valid_levels:
                resolved_thinking = max(valid_levels, key=lambda l: CANONICAL_THINKING_LEVELS.index(l))
            else:
                return None, f"无法确定模型 '{target_model_id}' 的默认思考设置，请在 Plus 中配置或使用自定义模型"

    return {
        "connection_file": str(Path(connection_file).expanduser()),
        "provider_id": selected_provider["id"],
        "model_id": target_model_id,
        "thinking_level": resolved_thinking,
    }, None


def resolve_plus_default(connection_file, *, timeout: int = 10) -> Tuple[Optional[dict], Optional[str]]:
    """Probe Plus MCP server and dynamically resolve current default provider, model, and thinking level."""
    try:
        record = load_connection(connection_file)
    except util.KanpilotError as exc:
        return None, exc.message

    client = MCPClient(record["url"], record["token"], timeout=timeout)
    try:
        client.connect()
        if client.protocol_version not in MCP_COMPATIBLE_VERSIONS:
            return None, f"PI-Desktop MCP 协议版本不受支持：{client.protocol_version or '未知'}"
        tools = client.tools_list()
        if "pi_desktop_invoke" not in tools:
            return None, "PI-Desktop 控制面缺少 pi_desktop_invoke 工具"

        providers_res = client.call_tool("pi_desktop_invoke", {"operation": "providers/list", "args": []})
        providers_res = _unwrap_invoke_result(providers_res)
        rows = providers_res.get("providers")

        settings_res = client.call_tool("pi_desktop_invoke", {"operation": "settings/get", "args": []})
        settings_res = _unwrap_invoke_result(settings_res)

        def fetch_models(pid: str):
            try:
                res = client.call_tool(
                    "pi_desktop_invoke",
                    {"operation": "providers/listModels", "args": [{"providerId": pid, "source": "cache"}]},
                )
                return res, None
            except Exception as e:
                return None, str(e)

        return resolve_default_from_catalog_data(
            connection_file,
            rows or [],
            settings_res or {},
            models_fetcher=fetch_models,
        )
    except util.KanpilotError as exc:
        return None, _strip_secret(exc.message, record.get("token"))
    except Exception as exc:
        return None, _strip_secret(str(exc), record.get("token") if "record" in locals() else None)
# ======================================================= Goal 报告（T5，§5.23.10）

# Plus 本机 MCP 只读工具 ``pi_goal_report_get {sessionId, executionId}``
# （``reportId`` 亦可）返回完整报告 JSON 并附可信 ``report_sha256``。
# 本模块只负责「取回 + 分态归一化」，放行判断（身份核对与确定性报告底线）
# 在命令层（cmd_read / cmd_exec）。

# 分态互不混淆：``ready`` / ``pending`` / ``draft`` / ``failed`` /
# ``not_found`` / ``corrupt`` / ``truncated``。``fallback`` 是**成功返回的
# 报告**，但其 ``integrity.kind=fallback`` 表示没有完成报告。任何失败态都
# 不返回伪造正文；``tool_missing``/``unreadable`` 是核心侧额外区分的读取
# 环境失败——临时读取失败不得伪称「没有报告」。
GOAL_REPORT_GET_TOOL = "pi_goal_report_get"
GOAL_REPORT_LIST_TOOL = "pi_goal_report_list"
GOAL_REPORT_STATES = (
    "ready", "pending", "draft", "failed", "not_found", "corrupt",
    "truncated",
)
# 核心侧额外分态（不属于报告自身状态）。
GOAL_REPORT_READ_FAILURES = ("tool_missing", "unreadable")
_GOAL_REPORT_FAILURE_WORDS = ("pending", "draft", "failed", "not_found",
                              "corrupt", "truncated")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

def fetch_goal_report(session_id, *, execution_id=None, report_id=None,
                      connection_file=None, client=None, timeout=30) -> dict:
    """只读取回并分态归一化一份 Plus Goal 报告（§5.23.10 T5-A）。

    返回 ``{"state", "detail", "report", "report_sha256", "report_id",
    "raw"}``：``state`` ∈ :data:`GOAL_REPORT_STATES` + 读取环境失败词汇；
    ``report`` 仅在 ``state="ready"`` 时为完整报告对象，其余一律 ``None``
    （绝不伪造正文）。调用方负责身份核对与确定性底线；本函数不做语义判断。
    """
    failure = {"state": "unreadable", "detail": None, "report": None,
               "report_sha256": None, "report_id": report_id, "raw": None}
    owned = False
    if client is None:
        if not connection_file:
            failure["detail"] = "缺少连接文件，无法读取 Goal 报告"
            return failure
        try:
            record = load_connection(connection_file)
        except util.KanpilotError as exc:
            failure["detail"] = exc.message
            return failure
        client = MCPClient(record["url"], record["token"], timeout=timeout)
        owned = True
    try:
        if owned:
            client.connect()
            tools = client.tools_list()
            if GOAL_REPORT_GET_TOOL not in tools:
                failure["state"] = "tool_missing"
                failure["detail"] = (
                    "PI-Desktop 控制面缺少只读报告工具："
                    f"{GOAL_REPORT_GET_TOOL}（PI-Desktop-Plus 版本过旧，"
                    "不支持侧报告读取；不据此伪造完成报告）"
                )
                return failure
        args = {"sessionId": session_id}
        if execution_id:
            args["executionId"] = execution_id
        if report_id:
            args["reportId"] = report_id
        payload = client.call_tool(GOAL_REPORT_GET_TOOL, args)
    except util.KanpilotError as exc:
        failure["state"] = _classify_report_failure(exc.code, exc.message)
        failure["detail"] = exc.message
        return failure
    return normalize_goal_report(payload, report_id=report_id)

def classify_report_error(code, message) -> str:
    """错误码/消息 → 报告分态（缺省 ``unreadable`` = 临时读取失败）。

    ``unreadable`` 与 ``not_found`` 必须区分：临时读取失败**不得**伪称
    「没有报告」（§5.23.10 T5-D）。
    """
    text = str(message or "").lower()
    if "unknown tool" in text or "缺少必需工具" in text:
        return "tool_missing"
    for word in _GOAL_REPORT_FAILURE_WORDS:
        if word in text:
            return word
    if code == "not_found":
        return "not_found"
    if code == "validation_failed":
        return "corrupt"
    return "unreadable"


# 兼容旧内部名（同一实现；读取层与 worker 共用）。
_classify_report_failure = classify_report_error

def normalize_goal_report(payload, *, report_id=None) -> dict:
    """工具回包 → 统一报告视图（成功态判 schema/身份完整，失败态明确分态）。"""
    result = {"state": "corrupt", "detail": None, "report": None,
              "report_sha256": None, "report_id": report_id, "raw": payload}
    if not isinstance(payload, dict):
        result["detail"] = "报告读取回包不是 JSON 对象"
        return result
    result["report_id"] = (payload.get("reportId")
                           or payload.get("report_id") or report_id)
    sha = payload.get("report_sha256") or payload.get("reportSha256")
    result["report_sha256"] = sha if isinstance(sha, str) and sha else None
    if payload.get("truncated") is True:
        result["state"] = "truncated"
        result["detail"] = "报告超过 256 KiB 上限，已明确截断（不返回截断正文）"
        return result
    error = payload.get("error")
    if isinstance(error, dict):
        result["state"] = _classify_report_failure(
            str(error.get("code") or ""), str(error.get("message") or "")
        )
        result["detail"] = str(error.get("message") or "报告读取失败")
        return result
    if payload.get("isError") is True:
        result["state"] = _classify_report_failure(None, payload.get("detail"))
        result["detail"] = str(payload.get("detail") or "报告读取失败")
        return result
    report = payload.get("report")
    if report is None and ("status" in payload or "sessionId" in payload):
        # Host 直接回传报告对象（并附 report_sha256）的形态。
        report = payload
    if not isinstance(report, dict):
        explicit = payload.get("state")
        if explicit in GOAL_REPORT_STATES:
            result["state"] = explicit
            result["detail"] = str(payload.get("detail") or "")
            return result
        result["state"] = "not_found"
        result["detail"] = "回包中没有报告对象"
        return result
    status = str(report.get("status") or "").lower()
    if status in ("pending", "draft", "failed"):
        result["state"] = status
        result["detail"] = str(
            payload.get("detail") or report.get("detail") or ""
        )
        return result
    if status != "ready":
        result["state"] = "corrupt"
        result["detail"] = f"报告 status 非法：{status or '(空)'}"
        return result
    if result["report_sha256"] is None:
        result["state"] = "corrupt"
        result["detail"] = "报告缺少可信 report_sha256"
        return result
    if not _SHA256_RE.match(result["report_sha256"]):
        result["state"] = "corrupt"
        result["detail"] = "report_sha256 不是 64 位十六进制"
        return result
    if not report.get("sessionId") or not report.get("executionId"):
        result["state"] = "corrupt"
        result["detail"] = "报告缺少 sessionId/executionId 身份字段"
        return result
    result["state"] = "ready"
    result["report"] = report
    return result

def report_identity(report: dict) -> dict:
    """报告身份字段视图（§5.23.10 T5-B；绝不按名称/标题匹配）。"""
    report = report if isinstance(report, dict) else {}
    return {
        "session_id": report.get("sessionId") or report.get("session_id"),
        "proposal_id": report.get("proposalId") or report.get("proposal_id"),
        "execution_id": (
            report.get("executionId") or report.get("execution_id")
        ),
        "turn_id": report.get("turnId") or report.get("turn_id"),
        # Plus Goal 契约的 artifact 哈希与 KaneoPilot 的 plan_sha256 各管
        # 各的文件，**不可**直接比较成同一个哈希（§5.23.10 T5-B）。
        "contract_hash": (
            report.get("contractHash") or report.get("contract_hash")
        ),
    }

def report_integrity_kind(report: dict):
    """``integrity.kind``（``structured`` / ``fallback``）；缺失 → None。"""
    integrity = (report or {}).get("integrity")
    if not isinstance(integrity, dict):
        return None
    kind = integrity.get("kind")
    return str(kind) if isinstance(kind, str) and kind else None

def report_execution_status(report: dict):
    """Host 侧执行事实 ``execution.status``（§5.23.10 T5-C）。"""
    execution = (report or {}).get("execution")
    if not isinstance(execution, dict):
        return None
    status = execution.get("status")
    return str(status).lower() if isinstance(status, str) and status else None

def report_verdict(report: dict):
    verdict = (report or {}).get("verdict")
    return str(verdict).lower() if isinstance(verdict, str) and verdict else None

def _id_of(item, *keys):
    if isinstance(item, str) and item:
        return item
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None

def report_reference_ids(report: dict) -> dict:
    """报告中实际存在的 criterion/check/evidence ID 集合（T5-C 引用核对）。"""
    report = report if isinstance(report, dict) else {}
    buckets = {}
    for name, item_keys in (
        ("criteria", ("id", "criterionId", "criterion_id", "key")),
        ("checks", ("id", "checkId", "check_id", "key")),
        ("evidences", ("id", "evidenceId", "evidence_id", "key")),
    ):
        ids = set()
        rows = report.get(name)
        if isinstance(rows, list):
            for item in rows:
                value = _id_of(item, *item_keys)
                if value:
                    ids.add(value)
        elif isinstance(rows, dict):
            for key, item in rows.items():
                value = _id_of(item, *item_keys) or (
                    key if isinstance(key, str) and key else None
                )
                if value:
                    ids.add(value)
        buckets[name] = ids
    return buckets
