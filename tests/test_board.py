"""六列看板契约：board_column 归列、board_substatus 词表、blocked_reason、
last_checked_at、review_summary、Review 前置守卫、过滤/计数与 list/get 一致性。

覆盖契约条款：protocol §2「看板列」（非 needs_user 直映射、伴随字段词表）、
§5.2（TaskSummary 字段、list-tasks board_column 过滤、overview counts.board）、
§5.7/§5.8（register-review/create-review-fix 修复链）、§5.9（Review 前置守卫）、
§5.14（failure_code 词表、send_state=unknown 优先、一致性、恢复即恢复）与
**§5.15.4（v1.3：异常统一进第六列 failed，由 lifecycle_column 保留生命周期
位置、exception 给出结构化异常信息）**；state-model §2.1/§2.2。

桌面只按 board_column 分列、board_substatus 标子状态、blocked_reason 显阻塞
（§5.14-7）。全部断言经 CLI 输出完成；失联构造仅用 expire_lease 时间后门。

v1.3 变化（相对 v1.2）：needs_user、评审未通过、失联一律进第六列 `failed`；
v1.2 的 `failure_stage` 归列口径只用于计算 `lifecycle_column`。细分异常判定表、
互斥与计数的系统化用例见 `test_board_v13.py`。
"""

from collections import Counter
from pathlib import Path

import pytest

try:
    from conftest import (
        assert_iso8601, expire_lease, ui_evidence, write_review_report,
    )
except ImportError:  # unittest discover 包上下文
    from tests.conftest import (
        assert_iso8601, expire_lease, ui_evidence, write_review_report,
    )


# ---------------------------------------------------------------------------
# 冻结词表（protocol §2 / §5.14 / §5.15.4）
# ---------------------------------------------------------------------------

BOARD_COLUMNS = (
    "needs_plan", "todo", "in_progress", "in_review", "done", "failed",
)

# lifecycle_column：纯生命周期位置（v1.3 §5.15.4）——不含 failed 列
LIFECYCLE_COLUMNS = ("needs_plan", "todo", "in_progress", "in_review", "done")

# 前五列维持 v1.2 词表；第六列 = exception.label（§5.15.4）
BOARD_SUBSTATUS_VOCAB = {
    "待规划", "规划中", "就绪", "已排队", "派发中",
    "执行中", "评审中", "已完成",
    "规划失败", "派发失败", "执行失败", "失败",
    "发送不明", "需人工处理", "执行端失联",
    "评审受阻", "监控异常", "评审未通过·待修复",
}

BOARD_SUMMARY_KEYS = {
    "board_column", "board_substatus", "last_checked_at", "review_summary",
    # v1.3 新增（§5.15.4 / §5.15.1）
    "lifecycle_column", "exception", "save_state", "saved_at",
    "auto_advance_paused",
}

_KIND_STAGE = {
    "plan": "plan", "dispatch": "dispatch",
    "monitor": "monitor", "review": "review",
}


def claim_request(factory, task_id, kind):
    """找到该 task 的 queued 请求并领取；返回 claim 响应（含 run_id）。

    v2：plan 请求不再自动生成——缺省时经 queue_plan（规划面板链）补齐。"""
    req = factory.find_request(task_id, kind)
    if req is None and kind == "plan":
        req_id, _i, _s = factory.queue_plan(task_id)
        req = factory.find_request(task_id, kind)
    assert req is not None, f"{task_id} 没有 {kind} 请求"
    c = factory.claim(_KIND_STAGE[kind], request_id=req["request_id"])
    assert c["claimed"] is True, f"{kind} claim 失败：{c}"
    return c


def assert_board(factory, task_id, column, substatus=None,
                 lifecycle_column=None, exception_label=None):
    """断言 get-task 归列；并校验 list-tasks 与 get-task 同列同子状态
    （§5.14-2：同任务同列）。返回 TaskDetail。

    v1.3 起额外校验：第六列子状态等于 exception.label；异常卡上
    `lifecycle_column` 与 `board_column` 必然不同（互斥）。
    """
    t = factory.get_task(task_id)
    assert t.get("board_column") == column, (
        f"{task_id} board_column 应为 {column!r}，实际 "
        f"{t.get('board_column')!r}（status={t.get('status')!r} "
        f"failure_code={t.get('failure_code')!r} "
        f"failure_stage={t.get('failure_stage')!r} "
        f"send_state={t.get('send_state')!r}）"
    )
    if substatus is not None:
        assert t.get("board_substatus") == substatus, (
            f"{task_id} board_substatus 应为 {substatus!r}，实际 "
            f"{t.get('board_substatus')!r}"
        )
    row = factory.list_task(task_id)
    assert row is not None, f"list-tasks 缺少 {task_id}"
    assert row.get("board_column") == t.get("board_column"), (
        f"list-tasks/get-task 列不一致：{row.get('board_column')!r} != "
        f"{t.get('board_column')!r}"
    )
    if lifecycle_column is not None:
        assert t.get("lifecycle_column") == lifecycle_column, (
            f"{task_id} lifecycle_column 应为 {lifecycle_column!r}，实际 "
            f"{t.get('lifecycle_column')!r}")
    if exception_label is not None:
        exc = t.get("exception")
        assert isinstance(exc, dict), f"{task_id} 应有 exception：{exc!r}"
        assert exc.get("label") == exception_label, (
            f"{task_id} exception.label 应为 {exception_label!r}，实际 "
            f"{exc.get('label')!r}")
        assert t.get("board_substatus") == exception_label, (
            "board_substatus 应等于 exception.label")
    assert row.get("lifecycle_column") == t.get("lifecycle_column")
    return t


