"""pi_desktop 后端测试（protocol §5.23.3，v3 T3）。

覆盖契约条款：
- ``pi_desktop_backend``：连接文件校验矩阵（不存在/权限不安全/active=false/
  非 loopback/pid 不活/缺 token）→ probe ok=False + 中文原因；initialize +
  tools/list + tools/call 全链；Authorization Bearer 断言；token 绝不出现在
  任何返回/异常字符串；Mcp-Session-Id 丢失自动重连一次；缺 pi_turn_get →
  不可用；请求体 >2 MiB 发送前拒绝。
- ``pi_desktop_worker``：KANPILOT_PI_DESKTOP_FAKE 五种分支；run_real 完整流
  （session_create → configure → prompt → turn_get 证明 + heartbeat → 首个
  session 事件 → 分页 session_get 采集 → turn 终态判定）；pi_turn_get 四态
  映射（running→completed/completed/error/aborted）；发送响应丢失 →
  send_state=unknown + 证据保留 + 绝不重发；计划超限/哈希不符 → 发送前失败；
  分页断裂 → transcript_incomplete 不写成功 terminal；超时 stop/abort；
  启动证明失败不写 session 事件。
- dispatch e2e：pi_desktop 轮次 claim → prepare → begin-send → start
  （拉起真 subprocess fake worker）→ confirm（turn_id 核验）。

隔离：全程无真实 PI-Desktop——fake MCP server 是本文件自写的
http.server 线程（JSON-RPC + Bearer + Mcp-Session-Id + turn store +
分页 session_get）。
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from helpers import CORE, KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    constants, db, dsh_events, mcp_scopes, mcp_server, pi_desktop_backend,
    pi_desktop_worker, round_store, util, cmd_dispatch,
)

TASK_ID = "taskpidesktop000000000001"
RUN_ID = "runpidesktop01"
TOKEN = "test-mcp-token-0123456789abcdef"
SESSION_ID = "pd-sess-1"
TURN_ID = "turn-1"
FOLLOW_UP_TURN_ID = "turn-2"
ALL_TOOLS = (tuple(constants.PI_DESKTOP_REQUIRED_TOOLS)
            + tuple(constants.PI_DESKTOP_GOAL_TOOLS) + tuple(constants.PI_DESKTOP_INPUT_TOOLS))


# ------------------------------------------------------------ fake MCP server


class _FakeMCPHandler(http.server.BaseHTTPRequestHandler):
    """PI-Desktop 控制面 stub：streamable-http JSON-RPC over POST。"""

    def log_message(self, *args):  # 静默（不占测试输出）
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
        # handler 的可变状态一律落在 FakeMCPServer 实例上（经 httpd._owner
        # 桥接；否则 httpd 上的影子字段会吞掉 init_count/drop_next_404 等
        # 写操作，断言读不到）。
        srv = self.server._owner
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        srv.requests.append({
            "authorization": self.headers.get("Authorization"),
            "mcp_session": self.headers.get("Mcp-Session-Id"),
            "body": raw[:200],
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
                    "protocolVersion": srv.protocol_version,
                    "capabilities": {},
                    "serverInfo": {"name": "fake-pi-desktop",
                                   "version": "0.15.6"},
                },
            }, headers={"Mcp-Session-Id": srv.mcp_session_id})
            return
        if srv.drop_next_404 and method != "initialize":
            srv.drop_next_404 = False
            self._send(404, {"error": "unknown MCP session"})
            return
        if method and method.startswith("notifications/"):
            self._send(202)
            return
        if method == "tools/list":
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {"tools": [
                    {"name": name, "inputSchema": srv.tool_schema(name)}
                    for name in srv.tools
                ]},
            })
            return
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            srv.calls.append((name, args))
            if name == "pi_agent_prompt" and srv.prompt_mode == "http500":
                srv.prompt_count += 1
                srv.prompt_landed = True
                self._send(500, {"error": "response lost mid-flight"})
                return
            if name == "pi_agent_stop" and getattr(srv, "stop_mode", "ok") == "http500":
                self._send(500, {"error": "stop response lost"})
                return
            if name == "pi_agent_abort" and getattr(srv, "abort_mode", "ok") == "http500":
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
    """线程内 fake 控制面：turn store + 分页消息库 + 行为开关。"""

    def __init__(self, tools=ALL_TOOLS):
        self.token = TOKEN
        self.mcp_session_id = "mcp-test-session"
        self.protocol_version = "2025-06-18"
        self.tools = list(tools)
        self.requests: list = []
        self.calls: list = []
        self.init_count = 0
        self.session_id = SESSION_ID
        self.turn_status = "running"
        self.turn_statuses: list = []     # 队列优先，耗尽回落 turn_status
        self.turn_error_code = None
        self.follow_up_turn = None        # 瞬时错误后同一会话自动重开的新 turn
        self.turn_missing = False
        self.current_turn = None
        self.is_running = True
        self.messages: list = []
        self.page_size = 1000             # 调小可强制 worker 翻页
        self.session_lost = False
        self.before_broken = False        # messageBefore 页返回空 session
        self.prompt_mode = "ok"           # ok | http500
        self.prompt_count = 0
        self.prompt_landed = False
        self.prompt_content = None
        self.configured = None
        self.created = None              # pi_session_create 入参（断言 mode=goal）
        self.plans: list = []            # pi_plans_pending 的待批准契约
        self.plans_state = "inactive"
        self.plans_pending_calls = 0
        self.plans_resolve_calls: list = []
        self.plans_resolve_mode = "ok"   # ok | error
        self.execution_turn = None       # 契约获批后宿主开启的执行轮次
        self.planning_state = "inactive"  # goal 契约模式投影
        self.stop_calls = 0
        self.abort_calls = 0
        self.stop_aborts = False          # agent_stop → turn 立即 aborted
        self.stop_mode = "ok"
        self.abort_mode = "ok"
        self.drop_next_404 = False
        self.providers = [{"id": "test-provider", "disabled": False}]
        self.models = [{"id": "test-model", "modelId": "test-model"}]
        self.settings = {}
        self.attach_calls: list = []
        self.model_source = "cache"
        # T6（§5.23.10）：pi_session_create 的 inputSchema 是否暴露 projectName。
        # 旧 Plus → False（兼容门必须明确阻断）。
        self.session_naming = True
        # T5：Goal 报告只读工具的行为开关。
        self.goal_reports: list = []      # list 结果（summary 行）
        self.goal_report: dict = None     # get 结果（完整报告 + report_sha256）
        self.goal_report_calls: list = []
        self.goal_report_mode = "ok"      # ok | error | timeout
        self.created_response_names = None  # 覆写 create 返回的显示名
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeMCPHandler
        )
        self.httpd.token = self.token
        self.httpd.mcp_session_id = self.mcp_session_id
        self.httpd.tools = self.tools
        self.httpd.owner = self           # tool_call 委托到本对象
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        # handler 需要访问 FakeMCPServer 属性（而不只是 httpd）。
        self.httpd._owner = self

    # handler 通过 self.server 访问；把可变状态代理到 httpd。
    def __getattr__(self, name):
        # __getattr__ 只在常规查找失败时调用；server 状态放 httpd 上。
        raise AttributeError(name)

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

    def tool_schema(self, name):
        """``tools/list`` 的 inputSchema（T6 兼容门读取 projectName）。"""
        if name == "pi_plans_resolve" and getattr(self, "required_verification_commands", False):
            return {"type": "object", "properties": {
                "requiredVerificationCommands": {"type": "array", "items": {"type": "string"}}}}
        if name == "pi_session_create":
            properties = {
                "projectPath": {"type": "string"},
                "title": {"type": "string"},
                "mode": {"type": "string"},
            }
            if self.session_naming:
                properties["projectName"] = {"type": "string"}
            return {"type": "object", "properties": properties}
        return {"type": "object", "properties": {}}

    def tool_call(self, name, args):
        if name == "pi_desktop_invoke":
            operation = args.get("operation")
            if operation == "providers/list":
                return {"structuredContent": {
                    "operation": operation, "result": {
                        "providers": self.providers,
                    }}}
            if operation == "providers/listModels":
                return {"structuredContent": {
                    "operation": operation, "result": {
                        "models": self.models, "source": self.model_source,
                    }}}
            if operation == "settings/get":
                return {"structuredContent": {
                    "operation": operation, "result": dict(self.settings),
                }}
            return {"structuredContent": {"error": {
                "code": "NOT_FOUND", "message": operation}}}
        if name == "pi_session_create":
            self.created = dict(args)
            returned_title = args.get("title")
            returned_project = args.get("projectName")
            if self.created_response_names:
                # T6：模拟 Plus 返回的显示名与请求不符（身份不符场景）。
                returned_title = self.created_response_names.get(
                    "title", returned_title
                )
                returned_project = self.created_response_names.get(
                    "projectName", returned_project
                )
            return {"structuredContent": {
                "session": {"id": self.session_id,
                            "title": returned_title,
                            "projectPath": args.get("projectPath"),
                            "projectName": returned_project}}}
        if name == "pi_goal_report_list":
            self.goal_report_calls.append((name, dict(args)))
            if self.goal_report_mode == "list_error":
                return {"isError": True, "structuredContent": {"error": {
                    "code": "INTERNAL", "message": "report list unavailable"}}}
            return {"structuredContent": {"reports": [
                dict(item) for item in self.goal_reports
            ]}}
        if name == "pi_goal_report_get":
            self.goal_report_calls.append((name, dict(args)))
            if self.goal_report_mode == "error":
                return {"isError": True, "structuredContent": {"error": {
                    "code": "NOT_FOUND", "message": "report not found"}}}
            if self.goal_report is None:
                return {"isError": True, "structuredContent": {"error": {
                    "code": "NOT_FOUND", "message": "report not found"}}}
            payload = dict(self.goal_report)
            payload.setdefault("sessionId", args.get("sessionId"))
            if args.get("executionId"):
                payload.setdefault("executionId", args["executionId"])
            return {"structuredContent": payload}
        if name == "pi_session_configure":
            self.configured = args
            return {"structuredContent": {"ok": True}}
        if name == "pi_session_attach_mcp":
            grant_path = Path(args["bridgeFile"])
            grant = json.loads(grant_path.read_text(encoding="utf-8"))
            role = grant.get("role")
            tool_names = sorted(mcp_scopes.TASK_ROLE_TOOLS[role])
            self.attach_calls.append({
                "args": dict(args), "grant": grant,
                "tool_names": tool_names,
            })
            return {"structuredContent": {
                "sessionId": args.get("sessionId"), "attached": True,
                "serverName": constants.MCP_SERVER_NAME,
                "role": role,
                "toolNames": tool_names,
            }}
        if name == "pi_agent_prompt":
            self.prompt_count += 1
            self.prompt_landed = True
            self.prompt_content = args.get("content")
            self.current_turn = TURN_ID
            return {"structuredContent": {
                "accepted": True, "turnId": TURN_ID}}
        if name == "pi_agent_status":
            return {"structuredContent": {"status": {
                "sessionId": args.get("sessionId"),
                "isRunning": self.is_running,
                "currentTurnId": self.current_turn,
                "planningState": self.planning_state,
                "pendingToolConfirmations": []}}}
        if name == "pi_session_get":
            if self.session_lost:
                return {
                    "isError": True,
                    "structuredContent": {"error": {
                        "code": "NOT_FOUND",
                        "message": "session not found"}},
                }
            before = args.get("messageBefore")
            limit = args.get("messageLimit") or 200
            limit = min(limit, self.page_size)
            # Plus message identities are opaque strings; paging uses the
            # physical integer offsets returned as messageStart/messageEnd.
            end = len(self.messages) if before is None else int(before)
            pool = self.messages[:end]
            page = pool[-limit:]
            start = end - len(page)
            return {"structuredContent": {"session": {
                "id": self.session_id,
                "messages": page,
                "messageStart": start if page else None,
                "messageEnd": end if page else None,
                "hasMoreBefore": bool(page) and start > 0,
                "hasMoreAfter": before is not None and len(page) < len(pool),
            }}}
        if name == "pi_turn_get":
            if self.turn_missing:
                return {
                    "isError": True,
                    "structuredContent": {"error": {
                        "code": "NOT_FOUND",
                        "message": "turn not found"}},
                }
            status = (self.turn_statuses.pop(0)
                      if self.turn_statuses else self.turn_status)
            record = {
                "sessionId": args.get("sessionId"),
                "turnId": args.get("turnId"),
                "status": status,
            }
            if self.turn_error_code:
                record["errorCode"] = self.turn_error_code
            if status in ("error", "aborted") and self.follow_up_turn:
                # 瞬时错误：Plus 在同一会话自动重开新 turn（旧 turn 仍是
                # error，但会话在跑另一个 turn）——currentTurnId 随之前移。
                self.current_turn = self.follow_up_turn
                self.follow_up_turn = None
                self.turn_error_code = None
            return {"structuredContent": record}
        if name == "pi_plans_pending":
            self.plans_pending_calls += 1
            pending = [dict(item) for item in self.plans
                       if item.get("status") == "pending"]
            return {"structuredContent": {
                "plans": pending,
                "state": "awaiting_approval" if pending else self.plans_state,
                "kind": "goal",
            }}
        if name == "pi_plans_resolve":
            self.plans_resolve_calls.append(dict(args))
            if self.plans_resolve_mode != "ok":
                return {"structuredContent": {"error": {
                    "code": "INTERNAL", "message": "resolve failed"}}}
            if args.get("action") == "approve":
                for item in self.plans:
                    if item.get("id") == args.get("proposalId"):
                        item["status"] = "approved"
                self.plans_state = "inactive"
                self.planning_state = "inactive"   # 批准后会话切回 Agent
                # 宿主批准后在同一会话开启执行轮次（新的 turnId）。
                if self.execution_turn:
                    self.current_turn = self.execution_turn
            return {"structuredContent": {
                "ok": True, "action": args.get("action"),
                "targetPermissionMode": args.get("targetPermissionMode"),
                "execution": {"id": "exec-1", "state": "queued"},
            }}
        if name in ("pi_agent_stop", "pi_agent_abort"):
            if name == "pi_agent_stop":
                self.stop_calls += 1
            else:
                self.abort_calls += 1
            self.is_running = False
            if self.stop_aborts or name == "pi_agent_abort":
                self.turn_status = "aborted"
            return {"structuredContent": {"ok": True}}
        return {"structuredContent": {"error": {
            "code": "NOT_FOUND", "message": f"unknown tool {name}"}}}


def _sync(server: FakeMCPServer):
    """重绑 httpd.tool_call（测试覆写 server.tool_call 后调用）。

    其余可变字段不再经 httpd 桥接——handler 直接读写 ``server._owner``，
    断言看到的就是服务端真实状态。
    """
    server.httpd.tool_call = server.tool_call


# ------------------------------------------------------------ 测试基类

class PiDesktopCase(KanpilotTestCase):
    """共享接缝：连接文件、spec 构造、worker 时序加速、事件读取断言。"""
    def setUp(self):
        super().setUp()
        self.server = FakeMCPServer().start()
        self.addCleanup(self.server.stop)
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
        # dispatch-start / get-session 经 headless source 解析
        # <runs_root>/<task_id>/<run_id>——缺省指向真实用户目录，
        # 测试一律钉在隔离根下（写入点与读取点共用同一解析链）。
        self.runs_root = self.root / "session_runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(
                    conn, "session_runs_root", str(self.runs_root)
                )
        finally:
            conn.close()
        _sync(self.server)

    def _restore_db_env(self):
        if self._saved_db_env is None:
            os.environ.pop("KANPILOT_DB", None)
        else:
            os.environ["KANPILOT_DB"] = self._saved_db_env

    # ---------------------------------------------------------- 构造

    def write_connection(self, **overrides) -> Path:
        """mcp-control.json（0600）；overrides 可覆盖任一字段/权限。"""
        data = {
            "active": True,
            "url": self.server.url,
            "token": self.server.token,
            "pid": os.getpid(),
            "startedAt": util.now_iso(),
        }
        mode = overrides.pop("mode", 0o600)
        data.update(overrides)
        path = self.root / "pi-desktop" / "mcp-control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(mode)
        return path


    def expected_prompt(self, spec) -> str:
        """worker 实际发送的 prompt（instruction + 计划正文，§5.23.3）。"""
        text = Path(spec["plan_path"]).read_text(encoding="utf-8")
        body = text.split("---", 2)[2] if text.startswith("---\n") else text
        if body.startswith("\n"):
            body = body[1:]
        return (spec.get("instruction") or "").rstrip() + "\n\n" + body

    def mark_needs_user(self, task_id, run_id, failure_code="execution_failed"):
        """把任务置为 needs_user 阻塞（测试用；模拟监控端 report）。"""
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='needs_user', failure_code=?, "
                    "failure_reason=?, updated_at=? WHERE task_id=?",
                    (failure_code, "测试阻塞原因", util.now_iso(), task_id),
                )
                conn.execute(
                    "UPDATE execution_rounds SET run_status='failed', "
                    "updated_at=? WHERE task_id=? AND run_id=?",
                    (util.now_iso(), task_id, run_id),
                )
        finally:
            conn.close()

    def write_spec_plan(self, body: str) -> Path:
        plan = self.plan_root / "Feedback" / f"plan-{TASK_ID}.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        front = "task_id: t\n"
        plan.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
        return plan

    def seed_claimed_dispatch(
        self, task_id=TASK_ID, run_id=RUN_ID, status="claimed"
    ) -> str:
        """登记隔离任务、执行轮次与 dispatch 请求，供 runtime grant 使用。"""
        conn = db.connect(self.db_path)
        try:
            task_exists = db.get_task(conn, task_id) is not None
        finally:
            conn.close()
        if not task_exists:
            remote_task = self.fake.add_task(
                task_id, "ryryo1reph1xfemqtwkjyiiz",
                title=f"test task {task_id}",
            )
            self.sync_task(remote_task, project_name="Feedback")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                round_store.upsert_on_claim(
                    conn, task_id, run_id, {"backend": "pi_desktop"},
                    util.now_iso(), backend="pi_desktop",
                )
                request, _ = db.create_request(
                    conn, util.now_iso(), "dispatch", f"dispatch:{task_id}",
                    task_id=task_id, requested_by="test",
                )
                conn.execute(
                    "UPDATE requests SET status=?, run_id=? "
                    "WHERE request_id=?",
                    (status, run_id, request["request_id"]),
                )
            return request["request_id"]
        finally:
            conn.close()

    def write_connection(self, **overrides) -> Path:
        mode = overrides.pop("mode", 0o600)
        data = {
            "active": True, "url": self.server.url, "token": self.server.token,
            "pid": os.getpid(), "startedAt": util.now_iso(),
        }
        data.update(overrides)
        path = self.root / "pi-desktop" / "mcp-control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(mode)
        return path

    def write_lost_observer_scene(self, spec, run_dir,
                                  status="failed", send_state="sent",
                                  detail=None):
        """观察者退出后的现场（§5.23.7）：session 事件 + 已确认发送的 job。

        ``detail`` 缺省用真实事故里的 ``heartbeat_failed:`` 前缀（旧版本把
        心跳被拒误判成轮次失败）；``status=failed`` 时补 terminal(failed)。
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        now = util.now_iso()
        detail = detail or (
            f"heartbeat_failed: request 8d44b63a status=succeeded run={RUN_ID}"
        )
        job = {
            "task_id": TASK_ID,
            "run_id": RUN_ID,
            "backend": "pi_desktop",
            "session_id": SESSION_ID,
            "session_title": "PI-Desktop 会话",
            "turn_id": TURN_ID,
            "send_state": send_state,
            "pi_desktop_pid": 999999,
            "config": spec.get("config"),
            "pid": 999999,
            "status": status,
            "started_at": now,
            "ended_at": now if status == "failed" else None,
            "exit_code": 1 if status == "failed" else None,
            "detail": detail if status == "failed" else None,
        }
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(job, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        lines = [
            {"v": 1, "seq": 1, "ts": now, "type": "session",
             "session_id": SESSION_ID, "session_title": "PI-Desktop 会话",
             "turn_id": TURN_ID},
            {"v": 1, "seq": 2, "ts": now, "type": "user", "content": "计划正文"},
        ]
        if status == "failed":
            lines.append({"v": 1, "seq": 3, "ts": now, "type": "terminal",
                          "outcome": "failed", "reason": detail})
        (run_dir / dsh_events.EVENT_LOG_NAME).write_text(
            "".join(
                json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n"
                for line in lines
            ),
            encoding="utf-8",
        )
        return job

    def kill_process_group(self, pid):
        """清理子进程（worker 以 start_new_session 独立进程组启动）。"""
        if not util.pid_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def make_spec(self, run_dir, cwd, plan_path, **overrides) -> dict:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        request_id = overrides.pop(
            "request_id", self.seed_claimed_dispatch()
        )
        spec = {
            "v": 1,
            "task_id": TASK_ID,
            "run_id": RUN_ID,
            "session_marker": f"{TASK_ID}/{RUN_ID}",
            "backend": "pi_desktop",
            "config": {
                "connection_file": str(self.write_connection()),
                "provider_id": "test-provider",
                "model_id": "test-model",
                "thinking_level": "low",
                "permission_mode": "auto",
            },
            "cwd": str(cwd),
            "repository": str(self.repos_root / "Feedback"),
            "plan_path": str(plan_path),
            "plan_sha256": hashlib.sha256(
                Path(plan_path).read_bytes()
            ).hexdigest(),
            "instruction": "任务标识：测试。完整执行以下计划正文。",
            "runs_dir": str(run_dir),
            "timeout_seconds": 30.0,
            "request_id": request_id,
            "db_path": str(self.db_path),
        }
        spec.update(overrides)
        # Runtime approval grants are bound to the dispatch-owned canonical spec.
        canonical = self.runs_root / spec["task_id"] / spec["run_id"]
        canonical.mkdir(parents=True, exist_ok=True)
        (canonical / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return spec

    def write_spec(self, spec) -> Path:
        path = Path(spec["runs_dir"]) / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return path

    @contextlib.contextmanager
    def env(self, **overrides):
        saved = {k: os.environ.get(k) for k in overrides}
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @contextlib.contextmanager
    def fast_clock(self):
        """加速 worker 内部时序（轮询/证明等待/停止宽限/心跳）。"""
        saved = {}
        for name, value in (
            ("_POLL_SECONDS", 0.05),
            ("_HEARTBEAT_SECONDS", 0.05),
            ("_PROOF_WAIT_SECONDS", 0.8),
            ("_STOP_GRACE_SECONDS", 0.2),
            ("_GOAL_CONTRACT_WAIT_SECONDS", 0.5),
            ("_GOAL_EXECUTION_WAIT_SECONDS", 0.5),
            ("_GOAL_POLL_SECONDS", 0.05),
        ):
            saved[name] = getattr(pi_desktop_worker, name)
            setattr(pi_desktop_worker, name, value)
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(pi_desktop_worker, name, value)

    # ---------------------------------------------------------- 读取断言

    def read_events(self, run_dir):
        events, _detail = dsh_events.iter_events(
            Path(run_dir) / dsh_events.EVENT_LOG_NAME
        )
        return events

    def read_job(self, run_dir):
        job, error = dsh_events.read_job(
            Path(run_dir), TASK_ID, RUN_ID, backend="pi_desktop"
        )
        self.assertIsNone(error, f"job.json 身份核验应通过：{error}")
        return job

    def default_messages(self):
        return [
            {"id": "msg-1", "role": "user", "content": "prompt 回显",
             "timestamp": "2026-09-20T00:00:00Z"},
            {"id": "msg-2", "role": "assistant",
             "content": [{"type": "text", "text": "执行完成。"}],
             "timestamp": "2026-09-20T00:00:01Z"},
            {"id": "msg-3", "role": "toolResult", "toolName": "bash",
             "toolStatus": "success", "toolCallId": "call-3",
             "toolArgs": {"command": "ls"},
             "toolResult": "ok\n", "toolCompletedAt": "2026-09-20T00:00:02Z",
             "isError": False,
             "exitCode": 0, "timestamp": "2026-09-20T00:00:02Z"},
            {"id": "msg-4", "role": "toolResult", "toolName": "read",
             "toolStatus": "success", "toolCallId": "call-4",
             "toolResult": "file body", "toolCompletedAt": "2026-09-20T00:00:03Z",
             "isError": False,
             "timestamp": "2026-09-20T00:00:03Z"},
        ]


# ------------------------------------------------------------ backend 层


class TestPiDesktopCatalog(PiDesktopCase):
    def test_single_provider_uses_explicit_defaults_and_thinking_precedence(self):
        self.server.providers = [{
            "id": "p1", "name": "Provider One", "enabled": True,
            "defaultModelId": "m1", "models": [{
                "id": "m1", "thinkingLevels": ["high"],
                "defaultThinkingLevel": "high",
            }],
        }]
        self.server.models = [{
            "id": "m1", "name": "Model One",
            "supportedThinkingLevels": ["low"],
        }]
        self.server.settings = {
            "defaultProviderId": "p1", "defaultModelId": "m1",
        }
        result = self.call("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()),
        })
        self.assertEqual(result["selected_provider_id"], "p1")
        self.assertEqual(result["default_model_id"], "m1")
        self.assertEqual(result["catalog_source"], "cache")
        self.assertEqual(result["providers"][0]["models"][0]["thinking_levels"], ["high"])
        self.assertEqual(result["providers"][0]["models"][0]["default_thinking_level"], "high")
        calls = [args for name, args in self.server.calls if name == "pi_desktop_invoke"]
        self.assertEqual([call["operation"] for call in calls], [
            "providers/list", "settings/get", "providers/listModels",
        ])
        self.assertEqual(calls[-1]["args"], [{"providerId": "p1", "source": "cache"}])

    def test_multiple_enabled_providers_without_default_stays_unselected(self):
        self.server.providers = [
            {"id": "p1", "enabled": True}, {"id": "p2", "enabled": True},
        ]
        result = self.call("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()),
        })
        self.assertIsNone(result["selected_provider_id"])
        self.assertIsNone(result["catalog_source"])
        self.assertEqual([item["models"] for item in result["providers"]], [[], []])
        self.assertFalse(any(args.get("operation") == "providers/listModels"
                             for name, args in self.server.calls
                             if name == "pi_desktop_invoke"))

    def test_explicit_provider_refresh_and_stale_default_do_not_pick_first_model(self):
        self.server.providers = [
            {"id": "p1", "enabled": True, "defaultModelId": "missing"},
            {"id": "p2", "enabled": True},
        ]
        self.server.settings = {
            "defaultProviderId": "p1", "defaultModelId": "stale-model",
        }
        self.server.models = [{"id": "m1", "name": "Only model"}]
        result = self.call("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()), "provider_id": "p1",
        })
        self.assertEqual(result["selected_provider_id"], "p1")
        self.assertEqual(result["default_model_id"], "stale-model")
        self.assertEqual(result["providers"][0]["models"][0]["id"], "m1")
        self.assertEqual(result["providers"][1]["models"], [])
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_invalid_catalog_source_is_rejected_and_token_is_not_exposed(self):
        self.server.model_source = "network"
        error = self.call_err("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()),
        })
        self.assertEqual(error.code, "validation_failed")
        self.assertNotIn(TOKEN, str(error))

    def test_incompatible_mcp_protocol_is_rejected_before_catalog_reads(self):
        self.server.protocol_version = "2099-01-01"
        error = self.call_err("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()),
        })
        self.assertEqual(error.code, "validation_failed")
        self.assertIn("协议版本", error.message)
        self.assertEqual(self.server.calls, [])

    def test_non_string_provider_and_defaults_are_normalized(self):
        self.server.providers = [{
            "id": "p1", "name": 123, "enabled": True,
            "defaultModelId": {"bad": True},
        }]
        self.server.settings = {
            "defaultProviderId": ["p1"], "defaultModelId": 7,
        }
        result = self.call("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()),
        })
        self.assertIsNone(result["default_provider_id"])
        self.assertIsNone(result["default_model_id"])
        self.assertIsNone(result["selected_provider_id"])
        provider = result["providers"][0]
        self.assertEqual(provider["name"], "p1")
        self.assertIsNone(provider["default_model_id"])

    def test_thinking_metadata_is_limited_to_plus_levels(self):
        self.server.providers = [{
            "id": "p1", "enabled": True, "defaultModelId": "m1",
            "models": [{
                "id": "m1", "thinkingLevels": ["high", "turbo", 4],
                "defaultThinkingLevel": "turbo",
            }],
        }]
        self.server.settings = {
            "defaultProviderId": "p1", "defaultModelId": "m1",
        }
        self.server.models = [{"id": "m1", "name": "Model One"}]
        result = self.call("pi-desktop-catalog", {
            "connection_file": str(self.write_connection()),
        })
        model = result["providers"][0]["models"][0]
        self.assertEqual(model["thinking_levels"], ["high"])
        self.assertIsNone(model["default_thinking_level"])


