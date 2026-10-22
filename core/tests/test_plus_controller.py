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


class TestHostEnvironment(PlusCase):
    def test_wrapper_overrides_host_default_environment(self):
        """宿主默认环境指向错误夹具 → 包装器仍命中隔离库（证明显式传入）。"""
        directory = pi_desktop_controller.invocation_dir("dispatch", "inv-test")
        wrapper = pi_desktop_controller.write_wrapper(directory, "dispatch")
        decoy_db = self.root / "decoy" / "kanpilot.db"
        decoy_app = self.root / "decoy" / "app-support"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "KANPILOT_DB": str(decoy_db),
            "KANPILOT_APP_SUPPORT": str(decoy_app),
            "KANPILOT_PLAN_ROOT": str(self.root / "decoy" / "plan"),
        }
        proc = subprocess.run(
            [str(wrapper), "get-task", "--json",
             json.dumps({"task_id": "task-missing"})],
            env=env, capture_output=True, text=True, timeout=30,
        )
        # 命中隔离库：任务不存在 → 核心 not_found（而不是打开 decoy 空库崩溃）。
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["code"], "not_found")
        self.assertFalse(decoy_db.exists(), "不得读取/创建宿主默认数据库")
        self.assertFalse(decoy_app.exists(), "不得使用宿主默认 app support")

    def test_wrapper_hits_isolated_database(self):
        """反向证明：包装器确实指向隔离库里的真实数据。"""
        task_id = "taskwrap00000000000000001"
        remote = self.fake.add_task(task_id, "fixture-project-feedback")
        self.sync_task(remote, project_name="Feedback")
        directory = pi_desktop_controller.invocation_dir("dispatch", "inv-test-2")
        wrapper = pi_desktop_controller.write_wrapper(directory, "dispatch")
        proc = subprocess.run(
            [str(wrapper), "get-task", "--json",
             json.dumps({"task_id": task_id})],
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["task"]["task_id"], task_id)

    def test_wrapper_and_prompt_block_never_expose_token(self):
        directory = pi_desktop_controller.invocation_dir("monitor", "inv-test-3")
        wrapper = pi_desktop_controller.write_wrapper(directory, "monitor")
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn("KANPILOT_DB=", text)
        self.assertIn("KANEO_CREDENTIALS_PATH=", text)
        self.assertIn(str(constants.core_root() / "kanpilot.py"), text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Bearer", text)
        block = pi_desktop_controller.host_environment_block(
            wrapper, directory, "monitor"
        )
        for key in pi_desktop_controller.REQUIRED_HOST_ENV:
            self.assertIn(key, block, key)
        self.assertIn(str(wrapper), block)
        self.assertNotIn(TOKEN, block)

    def test_host_environment_covers_contract_keys(self):
        env = pi_desktop_controller.host_environment()
        for key in pi_desktop_controller.REQUIRED_HOST_ENV:
            self.assertIn(key, env, key)
        self.assertEqual(env["KANPILOT_DB"], str(constants.default_db_path()))
        self.assertEqual(env["KANPILOT_APP_SUPPORT"],
                         str(constants.app_support()))
        self.assertNotIn(TOKEN, json.dumps(env))

    def test_point_cli_at_wrapper_rewrites_documented_entry(self):
        prompt = executor_runner.render_prompt("dispatch")
        wrapper = Path("/tmp/fake-wrapper")
        rewritten = pi_desktop_controller.point_cli_at_wrapper(prompt, wrapper)
        self.assertIn(str(wrapper), rewritten)
        self.assertNotIn(str(constants.core_root() / "kanpilot.py"), rewritten)


# ------------------------------------------------------------ dispatch 控制轮


class TestDispatchControlRound(PlusCase):
    def test_round_runs_agent_session_without_pi_subprocess(self):
        self.server.messages = default_messages()
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch")
        self.assertEqual(rc, 0)

        # 会话：mode=agent + 本轮固化模型/权限；建过一次会话与配置。
        self.assertEqual(self.server.created["mode"], "agent")
        self.assertEqual(self.server.created["providerId"], "test-provider")
        self.assertEqual(self.server.created["modelId"], "test-model")
        self.assertEqual(self.server.created["thinkingLevel"], "low")
        self.assertEqual(self.server.created["projectPath"],
                         str(self.invocation_dir("dispatch")))
        self.assertEqual(self.server.configured["mode"], "agent")
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        self.assertTrue(self.server.configured["confirm"])

        # 新 Plus 轮使用专用 MCP 指令，不含 CLI/shell 入口。
        self.assertEqual(self.server.prompt_count, 1)
        prompt = self.server.prompt_content
        self.assertIn("kp_task_dispatch_prepare", prompt)
        self.assertIn("只使用本会话挂接的命名 kp_task_* MCP 工具", prompt)
        self.assertNotIn("kanpilot-cli", prompt)
        self.assertNotIn("bash", prompt)
        self.assertNotIn(TOKEN, prompt)
        plus_names = [name for name, _args in self.server.calls]
        self.assertLess(plus_names.index("pi_session_configure"),
                        plus_names.index("pi_session_attach_mcp"))
        self.assertLess(plus_names.index("pi_session_attach_mcp"),
                        plus_names.index("pi_agent_prompt"))

        # 产物：job/events/transcript；Plus 不再注入 CLI 包装器。
        directory = self.invocation_dir("dispatch")
        job = self.read_job(directory)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["session_id"], SESSION_ID)
        self.assertEqual(job["turn_id"], TURN_ID)
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["backend"], "pi_desktop")
        self.assertNotIn(TOKEN, json.dumps(job, ensure_ascii=False))
        self.assertFalse((directory / "kanpilot-cli").exists())
        events = self.read_events(directory)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")
        conn = db.connect(self.db_path)
        try:
            receipts = conn.execute(
                "SELECT tool,status FROM runtime_operations WHERE scope_id=?",
                (job["scope_id"],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            {row["tool"]: row["status"] for row in receipts},
            {
                "kp_runtime_session_create": "accepted",
                "kp_runtime_session_configure": "accepted",
                "kp_runtime_attach_mcp": "accepted",
                "kp_runtime_prompt": "accepted",
            },
        )

        # 运行记录：新增字段 + 保留旧字段。
        run = self.executor_run("dispatch")
        self.assertEqual(run["backend"], "pi_desktop")
        self.assertEqual(run["session_id"], SESSION_ID)
        self.assertEqual(run["turn_id"], TURN_ID)
        self.assertEqual(run["invocation_id"], job["invocation_id"])
        self.assertEqual(run["send_state"], "sent")
        self.assertEqual(run["config"]["permission_mode"], "auto")
        for key in ("pid", "started_at", "log", "exit_code", "finished_at"):
            self.assertIn(key, run)

    def test_completed_round_does_not_write_task_state(self):
        """控制回合 completed 只表示控制回合结束：不写任务/请求状态。"""
        remote = self.fake.add_task(
            "taskctl000000000000000001", "fixture-project-feedback"
        )
        self.sync_task(remote, project_name="Feedback")
        self.server.messages = default_messages()
        with self.no_subprocess_spy():
            self.assertEqual(self.run_stage("dispatch"), 0)
        self.assertEqual(self.task("taskctl000000000000000001")["status"],
                         "needs_plan")
        self.assertEqual(self.requests(), [])
        events = self.read_events(self.invocation_dir("dispatch"))
        system = [e.get("content") or "" for e in events
                  if e["type"] == "system"]
        self.assertTrue(
            any("claim/report" in text for text in system), system
        )

    def test_active_unknown_control_round_blocks_new_round(self):
        """单飞：持久活跃/unknown 控制轮阻止同阶段重复启动（零网络调用）。"""
        self.set_setting("executor_runs", {"monitor": {
            "backend": "pi_desktop", "pid": os.getpid(),
            "invocation_id": "inv-active", "send_state": "unknown",
            "session_id": SESSION_ID, "turn_id": TURN_ID,
            "started_at": util.now_iso(), "finished_at": None,
        }})
        with self.no_subprocess_spy():
            rc = self.run_stage("monitor")
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.init_count, 0)
        self.assertEqual(self.server.prompt_count, 0)

    def test_prompt_response_lost_is_unknown_and_never_resent(self):
        self.server.prompt_mode = "http500"
        with self.no_subprocess_spy():
            first = self.run_stage("dispatch")
        self.assertEqual(first, 1)
        directory = self.invocation_dir("dispatch")
        job = self.read_job(directory)
        self.assertEqual(job["send_state"], "unknown")
        self.assertIn("prompt_response_lost", job["detail"])
        self.assertEqual(self.server.prompt_count, 1)
        self.assertEqual(self.server.created_count, 1)

        # 再跑同阶段：活跃 unknown 控制轮 → 拒绝再次启动，绝不重发。
        with self.no_subprocess_spy():
            second = self.run_stage("dispatch")
        self.assertEqual(second, 1)
        self.assertEqual(self.server.prompt_count, 1)
        self.assertEqual(self.server.created_count, 1)

    def test_legacy_resume_is_internal_adapter_read_only(self):
        """旧 job 无 scope 时只经内部历史 reader 观察，不建立/发送会话。"""
        config_snapshot = self.setting("exec_config")["pi_desktop"]
        self.set_setting("executor_runs", {"dispatch": {
            "backend": "pi_desktop", "pid": 999999,
            "invocation_id": "inv-resume", "send_state": "unknown",
            "session_id": SESSION_ID, "turn_id": TURN_ID,
            "config": dict(config_snapshot),
            "started_at": util.now_iso(), "finished_at": None,
        }})
        directory = pi_desktop_controller.invocation_dir("dispatch", "inv-resume")
        pi_desktop_controller.write_json(directory / "job.json", {
            "stage": "dispatch", "invocation_id": "inv-resume",
            "backend": "pi_desktop", "status": "failed",
            "send_state": "unknown", "session_id": SESSION_ID,
            "turn_id": TURN_ID, "pid": 999999,
            "started_at": util.now_iso(), "ended_at": util.now_iso(),
            "exit_code": 1, "detail": "prompt_response_lost",
            "config": dict(config_snapshot),
        })
        self.server.turn_status = "completed"
        self.server.messages = default_messages()
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch", resume=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.prompt_count, 0, "恢复绝不重发 prompt")
        self.assertEqual(self.server.created_count, 0, "恢复绝不新建会话")
        run = self.executor_run("dispatch")
        self.assertEqual(run["backend"], "pi_desktop")
        job = self.read_job(directory)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["send_state_recovered_from"], "unknown")
        events = self.read_events(directory)
        # LegacyReadClient 仅调用 turn/status/messages 读取，不做写操作。
        self.assertTrue(any(
            "只读恢复" in (e.get("content") or "") for e in events
        ))
        self.assertLessEqual(
            len([e for e in events if e["type"] == "session"]), 1,
            "session 事件不得重复写",
        )
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

    def test_resume_without_known_turn_keeps_manual_block(self):
        """无已知 session/turn 的 unknown：不自动重发，保留人工阻塞。"""
        self.set_setting("executor_runs", {"dispatch": {
            "backend": "pi_desktop", "pid": 999999,
            "invocation_id": "inv-no-turn", "send_state": "unknown",
            "session_id": None, "turn_id": None,
            "started_at": util.now_iso(), "finished_at": None,
        }})
        directory = pi_desktop_controller.invocation_dir("dispatch", "inv-no-turn")
        pi_desktop_controller.write_json(directory / "job.json", {
            "stage": "dispatch", "invocation_id": "inv-no-turn",
            "backend": "pi_desktop", "status": "running",
            "send_state": "unknown", "session_id": None, "turn_id": None,
        })
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch", resume=True)
        self.assertEqual(rc, 1)
        job = self.read_job(directory)
        self.assertEqual(job["status"], "running")
        self.assertEqual(self.server.prompt_count, 0)

    def test_timeout_stops_turn_and_never_marks_success(self):
        self.server.turn_status = "running"
        self.server.stop_aborts = True
        self.server.messages = default_messages()
        self.set_setting("executor_timeout_seconds", 1)
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch")
        self.assertEqual(rc, 1)
        job = self.read_job(self.invocation_dir("dispatch"))
        self.assertEqual(job["status"], "failed")
        self.assertNotEqual(job["exit_code"], 0)
        self.assertIn("timeout", job["detail"])
        self.assertGreaterEqual(self.server.stop_calls, 1)
        events = self.read_events(self.invocation_dir("dispatch"))
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")

    def test_permission_waiting_is_not_success(self):
        """权限等待：等待不算成功，超时后明确失败并保留权限等待证据。"""
        self.server.turn_status = "running"
        self.server.pending_tool_confirmations = [
            {"toolCallId": "call-1", "toolName": "bash"}
        ]
        self.server.messages = default_messages()
        self.set_setting("executor_timeout_seconds", 1)
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch")
        self.assertEqual(rc, 1)
        job = self.read_job(self.invocation_dir("dispatch"))
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job.get("permission_waiting"))
        self.assertIn("permission_waiting", job["detail"])
        self.assertEqual(job["send_state"], "sent",
                         "发送已获证明：权限等待是明确失败证据，不是发送不明")
        events = self.read_events(self.invocation_dir("dispatch"))
        self.assertTrue(any(
            "permission_waiting" in (e.get("content") or "") for e in events
        ))
        self.assertNotEqual(events[-1]["outcome"], "completed")

    def test_controller_unavailable_when_connection_missing(self):
        """连接文件缺失 → 明确失败（pi_desktop_controller_unavailable），不回退 pi。"""
        missing = self.root / "nope" / "mcp-control.json"
        self.seed_plus_settings(block={"connection_file": str(missing)})
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch")
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.init_count, 0)
        run = self.executor_run("dispatch")
        self.assertEqual(run["backend"], "pi_desktop")
        self.assertEqual(run["send_state"], "not_sent")
        job = self.read_job(self.invocation_dir("dispatch"))
        self.assertEqual(job["status"], "failed")
        self.assertIn("连接文件不存在", job["detail"])

    def test_tool_gate_requires_controller_tools(self):
        """工具门：缺 pi_agent_stop → 控制端不可用（发送前失败）。"""
        self.server.tools = [
            name for name in ALL_TOOLS if name != "pi_agent_stop"
        ]
        self.server.httpd.tools = self.server.tools
        self.server.httpd._owner = self.server
        with self.no_subprocess_spy():
            rc = self.run_stage("dispatch")
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.prompt_count, 0)
        self.assertIsNone(self.server.created)

    def test_dispatch_ignores_controller_backend_and_never_launches_model_cli(self):
        """缺省、pi、pi_desktop 均走 auto_dispatch，不渲染 prompt 或拉起模型。"""
        for backend in (None, "pi", "pi_desktop"):
            with self.subTest(controller_backend=backend):
                config = {"pi_desktop": dict(PI_DESKTOP_BLOCK)}
                if backend is not None:
                    config["controller_backend"] = backend
                self.set_setting("exec_config", config)
                with mock.patch.object(auto_dispatch, "run_stage", return_value=0) as dispatch, \
                        mock.patch.object(pi_desktop_controller, "run_stage") as legacy, \
                        mock.patch.object(executor_runner, "render_prompt",
                                          side_effect=AssertionError("新 dispatch 不应渲染模型 prompt")), \
                        self.no_subprocess_spy():
                    rc = executor_runner.run_stage("dispatch")
                self.assertEqual(rc, 0)
                dispatch.assert_called_once()
                self.assertEqual(dispatch.call_args.args, ("dispatch",))
                self.assertEqual(dispatch.call_args.kwargs["db_path"], self.db_path)
                self.assertFalse(dispatch.call_args.kwargs["resume"])
                dispatch.call_args.kwargs["conn"].close()
                legacy.assert_not_called()
                self.assertEqual(self.server.calls, [])


# ------------------------------------------------------------ monitor 控制轮


class TestMonitorControlRound(PlusCase):
    def test_monitor_round_uses_monitor_prompt_and_never_creates_sessions(self):
        self.server.turn_status = "running"
        self.server.turn_script = ["running", "running", "completed"]
        self.server.messages = default_messages()
        with self.no_subprocess_spy():
            rc = self.run_stage("monitor")
        self.assertEqual(rc, 0)
        prompt = self.server.prompt_content
        self.assertIn("只使用本会话挂接的命名 kp_task_* MCP 工具", prompt)
        self.assertNotIn("kanpilot-cli", prompt)
        self.assertIn("kp_task_observe", prompt)
        self.assertEqual(self.server.created["mode"], "agent")
        run = self.executor_run("monitor")
        self.assertEqual(run["backend"], "pi_desktop")
        self.assertEqual(run["turn_id"], TURN_ID)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
