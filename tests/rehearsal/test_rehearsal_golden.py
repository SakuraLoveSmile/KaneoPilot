"""黄金路径与修复轮场景（scenarios.md 场景 1/5/6/7/8/9/16）。

复用 ``tests/test_auto_pipeline.py`` 的 v5 全链夹具（假 Kaneo + 假 Plus +
假 claude 规划/评审 + 假 gh + 真实本地 git）：把它的 ``TestAutoPipeline``
包成 pytest fixture（手动 setUp/tearDown + doCleanup），因此真实 pytest 与
``python3 -m pytest tests/rehearsal -q`` 的 unittest discover 兜底路径都能跑。

每个场景按 scenarios.md「预期行为」列逐条断言；引擎尚未实现的能力（如空
diff 的备用模型重试执行）不伪造，仅在决策层断言并在模块 docstring 注明。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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

# tests/ 已在 sys.path（bootstrap）；全链夹具自包含（setUp 自建临时根）。
import test_auto_pipeline as _pipeline  # noqa: E402
# 只经模块对象引用夹具类：pytest 会收集模块命名空间里的任何 TestCase 子类，
# 因此不能 `from test_auto_pipeline import TestAutoPipeline`。
TASK = _pipeline.TASK  # noqa: E402
PROJECT = _pipeline.PROJECT  # noqa: E402
VERIFY = _pipeline.VERIFY  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
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


def _script_rounds(case, contents):
    """按轮次脚本化执行轮的工作树写入与 verify 记录。

    ``contents``：{轮次: 内容}；``None`` = 不写入（空 diff）；未列出的轮次
    写默认 ``round N``。verify 命令真实在工作树执行，退出码记入消息。
    """
    original = case.server.tool_call

    def plus(name, args):
        response = original(name, args)
        if name == "pi_plans_resolve" and args.get("action") == "approve":
            worktree = Path(case.server.created["projectPath"])
            content = contents.get(case.round, "round %d" % case.round)
            if content is None:
                # 空 diff 轮：原钩子已写入 feedback.txt 并跑过 verify——删掉
                # 写入与 __pycache__ 让工作树真正零改动；记录干净完成。
                (worktree / "feedback.txt").unlink(missing_ok=True)
                for cache in worktree.rglob("__pycache__"):
                    shutil.rmtree(cache, ignore_errors=True)
                exit_code, output = 0, "no changes written (empty-diff round)"
            else:
                (worktree / "feedback.txt").write_text(content + "\n",
                                                       encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                    cwd=worktree, capture_output=True, text=True, timeout=120)
                exit_code, output = result.returncode, result.stdout + result.stderr
            case.server.messages = [{
                "id": "verify-" + str(case.round), "role": "toolResult",
                "toolName": "exec", "toolArgs": {"command": VERIFY},
                "toolResult": {"output": output, "exit_code": exit_code},
                "exitCode": exit_code,
                "turnId": case.server.execution_turn,
            }]
        return response

    case.server.tool_call = plus


def _force_sync_due(case):
    conn = db.connect(case.db_path)
    try:
        core.update_delivery(conn, db.require_task(conn, TASK), util.now_iso(),
                             next_sync_at=None)
    finally:
        conn.close()


def _deliver_to_merge(case):
    """PR 已开 → 合并 → sync → done；返回最终 task 视图。"""
    case.call("tick")
    case._stage(_delivery_runner(), "delivery")
    value = case.task(TASK)
    assert value["delivery"].get("pr_number") == 7, value
    case.gh_state.write_text(json.dumps(
        {"created": True, "state": "MERGED", "mergedAt": util.now_iso()}))
    _force_sync_due(case)
    case.call("tick")
    case._stage(_delivery_runner(), "delivery")
    return case.task(TASK)


def _delivery_runner():
    from kanpilot import delivery_runner
    return delivery_runner


# ---------------------------------------------------------------------------
# 场景 1：黄金路径一遍到 PR 合并
# ---------------------------------------------------------------------------


def test_scenario_golden_path_to_pr_merge(pipeline):
    (pipeline.root / "review-count").write_text("2")  # 首轮评审即 pass
    pipeline._adopt_and_plan()
    value = pipeline.task(TASK)
    assert value["status"] == "ready", value
    assert decide.phase_condition(value["status"]) == ("plan", "ready")

    run_id = pipeline._execute()
    value = pipeline.task(TASK)
    assert value["status"] == "running", value
    assert value["run_id"] == run_id
    assert decide.phase_condition(value["status"]) == ("observe", "running")

    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "in_review", value
    assert len(value["delivery"]["commits"]) == 1, value["delivery"]

    pipeline._review()
    value = pipeline.task(TASK)
    assert value["status"] == "in_review", value

    value = _deliver_to_merge(pipeline)
    assert value["status"] == "done", value
    assert decide.phase_condition(value["status"]) == ("done", "merged")
    assert pipeline.fake.tasks[TASK]["status"] == "done"
    # 一次发送、无重发；主检出未被动过；Kaneo 评论仅交付一条
    assert pipeline.server.prompt_count == 1, pipeline.server.prompt_count
    assert pipeline.primary == (
        _git(pipeline.repo, "rev-parse", "HEAD"),
        _git(pipeline.repo, "status", "--porcelain"))
    assert len(pipeline.comments) == 1, pipeline.comments


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True,
                          timeout=30).stdout.strip()


# ---------------------------------------------------------------------------
# 场景 5：空 diff → 有界补救 → attention
# ---------------------------------------------------------------------------


def test_scenario_empty_diff_backup_model_then_attention(pipeline):
    pipeline._adopt_and_plan()
    _script_rounds(pipeline, {1: None})          # 轮次 1：执行完成但零改动
    run_id = pipeline._execute()
    value = pipeline.task(TASK)

    # 核心亲判（engine-verdict）：有效 diff 为空 → 不进交付
    verdict = pipeline.call("engine-verdict", {
        "task_id": TASK, "expected_execution_run_id": run_id,
        "expected_plan_sha256": value["plan_sha256"]})
    assert verdict["empty_diff"] is True, verdict
    assert verdict["verification_ok"] is False, verdict
    assert verdict["effective_changes"] == [], verdict

    # 质量阶梯（阶梯 3）：同计划有界补救 ≤2 轮，超出 → 阶梯 4 收件箱
    first = decide.failure_decision("empty_diff", fix_round=0)
    assert first.action == decide.START_FIX_ROUND, first
    assert first.params["fix_round"] == 1
    assert ladder.classify("empty_diff") == ladder.LADDER_QUALITY
    capped = decide.failure_decision("empty_diff", fix_round=ladder.MAX_FIX_ROUNDS)
    assert capped.action == decide.BLOCK, capped
    assert capped.ladder == ladder.LADDER_NEEDS_YOU, capped
    assert capped.attention is True

    # 监控读到"干净完成但无可计入改动" → no_changes 阻塞，不伪造提交/PR
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "no_changes", value
    assert not value["delivery"].get("commits"), value["delivery"]
    assert not value["delivery"].get("pr_number"), value["delivery"]

    # 修复轮仍空 diff（同计划重试一次）→ 再次核心亲判仍为空
    pipeline.call("resolve", {"task_id": TASK, "action": "replan"})
    pipeline._adopt_and_plan()
    _script_rounds(pipeline, {2: None})
    run2 = pipeline._execute()
    value = pipeline.task(TASK)
    verdict2 = pipeline.call("engine-verdict", {
        "task_id": TASK, "expected_execution_run_id": run2,
        "expected_plan_sha256": value["plan_sha256"]})
    assert verdict2["empty_diff"] is True, verdict2
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "no_changes", value

    # 补救轮用尽后引擎不再自动补救：reconcile 只记录决策、不下命令
    summary = _reconcile(pipeline)
    mine = [a for a in summary["actions"] if a.get("task_id") == TASK]
    assert mine and all(a["command"] is None for a in mine), mine
    # 收件箱单一来源：attention-list 能看到该阻塞任务
    items = pipeline.call("attention-list", {})["items"]
    assert any(i["task_id"] == TASK for i in items), items
    # 全程只发了两次 prompt（两轮执行），无额外自动重发
    assert pipeline.server.prompt_count == 2, pipeline.server.prompt_count
    # 注：备用模型快照选择（model: fallback）引擎尚未实现——决策层已验证
    # 有界补救与升级路径；执行层的换模型重试待 engine_builder 补齐。


def _reconcile(case, mode="live"):
    from kanpilot.engine import loop
    return loop.Reconciler(case.db_path, mode=mode).reconcile_once()


# ---------------------------------------------------------------------------
# 场景 6：verify 失败 → 同分支修复轮通过
# ---------------------------------------------------------------------------


def test_scenario_verify_fail_fix_round_passes(pipeline):
    pipeline._adopt_and_plan()
    _script_rounds(pipeline, {1: "oops"})        # 轮次 1：verify 失败
    run1 = pipeline._execute()
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "verification_unconfirmed", value
    assert ladder.classify(value["failure_code"]) == ladder.LADDER_QUALITY
    actions = set(value["allowed_actions"])
    assert {"accept_verification", "replan"} <= actions, actions

    # 阶梯 3 决策：同任务分支修复轮（fix_round+1），未超限
    step = decide.failure_decision("verification_unconfirmed", fix_round=0)
    assert step.action == decide.START_FIX_ROUND, step
    assert step.params["fix_round"] == 1

    # 用户/引擎驱动的修复轮：同分支重规划 → 第二轮写对 → 验证通过
    branch_before = value["delivery"]["worktree_path"]
    pipeline.call("resolve", {"task_id": TASK, "action": "replan"})
    pipeline._adopt_and_plan()
    value = pipeline.task(TASK)
    assert value["delivery"]["worktree_path"] == branch_before, value["delivery"]
    run2 = pipeline._execute()                    # 轮次 2 默认写对内容
    assert run2 != run1
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "in_review", value
    assert len(value["delivery"]["commits"]) == 1, value["delivery"]

    (pipeline.root / "review-count").write_text("2")   # 本轮评审即 pass
    pipeline._review()
    value = _deliver_to_merge(pipeline)
    assert value["status"] == "done", value
    assert pipeline.fake.tasks[TASK]["status"] == "done"


# ---------------------------------------------------------------------------
# 场景 7：verify 连续失败 → 修复轮用尽 → needs-you
# ---------------------------------------------------------------------------


def test_scenario_verify_fail_exhausted_needs_you(pipeline):
    pipeline._adopt_and_plan()
    _script_rounds(pipeline, {1: "oops", 2: "oops"})
    pipeline._execute()
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["failure_code"] == "verification_unconfirmed", value

    # 第一轮修复：同分支重规划后再失败
    pipeline.call("resolve", {"task_id": TASK, "action": "replan"})
    pipeline._adopt_and_plan()
    pipeline._execute()
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["failure_code"] == "verification_unconfirmed", value

    # 决策层：fix_round 达到 MAX_FIX_ROUNDS → BLOCK（阶梯 4），不再自动补救
    exhausted = decide.failure_decision("verification_unconfirmed",
                                        fix_round=ladder.MAX_FIX_ROUNDS)
    assert exhausted.action == decide.BLOCK, exhausted
    assert exhausted.reason == "fix_rounds_exhausted", exhausted
    assert exhausted.ladder == ladder.LADDER_NEEDS_YOU, exhausted
    assert ladder.retries_exhausted(ladder.LADDER_QUALITY,
                                    ladder.MAX_FIX_ROUNDS) is True

    # 引擎对阻塞任务只记录决策、不下任何命令（无第三次自动执行）
    summary = _reconcile(pipeline)
    mine = [a for a in summary["actions"] if a.get("task_id") == TASK]
    assert mine and all(a["command"] is None for a in mine), mine
    assert pipeline.server.prompt_count == 2, pipeline.server.prompt_count

    # 收件箱单一来源 + 用户动作可用
    items = pipeline.call("attention-list", {})["items"]
    mine_item = [i for i in items if i["task_id"] == TASK]
    assert len(mine_item) == 1, items
    assert set(mine_item[0]["allowed_actions"]) >= {"replan", "dismiss"}, mine_item
    assert not pipeline.task(TASK)["delivery"].get("pr_number")


# ---------------------------------------------------------------------------
# 场景 8：评审两轮 changes_requested 后 pass
# ---------------------------------------------------------------------------


def test_scenario_review_two_changes_requested_then_pass(pipeline):
    pipeline._adopt_and_plan()
    first = pipeline._execute()
    pipeline._monitor()
    assert pipeline.task(TASK)["status"] == "in_review"

    pipeline._review()                                  # 第 1 轮：要求修改
    value = pipeline.task(TASK)
    assert value["status"] == "needs_plan", value
    assert value["automation"]["fix_round"] == 1, value["automation"]

    pipeline._adopt_and_plan()
    second = pipeline._execute()
    assert second != first
    pipeline._monitor()
    pipeline._review()                                  # 第 2 轮：pass
    value = pipeline.task(TASK)
    assert value["status"] == "in_review", value
    # fix_round 只在 changes_requested 登记时递增；pass 不递增
    assert value["automation"]["fix_round"] == 1, value["automation"]
    commits = value["delivery"]["commits"]
    assert [c["execution_run_id"] for c in commits] == [first, second], commits

    value = _deliver_to_merge(pipeline)
    assert value["status"] == "done", value
    assert pipeline.server.prompt_count == 2, pipeline.server.prompt_count
    # 全程同分支、主检出未被动
    assert pipeline.primary == (
        _git(pipeline.repo, "rev-parse", "HEAD"),
        _git(pipeline.repo, "status", "--porcelain"))


# ---------------------------------------------------------------------------
# 场景 9：评审修复轮用尽 → needs-you
# ---------------------------------------------------------------------------


def _always_changes_requested(case):
    """删掉假评审计数文件：下次送审 n=1 → changes_requested（脚本会自增）。"""
    (case.root / "review-count").unlink(missing_ok=True)


def test_scenario_review_fix_rounds_exhausted_needs_you(pipeline):
    # 假评审持续 changes_requested（每次送审前删计数文件）
    pipeline._adopt_and_plan()
    rounds = []
    for expected_round in (1, 2):
        rounds.append(pipeline._execute())
        pipeline._monitor()
        _always_changes_requested(pipeline)
        pipeline._review()
        value = pipeline.task(TASK)
        assert value["automation"]["fix_round"] == expected_round, value["automation"]
        # changes_requested 已开同任务修复请求（needs_plan），直接再规划
        pipeline._adopt_and_plan()

    # 第三轮执行后再次送审：fix_round 已达上限 → fix_rounds_exhausted
    rounds.append(pipeline._execute())
    pipeline._monitor()
    _always_changes_requested(pipeline)
    pipeline._review()
    value = pipeline.task(TASK)
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "fix_rounds_exhausted", value
    assert value["automation"]["fix_round"] == ladder.MAX_FIX_ROUNDS, value["automation"]
    assert len(rounds) == 3 and len(set(rounds)) == 3, rounds
    assert pipeline.server.prompt_count == 3, pipeline.server.prompt_count

    # 收件箱单一来源 + 允许的动作（不越界自动修复）
    items = pipeline.call("attention-list", {})["items"]
    mine = [i for i in items if i["task_id"] == TASK]
    assert len(mine) == 1, items
    actions = set(mine[0]["allowed_actions"])
    assert {"replan", "review_recovery", "dismiss"} & actions, actions
    assert not pipeline.task(TASK)["delivery"].get("pr_number")


# ---------------------------------------------------------------------------
# 场景 16：take_over → resume_auto 仍能交付
# ---------------------------------------------------------------------------


def test_scenario_take_over_then_resume_auto_delivers(pipeline):
    pipeline.call("set-project-automation", {
        "project_id": PROJECT,
        "automation": {"enabled": True, "max_fix_rounds": 0}})
    pipeline._adopt_and_plan()
    pipeline._execute()
    pipeline._monitor()
    _always_changes_requested(pipeline)
    pipeline._review()
    value = pipeline.task(TASK)
    assert value["failure_code"] == "fix_rounds_exhausted", value

    # take_over：resolve dismiss → 人工接管（manual_takeover + 手动交付模式）
    pipeline.call("resolve", {"task_id": TASK, "action": "dismiss"})
    value = pipeline.task(TASK)
    assert value["automation"]["manual_takeover"] is True, value["automation"]
    assert value["delivery"]["mode"] == "manual", value["delivery"]
    events = pipeline.call("list-events", {"task_id": TASK, "limit": 50})["events"]
    assert any(e["kind"] == "automation_stopped" for e in events), events
    # 接管后引擎不再驱动：manual_takeover 任务不是 automation-managed
    takeover = decide.decide({"automation_managed": True,
                              "manual_takeover": True})
    assert takeover.action == decide.NOOP, takeover
    assert takeover.reason == "not_automation_managed", takeover
    summary = _reconcile(pipeline)
    mine = [a for a in summary["actions"] if a.get("task_id") == TASK]
    assert all(a.get("command") is None for a in mine), mine
    from kanpilot import auto_intake
    assert not any(TASK in p["adopted"]
                   for p in auto_intake.run_once(pipeline.db_path)["projects"])
    # 人工接管后自动交付被明确阻断（不伪造 PR/合并）
    delivery = pipeline.call("get-task", {"task_id": TASK})["task"]["delivery"]
    assert delivery.get("mode") == "manual", delivery
    assert not delivery.get("pr_number"), delivery

    # resume_auto 入口：allowed_actions 暴露 resume-auto。
    # restore-task{resume_auto:true} 是 resolve{action:"dismiss"} 的逆向入口
    # （计划 D4：manual_takeover → 可逆暂停）：清 manual_takeover、PR 身份齐全时
    # 把 delivery.mode 从 manual 回 pr、清 auto_advance_paused、落 automation_resumed。
    assert "resume-auto" in value["allowed_actions"], value["allowed_actions"]
    out = pipeline.call("restore-task", {"task_id": TASK, "resume_auto": True})
    assert out.get("ok") is True, out
    assert out.get("deduplicated") is False, out
    value = pipeline.task(TASK)
    assert value["auto_advance_paused"] in (0, False), value
    assert value["automation"]["manual_takeover"] is False, value["automation"]
    assert value["delivery"]["mode"] == "pr", value["delivery"]
    events = pipeline.call("list-events", {"task_id": TASK, "limit": 80})["events"]
    assert any(e["kind"] == "task_restored"
               and (e.get("detail") or {}).get("resume_auto")
               for e in events), events
    assert any(e["kind"] == "automation_resumed" for e in events), events

    # 接管已交回 → 引擎重新驱动该任务（is_automation_managed 恢复 True），
    # 这正是场景 16 要的 take_over → resume_auto → 自动交付闭环。
    summary = _reconcile(pipeline)
    mine = [a for a in summary["actions"] if a.get("task_id") == TASK]
    assert any(a.get("command") for a in mine), mine


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
