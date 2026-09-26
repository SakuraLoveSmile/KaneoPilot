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
    constants, db, dsh_events, pi_desktop_backend, pi_desktop_worker, util,
    cmd_dispatch,
)

TASK_ID = "taskpidesktop000000000001"
RUN_ID = "runpidesktop01"
TOKEN = "test-mcp-token-0123456789abcdef"
SESSION_ID = "pd-sess-1"
TURN_ID = "turn-1"
ALL_TOOLS = tuple(constants.PI_DESKTOP_REQUIRED_TOOLS)


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
                                   "version": "0.1.0"},
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
                "result": {"tools": [{"name": n} for n in srv.tools]},
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
        self.stop_calls = 0
        self.abort_calls = 0
        self.stop_aborts = False          # agent_stop → turn 立即 aborted
        self.stop_mode = "ok"
        self.abort_mode = "ok"
        self.drop_next_404 = False
        self.providers = [{"id": "test-provider", "disabled": False}]
        self.models = [{"id": "test-model", "modelId": "test-model"}]
        self.settings = {}
        self.model_source = "cache"
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
            return {"structuredContent": {
                "session": {"id": self.session_id,
                            "title": args.get("title")}}}
        if name == "pi_session_configure":
            self.configured = args
            return {"structuredContent": {"ok": True}}
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
            return {"structuredContent": record}
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


    def write_spec_plan(self, body: str) -> Path:
        plan = self.plan_root / "Feedback" / f"plan-{TASK_ID}.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        front = "task_id: t\n"
        plan.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
        return plan

    def seed_claimed_dispatch(self, task_id=TASK_ID, run_id=RUN_ID) -> str:
        """登记一条 claimed dispatch 请求（worker heartbeat 目标）。

        requests.task_id 有 FK——隔离 spec 的单测没有 tasks 行，按
        task_id=None 登记（heartbeat 的 task 更新对 task_id 为空自动跳过；
        _claimed_request 只核 status='claimed' + run_id）。
        """
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                request, _ = db.create_request(
                    conn, util.now_iso(), "dispatch", f"dispatch:{task_id}",
                    task_id=None, requested_by="test",
                )
                conn.execute(
                    "UPDATE requests SET status='claimed', run_id=? "
                    "WHERE request_id=?",
                    (run_id, request["request_id"]),
                )
            return request["request_id"]
        finally:
            conn.close()

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
        actual_prompt = pi_desktop_backend.MCPClient.agent_prompt

        def interrupted(client, session_id, content):
            actual_prompt(client, session_id, content)
            raise KeyboardInterrupt()

        with mock.patch.object(pi_desktop_backend.MCPClient, "agent_prompt", interrupted):
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
        spec, run_dir = self._base(request_id="nonexistent-request")
        rc = self._run(spec)
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertIn("heartbeat_failed", job["detail"])
        events = self.read_events(run_dir)
        self.assertIsNone(dsh_events.session_event(events))


# ------------------------------------------------------------ dispatch e2e


class TestDispatchPiDesktop(PiDesktopCase):
    """§5.23.3 派发状态机 e2e（fake worker subprocess + fake MCP 探测）。"""

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
