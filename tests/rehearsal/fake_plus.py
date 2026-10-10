"""假 PI-Desktop MCP 控制面（tests/rehearsal 夹具）。

按 ``core/kanpilot/pi_desktop_backend.py`` 的 ``MCPClient`` 实际协议实现：
streamable-http POST + JSON-RPC 2.0（initialize / notifications/initialized /
tools/list / tools/call），响应体经 ``structuredContent`` 返回工具结果。
绝不发起任何网络调用（仅 127.0.0.1 回环），token 只作握手校验、不落任何
业务断言之外的日志文件。

故障注入（``faults`` 或环境变量 ``KANPILOT_REHEARSAL_FAULT``）：
- ``plus_unreachable``：占端口但不监听 → 连接被拒（真实失败，非超时）；
- ``plus_timeout``：所有请求挂起 → 探测超时（区别于不可达）；
- ``plus_auth_fail``：恒 401（token 失效形态）；
- ``plus_tools_missing``：tools/list 缺必需工具（旧 Plus 形态）。
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

# probe_connection 默认工具门（constants.PI_DESKTOP_DISPATCH_TOOLS）。
REQUIRED_TOOLS = (
    "pi_turn_get", "pi_session_create", "pi_session_configure",
    "pi_agent_prompt", "pi_agent_status", "pi_session_get",
    "pi_agent_stop", "pi_agent_abort", "pi_desktop_invoke",
    "pi_plans_pending", "pi_plans_resolve",
)

PROTOCOL_VERSION = "2025-06-18"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class _PlusServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


class FakePlus:
    """线程内 MCP stub；行为可编程（会话/轮次/状态/消息）且可注入故障。"""

    def __init__(self, faults=(), token="rehearsal-plus-token"):
        self.faults = set(faults)
        self.token = token
        self.require_auth = True
        self.log = []                  # (method, rpc_method, params)
        self.sessions = {}             # session_id -> {"title": ...}
        self.turns = {}                # (session_id, turn_id) -> turn record
        self.statuses = {}             # session_id -> status inner dict
        self.messages = {}             # session_id -> [message, ...]
        self.providers = [
            {"id": "rehearsal-provider", "displayName": "Rehearsal", "disabled": False},
        ]
        self.models = [{"id": "rehearsal-model", "modelId": "rehearsal-model"}]
        self._server = None
        self._thread = None
        self.port = None

    # -- 编程接口 -----------------------------------------------------------

    def add_session(self, session_id=None, title="rehearsal session"):
        session_id = session_id or ("sess-" + uuid.uuid4().hex[:12])
        self.sessions[session_id] = {"id": session_id, "title": title}
        self.statuses.setdefault(session_id, {
            "currentTurnId": None, "planningState": "idle",
            "isRunning": False, "pendingPermission": None,
        })
        self.messages.setdefault(session_id, [])
        return session_id

    def set_turn(self, session_id, turn_id, **fields):
        record = {
            "sessionId": session_id, "turnId": turn_id,
            "status": "running", "startedAt": _iso_now(),
            "endedAt": None, "errorCode": None,
        }
        record.update(fields)
        self.turns[(session_id, turn_id)] = record
        return record

    def set_agent_status(self, session_id, **fields):
        status = dict(self.statuses.get(session_id) or {
            "currentTurnId": None, "planningState": "idle",
            "isRunning": False, "pendingPermission": None,
        })
        status.update(fields)
        self.statuses[session_id] = status
        return status

    def add_message(self, session_id, message):
        self.messages.setdefault(session_id, []).append(message)
        return message

    def requests_to(self, rpc_method):
        return [params for (_m, method, params) in self.log if method == rpc_method]

    # -- 生命周期 -----------------------------------------------------------

    def start(self):
        if "plus_unreachable" in self.faults:
            # 占一个空闲端口后立刻关闭：连接文件指向它 → 连接被拒。
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
            return self
        handler = self._make_handler()
        self._server = _PlusServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def write_connection_file(self, path):
        """写 mcp-control.json 形状连接文件（pid 取当前进程，保证存活校验通过）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "url": self.base_url + "/mcp",
            "token": self.token,
            "pid": os.getpid(),
            "active": True,
            "startedAt": _iso_now(),
        }), encoding="utf-8")
        path.chmod(0o600)
        return path

    # -- HTTP / JSON-RPC 层 ---------------------------------------------------

    def _make_handler(self):
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静默
                pass

            def _send(self, status, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_202(self):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                if "plus_timeout" in outer.faults:
                    time.sleep(60)     # 超过任何合理探针超时
                    return
                if "plus_auth_fail" in outer.faults:
                    self._send(401, {"error": "unauthorized"})
                    return
                if outer.require_auth:
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {outer.token}":
                        self._send(401, {"error": "unauthorized"})
                        return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    message = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._send(400, {"error": "invalid JSON"})
                    return
                method = message.get("method")
                params = message.get("params") or {}
                outer.log.append(("POST", method, params))
                if "id" not in message:            # 通知 → 202 空响应
                    self._send_202()
                    return
                result = outer._dispatch_rpc(method, params)
                self._send(200, {"jsonrpc": "2.0", "id": message["id"],
                                 "result": result})

        return _Handler

    def _dispatch_rpc(self, method, params):
        if method == "initialize":
            return {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "serverInfo": {"name": "fake-plus",
                                   "version": "0.0.0-rehearsal"}}
        if method == "tools/list":
            tools = []
            names = REQUIRED_TOOLS
            if "plus_tools_missing" in self.faults:
                names = tuple(n for n in REQUIRED_TOOLS
                              if n not in ("pi_plans_pending", "pi_plans_resolve"))
            for name in names:
                schema = {"type": "object", "properties": {}}
                if name == "pi_session_create":
                    # T6 兼容门：必须暴露 projectName。
                    schema["properties"]["projectName"] = {"type": "string"}
                tools.append({"name": name, "inputSchema": schema})
            return {"tools": tools}
        if method == "tools/call":
            return {"structuredContent": self._call_tool(
                params.get("name"), params.get("arguments") or {})}
        return {}

    def _call_tool(self, name, args):
        if name == "pi_agent_status":
            session_id = args.get("sessionId")
            return self.statuses.get(session_id) or {
                "currentTurnId": None, "planningState": "idle",
                "isRunning": False, "pendingPermission": None,
            }
        if name == "pi_turn_get":
            record = self.turns.get((args.get("sessionId"), args.get("turnId")))
            return {"turn": dict(record)} if record else {}
        if name == "pi_session_get":
            session_id = args.get("sessionId")
            messages = list(self.messages.get(session_id) or [])
            start = int(args.get("messageStart") or 0)
            end = int(args.get("messageEnd") or len(messages))
            page = messages[start:end]
            return {
                "session": {
                    "id": session_id,
                    "title": (self.sessions.get(session_id) or {}).get("title"),
                    "messages": page,
                    "messageStart": start,
                    "messageEnd": start + len(page),
                    "hasMoreBefore": start > 0,
                    "hasMoreAfter": start + len(page) < len(messages),
                },
                "truncated": False,
            }
        if name == "pi_session_create":
            session_id = self.add_session(
                title=args.get("title") or args.get("projectName")
                or "rehearsal session")
            return {"session": dict(self.sessions[session_id])}
        if name == "pi_agent_prompt":
            turn_id = "turn-" + uuid.uuid4().hex[:12]
            self.set_turn(args.get("sessionId"), turn_id)
            return {"turnId": turn_id}
        if name == "pi_desktop_invoke":
            operation = args.get("operation")
            if operation == "providers/list":
                return {"operation": operation,
                        "result": {"providers": self.providers}}
            if operation == "providers/listModels":
                return {"operation": operation,
                        "result": {"models": self.models, "source": "cache"}}
            return {"operation": operation, "result": {}}
        if name == "pi_plans_pending":
            return {"history": []}
        return {}
