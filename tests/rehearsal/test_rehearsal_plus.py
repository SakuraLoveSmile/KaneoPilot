"""Plus 不可达与发送状态未知场景（scenarios.md 场景 2/3/4）。

- 场景 2 plus_unreachable_before_start：派发前就绪探测（连接文件校验 +
  MCP 握手）失败 → 不启动 worker、不建会话、不下发 prompt；任务停在可安全态。
- 场景 3 plus_unreachable_during_run：运行中只读调用不可达 → 只读续接不重发
  （prompt/建会话计数不变、run_id 不变、send_state 不被翻错）。
- 场景 4 send_state_unknown_no_resend：prompt 响应丢失 → send_state=unknown，
  UI 不出现普通重试；仅 restore/accept_duplicate/mark_sent。

三个场景复用 tests/test_auto_pipeline.py 全链夹具：场景 2 在冻结派发轮次后
关闭假 Plus，场景 3 在运行后关闭；场景 4 的 ``prompt_mode="http500"``
模拟发送响应丢失。
"""

from __future__ import annotations

import json
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
    from conftest import FakeKaneo, TaskFactory, run_cli_raw  # noqa: E402
except ImportError:  # dotted 收集
    from tests.conftest import FakeKaneo, TaskFactory, run_cli_raw  # noqa: E402

import test_auto_pipeline as _pipeline  # noqa: E402
from kanpilot import auto_dispatch  # noqa: E402
from kanpilot.engine import decide, ladder  # noqa: E402


@pytest.fixture
def pipeline():
    """v5 全链演练夹具（手动生命周期；不依赖 unittest 运行器）。"""
    case = _pipeline.TestAutoPipeline()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()
        case.doCleanups()


def _session_creates(case):
    return len([c for c in case.server.calls if c[0] == "pi_session_create"])


# ---------------------------------------------------------------------------
# 场景 2：Plus 不可达（启动前）
# ---------------------------------------------------------------------------


def test_scenario_plus_unreachable_before_start(pipeline):
    # Freeze a usable Plus configuration before claim. Disconnect only after
    # prepare/begin-send so dispatch-start reaches the real availability gate.
    pipeline._adopt_and_plan()
    task_id = _pipeline.TASK
    claimed = pipeline.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
    assert claimed["claimed"] is True, claimed
    run_id = claimed["run_id"]
    context = claimed["context"]
    evidence = {"backend": "pi_desktop", "config": context["exec_config"],
                "session_marker": context["session_marker"], "repository": context["repository"]}
    identity = {"task_id": task_id, "run_id": run_id}
    pipeline.call("dispatch-prepare", {**identity, "evidence": evidence})
    pipeline.call("dispatch-begin-send", identity)
    assert not _session_creates(pipeline)
    assert pipeline.server.prompt_count == 0
    pipeline.server.stop()

    caps = pipeline.call("exec-capabilities")
    assert caps["pi_desktop"]["available"] is False, caps["pi_desktop"]
    assert caps["pi_desktop"]["tools_ok"] is False, caps["pi_desktop"]
    failure = pipeline.call_err("dispatch-start", identity)
    assert failure.code in ("validation_failed", "remote_unavailable"), failure
    secret = json.loads(pipeline.connection_file.read_text())["token"]
    assert secret not in json.dumps(caps, ensure_ascii=False)
    assert secret not in str(failure)
    assert not _session_creates(pipeline)
    assert pipeline.server.prompt_count == 0
    view = pipeline.task(task_id)
    assert view["status"] == "submitting", view
    assert view["send_state"] == "not_sent", view
    assert view["run_id"] == run_id, view


# ---------------------------------------------------------------------------
# 场景 3：Plus 不可达（运行中）
# ---------------------------------------------------------------------------


def test_scenario_plus_unreachable_during_run(pipeline):
    pipeline._adopt_and_plan()
    run_id = pipeline._execute()
    value = pipeline.task(_pipeline.TASK)
    assert value["status"] == "running", value
    assert value["send_state"] == "sent", value
    prompts_before = pipeline.server.prompt_count
    sessions_before = _session_creates(pipeline)

    pipeline.server.stop()  # Plus 控制面不可达（连接被拒）

    # 只读续接：不重发 prompt、不建第二会话、不新开轮次
    out = pipeline.call("round-observe",
                        {"task_id": _pipeline.TASK, "run_id": run_id})
    assert isinstance(out, dict), out
    value = pipeline.task(_pipeline.TASK)
    assert value["run_id"] == run_id, value
    assert value["send_state"] == "sent", value
    assert pipeline.server.prompt_count == prompts_before, (
        pipeline.server.prompt_count)
    assert _session_creates(pipeline) == sessions_before

    # 瞬时失败归阶梯 1：退避重试有界，绝不翻倍派发
    transient = decide.failure_decision("remote_unavailable", attempt_no=0)
    assert transient.action == decide.RETRY, transient
    assert transient.params["backoff_seconds"] == 1.0, transient
    assert ladder.classify("remote_unavailable") == ladder.LADDER_TRANSIENT

    # 监控阶段（Plus 仍不可达）不产生第二个执行轮次/第二次发送
    requests_before = [r["request_id"] for r in
                       pipeline.call("list-requests", {})["requests"]
                       if r["task_id"] == _pipeline.TASK
                       and r["kind"] == "dispatch"]
    pipeline.call_err("dispatch-start",
                      {"task_id": _pipeline.TASK, "run_id": run_id})
    requests_after = [r["request_id"] for r in
                      pipeline.call("list-requests", {})["requests"]
                      if r["task_id"] == _pipeline.TASK
                      and r["kind"] == "dispatch"]
    assert requests_after == requests_before, (requests_before, requests_after)
    assert pipeline.server.prompt_count == prompts_before


# ---------------------------------------------------------------------------
# 场景 4：发送状态未知 → 等待与对账（绝不重发）
# ---------------------------------------------------------------------------


def test_scenario_send_state_unknown_no_resend(pipeline):
    pipeline._adopt_and_plan()
    pipeline.server.prompt_mode = "http500"   # prompt 响应丢失（可能已受理）
    pipeline._stage(auto_dispatch, "dispatch")
    value = pipeline.task(_pipeline.TASK)

    # 不算失败、按 failure_stage 归列；send_state=unknown 优先
    assert value["send_state"] == "unknown", value
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "dispatch_failed", value
    actions = set(value["allowed_actions"])
    assert {"restore", "accept_duplicate", "mark_sent"} <= actions, actions
    assert "retry" not in actions, actions

    # 绝不重发：只发过一次 prompt、只建过一个会话
    assert pipeline.server.prompt_count == 1, pipeline.server.prompt_count
    assert _session_creates(pipeline) == 1

    # 证据保留：run_id/session_id 可对账
    assert value["run_id"], value

    # 人工认定未发送（restore）→ 失联解除，任务回可安全态；仍无重发
    pipeline.call("resolve", {"task_id": _pipeline.TASK, "action": "restore"})
    value = pipeline.task(_pipeline.TASK)
    assert value["send_state"] != "unknown", value
    assert pipeline.server.prompt_count == 1, pipeline.server.prompt_count
    assert _session_creates(pipeline) == 1


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