def report_needs_user(cli, claim, failure_code, note="阻塞"):
    return cli("report", {
        "request_id": claim["request_id"], "run_id": claim["run_id"],
        "outcome": "needs_user", "failure_code": failure_code,
        "failure_reason": note, "note": note,
    })


# ---------------------------------------------------------------------------
# §2 直映射矩阵（非 needs_user 状态）
# ---------------------------------------------------------------------------


class TestBoardDirectMapping:
    def test_full_lifecycle_columns(self, factory, cli):
        """单任务驱动全生命周期，逐态断言列与中文子状态（§2 直映射表）。"""
        task = factory.sync_task(title="看板直映射", description="走完全程")
        tid = task.task_id

        # needs_plan → needs_plan/待规划
        assert_board(factory, tid, "needs_plan", "待规划")

        # planning → needs_plan/规划中
        plan_claim = claim_request(factory, tid, "plan")
        assert_board(factory, tid, "needs_plan", "规划中")
        # ready（无活动请求）→ todo/就绪（publish-plan 已结束请求，
        # 不再 report；ready+hold → display 待确认执行，列仍是就绪）
        factory.publish(task)
        assert_board(factory, tid, "todo", "就绪")

        # ready + queued dispatch（confirm_plan 放行）→ todo/已排队
        factory.enqueue_dispatch(tid)
        assert_board(factory, tid, "todo", "已排队")

        # dispatching / prepared / submitting → todo/派发中
        d = claim_request(factory, tid, "dispatch")
        assert_board(factory, tid, "todo", "派发中")
        ev = ui_evidence(
            session_marker=d["context"]["session_marker"],
            repository=d["context"]["repository"],
            attachment_name=d["context"]["attachment_name"])
        cli("dispatch-prepare", {"task_id": tid, "run_id": d["run_id"],
                                 "evidence": ev})
        assert_board(factory, tid, "todo", "派发中")
        cli("dispatch-begin-send", {"task_id": tid, "run_id": d["run_id"]})
        assert_board(factory, tid, "todo", "派发中")

        # running → in_progress/执行中
        ev2 = dict(ev, message_visible=True, session_title="看板矩阵",
                   session_id=f"sess-{tid[:8]}")
        cli("dispatch-confirm", {"task_id": tid, "run_id": d["run_id"],
                                 "evidence": ev2})
        assert_board(factory, tid, "in_progress", "执行中")

        # in_review → in_review/评审中
        # §5.9 分支评审前置：core.require_committed_review 要求
        # delivery.commits 中存在 execution_run_id==本轮执行 run_id 且
        # sha==delivery.head_sha 的登记，缺证据即 validation_failed
        # （「本轮执行尚未完成核心本地提交，不能进入分支评审」）。这里沿用
        # tests/conftest.py TaskFactory.make_in_review 的既有正确生命周期模式：
        # 在 tmp_env 的临时本地仓库工作树里造一处真实改动，再调用核心命令
        # delivery-commit（monitor 请求身份 + expected_execution_run_id=派发
        # run_id）由核心完成本地提交并登记证据——不手工写入 delivery 证据、
        # 不弱化守卫、本用例仍走 in-review（pr/分支）路径，不触真实远端。
        m = claim_request(factory, tid, "monitor")
        delivery = factory.get_task(tid).get("delivery") or {}
        assert (delivery.get("mode") == "pr"
                or delivery.get("workspace_version") == 1) and all(
            delivery.get(key) for key in ("branch", "base_branch", "base_sha",
                                          "head_sha", "worktree_path")), (
            f"{tid} 应已登记完整分支评审工作区：{delivery}")
        (Path(delivery["worktree_path"]) / "board-lifecycle-feature.py").write_text(
            "VALUE = 1\n", encoding="utf-8")
        committed = cli("delivery-commit", {
            "task_id": tid, "request_id": m["request_id"], "run_id": m["run_id"],
            "expected_execution_run_id": d["run_id"]})
        assert committed["outcome"] in ("committed", "already_committed"), committed
        assert committed["sha"] == factory.get_task(tid)["delivery"]["head_sha"], committed
        cli("advance-task", {"task_id": tid, "run_id": m["run_id"],
                             "target": "in-review",
                             "expected_current": "in-progress"})
        cli("report", {"request_id": m["request_id"], "run_id": m["run_id"],
                       "outcome": "succeeded"})
        assert_board(factory, tid, "in_review", "评审中")

        # done → done/已完成
        cli("resolve", {"task_id": tid, "action": "mark_done"})
        assert_board(factory, tid, "done", "已完成")


