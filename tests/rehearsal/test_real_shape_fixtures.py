"""真实形状契约夹具新鲜度门（scenarios.md 场景 25）。

``fixtures/plus_*_real.json`` 是按核心解析口径重建的脱敏 Plus 输出样本
（``_provenance=reconstructed_from_core_parsers``）。本文件钉住：

- ``_captured_at`` 采集日期存在且 ≤30 天；超期 → 失败并提示从真实 Plus
  会话重新录制（形状可能已随 PI-Desktop 版本漂移）；
- ``_provenance`` 标记正确，``payload`` 为 MCP tools/call 结果形状
  （``{content, structuredContent, isError}``）；
- 内层视图字段与 ``pi_desktop_worker.py`` 实际读取口径一致（status/turn/
  session 三类）；
- 样本脱敏：不含 token / Authorization / 真实回环地址。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

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

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# 采集日期超过该天数 → 失败（提示重新录制）。30 天：Plus 控制面迭代周期内
# 形状漂移应被发现，而不是默默漂移。
STALENESS_DAYS = 30

STATUS_FIELDS = ("sessionId", "currentTurnId", "planningState", "isRunning",
                 "pendingPermission")
TURN_FIELDS = ("sessionId", "turnId", "status", "startedAt", "endedAt",
               "errorCode")
TURN_STATUSES = ("running", "completed", "error", "aborted")
SESSION_FIELDS = ("id", "messages", "messageStart", "messageEnd",
                  "hasMoreBefore", "hasMoreAfter")

FORBIDDEN_SUBSTRINGS = ("Authorization", "Bearer ", "accessToken",
                        "apiKey", "127.0.0.1", "localhost")


def _load(name):
    path = FIXTURES_DIR / name
    assert path.is_file(), f"缺少真实形状夹具：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _captured_on(data, name):
    captured = data.get("_captured_at")
    assert isinstance(captured, str) and captured, (
        f"{name} 缺少 _captured_at（ISO 日期）")
    return datetime.strptime(captured, "%Y-%m-%d").date()


def test_scenario_real_shape_fixture_staleness_gate():
    """场景 25：三件套新鲜度 + provenance + payload 形状 + 脱敏。"""
    names = ("plus_status_real.json", "plus_turn_get_real.json",
             "plus_session_get_real.json")
    today = date.today()
    for name in names:
        data = _load(name)
        captured = _captured_on(data, name)
        age = (today - captured).days
        assert age <= STALENESS_DAYS, (
            f"{name} 采集于 {captured}（{age} 天前，超过 {STALENESS_DAYS} 天）："
            f"Plus 控制面形状可能已漂移，请从真实 Plus 会话重新录制并更新 "
            f"_captured_at")
        assert data.get("_provenance") == "reconstructed_from_core_parsers", (
            f"{name} _provenance 应为 reconstructed_from_core_parsers")
        assert isinstance(data.get("_note"), str) and data["_note"], (
            f"{name} 缺少 _note 说明")
        payload = data.get("payload")
        assert isinstance(payload, dict), f"{name} payload 应为对象"
        assert {"content", "structuredContent", "isError"} <= set(payload), (
            f"{name} payload 不是 MCP tools/call 结果形状：{sorted(payload)}")
        blob = json.dumps(data, ensure_ascii=False)
        for secret in FORBIDDEN_SUBSTRINGS:
            assert secret not in blob, f"{name} 含未脱敏内容：{secret!r}"


def test_real_shape_status_matches_worker_parser():
    """pi_agent_status 形状：worker 读取的 status 视图字段齐全。"""
    data = _load("plus_status_real.json")
    status = data["payload"]["structuredContent"]["status"]
    for field in STATUS_FIELDS:
        assert field in status, f"status 缺少字段 {field}：{sorted(status)}"
    assert isinstance(status["isRunning"], bool)
    assert status["planningState"] in (
        "inactive", "idle", "planning", "awaiting_approval"), status


def test_real_shape_turn_get_matches_worker_parser():
    """pi_turn_get 形状：_turn_status/_turn_identity_matches 读取口径。"""
    data = _load("plus_turn_get_real.json")
    turn = data["payload"]["structuredContent"]["turn"]
    for field in TURN_FIELDS:
        assert field in turn, f"turn 缺少字段 {field}：{sorted(turn)}"
    assert turn["status"] in TURN_STATUSES, turn
    assert turn["sessionId"] == turn.get("sessionId")
    assert turn["turnId"]


def test_real_shape_session_get_matches_worker_parser():
    """pi_session_get 形状：分页字段 + 消息条目（role/content）。"""
    data = _load("plus_session_get_real.json")
    session = data["payload"]["structuredContent"]["session"]
    for field in SESSION_FIELDS:
        assert field in session, f"session 缺少字段 {field}：{sorted(session)}"
    messages = session["messages"]
    assert isinstance(messages, list) and messages, "messages 应为非空列表"
    for message in messages:
        role = message.get("role")
        assert isinstance(role, str) and role, message
        if role == "tool":
            # 工具消息按 worker 读取口径：toolName/toolStatus/toolResult。
            assert message.get("toolName"), message
            assert message.get("toolStatus"), message
            assert "toolResult" in message, message
        else:
            assert "content" in message, message
    roles = {m["role"] for m in messages}
    assert {"user", "assistant"} <= roles, roles


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
