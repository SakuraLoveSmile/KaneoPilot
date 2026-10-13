"""Plus runtime facade routed through the KaneoPilot MCP service."""

from __future__ import annotations

import datetime
import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from . import constants, harness_plus_adapter, mcp_client, mcp_scopes, util


def _read_admin_server(server_file: Path) -> tuple:
    path = Path(server_file).expanduser()
    if path.is_symlink() or not path.is_file():
        raise util.KanpilotError("not_found", f"KaneoPilot MCP 服务文件不可用：{path}")
    st = path.stat()
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 服务文件所有者不符")
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 服务文件权限必须为 owner-only")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise util.KanpilotError("validation_failed", f"KaneoPilot MCP 服务文件无法解析：{exc}")
    if not isinstance(data, dict) or data.get("active") is not True:
        raise util.KanpilotError("remote_unavailable", "KaneoPilot MCP 服务未运行")
    url = data.get("url")
    parsed = urlsplit(url or "")
    if (parsed.scheme != "http" or parsed.path.rstrip("/") != "/mcp"
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or not parsed.port):
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 服务 URL 不是有效的本机 /mcp 地址")
    token_file = Path(str(data.get("token_file") or "")).expanduser()
    if token_file.is_symlink() or not token_file.is_file():
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 管理凭据文件不可用")
    token_stat = token_file.stat()
    if hasattr(os, "getuid") and token_stat.st_uid != os.getuid():
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 管理凭据所有者不符")
    if stat.S_IMODE(token_stat.st_mode) not in (0o600, 0o400):
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 管理凭据权限必须为 owner-only")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise util.KanpilotError("validation_failed", "KaneoPilot MCP 管理凭据为空")
    return data, token