# ---------------------------------------------------------------------------
# §5.14-1 failed 列：failure_code ∈ {validation_failed, dispatch_failed,
# execution_failed, plan_failed} 才算失败
# ---------------------------------------------------------------------------


class TestBoardFailedColumn:
    def test_dispatch_fail_not_sent(self, factory, cli):
        """dispatch-fail not_sent → dispatch_failed → failed/派发失败。"""
        nu = factory.make_needs_user(send_state="not_sent")
        t = assert_board(factory, nu.task.task_id, "failed", "派发失败")
        assert t["failure_code"] == "dispatch_failed"
        assert t["blocked_reason"], "needs_user 必须有 blocked_reason（§2）"

    def test_plan_report_failed(self, factory, cli):
        """plan claim 后 report needs_user plan_failed → failed/规划失败。"""
        task = factory.sync_task(title="规划失败")
        c = claim_request(factory, task.task_id, "plan")
        report_needs_user(cli, c, "plan_failed", note="需求矛盾无法规划")
        t = assert_board(factory, task.task_id, "failed", "规划失败")
        assert t["failure_code"] == "plan_failed"
        assert t["blocked_reason"]

    def test_execution_report_failed(self, factory, cli):
        """running 的 monitor report execution_failed → failed/执行失败。"""
        running = factory.make_running(title="执行失败")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        report_needs_user(cli, c, "execution_failed", note="Devin 执行失败")
        t = assert_board(factory, tid, "failed", "执行失败")
        assert t["failure_code"] == "execution_failed"
        assert t["blocked_reason"]

    def test_dispatch_validation_failed(self, factory, cli):
        """dispatch claim 校验失败（远端已离开 To Do）→ failed/派发失败。"""
        ready = factory.make_ready(title="校验失败")
        tid = ready.task.task_id
        factory.kaneo.set_status(tid, "in-progress")  # 远端被人为推进
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        c = factory.claim("dispatch", request_id=req["request_id"])
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"  # §4.2-5 领取守卫失败
        t = assert_board(factory, tid, "failed", "派发失败")
        assert t["failure_code"] == "validation_failed"
        assert t["blocked_reason"]


# ---------------------------------------------------------------------------
# §5.15.4 needs_user 异常归列：一律进第六列 failed，lifecycle_column 按
# failure_stage 保留生命周期位置；send_state=unknown → send_unknown/发送不明
# ---------------------------------------------------------------------------


