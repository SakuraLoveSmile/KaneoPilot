"""真实录制夹具的新鲜度与形状门（scenarios.md 场景 25 的真实录制半边）。

``fixtures/plus_*_real.json`` 分两类 provenance，本文件只覆盖**真实录制**类：

- ``real_recording_via_mcp_pi_deskto``：由 ``MCP tools/call
  pi_desktop_invoke`` 对**在线真实 Plus**（pi-desktop-plus 0.15.7）发起只读
  调用后脱敏落盘；载荷形状为 ``{"operation": ..., "result": {...}}`` 或
  ``{"ok": false, "error": {...}}``。
- ``reconstructed_from_core_parsers``：按核心解析口径重建的脱敏样本，由
  ``test_real_shape_fixtures.py`` 覆盖，不在本文件范围。

本文件钉住：

- ``_captured_at`` 存在且 ≤30 天，超期 → 失败并提示重新录制；
- ``_provenance`` 为 ``real_recording_via_mcp_pi_desktop_invoke``；
- ``_operation`` 与文件名对应，且属白名单（只读 operation）；
- 载荷是 ``pi_desktop_invoke`` 信封（有 ``operation``+``result``，或
  ``ok:false``+``error``）；
- 脱敏：不含 token / Authorization / 真实回环地址 / 未脱敏 UUID；
- **形状结论门**：把本次录得的关键事实钉成断言，防止 Plus 升级后形状漂移
  被默默忽略（见下方 ``RECORDED_FACTS``）。
"""

from __future__ import annotations

import json
import os
import re
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
STALENESS_DAYS = 30
PROVENANCE = "real_recording_via_mcp_pi_desktop_invoke"

RECORDINGS = {
    "plus_app_getVersion_real.json": "app/getVersion",
    "plus_agent_status_real.json": "agent/getStatus",
    "plus_turn_get_real_recording.json": "turn/get",
    "plus_turn_get_not_found_real.json": "turn/get-missing",
    "plus_goal_report_list_real.json": "goalReport/list",
    "plus_plans_pending_real.json": "plans/pending",
}

FORBIDDEN_SUBSTRINGS = ("Authorization", "Bearer ", "accessToken", "apiKey",
                        "127.0.0.1", "localhost")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _load(name):
    path = FIXTURES_DIR / name
    assert path.is_file(), f"缺少真实录制夹具：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_scenario_real_recordings_freshness_and_shape():
    """场景 25（真实录制半边）：6 件套新鲜度 + provenance + 信封形状 + 脱敏。"""
    today = date.today()
    for name, operation in RECORDINGS.items():
        data = _load(name)
        captured = data.get("_captured_at")
        assert isinstance(captured, str) and captured, f"{name} 缺少 _captured_at"
        age = (today - datetime.strptime(captured, "%Y-%m-%d").date()).days
        assert age <= STALENESS_DAYS, (
            f"{name} 采集于 {captured}（{age} 天前，超过 {STALENESS_DAYS} 天）："
            f"Plus 控制面形状可能已漂移，请重新录制并更新 _captured_at"
        )
        assert data.get("_provenance") == PROVENANCE, (
            f"{name} _provenance 应为 {PROVENANCE}，实际 {data.get('_provenance')}"
        )
        assert isinstance(data.get("_note"), str) and data["_note"], (
            f"{name} 缺少 _note 说明"
        )
        assert data.get("_operation") == operation, (
            f"{name} _operation 应为 {operation}"
        )
        payload = data.get("payload")
        assert isinstance(payload, dict), f"{name} payload 应为对象"
        is_error = payload.get("ok") is False
        if is_error:
            assert isinstance(payload.get("error"), dict), (
                f"{name} 错误信封缺少 error 对象"
            )
            assert payload["error"].get("code"), f"{name} error 缺少 code"
        else:
            assert payload.get("operation") == operation.split("-")[0], (
                f"{name} payload.operation 应为 {operation}"
            )
            assert isinstance(payload.get("result"), dict), (
                f"{name} payload 缺少 result 对象"
            )
        blob = json.dumps(data, ensure_ascii=False)
        for secret in FORBIDDEN_SUBSTRINGS:
            assert secret not in blob, f"{name} 含未脱敏内容：{secret!r}"
        for match in UUID_RE.findall(blob):
            assert match == "00000000-0000-0000-0000-000000000000", (
                f"{name} 含未脱敏 UUID：{match}"
            )


