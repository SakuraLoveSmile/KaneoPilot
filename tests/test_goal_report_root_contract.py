"""Goal 报告完成判定与派发命名的根级契约（protocol §5.23.10，v3 T5/T6）。

本文件是 KaneoPilot 仓库根层的**契约级**集成核对：经真实 CLI（subprocess +
JSON 进出 + 退出码）验证新命令已注册、payload 校验、错误信封与分态语义，
并用隔离 fake Plus MCP（自写 http.server 线程）覆盖 T6 命名链路。

与 `core/tests/test_goal_report_contract.py` 的分工：那份用进程内接缝覆盖
完整分支矩阵（身份核对、报告底线、幂等、硬闸、worker 宽限）；本文件只做
根层「命令已接通 + 契约形状 + 隔离环境不越界」的端到端核对。

**隔离与真实边界**：
- 全程无真实 PI-Desktop-Plus、无真实模型调用；fake MCP 只实现只读报告工具
  与 `pi_session_create` 的命名契约。
- fake AI 只证明核心闸门（不冒充真实模型判断）：本文件不引入任何 AI 调用，
  所有「AI 判定」都是显式传入的 JSON，测试只断言**核心**接受/拒绝。
- 历史轮次（`goal_approval=null`）保持原状态：本文件断言只读命令不改写。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

try:
    from conftest import EXECUTORS, PROJECTS, tmp_env  # noqa: F401
except ImportError:  # unittest discover 包上下文
    from tests.conftest import EXECUTORS, PROJECTS, tmp_env  # noqa: F401

FEEDBACK_PROJECT_ID = PROJECTS["Feedback"]
REPORT_SHA = "b" * 64
PLAN_SHA = "a" * 64
CONTRACT_HASH = "c" * 64
SESSION_ID = "plus-goal-session-1"
EXECUTION_ID = "plus-exec-1"
PROPOSAL_ID = "plus-prop-1"
PLAN_TURN_ID = "plus-turn-plan"
EXEC_TURN_ID = "plus-turn-exec"


class _FakePlusHandler(http.server.BaseHTTPRequestHandler):
    """fake Plus 本机 MCP 控制面：initialize / tools/list / tools/call。"""

    def log_message(self, *args):  # 静默
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server 约定
        srv = self.server.owner
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
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
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {"protocolVersion": "2025-06-18",
                           "capabilities": {},
                           "serverInfo": {"name": "fake-plus",
                                          "version": "0.0.1"}},
            })
            return
        if method and method.startswith("notifications/"):
            self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                             "result": {}})
            return
        if method == "tools/list":
            tools = []
            for name in srv.tools:
                schema = {"type": "object", "properties": {}}
                if name == "pi_session_create":
                    properties = {"projectPath": {"type": "string"},
                                  "title": {"type": "string"}}
                    if srv.project_name_field:
                        properties["projectName"] = {"type": "string"}
                    schema = {"type": "object", "properties": properties}
                tools.append({"name": name, "inputSchema": schema})
            self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                             "result": {"tools": tools}})
            return
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            srv.calls.append((name, args))
            self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                             "result": srv.tool_call(name, args)})
            return
        self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32601,
                                   "message": "method not found"}})


class FakePlus:
    """fake Plus（只读报告工具 + 命名契约；绝不调用模型）。"""

    def __init__(self, tools=None, project_name_field=True):
        self.token = "fake-plus-token-0123456789abcdef"
        self.tools = list(tools or (
            "pi_turn_get", "pi_session_create", "pi_session_configure",
            "pi_agent_prompt", "pi_agent_status", "pi_session_get",
            "pi_agent_stop", "pi_agent_abort", "pi_desktop_invoke",
            "pi_plans_pending", "pi_plans_resolve",
            "pi_goal_report_list", "pi_goal_report_get",
        ))
        self.project_name_field = project_name_field
        self.calls: list = []
        self.report = self.default_report()
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakePlusHandler
        )
        self.httpd.owner = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @staticmethod
    def default_report(**overrides):
        report = {
            "reportId": "plus-report-1",
            "sessionId": SESSION_ID,
            "executionId": EXECUTION_ID,
            "proposalId": PROPOSAL_ID,
            "turnId": EXEC_TURN_ID,
            "contractHash": CONTRACT_HASH,
            "status": "ready",
            "integrity": {"kind": "structured"},
            "execution": {"status": "completed"},
            "verdict": "met",
            "criteria": [{"id": "c1", "text": "验收一"}],
            "checks": [{"id": "k1", "text": "测试", "status": "passed"}],
            "evidences": [{"id": "e1", "kind": "log"}],
        }
        report.update(overrides)
        return report

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
        if name == "pi_goal_report_list":
            return {"structuredContent": {"reports": [{
                "reportId": self.report.get("reportId"),
                "proposalId": self.report.get("proposalId"),
                "turnId": self.report.get("turnId"),
                "executionId": self.report.get("executionId"),
            }]}}
        if name == "pi_goal_report_get":
            if self.report is None:
                return {"isError": True, "structuredContent": {"error": {
                    "code": "NOT_FOUND", "message": "report not found"}}}
            payload = dict(self.report)
            payload["report_sha256"] = REPORT_SHA
            return {"structuredContent": payload}
        if name == "pi_session_create":
            return {"structuredContent": {"session": {
                "id": SESSION_ID, "title": args.get("title"),
                "projectPath": args.get("projectPath"),
                "projectName": args.get("projectName"),
            }}}
        if name == "pi_desktop_invoke":
            operation = args.get("operation")
            if operation == "providers/list":
                return {"structuredContent": {"operation": operation,
                                              "result": {"providers": [
                                                  {"id": "p1",
                                                   "disabled": False}]}}}
            if operation == "providers/listModels":
                return {"structuredContent": {"operation": operation,
                                              "result": {
                                                  "models": [{"id": "m1",
                                                              "modelId": "m1"}],
                                                  "source": "cache"}}}
            return {"structuredContent": {"error": {
                "code": "NOT_FOUND", "message": operation}}}
        if name == "pi_plans_pending":
            return {"structuredContent": {"plans": []}}
        return {"structuredContent": {"ok": True}}


@pytest.fixture
def fake_plus():
    server = FakePlus().start()
    yield server
    server.stop()


def write_connection(tmp_root: Path, server: FakePlus) -> Path:
    """mcp-control.json（0600，owner-only）。"""
    path = tmp_root / "pi-desktop" / "mcp-control.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "active": True, "url": server.url, "token": server.token,
        "pid": os.getpid(), "startedAt": "2026-09-27T00:00:00+00:00",
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def seed_goal_round(tmp_env, connection_file, *, status="running",
                    goal_approval=True, run_id="rungoalroot0001",
                    backend="pi_desktop", plan_sha256=PLAN_SHA,
                    title="根层契约任务"):
    """登记任务 + 轮次 + job.json/events.jsonl（隔离运行记录）。"""
    import sys

    import sqlite3

    # 隔离 DB 的 schema 由核心的 db.connect 建立（tmp_env 只给路径）。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    from kanpilot import db as core_db

    core_db.connect(tmp_env.db).close()
    task_id = "taskgoalroot00000001"
    runs_root = tmp_env.app_support / "runs"
    run_dir = runs_root / task_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    now = "2026-09-27T00:00:00.000000+00:00"
    conn = sqlite3.connect(str(tmp_env.db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tasks(task_id, project_id, project_name, "
            "title, repository, stage, status, run_id, session_marker, "
            "plan_path, plan_sha256, created_at, updated_at, save_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active')",
            (task_id, FEEDBACK_PROJECT_ID, "Feedback", title,
             str(tmp_env.repos_root / "Feedback"), "monitor", status, run_id,
             f"{task_id}/{run_id}",
             str(tmp_env.plan_root / "Feedback" / "p.md"), plan_sha256,
             now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execution_rounds(task_id, run_id, "
            "backend, run_status, config_json, started_at, save_state, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,'active',?,?)",
            (task_id, run_id, backend, "finished",
             json.dumps({"backend": backend,
                         "connection_file": str(connection_file),
                         "provider_id": "p1", "model_id": "m1",
                         "thinking_level": "low",
                         "permission_mode": "ask"}),
             now, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    job = {
        "task_id": task_id, "run_id": run_id, "backend": "pi_desktop",
        "session_id": SESSION_ID, "session_title": "Goal 会话",
        "turn_id": EXEC_TURN_ID, "send_state": "sent",
        "config": {"connection_file": str(connection_file)},
        "pid": 4242, "status": "finished", "started_at": now,
        "ended_at": now, "exit_code": 0, "detail": None,
    }
    if goal_approval:
        job["goal_approval"] = {
            "proposal_id": PROPOSAL_ID, "title": "目标契约",
            "status": "approved", "planning_turn_id": PLAN_TURN_ID,
            "execution_turn_id": EXEC_TURN_ID,
            "target_permission_mode": "ask", "approved_at": now,
            "approved_by": "kaneopilot", "execution_id": EXECUTION_ID,
        }
    (run_dir / "job.json").write_text(
        json.dumps(job, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [
            {"v": 1, "seq": 1, "ts": now, "type": "session",
             "session_id": SESSION_ID, "session_title": "Goal 会话",
             "turn_id": EXEC_TURN_ID},
            {"v": 1, "seq": 2, "ts": now, "type": "user", "content": "任务"},
            {"v": 1, "seq": 3, "ts": now, "type": "terminal",
             "outcome": "completed", "reason": "completed"},
        ]), encoding="utf-8"
    )
    return task_id, run_id, run_dir


def set_session_runs_root(tmp_env, path: Path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_env.db))
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('session_runs_root', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(str(path)),),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------- 契约形状

def test_goal_report_commands_are_registered(cli):
    """新命令已注册且 payload 校验生效（缺必填 / 未知字段）。"""
    out = cli("get-goal-report", {}, expect=1)
    assert out["code"] == "bad_request"
    assert "missing required field" in out["error"]

    out = cli("get-goal-report",
              {"task_id": "t", "run_id": "r", "bogus": 1}, expect=1)
    assert out["code"] == "bad_request"
    assert "unknown fields" in out["error"]

    out = cli("record-goal-evaluation", {"task_id": "t"}, expect=1)
    assert out["code"] == "bad_request"
    assert "missing required field" in out["error"]

    # advance-task 硬闸材料字段已注册（类型校验生效）。
    out = cli("advance-task", {
        "task_id": "t", "target": "in-review",
        "expected_current": "in-progress", "evaluation_event_id": "x",
    }, expect=1)
    assert out["code"] == "bad_request"
    assert "evaluation_event_id" in out["error"]


def test_read_only_goal_report_roundtrip_and_identity(cli, tmp_env, fake_plus):
    """只读取回报告：身份、分态、双哈希各自独立（§5.23.10 T5-A/T5-B）。"""
    conn_file = write_connection(tmp_env.root, fake_plus)
    task_id, run_id, run_dir = seed_goal_round(tmp_env, conn_file)
    set_session_runs_root(tmp_env, tmp_env.app_support / "runs")

    out = cli("get-goal-report", {"task_id": task_id, "run_id": run_id})
    assert out["state"] == "ready"
    assert out["read_only"] is True
    assert out["session_id"] == SESSION_ID
    assert out["proposal_id"] == PROPOSAL_ID
    assert out["execution_id"] == EXECUTION_ID
    assert out["turn_id"] == EXEC_TURN_ID
    assert out["report_sha256"] == REPORT_SHA
    assert out["integrity_kind"] == "structured"
    assert out["execution_status"] == "completed"
    assert out["verdict"] == "met"
    # plan_sha256（KaneoPilot 计划文件）与 contractHash（Plus 契约 artifact）
    # 各管各的文件——不得被当成同一个哈希。
    assert out["plan_sha256"] == PLAN_SHA
    assert out["report"]["contractHash"] == CONTRACT_HASH
    assert out["plan_sha256"] != out["report"]["contractHash"]
    # 只读命令不改写任务状态。
    assert cli("get-task", {"task_id": task_id})["task"]["status"] == "running"


def test_missing_goal_approval_is_a_deterministic_conflict(
    cli, tmp_env, fake_plus
):
    """无 job.goal_approval → 不能放行，且绝不返回伪造正文。"""
    conn_file = write_connection(tmp_env.root, fake_plus)
    task_id, run_id, _run_dir = seed_goal_round(
        tmp_env, conn_file, goal_approval=False
    )
    set_session_runs_root(tmp_env, tmp_env.app_support / "runs")

    out = cli("get-goal-report", {"task_id": task_id, "run_id": run_id},
              expect=1)
    assert out["code"] == "conflict"
    assert "goal_approval" in out["error"]
    assert "report" not in out


def test_fake_ai_cannot_bypass_the_core_bottom_line(cli, tmp_env, fake_plus):
    """fake AI 提交 complete 但报告是 fallback → 核心确定性拒绝。

    这里只证明**核心闸门**：不冒充真实模型判断（无任何模型调用）。
    """
    conn_file = write_connection(tmp_env.root, fake_plus)
    task_id, run_id, _run_dir = seed_goal_round(tmp_env, conn_file)
    set_session_runs_root(tmp_env, tmp_env.app_support / "runs")
    # Host 生成的是兜底报告：ready 但没有完成报告内容。
    fake_plus.report = FakePlus.default_report(
        integrity={"kind": "fallback"}, verdict="unknown"
    )

    # monitor claim（本端请求）必须有有效租约。
    import sqlite3

    conn = sqlite3.connect(str(tmp_env.db))
    try:
        conn.execute(
            "INSERT INTO requests(request_id, idempotency_key, task_id, "
            "stage, kind, status, priority, run_id, claimed_by, claimed_at, "
            "heartbeat_at, lease_expires_at, attempt_no, created_at, "
            "updated_at) VALUES ('req-monitor-1','monitor:x',?,'monitor',"
            "'monitor','claimed',100,'mrun-1',?,'2026-09-27T00:00:00.000000"
            "+00:00','2026-09-27T00:00:00.000000+00:00',"
            "'2099-01-01T00:00:00.000000+00:00',1,"
            "'2026-09-27T00:00:00.000000+00:00',"
            "'2026-09-27T00:00:00.000000+00:00')",
            (task_id, EXECUTORS["monitor"]),
        )
        conn.commit()
    finally:
        conn.close()
    out = cli("record-goal-evaluation", {
        "task_id": task_id, "execution_run_id": run_id,
        "monitor_request_id": "req-monitor-1", "monitor_run_id": "mrun-1",
        "plan_sha256": PLAN_SHA, "report_id": "plus-report-1",
        "report_sha256": REPORT_SHA, "decision": "complete",
        "criterion_reviews": [{"id": "c1", "result": "met"}],
        "reason": "fake AI 声称完成",
    }, expect=1)
    assert out["code"] == "conflict"
    assert "structured" in out["error"]


def test_advance_task_gate_requires_monitor_material(
    cli, tmp_env, fake_plus
):
    """绕过 monitor 直接推进 pi_desktop 轮次 → conflict，远端不动。"""
    conn_file = write_connection(tmp_env.root, fake_plus)
    task_id, run_id, _run_dir = seed_goal_round(tmp_env, conn_file)
    set_session_runs_root(tmp_env, tmp_env.app_support / "runs")
    # 远端任务在 in-progress：硬闸应在任何远端写之前拒绝。
    out = cli("advance-task", {
        "task_id": task_id, "target": "in-review",
        "expected_current": "in-progress",
    }, expect=1)
    assert out["code"] == "conflict"
    assert "绕过 monitor" in out["error"]
    # 本地状态未改（远端从未被触碰）。
    assert cli("get-task", {"task_id": task_id})["task"]["status"] == "running"


def test_pi_round_is_not_blocked_by_the_goal_gate(cli, tmp_env, fake_plus):
    """pi 后端轮次不受 T5 硬闸影响（§5.18.4 旧接口不变）。"""
    conn_file = write_connection(tmp_env.root, fake_plus)
    task_id, run_id, _run_dir = seed_goal_round(
        tmp_env, conn_file, backend="pi", run_id="runpiroot00000001",
    )
    set_session_runs_root(tmp_env, tmp_env.app_support / "runs")
    out = cli("advance-task", {
        "task_id": task_id, "target": "in-review",
        "expected_current": "in-progress",
    }, expect=1)
    # 只有远端/凭据相关失败，绝不出现 T5 硬闸的「绕过 monitor」拒绝。
    assert "绕过 monitor" not in out["error"]
    assert "evaluation_event_id" not in out["error"]


# ------------------------------------------------------------ T6 兼容门

def test_old_plus_missing_project_name_field_blocks_dispatch(
    cli, tmp_env
):
    """旧 Plus（schema 无 projectName）→ dispatch-start 明确阻断。"""
    server = FakePlus(project_name_field=False).start()
    try:
        conn_file = write_connection(tmp_env.root, server)
        # 保存 pi_desktop 配置（能力门会在 dispatch-start 前复核该轮快照）。
        cli("set-exec-config", {
            "backend": "pi_desktop",
            "pi_desktop": {
                "connection_file": str(conn_file),
                "provider_id": "p1", "model_id": "m1",
                "thinking_level": "low", "permission_mode": "ask",
            },
        })
        caps = cli("exec-capabilities", {})
        # 兼容门在能力查询里可见（session_naming=false 表示旧 Plus）。
        assert caps["pi_desktop"]["session_naming"] is False
    finally:
        server.stop()


def test_capabilities_expose_session_naming_gate(cli, tmp_env, fake_plus):
    """exec-capabilities 的 pi_desktop 分支暴露 T6 兼容门布尔值。"""
    conn_file = write_connection(tmp_env.root, fake_plus)
    cli("set-exec-config", {
        "backend": "pi_desktop",
        "pi_desktop": {
            "connection_file": str(conn_file),
            "provider_id": "p1", "model_id": "m1",
            "thinking_level": "low", "permission_mode": "ask",
        },
    })
    caps = cli("exec-capabilities", {})
    branch = caps["pi_desktop"]
    assert set(branch) == {"available", "detail", "tools_ok", "version",
                           "session_naming"}
    assert branch["session_naming"] is True
    blob = json.dumps(caps, ensure_ascii=False)
    assert fake_plus.token not in blob


def test_display_names_are_not_used_for_identity(cli, tmp_env, fake_plus):
    """两个同名不同路径的轮次仍是不同机器身份（T6：展示名不参与身份）。"""
    conn_file = write_connection(tmp_env.root, fake_plus)
    task_a, run_a, _ = seed_goal_round(
        tmp_env, conn_file, run_id="runshow0000000001"
    )
    set_session_runs_root(tmp_env, tmp_env.app_support / "runs")
    out = cli("get-goal-report", {"task_id": task_a, "run_id": run_a})
    # 报告中即使带着相同会话标题，身份仍只认 session/proposal/execution。
    fake_plus.report = FakePlus.default_report(sessionTitle="根层契约任务")
    again = cli("get-goal-report", {"task_id": task_a, "run_id": run_a})
    assert again["state"] == "ready"
    assert again["session_id"] == SESSION_ID
    assert again["plan_sha256"] == out["plan_sha256"]


# ------------------------------------------------------------------ 入口

def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