class TestBoardBlocked:
    def test_send_unknown_isolated_in_failed(self, factory, cli):
        """发送不明（send_state=unknown）不算失败但也不是正常卡：
        v1.3 起进第六列 failed / 发送不明，lifecycle_column=todo。

        dispatch-fail 写的 failure_code=dispatch_failed 本属失败类，
        但 §5.14-1 规定 unknown 优先于一切 code → send_unknown。
        """
        nu = factory.make_needs_user(send_state="unknown")
        t = assert_board(factory, nu.task.task_id, "failed", "发送不明",
                         lifecycle_column="todo",
                         exception_label="发送不明")
        assert t["send_state"] == "unknown"
        assert t["status"] == "needs_user"
        assert t["blocked_reason"]
        assert t["exception"]["kind"] == "send_unknown"
        assert t["exception"]["severity"] == "blocked"

    def test_plan_blocked_in_failed(self, factory, cli):
        """planning 阶段 report needs_user（reported）→ failed / 需人工处理，
        lifecycle_column=needs_plan（异常隔离但保留生命周期位置）。"""
        task = factory.sync_task(title="规划受阻")
        c = claim_request(factory, task.task_id, "plan")
        report_needs_user(cli, c, "reported", note="需求不清待澄清")
        t = assert_board(factory, task.task_id, "failed", "需人工处理",
                         lifecycle_column="needs_plan",
                         exception_label="需人工处理")
        assert t["status"] == "needs_user"
        assert t["blocked_reason"]
        assert t["exception"]["kind"] == "manual_block"

    def test_monitor_blocked_in_failed(self, factory, cli):
        """running 阶段观测受阻（observation_blocked）→ failed / 监控异常，
        lifecycle_column=in_progress。"""
        running = factory.make_running(title="执行受阻")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        report_needs_user(cli, c, "observation_blocked",
                          note="锁屏无法观察进度")
        t = assert_board(factory, tid, "failed", "监控异常",
                         lifecycle_column="in_progress",
                         exception_label="监控异常")
        assert t["blocked_reason"]
        assert t["exception"]["kind"] == "observation_blocked"

    def test_review_blocked_in_failed(self, factory, cli):
        """register-review blocked → needs_user+review_blocked →
        failed / 评审受阻，lifecycle_column=in_review；当前周期结论仍登记。"""
        ir = factory.make_in_review(title="评审受阻")
        tid = ir.task.task_id
        rv = claim_request(factory, tid, "review")
        rep = write_review_report(factory.env.app_support, task_id=tid,
                                  conclusion="blocked")
        # §5.24.5 分支评审领取身份（core.require_review_identity）：本夹具的
        # 任务工作区由 start-plans 登记为 workspace_version=1，登记任何评审
        # 结论都必须带 request_id + run_id，且两者同源上面那次真实 review
        # 领取；只传 run_id 属旧非分支手动兼容，分支评审下 validation_failed
        # 「分支评审必须携带领取身份」。不伪造请求、不把夹具改成 manual 逃避。
        cli("register-review", {"task_id": tid,
                                "request_id": rv["request_id"],
                                "run_id": rv["run_id"],
                                "report_path": str(rep.path),
                                "conclusion": "blocked"})
        t = assert_board(factory, tid, "failed", "评审受阻",
                         lifecycle_column="in_review",
                         exception_label="评审受阻")
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "review_blocked"
        assert t["blocked_reason"]
        assert t["exception"]["kind"] == "review_blocked"
        summary = t.get("review_summary")
        assert summary is not None and summary.get("conclusion") == "blocked"


# ---------------------------------------------------------------------------
# §5.15.4 失联：同样进第六列 failed / 执行端失联（失联不是失败，但也不是
# 正常卡）；lifecycle_column 保留原列；库内状态不变
# ---------------------------------------------------------------------------


class TestBoardLost:
    def test_lost_isolated_in_failed(self, factory, cli, tmp_env):
        running = factory.make_running(title="失联任务")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        expire_lease(tmp_env.db, c["request_id"])
        # v1.3：失联进第六列；lifecycle_column 仍是 in_progress（§5.15.4）
        t = assert_board(factory, tid, "failed", "执行端失联",
                         lifecycle_column="in_progress",
                         exception_label="执行端失联")
        assert t["status"] == "running"
        assert t["display_status"] == "执行端失联"
        assert t["blocked_reason"], "失联时 blocked_reason 非空（§2）"
        assert "失联" in t["blocked_reason"]
        assert t["exception"]["kind"] == "executor_lost"
        assert t["exception"]["severity"] == "blocked"
        # 失联标记含失联请求 kind（§2 伴随字段）
        assert any(token in t["blocked_reason"]
                   for token in ("monitor", "监控")), (
            f"blocked_reason 应含失联请求 kind：{t['blocked_reason']!r}")

    def test_release_lost_returns_to_column(self, factory, cli, tmp_env):
        """resolve release_lost → 回原列、异常与 blocked_reason 清空。"""
        running = factory.make_running(title="失联恢复")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        expire_lease(tmp_env.db, c["request_id"])
        assert_board(factory, tid, "failed", "执行端失联",
                     lifecycle_column="in_progress")
        cli("resolve", {"task_id": tid, "action": "release_lost",
                        "note": "重派监控"})
        t = assert_board(factory, tid, "in_progress", "执行中")
        assert t["lifecycle_column"] == "in_progress"
        assert t["exception"] is None, "恢复后异常应清空"
        assert t["display_status"] == "执行中"
        assert not t["blocked_reason"]
        # 释放后重建新 monitor 请求（§4.10）
        assert factory.find_request(tid, "monitor") is not None


# ---------------------------------------------------------------------------
# §5.14-4 恢复即恢复：resolve 清阻塞字段 → 归列回生命周期直映射
# ---------------------------------------------------------------------------