def test_scenario_real_recording_agent_status_is_not_terminal():
    """录制事实：agent/getStatus 只给粗粒度存活信号，**不含**终态 outcome。"""
    data = _load("plus_agent_status_real.json")
    status = data["payload"]["result"]["status"]
    assert set(status) == {"sessionId", "isRunning", "pendingToolConfirmations"}, (
        f"agent/getStatus 字段集已漂移：{sorted(status)}"
    )
    assert isinstance(status["isRunning"], bool)
    for terminal in ("outcome", "terminal", "conclusion", "verdict", "endedAt"):
        assert terminal not in status, (
            f"agent/getStatus 出现终态字段 {terminal}：终态信号来源假设需重审"
        )


def test_scenario_real_recording_turn_get_shape():
    """录制事实：turn/get 只返回 {sessionId,turnId,status}，无 startedAt/endedAt。"""
    data = _load("plus_turn_get_real_recording.json")
    turn = data["payload"]["result"]
    assert set(turn) == {"sessionId", "turnId", "status"}, (
        f"turn/get 字段集已漂移：{sorted(turn)}"
    )
    assert turn["status"] in ("running", "completed", "error", "aborted"), turn
    for absent in ("startedAt", "endedAt", "errorCode", "outcome"):
        assert absent not in turn, (
            f"turn/get 出现 {absent}：核心 _turn_status 口径需同步更新"
        )


def test_scenario_real_recording_turn_get_not_found_envelope():
    """录制事实：不存在的 turn → {ok:false,error:{code:NOT_FOUND}}，非超时。"""
    data = _load("plus_turn_get_not_found_real.json")
    error = data["payload"]["error"]
    assert error["code"] == "NOT_FOUND", error
    assert error["message"] == "turn not found", error
    assert "timeout" not in error["message"].lower(), (
        "NOT_FOUND 不得与超时混为一谈（阶梯归属不同）"
    )


def test_scenario_real_recording_goal_report_and_plan_shapes():
    """录制事实：goalReport/list 与 plans/pending 的结构化输出可用且字段稳定。"""
    reports = _load("plus_goal_report_list_real.json")["payload"]["result"]["reports"]
    assert isinstance(reports, list) and reports, "goalReport/list 应返回非空列表"
    report = reports[0]
    for field in ("reportId", "sessionId", "turnId", "status", "verdict",
                  "integrity", "goalTitle"):
        assert field in report, f"goalReport 条目缺少 {field}：{sorted(report)}"
    assert report["status"] in ("pending", "ready", "failed", "superseded"), report

    history = _load("plus_plans_pending_real.json")["payload"]["result"]["history"]
    assert isinstance(history, list) and history, "plans/pending 应返回非空 history"
    entry = history[0]
    for field in ("id", "kind", "action", "artifact", "executionState",
                  "createdAt", "plan"):
        assert field in entry, f"plans/pending 条目缺少 {field}：{sorted(entry)}"
    artifact = entry["artifact"]
    for field in ("relativePath", "sha256", "sizeBytes", "workspaceKind"):
        assert field in artifact, f"artifact 缺少 {field}：{sorted(artifact)}"
    assert len(artifact["sha256"]) == 64, artifact


def test_scenario_real_recording_app_version_matches_doctor_probe():
    """录制事实：doctor --live 的 plus 版本探针期望值（0.15.7）。"""
    data = _load("plus_app_getVersion_real.json")
    result = data["payload"]["result"]
    assert result["name"] == "Pi-Desktop-Plus", result
    assert re.match(r"^\d+\.\d+\.\d+$", result["version"]), result
    assert isinstance(result["protocolVersion"], int), result


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
