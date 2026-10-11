"""黄金路径与修复轮场景（scenarios.md 场景 1/5/6/7/8/9/16）。

复用 ``tests/test_auto_pipeline.py`` 的 v5 全链夹具（假 Kaneo + 假 Plus +
假 claude 规划/评审 + 假 gh + 真实本地 git）：把它的 ``TestAutoPipeline``
包成 pytest fixture（手动 setUp/tearDown + doCleanup），因此真实 pytest 与
``python3 -m pytest tests/rehearsal -q`` 的 unittest discover 兜底路径都能跑。

每个场景按 scenarios.md 断言真实 monitor 与自动补救；不由测试注入 verdict 或代调用 resolve。
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
    completed_turns = set()

    def plus(name, args):
        identity = (args.get("sessionId"), args.get("turnId"))
        if (name == "pi_turn_get" and identity in completed_turns
                and identity[0] != case.server.session_id):
            return {"structuredContent": {
                "sessionId": identity[0], "turnId": identity[1], "status": "completed"}}
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
            # Explicit fixture artifact, independent of the interpreter's
            # PYTHONDONTWRITEBYTECODE setting. Empty-diff rounds remain clean.
            if content is not None:
                cache = worktree / "tests" / "__pycache__"
                cache.mkdir(exist_ok=True)
                (cache / "fixture.pyc").write_bytes(b"rehearsal-build-artifact")
            case.server.messages = [{
                "id": "verify-" + str(case.round), "role": "toolResult",
                "toolName": "exec", "toolArgs": {"command": VERIFY},
                "toolResult": {"output": output, "exit_code": None if case.omit_exit else exit_code},
                "exitCode": None if case.omit_exit else exit_code,
                "turnId": case.server.execution_turn,
            }]
            # Scripted work is finished; expose the matching idle Plus state,
            # rather than the base fake's permanently running default.
            case.server.is_running = False
            completed_turns.add((case.server.session_id, case.server.execution_turn))
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
    _live(pipeline)
    _script_rounds(pipeline, {})
    pipeline.omit_exit = True  # transcript evidence is deliberately incomplete
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
    assert value["extra"]["engine_verdict"]["verification_ok"] is True
    _assert_commit_excludes_build_artifacts(pipeline, value)

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


def _assert_commit_excludes_build_artifacts(pipeline, view):
    """P1（3e8484f）：交付提交不含构建产物，且产物仍留在工作树。

    夹具显式放置 ``tests/__pycache__/fixture.pyc``；提交它们会把本机
    二进制差异带进分支（沙盒 PR add/add 冲突的根因）。过滤只改提交内容，
    不删工作树文件——两侧都钉。
    """
    from kanpilot import git_delivery as gd

    worktree = Path(view["delivery"]["worktree_path"])
    commits = view["delivery"]["commits"]
    assert commits, view["delivery"]
    for commit in commits:
        sha = commit.get("sha")
        assert sha, commit
        out = subprocess.run(
            ["git", "-C", str(worktree), "show", "--name-only", "--format=", sha],
            capture_output=True, text=True, timeout=30, check=True).stdout
        files = [line.strip() for line in out.splitlines() if line.strip()]
        assert files, f"交付提交无文件：{sha}"
        artifacts = [f for f in files if gd.is_build_artifact(f)]
        assert not artifacts, (
            f"交付提交含构建产物 {artifacts}（commit {sha[:8]}）")
    assert (worktree / "tests" / "__pycache__").exists(), (
        f"工作树应残留 verify 产物（过滤不删文件）：{worktree}")


# ---------------------------------------------------------------------------
# 场景 5：空 diff → 有界补救 → attention
# ---------------------------------------------------------------------------


def _live(case):
    case.call("set-project-engine-mode", {"project_id": PROJECT, "engine_mode": "live"})


def _reconcile(case, mode="live", intake=False):
    from kanpilot.engine import loop
    def run(command, payload):
        try:
            return case.call(command, payload)
        except util.KanpilotError as exc:
            print("engine command failed:", command, exc.code, util.redact(exc.message))
            raise
    return loop.Reconciler(case.db_path, mode=mode, enable_intake=intake,
                           run_command=run).reconcile_once()


def _automatic_plan(case):
    from kanpilot import planner_runner
    _reconcile(case, intake=True)  # engine intake prepares the repair plan request
    assert planner_runner.run_one(case.db_path), case.task(TASK)
    assert case.task(TASK)["status"] == "ready", case.task(TASK)
    _reconcile(case)  # automatic confirmation, no fixture resolve


def _assert_automatic_remediation(case):
    events = case.call("list-events", {"task_id": TASK, "limit": 200})["events"]
    fixes = [e for e in events if e["kind"] == "resolved"
             and (e.get("detail") or {}).get("action") in ("resume", "replan")]
    assert fixes, events
    assert all(e["actor"] == "automation" for e in fixes), fixes
    assert not [e for e in events if e["actor"] == "user"], events
    rounds = case.task(TASK)["extra"].get("engine_fix_rounds") or []
    assert len({r["execution_run_id"] for r in rounds}) == len(rounds), rounds
    assert len(rounds) <= 2, rounds


def test_scenario_empty_diff_backup_model_then_attention(pipeline):
    _live(pipeline)
    pipeline.call("set-project-fallback-model", {
        "project_id": PROJECT,
        "fallback_model": {"provider_id": "test-provider", "model_id": "test-model"}})
    pipeline._adopt_and_plan()
    plan_sha = pipeline.task(TASK)["plan_sha256"]
    _script_rounds(pipeline, {1: None, 2: None})
    first = pipeline._execute()
    pipeline._monitor()  # monitor itself produces the bound core verdict
    value = pipeline.task(TASK)
    assert value["extra"]["engine_verdict"]["empty_diff"] is True, value
    assert not value["delivery"].get("commits"), value
    summary = _reconcile(pipeline)
    assert any(a.get("task_id") == TASK and a["action"] == "start_fix_round"
               for a in summary["actions"]), summary
    value = pipeline.task(TASK)
    assert value["status"] == "ready", util.jdump({"task": value, "summary": summary})
    assert value["plan_sha256"] == plan_sha, value
    second = pipeline._execute()
    assert first != second
    pipeline._monitor()
    _reconcile(pipeline)
    value = pipeline.task(TASK)
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "fix_rounds_exhausted", value
    assert pipeline.server.prompt_count == 2
    assert len(value["extra"]["engine_fix_rounds"]) == 1, value
    assert value["extra"]["engine_fix_rounds"][0]["model"] == "fallback", value
    assert not value["delivery"].get("commits")
    assert not value["delivery"].get("pr_number")
    assert any(i["task_id"] == TASK for i in pipeline.call("attention-list", {})["items"])
    _assert_automatic_remediation(pipeline)
    _reconcile(pipeline)  # repeated ticks must not start another dispatch
    assert pipeline.server.prompt_count == 2


# ---------------------------------------------------------------------------
# 场景 6：真实 monitor verify 失败 → 自动同分支修复 → done
# ---------------------------------------------------------------------------


def test_scenario_verify_fail_fix_round_passes(pipeline):
    _live(pipeline)
    pipeline._adopt_and_plan()
    _script_rounds(pipeline, {1: "oops"})
    first = pipeline._execute()
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["extra"]["engine_verdict"]["verification_ok"] is False, value
    branch = value["delivery"]["worktree_path"]
    _reconcile(pipeline)
    assert pipeline.task(TASK)["status"] == "needs_plan", pipeline.task(TASK)
    _automatic_plan(pipeline)
    second = pipeline._execute()
    assert second != first
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "in_review", value
    assert value["delivery"]["worktree_path"] == branch
    assert len(value["delivery"]["commits"]) == 1
    (pipeline.root / "review-count").write_text("2")
    pipeline._review()
    assert _deliver_to_merge(pipeline)["status"] == "done"
    _assert_automatic_remediation(pipeline)


# ---------------------------------------------------------------------------
# 场景 7：连续 verify 失败 → 任务累计上限（1+2 轮）
# ---------------------------------------------------------------------------


def test_scenario_verify_fail_exhausted_needs_you(pipeline):
    _live(pipeline)
    pipeline._adopt_and_plan()
    _script_rounds(pipeline, {1: "oops", 2: "oops", 3: "oops"})
    run_ids = []
    for round_no in range(3):
        run_ids.append(pipeline._execute())
        pipeline._monitor()
        _reconcile(pipeline)
        if round_no < 2:
            _automatic_plan(pipeline)
    value = pipeline.task(TASK)
    assert len(set(run_ids)) == 3
    assert value["status"] == "needs_user", value
    assert value["failure_code"] == "fix_rounds_exhausted", value
    assert pipeline.server.prompt_count == 3
    assert len(value["extra"]["engine_fix_rounds"]) == 2, value
    _assert_automatic_remediation(pipeline)
    _reconcile(pipeline)
    assert pipeline.server.prompt_count == 3
    items = pipeline.call("attention-list", {})["items"]
    mine = [i for i in items if i["task_id"] == TASK]
    assert len(mine) == 1, items
    assert "replan" in mine[0]["allowed_actions"]
    assert not value["delivery"].get("pr_number")


# ---------------------------------------------------------------------------
# 场景 8：评审两轮 changes_requested 后 pass
# ---------------------------------------------------------------------------


def test_scenario_review_two_changes_requested_then_pass(pipeline):
    _script_rounds(pipeline, {})
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
    _assert_commit_excludes_build_artifacts(pipeline, value)

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
    assert mine and any(a.get("command") for a in mine), mine

    # 幂等：第二次 resume_auto 不重复落 automation_resumed 事件
    pipeline.call("restore-task", {"task_id": TASK, "resume_auto": True})
    events_again = pipeline.call("list-events",
                                 {"task_id": TASK, "limit": 80})["events"]
    assert len([e for e in events_again
                if e["kind"] == "automation_resumed"]) == 1, events_again

    # 仍能交付：评审 pass → PR → 合并 → done（同分支续走）
    (pipeline.root / "review-count").write_text("2")
    pipeline._review()
    value = _deliver_to_merge(pipeline)
    assert value["status"] == "done", value
    assert pipeline.fake.tasks[TASK]["status"] == "done"


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])


def test_scenario_review_tracked_build_artifact_is_accepted(pipeline):
    """A3: actual CLI review leaves a tracked artifact dirty and still settles."""
    _live(pipeline)
    artifact = pipeline.repo / "tracked.pyc"
    artifact.write_bytes(b"committed baseline cache")
    _git(pipeline.repo, "add", "tracked.pyc")
    _git(pipeline.repo, "commit", "-m", "tracked cache fixture baseline")
    _git(pipeline.repo, "push", "origin", "main")  # disposable local bare remote
    pipeline.primary = (_git(pipeline.repo, "rev-parse", "HEAD"),
                        _git(pipeline.repo, "status", "--porcelain"))
    claude = pipeline.root / "fake-claude"
    script = claude.read_text().replace(
        'if "conclusion" in schema["properties"]:\n',
        'if "conclusion" in schema["properties"]:\n'
        ' pathlib.Path("tracked.pyc").write_bytes(b"review regenerated cache")\n')
    claude.write_text(script)
    (pipeline.root / "review-count").write_text("2")
    _script_rounds(pipeline, {})
    pipeline._adopt_and_plan()
    pipeline._execute()
    pipeline._monitor()
    value = pipeline.task(TASK)
    assert value["status"] == "in_review", value
    head = value["delivery"]["head_sha"]
    pipeline._review()
    value = pipeline.task(TASK)
    assert value["extra"]["review_conclusion"] == "pass", value
    snapshot = pipeline.root / "trees" / TASK / ("review-" + head[:12])
    assert _git(snapshot, "status", "--porcelain") == "M tracked.pyc"
    assert _git(snapshot, "rev-parse", "HEAD") == head
    assert _git(snapshot, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _deliver_to_merge(pipeline)["status"] == "done"