class TestBoardRecovery:
    def test_retry_dispatch_failure_back_to_todo(self, factory, cli):
        """failed（dispatch 类）resolve retry → ready + dispatch 请求 →
        todo/已排队；历史失败不覆盖恢复后状态。"""
        nu = factory.make_needs_user(send_state="not_sent")
        tid = nu.task.task_id
        assert_board(factory, tid, "failed", "派发失败",
                     lifecycle_column="todo", exception_label="派发失败")
        cli("resolve", {"task_id": tid, "action": "retry"})
        t = assert_board(factory, tid, "todo", "已排队")
        assert t["status"] == "ready"
        # 阻塞字段清空：归列由当前生命周期决定，不由历史失败（§5.14-4）
        assert t["failure_code"] is None
        assert not t["blocked_reason"]
        assert t["exception"] is None, "恢复后不应再有异常"
        assert factory.find_request(tid, "dispatch") is not None

    def test_retry_plan_failure_back_to_needs_plan(self, factory, cli):
        """failed（plan 类）resolve retry → needs_plan + plan 请求。"""
        task = factory.sync_task(title="规划重试")
        tid = task.task_id
        c = claim_request(factory, tid, "plan")
        report_needs_user(cli, c, "plan_failed", note="规划失败一次")
        assert_board(factory, tid, "failed", "规划失败",
                     lifecycle_column="needs_plan",
                     exception_label="规划失败")
        cli("resolve", {"task_id": tid, "action": "retry"})
        t = assert_board(factory, tid, "needs_plan", "待规划")
        assert t["failure_code"] is None
        assert not t["blocked_reason"]
        assert t["exception"] is None
        # 恢复后可重新领取 → planning 仍归 needs_plan/规划中
        claim_request(factory, tid, "plan")
        assert_board(factory, tid, "needs_plan", "规划中")

    def test_dismiss_back_to_todo_ready(self, factory, cli):
        """dismiss → ready 且不建请求 → todo/就绪（§4.10）。"""
        nu = factory.make_needs_user(send_state="not_sent")
        cli("resolve", {"task_id": nu.task.task_id, "action": "dismiss"})
        t = assert_board(factory, nu.task.task_id, "todo", "就绪")
        assert t["status"] == "ready"
        assert not t["blocked_reason"]
        assert t["exception"] is None
        assert factory.find_request(nu.task.task_id, "dispatch") is None


# ---------------------------------------------------------------------------
# §5.9 Review 前置守卫（v1.2）+ §5.24.5 分支评审领取身份：未评审、或登记
# 缺领取身份，分支任务都不能完成；合规评审通过后方可完成
# ---------------------------------------------------------------------------


class TestReviewGuard:
    def test_branch_task_requires_compliant_review_before_done(
            self, factory, cli):
        """分支任务未评审不能完成，合规评审通过后完成（§5.9 + §5.24.5）。

        三段各自独立：
        1. in-progress 直接 advance done → conflict（§5.9 评审前置守卫）。
        2. 同一任务上不带领取身份的 register-review → validation_failed，且
           拦截发生在任何登记写入之前（core.require_review_identity 先于写
           事务）：pass 未落库、任务状态不变、原守卫继续拒绝。分支任务
           （workspace_version=1）的评审请求只在 in_review 创建与领取
           （constants.CLAIM_GUARDS["review"]），执行中不存在合规登记路径。
        3. 合规链路：make_in_review（内含真实 delivery-commit）→ 领取本轮评审
           请求 → 报告 pass → register-review 带同一 request_id/run_id →
           advance done（expected_current=in-review）→ done/已完成。
        """
        running = factory.make_running(title="跳评审完成")
        tid = running.task.task_id
        m = claim_request(factory, tid, "monitor")
        # 1) 无当前周期 pass 结论 → 守卫拒绝（§5.9 v1.2）；先置校验：远端未被
        #    写、本地状态不变
        out = cli("advance-task", {
            "task_id": tid, "run_id": m["run_id"], "target": "done",
            "expected_current": "in-progress"}, expect=1)
        assert out["code"] == "conflict"
        assert factory.kaneo.get_task(tid)["status"] == "in-progress"
        assert_board(factory, tid, "in_progress")

        # 2) 分支评审必须携带领取身份：这里只传 monitor 的执行 run（不是评审
        #    请求的 run），workspace_version=1 下被拒，且不留任何登记痕迹
        rep = write_review_report(factory.env.app_support, task_id=tid,
                                  conclusion="pass")
        rej = cli("register-review", {"task_id": tid, "run_id": m["run_id"],
                                      "report_path": str(rep.path),
                                      "conclusion": "pass"}, expect=1)
        assert rej["code"] == "validation_failed", rej
        assert "领取身份" in rej["error"], rej
        assert factory.get_task(tid).get("review_summary") is None
        assert factory.kaneo.get_task(tid)["status"] == "in-progress"
        assert_board(factory, tid, "in_progress")
        # pass 确实没有登记进去：同一 advance 仍被 §5.9 守卫拒绝
        again = cli("advance-task", {
            "task_id": tid, "run_id": m["run_id"], "target": "done",
            "expected_current": "in-progress"}, expect=1)
        assert again["code"] == "conflict"

        # 3) 合规路径：进入评审后领取真实评审请求，登记 pass 再完成
        ir = factory.make_in_review(title="评审通过完成")
        rid = ir.task.task_id
        rv = claim_request(factory, rid, "review")
        rep2 = write_review_report(factory.env.app_support, task_id=rid,
                                   conclusion="pass")
        cli("register-review", {
            "task_id": rid, "request_id": rv["request_id"],
            "run_id": rv["run_id"], "report_path": str(rep2.path),
            "conclusion": "pass"})
        # get-task 透出的是核心内部状态 in_review（in-review 是 Kaneo 远端词表）
        assert factory.get_task(rid)["status"] == "in_review"
        adv = cli("advance-task", {"task_id": rid, "target": "done",
                                   "expected_current": "in-review"})
        assert adv["ok"] is True
        assert adv["task_status"] == "done"
        assert_board(factory, rid, "done", "已完成")

    def test_done_from_in_review_needs_no_conclusion(self, factory, cli):
        """expected_current=in-review 不受守卫限制（§5.9）。"""
        ir = factory.make_in_review(title="评审直通")
        tid = ir.task.task_id
        adv = cli("advance-task", {"task_id": tid, "target": "done",
                                   "expected_current": "in-review"})
        assert adv["ok"] is True
        assert adv["task_status"] == "done"
        assert_board(factory, tid, "done", "已完成")