class TestBackendProbe(PiDesktopCase):
    def test_probe_ok_full_chain(self):
        conn_file = self.write_connection()
        result = pi_desktop_backend.probe_connection(conn_file)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["version"], "2025-06-18")
        for name in constants.PI_DESKTOP_REQUIRED_TOOLS:
            self.assertIn(name, result["tools"])
        # initialize + tools/list 走全链；server 收到 Bearer。
        self.assertGreaterEqual(self.server.init_count, 1)
        authz = [r["authorization"] for r in self.server.requests]
        self.assertTrue(all(a == f"Bearer {TOKEN}" for a in authz))
        # token 绝不出现在返回结构的字符串化结果里。
        blob = json.dumps(
            {k: (sorted(v) if isinstance(v, set) else v)
             for k, v in result.items()},
            ensure_ascii=False,
        )
        self.assertNotIn(TOKEN, blob)

    def test_probe_checks_provider_and_cached_model_via_invoke(self):
        result = pi_desktop_backend.probe_connection(
            self.write_connection(), provider_id="test-provider",
            model_id="test-model", timeout=2,
        )
        self.assertTrue(result["ok"], result)
        calls = [name for name, _args in self.server.calls]
        self.assertIn("pi_desktop_invoke", calls)

    def test_connection_file_matrix(self):
        # 不存在
        result = pi_desktop_backend.probe_connection(
            self.root / "nope" / "mcp-control.json"
        )
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["detail"])

        # 权限不安全（0644）
        result = pi_desktop_backend.probe_connection(
            self.write_connection(mode=0o644)
        )
        self.assertFalse(result["ok"])
        self.assertIn("权限不安全", result["detail"])

        # active=false
        result = pi_desktop_backend.probe_connection(
            self.write_connection(active=False)
        )
        self.assertFalse(result["ok"])
        self.assertIn("未启用", result["detail"])

        # 非 loopback URL（文件层拒绝，绝不发请求）
        result = pi_desktop_backend.probe_connection(
            self.write_connection(url="http://192.0.2.10:8080/mcp")
        )
        self.assertFalse(result["ok"])
        self.assertIn("回环", result["detail"])

        # pid 不活（挑一个必然不存在的 pid）
        result = pi_desktop_backend.probe_connection(
            self.write_connection(pid=2 ** 30 - 7)
        )
        self.assertFalse(result["ok"])
        self.assertIn("进程已不存在", result["detail"])

        # token 缺失
        result = pi_desktop_backend.probe_connection(
            self.write_connection(token="")
        )
        self.assertFalse(result["ok"])
        self.assertIn("token", result["detail"])

        # URL 路径不是 /mcp
        result = pi_desktop_backend.probe_connection(
            self.write_connection(
                url=f"http://127.0.0.1:{self.server.port}/api"
            )
        )
        self.assertFalse(result["ok"])
        self.assertIn("/mcp", result["detail"])

        result = pi_desktop_backend.probe_connection(
            self.write_connection(url=f"http://127.0.0.1:{self.server.port}")
        )
        self.assertFalse(result["ok"])
        self.assertIn("/mcp", result["detail"])

        connection = self.write_connection()
        token_file = connection.parent / "mcp-control.token"
        token_file.symlink_to(connection)
        result = pi_desktop_backend.probe_connection(connection)
        self.assertFalse(result["ok"])
        self.assertIn("符号链接", result["detail"])

        # 每个失败 detail 都不含 token 值。
        for detail_source in (result["detail"],):
            self.assertNotIn(TOKEN, detail_source or "")

    def test_missing_required_tool_unavailable(self):
        self.server.tools = [
            t for t in ALL_TOOLS if t != "pi_turn_get"
        ]
        _sync(self.server)
        result = pi_desktop_backend.probe_connection(
            self.write_connection()
        )
        self.assertFalse(result["ok"])
        self.assertIn("pi_turn_get", result["detail"])
        self.assertIn("缺少必需工具", result["detail"])

    def test_missing_goal_tool_unavailable(self):
        """缺 Goal 契约审批工具 → 派发就绪探测即失败（不做事后卡审批门）。"""
        self.server.tools = [
            t for t in ALL_TOOLS if t != "pi_plans_pending"
        ]
        _sync(self.server)
        result = pi_desktop_backend.probe_connection(
            self.write_connection()
        )
        self.assertFalse(result["ok"])
        self.assertIn("pi_plans_pending", result["detail"])

    def test_mcp_session_lost_auto_reconnect(self):
        """服务端 404 unknown session → 客户端自动重新 initialize 并重试
        一次（该拒绝发生在分发前，重发安全）。"""
        self.server.drop_next_404 = True
        _sync(self.server)
        client = pi_desktop_backend.MCPClient(
            self.server.url, TOKEN
        )
        client.connect()
        tools = client.tools_list()
        self.assertIn("pi_turn_get", tools)
        self.assertGreaterEqual(self.server.init_count, 2)

    def test_oversize_request_rejected_before_send(self):
        """请求体 >2 MiB → validation_failed，发送前拒绝（零网络调用）。"""
        calls_before = len(self.server.calls)
        client = pi_desktop_backend.MCPClient(
            self.server.url, TOKEN
        )
        client.connect()
        huge = "x" * (constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES + 10)
        with self.assertRaises(util.KanpilotError) as caught:
            client.agent_prompt(SESSION_ID, huge)
        self.assertEqual(caught.exception.code, "validation_failed")
        self.assertEqual(len(self.server.calls), calls_before)

    def test_token_never_in_error_text(self):
        """服务端错误消息经脱敏；KanpilotError 文本不含 token/Auth header。"""
        client = pi_desktop_backend.MCPClient(
            self.server.url, TOKEN
        )
        client.connect()
        with self.assertRaises(util.KanpilotError) as caught:
            client.call_tool("pi_nope_tool", {})
        message = str(caught.exception.message)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn("Bearer", message)