def _admin_register(url: str, token: str, payload: dict, timeout: float) -> dict:
    endpoint = urlsplit(url)
    admin_url = f"{endpoint.scheme}://{endpoint.netloc}/admin/scopes"
    req = urllib.request.Request(
        admin_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = None
        try:
            body = json.loads(exc.read().decode("utf-8"))
            message = body.get("error") if isinstance(body, dict) else None
        except ValueError:
            message = None
        raise util.KanpilotError(str(body.get("code", "remote_unavailable")) if isinstance(body, dict) else "remote_unavailable",
                                 str(message or f"KaneoPilot scope 注册失败（HTTP {exc.code}）"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise util.KanpilotError("remote_unavailable", f"无法连接 KaneoPilot MCP 服务：{getattr(exc, 'reason', exc)}")
    if not isinstance(body, dict) or body.get("ok") is not True or not isinstance(body.get("data"), dict):
        raise util.KanpilotError("protocol_error", "KaneoPilot scope 注册返回格式无效")
    return body["data"]


def stable_scope_id(role: str, *, invocation_id: Optional[str] = None,
                    task_id: Optional[str] = None, run_id: Optional[str] = None) -> str:
    """Use the durable invocation or task/run identity; never model-provided data."""
    if role in ("planner", "review", "execution") and task_id and run_id:
        identity = f"{task_id}_{run_id}"
    elif invocation_id:
        identity = invocation_id
    elif task_id and run_id:
        identity = f"{task_id}_{run_id}"
    else:
        raise util.KanpilotError("validation_failed", "runtime scope 缺少 invocation_id 或 task_id/run_id")
    candidate = f"{role}_{identity}"
    if len(candidate) > 128 or re.fullmatch(r"[A-Za-z0-9_-]+", candidate) is None:
        raise util.KanpilotError("validation_failed", "runtime scope 身份格式无效")
    return candidate


class RuntimeClient:
    """Compat facade for the Plus client API, with all calls via project MCP."""

    def __init__(self, *, role: str, connection_file: str, project_path: str,
                 runtime_config: dict, invocation_id: Optional[str] = None,
                 request_id: Optional[str] = None, run_id: Optional[str] = None,
                 task_id: Optional[str] = None, timeout: float = 30.0,
                 lease_seconds: Optional[float] = None,
                 read_only: bool = False, goal_continuation: Optional[dict] = None):
        self.role = role
        if not isinstance(connection_file, str) or not connection_file:
            raise util.KanpilotError("validation_failed", "runtime scope 缺少 connection_file")
        if not isinstance(project_path, str) or not project_path:
            raise util.KanpilotError("validation_failed", "runtime scope 缺少 project_path")
        self.connection_file = str(Path(connection_file).expanduser())
        self.project_path = str(Path(project_path).expanduser())
        self.runtime_config = {
            key: runtime_config[key] for key in constants.EXEC_PI_DESKTOP_KEYS
            if key in runtime_config
        }
        self.invocation_id = invocation_id or f"{task_id}_{run_id}"
        self.request_id, self.run_id, self.task_id = request_id, run_id, task_id
        self.timeout = timeout
        self.read_only = bool(read_only)
        self.goal_continuation = dict(goal_continuation) if goal_continuation else None
        self.lease_seconds = max(float(lease_seconds or timeout), 1.0)
        self.scope_id = stable_scope_id(
            role, invocation_id=invocation_id, task_id=task_id, run_id=run_id,
        )
        self.client: Optional[mcp_client.MCPClient] = None
        self.server_info = None
        self.protocol_version = constants.MCP_PROTOCOL_VERSION
        self._attached_sessions: set = set()
        self._required_tools: set = set()
        self.harness_pid: Optional[int] = None

    @property
    def _timeout(self) -> float:
        return self.timeout

    @_timeout.setter
    def _timeout(self, value: float) -> None:
        self.timeout = float(value)
        if self.client is not None:
            self.client.timeout = self.timeout

    def connect(self, required_tools=()) -> None:
        self._required_tools = set(required_tools)
        server_file = constants.app_support() / "mcp" / "server.json"
        server, admin_token = _read_admin_server(server_file)
        expires_at = self._expiry(self.lease_seconds)
        existing_expiry = self._existing_expiry(server_file.parent / "grants" / f"{self.scope_id}_runtime.json")
        if existing_expiry:
            expires_at = existing_expiry
        payload = {
            "scope_id": self.scope_id,
            "invocation_id": self.invocation_id,
            "role": self.role,
            "expires_at": expires_at,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "project_path": self.project_path,
            "runtime_config": self.runtime_config,
            "connection_file": self.connection_file,
        }
        registered = _admin_register(server["url"], admin_token, payload, self.timeout)
        grant = registered.get("runtime_grant_file")
        if not grant:
            raise util.KanpilotError("protocol_error", "scope 注册未返回 runtime grant")
        self.client = mcp_client.MCPClient.from_grant_file(Path(grant), timeout=self.timeout)
        init = self.client.initialize(client_name=f"kaneopilot-{self.role}")
        if not isinstance(init, dict) or init.get("serverInfo", {}).get("name") != constants.MCP_SERVER_NAME:
            raise util.KanpilotError("protocol_error", "runtime grant 未连接到 KaneoPilot MCP 服务")
        self.server_info = init.get("serverInfo")
        self.protocol_version = init.get("protocolVersion", constants.MCP_PROTOCOL_VERSION)
        tools = {item.get("name") for item in self.client.list_tools() if isinstance(item, dict)}
        required_runtime = mcp_scopes.RUNTIME_ROLE_TOOLS.get(self.role)
        if not required_runtime:
            raise util.KanpilotError("validation_failed", f"未知 runtime role：{self.role}")
        if self.read_only:
            required_runtime = {
                "kp_runtime_capabilities", "kp_runtime_status",
                "kp_runtime_turn_get", "kp_runtime_messages",
                "kp_runtime_proposals",
            }
            if self.role == "execution":
                required_runtime.update({
                    "kp_runtime_goal_report_list", "kp_runtime_goal_report_get",
                })
        missing = required_runtime - tools
        if missing:
            raise util.KanpilotError("unsupported_tools", "KaneoPilot MCP 缺少 runtime 工具：" + "、".join(sorted(missing)))
        caps = self._call("kp_runtime_capabilities", {})
        if caps.get("available") is not True or caps.get("tools_ok") is not True:
            raise util.KanpilotError("remote_unavailable", str(caps.get("detail") or "Plus 控制面不可用"))
        supported = set(caps.get("supported_operations") or ())
        pid = caps.get("pid")
        self.harness_pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        missing_plus = set(required_tools) - supported
        if missing_plus:
            raise util.KanpilotError("unsupported_tools", "Plus 控制面缺少必需工具：" + "、".join(sorted(missing_plus)))

    def reconnect(self) -> "RuntimeClient":
        candidate = RuntimeClient(
            role=self.role, connection_file=self.connection_file,
            project_path=self.project_path, runtime_config=self.runtime_config,
            invocation_id=self.invocation_id, request_id=self.request_id,
            run_id=self.run_id, task_id=self.task_id, timeout=self.timeout,
            lease_seconds=self.lease_seconds,
            read_only=self.read_only,
            goal_continuation=self.goal_continuation,
        )
        candidate.connect(required_tools=self._required_tools)
        return candidate

    @staticmethod
    def _expiry(lease_seconds: float) -> str:
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=lease_seconds + 60,
        )
        return expires.isoformat(timespec="seconds")

    @staticmethod
    def _existing_expiry(grant_file: Path) -> Optional[str]:
        if not grant_file.is_file() or grant_file.is_symlink():
            return None
        try:
            data = json.loads(grant_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("expires_at") if isinstance(data, dict) else None
        expires = util.parse_ts(value) if isinstance(value, str) else None
        if expires is None or expires <= util.now_dt():
            return None
        return value

    def _call(self, name: str, args: dict) -> dict:
        if self.client is None:
            raise util.KanpilotError("remote_unavailable", "runtime MCP client 尚未连接")
        return self.client.call_tool(name, {"scope_id": self.scope_id, **args})

    def _write(self, name: str, op: str, args: dict) -> dict:
        if self.read_only:
            bound = self.goal_continuation or {}
            permitted = (self.role == "execution" and bound.get("scope_id") == self.scope_id
                         and self.runtime_config.get("permission_mode") == "auto"
                         and name == "kp_runtime_proposal_resolve"
                         and args.get("action") == "accept"
                         and args.get("target_permission_mode") == "auto"
                         and all(args.get(key) == bound.get(key) and bound.get(key) is not None
                                 for key in ("session_id", "turn_id", "proposal_id", "tool_call_id", "version")))
            if not permitted:
                raise util.KanpilotError(
                    "forbidden",
                    "恢复为只读，待处理 proposal 需人工确认；未重发任务",
                )
            args = {**args, "goal_continuation": True}
        return self._call(name, {"operation_id": op, **args})

    def tools_list(self) -> set:
        caps = self._call("kp_runtime_capabilities", {})
        return set(caps.get("supported_operations") or [])

    def session_create(self, project_path: str, *, mode: str, provider_id=None,
                       model_id=None, thinking_level=None, title=None,
                       project_name=None, **_kwargs) -> dict:
        config = dict(self.runtime_config)
        for key, value in (("provider_id", provider_id), ("model_id", model_id),
                           ("thinking_level", thinking_level)):
            if value is not None:
                config[key] = value
        arguments = {
            "mode": mode, "project_path": str(Path(project_path).expanduser()),
            "runtime_config": config,
        }
        if title is not None:
            arguments["title"] = title
        if project_name is not None:
            arguments["project_name"] = project_name
        result = self._write("kp_runtime_session_create", "session_create", arguments)
        session_id = result.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise util.KanpilotError("protocol_error", "runtime session_create 未返回有效 session_id")
        session = {"id": session_id}
        for source, target in (("title", "title"), ("project_name", "projectName")):
            if source in result:
                session[target] = result[source]
        return {"session": session}

    def session_configure(self, session_id: str, *, mode: str,
                          permission_mode: str, **_kwargs) -> dict:
        result = self._write("kp_runtime_session_configure", "session_configure", {
            "session_id": session_id, "mode": mode,
            "permission_mode": permission_mode,
        })
        if result.get("session_id") != session_id or result.get("configured") is not True:
            raise util.KanpilotError("protocol_error", "runtime session_configure 未确认配置")
        return result

    def attach_mcp(self, session_id: str) -> dict:
        result = self._write("kp_runtime_attach_mcp", "attach_mcp", {"session_id": session_id})
        if result.get("session_id") != session_id or result.get("attached") is not True:
            raise util.KanpilotError("validation_failed", "KaneoPilot task MCP 未挂接")
        names = result.get("tool_names")
        expected = mcp_scopes.TASK_ROLE_TOOLS.get(self.role)
        if not isinstance(names, list) or not expected or not expected.issubset(set(names)):
            raise util.KanpilotError("unsupported_tools", "挂接后的 task 工具集不符合 scope role")
        if result.get("server_name") != constants.MCP_SERVER_NAME:
            raise util.KanpilotError("validation_failed", "挂接的 MCP server 身份不匹配")
        self._attached_sessions.add(session_id)
        return result

    def agent_prompt(self, session_id: str, content: str, *, attachments=None) -> dict:
        result = self._write("kp_runtime_prompt", "prompt", {
            "session_id": session_id, "content": content,
            **({"attachments": attachments} if attachments else {}),
        })
        if result.get("session_id") != session_id or result.get("accepted") is not True:
            raise util.KanpilotError("protocol_error", "runtime prompt 未确认接受")
        turn_id = result.get("turn_id")
        return {"session_id": session_id, "turnId": str(turn_id) if turn_id else None,
                "accepted": True}

    def agent_status(self, session_id: str) -> dict:
        result = self._call("kp_runtime_status", {"session_id": session_id})
        if result.get("session_id") != session_id:
            raise util.KanpilotError("protocol_error", "runtime status session_id 不匹配")
        raw = dict(result.get("raw_status") or {})
        raw.update({"isRunning": result.get("is_running"),
                    "currentTurnId": result.get("current_turn_id"),
                    "pendingPermission": result.get("pending_permission")})
        return raw

    def turn_get(self, session_id: str, turn_id: str, *, require_identity: bool = False) -> dict:
        result = self._call("kp_runtime_turn_get", {"session_id": session_id, "turn_id": turn_id,
                            **({"require_identity": True} if require_identity else {})})
        return {"sessionId": result.get("session_id"), "turnId": result.get("turn_id"),
                "status": result.get("status"), "startedAt": result.get("started_at"),
                "endedAt": result.get("ended_at"),
                "errorCode": result.get("error_code")}

    def session_get(self, session_id: str, **kwargs) -> dict:
        result = self._call("kp_runtime_messages", {
            "session_id": session_id,
            **({"before": kwargs["message_before"]} if kwargs.get("message_before") is not None else {}),
            **({"limit": kwargs["message_limit"]} if kwargs.get("message_limit") is not None else {}),
        })
        if result.get("truncated"):
            return {"truncated": True, "reason": result.get("reason", "MCP_RESULT_LIMIT")}
        return {"session": {
            "id": session_id, "messages": result.get("messages", []),
            "messageStart": result.get("message_start"),
            "messageEnd": result.get("message_end"),
            "hasMoreBefore": result.get("has_more_before", False),
            "hasMoreAfter": result.get("has_more_after", False),
        }}

    def plans_pending(self, session_id: str) -> dict:
        result = self._call("kp_runtime_proposals", {"session_id": session_id})
        proposals = result.get("proposals")
        if not isinstance(proposals, list):
            raise util.KanpilotError("protocol_error", "runtime proposals 未返回 proposals 数组")
        pending = {"plans": proposals}
        history = result.get("history")
        if history is not None:
            if not isinstance(history, list):
                raise util.KanpilotError("protocol_error", "runtime proposals history 格式无效")
            pending["history"] = history
        return pending

    def plans_resolve(self, proposal_id: str, session_id: str, turn_id: str,
                      tool_call_id: str, action: str,
                      target_permission_mode=None, version: Optional[int] = None,
                      **_kwargs) -> dict:
        normalized_action = "accept" if action == "approve" else action
        result = self._write("kp_runtime_proposal_resolve", f"proposal_{proposal_id}_{normalized_action}", {
            "session_id": session_id, "turn_id": turn_id,
            "proposal_id": proposal_id, "tool_call_id": tool_call_id,
            "version": version, "action": normalized_action,
            **({"target_permission_mode": target_permission_mode}
               if target_permission_mode else {}),
        })
        detail = result.get("detail")
        resolved = dict(detail) if isinstance(detail, dict) else {}
        resolved.update({
            "session_id": result.get("session_id"),
            "proposal_id": result.get("proposal_id"),
            "action": result.get("action"),
            "resolved": result.get("resolved"),
        })
        return resolved

    def agent_stop(self, session_id: str) -> dict:
        return self._write("kp_runtime_stop", "stop", {"session_id": session_id})

    def agent_abort(self, session_id: str) -> dict:
        return self._write("kp_runtime_abort", "abort", {"session_id": session_id})

    def goal_report_list(self, session_id: str) -> dict:
        return self._call("kp_runtime_goal_report_list", {"session_id": session_id})

    def goal_report_get(self, session_id: str, *, execution_id=None,
                        report_id=None) -> dict:
        return self._call("kp_runtime_goal_report_get", {
            "session_id": session_id,
            **({"execution_id": execution_id} if execution_id is not None else {}),
            **({"report_id": report_id} if report_id is not None else {}),
        })


class HistoricalReadClient:
    """Internal-adapter-only reader for pre-scope sessions with durable job identity."""

    def __init__(self, connection_file: str, timeout: float = 10.0):
        self.connection_file = str(Path(connection_file).expanduser())
        self.timeout = timeout

    @property
    def _timeout(self) -> float:
        return self.timeout

    @_timeout.setter
    def _timeout(self, value: float) -> None:
        self.timeout = float(value)

    def connect(self, required_tools=()) -> None:
        caps = harness_plus_adapter.plus_capabilities(
            self.connection_file, timeout=max(1, int(self.timeout)),
        )
        if caps.get("available") is not True:
            raise util.KanpilotError("remote_unavailable", str(caps.get("detail") or "Plus 控制面不可用"))
        supported = set(caps.get("supported_operations") or ())
        pid = caps.get("pid")
        self.harness_pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        missing = set(required_tools) - supported
        if missing:
            raise util.KanpilotError("unsupported_tools", "Plus 历史只读工具缺失：" + "、".join(sorted(missing)))

    def reconnect(self) -> "HistoricalReadClient":
        client = HistoricalReadClient(self.connection_file, self.timeout)
        client.connect()
        return client

    def turn_get(self, session_id: str, turn_id: str) -> dict:
        result = harness_plus_adapter.plus_turn_get(
            self.connection_file, session_id=session_id,
            turn_id=turn_id, timeout=max(1, int(self.timeout)),
        )
        return {"sessionId": result.get("session_id"),
                "turnId": result.get("turn_id"), "status": result.get("status"),
                "startedAt": result.get("started_at"),
                "endedAt": result.get("ended_at"),
                "errorCode": result.get("error_code")}

    def session_get(self, session_id: str, **kwargs) -> dict:
        result = harness_plus_adapter.plus_messages(
            self.connection_file, session_id=session_id,
            before=kwargs.get("message_before"),
            limit=kwargs.get("message_limit"),
            timeout=max(1, int(self.timeout)),
        )
        if result.get("truncated"):
            return {"truncated": True, "reason": result.get("reason", "MCP_RESULT_LIMIT")}
        return {"session": {
            "id": session_id, "messages": result.get("messages", []),
            "messageStart": result.get("message_start"),
            "messageEnd": result.get("message_end"),
            "hasMoreBefore": result.get("has_more_before", False),
            "hasMoreAfter": result.get("has_more_after", False),
        }}

    def agent_status(self, session_id: str) -> dict:
        result = harness_plus_adapter.plus_status(
            self.connection_file, session_id=session_id,
            timeout=max(1, int(self.timeout)),
        )
        return result.get("raw_status") or {
            "isRunning": result.get("is_running"),
            "currentTurnId": result.get("current_turn_id"),
        }

    def plans_pending(self, session_id: str) -> dict:
        result = harness_plus_adapter.plus_proposals(
            self.connection_file, session_id=session_id,
            timeout=max(1, int(self.timeout)),
        )
        return {"plans": result.get("proposals", []),
                "history": result.get("history", [])}

    def _deny_write(self, *_args, **_kwargs):
        raise util.KanpilotError(
            "forbidden",
            "恢复为只读，待处理 proposal 需人工确认；未重发任务",
        )

    def goal_report_list(self, session_id: str) -> dict:
        return harness_plus_adapter.plus_goal_report_list(
            self.connection_file, session_id=session_id,
            timeout=max(1, int(self.timeout)),
        )

    def goal_report_get(self, session_id: str, *, execution_id=None,
                        report_id=None) -> dict:
        return harness_plus_adapter.plus_goal_report_get(
            self.connection_file, session_id=session_id,
            execution_id=execution_id, report_id=report_id,
            timeout=max(1, int(self.timeout)),
        )

    session_create = _deny_write
    session_configure = _deny_write
    attach_mcp = _deny_write
    agent_prompt = _deny_write
    plans_resolve = _deny_write
    agent_stop = _deny_write
    agent_abort = _deny_write