# ---------------------------------------------------------------------------
# §5.7/§5.8 + §5.14-3 修复链：待修复子状态与 review_summary.fix_task_id
# ---------------------------------------------------------------------------


class TestReviewFixBoard:
    def test_changes_requested_pending_fix(self, factory, cli):
        ir = factory.make_in_review(title="待修复任务")
        src = ir.task.task_id
        # 新一轮 in_review 起始即清空（§2）：登记前 review_summary 为空
        assert factory.get_task(src).get("review_summary") is None

        rv = claim_request(factory, src, "review")
        # §5.24.5：create-review-fix 与 register-review 共享
        # core.require_review_identity，分支任务必须带这次真实 review 领取
        # （rv）的 request_id/run_id，不能只靠 source_task_id
        fix = cli("create-review-fix", {
            "source_task_id": src, "title": "看板修复任务",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-BOARD-1",
            "request_id": rv["request_id"], "run_id": rv["run_id"]})
        assert fix["created"] is True
        fix_id = fix["fix_task_id"]

        rep = write_review_report(factory.env.app_support, task_id=src,
                                  conclusion="changes_requested")
        # 领取身份同上：rv 是本轮 in_review 的真实 review claim（run_id 是
        # 评审请求的 run，不是任务执行 run）
        cli("register-review", {
            "task_id": src, "request_id": rv["request_id"],
            "run_id": rv["run_id"],
            "report_path": str(rep.path), "conclusion": "changes_requested",
            "fix_task_id": fix_id, "review_key": "RK-BOARD-1"})

        # §5.27.18：历史二级修复的父任务留评审列；Review 活动期间不能收尾。
        t = assert_board(factory, src, "in_review", "收尾校验受阻",
                         lifecycle_column="in_review",
                         exception_label="收尾校验受阻")
        assert t["exception"]["kind"] == "parent_closeout_blocked"
        assert t["exception"]["severity"] == "blocked"
        assert t["exception"]["fix_task_id"] is None
        assert t["fix_task_id"] == fix_id
        closeout = t["parent_closeout"]
        assert closeout["state"] == "blocked"
        assert closeout["can_complete"] is False
        assert closeout["required_children"] == 1
        assert closeout["completed_children"] == 0
        assert {b["code"] for b in closeout["blockers"]} == {"active_request"}
        summary = t.get("review_summary")
        assert summary is not None, "register-review 后应有 review_summary"
        assert {"conclusion", "registered_at", "fix_task_id", "review_key",
                "report_path"} <= set(summary)
        assert summary["conclusion"] == "changes_requested"
        assert summary["fix_task_id"] == fix_id
        assert summary["review_key"] == "RK-BOARD-1"
        assert summary["report_path"] == str(rep.path)
        assert_iso8601(summary["registered_at"])

        # 修复任务自身走 needs_plan 列，不占原任务的列（§5.14-3）
        ft = assert_board(factory, fix_id, "needs_plan", "待规划")
        assert ft["plan_kind"] == "review_fix"
        assert ft["review_source_task_id"] == src

        # 不重复建：同 dedupe_key 返回既有（命中在守卫之前返回）；调用方仍
        # 携带同一次领取身份；重复 register 不报错
        f2 = cli("create-review-fix", {
            "source_task_id": src, "title": "看板修复任务",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-BOARD-1",
            "request_id": rv["request_id"], "run_id": rv["run_id"]})
        assert f2["created"] is False
        assert f2["fix_task_id"] == fix_id
        # 重复登记复用同一仍被领取的评审身份（请求未 report 前 status 仍是
        # claimed）——幂等核对的是身份+review_key，不是缺身份的手动兼容路径。
        cli("register-review", {
            "task_id": src, "request_id": rv["request_id"],
            "run_id": rv["run_id"],
            "report_path": str(rep.path), "conclusion": "changes_requested",
            "fix_task_id": fix_id, "review_key": "RK-BOARD-1"})
        ids = [x["task_id"] for x in cli("list-tasks", {})["tasks"]]
        assert ids.count(fix_id) == 1 and ids.count(src) == 1