# ------------------------------------------------------------ worker fake 接缝


class TestWorkerFakeModes(PiDesktopCase):
    def _run_main_fake(self, mode):
        run_dir = self.root / "runs" / RUN_ID
        plan = self.write_spec_plan("# 计划正文\n")
        cwd = self.root / "worktree"
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.make_spec(run_dir, cwd, plan)
        spec_path = self.write_spec(spec)
        with self.env(KANPILOT_PI_DESKTOP_FAKE=mode):
            rc = pi_desktop_worker.main(["--spec", str(spec_path)])
        return rc, run_dir

    def test_fake_finished(self):
        rc, run_dir = self._run_main_fake("finished")
        self.assertEqual(rc, 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["backend"], "pi_desktop")
        self.assertEqual(job["send_state"], "sent")
        self.assertTrue(job["turn_id"].startswith("fake-turn-"))
        self.assertTrue(job["session_id"].startswith("fake-pd-session-"))
        events = self.read_events(run_dir)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[0]["turn_id"], job["turn_id"])
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

    def test_fake_failed(self):
        rc, run_dir = self._run_main_fake("failed")
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 1)
        events = self.read_events(run_dir)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")

    def test_fake_crash_no_terminal(self):
        rc, run_dir = self._run_main_fake("crash")
        self.assertEqual(rc, 3)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "running")
        self.assertIsNone(job["ended_at"])
        events = self.read_events(run_dir)
        self.assertIsNone(dsh_events.terminal_event(events))

    def test_fake_send_unknown(self):
        """send_unknown：job send_state=unknown + session_id/turn_id 已知 +
        不写 terminal（发送不明但身份/证据保留的现场）。"""
        rc, run_dir = self._run_main_fake("send_unknown")
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["send_state"], "unknown")
        self.assertTrue(job["session_id"])
        self.assertTrue(job["turn_id"])
        self.assertIn("prompt_response_lost", job["detail"])
        events = self.read_events(run_dir)
        self.assertEqual(events[0]["type"], "session")
        self.assertIsNone(dsh_events.terminal_event(events))

    def test_fake_unknown_mode_falls_back_finished(self):
        rc, run_dir = self._run_main_fake("mystery")
        self.assertEqual(rc, 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")

    def test_fake_running_spawns_and_is_killed(self):
        run_dir = self.root / "runs" / RUN_ID
        plan = self.write_spec_plan("# 计划正文\n")
        cwd = self.root / "worktree"
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.make_spec(run_dir, cwd, plan)
        spec_path = self.write_spec(spec)
        env = dict(os.environ)
        env["KANPILOT_PI_DESKTOP_FAKE"] = "running"
        env["PYTHONPATH"] = str(CORE)
        proc = subprocess.Popen(
            [sys.executable, "-m", "kanpilot.pi_desktop_worker",
             "--spec", str(spec_path)],
            cwd=str(CORE), env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        saw_user = False
        deadline = time.monotonic() + 10.0
        try:
            while time.monotonic() < deadline:
                events = self.read_events(run_dir)
                if any(e.get("type") == "user" for e in events):
                    saw_user = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            proc.wait(timeout=10)
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(saw_user, "running fake 应写 session+user 事件")
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "running")


# ------------------------------------------------------------ run_real 全流程


class TestRunReal(PiDesktopCase):
    def _run(self, spec):
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            return pi_desktop_worker.run_real(spec)

    def _base(self, body="# 计划正文\n按计划执行。", **kw):
        run_dir = self.root / "runs" / RUN_ID
        plan = self.write_spec_plan(body)
        cwd = self.root / "worktree"
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.make_spec(run_dir, cwd, plan, **kw)
        return spec, run_dir

    def test_old_plus_missing_execution_input_tools_blocks_before_prompt(self):
        self.server.tools.remove('pi_asks_pending')
        spec,run_dir=self._base()
        self.assertEqual(self._run(spec),1)
        self.assertEqual(self.server.prompt_count,0)
        self.assertIsNone(self.server.created)
        self.assertIn('pi_asks_pending',self.read_job(run_dir)['detail'])

    def test_completed_flow_events_and_order(self):
        """turn running→completed：job finished、send_state=sent、首事件
        session（含 turn_id）、消息映射、terminal(completed)、job 写序。"""
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 0)

        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["turn_id"], TURN_ID)
        self.assertEqual(job["session_id"], SESSION_ID)
        self.assertEqual(self.server.attach_calls, [])
        setup_calls = [name for name, _args in self.server.calls
                       if name in ("pi_session_create", "pi_session_configure",
                                   "pi_session_attach_mcp", "pi_agent_prompt")]
        self.assertEqual(setup_calls, ["pi_session_create", "pi_session_configure",
                                       "pi_agent_prompt"])
        scope = self.project_mcp.scope_manager.scopes[job["scope_id"]]
        self.assertEqual(scope.task_id, TASK_ID)
        self.assertEqual(scope.run_id, RUN_ID)
        self.assertIsNone(scope.request_id)
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, spec["request_id"])
            self.assertIsNotNone(request)
            self.assertEqual(request["task_id"], TASK_ID)
            self.assertEqual(request["run_id"], RUN_ID)
        finally:
            conn.close()
        self.assertEqual(job["pi_desktop_pid"], os.getpid())
        self.assertIsNotNone(job["ended_at"])

        events = self.read_events(run_dir)
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual(events[0]["type"], "session",
                         "首个事件必须是 session（启动证明后才写）")
        self.assertEqual(events[0]["turn_id"], TURN_ID)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")
        types = [e["type"] for e in events]
        self.assertIn("user", types)
        self.assertIn("assistant_message", types)
        self.assertIn("command_output", types)   # bash → command_output
        self.assertIn("tool_result", types)      # read → tool_result
        cmd = next(e for e in events if e["type"] == "command_output")
        self.assertEqual(cmd["exit_code"], 0)

        # prompt 内容 = instruction + 计划正文；session_configure 已下发
        # permission_mode=auto（非 inherit）。
        self.assertIn("任务标识", self.server.prompt_content)
        self.assertIn("计划正文", self.server.prompt_content)
        self.assertEqual(self.server.prompt_count, 1)
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        self.assertTrue(self.server.configured["confirm"])
        # Authorization Bearer 已发送。
        authz = [r["authorization"] for r in self.server.requests]
        self.assertIn(f"Bearer {TOKEN}", authz)

    # -------------------------------------------- Goal 契约审批门（§5.23.9）

    def _goal_contract(self, proposal_id="prop-goal-1", status="pending"):
        """Plus ``plans.pending`` 的 Goal 契约条目（host-core camelCase）。"""
        return {
            "id": proposal_id,
            "sessionId": SESSION_ID,
            "turnId": TURN_ID,
            "toolCallId": "tool-goal-1",
            "kind": "goal",
            "title": "目标契约：按计划完成",
            "markdown": "# 目标\n\n- 验收：测试通过\n",
            "question": "批准执行？",
            "status": status,
            "version": 3,
            "createdAt": util.now_iso(),
            "updatedAt": util.now_iso(),
        }

    def test_goal_mode_contract_auto_approved_then_execution_observed(self):
        """§5.23.9：goal 模式建会话；规划轮以契约收官 → 本机代批（留痕）→
        换绑同会话执行轮次继续观察，绝不重发 prompt。"""
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]   # 证明 / 规划轮
        self.server.turn_status = "completed"                  # 执行轮次终态
        self.server.plans = [self._goal_contract()]
        self.server.planning_state = "awaiting_approval"
        self.server.execution_turn = "turn-goal-exec"
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 0)

        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["turn_id"], "turn-goal-exec")
        approval = job["goal_approval"]
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["proposal_id"], "prop-goal-1")
        self.assertEqual(approval["planning_turn_id"], TURN_ID)
        self.assertEqual(approval["execution_turn_id"], "turn-goal-exec")
        self.assertEqual(approval["target_permission_mode"], "auto")
        self.assertEqual(approval["approved_by"], "kaneopilot")
        self.assertNotIn("turn_adopted", job,
                         "契约批准不是瞬时错误接续，不得混入 turn_adopted")

        # 建会话与配置都下发 goal；批准必带 confirm + 目标权限模式。
        self.assertEqual(self.server.created["mode"], "goal")
        self.assertEqual(self.server.configured["mode"], "goal")
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        self.assertEqual(self.server.prompt_count, 1, "批准执行不得重发 prompt")
        self.assertEqual(len(self.server.plans_resolve_calls), 1)
        resolve = self.server.plans_resolve_calls[0]
        self.assertEqual(resolve["action"], "approve")
        self.assertIs(resolve["confirm"], True)
        self.assertEqual(resolve["targetPermissionMode"], "auto")
        self.assertEqual(resolve["proposalId"], "prop-goal-1")
        self.assertEqual(resolve["turnId"], TURN_ID)
        self.assertEqual(resolve["version"], 3)

        events = self.read_events(run_dir)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[0]["turn_id"], TURN_ID,
                         "session 事件证明的是规划轮次启动")
        system = [e.get("content") or "" for e in events
                  if e["type"] == "system"]
        self.assertTrue(any("goal_contract" in c for c in system), system)
        self.assertTrue(any("goal_approved" in c for c in system), system)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

    def test_goal_contract_missing_fails_without_resend(self):
        """会话仍在 goal 契约模式（planning）但轮次已完成、无契约 → 明确失败。"""
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]
        self.server.planning_state = "planning"
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("goal_contract_missing", job["detail"])
        self.assertEqual(self.server.prompt_count, 1)
        self.assertEqual(self.server.plans_resolve_calls, [])
        events = self.read_events(run_dir)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertEqual(events[-1]["reason"], "goal_contract_missing")

    def test_goal_approval_failure_fails_without_resend(self):
        """契约仍 pending 时代批失败 → goal_approval_failed（不重发）。"""
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]
        self.server.plans = [self._goal_contract()]
        self.server.planning_state = "awaiting_approval"
        self.server.plans_resolve_mode = "error"
        self.server.execution_turn = "turn-goal-exec"
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("goal_approval_failed", job["detail"])
        self.assertEqual(self.server.prompt_count, 1)
        self.assertEqual(job["turn_id"], TURN_ID, "未批准不得换绑轮次")

    def test_goal_execution_unstarted_fails_without_resend(self):
        """契约已批准但宿主未开启执行轮次 → goal_execution_unstarted。"""
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]
        self.server.plans = [self._goal_contract()]
        self.server.planning_state = "awaiting_approval"
        self.server.execution_turn = None       # dispatcher 未开跑
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("goal_execution_unstarted", job["detail"])
        self.assertEqual(self.server.prompt_count, 1)
        self.assertIsNone(job.get("goal_approval"))

    def test_missing_goal_approval_tool_fails_before_session(self):
        """控制面缺 pi_plans_* → 发送前 unsupported_tools（不留孤儿会话）。"""
        self.server.tools = [t for t in ALL_TOOLS if t != "pi_plans_resolve"]
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("pi_plans_resolve", job["detail"])
        self.assertEqual(self.server.prompt_count, 0, "工具门失败前不得建会话/发 prompt")
        self.assertIsNone(self.server.created)

    def test_turn_error_maps_failed_with_error_code(self):
        self.server.turn_status = "error"
        self.server.turn_error_code = "MODEL_OVERLOADED"
        _sync(self.server)
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("MODEL_OVERLOADED", job["detail"])
        events = self.read_events(run_dir)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertIn("MODEL_OVERLOADED", events[-1]["reason"])

    def test_turn_aborted_maps_failed(self):
        self.server.turn_status = "aborted"
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("aborted", job["detail"])

    def test_prompt_response_lost_marks_send_unknown_no_retry(self):
        """发送响应丢失：send_state=unknown + 证据保留 + 绝不重发 prompt。"""
        self.server.prompt_mode = "http500"
        self.server.current_turn = None   # agent_status 查不到已受理 turn
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["send_state"], "unknown")
        self.assertIn("prompt_response_lost", job["detail"])
        self.assertEqual(job["session_id"], SESSION_ID)
        self.assertEqual(self.server.prompt_count, 1,
                         "发送响应丢失绝不自动重发 pi_agent_prompt")
        # 身份/证据保留在 job.json；无成功 terminal。
        events = self.read_events(run_dir)
        terminal = dsh_events.terminal_event(events)
        self.assertNotEqual(
            (terminal or {}).get("outcome"), "completed"
        )

    def test_prompt_lost_but_status_recovers_turn(self):
        """响应丢失但 agent_status 暴露 currentTurnId 仍保持 unknown；
        currentTurnId 只能作为候选证据，不能自动恢复发送。"""
        self.server.prompt_mode = "http500"
        self.server.current_turn = "turn-9"   # 服务端实际已受理
        self.server.turn_statuses = ["running", "completed"]
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["send_state"], "unknown")
        self.assertEqual(job["turn_id"], "turn-9")
        self.assertEqual(self.server.prompt_count, 1)
        self.assertIn("候选证据", job["detail"])
        evidence = run_dir / "unverified-events.jsonl"
        self.assertTrue(evidence.is_file())
        self.assertIn("source_message_id", evidence.read_text(encoding="utf-8"))
        self.assertNotIn(TOKEN, evidence.read_text(encoding="utf-8"))

    def test_worker_interrupted_after_prompt_accepted_keeps_unknown(self):
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        from kanpilot import runtime_client

        actual_prompt = runtime_client.RuntimeClient.agent_prompt

        def interrupted(client, session_id, content):
            actual_prompt(client, session_id, content)
            raise KeyboardInterrupt()

        with mock.patch.object(runtime_client.RuntimeClient, "agent_prompt", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self._run(spec)
        self.assertEqual(self.server.prompt_count, 1)
        self.assertEqual(self.read_job(run_dir)["send_state"], "unknown")
        self.assertIsNone(dsh_events.session_event(self.read_events(run_dir)))

    def test_plan_too_large_fails_before_send(self):
        """计划正文 >2 MiB → 发送前明确失败（plan_too_large），
        零次 agent_prompt。"""
        huge_body = "# 计划\n" + "x" * (
            constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES + 100
        )
        spec, run_dir = self._base(body=huge_body)
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("plan_too_large", job["detail"])
        self.assertEqual(job["send_state"], "not_sent")
        self.assertEqual(self.server.prompt_count, 0)

    def test_plan_hash_mismatch_fails_before_session(self):
        """计划哈希与登记不符 → plan_mismatch，连 session 都不创建。"""
        spec, run_dir = self._base()
        spec["plan_sha256"] = "0" * 64
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertIn("plan_mismatch", job["detail"])
        self.assertEqual(self.server.prompt_count, 0)
        self.assertIsNone(job["session_id"])

    def test_pagination_walks_full_history(self):
        """分页游标翻页到底：page_size=2 → 4 条消息跨 2 页全部采集。"""
        self.server.page_size = 2
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 0)
        events = self.read_events(run_dir)
        types = [e["type"] for e in events]
        # 4 条 UiMessage 全部映射 + session + terminal。
        self.assertEqual(types.count("user"), 1)
        self.assertEqual(types.count("assistant_message"), 1)
        self.assertEqual(types.count("command_output"), 1)
        self.assertEqual(types.count("tool_result"), 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")

    def test_string_ids_and_revisions_are_preserved(self):
        self.server.turn_statuses = ["running", "completed"]
        self.server.messages = [{
            "id": "msg-revised", "role": "assistant", "content": "old",
            "timestamp": "2026-09-20T00:00:00Z",
        }]
        _sync(self.server)
        spec, run_dir = self._base()
        original = self.server.tool_call
        changed = {"done": False}

        def revise(name, args):
            result = original(name, args)
            if name == "pi_session_get" and not changed["done"]:
                changed["done"] = True
                self.server.messages[0]["content"] = "new"
            return result

        self.server.tool_call = revise
        _sync(self.server)
        self.assertEqual(self._run(spec), 0)
        events = self.read_events(run_dir)
        assistant = [e for e in events if e["type"] == "assistant_message"]
        self.assertTrue(assistant)
        self.assertEqual(assistant[-1]["source_message_id"], "msg-revised")
        self.assertGreaterEqual(assistant[-1]["source_revision"], 1)

    def test_unknown_message_shape_blocks_success(self):
        self.server.turn_status = "completed"
        self.server.messages = [{
            "id": "msg-unknown", "role": "futureRole", "payload": {"x": 1},
        }]
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 1)
        job = self.read_job(run_dir)
        self.assertTrue(
            "transcript_incomplete" in job["detail"]
            or "transcript_read_failed" in job["detail"]
        )
        self.assertIsNone(dsh_events.terminal_event(self.read_events(run_dir)))

    def test_truncated_transcript_blocks_success(self):
        """分页链断裂（messageBefore 页空 session）→ transcript_incomplete、
        不写 terminal、已采证据保留。"""
        self.server.page_size = 2
        self.server.before_broken = True
        _sync(self.server)
        # before_broken：messageBefore 调用返回空 session（服务端对象层
        # 定制——覆写 tool_call 分支）。
        orig = self.server.tool_call

        def broken(name, args):
            if name == "pi_session_get" and args.get("messageBefore"):
                return {"structuredContent": {"session": None}}
            return orig(name, args)

        self.server.tool_call = broken
        _sync(self.server)
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(
            "transcript_incomplete" in job["detail"]
            or "transcript_read_failed" in job["detail"]
        )
        events = self.read_events(run_dir)
        self.assertIsNone(
            dsh_events.terminal_event(events),
            "截断/不完整转录不得写 terminal（轮次不得判成功）",
        )
        # 已采集的最新页证据仍然保留。
        self.assertTrue(
            any(e["type"] == "command_output" for e in events)
        )

    def test_session_read_failure_blocks_success(self):
        """session_get 中途 not_found → read_error 保留证据 + 不写 terminal。"""
        self.server.session_lost = True
        self.server.turn_status = "completed"
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("transcript_read_failed", job["detail"])
        events = self.read_events(run_dir)
        self.assertIsNone(dsh_events.terminal_event(events))

    def test_read_only_session_reconnect_does_not_replay_prompt(self):
        """只读采集遇到过期 MCP session 可重连并继续，prompt 只落地一次。"""
        self.server.drop_next_404 = True
        self.server.turn_statuses = ["running", "completed"]
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 0)
        self.assertEqual(self.server.prompt_count, 1)
        self.assertGreaterEqual(self.server.init_count, 2)

    def test_startup_unproven_no_session_event(self):
        """pi_turn_get 持续 not_found → 启动证明失败：job failed、
        绝不写 session 事件（已排队≠已开始）。"""
        self.server.turn_missing = True
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertIn("startup_unproven", job["detail"])
        events = self.read_events(run_dir)
        self.assertIsNone(
            dsh_events.session_event(events),
            "启动证明失败不得写 session 事件",
        )
        evidence = run_dir / "unverified-events.jsonl"
        self.assertTrue(evidence.is_file())
        self.assertIn("source_message_id", evidence.read_text(encoding="utf-8"))
        self.assertNotIn(TOKEN, evidence.read_text(encoding="utf-8"))

    def test_startup_terminal_error_does_not_prove_started(self):
        """error/aborted 是终止证据，不是启动证明，不能落 session 事件。"""
        self.server.turn_status = "error"
        self.server.turn_error_code = "MODEL_OVERLOADED"
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 1)
        events = self.read_events(run_dir)
        self.assertIsNone(dsh_events.session_event(events))
        evidence = run_dir / "unverified-events.jsonl"
        self.assertTrue(evidence.is_file())
        self.assertIn("source_message_id", evidence.read_text(encoding="utf-8"))
        self.assertNotIn(TOKEN, evidence.read_text(encoding="utf-8"))

    def test_pagination_gap_blocks_success(self):
        """物理分页边界不衔接时，即使 hasMoreBefore 最终为 false 也不能完成。"""
        self.server.page_size = 2
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        original = self.server.tool_call

        def gap(name, args):
            result = original(name, args)
            if name == "pi_session_get" and args.get("messageBefore") is not None:
                result["structuredContent"]["session"]["messageEnd"] = 0
            return result

        self.server.tool_call = gap
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 1)
        self.assertIn("transcript_incomplete", self.read_job(run_dir)["detail"])

    def test_pagination_cursor_repeat_blocks_success(self):
        self.server.page_size = 2
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        original = self.server.tool_call

        def repeat(name, args):
            result = original(name, args)
            if name == "pi_session_get" and args.get("messageBefore") is not None:
                session = result["structuredContent"]["session"]
                session["messageStart"] = args["messageBefore"]
                session["messageEnd"] = args["messageBefore"]
                session["hasMoreBefore"] = True
            return result

        self.server.tool_call = repeat
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 1)
        self.assertIn("transcript_incomplete", self.read_job(run_dir)["detail"])

    def test_has_more_before_empty_page_with_decreasing_cursor_continues(self):
        self.server.page_size = 2
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        original = self.server.tool_call

        seen_empty = {"done": False}
        def empty_page(name, args):
            if name == "pi_session_get" and args.get("messageBefore") is not None:
                if not seen_empty["done"]:
                    seen_empty["done"] = True
                    return {"structuredContent": {"session": {
                        "id": self.server.session_id, "messages": [],
                        "messageStart": 1, "messageEnd": 2,
                        "hasMoreBefore": True,
                    }}}
            return original(name, args)

        self.server.tool_call = empty_page
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 0)

    def test_has_more_before_empty_page_without_boundary_blocks_success(self):
        self.server.page_size = 2
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        original = self.server.tool_call

        def empty_page(name, args):
            if name == "pi_session_get" and args.get("messageBefore") is not None:
                return {"structuredContent": {"session": {
                    "id": self.server.session_id, "messages": [],
                    "hasMoreBefore": True,
                }}}
            return original(name, args)

        self.server.tool_call = empty_page
        _sync(self.server)
        spec, run_dir = self._base()
        self.assertEqual(self._run(spec), 1)
        self.assertIn("transcript_incomplete", self.read_job(run_dir)["detail"])

    def test_timeout_stops_agent_and_verifies(self):
        """超时 → pi_agent_stop → turn 落 aborted → job failed + terminal。"""
        self.server.turn_status = "running"
        self.server.stop_aborts = True
        _sync(self.server)
        spec, run_dir = self._base(timeout_seconds=0.1)
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("timeout", job["detail"])
        self.assertGreaterEqual(self.server.stop_calls, 1)
        events = self.read_events(run_dir)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["reason"], "timeout")

    def test_timeout_stop_ack_without_terminal_turn_sends_abort(self):
        """stop 的 accepted 不算停止；持久 turn 仍 running 时必须 abort。"""
        self.server.turn_status = "running"
        self.server.stop_aborts = False
        _sync(self.server)
        spec, run_dir = self._base(timeout_seconds=0.1)
        self.assertEqual(self._run(spec), 1)
        self.assertGreaterEqual(self.server.stop_calls, 1)
        self.assertGreaterEqual(self.server.abort_calls, 1)
        job = self.read_job(run_dir)
        self.assertIn("timeout", job["detail"])

    def test_timeout_stop_abort_response_loss_is_unknown(self):
        self.server.turn_status = "running"
        self.server.stop_mode = "http500"
        self.server.abort_mode = "http500"
        _sync(self.server)
        spec, run_dir = self._base(timeout_seconds=0.1)
        self.assertEqual(self._run(spec), 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["send_state"], "unknown")
        self.assertNotIn("已停止", job["detail"] or "")
        self.assertNotIn("abort 已发送", job["detail"] or "")

    def test_turn_mismatch_send_unknown(self):
        """agent_status.currentTurnId 与本轮 turn 不符 → 身份不符：
        send_state=unknown + job failed，绝不重发。"""
        self.server.turn_statuses = ["running", "running"]
        _sync(self.server)

        # 服务端暴露的当前 turn 与 prompt 返回的 turn 不一致。
        orig = self.server.tool_call

        def mismatched(name, args):
            if name == "pi_agent_status":
                return {"structuredContent": {"status": {
                    "sessionId": args.get("sessionId"),
                    "isRunning": True,
                    "currentTurnId": "other-turn",
                    "pendingToolConfirmations": []}}}
            return orig(name, args)

        self.server.tool_call = mismatched
        _sync(self.server)
        spec, run_dir = self._base()
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["send_state"], "unknown")
        self.assertIn("turn_mismatch", job["detail"])
        self.assertEqual(self.server.prompt_count, 1)

    def test_heartbeat_failure_no_session_event(self):
        """heartbeat 失败 → 不写 session 事件（证明未闭环）。"""
        self.server.turn_status = "completed"
        _sync(self.server)
        spec, run_dir = self._base()
        with mock.patch.object(
            pi_desktop_worker, "_heartbeat",
            side_effect=util.KanpilotError(
                "remote_unavailable", "test heartbeat failure"
            ),
        ):
            rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertIn("heartbeat_failed", job["detail"])
        events = self.read_events(run_dir)
        self.assertIsNone(dsh_events.session_event(events))

    def test_observe_heartbeat_stale_run_keeps_round_running(self):
        """dispatch-confirm 后心跳必回 stale_run（正常路径）→ 观察继续、轮次
        不判失败（§5.23.7；真实事故：会话仍在跑却被写成 heartbeat_failed）。"""
        # 观察循环要跨过至少一个心跳周期（fast_clock 下 0.05s），才能覆盖
        # 「confirm 之后心跳被拒」的真实时序。
        self.server.turn_statuses = ["running", "running", "running",
                                     "completed"]
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        calls = {"n": 0}
        real_heartbeat = pi_desktop_worker._heartbeat

        def heartbeat_after_confirm(spec_arg):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_heartbeat(spec_arg)   # 启动证明心跳时请求仍 claimed
            raise util.KanpilotError(
                "stale_run",
                f"request {spec['request_id']} status=succeeded run={RUN_ID}",
            )

        with mock.patch.object(
            pi_desktop_worker, "_heartbeat", heartbeat_after_confirm
        ):
            rc = self._run(spec)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(calls["n"], 2, "观察循环应至少再试一次心跳")
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["send_state"], "sent")
        self.assertIn("stale_run", job.get("heartbeat_stopped", ""))
        self.assertNotIn("heartbeat_failed", json.dumps(job, ensure_ascii=False))
        self.assertEqual(self.server.prompt_count, 1)
        events = self.read_events(run_dir)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

    def test_resume_settles_round_without_re_prompt(self):
        """--resume 只读续接：不建会话、不重发 prompt、不重复 session 事件，
        按持久终态收尾并保留上一次收尾现场（§5.23.7）。"""
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        before = self.write_lost_observer_scene(spec, run_dir)
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            rc = pi_desktop_worker.run_real(spec, resume=True)
        self.assertEqual(rc, 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["send_state"], "sent")
        self.assertIsNotNone(job["observation_resumed_at"])
        self.assertEqual(job["observe_pid"], os.getpid())
        self.assertEqual(job["resume_previous"]["status"], "failed")
        self.assertEqual(job["resume_previous"]["detail"], before["detail"])
        self.assertIsNone(job["resume_detail"])
        self.assertEqual(self.server.prompt_count, 0, "绝不重发 prompt")
        self.assertIsNone(self.server.configured, "绝不重下发 permission_mode")
        events = self.read_events(run_dir)
        self.assertEqual(
            len([e for e in events if e["type"] == "session"]), 1,
            "session 事件不得重复写",
        )
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

    def test_resume_with_unreadable_turn_defers_and_keeps_evidence(self):
        """续接观察只负责观察：turn 读不到 → 退出码 3 + resume_detail，
        既有轮次终态、detail 与事件一字不改（§5.23.7）。"""
        self.server.turn_missing = True
        _sync(self.server)
        spec, run_dir = self._base()
        before = self.write_lost_observer_scene(spec, run_dir)
        before_events = self.read_events(run_dir)
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            rc = pi_desktop_worker.run_real(spec, resume=True)
        self.assertEqual(rc, 3)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["detail"], before["detail"])
        self.assertEqual(job["exit_code"], 1)
        self.assertIn("startup_unproven", job["resume_detail"])
        self.assertEqual(job["resume_attempts"], 1)
        self.assertNotIn("observation_resumed_at", job)
        self.assertEqual(self.read_events(run_dir), before_events)
        self.assertEqual(self.server.prompt_count, 0)

    def test_resume_rejects_foreign_or_unsent_scene(self):
        """身份不符/未确认发送/已 finished 的现场不得续接，也不得改写。"""
        spec, run_dir = self._base()
        run_dir.mkdir(parents=True, exist_ok=True)
        job = self.write_lost_observer_scene(spec, run_dir)
        # ① 身份不符：task_id/run_id 与目录不一致。
        foreign = dict(job, run_id="other-run")
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(foreign, ensure_ascii=False), encoding="utf-8"
        )
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 1)
        # ② 未确认发送（send_state=not_sent，且无 session 事件）。
        self.write_lost_observer_scene(spec, run_dir, send_state="not_sent")
        (run_dir / dsh_events.EVENT_LOG_NAME).write_text(
            "", encoding="utf-8"
        )
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 1)
        # ③ 已按 completed 收尾：幂等返回 0，不连接、不写文件。
        finished = self.write_lost_observer_scene(spec, run_dir, status="running")
        finished.update({"status": "finished", "exit_code": 0, "detail": None})
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(finished, ensure_ascii=False), encoding="utf-8"
        )
        snapshot = (run_dir / dsh_events.JOB_NAME).read_text(encoding="utf-8")
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 0)
        self.assertEqual(
            (run_dir / dsh_events.JOB_NAME).read_text(encoding="utf-8"),
            snapshot,
        )
        self.assertEqual(self.server.prompt_count, 0)
        self.assertEqual(self.server.init_count, 0)

    def test_resume_accepts_false_failure_scene_and_repairs_send_state(self):
        """旧版假失败现场（send_state=unknown + session 事件 + turn_id）可续接：
        session 事件即「已确认发送」硬证据，续接后 send_state 修回 sent、原值
        留在 resume_previous（§5.23.7 真实事故现场）。"""
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        _sync(self.server)
        spec, run_dir = self._base()
        before = self.write_lost_observer_scene(spec, run_dir,
                                                send_state="unknown")
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            rc = pi_desktop_worker.run_real(spec, resume=True)
        self.assertEqual(rc, 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["resume_previous"]["send_state"], "unknown")
        self.assertEqual(job["resume_previous"]["detail"], before["detail"])
        self.assertEqual(job["resume_previous"]["status"], "failed")
        self.assertEqual(self.server.prompt_count, 0)

    def test_resume_without_job_scene_is_rejected(self):
        """无现场（job.json/session 事件缺失）→ 拒绝续接，绝不新建会话。"""
        spec, run_dir = self._base()
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 1)
        self.assertFalse((run_dir / dsh_events.JOB_NAME).exists())
        self.assertEqual(self.server.init_count, 0)

    def test_resume_defers_when_round_timeout_elapsed(self):
        """已超时的轮次不续接、也不停止仍在跑的会话（交人工判定终态）。"""
        self.server.turn_status = "running"
        _sync(self.server)
        spec, run_dir = self._base()
        job = self.write_lost_observer_scene(spec, run_dir, status="running")
        job["started_at"] = "2020-01-01T00:00:00+00:00"
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8"
        )
        before_events = self.read_events(run_dir)
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            rc = pi_desktop_worker.run_real(spec, resume=True)
        self.assertEqual(rc, 3)
        current = self.read_job(run_dir)
        self.assertEqual(current["status"], "running")
        self.assertIn("round_timeout_elapsed", current["resume_detail"])
        self.assertEqual(self.read_events(run_dir), before_events)
        self.assertEqual(self.server.stop_calls, 0, "不得动手停止会话")
        self.assertEqual(self.server.abort_calls, 0)

    def test_round_detail_supersedes_stale_terminal_after_resume(self):
        """读取层：续接接管后（status=running + observation_resumed_at），早于
        接管时刻的 terminal(failed) 不再作为轮次判定依据（事件行 append-only
        不改写），原文转 terminal_superseded 供审计；接管后新写的 terminal 才
        是本轮终态（§5.23.7 真实事故的「界面仍显示轮次失败」）。"""
        spec, run_dir = self._base()
        self.write_lost_observer_scene(spec, run_dir)  # 旧版假失败现场
        events = self.read_events(run_dir)
        self.assertEqual(
            dsh_events.terminal_event(events)["outcome"], "failed"
        )

        row = {
            "task_id": TASK_ID, "run_id": RUN_ID, "backend": "pi_desktop",
            "session_id": SESSION_ID, "session_title": "PI-Desktop 会话",
            "run_status": "running", "config_json": None,
            "worktree_path": None, "started_at": util.now_iso(),
            "pid": 999999, "save_state": None, "saved_at": None,
            "created_at": None, "updated_at": None,
        }
        resumed = {}
        resumed.update(self.read_job(run_dir))
        resumed.update({
            "status": "running", "ended_at": None, "exit_code": None,
            "detail": None, "observe_pid": 999999,
            "observation_resumed_at": "2099-01-01T00:00:00+00:00",
        })
        detail = dsh_events.round_detail(row, resumed, events)
        self.assertEqual(detail["run_status"], "running")
        self.assertIsNone(
            detail["terminal"], "接管前的假失败不再是本轮终态"
        )
        self.assertEqual(detail["terminal_superseded"]["outcome"], "failed")
        self.assertTrue(
            detail["terminal_superseded"]["reason"].startswith(
                "heartbeat_failed"
            ),
            "旧终态原文保留在审计位",
        )

        # 接管后新写 terminal(completed) → 恢复为本轮终态；旧终态仍在审计位。
        events = events + [{
            "v": 1, "seq": 4, "ts": "2099-01-01T00:00:05+00:00",
            "type": "terminal", "outcome": "completed",
        }]
        detail = dsh_events.round_detail(row, resumed, events)
        self.assertEqual(detail["terminal"]["outcome"], "completed")
        self.assertEqual(detail["terminal_superseded"]["outcome"], "failed")

        # 未续接的现场（status=failed，无 observation_resumed_at）不启用作废：
        # 终态照旧可读，失败判定不被吞掉。
        failed_job = self.read_job(run_dir)
        self.assertEqual(failed_job["status"], "failed")
        detail = dsh_events.round_detail(
            row, failed_job, self.read_events(run_dir)
        )
        self.assertEqual(detail["terminal"]["outcome"], "failed")
        self.assertIsNone(detail["terminal_superseded"])


    # ---------------------------------------------------- 采集器分页（真实事故）

    def _collector(self, client, name="events-collect.jsonl"):
        log = pi_desktop_worker._EventLog(self.root / name)
        collector = pi_desktop_worker._Collector(client, SESSION_ID, log, "")
        collector.gate_open = True        # 测试直接开闸，不依赖启动证明
        return collector

    def test_result_limit_shrinks_page_until_readable(self):
        """控制面对大页返回 MCP_RESULT_LIMIT 截断（真实事故：messageLimit=200
        的页被截断，长会话轮次恒判 transcript_incomplete）→ 采集按半数自适应
        缩小页大小重试，读通后不得判截断。"""
        messages = self.default_messages()
        limits = []

        class Client:
            def session_get(self, session_id, message_before=None,
                            message_limit=None):
                limits.append(message_limit)
                if (message_limit or 0) > 20:
                    return {"truncated": True, "reason": "MCP_RESULT_LIMIT"}
                end = len(messages) if message_before is None                     else int(message_before)
                pool = messages[:end]
                page = pool[-message_limit:]
                start = end - len(page)
                return {"session": {
                    "id": SESSION_ID, "messages": page,
                    "messageStart": start, "messageEnd": end,
                    "hasMoreBefore": start > 0, "hasMoreAfter": False,
                }}

        collector = self._collector(Client())
        collector.poll()
        self.assertIn(
            pi_desktop_worker._PAGE_LIMIT, limits, "先按初始页大小请求"
        )
        self.assertIn(
            pi_desktop_worker._PAGE_LIMIT // 2, limits, "超限后减半重试"
        )
        self.assertEqual(
            limits[-1], pi_desktop_worker._PAGE_LIMIT // 2,
            "降档后的页大小沿用到后续页，不每页重试",
        )
        self.assertFalse(collector.truncated, "缩页后应读到完整链")
        self.assertIsNone(collector.read_error)
        self.assertEqual(collector.verified_end, len(messages))

    def test_repoll_stops_at_verified_prefix_and_rewrite_falls_back(self):
        """整段链核验过一次后，后续轮询在「已核验前缀」锚点处收敛，不再重读
        整段历史（长会话不被页数上限判死）；前缀被改写（锚点指纹变化）→
        安全回退为整段重读。"""
        base = self.default_messages()
        messages = [
            dict(base[index % len(base)], id=f"msg-{index}")
            for index in range(len(base) * 20)
        ]
        page_size = 2
        calls = []

        class Client:
            def session_get(self, session_id, message_before=None,
                            message_limit=None):
                limit = min(message_limit or page_size, page_size)
                end = len(messages) if message_before is None                     else int(message_before)
                pool = messages[:end]
                page = pool[-limit:]
                start = end - len(page)
                calls.append((message_before, start, end))
                return {"session": {
                    "id": SESSION_ID, "messages": page,
                    "messageStart": start, "messageEnd": end,
                    "hasMoreBefore": start > 0, "hasMoreAfter": False,
                }}

        collector = self._collector(Client())
        collector.poll()
        self.assertFalse(collector.truncated)
        self.assertEqual(collector.verified_end, len(messages))
        full_walk = len(calls)
        self.assertGreater(full_walk, 2, "首轮必须自 0 起核验整段链")

        calls.clear()
        collector.poll()
        self.assertGreater(len(calls), 0)
        self.assertLessEqual(len(calls), 2, "第二轮回读应在锚点处收敛")
        self.assertEqual(collector.verified_end, len(messages))

        # 前缀（与新页重叠的段）被改写 → 指纹不符 → 不据旧前缀提前收敛。
        messages[len(messages) - 1] = dict(
            messages[-1], content="前缀末端被改写"
        )
        calls.clear()
        collector.poll()
        self.assertGreater(len(calls), 2, "重叠段指纹失配必须回退整段重读")
        self.assertEqual(collector.verified_end, len(messages))

    def test_transient_truncation_does_not_poison_round(self):
        """一次瞬时截断只作废本次轮询：下一轮读通后截断标记清除，轮次仍可判
        完成（旧实现把截断粘死，长会话轮次永无成功可能）。"""
        messages = self.default_messages()
        broken = {"on": True}

        class Client:
            def session_get(self, session_id, message_before=None,
                            message_limit=None):
                if broken["on"]:
                    return {"truncated": True, "reason": "MCP_RESULT_LIMIT"}
                end = len(messages) if message_before is None                     else int(message_before)
                pool = messages[:end]
                page = pool[-message_limit:]
                start = end - len(page)
                return {"session": {
                    "id": SESSION_ID, "messages": page,
                    "messageStart": start, "messageEnd": end,
                    "hasMoreBefore": start > 0, "hasMoreAfter": False,
                }}

        collector = self._collector(Client())
        collector.poll()
        self.assertTrue(collector.truncated, "本轮确实读到截断")
        broken["on"] = False
        collector.poll()
        self.assertFalse(collector.truncated, "下一轮读通 → 截断清除")
        self.assertEqual(collector.verified_end, len(messages))


    def test_transient_turn_error_adopts_follow_up_turn(self):
        """瞬时模型错误（EMPTY_MODEL_RESPONSE）后 Plus 在同一会话自动重开新
        turn：只读接续观察新 turn（不重发 prompt、不新建会话、不重复 session
        事件），轮次随后按新 turn 的真实终态收尾（§5.23.8）。"""
        spec, run_dir = self._base()
        prompt = self.expected_prompt(spec)
        self.server.turn_statuses = ["error", "completed"]
        self.server.turn_error_code = "EMPTY_MODEL_RESPONSE"
        self.server.follow_up_turn = FOLLOW_UP_TURN_ID
        self.server.messages = [
            {"id": "msg-1", "role": "user", "content": prompt,
             "timestamp": "2026-09-20T00:00:00Z"},
            {"id": "msg-2", "role": "assistant",
             "content": [{"type": "text", "text": "继续执行验证。"}],
             "timestamp": "2026-09-20T00:00:01Z"},
        ]
        _sync(self.server)

        self.assertEqual(self._run(spec), 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["turn_id"], FOLLOW_UP_TURN_ID)
        self.assertEqual(job["turn_adopted"][0]["from"], TURN_ID)
        self.assertEqual(job["turn_adopted"][0]["to"], FOLLOW_UP_TURN_ID)
        self.assertEqual(
            job["turn_adopted"][0]["code"], "EMPTY_MODEL_RESPONSE"
        )
        self.assertEqual(self.server.prompt_count, 1, "接续绝不重发 prompt")
        events = self.read_events(run_dir)
        self.assertEqual(
            len([e for e in events if e["type"] == "session"]), 1,
            "session 事件不得重复写",
        )
        self.assertTrue(
            any("turn_adopted" in str(e.get("content") or "")
                for e in events),
            "接续必须留痕（events.jsonl）",
        )

    def test_non_transient_turn_error_is_not_adopted(self):
        """非瞬时错误（如认证失败）不接续：照旧 job failed + terminal(failed)。"""
        spec, run_dir = self._base()
        self.server.turn_status = "error"
        self.server.turn_error_code = "AUTH_FAILED"
        self.server.current_turn = FOLLOW_UP_TURN_ID
        self.server.messages = [
            {"id": "msg-1", "role": "user",
             "content": self.expected_prompt(spec),
             "timestamp": "2026-09-20T00:00:00Z"},
        ]
        _sync(self.server)

        self.assertEqual(self._run(spec), 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("AUTH_FAILED", job["detail"])
        self.assertFalse(job.get("turn_adopted"))
        events = self.read_events(run_dir)
        self.assertEqual(
            dsh_events.terminal_event(events)["reason"], "AUTH_FAILED"
        )

    def test_follow_up_adopted_after_user_appended_instruction(self):
        """真实事故回归：用户在本任务的 Plus 会话里追加过一句指令（「用中文
        回复我」），锚点是**第一条** user 消息（本轮 prompt），因此仍照常接续
        ——不能因为用户随口追加一句就把仍在跑的任务判成失败（§5.23.8）。"""
        spec, run_dir = self._base()
        prompt = self.expected_prompt(spec)
        self.server.turn_statuses = ["error", "completed"]
        self.server.turn_error_code = "EMPTY_MODEL_RESPONSE"
        self.server.follow_up_turn = FOLLOW_UP_TURN_ID
        self.server.messages = [
            {"id": "msg-1", "role": "user", "content": prompt,
             "timestamp": "2026-09-20T00:00:00Z"},
            {"id": "msg-2", "role": "assistant",
             "content": [{"type": "text", "text": "第一轮完成。"}],
             "timestamp": "2026-09-20T00:00:01Z"},
            {"id": "msg-3", "role": "user", "content": "用中文回复我",
             "timestamp": "2026-09-20T00:00:02Z"},
        ]
        _sync(self.server)

        self.assertEqual(self._run(spec), 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["turn_id"], FOLLOW_UP_TURN_ID)
        self.assertEqual(job["turn_adopted"][0]["to"], FOLLOW_UP_TURN_ID)
        self.assertEqual(self.server.prompt_count, 1, "接续绝不重发 prompt")
        events = self.read_events(run_dir)
        self.assertTrue(
            any(e["type"] == "user" and "用中文回复我" in str(e.get("content"))
                for e in events),
            "追加的 user 消息仍照常采集",
        )

    def test_follow_up_not_adopted_when_session_is_foreign(self):
        """会话归属锚点不符（第一条 user 消息不是本轮 prompt）→ 该会话不是
        本轮的，绝不接续：照旧按失败收尾。"""
        spec, run_dir = self._base()
        self.server.turn_statuses = ["error", "completed"]
        self.server.turn_error_code = "EMPTY_MODEL_RESPONSE"
        self.server.follow_up_turn = FOLLOW_UP_TURN_ID
        self.server.messages = [
            {"id": "msg-1", "role": "user",
             "content": "另一个任务的指令：随便做点别的。",
             "timestamp": "2026-09-20T00:00:00Z"},
            {"id": "msg-2", "role": "assistant",
             "content": [{"type": "text", "text": "好的。"}],
             "timestamp": "2026-09-20T00:00:01Z"},
        ]
        _sync(self.server)

        self.assertEqual(self._run(spec), 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertFalse(job.get("turn_adopted"))
        self.assertEqual(job["turn_id"], TURN_ID)
        events = self.read_events(run_dir)
        self.assertEqual(
            dsh_events.terminal_event(events)["reason"], "EMPTY_MODEL_RESPONSE"
        )

    def test_follow_up_not_adopted_when_session_idle(self):
        """同一会话但已不在跑任何 turn（isRunning=false）→ 无可接续的执行，
        照旧按失败收尾，绝不自行重开。"""
        spec, run_dir = self._base()
        self.server.turn_statuses = ["error", "completed"]
        self.server.turn_error_code = "EMPTY_MODEL_RESPONSE"
        self.server.current_turn = FOLLOW_UP_TURN_ID
        self.server.is_running = False
        self.server.messages = [
            {"id": "msg-1", "role": "user",
             "content": self.expected_prompt(spec),
             "timestamp": "2026-09-20T00:00:00Z"},
        ]
        _sync(self.server)

        self.assertEqual(self._run(spec), 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertFalse(job.get("turn_adopted"))
        self.assertEqual(job["turn_id"], TURN_ID)


# ------------------------------------------------------------ dispatch e2e


class TestDispatchPiDesktop(PiDesktopCase):
    """§5.23.3 派发状态机 e2e（fake worker subprocess + fake MCP 探测）。"""

    def setUp(self):
        super().setUp()
        self.server.required_verification_commands = True
        # New contracted dispatches require a readable verification report;
        # TestRunReal keeps the legacy server shape for historical specs.
        self.server.tools.extend(constants.PI_DESKTOP_GOAL_REPORT_TOOLS)

    def test_new_contract_missing_report_capability_never_sends(self):
        claim = self._submit_round()
        self.server.tools.remove("pi_goal_report_get")
        _sync(self.server)
        with self.env(KANPILOT_PI_START_WAIT="1"):
            started = self.call("dispatch-start", {"task_id": TASK_ID, "run_id": claim["run_id"]})
        self.addCleanup(self.kill_process_group, started["pid"])
        path = self.runs_root / TASK_ID / claim["run_id"] / "job.json"
        deadline = time.monotonic() + 10
        job = {}
        while time.monotonic() < deadline:
            if path.is_file():
                job = json.loads(path.read_text())
                if job.get("status") == "failed":
                    break
            time.sleep(0.05)
        self.assertEqual(job.get("status"), "failed", job)
        self.assertEqual(job.get("send_state"), "not_sent")
        self.assertIn("verification_report", job.get("detail", ""))
        self.assertTrue(job.get("stage_contract_required"))
        self.assertIsNone(self.server.created)
        self.assertEqual(self.server.prompt_count, 0)

    def write_plan(self, project_name, task, meta_overrides=None,
                   body="# plan\n\n```verify\ntrue\n```\n"):
        # New dispatches freeze a verification contract before their real fake
        # worker starts. Legacy/old-Plus fixtures in TestRunReal remain unchanged.
        return super().write_plan(project_name, task, meta_overrides, body)

    def _save_pi_desktop_config(self, conn_file):
        return self.call("set-exec-config", {
            "backend": "pi_desktop",
            "pi_desktop": {
                "connection_file": str(conn_file),
                "provider_id": "test-provider",
                "model_id": "test-model",
                "thinking_level": "low",
                "permission_mode": "ask",
            },
        })

    def _submit_round(self):
        self._save_pi_desktop_config(self.write_connection())
        self.make_ready_task(task_id=TASK_ID)
        self._init_git_fixture()
        self.enqueue_dispatch(TASK_ID)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"], claim)
        ctx = claim["context"]
        evidence = {
            "session_marker": ctx["session_marker"],
            "repository": ctx["repository"],
            "backend": "pi_desktop",
            "config": ctx["exec_config"],
        }
        self.call("dispatch-prepare", {
            "task_id": TASK_ID, "run_id": claim["run_id"],
            "evidence": evidence,
        })
        self.call("dispatch-begin-send", {
            "task_id": TASK_ID, "run_id": claim["run_id"],
        })
        return claim

    def _init_git_fixture(self):
        repo = self.repos_root / "Feedback"
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / ".fixture").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

    def test_intent_worktree_failure_is_visible_and_second_start_conflicts(self):
        claim = self._submit_round()
        with mock.patch.object(
            cmd_dispatch, "_ensure_worktree",
            side_effect=util.KanpilotError("internal", "worktree failed"),
        ):
            first = self.call_err("dispatch-start", {
                "task_id": TASK_ID, "run_id": claim["run_id"],
            })
        self.assertEqual(first.code, "internal")
        second = self.call_err("dispatch-start", {
            "task_id": TASK_ID, "run_id": claim["run_id"],
        })
        self.assertEqual(second.code, "conflict")

    def test_full_dispatch_flow_pi_desktop(self):
        conn_file = self.write_connection()
        saved = self._save_pi_desktop_config(conn_file)
        self.assertEqual(saved["config"]["backend"], "pi_desktop")

        fixture = self.make_ready_task(task_id=TASK_ID)
        self._init_git_fixture()
        self.enqueue_dispatch(TASK_ID)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"], claim)
        run_id = claim["run_id"]
        ctx = claim["context"]
        self.assertEqual(ctx["backend"], "pi_desktop")
        snapshot = ctx["exec_config"]
        self.assertEqual(snapshot["backend"], "pi_desktop")
        self.assertEqual(snapshot["connection_file"], str(conn_file))
        # 快照/上下文绝不携带 token。
        self.assertNotIn(TOKEN, json.dumps(snapshot, ensure_ascii=False))

        # 轮次固化：backend + config_json（§5.16.1）。
        conn = db.connect(self.db_path)
        try:
            from kanpilot import round_store
            row = round_store.get_round(conn, TASK_ID, run_id)
        finally:
            conn.close()
        self.assertEqual(row["backend"], "pi_desktop")
        self.assertEqual(util.jload(row["config_json"]), snapshot)

        # prepare：pi_desktop 证据分支（marker/repository/backend/config）。
        evidence = {
            "session_marker": ctx["session_marker"],
            "repository": ctx["repository"],
            "backend": "pi_desktop",
            "config": snapshot,
        }
        prepared = self.call("dispatch-prepare", {
            "task_id": TASK_ID, "run_id": run_id, "evidence": evidence,
        })
        self.assertEqual(prepared["status"], "prepared")

        # begin-send → submitting（远端迁移不变）。
        sent = self.call("dispatch-begin-send", {
            "task_id": TASK_ID, "run_id": run_id,
        })
        self.assertEqual(sent["status"], "submitting")

        # dispatch-start：probe 通过 → 拉起真 subprocess fake worker。
        with self.env(KANPILOT_PI_DESKTOP_FAKE="finished"):
            started = self.call("dispatch-start", {
                "task_id": TASK_ID, "run_id": run_id,
            })
        self.assertEqual(started["status"], "started", started)
        self.assertTrue(started["session_id"].startswith("fake-pd-session-"))
        session_id = started["session_id"]

        # worker 已写 job/events：<runs_root>/<task_id>/<run_id>
        # （setUp 钉住的 session_runs_root），session 事件含 turn_id。
        run_dir = self.runs_root / TASK_ID / run_id
        events_path = run_dir / dsh_events.EVENT_LOG_NAME
        self.assertTrue(
            events_path.is_file(), "fake worker 应已写 events.jsonl"
        )
        events, _ = dsh_events.iter_events(events_path)
        self.assertEqual(events[0]["type"], "session")
        turn_id = events[0]["turn_id"]
        self.assertTrue(turn_id.startswith("fake-turn-"))
        job, error = dsh_events.read_job(
            run_dir, TASK_ID, run_id, backend="pi_desktop"
        )
        self.assertIsNone(error)
        self.assertEqual(job["backend"], "pi_desktop")

        # confirm：turn_id 不符 → evidence_invalid，task 停留 submitting。
        bad = self.call_err("dispatch-confirm", {
            "task_id": TASK_ID, "run_id": run_id,
            "evidence": {**evidence, "session_id": session_id,
                         "turn_id": "wrong-turn", "protocol_seen": True},
        })
        self.assertEqual(bad.code, "evidence_invalid")
        self.assertEqual(self.task(TASK_ID)["status"], "submitting")

        # confirm 正确证据 → running。
        confirmed = self.call("dispatch-confirm", {
            "task_id": TASK_ID, "run_id": run_id,
            "evidence": {**evidence, "session_id": session_id,
                         "turn_id": turn_id, "protocol_seen": True},
        })
        self.assertEqual(confirmed["status"], "running")
        view = self.task(TASK_ID)
        self.assertEqual(view["status"], "running")
        self.assertEqual(view["session_id"], session_id)
        reqs = self.requests(status="succeeded")
        self.assertTrue(any(r["kind"] == "dispatch" for r in reqs))

        # round_detail 透出 turn_id/send_state（§5.15.5 job 顶层字段）。
        detail = self.call("get-session", {
            "task_id": TASK_ID, "run_id": run_id,
        })
        rd = detail.get("round_detail") or {}
        self.assertEqual(rd.get("backend"), "pi_desktop")
        self.assertEqual(rd.get("turn_id"), turn_id)
        self.assertEqual(rd.get("send_state"), "sent")

    def test_round_observe_reattaches_after_observer_loss(self):
        """观察者退出后 round-observe 只读续接（不重发 prompt、不新建会话）；
        观察者仍存活时幂等（deduplicated）——§5.23.7 真实事故回归。"""
        conn_file = self.write_connection()
        self._save_pi_desktop_config(conn_file)
        self.make_ready_task(task_id=TASK_ID)
        self._init_git_fixture()
        self.enqueue_dispatch(TASK_ID)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"], claim)
        run_id = claim["run_id"]
        ctx = claim["context"]
        evidence = {
            "session_marker": ctx["session_marker"],
            "repository": ctx["repository"],
            "backend": "pi_desktop",
            "config": ctx["exec_config"],
        }
        self.call("dispatch-prepare", {
            "task_id": TASK_ID, "run_id": run_id, "evidence": evidence,
        })
        self.call("dispatch-begin-send", {
            "task_id": TASK_ID, "run_id": run_id,
        })
        # 真 worker 观察一个不落终态的 turn（fake MCP 常驻 running）。
        self.server.turn_statuses = []
        self.server.turn_status = "running"
        self.server.messages = []
        _sync(self.server)
        with self.env(KANPILOT_PI_START_WAIT="20"):
            started = self.call("dispatch-start", {
                "task_id": TASK_ID, "run_id": run_id,
            })
        self.assertEqual(started["status"], "started", started)
        worker_pid = started["pid"]
        self.addCleanup(self.kill_process_group, worker_pid)
        run_dir = self.runs_root / TASK_ID / run_id
        events, _ = dsh_events.iter_events(
            run_dir / dsh_events.EVENT_LOG_NAME
        )
        turn_id = events[0]["turn_id"]
        confirmed = self.call("dispatch-confirm", {
            "task_id": TASK_ID, "run_id": run_id,
            "evidence": {**evidence, "session_id": started["session_id"],
                         "turn_id": turn_id, "protocol_seen": True},
        })
        self.assertEqual(confirmed["status"], "running")

        # ① 观察进程仍在 → 幂等返回，不拉起第二个观察者。
        alive = self.call("round-observe", {
            "task_id": TASK_ID, "run_id": run_id, "executor": "kaneo-monitor",
        })
        self.assertEqual(alive["status"], "observing", alive)
        self.assertTrue(alive["deduplicated"])
        self.assertEqual(alive["pid"], worker_pid)
        self.assertEqual(self.server.prompt_count, 1)

        # ② 模拟观察者消失（应用崩溃/被清理），Plus 侧会话仍在跑并随后完成。
        self.kill_process_group(worker_pid)
        deadline = time.monotonic() + 20.0
        while util.pid_alive(worker_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(util.pid_alive(worker_pid))
        lost = dsh_events.read_job(
            run_dir, TASK_ID, run_id, backend="pi_desktop"
        )[0]
        self.assertEqual(lost["status"], "running")
        self.assertEqual(lost["send_state"], "sent")
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        _sync(self.server)

        with self.env(KANPILOT_PI_START_WAIT="20"):
            resumed = self.call("round-observe", {
                "task_id": TASK_ID, "run_id": run_id,
                "executor": "kaneo-monitor",
            })
        self.assertIn(resumed["status"], ("observing", "settled"), resumed)
        self.assertNotEqual(resumed["pid"], worker_pid)
        self.addCleanup(self.kill_process_group, resumed["pid"])

        final = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            final = dsh_events.read_job(
                run_dir, TASK_ID, run_id, backend="pi_desktop"
            )[0]
            if final and final.get("status") == "finished":
                break
            time.sleep(0.1)
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(final["observe_pid"], resumed["pid"])
        self.assertIsNotNone(final["observation_resumed_at"])
        self.assertEqual(self.server.prompt_count, 1, "续接绝不重发 prompt")

        events, _ = dsh_events.iter_events(
            run_dir / dsh_events.EVENT_LOG_NAME
        )
        self.assertEqual(
            len([e for e in events if e["type"] == "session"]), 1,
            "session 事件不得重复写",
        )
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

        # ③ 读取层透出观察进程身份与 run_status（§5.23.7）。
        detail = self.call("get-session", {
            "task_id": TASK_ID, "run_id": run_id,
        })
        rd = detail["round_detail"]
        self.assertEqual(rd["observe_pid"], resumed["pid"])
        self.assertEqual(rd["run_status"], "finished")
        self.assertEqual(rd["terminal"]["outcome"], "completed")
        # ④ 轮次已收尾 → 再调 round-observe 幂等返回 finished。
        done = self.call("round-observe", {
            "task_id": TASK_ID, "run_id": run_id,
        })
        self.assertEqual(done["status"], "finished")

    def _start_observed_round(self):
        """派发 + 确认一轮 pi_desktop 观察现场（真 fake worker 子进程）。"""
        claim = self._submit_round()
        run_id = claim["run_id"]
        self.server.turn_statuses = []
        self.server.turn_status = "running"
        self.server.messages = []
        _sync(self.server)
        with self.env(KANPILOT_PI_START_WAIT="20"):
            started = self.call("dispatch-start", {
                "task_id": TASK_ID, "run_id": run_id,
            })
        self.assertEqual(started["status"], "started", started)
        worker_pid = started["pid"]
        self.addCleanup(self.kill_process_group, worker_pid)
        run_dir = self.runs_root / TASK_ID / run_id
        events, _ = dsh_events.iter_events(
            run_dir / dsh_events.EVENT_LOG_NAME
        )
        turn_id = events[0]["turn_id"]
        ctx = claim["context"]
        confirmed = self.call("dispatch-confirm", {
            "task_id": TASK_ID, "run_id": run_id,
            "evidence": {
                "session_marker": ctx["session_marker"],
                "repository": ctx["repository"],
                "backend": "pi_desktop",
                "config": ctx["exec_config"],
                "session_id": started["session_id"],
                "turn_id": turn_id, "protocol_seen": True,
            },
        })
        self.assertEqual(confirmed["status"], "running")
        return {"run_id": run_id, "run_dir": run_dir,
                "worker_pid": worker_pid, "turn_id": turn_id}

    def _lose_observer(self, worker_pid):
        """模拟观察进程消失（应用重启/被清理）并等到进程确实退出。"""
        self.kill_process_group(worker_pid)
        deadline = time.monotonic() + 20.0
        while util.pid_alive(worker_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(util.pid_alive(worker_pid))

    def test_round_observe_reopens_stale_execution_failure(self):
        """§5.23.8：瞬时错误留下的「执行失败」阻塞，在只读续接观察成活后自动
        解除——会话仍在跑时，调度台不得继续把任务摆成失败。"""
        site = self._start_observed_round()
        run_id, run_dir = site["run_id"], site["run_dir"]
        self._lose_observer(site["worker_pid"])
        self.mark_needs_user(TASK_ID, run_id)          # 监控端旧口径的阻塞
        self.assertEqual(self.task(TASK_ID)["status"], "needs_user")

        with self.env(KANPILOT_PI_START_WAIT="20"):
            resumed = self.call("round-observe", {
                "task_id": TASK_ID, "run_id": run_id,
                "executor": "kaneo-monitor",
            })
        self.assertEqual(resumed["status"], "observing", resumed)
        self.assertTrue(resumed["reopened"], resumed)
        self.addCleanup(self.kill_process_group, resumed["pid"])

        view = self.task(TASK_ID)
        self.assertEqual(view["status"], "running")
        self.assertEqual(view["run_id"], run_id)
        self.assertIsNone(view["failure_code"])
        self.assertIsNone(view["failure_reason"])
        # 解除阻塞可审计：事件 detail 带 reopened=true。
        events = [e for e in self.call("list-events", {
            "task_id": TASK_ID})["events"] if e["kind"] == "round_observe"]
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["detail"]["run_id"], run_id)
        self.assertTrue(events[0]["detail"]["reopened"])

        # 同一会话随后真实完成 → 轮次按 completed 收尾，全程未重发 prompt。
        self.server.turn_status = "completed"
        self.server.messages = self.default_messages()
        _sync(self.server)
        final = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            final = dsh_events.read_job(
                run_dir, TASK_ID, run_id, backend="pi_desktop"
            )[0]
            if final and final.get("status") == "finished":
                break
            time.sleep(0.1)
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(self.server.prompt_count, 1, "续接绝不重发 prompt")

    def test_round_observe_keeps_other_blocks(self):
        """只解除本轮的 execution_failed：其它原因的阻塞原样保留。"""
        site = self._start_observed_round()
        run_id = site["run_id"]
        self._lose_observer(site["worker_pid"])
        self.mark_needs_user(TASK_ID, run_id, failure_code="review_failed")

        with self.env(KANPILOT_PI_START_WAIT="20"):
            resumed = self.call("round-observe", {
                "task_id": TASK_ID, "run_id": run_id,
                "executor": "kaneo-monitor",
            })
        self.assertEqual(resumed["status"], "observing", resumed)
        self.assertFalse(resumed["reopened"])
        self.addCleanup(self.kill_process_group, resumed["pid"])
        view = self.task(TASK_ID)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["failure_code"], "review_failed")

    def test_prepare_rejects_wrong_backend_evidence(self):
        """pi_desktop 轮次收 backend=\"pi\" 证据 → conflict（后端不符）。"""
        conn_file = self.write_connection()
        self._save_pi_desktop_config(conn_file)
        self.make_ready_task(task_id=TASK_ID)
        self.enqueue_dispatch(TASK_ID)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        ctx = claim["context"]
        bad = self.call_err("dispatch-prepare", {
            "task_id": TASK_ID, "run_id": claim["run_id"],
            "evidence": {
                "session_marker": ctx["session_marker"],
                "repository": ctx["repository"],
                "backend": "pi",
                "config": ctx["exec_config"],
            },
        })
        self.assertEqual(bad.code, "conflict")
        self.assertEqual(self.task(TASK_ID)["status"], "dispatching")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
