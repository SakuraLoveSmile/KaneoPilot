"""引擎/调度演练场景（scenarios.md 场景 10/11/12/17/18/19/20；引擎门控）。

覆盖规格条款：state-model §8（失败阶梯/退避/预算门）、§5（串行位/容量）、§7；
protocol §5.27.1（租约+心跳+退避重启+失联判定）、§5.27.2（engine status）、
§5.27.3（attention-list 契约）、§5.12（migrate 备份/幂等/不隐式升级）、§4.3
（失联派生）、§4.10；runtime_operations 回执契约（§5.25）；automation-contract
§调度容量/§计划自动确认（预算门）。

驱动方式：决策层直调（``kanpilot.engine.decide``/``ladder`` 纯函数）；DB 层经
``tests/conftest`` 的 factory/cli/DB 后门 + 内进程 ``kanpilot.engine.loop.Reconciler``
（真实 DB，不 sleep，时间用租约后门）。所有场景只写隔离临时库。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_PARENT)
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

# 注册演练 conftest 的 fixture（unittest discover 兜底路径不会自动加载）。
try:
    from tests.rehearsal import conftest as _rehearsal_conftest  # noqa: F401,E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    import conftest as _rehearsal_conftest  # noqa: F401,E402

# tests/conftest 导入时已把 core/ 插入 sys.path（KANPILOT_CORE_BIN 约定）。
from kanpilot import auto_intake, core as kcore, db as kdb, util as kutil  # noqa: E402
from kanpilot import runtime_operations  # noqa: E402
from kanpilot.engine import attention as kattention  # noqa: E402
from kanpilot.engine import decide as kdecide  # noqa: E402
from kanpilot.engine import health as khealth  # noqa: E402
from kanpilot.engine import ladder as kladder  # noqa: E402
from kanpilot.engine import loop as kloop  # noqa: E402

try:
    from test_migration import _migrate  # noqa: E402
except ImportError:  # 包上下文
    from tests.test_migration import _migrate  # noqa: E402

try:
    from conftest import make_due  # noqa: E402
except ImportError:  # 包上下文
    from tests.conftest import make_due  # noqa: E402

# ---------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------


def _connect(db_path):
    return kdb.connect(Path(db_path))


def _adopt(db_path, task_id, **fields):
    """把任务标记为自动化管理（引擎只驱动 automation-managed 任务）。"""
    conn = _connect(db_path)
    try:
        with kdb.write_txn(conn):
            task = kdb.require_task(conn, task_id)
            base = {"adopted_at": kutil.now_iso(), "manual_takeover": False,
                    "plan_wanted": False, "plan_attempts": 0,
                    "segment_started_at": kutil.now_iso(), "cost_usd": 0}
            base.update(fields)
            kcore.update_automation(conn, task, kutil.now_iso(), **base)
    finally:
        conn.close()


def _count_requests(db_path, task_id, kind, statuses):
    if isinstance(statuses, str):
        statuses = (statuses,)
    placeholders = ",".join("?" * len(statuses))
    conn = _connect(db_path)
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM requests WHERE task_id=? AND kind=? "
            f"AND status IN ({placeholders})",
            (task_id, kind, *statuses)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 场景 10：transient_backoff_capped_then_escalate
# ---------------------------------------------------------------------------


def test_scenario_transient_backoff_capped_then_escalate(cli, factory, tmp_env):
    """瞬时失败阶梯 1：指数退避重试真实执行 → 封顶守卫停止重试。

    执行层已验证：瞬时失败 → 引擎重新入队（退避 not_before + actor=automation）。
    已知缺口（留痕报 engine_builder）：重入队以新请求承载、attempt_no 归零
    重计，≤2/阶段 的封顶在实时链路不跨请求累积；此处封顶断言以后门构造的
    attempt_no 达上限失败请求钉住守卫本身。"""
    # 决策层（规格阶梯表）
    transient = ["remote_unavailable", "kaneo_timeout", "kaneo_500",
                 "network_timeout", "git_probe_timeout"]
    for code in transient:
        assert kladder.classify(code) == kladder.LADDER_TRANSIENT == 1, code
    d0 = kdecide.failure_decision("kaneo_timeout", attempt_no=0)
    assert d0.action == kdecide.RETRY
    assert d0.params["backoff_seconds"] == 1.0 and d0.params["attempt_no"] == 1
    d1 = kdecide.failure_decision("kaneo_timeout", attempt_no=1)
    assert d1.action == kdecide.RETRY
    assert d1.params["backoff_seconds"] == 2.0 and d1.params["attempt_no"] == 2
    d2 = kdecide.failure_decision("kaneo_timeout", attempt_no=2)
    assert d2.action == kdecide.BLOCK
    assert d2.reason == "transient_exhausted"
    assert d2.ladder == kladder.escalate(1) == kladder.LADDER_ENVIRONMENTAL == 2
    assert d2.attention is True
    assert kladder.MAX_TRANSIENT_RETRIES == 2
    # 环境类（阶梯 2）不自动重试
    for code in ("not_git_repo", "gh_missing"):
        assert kladder.classify(code) == 2, code
        assert kladder.auto_retry_allowed(2) is False

    def _transient_failure(cli, request_id, stage):
        # 定点领取（带 request_id）跳过 not_before 冷却期（protocol §4.2-4）。
        executor = "kaneo-monitor" if stage == "monitor" else "kaneo-dispatch"
        claimed = cli("claim", {"stage": stage, "executor": executor,
                                "request_id": request_id})
        assert claimed["claimed"] is True, claimed
        cli("report", {"request_id": claimed["request_id"],
                       "run_id": claimed["run_id"], "outcome": "failed",
                       "failure_code": "remote_unavailable",
                       "failure_reason": "Kaneo 瞬时不可达（演练注入）"})
        return claimed

    def _queued(reqs, tid, kind):
        return [r for r in reqs if r["task_id"] == tid and r["kind"] == kind
                and r["status"] == "queued"]

    # 执行层 A（monitor kind）：R1→R2 退避重试（retry_base 跨请求累积），
    # R3 封顶——连续第三次瞬时失败不再重试、不产生新请求（§8 ≤2/阶段）。
    running = factory.make_running()
    tid = running.task.task_id
    _adopt(tmp_env.db, tid)
    monitor = factory.active_request(tid, kind="monitor")
    assert monitor is not None, "running 任务应有 monitor 请求"
    _transient_failure(cli, monitor["request_id"], "monitor")
    summary = kloop.Reconciler(tmp_env.db, mode="live").reconcile_once()
    retry_actions = [a for a in summary["actions"]
                     if a["task_id"] == tid and a["action"] == kdecide.RETRY]
    assert retry_actions and all(a.get("executed") is True
                                 for a in retry_actions), summary["actions"]
    reqs = cli("list-requests", {})["requests"]
    retried = _queued(reqs, tid, "monitor")
    assert len(retried) == 1, reqs
    assert retried[0]["requested_by"] == "automation", retried[0]
    assert kutil.ts_before(kutil.now_iso(), retried[0]["not_before"]), retried[0]
    payload = cli("get-request",
                  {"request_id": retried[0]["request_id"]})["payload"]
    assert payload.get("retry_base") == 1, payload

    # R2：领回重试请求再失败 → 再次退避重试，retry_base 跨请求累积为 2
    # （0a093bc：retry_base 随请求 payload 落库，_transient_retry 读它）。
    make_due(tmp_env.db, retried[0]["request_id"])
    _transient_failure(cli, retried[0]["request_id"], "monitor")
    summary = kloop.Reconciler(tmp_env.db, mode="live").reconcile_once()
    retry_actions = [a for a in summary["actions"]
                     if a["task_id"] == tid and a["action"] == kdecide.RETRY]
    assert retry_actions and all(a.get("executed") is True
                                 for a in retry_actions), summary["actions"]
    reqs = cli("list-requests", {})["requests"]
    retried2 = _queued(reqs, tid, "monitor")
    assert len(retried2) == 1, reqs
    payload2 = cli("get-request",
                   {"request_id": retried2[0]["request_id"]})["payload"]
    assert payload2.get("retry_base") == 2, payload2

    # R3：第三次瞬时失败 → 封顶（≤2/阶段）：不再重试、不产生新请求；
    # 失败原因保留在失败请求上，等待用户/环境修复。
    make_due(tmp_env.db, retried2[0]["request_id"])
    _transient_failure(cli, retried2[0]["request_id"], "monitor")
    summary = kloop.Reconciler(tmp_env.db, mode="live").reconcile_once()
    mine3 = [a for a in summary["actions"] if a["task_id"] == tid]
    assert not [a for a in mine3 if a["action"] == kdecide.RETRY], mine3
    reqs = cli("list-requests", {})["requests"]
    assert not _queued(reqs, tid, "monitor"), reqs
    assert [r for r in reqs if r["task_id"] == tid and r["kind"] == "monitor"
            and r["status"] == "failed"], reqs

    # 执行层 B（dispatch kind）：dispatching 状态瞬时失败 → 退避重入队放行
    # （retry_base 为凭据）；普通 enqueue 从 dispatching 仍被拒。
    ready = factory.make_ready()
    dtid = ready.task.task_id
    _adopt(tmp_env.db, dtid)
    factory.enqueue_dispatch(dtid)
    dclaimed = cli("claim", {"stage": "dispatch",
                             "executor": "kaneo-dispatch"})
    assert dclaimed["claimed"] is True, dclaimed
    cli("report", {"request_id": dclaimed["request_id"],
                   "run_id": dclaimed["run_id"], "outcome": "failed",
                   "failure_code": "remote_unavailable",
                   "failure_reason": "Kaneo 瞬时不可达（演练注入）"})
    plain = cli("enqueue", {"task_id": dtid, "kind": "dispatch"}, expect=1)
    assert plain["code"] == "bad_request", plain
    summary = kloop.Reconciler(tmp_env.db, mode="live").reconcile_once()
    dretry = [a for a in summary["actions"]
              if a["task_id"] == dtid and a["action"] == kdecide.RETRY]
    assert dretry and all(a.get("executed") is True
                          for a in dretry), summary["actions"]
    reqs = cli("list-requests", {})["requests"]
    dqueued = _queued(reqs, dtid, "dispatch")
    assert len(dqueued) == 1, reqs
    assert dqueued[0]["requested_by"] == "automation", dqueued[0]
    assert kutil.ts_before(kutil.now_iso(), dqueued[0]["not_before"]), dqueued[0]
    dpayload = cli("get-request",
                   {"request_id": dqueued[0]["request_id"]})["payload"]
    assert dpayload.get("retry_base") == 1, dpayload

    # 升级面：needs_user 任务 → BLOCK、无命令执行、带 explain；shadow 不写库。
    blocked = factory.make_needs_user()
    btid = blocked.task.task_id
    _adopt(tmp_env.db, btid)
    before = _count_requests(tmp_env.db, btid, "dispatch",
                             ("queued", "claimed"))
    live = kloop.Reconciler(tmp_env.db, mode="live").reconcile_once()
    live_mine = [a for a in live["actions"] if a["task_id"] == btid]
    assert len(live_mine) == 1, live_mine
    assert live_mine[0]["action"] == kdecide.BLOCK, live_mine
    assert live_mine[0]["command"] is None, live_mine
    assert live_mine[0].get("explain"), live_mine
    shadow = kloop.Reconciler(tmp_env.db, mode="shadow").reconcile_once()
    assert all(a.get("executed") is not True for a in shadow["actions"]), shadow
    assert _count_requests(tmp_env.db, btid, "dispatch",
                           ("queued", "claimed")) == before


# ---------------------------------------------------------------------------
# 场景 11：engine_killed_at_step_no_duplicate_effects
# ---------------------------------------------------------------------------


def test_scenario_engine_killed_at_step_no_duplicate_effects(factory, tmp_env):
    """回执幂等 + 引擎重启不重复副作用（协议 §5.27.1 / runtime_operations）。"""
    scope, op = "scope-a", str(uuid.uuid4())
    payload = {"kind": "dispatch", "task_id": "t1"}
    conn = _connect(tmp_env.db)
    try:
        first = runtime_operations.begin_operation(
            conn, scope, op, "harness-1", "kp_task_claim", payload)
        assert first["status"] == "prepared"
        second = runtime_operations.begin_operation(
            conn, scope, op, "harness-1", "kp_task_claim", payload)
        assert second["status"] == first["status"]  # 既有回执
        n = conn.execute(
            "SELECT COUNT(*) FROM runtime_operations WHERE scope_id=? AND operation_id=?",
            (scope, op)).fetchone()[0]
        assert n == 1, "同 (scope,op,payload) 不得新增行"
        with pytest.raises(kutil.KanpilotError) as exc:
            runtime_operations.begin_operation(
                conn, scope, op, "harness-1", "kp_task_claim",
                {"kind": "dispatch", "task_id": "OTHER"})
        assert exc.value.code == "conflict"
    finally:
        conn.close()

    # 引擎杀裂恢复：ready 自动化任务，两个 run 各 1 tick，第二次入队 skipped。
    ready = factory.make_ready()
    tid = ready.task.task_id
    _adopt(tmp_env.db, tid)
    conn = _connect(tmp_env.db)
    try:
        with kdb.write_txn(conn):
            task = kdb.require_task(conn, tid)
            kdb.set_extra(conn, task, kutil.now_iso(), plan_execution_hold=True)
    finally:
        conn.close()

    r1 = kloop.Reconciler(tmp_env.db, mode="live").run(
        instance_id=str(uuid.uuid4()), max_ticks=1, sleeper=lambda s: None)
    assert r1["ticks"] == 1
    dispatched = _count_requests(tmp_env.db, tid, "dispatch", ("queued", "claimed"))
    assert dispatched == 1, "首个 tick 应入队一个 dispatch 请求"

    r2 = kloop.Reconciler(tmp_env.db, mode="live").run(
        instance_id=str(uuid.uuid4()), max_ticks=1, sleeper=lambda s: None)
    assert r2["ticks"] == 1
    assert _count_requests(tmp_env.db, tid, "dispatch",
                           ("queued", "claimed")) == 1, "重启不得重复入队"

    # 租约释放后无活跃引擎实例（health.active_instance_count）。
    conn = _connect(tmp_env.db)
    try:
        assert khealth.active_instance_count(conn, kutil.now_dt()) == 0
    finally:
        conn.close()

    # 单实例租约 + 失联接管：活跃未过期租约拒绝第二引擎；把 lease_expires_at
    # 经后门推到过去（模拟引擎被杀、无心跳）→ 过期租约可被新实例接管（§5.27.1）。
    from datetime import timedelta
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    conn = _connect(tmp_env.db)
    try:
        khealth.acquire_or_renew_lease(conn, kutil.now_iso(), instance_id=a,
                                       stage=None, pid=1, lease_seconds=60)
        with pytest.raises(kutil.KanpilotError) as exc:
            khealth.acquire_or_renew_lease(conn, kutil.now_iso(), instance_id=b,
                                           stage=None, pid=2, lease_seconds=60)
        assert exc.value.code == "conflict"
        with kdb.write_txn(conn):
            instances = kdb.get_setting(conn, "executor_instances")
            instances[a]["lease_expires_at"] = (
                kutil.now_dt() - timedelta(seconds=120)).isoformat()
            kdb.set_setting(conn, "executor_instances", instances)
        khealth.acquire_or_renew_lease(conn, kutil.now_iso(), instance_id=b,
                                       stage=None, pid=2, lease_seconds=60)
        assert khealth.active_instance_count(conn, kutil.now_dt()) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 场景 12：lease_expired_per_step_reclaim
# ---------------------------------------------------------------------------


def test_scenario_lease_expired_per_step_reclaim(factory, tmp_env, cli):
    """claimed 请求租约过期 → 读取层派生失联，不自动释放/重启；release_lost 解除。

    构造：ready → enqueue dispatch → claim dispatch（不 confirm）→ 任务
    ``dispatching``（dispatch 中途）+ claimed dispatch 请求。这是唯一能用 CLI
    表达「running/中途态 + claimed 请求」的形态（``make_running`` 会 confirm 并
    结束 dispatch 请求，无 claimed 请求可过期）。dispatch 中途失联 → 释放后
    needs_user（protocol §5.27.4 / state-model §7）。
    """
    from conftest import expire_lease  # noqa: E402

    ready = factory.make_ready()
    tid = ready.task.task_id
    factory.enqueue_dispatch(tid)
    req = factory.find_request(tid, "dispatch")
    claim = factory.claim("dispatch", request_id=req["request_id"])
    req_id = claim["request_id"]
    # 引擎只驱动自动化任务：标记为自动化管理后，reconciler 才会考虑重新入队。
    _adopt(tmp_env.db, tid)

    # 无活跃引擎实例
    assert cli("engine-status", {})["instances"] == []

    expire_lease(tmp_env.db, req_id, seconds=3600)

    # 读取层派生失联（不自动释放/重启）
    task = cli("get-task", {"task_id": tid})["task"]
    assert task["display_status"] == "执行端失联", task["display_status"]
    # DB 中请求仍为 claimed（不自动释放）
    rows = _connect(tmp_env.db).execute(
        "SELECT status FROM requests WHERE request_id=?", (req_id,)).fetchall()
    assert rows and rows[0][0] == "claimed", rows

    # 引擎不重复入队（已有 claimed 失联请求 → already_scheduled）
    before = _count_requests(tmp_env.db, tid, "dispatch", ("queued", "claimed"))
    kloop.Reconciler(tmp_env.db, mode="live").reconcile_once()
    assert _count_requests(tmp_env.db, tid, "dispatch",
                           ("queued", "claimed")) == before, "不得重复领取"

    # resolve release_lost → 请求 cancelled、失联解除（dispatch 中途 → needs_user）
    cli("resolve", {"task_id": tid, "action": "release_lost"})
    rows = _connect(tmp_env.db).execute(
        "SELECT status FROM requests WHERE request_id=?", (req_id,)).fetchall()
    assert rows and rows[0][0] == "cancelled", rows
    task = cli("get-task", {"task_id": tid})["task"]
    assert task["display_status"] != "执行端失联", task["display_status"]


# ---------------------------------------------------------------------------
# 场景 17：budget_and_time_cap_triggered
# ---------------------------------------------------------------------------


def test_scenario_budget_and_time_cap_triggered(factory, tmp_env, cli):
    """预算门：段成本/时长超限 → BLOCK auto_budget_exceeded（阶梯 4）进 needs-you。"""
    # 决策层：(plan, ready) 的 CONFIRM_PLAN 受预算门；budget_ok=False → BLOCK。
    snap = {"automation_managed": True, "status": "ready", "budget_ok": False}
    d = kdecide.decide(snap)
    assert d.action == kdecide.BLOCK
    assert d.reason == "auto_budget_exceeded"
    assert d.ladder == kladder.LADDER_NEEDS_YOU == 4
    assert d.attention is True
    ok = kdecide.decide({"automation_managed": True, "status": "ready",
                         "budget_ok": True})
    assert ok.action == kdecide.CONFIRM_PLAN

    # DB 层：项目设小 max_task_cost_usd，任务 record-cost 超限 → 预算门 not ok。
    running = factory.make_running()
    tid = running.task.task_id
    project_id = running.task.project_id
    cli("set-project-automation", {"project_id": project_id,
                                   "automation": {"enabled": True,
                                                  "max_task_cost_usd": 0.5}})
    # 先纳入自动化管理，再经 record-cost 登记超额成本（真实命令路径）。
    _adopt(tmp_env.db, tid)
    cli("record-cost", {"task_id": tid,
                        "request_id": running.dispatch_request_id,
                        "run_id": running.dispatch_run_id,
                        "stage": "dispatch", "cost_usd": 100.0,
                        "source": "rehearsal"})
    # 内进程预算核验：段成本 100 >= 上限 0.5 → budget not ok → decide BLOCK。
    conn = _connect(tmp_env.db)
    try:
        task = kdb.require_task(conn, tid)
        from kanpilot import automation_policy
        policy = automation_policy.policy_for(conn, project_id)
        budget = kcore.automation_budget(task, policy, kutil.now_dt())
        assert budget["ok"] is False, budget
    finally:
        conn.close()
    snap2 = {"automation_managed": True, "status": "ready", "budget_ok": False}
    assert kdecide.decide(snap2).reason == "auto_budget_exceeded"
    actions = set(cli("get-task", {"task_id": tid})["task"]["allowed_actions"])
    assert {"resume", "replan", "mark_done", "dismiss"} & actions, actions


# ---------------------------------------------------------------------------
# 场景 18：concurrency_two_slots_four_tasks_no_deadlock
# ---------------------------------------------------------------------------


def test_scenario_concurrency_two_slots_four_tasks_no_deadlock(
        factory, tmp_env, cli, fake_kaneo, monkeypatch):
    """max_active_tasks=2：4 个 to-do 只采纳 ≤2，其余留 to-do 无 task 行；无死锁。"""
    from kanpilot import (automation_policy, bridge as kbridge,
                          git_delivery, planner_inputs, remote as kremote)

    from conftest import PROJECTS  # noqa: E402
    project = "StudyTool"
    project_id = PROJECTS[project]
    repository = tmp_env.repos_root / project
    repository.mkdir(parents=True, exist_ok=True)
    # 真 git 仓库 + GitHub 形状 origin（rehearsal_git 同构），供 preflight/
    # fetch-todos 的 repository 可用性判定。
    import subprocess
    for args in (("init", "-b", "main"),
                 ("config", "user.name", "F"), ("config", "user.email", "f@x.invalid"),
                 ("commit", "--allow-empty", "-m", "base"),
                 ("remote", "add", "origin",
                  "https://github.com/owner/repo.git")):
        subprocess.run(["git", "-C", str(repository), *args],
                       check=True, capture_output=True, timeout=30)

    cli("set-automation-global", {"max_active_tasks": 2})
    cli("set-project-mapping", {"project_id": project_id,
                                "repository": str(repository),
                                "enabled": True, "expected_revision": 0})
    cli("set-project-automation", {"project_id": project_id,
                                   "automation": {"enabled": True}})

    # 环境就绪门打洞（本场景只测容量/串行，不测 preflight 本体）。
    monkeypatch.setattr(automation_policy, "automation_preflight",
                        lambda c, p: [])
    monkeypatch.setattr(git_delivery, "prepare_branch_worktree",
                        lambda repo, tid, **kw: {
                            "branch": "kaneo/" + tid, "worktree_path": str(repo),
                            "base_branch": "main", "base_sha": "a" * 40,
                            "head_sha": "a" * 40})

    # 假 Kaneo 造 4 个 to-do 任务；经 bridge.task_context 逐条取需求指纹。
    # （rehearsal fake_kaneo 的 /api/task/tasks/<pid> 返回原始列表，而
    # bridge.tasks_for_status 期望 {data:{columns}} 看板形状，故 fetch-todos
    # 的列表枚举在此假件上不可用；按 core/tests/test_auto_intake.py 同 seam 经
    # planner_inputs.fetch_todos 注入等价的 to-do 条目，采纳/容量逻辑仍为真。）
    client = kremote.get_client()
    items = []
    for i in range(4):
        rt = fake_kaneo.add_task(project_id, f"并发任务 {i}", f"需求 {i}",
                                 status="to-do")
        ctx = kbridge.task_context(client, rt["id"])
        items.append({
            "task_id": rt["id"], "project_id": project_id,
            "project_name": project, "title": f"并发任务 {i}",
            "requirements_sha256": ctx["requirements_sha256"],
            "source_sha256": ctx["source_sha256"], "kaneo_status": "to-do",
            "selectable": True, "number": i + 1,
            "created_at": rt["createdAt"]})
    monkeypatch.setattr(planner_inputs, "fetch_todos",
                        lambda conn: {"items": items})

    summary = auto_intake.run_once(tmp_env.db)
    adopted = [t for p in summary.get("projects", []) for t in (p.get("adopted") or [])]
    assert len(adopted) == 2, f"容量=2 应恰好采纳 2：{summary}"
    # 采纳者落 task 行；未采纳者留在 Kaneo to-do 且无 task 行
    conn = _connect(tmp_env.db)
    try:
        task_rows = {r[0] for r in conn.execute("SELECT task_id FROM tasks")}
    finally:
        conn.close()
    assert set(adopted) <= task_rows, (adopted, task_rows)
    for it in items:
        if it["task_id"] not in adopted:
            assert it["task_id"] not in task_rows, it["task_id"]
            assert fake_kaneo.tasks[it["task_id"]]["status"] == "to-do"

    # dispatch 串行位（state-model §5）：两个 ready 任务同时至多一个 claimed。
    r_a = factory.make_ready(title="串行A")
    r_b = factory.make_ready(title="串行B")
    factory.enqueue_dispatch(r_a.task.task_id)
    factory.enqueue_dispatch(r_b.task.task_id)
    c1 = factory.claim("dispatch")
    assert c1["claimed"] is True, c1
    c2 = factory.claim("dispatch")
    assert c2["claimed"] is False and c2.get("reason") == "stage_busy", c2
    claimed = [r for r in cli("list-requests", {})["requests"]
               if r["kind"] == "dispatch" and r["status"] == "claimed"]
    assert len(claimed) == 1, f"同时至多一个 claimed dispatch：{claimed}"


# ---------------------------------------------------------------------------
# 场景 19：migration_v5_to_v6_gc_reversible
# ---------------------------------------------------------------------------


def test_scenario_migration_v5_to_v6_gc_reversible(cli, tmp_env, legacy_state):
    """migrate 备份/幂等/dry_run 契约；v6 升级与 GC 引擎缺能力（SCHEMA_VERSION=5）。"""
    import hashlib
    from kanpilot import constants

    assert constants.SCHEMA_VERSION == 5, "当前 schema 版本应为 5（v6 未实现）"

    before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
              for p in (legacy_state.executor_manifest,
                        legacy_state.planner_manifest,
                        legacy_state.reviewer_manifest)}
    out = _migrate(cli, tmp_env)
    assert out["ok"] is True
    backup = Path(out["backup_path"])
    assert backup.exists()
    copied = list(backup.rglob("manifest.json"))
    assert len(copied) >= 3, f"备份应含三 manifest：{list(backup.rglob('*'))}"
    for p, h in before.items():
        assert hashlib.sha256(Path(p).read_bytes()).hexdigest() == h, p
    # 版本仍为 5
    conn = _connect(tmp_env.db)
    try:
        version = conn.execute("SELECT version FROM schema_meta").fetchone()[0]
    finally:
        conn.close()
    assert version == 5

    tasks1 = cli("list-tasks", {})["tasks"]
    reqs1 = cli("list-requests", {})["requests"]
    out2 = _migrate(cli, tmp_env)
    assert out2["ok"] is True
    assert len(cli("list-tasks", {})["tasks"]) == len(tasks1)
    assert len(cli("list-requests", {})["requests"]) == len(reqs1)


def test_migration_no_implicit_upgrade(cli, tmp_env):
    """普通命令对旧库/不明库报 migration_needed，不隐式升级（§5.12）。

    场景 19 的支撑断言（非独立场景）：SCHEMA_VERSION=5，缺版本/版本过低的库
    经普通命令拒绝并报 ``migration_needed``，只有显式 ``migrate`` 才允许升级。
    """
    import sqlite3
    # 先建一个 v5 库，再把版本改低 → 普通命令应拒绝（不升级）。
    cli("overview", {})
    conn = sqlite3.connect(str(tmp_env.db))
    try:
        conn.execute("UPDATE schema_meta SET version=4")
        conn.commit()
    finally:
        conn.close()
    out = cli("list-tasks", {}, expect=1)
    assert out["code"] == "migration_needed", out

    # 缺版本（有表但无 schema_meta 版本行）→ 同样拒绝，不隐式初始化/升级。
    bogus = tmp_env.root / "no-version.db"
    conn = sqlite3.connect(str(bogus))
    try:
        conn.execute("CREATE TABLE tasks(task_id TEXT)")
        conn.commit()
    finally:
        conn.close()
    out2 = cli("list-tasks", {}, db=bogus, expect=1)
    assert out2["code"] == "migration_needed", out2


# ---------------------------------------------------------------------------
# 场景 20：attention_list_contract_snapshot（protocol §5.27.3 冻结规格）
# ---------------------------------------------------------------------------

# 单一来源 attention.ITEM_FIELDS 的 15 字段（顺序即规格顺序）。
ATTENTION_ITEM_FIELDS = (
    "attention_id", "task_id", "project_name", "title", "reason_code",
    "failure_code", "failure_stage", "failure_reason", "evidence",
    "blocked_summary", "recommended", "alternatives", "allowed_actions",
    "attention_state", "since",
)

# §5.27.3 冻结的 13 个 reason_code 枚举。
REASON_CODES = (
    "needs_plan_input", "verify_unconfirmed", "review_changes_exhausted",
    "delivery_blocked", "remote_unavailable", "plus_unreachable",
    "plus_send_unknown", "gh_environment", "push_rejected",
    "pr_closed_unmerged", "budget_exceeded", "concurrency_stalled",
    "needs_user_other",
)

# evidence 绝不含的凭据/连接材料字样。
_SECRET_WORDS = ("token", "accessToken", "connection_file")


def _view(task_id, failure_code, failure_stage="", since="2026-10-10T00:00:00+00:00"):
    """合成 needs_user 任务视图（attention.collect 的入参形状）。"""
    return {
        "task_id": task_id, "project_name": "StudyTool", "title": task_id,
        "status": "needs_user", "save_state": "active",
        "failure_code": failure_code, "failure_stage": failure_stage,
        "failure_reason": "rehearsal", "blocked_summary": "rehearsal",
        "blocked_at": since, "updated_at": since,
        "allowed_actions": ["replan", "dismiss"],
        "evidence": [{"kind": "event", "id": 1, "at": since,
                      "detail": "rehearsal"}],
    }


def test_scenario_attention_list_contract_snapshot(cli, factory):
    """attention-list 只读聚合：15 字段精确快照 + reason_code 枚举 + 证据脱敏。"""
    assert tuple(kattention.ITEM_FIELDS) == ATTENTION_ITEM_FIELDS
    blocked = factory.make_needs_user()
    tid = blocked.task.task_id
    out = cli("attention-list", {})
    items = out["items"]

    # 同一任务不重复出现
    ids = [i["task_id"] for i in items]
    assert len(ids) == len(set(ids)), "同一任务不得重复出现"
    mine = [i for i in items if i["task_id"] == tid]
    assert len(mine) == 1, items
    item = mine[0]

    # 15 字段精确相等（不缺不多）
    assert set(item.keys()) == set(ATTENTION_ITEM_FIELDS), (
        f"缺字段：{sorted(set(ATTENTION_ITEM_FIELDS) - set(item))}；"
        f"多字段：{sorted(set(item) - set(ATTENTION_ITEM_FIELDS))}")

    # attention_id 确定性格式 attn_<task_id>_<reason_code>（重启稳定）
    assert item["attention_id"] == f"attn_{tid}_{item['reason_code']}", item

    # reason_code ∈ 13 个冻结枚举
    assert item["reason_code"] in REASON_CODES, item

    # attention_state 词表
    assert item["attention_state"] in ("actionable", "historical",
                                       "informational"), item

    # evidence：list，每项 kind/id/at/detail，且脱敏（无 token/连接材料）
    assert isinstance(item["evidence"], list), item
    for entry in item["evidence"]:
        assert isinstance(entry, dict), entry
        assert {"kind", "id", "at", "detail"} <= set(entry), entry
    blob = json.dumps(item["evidence"], ensure_ascii=False)
    for word in _SECRET_WORDS:
        assert word not in blob, f"evidence 含未脱敏内容：{word}"

    # recommended 为 None 或 allowed_actions 之一；alternatives 是 list
    actions = item["allowed_actions"]
    assert isinstance(actions, list), item
    assert item["recommended"] is None or item["recommended"] in actions, item
    assert isinstance(item["alternatives"], list), item

    # since 可解析为 ISO-8601
    parsed = kutil.parse_ts(item["since"])
    assert parsed is not None, item

    # save_state 缺省 active；include_historical 包含默认集合
    hist = cli("attention-list", {"include_historical": True})["items"]
    hist_ids = {i["task_id"] for i in hist}
    assert set(ids) <= hist_ids, "include_historical 应包含默认集合"


def test_attention_collect_orders_by_ladder_and_dedups():
    """聚合纯函数：阶梯 4>3>2>1 排序 + 同任务去重（§5.27.3）。"""
    now = "2026-10-10T00:00:00+00:00"
    views = [
        _view("task-ladder1", "remote_unavailable", since="2026-10-09T00:00:00+00:00"),
        _view("task-ladder3", "verification_unconfirmed"),
        _view("task-ladder2", "gh_missing"),
        _view("task-ladder4", "auto_budget_exceeded"),
        _view("task-ladder3", "verification_unconfirmed"),   # 同任务重复 → 去重
    ]
    items = kattention.collect(views, now, include_historical=True)
    order = [i["task_id"] for i in items]
    assert order == ["task-ladder4", "task-ladder3", "task-ladder2",
                     "task-ladder1"], order
    # reason_code 与阶梯映射（冻结表）
    by_task = {i["task_id"]: i for i in items}
    assert by_task["task-ladder4"]["reason_code"] == "budget_exceeded"
    assert by_task["task-ladder3"]["reason_code"] == "verify_unconfirmed"
    assert by_task["task-ladder2"]["reason_code"] == "gh_environment"
    assert by_task["task-ladder1"]["reason_code"] == "remote_unavailable"
    for item in items:
        assert item["attention_id"] == f"attn_{item['task_id']}_{item['reason_code']}"
        assert item["reason_code"] in REASON_CODES


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
