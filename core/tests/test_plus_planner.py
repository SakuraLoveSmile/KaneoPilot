"""v4/T6.2 Plus 计划生成测试（protocol.md §5.24.3，planner T6.2-B）。

覆盖：
- codex 分支不受影响（缺 ``planner_backend`` 的旧请求照旧走 codex）；
- 非法 ``planner_backend`` → 明确失败，不暗换后端；
- Plus 快照门（五字段 / 新请求 ``auto``、兼容既有 ``ask``）与 ``planner_method`` 门
  （``spec_plan`` 明确禁用）、输入 manifest 指纹门；
- 独立规划 worktree（git worktree，HEAD 基线，不写用户主工作区）+ 材料哈希
  校验与复制 + 图片附件（Plus ``AgentPromptAttachment`` 真实形状）；
- ``pi_session_create(mode="plan")`` / ``pi_session_configure(快照权限)`` /
  发送前 ``send_state="unknown"`` / 持久 turn 启动证明后 ``sent``；
- ``ready`` 唯一来源 = 本 session/turn 的 ``kind="plan"`` proposal 严格身份
  匹配；reject 关闭并**确认 rejected**（空 pending 清单不算成功）；
- ``needs_input`` 最终答复 → ``report needs_user``；
- 审批冲突（已批准 / 出现执行轮次）禁止发布、不发 approve；
- 失败分类：``plan_proposal_missing``/``plan_proposal_ambiguous``/
  ``plan_approval_conflict``/``plan_reject_failed``/``attachment_unsupported``/
  ``transcript_incomplete``；
- 恢复不重发（同请求历史轮次已发送 → 只读重连观察）、无 turn 的 unknown 保留
  人工阻塞；
- review_fix Plus 计划仍核验 ``review_key``/报告并回写 frontmatter。

fake 一律真实形状：字符串消息 ID、物理 ``messageStart``/``messageEnd`` 游标、
``PlanProposal``（id/sessionId/turnId/toolCallId/kind/status/version/markdown）。

T6.2-B 交付状态（工作树可读，供集成核对；2026-09-30 23:25）
------------------------------------------------------------
改动文件（全部在本任务 writeScopes 内）：
1. ``core/kanpilot/planner_runner.py`` — payload ``planner_backend`` 分流
   （缺省/``codex`` 分支一字未改；``pi_desktop`` → ``pi_desktop_planner``；
   非法值 ``_fail_unknown_backend`` 明确失败）；frontmatter 元数据构造抽为
   ``build_plan_meta(..., model_config=None)``。
2. ``core/kanpilot/pi_desktop_planner.py``（新增）— 快照门/输入指纹门/独立
   git 规划 worktree/材料哈希校验与复制/图片附件真实形状/plan+ask 会话/
   发送前 unknown/启动证明/proposal 严格身份匹配与 reject 确认/needs_input/
   失败分类/恢复不重发。
3. ``core/executor_prompts/planner_prompt.md`` — 后端中立化（Codex 契约原样
   保留，Plus 契约由执行端追加），明确请求由用户手动
   ``prepare-plan-input`` + ``start-plans`` 发起。
4. 本文件（41 用例）。

命令与结果：
- ``cd core && /Users/developer/.pyenv/shims/python3 -m pytest -p no:cacheprovider
  tests/test_plus_planner.py tests/test_planner_method.py -q`` → 62 passed
- ``… tests/test_plus_planner.py -q`` → 41 passed
- ``… tests -q`` → **514 passed, 31 subtests passed, 0 failed**（2026-10-01 Lead 复跑；
  作者交付时为 506/24，其后 B/C 的用例与本文件的收紧断言一并计入）

遗留（均属他人独占文件的接口，非本任务代码缺口；已 send_message 报主 agent）：
A. **已闭环（2026-10-01）**：``cli.py`` 已登记 ``backend``/``pi_desktop``
   （``set-planner-config``/``set-review-config``）与 ``controller_backend``；
   真实 CLI 实测三条 ``set-*`` 均成功并返回完整 config（以 ``cli.COMMANDS``
   注册表为准）。本测试夹具此前走 ``planner_config.save(...)`` 以避开缺表，
   现在可直接回归 CLI 入口。
B. **已闭环（2026-10-01）**：核心的 ``planfile.expected_model_snapshot`` 已按请求
   的 ``runtime_config`` 校验 Plus 请求（``cmd_exec.cmd_publish_plan`` 与
   ``pi_desktop_planner`` 共用同一解析口径），因此 Plus 计划现在发布**真值**
   快照 —— ``planner_model = model_id``、``reasoning_effort = thinking_level``，
   ``model_pair_source = "runtime_config_snapshot"``（本节测试已按此收紧，
   不再接受 ``compat_constants_fallback``）；候选序里的 §6 常量只在快照确实
   缺失时才可能被选中并留痕。
C. **已闭环**：任务板 ``51af1954`` 等六个 T6 任务已由 Lead 置 completed。
附：两处解释性取舍——① 身份不符/多匹配/空正文/needs_input 与产物混入时不
reject、不发布，仅明确失败并保留 Plus 侧现场；reject 只在唯一匹配产物的
ready 路径执行；② 「转录完整」= 未截断 + 无读错误 + 非空，``unrecognized``
消息结构按 system 事件留证、不单独阻断 ready。
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import PROJECTS, WORKSPACE_ID, KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    constants, core, db, dsh_events, git_delivery, mcp_server, mcp_scopes,
    pi_desktop_backend, pi_desktop_planner, runtime_client,
    planner_config, planner_runner, round_store, util,
)

TOKEN = "test-mcp-token-plan-0001"
SESSION_ID = "pd-plan-sess-1"
TURN_ID = "pd-plan-turn-1"
EXEC_TURN_ID = "pd-plan-exec-1"
PROPOSAL_ID = "proposal-plan-1"
TOOL_CALL_ID = "toolcall-plan-1"
TASK_ID = "taskplus00000000001"
PLUS_TOOLS = tuple(constants.PI_DESKTOP_PLAN_TOOLS)


# ------------------------------------------------------------ fake MCP 控制面


class _FakeHandler(http.server.BaseHTTPRequestHandler):
    """PI-Desktop 控制面 stub（streamable-http JSON-RPC over POST /mcp）。"""

    def log_message(self, *args):  # 静默
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode("utf-8") if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server 约定
        srv = self.server.owner
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        srv.requests.append(self.headers.get("Authorization"))
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
                    "serverInfo": {"name": "fake-pi-desktop",
                                   "version": "0.9.9"},
                },
            })
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
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": srv.tool_call(name, args),
            })
            return
        self._send(200, {
            "jsonrpc": "2.0", "id": msg.get("id"),
            "error": {"code": -32601, "message": "method not found"},
        })


class FakePlusServer:
    """线程内 fake 控制面：会话/turn/分页转录/plan 产物 + 行为开关。"""

    def __init__(self):
        self.token = TOKEN
        self.tools = list(PLUS_TOOLS)
        self.requests: list = []
        self.calls: list = []
        self.init_count = 0
        self.session_id = SESSION_ID
        self.turn_id = TURN_ID
        # 会话/发送
        self.interaction_mode = "unattended"
        self.interaction_policy_version = 1
        self.created = None
        self.configured = None
        self.prompt_content = None
        self.prompt_attachments = None
        self.prompt_count = 0
        self.prompt_mode = "ok"          # ok | lost
        # 轮次
        self.turn_status = "completed"
        self.turn_statuses: list = []    # 队列优先
        self.turn_error_code = None
        self.current_turn = TURN_ID
        self.is_running = False
        # 转录（字符串消息 ID + 物理游标）
        self.messages: list = []
        self.page_size = 200
        self.transcript_truncated = False
        # 产物
        self.plans: list = []
        self.history: list = []
        self.plans_kind = "plan"
        self.resolve_mode = "ok"         # ok | error | error_concurrent | quiet
        self.resolve_calls: list = []
        self.execution_turn = None
        # 目录
        self.providers = [{"id": "test-provider", "disabled": False}]
        self.models = [{"id": "test-model", "modelId": "test-model"}]
        self.model_source = "cache"
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeHandler)
        self.httpd.owner = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    # ---------------------------------------------------------- 状态助手

    def add_message(self, role, content, **extra):
        """transcript 消息（id 为字符串；物理位置由列表下标决定）。"""
        message = {
            "id": f"msg-{len(self.messages) + 1}", "role": role,
            "content": content, "timestamp": util.now_iso(),
        }
        message.update(extra)
        self.messages.append(message)
        return message

    def add_plan_proposal(self, *, kind="plan", status="pending",
                          markdown="# 计划\n\n1. 步骤一\n",
                          session_id=SESSION_ID, turn_id=TURN_ID,
                          tool_call_id=TOOL_CALL_ID, proposal_id=PROPOSAL_ID,
                          version=3, title="测试计划"):
        proposal = {
            "id": proposal_id, "sessionId": session_id, "turnId": turn_id,
            "toolCallId": tool_call_id, "kind": kind, "title": title,
            "markdown": markdown, "plan": markdown, "question": "",
            "version": version, "status": status,
            "createdAt": util.now_iso(), "updatedAt": util.now_iso(),
        }
        self.plans.append(proposal)
        return proposal

    def tool_names(self):
        return [name for name, _args in self.calls]

    # ---------------------------------------------------------- tool_call

    def tool_call(self, name, args):
        if name == "pi_desktop_invoke":
            operation = args.get("operation")
            if operation == "providers/list":
                return {"structuredContent": {
                    "operation": operation,
                    "result": {"providers": self.providers}}}
            if operation == "providers/listModels":
                return {"structuredContent": {
                    "operation": operation,
                    "result": {"models": self.models,
                               "source": self.model_source}}}
            return {"structuredContent": {"error": {
                "code": "NOT_FOUND", "message": operation}}}
        if name == "pi_session_create":
            self.created = dict(args)
            return {"structuredContent": {"session": {
                "id": self.session_id, "title": args.get("title"),
                "interactionMode": self.interaction_mode, "interactionPolicyVersion": self.interaction_policy_version}}}
        if name == "pi_session_configure":
            self.configured = dict(args)
            return {"structuredContent": {"ok": True, "session": {"id": self.session_id, "interactionMode": self.interaction_mode, "interactionPolicyVersion": self.interaction_policy_version}}}
        if name == "pi_session_attach_mcp":
            grant = json.loads(Path(args["bridgeFile"]).read_text(encoding="utf-8"))
            role = grant.get("role")
            return {"structuredContent": {
                "sessionId": args.get("sessionId"), "attached": True,
                "serverName": constants.MCP_SERVER_NAME, "role": role,
                "toolNames": sorted(mcp_scopes.TASK_ROLE_TOOLS[role]),
            }}
        if name == "pi_agent_prompt":
            self.prompt_count += 1
            self.prompt_content = args.get("content")
            self.prompt_attachments = args.get("attachments")
            if self.prompt_mode == "lost":
                return {"isError": True, "structuredContent": {"error": {
                    "code": "INTERNAL", "message": "response lost mid-flight"}}}
            # 批准后宿主在同会话开启执行轮次时，execution_turn 立即生效。
            self.current_turn = self.execution_turn or self.turn_id
            return {"structuredContent": {"accepted": True,
                                          "turnId": self.turn_id}}
        if name == "pi_agent_status":
            return {"structuredContent": {"status": {
                "sessionId": args.get("sessionId"),
                "isRunning": self.is_running,
                "currentTurnId": self.current_turn,
                "planningState": ("awaiting_approval" if self.plans
                                  else "inactive"),
                "pendingToolConfirmations": [],
            }}}
        if name == "pi_session_get":
            if self.transcript_truncated and self.prompt_count:
                return {"structuredContent": {
                    "truncated": True,
                    "session": {"id": self.session_id, "messages": []}}}
            before = args.get("messageBefore")
            limit = args.get("messageLimit") or 200
            limit = min(limit, self.page_size)
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
        if name == "pi_turn_get":
            status = (self.turn_statuses.pop(0) if self.turn_statuses
                      else self.turn_status)
            record = {
                "sessionId": args.get("sessionId"),
                "turnId": args.get("turnId"), "status": status,
            }
            if self.turn_error_code:
                record["errorCode"] = self.turn_error_code
            return {"structuredContent": record}
        if name == "pi_plans_pending":
            pending = [dict(item) for item in self.plans
                       if item.get("status") == "pending"]
            # 已关闭/已批准的产物只经 history（近期不可变检查点）可见。
            closed = [dict(item) for item in self.plans
                      if item.get("status") != "pending"]
            return {"structuredContent": {
                "plans": pending,
                "history": closed + [dict(item) for item in self.history],
                "state": "awaiting_approval" if pending else "inactive",
                "kind": self.plans_kind,
            }}
        if name == "pi_plans_resolve":
            self.resolve_calls.append(dict(args))
            if self.resolve_mode in ("error", "error_concurrent"):
                if self.resolve_mode == "error_concurrent":
                    self._close(args.get("proposalId"), "rejected")
                return {"structuredContent": {"error": {
                    "code": "INTERNAL", "message": "resolve failed"}}}
            closed = self._close(
                args.get("proposalId"),
                "approved" if args.get("action") == "approve" else "rejected")
            if closed is None:
                return {"structuredContent": {"error": {
                    "code": "NOT_FOUND", "message": "proposal not found"}}}
            if self.resolve_mode == "quiet":
                # 已关闭但响应体不带 proposal（须靠 pending/history 确认）。
                return {"structuredContent": {
                    "ok": True, "action": args.get("action")}}
            return {"structuredContent": {
                "ok": True, "action": args.get("action"),
                "proposal": dict(closed), "state": "inactive"}}
        if name == "pi_agent_stop":
            self.is_running = False
            self.turn_status = "aborted"
            return {"structuredContent": {"ok": True}}
        if name == "pi_agent_abort":
            self.is_running = False
            self.turn_status = "aborted"
            return {"structuredContent": {"ok": True}}
        return {"structuredContent": {"error": {
            "code": "NOT_FOUND", "message": f"unknown tool {name}"}}}

    def _close(self, proposal_id, status):
        """把 pending 产物移入 history 并置终态（模拟宿主关闭）。"""
        found = None
        for item in list(self.plans):
            if item.get("id") == proposal_id:
                found = item
                self.plans.remove(item)
                found["status"] = status
                found["resolvedAt"] = util.now_iso()
                self.history.append(found)
        return found


# ------------------------------------------------------------ 立即返回的等待

_FAST_WAITS = {
    "_POLL_SECONDS": 0.05,
    "_PROOF_POLL_SECONDS": 0.02,
    "_PROOF_WAIT_SECONDS": 1.0,
    "_PROPOSAL_WAIT_SECONDS": 0.4,
    "_PROPOSAL_POLL_SECONDS": 0.02,
    "_REJECT_CONFIRM_SECONDS": 0.3,
    "_REJECT_CONFIRM_POLL_SECONDS": 0.02,
    "_STOP_GRACE_SECONDS": 0.1,
    "_STOP_VERIFY_SECONDS": 0.2,
}


class _FastWaits:
    def __enter__(self):
        self._patcher = mock.patch.multiple(pi_desktop_planner, **_FAST_WAITS)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


# ------------------------------------------------------------ 测试基类


class PlusPlannerCase(KanpilotTestCase):
    """隔离接缝：Plus 连接文件、会话/运行根、git 仓库、真实 start-plans 链路。"""

    def setUp(self):
        super().setUp()
        self._snapshots: dict = {}
        self.server = FakePlusServer().start()
        self.addCleanup(self.server.stop)
        self.runs_root = self.app_support / "runs"
        self.worktrees_root = self.app_support / "worktrees"
        self.inputs_root = self.app_support / "planner_inputs"
        for path in (self.runs_root, self.worktrees_root, self.inputs_root):
            path.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root",
                               str(self.runs_root))
                db.set_setting(conn, "worktrees_root",
                               str(self.worktrees_root))
        finally:
            conn.close()
        self.connection_file = self.write_connection()
        self.project_mcp = mcp_server.MCPServer(
            db_path=self.db_path, app_support_dir=self.app_support,
            connection_file=self.app_support / "mcp" / "server.json",
        )
        self.project_mcp.start()
        self.addCleanup(self.project_mcp.stop)
        os.environ.pop("KANPILOT_CODEX_FAKE", None)
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)

    def fast(self):
        return _FastWaits()

    # ---------------------------------------------------------- 夹具

    def write_connection(self, **overrides):
        data = {
            "active": True, "url": self.server.url, "token": self.server.token,
            "pid": os.getpid(), "startedAt": util.now_iso(),
        }
        data.update(overrides)
        path = self.root / "pi-desktop" / "mcp-control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        return path

    def runtime_block(self, **overrides):
        block = {
            "connection_file": str(self.connection_file),
            "provider_id": "test-provider", "model_id": "test-model",
            "thinking_level": "medium", "permission_mode": "auto",
        }
        block.update(overrides)
        return block

    def save_plus_config(self, **overrides):
        """保存 Plus 规划配置（经 planner_config.save 的离线结构校验路径）。

        注：CLI ``set-planner-config`` 的字段表（cli.py，主 agent 独占）当前尚未
        登记 ``backend``/``pi_desktop``，故测试直接使用同一模块的保存入口（校验
        语义一致：五字段 + permission_mode=auto）。
        """
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                return planner_config.save(conn, {
                    "backend": "pi_desktop",
                    "pi_desktop": self.runtime_block(**overrides),
                }, util.now_iso())
        finally:
            conn.close()

    def git_repo(self, project_name="Feedback"):
        """真实 git 仓库（HEAD 提交），供独立规划 worktree 使用。"""
        if not shutil.which("git"):
            self.skipTest("git 不可用")
        repo = self.make_repo(project_name)
        (repo / "AGENTS.md").write_text("# repo\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=test@example.com",
             "-c", "user.name=test", "commit", "-qm", "init"],
            check=True)
        return repo

    def plain_repo(self, project_name="Feedback"):
        path = self.repos_root / project_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def register_task(self, task_id, repo, *, plan_kind="standard",
                      title="Plus 计划任务", status="to-do"):
        """远端任务 + 本地快照（requirements/source 指纹，publish-plan 复验用）。"""
        task = self.fake.add_task(task_id, "fixture-project-feedback",
                                  status=status, title=title)
        source_sha, req_sha, source_path = self.make_source_snapshot(task)
        self.sync_task(task, project_name="Feedback", repository=str(repo),
                       plan_kind=plan_kind, requirements_sha256=req_sha,
                       source_sha256=source_sha, source_path=source_path)
        self._snapshots[task_id] = {"requirements_sha256": req_sha,
                                    "source_sha256": source_sha}
        return task

    def new_task(self, task_id=TASK_ID, *, repo=None, plan_kind="standard"):
        return self.register_task(task_id, repo, plan_kind=plan_kind)

    def manifest_task_block(self, task_id, repo, plan_kind="standard"):
        return {
            "task_id": task_id, "project_name": "Feedback",
            "title": "Plus 计划任务", "repository": str(repo),
            "plan_kind": plan_kind,
        }

    def make_input(self, task_id, repo, *, files=None, plan_kind="standard"):
        snapshot = self._snapshots.get(task_id) or {}
        return self.make_planner_input(
            task_id,
            requirements_sha256=snapshot.get("requirements_sha256"),
            source_sha256=snapshot.get("source_sha256"),
            manifest_extra={
                "task": self.manifest_task_block(task_id, repo, plan_kind),
                "files": files or [],
            },
        )

    def material_entry(self, payload: bytes, name="shot.png",
                       media_type="image/png", sha256=None, rel=None):
        """manifest 材料条目（形状与 planner_inputs.register 一致）。"""
        rel = rel or f"files/{name}"
        return {
            "name": name, "source": "attachment", "media_type": media_type,
            "sha256": sha256 if sha256 is not None
            else util.sha256_bytes(payload),
            "size": len(payload), "path": rel,
        }

    def write_material_file(self, info, rel, payload: bytes):
        path = self.inputs_root / info["input_id"] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def start_plus_plan(self, task_id, info, *, expect_ok=True):
        # Plus 项提交时要求已保存完整五字段（§5.24.3）；先落默认块（幂等）。
        self.save_plus_config()
        result = self.call("start-plans", {"items": [{
            "input_id": info["input_id"],
            "input_sha256": info["input_sha256"],
            "planner_backend": "pi_desktop",
        }]})
        entry = result["results"][0]
        if expect_ok:
            self.assertTrue(entry.get("request_id"), f"提交失败: {entry}")
        return entry

    def plus_request(self, *, files=None, image_payload=None,
                     plan_kind="standard", task_id=TASK_ID, repo=None,
                     git=True):
        """完整 Plus 请求夹具：任务 + git 仓库 + 材料 + start-plans。"""
        repo = repo if repo is not None else (
            self.git_repo() if git else self.plain_repo())
        self.new_task(task_id, repo=repo, plan_kind=plan_kind)
        entries = list(files or [])
        payload = image_payload
        if payload is not None:
            entries.append(self.material_entry(payload))
        info = self.make_input(task_id, repo, files=entries,
                               plan_kind=plan_kind)
        if payload is not None:
            self.write_material_file(info, f"files/shot.png", payload)
        entry = self.start_plus_plan(task_id, info)
        return {
            "task_id": task_id, "repo": repo, "request_id": entry["request_id"],
            "info": info,
        }

    # ---------------------------------------------------------- 运行/读取

    def run_round(self):
        progressed = planner_runner.run_one(self.db_path)
        self.assertTrue(progressed, "run_one 未领取到 plan 请求")

    def latest_run_id(self, request_id):
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT run_id FROM attempts WHERE request_id=? "
                "ORDER BY id DESC LIMIT 1", (request_id,)).fetchone()
        finally:
            conn.close()
        return row["run_id"] if row else None

    def run_dir(self, task_id, run_id):
        return self.runs_root / task_id / run_id

    def job_for(self, task_id, request_id):
        run_id = self.latest_run_id(request_id)
        self.assertTrue(run_id, "没有轮次记录")
        path = self.run_dir(task_id, run_id) / dsh_events.JOB_NAME
        return run_id, json.loads(path.read_text(encoding="utf-8"))

    def events_for(self, task_id, run_id):
        events, detail = dsh_events.iter_events(
            self.run_dir(task_id, run_id) / dsh_events.EVENT_LOG_NAME)
        self.assertIsNone(detail, f"events.jsonl 解析失败：{detail}")
        return events

    def terminal_reason(self, events):
        terminal = dsh_events.terminal_event(events)
        self.assertIsNotNone(terminal, "缺 terminal 事件")
        return terminal["reason"]

    def request_view(self, request_id):
        return next(r for r in self.requests()
                    if r["request_id"] == request_id)

    def request_payload(self, request_id):
        result = self.call("get-request", {"request_id": request_id})
        payload = result.get("payload")
        if payload is None:
            payload = (result.get("request") or {}).get("payload")
        return payload or {}

    def git_status(self, repo):
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def plan_files(self, project_name="Feedback"):
        files = []
        directory = self.plan_root / project_name
        if directory.is_dir():
            files.extend(directory.glob("*.md"))
        pid = PROJECTS.get(project_name, project_name)
        proj_dir = self.plan_root / "projects" / pid
        if proj_dir.is_dir():
            files.extend(proj_dir.glob("*.md"))
        return sorted(set(files))


class TestWorkspaceRepairPlanningGate(PlusPlannerCase):
    def set_repair_gate(self, task_id):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "review_fix_workspace_repair:" + task_id,
                               "repair:pending")
        finally:
            conn.close()

    def test_repair_blocks_start_without_queuing_or_prompting(self):
        repo = self.git_repo()
        self.new_task(TASK_ID, repo=repo)
        info = self.make_input(TASK_ID, repo)
        self.set_repair_gate(TASK_ID)
        result = self.start_plus_plan(TASK_ID, info, expect_ok=False)
        self.assertEqual(result["code"], "conflict")
        self.assertIn("恢复尚未完成", result["error"])
        self.assertFalse(self.requests())
        self.assertEqual(self.server.prompt_count, 0)

    def test_repair_blocks_claim_of_preexisting_queued_request(self):
        fixture = self.plus_request()
        self.set_repair_gate(fixture["task_id"])
        with self.assertRaisesRegex(util.KanpilotError, "恢复尚未完成"):
            planner_runner.run_one(self.db_path)
        self.assertEqual(self.request_view(fixture["request_id"])["status"], "queued")
        self.assertIsNone(self.latest_run_id(fixture["request_id"]))
        self.assertEqual(self.server.prompt_count, 0)


# ------------------------------------------------------------ codex 兼容


class TestCodexBranchUntouched(PlusPlannerCase):
    """缺 planner_backend 的旧请求（以及 codex 请求）行为不变。"""

    def codex_plan_request(self, task_id, repo):
        """codex 请求（payload 无 planner_backend，模型快照在 model_config）。"""
        info = self.make_input(task_id, repo)
        payload = {
            "manual_authorized": True,
            "input_id": info["input_id"],
            "planner_input_sha256": info["input_sha256"],
            "manifest": info["manifest"],
            "model_config": dict(planner_config.DEFAULTS["standard"]),
            "confirmed_at": util.now_iso(),
        }
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                request, _created = db.create_request(
                    conn, util.now_iso(), "plan", f"plan:{task_id}",
                    task_id=task_id, payload=payload, requested_by="user")
                request_id = request["request_id"]
        finally:
            conn.close()
        return request_id

    def test_legacy_request_still_runs_codex(self):
        os.environ["KANPILOT_CODEX_FAKE"] = "ready"
        repo = self.plain_repo()
        self.new_task("taskcodex00000000001", repo=repo)
        request_id = self.codex_plan_request("taskcodex00000000001", repo)
        self.run_round()

        view = self.task("taskcodex00000000001")
        self.assertEqual(view["status"], "ready")
        self.assertTrue(view["plan_path"])
        self.assertTrue(self.plan_files(), "codex 分支未产出计划文件")
        job = json.loads(
            (self.run_dir("taskcodex00000000001",
                          self.latest_run_id(request_id))
             / dsh_events.JOB_NAME).read_text())
        self.assertEqual(job["backend"], "codex")

    def test_plus_request_does_not_touch_codex_once_flag(self):
        """Plus 分支不落 codex.jsonl / 不拉 codex（无 codex 侧产物）。"""
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "plan prompt")
        self.server.add_message("assistant", json.dumps({
            "status": "needs_input", "plan_markdown": "",
            "questions": ["缺材料"], "evidence": []}))
        with self.fast():
            self.run_round()
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["backend"], "pi_desktop")
        run_dir = self.run_dir(fixture["task_id"], run_id)
        self.assertFalse(
            (run_dir / constants.PLANNER_CODEX_LOG_NAME).exists(),
            "Plus 轮次不应产生 codex.jsonl")

    def test_unknown_backend_fails_explicitly(self):
        repo = self.git_repo()
        self.new_task("taskbadbackend0000001", repo=repo)
        info = self.make_input("taskbadbackend0000001", repo)
        request_id, _ = self.queue_plan("taskbadbackend0000001")
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, request_id)
            payload = db.request_payload(request)
            payload["planner_backend"] = "mystery"
            with db.write_txn(conn):
                db.update_request(conn, request_id, util.now_iso(),
                                 payload_json=payload)
        finally:
            conn.close()
        self.run_round()

        view = self.task("taskbadbackend0000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(self.request_view(request_id)["status"], "failed")
        _run_id, job = self.job_for("taskbadbackend0000001", request_id)
        self.assertEqual(job["backend"], "mystery")
        self.assertIn("非法", job["detail"])
        self.assertEqual(self.server.created, None)
        self.assertEqual(self.server.prompt_count, 0)
        # 不产出计划文件（没有换后端执行）。
        self.assertFalse(self.plan_files())


# ------------------------------------------------------------ 快照/输入门


class TestPlusGates(PlusPlannerCase):
    def test_runtime_snapshot_required(self):
        repo = self.git_repo()
        self.new_task("tasknosnapshot00000001", repo=repo)
        info = self.make_input("tasknosnapshot00000001", repo)
        entry = self.start_plus_plan("tasknosnapshot00000001", info)
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, entry["request_id"])
            payload = db.request_payload(request)
            payload.pop("runtime_config", None)
            with db.write_txn(conn):
                db.update_request(conn, entry["request_id"], util.now_iso(),
                                 payload_json=payload)
        finally:
            conn.close()
        with self.fast():
            self.run_round()
        view = self.task("tasknosnapshot00000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(view["failure_code"], "planner_failed")
        self.assertEqual(self.request_view(entry["request_id"])["status"],
                         "failed")
        self.assertIn("runtime_config",
                      self.task("tasknosnapshot00000001")["failure_reason"])
        self.assertEqual(self.server.created, None)

    def test_permission_mode_rejects_accept_edits(self):
        repo = self.git_repo()
        self.new_task("taskperm000000000001", repo=repo)
        info = self.make_input("taskperm000000000001", repo)
        entry = self.start_plus_plan("taskperm000000000001", info)
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, entry["request_id"])
            payload = db.request_payload(request)
            payload["runtime_config"]["permission_mode"] = "accept-edits"
            with db.write_txn(conn):
                db.update_request(conn, entry["request_id"], util.now_iso(),
                                 payload_json=payload)
        finally:
            conn.close()
        with self.fast():
            self.run_round()
        view = self.task("taskperm000000000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("ask", view["failure_reason"])
        self.assertEqual(self.server.created, None)

    def test_spec_plan_disabled_under_plus(self):
        repo = self.git_repo()
        self.new_task("taskspec000000000001", repo=repo)
        info = self.make_input("taskspec000000000001", repo)
        entry = self.start_plus_plan("taskspec000000000001", info)
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, entry["request_id"])
            payload = db.request_payload(request)
            payload["planner_method"] = "spec_plan"
            with db.write_txn(conn):
                db.update_request(conn, entry["request_id"], util.now_iso(),
                                 payload_json=payload)
        finally:
            conn.close()
        with self.fast():
            self.run_round()
        view = self.task("taskspec000000000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("spec_plan", view["failure_reason"])
        self.assertEqual(self.server.created, None)
        self.assertIsNone(self.server.prompt_content)

    def test_input_fingerprint_mismatch(self):
        repo = self.git_repo()
        self.new_task("taskinputsha000000001", repo=repo)
        info = self.make_input("taskinputsha000000001", repo)
        entry = self.start_plus_plan("taskinputsha000000001", info)
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, entry["request_id"])
            payload = db.request_payload(request)
            payload["planner_input_sha256"] = "0" * 64
            with db.write_txn(conn):
                db.update_request(conn, entry["request_id"], util.now_iso(),
                                 payload_json=payload)
        finally:
            conn.close()
        with self.fast():
            self.run_round()
        view = self.task("taskinputsha000000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("指纹", view["failure_reason"])
        self.assertEqual(self.server.created, None)

    def test_worktree_requires_git_repo(self):
        """非 git 仓库 → 明确失败，绝不退回用户主工作区。"""
        repo = self.plain_repo()
        self.new_task("tasknogit00000000001", repo=repo)
        info = self.make_input("tasknogit00000000001", repo)
        self.save_plus_config()
        entry = self.call("start-plans", {"items": [{"input_id": info["input_id"],
            "input_sha256": info["input_sha256"], "planner_backend": "pi_desktop"}]})["results"][0]
        self.assertEqual(entry["code"], "worktree_invalid")
        view = self.task("tasknogit00000000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(self.server.created, None)
        # 没有在用户仓库里落下任何东西。
        self.assertEqual(sorted(p.name for p in repo.iterdir()), [])


# ------------------------------------------------------------ happy path


class TestPlusReadyPath(PlusPlannerCase):
    def _fixture(self, *, markdown="# 实施计划\n\n1. 步骤一\n2. 步骤二\n"):
        payload = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        fixture = self.plus_request(image_payload=payload)
        self.save_plus_config()
        self.server.add_message("user", "kanpilot plan prompt")
        self.server.add_message("assistant", markdown)
        self.server.add_plan_proposal(markdown=markdown)
        return fixture, payload

    def test_legacy_queued_ask_snapshot_is_configured_without_escalation(self):
        fixture, _ = self._fixture()
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, fixture["request_id"])
            payload = db.request_payload(request)
            payload["runtime_config"]["permission_mode"] = "ask"
            with db.write_txn(conn):
                db.update_request(conn, fixture["request_id"], util.now_iso(), payload_json=payload)
        finally:
            conn.close()
        # Current settings remain auto, but this already authorized request is ask.
        self.save_plus_config()
        with self.fast():
            self.run_round()
        self.assertEqual(self.task(fixture["task_id"])["status"], "ready")
        self.assertEqual(self.server.configured["mode"], "plan")
        self.assertEqual(self.server.configured["permissionMode"], "ask")
        # 旧 ask 快照保留原说明；asktool 同样无人作答，仍禁止。
        self.assertIn("权限为 **Ask**", self.server.prompt_content)
        self.assertNotIn("权限为 **auto**", self.server.prompt_content)
        self.assertIn("**禁止**调用 `asktool`", self.server.prompt_content)
        _, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["config"]["permission_mode"], "ask")
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.request_payload(db.get_request(conn, fixture["request_id"]))["runtime_config"],
                             payload["runtime_config"])
        finally:
            conn.close()

    def test_ready_publishes_plan_and_rejects_proposal(self):
        fixture, _payload = self._fixture()
        request_id = fixture["request_id"]
        with self.fast():
            self.run_round()

        # 1. 会话与权限：mode=plan、permissionMode=auto、projectPath=规划 worktree
        created = self.server.created
        self.assertIsNotNone(created)
        self.assertEqual(created["mode"], "plan")
        self.assertEqual(created["providerId"], "test-provider")
        self.assertEqual(created["modelId"], "test-model")
        self.assertEqual(created["thinkingLevel"], "medium")
        worktree = Path(created["projectPath"])
        self.assertNotEqual(worktree, fixture["repo"])
        self.assertTrue(str(worktree).startswith(str(self.worktrees_root.resolve())))
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        self.assertEqual(self.server.configured["mode"], "plan")
        self.assertIs(self.server.configured["confirm"], True)
        self.assertEqual(self.server.configured["id"], SESSION_ID)
        setup_calls = [name for name in self.server.tool_names()
                       if name in ("pi_session_create", "pi_session_configure",
                                   "pi_session_attach_mcp", "pi_agent_prompt")]
        self.assertEqual(setup_calls, ["pi_session_create", "pi_session_configure",
                                       "pi_agent_prompt"])
        self.assertNotIn("kp_task_", self.server.prompt_content)

        # 2. reject 关闭生成阶段产物，绝不 approve
        self.assertTrue(self.server.resolve_calls, "未调用 plans_resolve")
        resolve = self.server.resolve_calls[0]
        self.assertEqual(resolve["action"], "reject")
        self.assertIs(resolve["confirm"], True)
        self.assertEqual(resolve["proposalId"], PROPOSAL_ID)
        self.assertEqual(resolve["sessionId"], SESSION_ID)
        self.assertEqual(resolve["turnId"], TURN_ID)
        self.assertEqual(resolve["toolCallId"], TOOL_CALL_ID)
        self.assertEqual(resolve["version"], 3)
        self.assertNotIn("targetPermissionMode", resolve)
        self.assertTrue(all(call["action"] == "reject"
                            for call in self.server.resolve_calls))
        self.assertEqual(self.server.plans, [], "产物未被关闭")
        self.assertEqual(self.server.history[0]["status"], "rejected")

        # 3. 发布：任务 ready + hold，请求 finished
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "ready")
        self.assertEqual(self.request_view(request_id)["status"], "succeeded")
        self.assertTrue(view["plan_path"])
        plan_extra = self.call("get-task",
                               {"task_id": fixture["task_id"]})["task"]
        self.assertTrue(plan_extra.get("plan_execution_hold", True))

        # 4. 计划文件 frontmatter：真实输入 + 模型对 + 用户仓库（非 worktree）
        plan_path = Path(view["plan_path"])
        self.assertTrue(plan_path.is_file())
        text = plan_path.read_text(encoding="utf-8")
        self.assertIn("1. 步骤一", text)
        self.assertIn(f'task_id: "{fixture["task_id"]}"', text)
        self.assertIn(f'repository: "{fixture["repo"]}"', text)
        self.assertNotIn(str(worktree), text)

        # 5. job/events 留痕：轮次身份、产物身份、reject 留痕
        run_id, job = self.job_for(fixture["task_id"], request_id)
        self.assertEqual(job["backend"], "pi_desktop")
        self.assertEqual(job["session_id"], SESSION_ID)
        self.assertEqual(job["turn_id"], TURN_ID)
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["plan_proposal"]["id"], PROPOSAL_ID)
        self.assertEqual(job["plan_proposal"]["status"], "rejected")
        self.assertEqual(job["worktree_path"], str(worktree))
        self.assertEqual(job["plan_kind"], "standard")
        events = self.events_for(fixture["task_id"], run_id)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[0]["session_id"], SESSION_ID)
        self.assertEqual(self.terminal_reason(events), "plan_published")
        system = [e for e in events if e["type"] == "system"]
        self.assertTrue(any("plan_proposal_rejected" in (e.get("content") or "")
                            for e in system), f"缺 reject 留痕：{system}")

        # 6. 用户主工作区零写入（产物只在规划 worktree / PLAN_ROOT）
        self.assertEqual(self.git_status(fixture["repo"]), "")
        self.assertFalse((fixture["repo"] / ".pi").exists())

        # 7. 轮次索引：真实后端 + 会话身份 + worktree + 脱敏运行快照
        rounds = self.call("list-sessions",
                           {"task_id": fixture["task_id"]})["sessions"]
        self.assertTrue(rounds)
        self.assertEqual(rounds[0]["backend"], "pi_desktop")
        self.assertEqual(rounds[0]["session_id"], SESSION_ID)
        conn = db.connect(self.db_path)
        try:
            round_view = round_store.round_view(
                round_store.get_round(conn, fixture["task_id"], run_id))
        finally:
            conn.close()
        self.assertEqual(round_view["backend"], "pi_desktop")
        self.assertEqual(round_view["config"]["model_id"], "test-model")
        self.assertEqual(round_view["config"]["permission_mode"], "auto")
        self.assertEqual(round_view["worktree_path"], str(worktree))

    def test_materials_verified_and_copied_into_worktree(self):
        fixture, payload = self._fixture()
        with self.fast():
            self.run_round()
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        material_dir = Path(job["material_dir"])
        self.assertTrue(material_dir.is_dir())
        copied = material_dir / "shot.png"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), payload)
        self.assertEqual(util.sha256_file(copied),
                         util.sha256_bytes(payload))
        self.assertTrue((material_dir / "manifest.json").is_file())
        # 材料目录在规划 worktree 内（Plus 附件路径校验要求）。
        self.assertTrue(
            "plan-materials" in material_dir.parts)

    def test_image_attachment_real_plus_shape(self):
        fixture, payload = self._fixture()
        real_prompt = runtime_client.RuntimeClient.agent_prompt
        observed = {}
        def prompt(client, *args, **kwargs):
            observed["attachment_exists"] = Path(kwargs["attachments"][0]["path"]).is_file()
            return real_prompt(client, *args, **kwargs)
        with self.fast(), mock.patch.object(runtime_client.RuntimeClient, "agent_prompt", prompt):
            self.run_round()
        attachments = self.server.prompt_attachments
        self.assertIsInstance(attachments, list)
        self.assertEqual(len(attachments), 1)
        item = attachments[0]
        self.assertEqual(set(item), {"path", "name", "kind", "mimeType",
                                     "size"})
        self.assertEqual(item["kind"], "image")
        self.assertEqual(item["name"], "shot.png")
        self.assertEqual(item["mimeType"], "image/png")
        self.assertEqual(item["size"], len(payload))
        self.assertTrue(os.path.isabs(item["path"]))
        self.assertTrue(observed["attachment_exists"])
        self.assertFalse(Path(item["path"]).exists())
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertTrue(item["path"].startswith(job["worktree_path"] + os.sep))
        # 绝对路径也出现在 prompt 里（模型可直接读取）。
        self.assertIn(item["path"], self.server.prompt_content)
        self.assertIn("Pi-Desktop-Plus 计划模式", self.server.prompt_content)

    def test_auto_prompt_is_unattended_and_forbids_asktool(self):
        """§5.24.3 第 6 项：auto 轮次不声称 Ask；asktool 无人作答，必须禁止。"""
        self._fixture()
        with self.fast():
            self.run_round()
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        prompt = self.server.prompt_content
        self.assertIn("权限为 **auto**", prompt)
        self.assertIn("**无人值守**", prompt)
        self.assertNotIn("权限为 **Ask**", prompt)
        self.assertNotIn("{PERMISSION_LINE}", prompt)
        self.assertIn("**禁止**调用 `asktool`", prompt)
        self.assertIn("「用 asktool 解决未定选择」的指引本轮**不适用**", prompt)
        self.assertIn("待确认事项", prompt)

    def test_send_state_unknown_before_prompt_and_prompt_body_has_task(self):
        fixture, _payload = self._fixture()
        observed = {}
        real_prompt = runtime_client.RuntimeClient.agent_prompt

        def wrapped(client, session_id, content, attachments=None):
            observed["job"] = json.loads(
                (self.run_dir(fixture["task_id"],
                              self.latest_run_id(fixture["request_id"]))
                 / dsh_events.JOB_NAME).read_text(encoding="utf-8"))
            if attachments:
                return real_prompt(client, session_id, content,
                                   attachments=attachments)
            return real_prompt(client, session_id, content)

        with mock.patch.object(runtime_client.RuntimeClient, "agent_prompt",
                               wrapped), self.fast():
            self.run_round()
        self.assertEqual(observed["job"]["send_state"], "unknown",
                         "发送前必须先落 send_state=unknown")
        self.assertIsNone(observed["job"]["turn_id"])
        self.assertIn("Plus 计划任务", self.server.prompt_content)
        self.assertIn("SubmitPlan", self.server.prompt_content)

    def test_running_then_completed_turn_keeps_heartbeating(self):
        fixture, _payload = self._fixture()
        self.server.is_running = True
        self.server.current_turn = TURN_ID
        self.server.turn_statuses = ["running", "running", "completed"]
        with self.fast():
            self.run_round()
        self.assertEqual(self.task(fixture["task_id"])["status"], "ready")
        self.assertEqual(
            self.request_view(fixture["request_id"])["status"], "succeeded")


# ------------------------------------------------------------ proposal 身份


class TestProposalIdentity(PlusPlannerCase):
    def _base(self, markdown="# 计划\n"):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", markdown)
        return fixture

    def _assert_failed(self, fixture, reason):
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(view["failure_code"], "planner_failed")
        self.assertIn(reason, view["failure_reason"])
        self.assertEqual(self.request_view(fixture["request_id"])["status"],
                         "failed")
        run_id, job = self.job_for(fixture["task_id"],
                                  fixture["request_id"])
        events = self.events_for(fixture["task_id"], run_id)
        self.assertEqual(self.terminal_reason(events), reason)
        self.assertFalse(self.plan_files(), "失败轮次不得发布计划")
        return job

    def test_identity_mismatch_is_ambiguous(self):
        """pending plan 产物但 turnId 不同 → 不猜选，明确失败。"""
        fixture = self._base()
        self.server.add_plan_proposal(turn_id="other-turn")
        with self.fast():
            self.run_round()
        self._assert_failed(fixture, "plan_proposal_ambiguous")
        self.assertEqual(self.server.resolve_calls, [],
                         "身份不符时不得 reject/publish")

    def test_multiple_matching_proposals_ambiguous(self):
        fixture = self._base()
        self.server.add_plan_proposal(proposal_id="proposal-plan-1")
        self.server.add_plan_proposal(proposal_id="proposal-plan-2")
        with self.fast():
            self.run_round()
        self._assert_failed(fixture, "plan_proposal_ambiguous")
        self.assertEqual(self.server.resolve_calls, [])

    def test_no_proposal_and_garbage_answer_is_missing(self):
        fixture = self._base(markdown="我建议先重构。")
        with self.fast():
            self.run_round()
        self._assert_failed(fixture, "plan_proposal_missing")
        self.assertEqual(self.server.resolve_calls, [])

    def test_empty_proposal_markdown_is_missing(self):
        fixture = self._base()
        self.server.add_plan_proposal(markdown="")
        with self.fast():
            self.run_round()
        self._assert_failed(fixture, "plan_proposal_missing")
        # 空正文的产物不 reject、不发布，保留现场交人工核对。
        self.assertEqual(self.server.resolve_calls, [])
        self.assertTrue(self.plan_files() == [])

    def test_goal_kind_proposal_is_not_a_plan(self):
        """kind=goal 的产物不是计划来源（Goal 属任务 worker）。"""
        fixture = self._base()
        self.server.add_plan_proposal(kind="goal")
        with self.fast():
            self.run_round()
        self._assert_failed(fixture, "plan_proposal_missing")
        self.assertEqual(self.server.resolve_calls, [])


# ------------------------------------------------------------ reject 确认


class TestRejectConfirmation(PlusPlannerCase):
    def _base(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 计划\n")
        self.server.add_plan_proposal()
        return fixture

    def test_reject_confirmed_from_pending_history(self):
        fixture = self._base()
        self.server.resolve_mode = "quiet"     # 响应体不带 proposal
        with self.fast():
            self.run_round()
        self.assertEqual(self.task(fixture["task_id"])["status"], "ready")
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["plan_proposal"]["status"], "rejected")

    def test_reject_concurrent_close_is_accepted(self):
        """reject 调用报错但产物确已 rejected（并发关闭）→ 按已关闭继续。"""
        fixture = self._base()
        self.server.resolve_mode = "error_concurrent"
        with self.fast():
            self.run_round()
        self.assertEqual(self.task(fixture["task_id"])["status"], "ready")
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["plan_proposal"]["status"], "rejected")
        self.assertTrue(job["plan_proposal"]["concurrent"])

    def test_reject_failure_keeps_unpublished(self):
        """reject 失败且产物仍 pending → plan_reject_failed，保留产物不发布。"""
        fixture = self._base()
        self.server.resolve_mode = "error"
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_reject_failed", view["failure_reason"])
        self.assertEqual(self.request_view(fixture["request_id"])["status"],
                         "failed")
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertNotIn("plan_path", job)
        self.assertEqual(len(self.server.plans), 1, "pending 产物必须保留")
        self.assertFalse(self.plan_files())

    def test_empty_pending_after_reject_is_not_success(self):
        """产物从清单消失但没有任何 rejected 证据 → 空 pending 不算成功。"""
        fixture = self._base()

        def vanish(proposal_id, status):
            for item in list(self.server.plans):
                if item.get("id") == proposal_id:
                    self.server.plans.remove(item)
            return None

        self.server._close = vanish
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_reject_failed", view["failure_reason"])
        self.assertFalse(self.plan_files())


# ------------------------------------------------------------ 审批冲突


class TestApprovalConflict(PlusPlannerCase):
    def _base(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 计划\n")
        return fixture

    def test_approved_proposal_blocks_publish(self):
        fixture = self._base()
        self.server.add_plan_proposal(status="approved")
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_approval_conflict", view["failure_reason"])
        self.assertIn("不声称已阻止", view["failure_reason"])
        self.assertEqual(self.server.resolve_calls, [],
                         "冲突时不得 approve/reject")
        self.assertFalse(self.plan_files())
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertNotIn("plan_path", job)

    def test_execution_turn_blocks_publish(self):
        """同会话出现执行轮次（无待批准产物）→ 冲突，禁止二次派发。"""
        fixture = self._base()
        # 宿主批准后在同会话开启执行轮次（prompt 之后 currentTurnId 前移）。
        self.server.execution_turn = EXEC_TURN_ID
        self.server.turn_status = "completed"
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_approval_conflict", view["failure_reason"])
        self.assertIn(EXEC_TURN_ID, view["failure_reason"])
        self.assertFalse(self.plan_files())


# ------------------------------------------------------------ needs_input


class TestNeedsInput(PlusPlannerCase):
    def test_structured_question_preserved_in_software_card(self):
        from test_plan_answers import QUESTION
        fixture = self.plus_request()
        self.save_plus_config()
        answer = {"status": "needs_input", "plan_markdown": "# 草稿", "questions": [QUESTION], "evidence": []}
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", json.dumps(answer, ensure_ascii=False))
        with self.fast():
            self.run_round()
        view = self.call("get-task", {"task_id": fixture["task_id"]})["task"]
        self.assertEqual(view["plan_clarification"]["questions"], [QUESTION])
        self.assertIn("播放全部", view["failure_reason"])
        self.assertNotIn("{'id'", view["failure_reason"])
        self.assertEqual(self.server.resolve_calls, [])
        self.assertEqual(self.server.plans, [])


    def test_needs_input_reports_needs_user_without_proposal(self):
        fixture = self.plus_request()
        self.save_plus_config()
        answer = {
            "status": "needs_input",
            "plan_markdown": "# 草稿\n待确认",
            "questions": ["需要确认目标版本范围"],
            "evidence": ["AGENTS.md"],
        }
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", json.dumps(answer,
                                                        ensure_ascii=False))
        with self.fast():
            self.run_round()

        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["failure_code"], "reported")
        self.assertIn("需要确认目标版本范围", view["failure_reason"])
        # needs_user 汇报沿既有口径：请求终态 failed + result.outcome=needs_user
        request_view = self.request_view(fixture["request_id"])
        self.assertEqual(request_view["status"], "failed")
        self.assertEqual(request_view["result"]["outcome"], "needs_user")
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        events = self.events_for(fixture["task_id"], run_id)
        self.assertEqual(self.terminal_reason(events), "needs_input")
        draft = self.run_dir(fixture["task_id"], run_id) / "plan-draft.md"
        self.assertTrue(draft.is_file())
        self.assertIn("待确认", draft.read_text(encoding="utf-8"))
        # 不提交产物、不发 approve、不发布计划。
        self.assertEqual(self.server.resolve_calls, [])
        self.assertEqual(self.server.plans, [])
        self.assertFalse(self.plan_files())

    def test_needs_input_with_proposal_is_ambiguous(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", json.dumps({
            "status": "needs_input", "plan_markdown": "",
            "questions": ["缺材料"], "evidence": []}, ensure_ascii=False))
        self.server.add_plan_proposal()
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_proposal_ambiguous", view["failure_reason"])
        self.assertEqual(self.server.resolve_calls, [])
        self.assertFalse(self.plan_files())

    def test_ready_answer_without_proposal_is_missing(self):
        """最终答复自称 ready 但没有产物 → plan_proposal_missing。"""
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", json.dumps({
            "status": "ready", "plan_markdown": "# 计划\n",
            "questions": [], "evidence": []}, ensure_ascii=False))
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_proposal_missing", view["failure_reason"])
        self.assertFalse(self.plan_files())


# ------------------------------------------------------------ 附件门


class TestAttachmentGates(PlusPlannerCase):
    def _with_image(self, *, sha256=None, write=True, media_type="image/png"):
        payload = b"\x89PNG\r\n\x1a\nbytes"
        repo = self.git_repo()
        self.new_task("taskattach00000000001", repo=repo)
        entry = self.material_entry(payload, sha256=sha256,
                                    media_type=media_type)
        info = self.make_input("taskattach00000000001", repo, files=[entry])
        if write:
            self.write_material_file(info, entry["path"], payload)
        else:
            # 材料目录存在、但清单登记的文件缺失（不可变材料理论不缺）。
            self.write_material_file(info, "files/placeholder.txt", b"x")
        self.save_plus_config()
        request_id = self.start_plus_plan("taskattach00000000001",
                                         info)["request_id"]
        return {"task_id": "taskattach00000000001", "repo": repo,
                "request_id": request_id, "info": info, "payload": payload}

    def test_hash_mismatch_blocks_before_send(self):
        fixture = self._with_image(sha256="a" * 64)
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("哈希不符", view["failure_reason"])
        self.assertEqual(self.server.created, None, "未通过材料门不得建会话")
        self.assertEqual(self.server.prompt_count, 0)
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        events = self.events_for(fixture["task_id"], run_id)
        self.assertEqual(self.terminal_reason(events),
                         "attachment_unsupported")

    def test_missing_image_file_blocks(self):
        fixture = self._with_image(write=False)
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertIn("缺失", view["failure_reason"])
        self.assertEqual(self.server.created, None)

    def test_agent_prompt_without_attachments_param_is_blocked(self):
        """后端未提供可选 attachments 参数 → 明确阻塞，绝不静默丢图。"""
        payload = b"\x89PNG\r\n\x1a\nbytes"
        repo = self.git_repo()
        self.new_task("tasknoattachparam000001", repo=repo)
        entry = self.material_entry(payload)
        info = self.make_input("tasknoattachparam000001", repo, files=[entry])
        self.write_material_file(info, entry["path"], payload)
        self.save_plus_config()
        request_id = self.start_plus_plan("tasknoattachparam000001",
                                         info)["request_id"]

        def legacy_prompt(client, session_id, content):
            return pi_desktop_backend.MCPClient.call_tool(
                client, "pi_agent_prompt",
                {"sessionId": session_id, "content": content})

        with mock.patch.object(runtime_client.RuntimeClient, "agent_prompt",
                               legacy_prompt), self.fast():
            self.run_round()
        view = self.task("tasknoattachparam000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("attachment_unsupported", view["failure_reason"])
        self.assertEqual(self.server.prompt_count, 0, "未发出任何 prompt")
        _run_id, job = self.job_for("tasknoattachparam000001", request_id)
        self.assertEqual(job["send_state"], "not_sent")

    def test_oversized_request_rejected_before_send(self):
        """请求体超 2 MiB → 发送前拒绝，不截断正文或图片。"""
        huge = "x" * (constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES + 1000)
        repo = self.git_repo()
        self.new_task("taskoversize000000001", repo=repo)
        info = self.make_planner_input(
            "taskoversize000000001",
            manifest_extra={
                "task": self.manifest_task_block("taskoversize000000001",
                                                 repo),
                "files": [],
                "supplement": huge,
            })
        self.save_plus_config()
        request_id = self.start_plus_plan("taskoversize000000001",
                                         info)["request_id"]
        with self.fast():
            self.run_round()
        view = self.task("taskoversize000000001")
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_request_too_large", view["failure_reason"])
        self.assertEqual(self.server.prompt_count, 0)
        _run_id, job = self.job_for("taskoversize000000001", request_id)
        self.assertEqual(job["send_state"], "not_sent")


# ------------------------------------------------------------ 转录/轮次门


class TestTranscriptGates(PlusPlannerCase):
    def _base(self, **server_tweaks):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 计划\n")
        self.server.add_plan_proposal()
        for key, value in server_tweaks.items():
            setattr(self.server, key, value)
        return fixture

    def test_truncated_transcript_blocks_publish(self):
        fixture = self._base(transcript_truncated=True)
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("transcript_incomplete", view["failure_reason"])
        self.assertEqual(self.server.resolve_calls, [],
                         "分页不完整不得读产物、不得发布")
        self.assertFalse(self.plan_files())

    def test_empty_transcript_blocks_publish(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_plan_proposal()
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertIn("transcript_incomplete", view["failure_reason"])
        self.assertFalse(self.plan_files())

    def test_turn_error_is_explicit_failure(self):
        fixture = self._base(turn_status="error",
                             turn_error_code="MODEL_UNAVAILABLE")
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("MODEL_UNAVAILABLE", view["failure_reason"])
        run_id, _job = self.job_for(fixture["task_id"], fixture["request_id"])
        events = self.events_for(fixture["task_id"], run_id)
        self.assertEqual(self.terminal_reason(events), "MODEL_UNAVAILABLE")

    def test_timeout_stops_turn_and_reports_timeout(self):
        fixture = self.plus_request()
        self.save_plus_config()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "codex_timeout_seconds", 1)
        finally:
            conn.close()
        self.server.turn_status = "running"
        self.server.is_running = True
        self.server.current_turn = TURN_ID
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(view["failure_code"], "planner_timeout")
        self.assertIn("timeout", view["failure_reason"])
        self.assertIn("pi_agent_stop", self.server.tool_names())
        self.assertFalse(self.plan_files())

    def test_prompt_response_lost_keeps_unknown(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.prompt_mode = "lost"
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("send_unknown", view["failure_reason"])
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        conn = db.connect(self.db_path)
        try:
            slot = conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?",
                                (job["pool_lease"]["slot_id"],)).fetchone()
            self.assertEqual(slot["state"], "leased")
            self.assertEqual(slot["owner_state"], "unknown")
        finally:
            conn.close()
        self.assertEqual(self.server.prompt_count, 1, "响应不明不得重发")
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["send_state"], "unknown")


# ------------------------------------------------------------ 恢复不重发


class TestPlanSnapshotWorktree(PlusPlannerCase):
    def snapshot_fixture(self):
        fixture = self.plus_request()
        sha = subprocess.run(
            ["git", "-C", str(fixture["repo"]), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        conn = db.connect(self.db_path)
        try:
            payload = db.request_payload(db.get_request(conn, fixture["request_id"]))
            payload["delivery_head_sha"] = sha
            with db.write_txn(conn):
                db.update_request(conn, fixture["request_id"], util.now_iso(),
                                  payload_json=payload)
        finally:
            conn.close()
        fixture["sha"] = sha
        from kanpilot import worktree_pool
        repo_key = worktree_pool.repository_identity(fixture["repo"])["repo_key"]
        fixture["worktree"] = self.worktrees_root.resolve() / "pool" / repo_key / "slot-1"
        return fixture

    def test_full_pool_keeps_same_request_queued_without_sending(self):
        from kanpilot import worktree_pool
        fixture = self.snapshot_fixture()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "worktree_pool_size", 1)
            worktree_pool.acquire(conn, fixture["repo"], stage="plan", holder_id="another-reader",
                                  sha=fixture["sha"])
        finally:
            conn.close()
        self.run_round()
        request = self.request_view(fixture["request_id"])
        self.assertEqual(request["status"], "queued")
        self.assertEqual(self.task(fixture["task_id"])["status"], "needs_plan")
        self.assertEqual(self.server.prompt_count, 0)
        self.assertIsNone(self.server.created)

    def test_resume_rejects_previous_generation_before_connecting(self):
        from kanpilot import worktree_pool
        fixture = self.snapshot_fixture()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": fixture["request_id"]})
        conn = db.connect(self.db_path)
        try:
            old = worktree_pool.acquire(conn, fixture["repo"], stage="plan", holder_id="old-reader", sha=fixture["sha"])
            worktree_pool.release(conn, old["slot_id"], old["holder_id"], old["lease_token"], owner_stopped=True)
            current = worktree_pool.acquire(conn, fixture["repo"], stage="plan", holder_id=claim["run_id"], sha=fixture["sha"])
            self.assertEqual(old["path"], current["path"])
            self.assertNotEqual(old["lease_token"], current["lease_token"])
        finally:
            conn.close()
        directory = self.root / "resume-generation"
        directory.mkdir()
        round_ = pi_desktop_planner._Round(self.db_path, fixture["request_id"], claim["run_id"],
            context=claim["context"], task=claim["context"]["task"], task_id=fixture["task_id"],
            run_dir=directory, timeout=1, heartbeat_every=1)
        with mock.patch.object(round_, "_connect") as connect, mock.patch.object(round_, "fail", return_value=True) as fail:
            self.assertTrue(round_._resume({"pool_lease": old, "run_id": "old-reader"}))
        connect.assert_not_called()
        self.assertEqual(fail.call_args.args[1], "plan_round_unverifiable")
        self.assertNotIn("pool_lease", round_.job)

    def assert_preparation_failed(self, fixture, code, detail):
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 1)
        self.assertIn(detail, job["detail"])
        self.assertEqual(self.terminal_reason(
            self.events_for(fixture["task_id"], run_id)), code)
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn(detail, view["failure_reason"])
        self.assertEqual(self.request_view(fixture["request_id"])["status"], "failed")
        self.assertIsNone(self.server.created)
        self.assertEqual(self.server.prompt_count, 0)
        self.assertEqual(self.server.calls, [], "工作树失败前不得访问 Plus 控制面")
        self.assertFalse(self.plan_files())
        self.assertEqual(self.git_status(fixture["repo"]), "")

    def test_worktree_invalid_keeps_reason_and_redacts_runtime_credentials(self):
        fixture = self.snapshot_fixture()
        error = git_delivery.GitDeliveryError(
            "worktree_invalid",
            f"快照身份不符：{TOKEN} https://user:private@github.com/org/repo")
        with mock.patch("kanpilot.worktree_pool.acquire",
                               side_effect=error):
            self.run_round()
        self.assert_preparation_failed(fixture, "worktree_invalid", "快照身份不符")
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        persisted = json.dumps({
            "job": job, "events": self.events_for(fixture["task_id"], run_id),
            "task": self.task(fixture["task_id"]),
            "request": self.request_view(fixture["request_id"]),
        })
        self.assertNotIn(TOKEN, persisted)
        self.assertNotIn("user:private", persisted)
        self.assertIn("redacted", persisted)

    def test_git_transient_keeps_specific_reason(self):
        fixture = self.snapshot_fixture()
        with mock.patch("kanpilot.worktree_pool.acquire",
                               side_effect=git_delivery.GitDeliveryError(
                                   "git_transient", "Git 操作暂时不可用")):
            self.run_round()
        self.assert_preparation_failed(fixture, "git_transient", "Git 操作暂时不可用")

    def test_unavailable_connection_does_not_replace_worktree_error(self):
        fixture = self.snapshot_fixture()
        self.write_connection(active=False)
        with mock.patch("kanpilot.worktree_pool.acquire",
                               side_effect=git_delivery.GitDeliveryError(
                                   "worktree_invalid", "快照工作树有源码改动")):
            self.run_round()
        self.assert_preparation_failed(fixture, "worktree_invalid", "快照工作树有源码改动")

    def test_manual_branch_validation_keeps_specific_reason(self):
        fixture = self.snapshot_fixture()
        branch = self.worktrees_root / fixture["task_id"] / "branch"
        branch.parent.mkdir(parents=True, exist_ok=True)
        branch_name = "kaneo/" + fixture["task_id"]
        self.assertTrue(branch.is_dir())
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_extra(conn, db.require_task(conn, fixture["task_id"]),
                             util.now_iso(), automation={"manual_takeover": True},
                             delivery={"mode": "manual", "branch": branch_name,
                                       "worktree_path": str(branch),
                                       "head_sha": fixture["sha"]})
                payload = db.request_payload(db.get_request(conn, fixture["request_id"]))
                payload.pop("workspace_version", None)  # Preserve legacy manual takeover coverage.
                payload.update(delivery_mode="manual", plan_cwd=str(branch))
                db.update_request(conn, fixture["request_id"], util.now_iso(),
                                  payload_json=payload)
        finally:
            conn.close()
        with mock.patch.object(git_delivery, "verify_branch_worktree",
                               side_effect=git_delivery.GitDeliveryError(
                                   "worktree_invalid", "任务工作树归属或分支不符")):
            self.run_round()
        self.assert_preparation_failed(fixture, "worktree_invalid", "任务工作树归属或分支不符")

    def test_foreign_snapshot_identity_blocks_plus_creation(self):
        fixture = self.snapshot_fixture()
        foreign = self.git_repo("Other")
        fixture["worktree"].parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(foreign), "worktree", "add", "--detach",
                        str(fixture["worktree"]), "HEAD"],
                       capture_output=True, check=True)
        self.run_round()
        self.assert_preparation_failed(fixture, "worktree_invalid", "新槽位路径已有未登记内容")
        self.assertEqual(self.git_status(foreign), "")

    def test_snapshot_tracked_changes_still_block_plus_creation(self):
        fixture = self.snapshot_fixture()
        fixture["worktree"].parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(fixture["repo"]), "worktree", "add", "--detach",
                        str(fixture["worktree"]), fixture["sha"]], capture_output=True, check=True)
        (fixture["worktree"] / "AGENTS.md").write_text("changed source\n", encoding="utf-8")
        self.run_round()
        self.assert_preparation_failed(fixture, "worktree_invalid", "新槽位路径已有未登记内容")
        self.assertEqual((fixture["worktree"] / "AGENTS.md").read_text(), "changed source\n")

    def test_released_snapshot_preserves_plus_plan_evidence_before_reuse(self):
        fixture = self.snapshot_fixture()
        original = "# 已有 Plus 计划\n\n保留这份规划证据。\n".encode("utf-8")
        real_prompt = runtime_client.RuntimeClient.agent_prompt
        def prompt(client, *args, **kwargs):
            artifact = fixture["worktree"] / ".pi" / "plan" / "previous-plan.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(original)
            return real_prompt(client, *args, **kwargs)
        self.server.add_message("user", "kanpilot plan prompt")
        self.server.add_message("assistant", "# 新计划\n")
        self.server.add_plan_proposal(markdown="# 新计划\n")
        with self.fast(), mock.patch.object(runtime_client.RuntimeClient, "agent_prompt", prompt):
            self.run_round()
        self.assertEqual(self.task(fixture["task_id"])["status"], "ready")
        self.assertEqual(self.server.created["projectPath"], str(fixture["worktree"]))
        self.assertEqual(self.server.prompt_count, 1)
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        persisted = Path(job["material_dir"]).parents[1]
        self.assertEqual((persisted / "plus-plan-evidence" / "previous-plan.md").read_bytes(), original)
        self.assertFalse((fixture["worktree"] / ".pi" / "plan" / "previous-plan.md").exists())
        self.assertTrue(job["pool_released"])
        self.assertEqual(self.git_status(fixture["repo"]), "")


class TestUnexpectedPlannerExit(PlusPlannerCase):
    def test_git_delivery_error_keeps_code_and_sanitized_message(self):
        fixture = self.plus_request()
        error = git_delivery.GitDeliveryError(
            "branch_exists", "分支已存在：https://token@host/private token=secret-token-value")
        with mock.patch.object(pi_desktop_planner._Round, "_prepare_materials",
                               side_effect=error):
            self.run_round()
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        conn = db.connect(self.db_path)
        try:
            details = [util.jload(row["detail_json"]) for row in conn.execute(
                "SELECT detail_json FROM events WHERE task_id=? AND request_id=?",
                (fixture["task_id"], fixture["request_id"]))]
            evidence = util.jload(conn.execute(
                "SELECT evidence_json FROM attempts WHERE request_id=? AND run_id=?",
                (fixture["request_id"], run_id)).fetchone()["evidence_json"])
        finally:
            conn.close()
        self.assertEqual(evidence["git_delivery_code"], "branch_exists")
        self.assertIn("分支已存在", evidence["git_delivery_message"])
        reported = next(detail for detail in details if detail.get("outcome") == "failed")
        self.assertIn("branch_exists", reported["note"])
        self.assertIn("分支已存在", reported["note"])
        persisted = json.dumps({"job": job, "details": details, "evidence": evidence,
                                "events": self.events_for(fixture["task_id"], run_id)})
        self.assertNotIn("https://", persisted)
        self.assertNotIn("token@host", persisted)
        self.assertNotIn("secret-token-value", persisted)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.server.prompt_count, 0)

    def test_exception_before_session_creation_finishes_claim(self):
        fixture = self.plus_request()
        raw_detail = f"token={TOKEN}; raw request payload must not be persisted"
        with mock.patch.object(pi_desktop_planner._Round, "_prepare_materials",
                               side_effect=RuntimeError(raw_detail)):
            self.run_round()

        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 1)
        self.assertTrue(job["ended_at"])
        self.assertIn("RuntimeError", job["detail"])
        self.assertNotIn(TOKEN, json.dumps(job))
        self.assertNotIn("raw request payload", json.dumps(job))
        events = self.events_for(fixture["task_id"], run_id)
        self.assertEqual(self.terminal_reason(events), "planner_internal_error")
        self.assertNotIn(raw_detail, json.dumps(events))
        request = self.request_view(fixture["request_id"])
        self.assertEqual(request["status"], "failed")
        self.assertNotIn(raw_detail, json.dumps(request))
        self.assertEqual(self.task(fixture["task_id"])["status"], "needs_plan")
        self.assertIsNone(self.server.created)
        self.assertEqual(self.server.prompt_count, 0)

    def test_exception_after_replacement_claim_keeps_new_round(self):
        fixture = self.plus_request()
        new_run = "replacement-plan-run"
        new_dir = self.run_dir(fixture["task_id"], new_run)
        new_dir.mkdir(parents=True)
        new_job = new_dir / dsh_events.JOB_NAME
        sentinel = b'{"run_id":"replacement-plan-run","status":"running"}'
        new_job.write_bytes(sentinel)

        def replace_then_crash():
            conn = db.connect(self.db_path)
            try:
                with db.write_txn(conn):
                    db.update_request(conn, fixture["request_id"], util.now_iso(),
                                      run_id=new_run)
            finally:
                conn.close()
            raise RuntimeError("old process crashed")

        with mock.patch.object(pi_desktop_planner._Round, "_prepare_materials",
                               side_effect=replace_then_crash):
            self.run_round()

        old_run, old_job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(old_job["status"], "running")
        self.assertFalse((self.run_dir(fixture["task_id"], old_run)
                          / dsh_events.EVENT_LOG_NAME).exists())
        request = self.request_view(fixture["request_id"])
        self.assertEqual(request["run_id"], new_run)
        self.assertEqual(request["status"], "claimed")
        self.assertEqual(self.task(fixture["task_id"])["status"], "planning")
        self.assertEqual(new_job.read_bytes(), sentinel)
        self.assertEqual(self.server.prompt_count, 0)

    def test_exception_after_lease_expiry_does_not_report(self):
        fixture = self.plus_request()

        def expire_then_crash():
            conn = db.connect(self.db_path)
            try:
                with db.write_txn(conn):
                    db.update_request(conn, fixture["request_id"], util.now_iso(),
                                      lease_expires_at=util.plus_seconds(-1))
            finally:
                conn.close()
            raise RuntimeError("expired process crashed")

        with mock.patch.object(pi_desktop_planner._Round, "_prepare_materials",
                               side_effect=expire_then_crash):
            self.run_round()

        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["status"], "running")
        self.assertFalse((self.run_dir(fixture["task_id"], run_id)
                          / dsh_events.EVENT_LOG_NAME).exists())
        conn = db.connect(self.db_path)
        try:
            request = db.get_request(conn, fixture["request_id"])
            self.assertEqual(request["status"], "claimed")
            self.assertIsNone(request["result_json"])
            self.assertEqual(db.get_task(conn, fixture["task_id"])["status"],
                             "planning")
        finally:
            conn.close()

    def test_exception_after_sending_keeps_proof_without_resending(self):
        fixture = self.plus_request()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 计划\n")
        with self.fast(), mock.patch.object(
                pi_desktop_planner._Round, "_observe_until_settled",
                side_effect=RuntimeError("observer crashed")):
            self.run_round()
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["session_id"], SESSION_ID)
        self.assertEqual(job["turn_id"], TURN_ID)
        self.assertEqual(self.terminal_reason(
            self.events_for(fixture["task_id"], run_id)), "planner_internal_error")
        self.assertEqual(self.server.prompt_count, 1)
        self.assertFalse(planner_runner.run_one(self.db_path))
        self.assertEqual(self.server.prompt_count, 1)


class TestResumeDoesNotResend(PlusPlannerCase):
    def _previous_job(self, task_id, request_id, *, turn_id=TURN_ID,
                      send_state="sent"):
        """手工造「上一轮」现场：claim + job.json（不跑模型）。"""
        claim = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request_id,
        })
        self.assertTrue(claim["claimed"], f"claim 失败：{claim}")
        run_id = claim["run_id"]
        run_dir = self.run_dir(task_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.worktrees_root / task_id / run_id
        worktree.mkdir(parents=True, exist_ok=True)
        job = {
            "task_id": task_id, "run_id": run_id, "request_id": request_id,
            "backend": "pi_desktop", "session_id": SESSION_ID,
            "session_title": "kanpilot plan", "turn_id": turn_id,
            "worktree_path": str(worktree),
            "config": self.runtime_block(), "pid": os.getpid(),
            "status": "running", "started_at": util.now_iso(),
            "ended_at": None, "exit_code": None, "detail": None,
            "send_state": send_state,
        }
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(job, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        return run_id

    def _release(self, task_id):
        """把上一轮释放回队列（失联释放 → 用户重新提交，同材料同后端）。"""
        self.call("resolve", {"task_id": task_id, "action": "replan",
                              "resolution": "重试"})
        return self.call("start-plans", {"items": [{
            "input_id": self._input_id, "input_sha256": self._input_sha,
            "planner_backend": "pi_desktop"}]})

    def _fixture_with_previous(self, *, turn_id=TURN_ID, send_state="sent"):
        fixture = self.plus_request()
        self.save_plus_config()
        self._input_id = fixture["info"]["input_id"]
        self._input_sha = fixture["info"]["input_sha256"]
        prev_run = self._previous_job(fixture["task_id"],
                                     fixture["request_id"], turn_id=turn_id,
                                     send_state=send_state)
        # 让任务回到 needs_plan 以便重新提交同一材料（同 input_sha → 新 attempt）
        self.set_task_status(fixture["task_id"], "needs_plan")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE requests SET status='queued', claimed_by=NULL, "
                    "lease_expires_at=NULL WHERE request_id=?",
                    (fixture["request_id"],))
        finally:
            conn.close()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 计划\n")
        self.server.add_plan_proposal()
        return fixture, prev_run

    def test_resumed_turn_missing_policy_is_blocked_without_prompt(self):
        fixture, previous = self._fixture_with_previous()
        self.server.interaction_policy_version = None
        with self.fast():
            self.run_round()
        self.assertEqual(self.server.prompt_count, 0)
        self.assertIsNone(self.server.created)
        self.assertIsNone(self.server.configured)
        self.assertEqual(self.server.resolve_calls, [])
        self.assertIn('硬交互策略', self.task(fixture['task_id'])['failure_reason'])

    def test_resume_observes_existing_turn_without_resend(self):
        fixture, prev_run = self._fixture_with_previous()
        with self.fast():
            self.run_round()

        # 不重复建会话 / 不发 prompt
        self.assertIsNone(self.server.created, "恢复轮不得新建会话")
        self.assertEqual(self.server.prompt_count, 0, "恢复轮不得重发 prompt")
        # 旧 job 无 project scope，只允许历史只读读取；reject 属 Plus 写操作，交人工。
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("plan_reject_failed", view["failure_reason"])
        _run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["resume_from"]["run_id"], prev_run)
        self.assertEqual(job["session_id"], SESSION_ID)
        self.assertEqual(job["turn_id"], TURN_ID)
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["send_state_proven_by"], "persistent_turn")
        self.assertEqual(job["status"], "failed")
        # 旧现场原样保留
        old = json.loads((self.run_dir(fixture["task_id"], prev_run)
                          / dsh_events.JOB_NAME).read_text(encoding="utf-8"))
        self.assertEqual(old["turn_id"], TURN_ID)
        self.assertEqual(old["send_state"], "sent")

    def test_legacy_running_ask_resumes_without_reconfigure(self):
        fixture, prev_run = self._fixture_with_previous()
        previous_path = self.run_dir(fixture["task_id"], prev_run) / dsh_events.JOB_NAME
        previous = json.loads(previous_path.read_text())
        previous["config"]["permission_mode"] = "ask"
        previous_path.write_text(json.dumps(previous))
        original_bytes = previous_path.read_bytes()
        conn = db.connect(self.db_path)
        try:
            payload = db.request_payload(db.get_request(conn, fixture["request_id"]))
            payload["runtime_config"]["permission_mode"] = "ask"
            with db.write_txn(conn):
                db.update_request(conn, fixture["request_id"], util.now_iso(), payload_json=payload)
        finally:
            conn.close()
        with self.fast():
            self.run_round()
        self.assertIsNone(self.server.created)
        self.assertIsNone(self.server.configured)
        self.assertEqual(self.server.prompt_count, 0)
        _, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["config"]["permission_mode"], "ask")
        self.assertEqual(previous_path.read_bytes(), original_bytes)

    def test_unexpected_resume_exit_does_not_resend_existing_turn(self):
        fixture, prev_run = self._fixture_with_previous()
        with self.fast(), mock.patch.object(
                pi_desktop_planner._Round, "_observe_until_settled",
                side_effect=RuntimeError("resume observer crashed")):
            self.run_round()
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertEqual(job["resume_from"]["run_id"], prev_run)
        self.assertEqual(job["send_state"], "sent")
        self.assertEqual(job["turn_id"], TURN_ID)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.terminal_reason(
            self.events_for(fixture["task_id"], run_id)), "planner_internal_error")
        self.assertEqual(self.request_view(fixture["request_id"])["status"],
                         "failed")
        self.assertIsNone(self.server.created)
        self.assertEqual(self.server.prompt_count, 0)

    def test_resume_without_turn_and_unknown_blocks(self):
        fixture, _prev = self._fixture_with_previous(
            turn_id=None, send_state="unknown")
        with self.fast():
            self.run_round()
        view = self.task(fixture["task_id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("send_unknown", view["failure_reason"])
        self.assertIsNone(self.server.created)
        self.assertEqual(self.server.prompt_count, 0)

    def test_resume_connection_unavailable_defers(self):
        fixture, prev_run = self._fixture_with_previous()
        self.connection_file.unlink()
        with self.fast():
            self.run_round()
        # 不改既有轮次终态、不汇报：请求仍在 claim 状态等待人工/下一次恢复
        self.assertEqual(self.request_view(fixture["request_id"])["status"],
                         "claimed")
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertNotEqual(run_id, prev_run)
        self.assertEqual(job["status"], "running")
        self.assertIn("connection_unavailable", job["resume_detail"])
        events = self.events_for(fixture["task_id"], run_id)
        self.assertIsNone(dsh_events.terminal_event(events),
                          "defer 不得写 terminal")
        self.assertIsNone(self.server.created)


# ------------------------------------------------------------ review_fix


class TestPublishedModelValidation(unittest.TestCase):
    def test_missing_review_report_preserves_validation_error(self):
        runtime = {"model_id": "test-model", "thinking_level": "medium"}
        payload = {"planner_backend": "pi_desktop", "runtime_config": runtime}
        context = {"task_id": TASK_ID, "payload": payload,
                   "task": {"plan_kind": "review_fix"}}
        manifest = {"task": {"project_name": "Feedback", "repository": "/repo"}}
        pair, source, note = pi_desktop_planner._published_model_pair(
            context, manifest, util.now_iso(), None, payload, runtime, "review_fix")
        self.assertEqual(pair, {"planner_model": "test-model", "reasoning_effort": "medium"})
        self.assertEqual(source, "runtime_config_snapshot")
        self.assertIn("review_report_path", note)
        self.assertIn("review_report_sha256", note)


class TestReviewFixPlusPath(PlusPlannerCase):
    def register_review_source(self, source_task_id, repo, review_key):
        self.register_task(source_task_id, repo)
        delivery = git_delivery.prepare_local_branch_worktree(repo, source_task_id,
                                                              worktrees_root=self.worktrees_root)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                source = db.require_task(conn, source_task_id)
                core.update_delivery(conn, source, util.now_iso(), **delivery)
                db.set_extra(conn, db.require_task(conn, source_task_id), util.now_iso(),
                             review_history=[{"review_key": review_key, "head_sha": delivery["head_sha"]}])
        finally:
            conn.close()

    def test_review_fix_publishes_with_review_metadata(self):
        self._publish_review_fix(legacy_missing_report=False)

    def test_legacy_review_fix_imports_registered_parent_report(self):
        self._publish_review_fix(legacy_missing_report=True)

    def _publish_review_fix(self, *, legacy_missing_report):
        task_id = "taskreviewfix0000001"
        source_task_id = "tasksourcereview00001"
        repo = self.git_repo()
        self.register_task(task_id, repo, plan_kind="review_fix",
                           title="修复计划")
        report = self.reports_root / source_task_id / "run1.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# Review 报告\n\n- changes_requested\n",
                          encoding="utf-8")
        review_key = util.sha256_text("review-cycle-1")
        self.register_review_source(source_task_id, repo, review_key)
        self.set_task_status(
            task_id, "needs_plan", plan_kind="review_fix",
            review_key=review_key, review_source_task_id=source_task_id,
            review_report_path=str(report),
            review_report_sha256=util.sha256_file(report),
        )
        if legacy_missing_report:
            conn = db.connect(self.db_path)
            try:
                with db.write_txn(conn):
                    db.update_task(conn, task_id, util.now_iso(),
                                   review_report_path=None, review_report_sha256=None)
                    db.update_task(conn, source_task_id, util.now_iso(),
                                   review_key=review_key, fix_task_id=task_id,
                                   review_report_path=str(report),
                                   review_report_sha256=util.sha256_file(report))
                    source = db.require_task(conn, source_task_id)
                    history = db.task_extra(source)["review_history"]
                    history[-1].update(
                        fix_task_id=task_id, conclusion="changes_requested",
                        report_path=str(report), report_sha256=util.sha256_file(report))
                    db.set_extra(conn, source, util.now_iso(),
                                 review_conclusion="changes_requested", review_history=history)
            finally:
                conn.close()
        info = self.make_input(task_id, repo, plan_kind="review_fix")
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 修复计划\n")
        self.server.add_plan_proposal(
            markdown="# 修复计划\n\n1. 修复 review 指出的问题\n")
        request_id = self.start_plus_plan(task_id, info)["request_id"]
        with self.fast():
            self.run_round()

        view = self.task(task_id)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(self.request_view(request_id)["status"], "succeeded")
        text = Path(view["plan_path"]).read_text(encoding="utf-8")
        self.assertIn("review_fix", text)
        self.assertIn(review_key, text)
        self.assertIn(source_task_id, text)
        self.assertIn(str(report), text)
        self.assertIn(util.sha256_file(report), text)
        self.assertIn("修复 review 指出的问题", text)
        _run_id, job = self.job_for(task_id, request_id)
        # 发布模型对必须取 Plus 快照真值（§5.24.3）：核心的
        # planfile.expected_model_snapshot 已按 runtime_config 校验，因此这里
        # 不允许再回落 §6 历史常量——否则计划文件会声称一个从未运行的 Codex 模型。
        self.assertEqual(job["model_pair_source"], "runtime_config_snapshot")
        self.assertEqual(job["model_pair"]["planner_model"], "test-model")
        self.assertEqual(job["model_pair"]["reasoning_effort"], "medium")
        self.assertIn(f'planner_model: "{job["model_pair"]["planner_model"]}"',
                      text)
        self.assertEqual(job["plan_proposal"]["status"], "rejected")

    def test_review_fix_report_hash_mismatch_blocks_publish(self):
        """review_key 链核验失败（报告哈希不符）→ 不发布。"""
        task_id = "taskreviewbad00000001"
        source_task_id = "tasksourcereview00002"
        repo = self.git_repo()
        self.register_task(task_id, repo, plan_kind="review_fix",
                           title="修复计划")
        report = self.reports_root / source_task_id / "run1.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# Review 报告\n", encoding="utf-8")
        self.register_review_source(source_task_id, repo, "k" * 64)
        self.set_task_status(
            task_id, "needs_plan", plan_kind="review_fix",
            review_key="k" * 64, review_source_task_id=source_task_id,
            review_report_path=str(report),
            review_report_sha256="b" * 64,      # 与真实文件不符
        )
        info = self.make_input(task_id, repo, plan_kind="review_fix")
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_message("assistant", "# 修复计划\n")
        self.server.add_plan_proposal(markdown="# 修复计划\n")
        request_id = self.start_plus_plan(task_id, info)["request_id"]
        with self.fast():
            self.run_round()
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_plan")
        self.assertIn("发布被拒", view["failure_reason"])
        self.assertEqual(self.request_view(request_id)["status"], "failed")
        # 产物已按 §5.24.3-4 reject 关闭（拒绝的是本地产物，不是契约本身）
        self.assertEqual(self.server.plans, [])


if __name__ == "__main__":
    unittest.main()


class TestHardInteractionPolicy(PlusPlannerCase):
    def test_missing_policy_is_rejected_before_prompt(self):
        self.server.interaction_policy_version = None
        self.plus_request()
        self.save_plus_config()
        self.server.add_message('user', 'prompt')
        self.server.add_plan_proposal()
        with self.fast():
            self.run_round()
        self.assertEqual(self.server.prompt_count, 0)

    def test_interaction_required_overrides_proposal(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.turn_status = 'error'
        self.server.turn_error_code = 'INTERACTION_REQUIRED'
        self.server.turn_error_details = {
            'kind': 'interaction_required', 'toolName': 'asktool',
            'toolCallId': 'ask-1', 'questions': [{
                'question': '目标版本？', 'options': ['版本一', {'label': '版本二', 'description': '新版本'}]}]}
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name in ('pi_session_create', 'pi_session_configure', 'pi_session_get'):
                body = result['structuredContent']
                body.setdefault('session', {'id': SESSION_ID}).update(
                    interactionMode='unattended', interactionPolicyVersion=1)
            if name == 'pi_turn_get':
                result['structuredContent']['errorDetails'] = self.server.turn_error_details
            return result
        self.server.tool_call = call
        self.server.add_message('user', 'prompt')
        self.server.add_plan_proposal()
        with self.fast():
            self.run_round()
        view = self.task(fixture['task_id'])
        self.assertEqual(view['status'], 'needs_user')
        self.assertEqual(view['plan_clarification']['questions'][0]['title'], '目标版本？')
        self.assertEqual(self.server.resolve_calls, [])
        self.assertFalse(self.plan_files())

    def policy_failure(self, tool, change):
        fixture = self.plus_request()
        self.save_plus_config()
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name == tool:
                change(result['structuredContent']['session'])
            return result
        self.server.tool_call = call
        self.server.add_message('user', 'prompt')
        self.server.add_plan_proposal()
        with self.fast():
            self.run_round()
        self.assertEqual(self.server.prompt_count, 0)
        self.assertEqual(self.server.resolve_calls, [])
        self.assertIn('硬交互策略', self.task(fixture['task_id'])['failure_reason'])

    def test_create_receipt_unsupported_version(self):
        self.policy_failure('pi_session_create', lambda session: session.update(interactionPolicyVersion=2))

    def test_configure_receipt_mode_mismatch(self):
        self.policy_failure('pi_session_configure', lambda session: session.update(interactionMode='interactive'))

    def test_configure_receipt_invalid_boolean_version(self):
        self.policy_failure('pi_session_configure', lambda session: session.update(interactionPolicyVersion=True))

    def test_readback_missing_policy(self):
        self.policy_failure('pi_session_get', lambda session: session.pop('interactionMode'))

    def test_readback_mode_mismatch(self):
        self.policy_failure('pi_session_get', lambda session: session.update(interactionMode='interactive'))

    def test_host_rejects_unattended_without_prompt(self):
        fixture = self.plus_request()
        self.save_plus_config()
        actual = self.server.tool_call
        def call(name, args):
            if name == 'pi_session_create':
                return {'isError': True, 'structuredContent': {'error': {'code': 'INVALID_ARGUMENT', 'message': 'unknown interactionMode'}}}
            return actual(name, args)
        self.server.tool_call = call
        with self.fast():
            self.run_round()
        self.assertEqual(self.server.prompt_count, 0)
        self.assertEqual(self.task(fixture['task_id'])['status'], 'needs_plan')

    def intercepted_scene(self, questions, *, empty_transcript=False, **extra):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.turn_status = 'error'
        self.server.turn_error_code = 'INTERACTION_REQUIRED'
        details = {'kind': 'interaction_required', 'toolName': 'asktool',
                   'toolCallId': 'ask-1', 'questions': questions, **extra}
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name == 'pi_turn_get':
                result['structuredContent']['errorDetails'] = details
            return result
        self.server.tool_call = call
        # A final pass-like answer and pending proposal must never take precedence.
        self.server.add_message('user', 'prompt')
        self.server.add_message('assistant', '{"status":"ready","plan_markdown":"# Plan","questions":[],"evidence":[]}')
        if empty_transcript:
            self.server.messages = []
        self.server.add_plan_proposal()
        with self.fast():
            self.run_round()
        return fixture

    def test_invalid_arguments_keep_evidence_without_inventing_questions(self):
        fixture = self.intercepted_scene([], invalidArguments=True)
        view = self.task(fixture['task_id'])
        self.assertEqual(view['status'], 'needs_plan')
        self.assertIn('问题无效', view['failure_reason'])
        self.assertFalse(view.get('plan_clarification'))
        self.assertEqual(self.server.resolve_calls, [])
        run_id, job = self.job_for(fixture['task_id'], fixture['request_id'])
        self.assertTrue(Path(job['interaction_required_path']).exists())
        self.assertFalse(self.plan_files())

    def test_multiselect_can_be_answered_and_preserves_all_choices(self):
        questions = [{'question': '包含哪些版本？', 'options': ['版本一', {'label': '版本二', 'description': '新版本'}], 'multiSelect': True}]
        fixture = self.intercepted_scene(questions)
        # The legacy hand-built fixture stores its manifest only in SQLite.
        # Answer submission also verifies the source manifest on disk.
        with db.connect(self.db_path) as conn:
            manifest = db.planner_input_manifest(db.get_planner_input(conn, fixture['info']['input_id']))
        (self.inputs_root / fixture['info']['input_id']).mkdir(parents=True, exist_ok=True)
        (self.inputs_root / fixture['info']['input_id'] / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        view = self.task(fixture['task_id'])
        clarification = view['plan_clarification']
        question = clarification['questions'][0]
        self.assertEqual(question['options'], [])
        self.assertIn('版本二：新版本', question['description'])
        answers = [{'question_id': question['id'], 'text': '版本一、版本二'}]
        result = self.call('submit-plan-answers', {'task_id': fixture['task_id'],
            'clarification_id': clarification['id'], 'operation_id': 'multi-answer', 'answers': answers})
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, result['request_id']))
        self.assertEqual(payload['manifest']['plan_clarification']['answers'], answers)
        self.assertEqual(payload['manifest']['plan_clarification']['questions'], clarification['questions'])

    def test_completed_duplicate_run_does_not_resend_or_publish(self):
        fixture = self.intercepted_scene([{'question': '选择？', 'options': ['A', 'B']}])
        before = self.server.prompt_count
        with self.fast():
            self.assertFalse(planner_runner.run_one(self.db_path))
        self.assertEqual(self.server.prompt_count, before)
        self.assertEqual(self.server.resolve_calls, [])
        self.assertEqual(self.task(fixture['task_id'])['status'], 'needs_user')

    def test_interception_from_foreign_turn_is_not_settled(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.turn_status = 'error'
        self.server.turn_error_code = 'INTERACTION_REQUIRED'
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name == 'pi_turn_get':
                result['structuredContent'].update(turnId='foreign-turn', errorDetails={
                    'kind': 'interaction_required', 'toolName': 'asktool', 'toolCallId': 'ask-1',
                    'questions': [{'question': '其他任务的问题', 'options': ['A', 'B']}]})
            return result
        self.server.tool_call = call
        self.server.add_message('user', 'prompt')
        with self.fast():
            self.run_round()
        self.assertEqual(self.task(fixture['task_id'])['status'], 'needs_plan')
        self.assertFalse(self.task(fixture['task_id']).get('plan_clarification'))
        self.assertEqual(self.server.resolve_calls, [])

    def test_interception_with_no_assistant_text_still_saves_questions(self):
        fixture = self.intercepted_scene([{'question': '选择？', 'options': ['A', 'B']}], empty_transcript=True)
        self.assertEqual(self.task(fixture['task_id'])['status'], 'needs_user')
        self.assertEqual(self.task(fixture['task_id'])['plan_clarification']['questions'][0]['title'], '选择？')
        self.assertEqual(self.server.resolve_calls, [])

    def test_atomic_host_policy_rejection_never_recovers_an_old_turn(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message('user', 'old prompt')
        self.server.add_plan_proposal()
        actual = self.server.tool_call
        def call(name, args):
            if name == 'pi_agent_prompt':
                self.assertEqual(args['expectedInteractionMode'], 'unattended')
                return {'isError': True, 'structuredContent': {'error': {
                    'code': 'INTERACTION_POLICY_MISMATCH', 'message': 'policy changed before admission'}}}
            return actual(name, args)
        self.server.tool_call = call
        with self.fast():
            self.run_round()
        run_id, job = self.job_for(fixture['task_id'], fixture['request_id'])
        self.assertEqual(job['send_state'], 'not_sent')
        self.assertFalse(job.get('turn_id'))
        self.assertEqual(self.server.prompt_count, 0)
        self.assertEqual(self.server.resolve_calls, [])
        self.assertFalse(self.plan_files())


class TestPlanPoolTerminationProof(PlusPlannerCase):
    def test_raw_terminal_without_turn_identity_never_releases_slot(self):
        fixture = self.plus_request()
        self.save_plus_config()
        self.server.add_message("user", "prompt")
        self.server.add_plan_proposal()
        actual = self.server.tool_call
        def call(name, args):
            result = actual(name, args)
            if name == "pi_turn_get":
                result["structuredContent"].pop("turnId", None)
            return result
        self.server.tool_call = call
        with self.fast():
            self.run_round()
        run_id, job = self.job_for(fixture["task_id"], fixture["request_id"])
        self.assertIsNot(job.get("owner_stopped"), True)
        with db.connect(self.db_path) as conn:
            slot = conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (job["pool_lease"]["slot_id"],)).fetchone()
            self.assertEqual(slot["state"], "leased")
            self.assertEqual(slot["owner_state"], "unknown")
        self.assertFalse(self.plan_files())