# ---------------------------------------------------------------------------
# §5.2 字段形状 / board_column 过滤 / counts.board / last_checked_at
# ---------------------------------------------------------------------------


class TestBoardFieldShapes:
    def test_summary_and_detail_board_fields(self, factory, cli):
        """TaskSummary/TaskDetail 均含看板伴随字段（§2/§5.2）。"""
        task = factory.sync_task(title="看板字段")
        row = factory.list_task(task.task_id)
        assert row is not None
        assert BOARD_SUMMARY_KEYS <= set(row), (
            f"TaskSummary 缺看板字段：{BOARD_SUMMARY_KEYS - set(row)}")
        detail = factory.get_task(task.task_id)
        assert BOARD_SUMMARY_KEYS <= set(detail), (
            f"TaskDetail 缺看板字段：{BOARD_SUMMARY_KEYS - set(detail)}")
        # 词表校验
        assert row["board_column"] in BOARD_COLUMNS
        assert row["board_substatus"] in BOARD_SUBSTATUS_VOCAB
        # 无结论 → review_summary 为 null
        assert row["review_summary"] is None
        # sync 已写 kaneo_checked_at → last_checked_at 非空且 ISO8601（§2）
        assert row["last_checked_at"] is not None
        assert_iso8601(row["last_checked_at"])

    def test_last_checked_at_after_heartbeat_and_report(self, factory, cli):
        """last_checked_at = max(monitor/review finished, heartbeat,
        kaneo_checked_at)（§2）：心跳与尝试完成后非空。"""
        running = factory.make_running(title="观测时间")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        cli("heartbeat", {"request_id": c["request_id"],
                          "run_id": c["run_id"], "progress": "过半"})
        t = factory.get_task(tid)
        assert t["last_checked_at"] is not None
        assert_iso8601(t["last_checked_at"])
        cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                       "outcome": "succeeded"})
        t2 = factory.get_task(tid)
        assert t2["last_checked_at"] is not None
        assert_iso8601(t2["last_checked_at"])

    def test_board_column_filter(self, factory, cli):
        """list-tasks board_column 过滤按归列结果过滤（§5.2）。"""
        np_task = factory.sync_task(title="列过滤A")
        ready = factory.make_ready(project="Music", title="列过滤B")
        running = factory.make_running(project="Library", title="列过滤C")

        out = cli("list-tasks", {"board_column": "todo"})
        ids = {t["task_id"] for t in out["tasks"]}
        assert ready.task.task_id in ids
        assert np_task.task_id not in ids
        assert running.task.task_id not in ids
        assert all(t["board_column"] == "todo" for t in out["tasks"])
        # 与手工分组一致
        all_rows = cli("list-tasks", {})["tasks"]
        manual = {t["task_id"] for t in all_rows
                  if t["board_column"] == "todo"}
        assert ids == manual
        # 空列 → 空列表
        assert cli("list-tasks", {"board_column": "failed"})["tasks"] == []
        # 六列过滤构成全分划：每任务恰出现一次（§2）
        seen = []
        for col in BOARD_COLUMNS:
            part = cli("list-tasks", {"board_column": col})["tasks"]
            assert all(t["board_column"] == col for t in part)
            seen += [t["task_id"] for t in part]
        assert sorted(seen) == sorted(t["task_id"] for t in all_rows)

    def test_overview_board_counts(self, factory, cli):
        """overview counts.board 六列计数与 list-tasks 分组一致（§5.14-2）。"""
        factory.sync_task(title="列计数-规划")                          # needs_plan
        factory.make_ready(project="Music", title="列计数-就绪")        # todo
        factory.make_running(project="Library", title="列计数-执行")    # in_progress
        factory.make_in_review(project="Comic", title="列计数-评审")    # in_review
        factory.sync_task(title="列计数-完成", project="Feedback",
                          remote_status="done")                         # done
        # failed：plan 阶段明确失败（代价最小的失败类构造）
        t = factory.sync_task(title="列计数-失败", project="Music")
        c = claim_request(factory, t.task_id, "plan")
        report_needs_user(cli, c, "plan_failed", note="失败")

        rows = cli("list-tasks", {})["tasks"]
        manual = Counter(r["board_column"] for r in rows)
        counts = cli("overview", {})["counts"]
        assert "board" in counts, "overview.counts 缺 board 键（§5.14-2）"
        board = counts["board"]
        assert set(board) == set(BOARD_COLUMNS), (
            f"counts.board 应为六列键全集：{sorted(board)}")
        for col in BOARD_COLUMNS:
            assert board[col] == manual.get(col, 0), (
                f"counts.board[{col}]={board[col]} 与 list-tasks 分组 "
                f"{manual.get(col, 0)} 不一致")
        assert manual == Counter({
            "needs_plan": 1, "todo": 1, "in_progress": 1,
            "in_review": 1, "done": 1, "failed": 1})


