"""历史 Plus 控制端及 v5 确定性路由测试（protocol.md §5.24.4 / §5.27）。

覆盖契约条款：
- 历史入口：直接调用 Plus 控制器，建 ``mode="agent"`` 控制会话运行既有角色
  prompt，**零** ``pi -p`` 子进程（Popen spy）。新 dispatch 路由不受旧
  ``exec_config.controller_backend`` 影响，始终调用确定性流程，不拉起模型。
- 宿主环境显式传入：固定包装器注入 KANPILOT_DB/APP_SUPPORT/PLAN_ROOT/
  REPOS_ROOT/LEGACY_ROOT/KANEO_CREDENTIALS_PATH + 核心脚本路径；把宿主默认
  环境指向**错误夹具**（decoy DB）后仍命中隔离库；包装器/prompt/产物无 token。
- 产物与运行记录：``<APP_SUPPORT>/executor-sessions/<stage>/<invocation_id>/``
  的 job/events/transcript + 包装器；``executor_runs[stage]`` 增
  backend/session_id/turn_id/invocation_id/config/send_state，保留旧字段。
- 发送与恢复：发送前 ``send_state=unknown``；响应丢失不重发；已有 session/turn
  只只读重连；单飞含持久活跃/unknown 控制轮；超时 stop→abort 有界收尾。
- 不越权判定：控制回合 completed 只表示控制模型回合结束（不写任务状态）。

隔离：全程无真实 PI-Desktop——fake MCP server 是本文件自写的 http.server
线程（JSON-RPC + Bearer + Mcp-Session-Id + turn store + 分页 session_get +
plans 拒绝 + 权限等待开关）。
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    auto_dispatch, constants, db, executor_runner, mcp_server, mcp_scopes,
    pi_desktop_backend, pi_desktop_controller,
    util,
)

TOKEN = "test-plus-token-0123456789abcdef"
SESSION_ID = "plus-sess-1"
TURN_ID = "turn-1"
ALL_TOOLS = tuple(constants.PI_DESKTOP_DISPATCH_TOOLS) + ("pi_session_attach_mcp",)

PI_DESKTOP_BLOCK = {
    "provider_id": "test-provider",
    "model_id": "test-model",
    "thinking_level": "low",
    "permission_mode": "auto",
}

# 控制端测试共用任务标识（review 测试同样引用）。
TASK_ID = "taskplus0000000000000001"


# ------------------------------------------------------------ fake MCP server


class _FakeMCPHandler(http.server.BaseHTTPRequestHandler):
    """PI-Desktop 控制面 stub（streamable-http JSON-RPC over POST /mcp）。"""

    def log_message(self, *args):  # 静默
        pass

    def _send(self, code, obj=None, headers=None):
        body = json.dumps(obj).encode("utf-8") if obj is not None else b""
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server 约定
        srv = self.server._owner
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        srv.requests.append({
            "authorization": self.headers.get("Authorization"),
            "body": raw.decode("utf-8", "replace"),
        })
        if self.headers.get("Authorization") != f"Bearer {srv.token}":
            self._send(401, {"error": "unauthorized"})
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self._send(400, {"error": "bad json"})
            return
        method = msg.get("method")
        if method == "initialize":
            srv.init_count += 1
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "serverInfo": {"name": "fake-plus", "version": "0.1.0"},
                },
            }, headers={"Mcp-Session-Id": srv.mcp_session_id})
            return
        if method and method.startswith("notifications/"):
            self._send(202)
            return
        if method == "tools/list":
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {"tools": [{"name": n} for n in srv.tools]},
            })
            return
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            srv.calls.append((name, dict(args)))
            if name == "pi_agent_prompt" and srv.prompt_mode == "http500":
                srv.prompt_count += 1
                srv.prompt_content = args.get("content")
                self._send(500, {"error": "response lost mid-flight"})
                return
            if name == "pi_agent_stop" and srv.stop_mode == "http500":
                self._send(500, {"error": "stop response lost"})
                return
            if name == "pi_agent_abort" and srv.abort_mode == "http500":
                self._send(500, {"error": "abort response lost"})
                return
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": srv.tool_call(name, args),
            })
            return
        self._send(200, {
            "jsonrpc": "2.0", "id": msg.get("id"),
            "error": {"code": -32601, "message": "method not found"},
        })


class FakeMCPServer:
    """线程内 fake 控制面：turn 脚本 + 分页消息库 + 行为开关。"""

    def __init__(self, tools=ALL_TOOLS):
        self.token = TOKEN
        self.mcp_session_id = "mcp-test-session"
        self.tools = list(tools)
        self.requests: list = []
        self.calls: list = []
        self.init_count = 0
        self.session_id = SESSION_ID
        self.turn_status = "completed"
        self.turn_script: list = []        # 每次 pi_turn_get 依次弹出，耗尽回落
        self.turn_missing = False
        self.pending_tool_confirmations: list = []
        self.messages: list = []
        self.page_size = 1000
        self.plans: list = []
        self.plans_resolve_mode = "ok"
        self.prompt_mode = "ok"            # ok | http500
        self.prompt_count = 0
        self.prompt_content = None
        self.interaction_mode = "unattended"
        self.interaction_policy_version = 1
        self.created = None
        self.created_count = 0
        self.configured = None
        self.configured_count = 0
        self.stop_calls = 0
        self.abort_calls = 0
        self.stop_aborts = False
        self.stop_mode = "ok"
        self.abort_mode = "ok"
        self.providers = [{"id": "test-provider", "disabled": False}]
        self.models = [{"id": "test-model", "modelId": "test-model"}]
        self.model_source = "cache"
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeMCPHandler
        )
        self.httpd.token = self.token
        self.httpd.mcp_session_id = self.mcp_session_id
        self.httpd.tools = self.tools
        self.httpd._owner = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    # ------------------------------------------------------------- 工具
    def tool_call(self, name, args):
        if name == "pi_desktop_invoke":
            operation = args.get("operation")
            if operation == "providers/list":
                return {"structuredContent": {
                    "operation": operation,
                    "result": {"providers": self.providers},
                }}
            if operation == "providers/listModels":
                return {"structuredContent": {
                    "operation": operation,
                    "result": {"models": self.models,
                               "source": self.model_source},
                }}
            return {"structuredContent": {"error": {
                "code": "NOT_FOUND", "message": operation}}}
        if name == "pi_session_create":
            self.created = dict(args)
            self.created_count += 1
            return {"structuredContent": {
                "session": {"id": self.session_id, "title": args.get("title"),
                "interactionMode": self.interaction_mode, "interactionPolicyVersion": self.interaction_policy_version},
            }}
        if name == "pi_session_configure":
            self.configured = dict(args)
            self.configured_count += 1
            return {"structuredContent": {"ok": True, "session": {"id": self.session_id, "interactionMode": self.interaction_mode, "interactionPolicyVersion": self.interaction_policy_version}}}
        if name == "pi_session_attach_mcp":
            grant_path = Path(args["bridgeFile"])
            grant = json.loads(grant_path.read_text(encoding="utf-8"))
            role = grant.get("role")
            return {"structuredContent": {
                "sessionId": args.get("sessionId"), "attached": True,
                "serverName": constants.MCP_SERVER_NAME, "role": role,
                "toolNames": sorted(mcp_scopes.TASK_ROLE_TOOLS[role]),
            }}
        if name == "pi_agent_prompt":
            self.prompt_count += 1
            self.prompt_content = args.get("content")
            return {"structuredContent": {"accepted": True, "turnId": TURN_ID}}
        if name == "pi_agent_status":
            return {"structuredContent": {"status": {
                "sessionId": args.get("sessionId"),
                "isRunning": self.turn_status not in ("completed", "error",
                                                      "aborted"),
                "currentTurnId": TURN_ID,
                "pendingToolConfirmations": list(self.pending_tool_confirmations),
            }}}
        if name == "pi_turn_get":
            if self.turn_missing:
                return {"isError": True, "structuredContent": {"error": {
                    "code": "NOT_FOUND", "message": "turn not found"}}}
            status = (self.turn_script.pop(0)
                      if self.turn_script else self.turn_status)
            return {"structuredContent": {
                "sessionId": args.get("sessionId"),
                "turnId": args.get("turnId"),
                "status": status,
            }}
        if name == "pi_session_get":
            before = args.get("messageBefore")
            limit = min(args.get("messageLimit") or 200, self.page_size)
            end = len(self.messages) if before is None else int(before)
            pool = self.messages[:end]
            page = pool[-limit:]
            start = end - len(page)
            return {"structuredContent": {"session": {
                "id": self.session_id,
                "interactionMode": self.interaction_mode, "interactionPolicyVersion": self.interaction_policy_version,
                "messages": page,
                "messageStart": start if page else None,
                "messageEnd": end if page else None,
                "hasMoreBefore": bool(page) and start > 0,
                "hasMoreAfter": before is not None and len(page) < len(pool),
            }}}
        if name == "pi_plans_pending":
            pending = [dict(item) for item in self.plans
                       if item.get("status") == "pending"]
            return {"structuredContent": {"plans": pending}}
        if name == "pi_plans_resolve":
            if self.plans_resolve_mode != "ok":
                return {"structuredContent": {"error": {
                    "code": "INTERNAL", "message": "resolve failed"}}}
            for item in self.plans:
                if item.get("id") == args.get("proposalId"):
                    action = args.get("action")
                    item["status"] = (
                        "approved" if action == "approve" else "rejected"
                    )
            return {"structuredContent": {
                "ok": True, "action": args.get("action")}}
        if name in ("pi_agent_stop", "pi_agent_abort"):
            if name == "pi_agent_stop":
                self.stop_calls += 1
                if self.stop_aborts:
                    self.turn_status = "aborted"
            else:
                self.abort_calls += 1
                self.turn_status = "aborted"
            return {"structuredContent": {"ok": True}}
        return {"structuredContent": {"error": {
            "code": "NOT_FOUND", "message": f"unknown tool {name}"}}}


def assistant_json(payload: dict) -> dict:
    """最终 assistant 消息（冻结 JSON 答复）。"""
    return {
        "id": "msg-final", "role": "assistant",
        "content": json.dumps(payload, ensure_ascii=False),
        "timestamp": "2026-09-20T00:00:09Z",
    }


def default_messages() -> list:
    return [
        {"id": "msg-1", "role": "user", "content": "控制指令回显",
         "timestamp": "2026-09-20T00:00:00Z"},
        {"id": "msg-2", "role": "assistant", "content": "已按角色流程执行。",
         "timestamp": "2026-09-20T00:00:05Z"},
    ]


# ------------------------------------------------------------ 测试基类


class PlusCase(KanpilotTestCase):
    """Plus 控制端共享接缝：连接文件、控制端配置、时序加速、运行记录读取。"""

    def setUp(self):
        super().setUp()
        self.server = FakeMCPServer().start()
        self.addCleanup(self.server.stop)
        # 控制轮与包装器必须命中同一个隔离库：runner 读 KANPILOT_DB，包装器把它
        # 显式传给子进程——这里钉住进程级环境缝（helpers 不自带 KANPILOT_DB）。
        self._saved_db_env = os.environ.get("KANPILOT_DB")
        os.environ["KANPILOT_DB"] = str(self.db_path)
        self.addCleanup(self._restore_db_env)
        self.connection_file = self.write_connection()
        self.project_mcp = mcp_server.MCPServer(
            db_path=self.db_path, app_support_dir=self.app_support,
            connection_file=self.app_support / "mcp" / "server.json",
        )
        self.project_mcp.start()
        self.addCleanup(self.project_mcp.stop)
        self.fast_clock()
        self.seed_plus_settings()

    def _restore_db_env(self):
        if self._saved_db_env is None:
            os.environ.pop("KANPILOT_DB", None)
        else:
            os.environ["KANPILOT_DB"] = self._saved_db_env

    def fast_clock(self):
        for name, value in (
            ("_POLL_SECONDS", 0.01),
            ("_PROOF_WAIT_SECONDS", 0.4),
            ("_STOP_VERIFY_SECONDS", 0.2),
            ("_STOP_GRACE_SECONDS", 0.2),
        ):
            saved = getattr(pi_desktop_controller, name)
            setattr(pi_desktop_controller, name, value)
            self.addCleanup(setattr, pi_desktop_controller, name, saved)

    def write_connection(self, **overrides) -> Path:
        data = {
            "active": True,
            "url": self.server.url,
            "token": TOKEN,
            "pid": os.getpid(),
            "startedAt": util.now_iso(),
        }
        data.update(overrides)
        path = self.root / "pi-desktop" / "mcp-control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        return path

    def set_setting(self, name, value):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, name, value)
        finally:
            conn.close()

    def setting(self, name):
        conn = db.connect(self.db_path)
        try:
            return db.get_setting(conn, name)
        finally:
            conn.close()

    def seed_plus_settings(self, **exec_overrides):
        block = dict(PI_DESKTOP_BLOCK)
        block["connection_file"] = str(self.connection_file)
        block.update(exec_overrides.pop("block", {}))
        self.set_setting("exec_config", {
            "controller_backend": exec_overrides.pop("controller_backend",
                                                      "pi_desktop"),
            "pi_desktop": block,
        })
        self.set_setting("review_config", {
            "mode": "inherit", "backend": "pi_desktop", "pi_desktop": block,
        })
        for key, value in exec_overrides.items():
            self.set_setting(key, value)

    def run_stage(self, stage, resume=False):
        """历史控制轮单独验证，不经过 v5 的确定性 dispatch/monitor 路由。"""
        conn = db.connect(self.db_path)
        try:
            return pi_desktop_controller.run_stage(
                stage, conn=conn, db_path=self.db_path,
                prompt=executor_runner.render_prompt(stage),
                timeout=executor_runner._timeout_seconds(conn),
                log_path=executor_runner._log_path(stage),
                record_run=lambda **fields: executor_runner._record_run(
                    conn, stage, **fields),
                resume=resume,
            )
        finally:
            conn.close()

    def executor_run(self, stage):
        runs = self.setting("executor_runs") or {}
        return runs.get(stage) or {}

    def invocation_dir(self, stage, invocation_id=None):
        invocation_id = invocation_id or self.executor_run(stage)["invocation_id"]
        return (constants.app_support() / "executor-sessions" / stage
                / invocation_id)

    def read_events(self, directory):
        path = Path(directory) / "events.jsonl"
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def read_job(self, directory):
        return json.loads(
            (Path(directory) / "job.json").read_text(encoding="utf-8")
        )

    def no_subprocess_spy(self):
        """Popen spy：允许本地只读工具（git），但拉起 pi/codex 模型 CLI 即失败。

        Plus 控制端**不得**拉起 ``pi -p``/``codex`` 这类模型运行端；本地 git
        只读材料准备属 runner 自身行为。
        """
        self.subprocess_calls = []
        real = subprocess.Popen

        def spy(argv, *args, **kwargs):
            command = [
                str(part) for part in
                (argv if isinstance(argv, (list, tuple)) else [argv])
            ]
            head = command[0] if command else ""
            if head == constants.pi_bin() or os.path.basename(head) in (
                "pi", "codex",
            ):
                raise AssertionError(
                    f"Plus 分支不得拉起模型 CLI 子进程：{command}"
                )
            self.subprocess_calls.append(command)
            return real(argv, *args, **kwargs)

        return mock.patch.object(subprocess, "Popen", spy)


# ------------------------------------------------------------ 宿主环境/包装器
# ------------------------------------------------------------ dispatch 控制轮
# ------------------------------------------------------------ monitor 控制轮
if __name__ == "__main__":  # pragma: no cover
    unittest.main()
