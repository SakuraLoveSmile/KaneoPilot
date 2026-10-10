"""session-view 只读会话总览契约（protocol §5.27.5；engine.commands.cmd_session_view）。

覆盖：入参校验（task_id/run_id 至少一个、任务不存在）、无执行轮次、
运行轮次形状（冻结字段 + connection.ok + allowed_actions）、needs_user 的
blocked_reason、旧后端（dsh/headless/ATIF）→ unavailable「旧后端会话已弃用」、
仅 run_id 反查任务。驱动全部经 CLI（SQLite 是唯一状态写入源）；旧后端轮次行
用 conftest 的 DB 后门构造（CLI 无法表达已弃用后端）。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_PARENT)
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

try:
    from tests.rehearsal import conftest as _rehearsal_conftest  # noqa: F401,E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    import conftest as _rehearsal_conftest  # noqa: F401,E402

try:
    from conftest import db_exec  # noqa: E402
except ImportError:  # dotted 收集
    from tests.conftest import db_exec  # noqa: E402

# §5.27.5 冻结的 session 字段（blocked_reason 仅 needs_user 时出现）。
SESSION_FIELDS = frozenset({
    "task_id", "run_id", "backend", "run_status", "session_id", "turn_id",
    "save_state", "state", "connection", "allowed_actions",
})
CONNECTION_FIELDS = frozenset({"state", "detail"})
RUN_STATUS_VOCAB = ("pending", "running", "finished", "failed", "unknown")


def test_session_view_requires_task_or_run(cli):
    out = cli("session-view", {}, expect=1)
    assert out["code"] == "bad_request", out
    assert "task_id" in out["error"], out
    out = cli("session-view", {"task_id": "no-such-task"}, expect=1)
    assert out["code"] == "not_found", out


def test_session_view_without_round_is_rejected(cli, factory):
    task = factory.sync_task()
    out = cli("session-view", {"task_id": task.task_id}, expect=1)
    assert out["code"] == "bad_request", out
    assert "执行轮次" in out["error"], out


def test_session_view_running_round_shape(cli, factory):
    running = factory.make_running()
    task_id = running.task.task_id
    run_id = running.dispatch_run_id
    out = cli("session-view", {"task_id": task_id})
    session = out["session"]
    assert SESSION_FIELDS <= set(session), (
        f"缺字段：{sorted(SESSION_FIELDS - set(session))}")
    assert session["task_id"] == task_id, session
    assert session["run_id"] == run_id, session
    assert session["run_status"] in RUN_STATUS_VOCAB, session
    assert CONNECTION_FIELDS <= set(session["connection"]), session
    assert session["connection"]["state"] in ("ok", "degraded", "unavailable"), \
        session
    assert isinstance(session["allowed_actions"], list), session
    assert "blocked_reason" not in session, session  # 非 needs_user 不出现


def test_session_view_blocked_reason_for_needs_user(cli, factory):
    blocked = factory.make_needs_user()
    task_id = blocked.task.task_id
    run_id = blocked.dispatch_run_id
    out = cli("session-view", {"task_id": task_id, "run_id": run_id})
    session = out["session"]
    assert session["run_id"] == run_id, session
    assert session.get("blocked_reason"), session
    view = cli("get-task", {"task_id": task_id})["task"]
    assert session["blocked_reason"] in (
        view.get("blocked_summary"), view.get("failure_reason")), session


def test_session_view_old_backend_unavailable(cli, factory):
    """旧后端（dsh）轮次 → connection unavailable + 「旧后端会话已弃用」。"""
    task = factory.sync_task()
    task_id = task.task_id
    run_id = "legacy-dsh-run-0001"
    db_exec(
        cli.env["KANPILOT_DB"],
        "INSERT INTO execution_rounds(task_id, run_id, backend, run_status, "
        "save_state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, run_id, "dsh", "finished", "active",
         "2026-10-10T00:00:00+00:00", "2026-10-10T00:00:00+00:00"))
    out = cli("session-view", {"task_id": task_id, "run_id": run_id})
    session = out["session"]
    assert session["backend"] == "dsh", session
    assert session["connection"]["state"] == "unavailable", session
    assert session["connection"]["detail"] == "旧后端会话已弃用", session
    assert session["state"] == "unavailable", session


def test_session_view_resolves_task_from_run_id(cli, factory):
    running = factory.make_running()
    task_id = running.task.task_id
    run_id = running.dispatch_run_id
    out = cli("session-view", {"run_id": run_id})
    assert out["session"]["task_id"] == task_id, out
    out = cli("session-view", {"run_id": "no-such-run"}, expect=1)
    assert out["code"] == "bad_request", out


def test_session_view_status_only_never_reads_messages(cli, factory):
    """status_only=true 只回状态摘要，不带 cursor 等消息读取位。"""
    running = factory.make_running()
    task_id = running.task.task_id
    out = cli("session-view", {"task_id": task_id, "status_only": True})
    assert "cursor" not in out["session"], out
    assert SESSION_FIELDS <= set(out["session"]), out


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
