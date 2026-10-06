"""六列看板异常隔离契约测试（protocol §5.15.4，v1.3）。

覆盖契约条款：protocol §2「看板列」v1.3 口径与伴随字段、§5.15.4
（`board_column`/`lifecycle_column`/`exception` 三字段、六列唯一分列依据、
异常统一进第六列 `failed`、kind/label/severity 判定表、观测异常自动清除、
历史错误不影响已恢复任务、收到新会话日志不解除异常）、§5.9（新一轮
in-review 清空上一周期结论）、§5.7/§5.8（修复链）、§5.14（v1.2 一致性：
list/get 同列、counts.board、board_column 过滤）；state-model §2.2。

关键不变量（本文件反复断言）：

- **互斥**：每任务恰归入一列；`board_column=='failed'` ⟺ `exception` 非空；
  无异常时 `board_column == lifecycle_column`（异常时二者必然不同）。
- `lifecycle_column` 只取五列（纯生命周期位置，needs_user 按 failure_stage
  归列），`board_column` 取六列（异常一律进 `failed`）。
- `board_substatus` 在第六列等于 `exception.label`；前五列维持 v1.2 词表。
"""

from collections import Counter

import pytest

try:
    from conftest import (
        assert_iso8601, expire_lease, set_task_status, ui_evidence,
        write_review_report,
    )
except ImportError:  # unittest discover 包上下文
    from tests.conftest import (
        assert_iso8601, expire_lease, set_task_status, ui_evidence,
        write_review_report,
    )


# ---------------------------------------------------------------------------
# 冻结词表（protocol §2 / §5.14 / §5.15.4）
# ---------------------------------------------------------------------------

BOARD_COLUMNS = (
    "needs_plan", "todo", "in_progress", "in_review", "done", "failed",
)

# lifecycle_column：纯生命周期位置（无 failed 列）
LIFECYCLE_COLUMNS = ("needs_plan", "todo", "in_progress", "in_review", "done")

# 前五列子状态词表（v1.2 口径，§5.15.4 明前五列维持）
NORMAL_SUBSTATUS = {
    "待规划", "规划中", "就绪", "已排队", "派发中",
    "执行中", "评审中", "已完成",
}

# exception.label 冻结词表（§5.15.4）
EXCEPTION_LABELS = {
    "规划失败", "派发失败", "执行失败", "失败",
    "发送不明", "需人工处理", "执行端失联",
    "评审受阻", "监控异常", "评审未通过·待修复",
}

EXCEPTION_KINDS = {
    "failed", "send_unknown", "manual_block", "executor_lost",
    "review_blocked", "observation_blocked", "review_rejected",
}

EXCEPTION_KEYS = {
    "kind", "label", "severity", "detail", "failure_code", "fix_task_id",
}