# ---------------------------------------------------------------------------
# §5.14-2 一致性：每任务恰出现一次；list-tasks 与 get-task 同列
# ---------------------------------------------------------------------------


class TestBoardConsistency:
    def test_unique_and_consistent_columns(self, factory, cli):
        factory.sync_task(title="一致性A")
        factory.make_ready(project="Music", title="一致性B")
        factory.make_running(project="Library", title="一致性C")
        factory.make_needs_user(send_state="not_sent", project="Comic",
                                title="一致性D")
        rows = cli("list-tasks", {})["tasks"]
        ids = [t["task_id"] for t in rows]
        # 每个任务在 list-tasks 恰出现一次（§2）
        assert len(ids) == len(set(ids)), "list-tasks 中任务重复出现"
        for row in rows:
            # 归列值合法词表
            assert row["board_column"] in BOARD_COLUMNS
            assert row["board_substatus"] in BOARD_SUBSTATUS_VOCAB
            # v1.3：lifecycle_column 只取五列；异常卡两列必然不同
            assert row["lifecycle_column"] in LIFECYCLE_COLUMNS
            if row["exception"] is None:
                assert row["board_column"] == row["lifecycle_column"]
                assert row["board_substatus"] not in (
                    "规划失败", "派发失败", "执行失败", "发送不明",
                    "需人工处理", "执行端失联", "评审受阻", "监控异常",
                    "评审未通过·待修复")
            else:
                assert row["board_column"] == "failed"
                assert row["board_substatus"] == row["exception"]["label"]
            # get-task 同列同子状态
            detail = factory.get_task(row["task_id"])
            assert detail["board_column"] == row["board_column"], (
                f"{row['task_id']} list/get 列不一致："
                f"{row['board_column']} != {detail['board_column']}")
            assert detail["board_substatus"] == row["board_substatus"]
            assert detail["lifecycle_column"] == row["lifecycle_column"]
            # needs_user 阻塞行必有 blocked_reason
            if row["status"] == "needs_user":
                assert row["blocked_reason"], (
                    f"{row['task_id']} needs_user 但 blocked_reason 为空")

    def test_six_columns_partition_all_tasks(self, factory, cli):
        """六列过滤构成全分划：正常卡只在前五列、异常卡只在第六列。"""
        factory.sync_task(title="分划A")
        factory.make_ready(project="Music", title="分划B")
        factory.make_running(project="Library", title="分划C")
        factory.make_in_review(project="Comic", title="分划D")
        factory.make_needs_user(send_state="not_sent", project="Feedback",
                                title="分划E")
        rows = cli("list-tasks", {})["tasks"]
        seen = []
        for col in BOARD_COLUMNS:
            part = cli("list-tasks", {"board_column": col})["tasks"]
            assert all(t["board_column"] == col for t in part)
            seen += [t["task_id"] for t in part]
        assert sorted(seen) == sorted(t["task_id"] for t in rows)
        # 异常卡只出现在 failed 列
        failed = cli("list-tasks", {"board_column": "failed"})["tasks"]
        assert {t["task_id"] for t in failed if t["exception"]} == \
            {t["task_id"] for t in failed}
        for col in BOARD_COLUMNS[:5]:
            assert all(t["exception"] is None
                       for t in cli("list-tasks", {"board_column": col})
                       ["tasks"]), f"{col} 列混入了异常卡"


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