# needs_user 时 failure_code → (kind, label, severity)（§5.15.4 判定表）
FAILED_CODES = {
    "plan_failed": ("failed", "规划失败"),
    "validation_failed": ("failed", "派发失败"),
    "dispatch_failed": ("failed", "派发失败"),
    "execution_failed": ("failed", "执行失败"),
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
        factory.queue_plan(task_id)
        req = factory.find_request(task_id, kind)
    assert req is not None, f"{task_id} 没有 {kind} 请求"
    c = factory.claim(_KIND_STAGE[kind], request_id=req["request_id"])
    assert c["claimed"] is True, f"{kind} claim 失败：{c}"
    return c


def report_needs_user(cli, claim, failure_code, note="阻塞"):
    return cli("report", {
        "request_id": claim["request_id"], "run_id": claim["run_id"],
        "outcome": "needs_user", "failure_code": failure_code,
        "failure_reason": note, "note": note,
    })


def pair(factory, cli, task_id, save_state=None):
    """返回 (TaskSummary, TaskDetail)，并校验两者三字段一致（§5.14-2）。"""
    payload = {} if save_state is None else {"save_state": save_state}
    row = next((t for t in cli("list-tasks", payload)["tasks"]
                if t["task_id"] == task_id), None)
    detail = factory.get_task(task_id)
    if row is not None:
        for field in ("board_column", "lifecycle_column", "board_substatus"):
            assert row[field] == detail[field], (
                f"{task_id} list/get {field} 不一致："
                f"{row[field]!r} != {detail[field]!r}")
        assert (row["exception"] is None) == (detail["exception"] is None)
        if row["exception"] is not None:
            assert row["exception"] == detail["exception"]
    return row, detail


_ANY = object()   # 「不校验该字段」哨兵


def assert_exception(factory, cli, task_id, *, kind, label, severity,
                     lifecycle_column=None, board_column="failed",
                     failure_code=_ANY, fix_task_id=None):
    """断言异常结构（§5.15.4 判定表）+ 与 lifecycle_column 的互斥关系。

    `failure_code` 缺省不校验：它是透出的原 `tasks.failure_code`
    （发送不明也保留 dispatch_failed 之类原始码），各用例按需显式指定。
    """
    row, t = pair(factory, cli, task_id)
    assert row is not None, f"list-tasks 缺少 {task_id}"
    exc = t["exception"]
    assert isinstance(exc, dict), (
        f"{task_id} 应有 exception，实际 {exc!r}（status={t['status']!r} "
        f"failure_code={t['failure_code']!r}）")
    assert EXCEPTION_KEYS <= set(exc), f"exception 缺字段：{exc}"
    assert exc["kind"] == kind, f"{task_id} kind 应为 {kind}：{exc['kind']}"
    assert exc["label"] == label, f"{task_id} label 应为 {label}：{exc['label']}"
    assert exc["severity"] == severity, (
        f"{task_id} severity 应为 {severity}：{exc['severity']}")
    assert exc["label"] in EXCEPTION_LABELS
    assert exc["kind"] in EXCEPTION_KINDS
    assert isinstance(exc["detail"], str) and exc["detail"].strip(), (
        f"{task_id} exception.detail 应为非空原因文本：{exc['detail']!r}")
    if failure_code is not _ANY:
        assert exc["failure_code"] == failure_code, (
            f"{task_id} exception.failure_code 应为 {failure_code}："
            f"{exc['failure_code']}")
    assert exc["fix_task_id"] == fix_task_id
    # detail 同 blocked_reason（§5.15.4）
    assert exc["detail"] == t["blocked_reason"], (
        f"exception.detail 应与 blocked_reason 一致："
        f"{exc['detail']!r} != {t['blocked_reason']!r}")
    # 互斥：异常卡只在第六列；第六列子状态 = exception.label
    assert t["board_column"] == board_column, (
        f"{task_id} board_column 应为 {board_column}：{t['board_column']}")
    assert t["board_substatus"] == label, (
        f"{task_id} 第六列 board_substatus 应等于 exception.label "
        f"{label}：{t['board_substatus']}")
    if lifecycle_column is not None:
        assert t["lifecycle_column"] == lifecycle_column, (
            f"{task_id} lifecycle_column 应为 {lifecycle_column}："
            f"{t['lifecycle_column']}")
        assert t["lifecycle_column"] != t["board_column"], (
            "异常任务的 lifecycle_column 与 board_column 必须不同（互斥）")
    return t


def assert_normal(factory, cli, task_id, column, substatus=None):
    """断言无异常卡：board_column == lifecycle_column 且 exception 为 null。"""
    row, t = pair(factory, cli, task_id)
    assert row is not None, f"list-tasks 缺少 {task_id}"
    assert t["exception"] is None, (
        f"{task_id} 不应有异常：{t['exception']!r}")
    assert t["board_column"] == column, (
        f"{task_id} board_column 应为 {column}：{t['board_column']}")
    assert t["lifecycle_column"] == column, (
        f"{task_id} 无异常时 lifecycle_column 应等于 board_column："
        f"{t['lifecycle_column']} != {column}")
    assert t["board_substatus"] in NORMAL_SUBSTATUS, (
        f"前五列子状态应维持 v1.2 词表：{t['board_substatus']!r}")
    if substatus is not None:
        assert t["board_substatus"] == substatus, (
            f"{task_id} board_substatus 应为 {substatus}："
            f"{t['board_substatus']}")
    return t


# ---------------------------------------------------------------------------
# 任务 1：分类互斥与计数（正常/明确失败/发送不明/失联/评审未通过 同时在场）
# ---------------------------------------------------------------------------


class TestClassificationMutex:
    @pytest.fixture
    def mixed(self, factory, cli, tmp_env):
        """一次造齐五类异常 + 五类正常任务；值统一为 task_id 字符串。"""
        s = {}

        # --- 正常 ---
        s["np"] = factory.sync_task(title="正常-待规划").task_id
        s["ready"] = factory.make_ready(title="正常-就绪").task.task_id
        s["running"] = factory.make_running(title="正常-执行中").task.task_id
        s["in_review"] = factory.make_in_review(
            title="正常-评审中").task.task_id
        done = factory.make_running(title="正常-已完成")
        cli("resolve", {"task_id": done.task.task_id, "action": "mark_done"})
        s["done"] = done.task.task_id

        # --- 明确失败（plan_failed）---
        pf = factory.sync_task(title="异常-规划失败")
        report_needs_user(cli, claim_request(factory, pf.task_id, "plan"),
                          "plan_failed", note="需求矛盾无法规划")
        s["plan_failed"] = pf.task_id

        # --- 发送不明 ---
        s["send_unknown"] = factory.make_needs_user(
            send_state="unknown", title="异常-发送不明").task.task_id

        # --- 失联（running + monitor 租约过期）---
        lost = factory.make_running(title="异常-失联")
        lc = claim_request(factory, lost.task.task_id, "monitor")
        expire_lease(tmp_env.db, lc["request_id"])
        s["lost"] = lost.task.task_id
        s["lost_request_id"] = lc["request_id"]

        # --- 评审未通过（changes_requested + fix 任务）---
        rj = factory.make_in_review(title="异常-评审未通过")
        src = rj.task.task_id
        rv = claim_request(factory, src, "review")
        fix = cli("create-review-fix", {
            "source_task_id": src, "title": "修复任务",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-V13-MUTEX"})
        rep = write_review_report(factory.env.app_support, task_id=src,
                                  conclusion="changes_requested")
        cli("register-review", {
            "task_id": src, "run_id": rv["run_id"],
            "report_path": str(rep.path), "conclusion": "changes_requested",
            "fix_task_id": fix["fix_task_id"], "review_key": "RK-V13-MUTEX"})
        s["review_rejected"] = src
        s["fix_id"] = fix["fix_task_id"]
        return s

    def test_columns_are_mutually_exclusive(self, factory, cli, mixed):
        """每任务恰归入一列；异常只在第六列；无异常时两列字段相同。"""
        assert_normal(factory, cli, mixed["np"], "needs_plan", "待规划")
        assert_normal(factory, cli, mixed["ready"], "todo", "就绪")
        assert_normal(factory, cli, mixed["running"],
                      "in_progress", "执行中")
        assert_normal(factory, cli, mixed["in_review"],
                      "in_review", "评审中")
        assert_normal(factory, cli, mixed["done"], "done", "已完成")

        assert_exception(factory, cli, mixed["plan_failed"],
                         kind="failed", label="规划失败", severity="failed",
                         lifecycle_column="needs_plan",
                         failure_code="plan_failed")
        assert_exception(factory, cli, mixed["send_unknown"],
                         kind="send_unknown", label="发送不明",
                         severity="blocked", lifecycle_column="todo",
                         failure_code="dispatch_failed")
        assert_exception(factory, cli, mixed["lost"],
                         kind="executor_lost", label="执行端失联",
                         severity="blocked", lifecycle_column="in_progress",
                         failure_code=None)
        assert_exception(factory, cli, mixed["review_rejected"],
                         kind="review_rejected", label="评审未通过·待修复",
                         severity="blocked", lifecycle_column="in_review",
                         failure_code=None,
                         fix_task_id=mixed["fix_id"])
        # 修复任务自身是正常卡，不占原任务的列
        assert_normal(factory, cli, mixed["fix_id"], "needs_plan", "待规划")

    def test_partition_and_counts(self, factory, cli, mixed):
        """六列过滤构成全分划（每任务恰出现一次）；overview.counts.board
        与 list-tasks 分组一致（§5.14-2）。"""
        rows = cli("list-tasks", {})["tasks"]
        ids = [t["task_id"] for t in rows]
        expected = {
            mixed["np"], mixed["ready"], mixed["running"],
            mixed["in_review"], mixed["done"], mixed["plan_failed"],
            mixed["send_unknown"], mixed["lost"], mixed["review_rejected"],
            mixed["fix_id"],
        }
        assert set(ids) == expected, (
            f"list-tasks 应含全部十项：缺 {expected - set(ids)}，"
            f"多 {set(ids) - expected}")
        assert len(ids) == len(set(ids)), "任务重复出现"

        seen = []
        for col in BOARD_COLUMNS:
            part = cli("list-tasks", {"board_column": col})["tasks"]
            assert all(t["board_column"] == col for t in part)
            seen += [t["task_id"] for t in part]
        assert sorted(seen) == sorted(ids), "六列过滤未构成全分划"

        manual = Counter(t["board_column"] for t in rows)
        board = cli("overview", {})["counts"]["board"]
        assert set(board) == set(BOARD_COLUMNS)
        for col in BOARD_COLUMNS:
            assert board[col] == manual.get(col, 0), (
                f"counts.board[{col}]={board[col]} 与分组 "
                f"{manual.get(col, 0)} 不一致")
        assert manual["failed"] == 4, f"应有四个异常卡：{manual}"
        assert board["failed"] == 4

        # 异常卡互斥：exception 非空 ⟺ 第六列
        for t in rows:
            assert (t["board_column"] == "failed") == (
                t["exception"] is not None), (
                f"{t['task_id']} 列/异常不一致：{t['board_column']} "
                f"{t['exception']}")
            if t["exception"] is None:
                assert t["board_column"] == t["lifecycle_column"]
            else:
                assert t["lifecycle_column"] in LIFECYCLE_COLUMNS
                assert t["lifecycle_column"] != t["board_column"]

    def test_failed_column_filter_and_get_consistency(self, factory, cli,
                                                      mixed):
        """第六列过滤只回异常卡；get-task 与 list-tasks 完全一致。"""
        failed = cli("list-tasks", {"board_column": "failed"})["tasks"]
        failed_ids = {t["task_id"] for t in failed}
        assert failed_ids == {
            mixed["plan_failed"], mixed["send_unknown"], mixed["lost"],
            mixed["review_rejected"],
        }
        for t in failed:
            assert t["board_substatus"] == t["exception"]["label"]
            assert t["board_substatus"] in EXCEPTION_LABELS
            assert t["blocked_reason"], "第六列必须有 blocked_reason"
            detail = factory.get_task(t["task_id"])
            assert detail["exception"] == t["exception"]
            assert detail["board_substatus"] == t["board_substatus"]
            assert detail["lifecycle_column"] == t["lifecycle_column"]


# ---------------------------------------------------------------------------
# 任务 1（续）：exception.kind/label/severity 判定表逐码覆盖
# ---------------------------------------------------------------------------


class TestExceptionKindTable:
    def test_plan_failed(self, factory, cli):
        t = factory.sync_task(title="判定-规划失败")
        report_needs_user(cli, claim_request(factory, t.task_id, "plan"),
                          "plan_failed")
        assert_exception(factory, cli, t.task_id, kind="failed",
                         label="规划失败", severity="failed",
                         lifecycle_column="needs_plan",
                         failure_code="plan_failed")

    def test_dispatch_failed(self, factory, cli):
        nu = factory.make_needs_user(send_state="not_sent",
                                     title="判定-派发失败")
        assert_exception(factory, cli, nu.task.task_id, kind="failed",
                         label="派发失败", severity="failed",
                         lifecycle_column="todo",
                         failure_code="dispatch_failed")

    def test_validation_failed(self, factory, cli):
        ready = factory.make_ready(title="判定-校验失败")
        tid = ready.task.task_id
        factory.kaneo.set_status(tid, "in-progress")
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        c = factory.claim("dispatch", request_id=req["request_id"])
        assert c["claimed"] is False and c["reason"] == "guard_failed"
        assert_exception(factory, cli, tid, kind="failed",
                         label="派发失败", severity="failed",
                         lifecycle_column="todo",
                         failure_code="validation_failed")

    def test_execution_failed(self, factory, cli):
        running = factory.make_running(title="判定-执行失败")
        report_needs_user(cli, claim_request(factory,
                                             running.task.task_id, "monitor"),
                          "execution_failed", note="Devin 执行失败")
        assert_exception(factory, cli, running.task.task_id, kind="failed",
                         label="执行失败", severity="failed",
                         lifecycle_column="in_progress",
                         failure_code="execution_failed")

    def test_send_unknown_wins_over_failure_code(self, factory, cli):
        """send_state='unknown' 优先于一切 code → send_unknown/发送不明。"""
        nu = factory.make_needs_user(send_state="unknown",
                                     title="判定-发送不明")
        t = assert_exception(factory, cli, nu.task.task_id,
                             kind="send_unknown", label="发送不明",
                             severity="blocked", lifecycle_column="todo",
                             failure_code="dispatch_failed")
        assert t["send_state"] == "unknown"
        assert t["display_status"] == "需人工"

    def test_reported_is_manual_block(self, factory, cli):
        """通用 reported → manual_block/需人工处理（不算失败）。"""
        t = factory.sync_task(title="判定-通用阻塞")
        report_needs_user(cli, claim_request(factory, t.task_id, "plan"),
                          "reported", note="需求不清待澄清")
        assert_exception(factory, cli, t.task_id, kind="manual_block",
                         label="需人工处理", severity="blocked",
                         lifecycle_column="needs_plan",
                         failure_code="reported")

    def test_unrecognized_code_is_manual_block(self, factory, cli):
        """未识别码一律按阻塞类处理——失败列只认明确证据（§5.14-1）。"""
        t = factory.sync_task(title="判定-未识别码")
        report_needs_user(cli, claim_request(factory, t.task_id, "plan"),
                          "some_new_code", note="未知阻塞")
        assert_exception(factory, cli, t.task_id, kind="manual_block",
                         label="需人工处理", severity="blocked",
                         lifecycle_column="needs_plan",
                         failure_code="some_new_code")

    def test_observation_blocked(self, factory, cli):
        running = factory.make_running(title="判定-监控异常")
        report_needs_user(cli, claim_request(factory,
                                             running.task.task_id, "monitor"),
                          "observation_blocked", note="锁屏无法观察")
        assert_exception(factory, cli, running.task.task_id,
                         kind="observation_blocked", label="监控异常",
                         severity="blocked", lifecycle_column="in_progress",
                         failure_code="observation_blocked")

    def test_review_blocked(self, factory, cli):
        ir = factory.make_in_review(title="判定-评审受阻")
        tid = ir.task.task_id
        rv = claim_request(factory, tid, "review")
        rep = write_review_report(factory.env.app_support, task_id=tid,
                                  conclusion="blocked")
        cli("register-review", {"task_id": tid, "run_id": rv["run_id"],
                                "report_path": str(rep.path),
                                "conclusion": "blocked"})
        t = assert_exception(factory, cli, tid, kind="review_blocked",
                             label="评审受阻", severity="blocked",
                             lifecycle_column="in_review",
                             failure_code="review_blocked")
        assert t["status"] == "needs_user"
        summary = t["review_summary"]
        assert summary and summary["conclusion"] == "blocked"

    def test_executor_lost(self, factory, cli, tmp_env):
        running = factory.make_running(title="判定-失联")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        expire_lease(tmp_env.db, c["request_id"])
        t = assert_exception(factory, cli, tid, kind="executor_lost",
                             label="执行端失联", severity="blocked",
                             lifecycle_column="in_progress",
                             failure_code=None)
        # 失联不是失败：库内状态保持 running，display_status 派生失联
        assert t["status"] == "running"
        assert t["display_status"] == "执行端失联"
        assert "失联" in t["blocked_reason"]

    def test_lost_dispatch_also_isolated(self, factory, cli, tmp_env):
        """派发中失联同样进第六列（规则 3 与 status 无关）。"""
        ready = factory.make_ready(title="判定-派发失联")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        c = factory.claim("dispatch", request_id=req["request_id"])
        expire_lease(tmp_env.db, c["request_id"])
        assert_exception(factory, cli, tid, kind="executor_lost",
                         label="执行端失联", severity="blocked",
                         lifecycle_column="todo", failure_code=None)

    def test_review_rejected_with_fix_task(self, factory, cli):
        """in_review + changes_requested → review_rejected，关联 fix_task_id。"""
        ir = factory.make_in_review(title="判定-评审未通过")
        src = ir.task.task_id
        rv = claim_request(factory, src, "review")
        fix = cli("create-review-fix", {
            "source_task_id": src, "title": "修复任务",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-V13-KIND"})
        rep = write_review_report(factory.env.app_support, task_id=src,
                                  conclusion="changes_requested")
        cli("register-review", {
            "task_id": src, "run_id": rv["run_id"],
            "report_path": str(rep.path), "conclusion": "changes_requested",
            "fix_task_id": fix["fix_task_id"], "review_key": "RK-V13-KIND"})
        t = assert_exception(factory, cli, src, kind="review_rejected",
                             label="评审未通过·待修复", severity="blocked",
                             lifecycle_column="in_review",
                             failure_code=None,
                             fix_task_id=fix["fix_task_id"])
        summary = t["review_summary"]
        assert summary["conclusion"] == "changes_requested"
        assert summary["fix_task_id"] == fix["fix_task_id"]
        assert summary["review_key"] == "RK-V13-KIND"
        assert_iso8601(summary["registered_at"])


# ---------------------------------------------------------------------------
# 任务 2：评审修复链（fix 独立流程 → 原任务回待评审）
# ---------------------------------------------------------------------------


class TestReviewFixChain:
    def _rejected(self, factory, cli, key):
        ir = factory.make_in_review(title="修复链-原任务")
        src = ir.task.task_id
        rv = claim_request(factory, src, "review")
        fix = cli("create-review-fix", {
            "source_task_id": src, "title": "修复链-修复任务",
            "description": "修复描述", "priority": "high",
            "dedupe_key": key})
        rep = write_review_report(factory.env.app_support, task_id=src,
                                  conclusion="changes_requested")
        cli("register-review", {
            "task_id": src, "run_id": rv["run_id"],
            "report_path": str(rep.path), "conclusion": "changes_requested",
            "fix_task_id": fix["fix_task_id"], "review_key": key})
        return src, fix["fix_task_id"], rv

    def test_fix_task_runs_independent_flow(self, factory, cli):
        """修复任务走自己的 needs_plan→ready 流程，不占原任务的列。"""
        src, fix_id, _rv = self._rejected(factory, cli, "RK-V13-CHAIN")
        assert_exception(factory, cli, src, kind="review_rejected",
                         label="评审未通过·待修复", severity="blocked",
                         lifecycle_column="in_review", fix_task_id=fix_id)
        # 修复任务：needs_plan 正常卡
        ft = assert_normal(factory, cli, fix_id, "needs_plan", "待规划")
        assert ft["plan_kind"] == "review_fix"
        assert ft["review_source_task_id"] == src
        assert ft["review_key"] == "RK-V13-CHAIN"

        pc = claim_request(factory, fix_id, "plan")
        detail = factory.get_task(fix_id)
        # review_fix 计划文件必须带评审关联 frontmatter（§5.22.4 校验）
        rep = write_review_report(factory.env.app_support, task_id=src,
                                  conclusion="changes_requested")
        plan = factory.publish(type("T", (), {
            "task_id": fix_id, "project_id": detail["project_id"],
            "project_name": detail["project_name"],
            "repository": detail["repository"],
            "source_sha256": detail["source_sha256"],
            "requirements_sha256": detail["requirements_sha256"],
            "title": "修复链-修复任务"})(), plan_kind="review_fix",
            extra_frontmatter={
                "review_report_path": str(rep.path),
                "review_report_sha256": rep.sha256,
                "review_source_task_id": src,
                "review_key": "RK-V13-CHAIN",
            })
        assert_normal(factory, cli, fix_id, "todo", "就绪")
        # 原任务仍在第六列，互不影响
        assert_exception(factory, cli, src, kind="review_rejected",
                         label="评审未通过·待修复", severity="blocked",
                         lifecycle_column="in_review", fix_task_id=fix_id)
        assert plan.resp["task_status"] == "ready"

    def test_source_returns_to_in_review_after_new_round(self, factory, cli,
                                                        tmp_env):
        """新一轮 in-review → 原任务回待评审列：上一周期结论清空、
        fix/review_key 历史保留、异常消失、新建 review 请求（§5.9/§5.15.4）。

        构造：修复完成后 Kaneo 卡片拖回 in-progress、任务回到 running
        （`set_task_status` 后门：sync-tasks 没有 in_review→running 的回退
        分支——已知产品缺口，v1.2 既有、非 v1.3 回归，已上报用户决定，本轮
        不改；core/tests/test_board.py 的同一构也走该后门），再 advance-task
        进新一轮 in-review。
        """
        src, fix_id, rv = self._rejected(factory, cli, "RK-V13-ROUND")
        assert_exception(factory, cli, src, kind="review_rejected",
                         label="评审未通过·待修复", severity="blocked",
                         lifecycle_column="in_review", fix_task_id=fix_id)
        # 评审执行端回报本轮完成（结论已登记）
        cli("report", {"request_id": rv["request_id"], "run_id": rv["run_id"],
                       "outcome": "succeeded", "note": "结论已登记"})

        # 修复完成 → 回 running（后门；等价于人把 Kaneo 卡片拖回 in-progress）
        factory.kaneo.set_status(src, "in-progress")
        set_task_status(tmp_env.db, src, "running")
        t = factory.get_task(src)
        assert t["status"] == "running"
        # 历史结论不参与归列：非 in_review 状态即使残留 changes_requested
        # 也不是异常卡（与 core/tests/test_columns.py 同一口径）
        t = assert_normal(factory, cli, src, "in_progress", "执行中")
        # 历史关联保留
        assert t["fix_task_id"] == fix_id
        assert t["review_key"] == "RK-V13-ROUND"

        # 推进新一轮 in-review（run_id 可选：§5.9 advance-task 契约）
        adv = cli("advance-task", {"task_id": src, "target": "in-review",
                                   "expected_current": "in-progress"})
        assert adv["ok"] is True
        assert adv["task_status"] == "in_review"

        # 新一轮 in-review 起始即清空上一周期结论 → 异常消失、回待评审列
        t = assert_normal(factory, cli, src, "in_review", "评审中")
        assert t["review_summary"] is None or \
            t["review_summary"].get("conclusion") != "changes_requested"
        # 历史关联保留
        assert t["fix_task_id"] == fix_id
        assert t["review_key"] == "RK-V13-ROUND"
        # 新建 review 请求（可再次评审）
        cli("tick", {})
        req = factory.find_request(src, "review")
        assert req is not None and req["status"] == "queued"

        # 新一轮 pass → done，异常不复活
        rv2 = claim_request(factory, src, "review")
        rep = write_review_report(factory.env.app_support, task_id=src,
                                  conclusion="pass")
        cli("register-review", {"task_id": src, "run_id": rv2["run_id"],
                                "report_path": str(rep.path),
                                "conclusion": "pass"})
        cli("advance-task", {"task_id": src, "target": "done",
                             "expected_current": "in-review"})
        t2 = assert_normal(factory, cli, src, "done", "已完成")
        assert t2["status"] == "done"
        assert t2["review_summary"]["conclusion"] == "pass"


# ---------------------------------------------------------------------------
# 任务 2（续）：历史失败不污染当前状态
# ---------------------------------------------------------------------------


class TestHistoryDoesNotPollute:
    def test_retry_clears_exception(self, factory, cli):
        """failed（dispatch 类）resolve retry → todo/已排队，exception 清空，
        历史 attempt/event 仍在但不参与归列（§5.15.4）。"""
        nu = factory.make_needs_user(send_state="not_sent",
                                     title="历史-派发失败")
        tid = nu.task.task_id
        assert_exception(factory, cli, tid, kind="failed",
                         label="派发失败", severity="failed",
                         lifecycle_column="todo",
                         failure_code="dispatch_failed")
        attempts_before = len(factory.get_task(tid)["attempts"])
        cli("resolve", {"task_id": tid, "action": "retry"})
        t = assert_normal(factory, cli, tid, "todo", "已排队")
        assert t["status"] == "ready"
        assert t["failure_code"] is None
        assert not t["blocked_reason"]
        # 历史执行记录保留（审计），只是不再影响归列
        assert len(t["attempts"]) >= attempts_before

    def test_retry_plan_failure_then_republish(self, factory, cli):
        """plan_failed → retry → needs_plan → 再规划成功 → todo，全程无异常。"""
        t = factory.sync_task(title="历史-规划失败")
        tid = t.task_id
        report_needs_user(cli, claim_request(factory, tid, "plan"),
                          "plan_failed")
        assert_exception(factory, cli, tid, kind="failed",
                         label="规划失败", severity="failed",
                         lifecycle_column="needs_plan",
                         failure_code="plan_failed")
        cli("resolve", {"task_id": tid, "action": "retry"})
        assert_normal(factory, cli, tid, "needs_plan", "待规划")
        claim_request(factory, tid, "plan")
        factory.publish(t)
        # publish-plan 已结束请求，不再 report
        assert_normal(factory, cli, tid, "todo", "就绪")

    def test_execution_failed_then_recovered_running(self, factory, cli):
        """execution_failed → replan → 重走 plan → ready：异常清除、历史失败
        不污染当前归列（§5.15.4）。

        附带固化一个已知产品缺口（v1.2 既有、非 v1.3 回归，已上报用户决定，
        （conflicting send evidence，v2 起码为 send_conflict，归列为
        manual_block/需人工处理），且 §4.10 的
        resolve 动作里没有一个能清掉 send_state="sent" 的发送证据（retry 仅限
        not_sent；restore/mark_sent/accept_duplicate 仅限 unknown）——
        replan 之后重派发无路可走。此处只断言当前行为，防回归。
        """
        running = factory.make_running(title="历史-执行失败")
        tid = running.task.task_id
        report_needs_user(cli, claim_request(factory, tid, "monitor"),
                          "execution_failed", note="执行失败一次")
        assert_exception(factory, cli, tid, kind="failed",
                         label="执行失败", severity="failed",
                         lifecycle_column="in_progress",
                         failure_code="execution_failed")
        cli("resolve", {"task_id": tid, "action": "replan"})
        assert_normal(factory, cli, tid, "needs_plan", "待规划")
        # publish-plan 前置复核：远端须回 To Do（任务由人/外部拉回）
        factory.kaneo.set_status(tid, "to-do")
        # 重新规划 → publish → ready：异常与失败码已清空（历史不污染）
        task = running.task
        claim_request(factory, tid, "plan")
        factory.publish(task)
        # publish-plan 已结束请求，不再 report
        t = assert_normal(factory, cli, tid, "todo", "就绪")
        assert t["status"] == "ready"
        assert t["failure_code"] is None
        assert not t["blocked_reason"]
        assert t["exception"] is None

        # 重派发：校验 6 拒绝（已发送证据冲突）→ 新的 needs_user。
        # v2：该守卫码为 send_conflict（§5.14 失败码表外），归列为
        # manual_block/需人工处理而非派发失败。
        factory.enqueue_dispatch(tid)
        dreq = factory.find_request(tid, "dispatch")
        assert dreq is not None, "replan 后应可重新入队"
        d = factory.claim("dispatch", request_id=dreq["request_id"])
        assert d["claimed"] is False
        assert d.get("reason") == "guard_failed"
        assert d.get("code") == "send_conflict"
        blocked = assert_exception(factory, cli, tid, kind="manual_block",
                                   label="需人工处理", severity="blocked",
                                   lifecycle_column="todo",
                                   failure_code="send_conflict")
        assert blocked["status"] == "needs_user"
        # 事件里仍有历史失败记录（审计不丢），但归列不看它
        kinds = {e["kind"] for e in cli("list-events",
                                        {"task_id": tid})["events"]}
        assert "reported" in kinds

    def test_release_lost_clears_exception(self, factory, cli, tmp_env):
        """失联 → release_lost → 回 in_progress，异常与 blocked_reason 清空。"""
        running = factory.make_running(title="历史-失联恢复")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        expire_lease(tmp_env.db, c["request_id"])
        assert_exception(factory, cli, tid, kind="executor_lost",
                         label="执行端失联", severity="blocked",
                         lifecycle_column="in_progress")
        cli("resolve", {"task_id": tid, "action": "release_lost"})
        t = assert_normal(factory, cli, tid, "in_progress", "执行中")
        assert t["display_status"] == "执行中"
        assert not t["blocked_reason"]
        assert factory.find_request(tid, "monitor") is not None

    def test_send_unknown_restore_clears_exception(self, factory, cli):
        """发送不明 → resolve restore → todo/已排队，异常清空。"""
        nu = factory.make_needs_user(send_state="unknown",
                                     title="历史-发送不明恢复")
        tid = nu.task.task_id
        assert_exception(factory, cli, tid, kind="send_unknown",
                         label="发送不明", severity="blocked",
                         lifecycle_column="todo")
        cli("resolve", {"task_id": tid, "action": "restore"})
        t = assert_normal(factory, cli, tid, "todo", "已排队")
        assert t["send_state"] == "not_sent"
        assert t["sent"] in (0, False)

    def test_observation_blocked_auto_cleared(self, factory, cli):
        """观测异常自动清除：monitor 观测成功 → 回 running、异常消失
        （§5.15.4）；发送不明/人工阻塞/失联不适用（见上一个测试）。

        构造：in_review 任务同时持有一个 monitor 请求与 review 请求；
        先由 review 侧 report needs_user(observation_blocked) 阻塞任务
        （monitor 请求仍 claimed），再由 monitor report succeeded 触发自动清除。
        """
        ir = factory.make_in_review(title="观测自动清除")
        tid = ir.task.task_id
        cli("enqueue", {"task_id": tid, "kind": "monitor"})
        mreq = factory.find_request(tid, "monitor")
        m = factory.claim("monitor", request_id=mreq["request_id"])
        assert m["claimed"] is True
        rreq = factory.find_request(tid, "review")
        r = factory.claim("review", request_id=rreq["request_id"])
        assert r["claimed"] is True
        report_needs_user(cli, r, "observation_blocked", note="暂时锁屏")
        assert_exception(factory, cli, tid, kind="observation_blocked",
                         label="监控异常", severity="blocked",
                         lifecycle_column="in_review",
                         failure_code="observation_blocked")

        out = cli("report", {"request_id": m["request_id"],
                             "run_id": m["run_id"],
                             "outcome": "succeeded", "note": "连接恢复"})
        assert out["ok"] is True
        t = assert_normal(factory, cli, tid, "in_progress", "执行中")
        assert t["status"] == "running"
        assert t["failure_code"] is None
        assert not t["blocked_reason"]

    def test_session_log_does_not_clear_exception(self, factory, cli,
                                                  tmp_env):
        """收到新会话日志不解除任何异常（§5.15.4）：会话命令只读，
        不改变异常归列。"""
        running = factory.make_running(title="会话不解除异常")
        tid = running.task.task_id
        c = claim_request(factory, tid, "monitor")
        report_needs_user(cli, c, "execution_failed", note="执行失败")
        assert_exception(factory, cli, tid, kind="failed",
                         label="执行失败", severity="failed",
                         lifecycle_column="in_progress",
                         failure_code="execution_failed")
        marker = factory.get_task(tid)["session_marker"]
        assert marker
        # 会话读取命令（无 Devin 数据源 → unavailable，但不得改异常）
        cli("session-messages", {"task_id": tid})
        cli("get-session", {"task_id": tid, "run_id":
                            factory.get_task(tid)["run_id"]})
        cli("list-sessions", {"task_id": tid})
        assert_exception(factory, cli, tid, kind="failed",
                         label="执行失败", severity="failed",
                         lifecycle_column="in_progress",
                         failure_code="execution_failed")


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
