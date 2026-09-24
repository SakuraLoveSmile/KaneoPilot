"""save_state / archive-task / trash-task / restore-task 契约测试（v1.3）。

覆盖契约条款：protocol §5.15.1（save_state 与生命周期正交、三态语义、
「已完成 ≠ 已归档」）、§5.15.2（三个命令的 payload/效果、禁用条件与中文
冲突原因、排队取消与领取竞争的事务口径、幂等）、§5.15.3（调度入口排除：
list-tasks 过滤、overview 只统计 active、tick/claim/enqueue/sync-tasks）、
§5.15.6（allowed_actions 新增 archive/trash/restore/resume-auto）；
state-model §2.1（save_state 转换表与事件 kind）。

竞争用例（取消 queued 与领取并发）见 `test_concurrency.py`——真实多进程。

DB 后门 `set_save_state` 仅用于隔离验证 §5.15.3 排除逻辑（archive-task 前置
拒绝 running/in_review 等可被 tick/claim 选中的状态，纯 CLI 无法构造），
不用于伪造业务状态。
"""

import pytest

try:
    from conftest import (
        EXECUTORS, assert_iso8601, expire_lease, rewind_finished_at,
        set_save_state, ui_evidence,
    )
except ImportError:  # unittest discover 包上下文
    from tests.conftest import (
        EXECUTORS, assert_iso8601, expire_lease, rewind_finished_at,
        set_save_state, ui_evidence,
    )


SAVE_STATES = ("active", "archived", "trashed")

# §5.15.2 禁用条件：正在规划/派发/执行/评审（constants.ARCHIVE_BLOCKED_STATUSES）
BLOCKED_STATUSES = (
    "planning", "dispatching", "prepared", "submitting", "running", "in_review",
)


def _sync_payload(task, kaneo_status=None):
    """按 §5.4 形状重建 sync-tasks payload（用于「再次同步不重导入」）。"""
    return {
        "executor": EXECUTORS["plan"],
        "projects": [{
            "project_id": task.project_id,
            "project_name": task.project_name,
            "repository": str(task.repository),
            "tasks": [{
                "task_id": task.task_id,
                "title": task.title,
                "kaneo_status": kaneo_status or "to-do",
                "requirements_sha256": task.requirements_sha256,
                "source_sha256": task.source_sha256,
                "source_path": str(task.source_path),
            }],
        }],
    }


def _events(cli, task_id):
    return cli("list-events", {"task_id": task_id})["events"]


def _kinds(cli, task_id):
    return {e["kind"] for e in _events(cli, task_id)}


def _row(cli, task_id, save_state=None):
    payload = {} if save_state is None else {"save_state": save_state}
    return next((t for t in cli("list-tasks", payload)["tasks"]
                 if t["task_id"] == task_id), None)


# ---------------------------------------------------------------------------
# §5.15.1 字段与语义
# ---------------------------------------------------------------------------


class TestSaveStateFields:
    def test_summary_and_detail_fields(self, factory, cli):
        """TaskSummary/TaskDetail 均含 save_state/saved_at/auto_advance_paused。"""
        ready = factory.make_ready(title="保存状态字段")
        row = factory.list_task(ready.task.task_id)
        assert {"save_state", "saved_at", "auto_advance_paused"} <= set(row)
        detail = factory.get_task(ready.task.task_id)
        assert {"save_state", "saved_at", "auto_advance_paused"} <= set(detail)
        assert row["save_state"] == "active"
        assert row["saved_at"] is None
        assert row["auto_advance_paused"] in (0, False)

    def test_done_is_not_archived(self, factory, cli):
        """已完成（board_column=done）与已归档（save_state=archived）是两个
        概念：完成任务不自动归档（§5.15.1）。"""
        running = factory.make_running(title="完成不归档")
        tid = running.task.task_id
        cli("resolve", {"task_id": tid, "action": "mark_done"})
        t = factory.get_task(tid)
        assert t["status"] == "done"
        assert t["save_state"] == "active"
        assert _row(cli, tid) is not None, "done 任务仍在活跃看板"

    def test_lifecycle_untouched_by_save_state(self, factory, cli):
        """save_state 不改写 status/stage，也不产生 status_change 事件
        （state-model §2.1）。"""
        ready = factory.make_ready(title="归档不改生命周期")
        tid = ready.task.task_id
        before = factory.get_task(tid)
        status_changes_before = len([e for e in _events(cli, tid)
                                     if e["kind"] == "status_change"])
        cli("archive-task", {"task_id": tid})
        after = factory.get_task(tid)
        assert after["status"] == before["status"] == "ready"
        assert after["stage"] == before["stage"]
        status_changes_after = len([e for e in _events(cli, tid)
                                    if e["kind"] == "status_change"])
        assert status_changes_after == status_changes_before, (
            "归档不应产生 status_change 事件")


# ---------------------------------------------------------------------------
# §5.15.2 archive-task / trash-task
# ---------------------------------------------------------------------------


class TestArchiveTrash:
    def test_archive_active_task(self, factory, cli):
        ready = factory.make_ready(title="可归档任务")
        tid = ready.task.task_id
        out = cli("archive-task", {"task_id": tid})
        assert out.get("ok") is True
        t = factory.get_task(tid)
        assert t["save_state"] == "archived"
        assert_iso8601(t["saved_at"])
        assert "task_archived" in _kinds(cli, tid)
        # 默认列表不再出现；archived/all 出现
        assert _row(cli, tid) is None
        assert _row(cli, tid, "archived") is not None
        assert _row(cli, tid, "all") is not None
        assert _row(cli, tid, "trashed") is None

    def test_trash_active_task(self, factory, cli):
        ready = factory.make_ready(title="可回收任务")
        tid = ready.task.task_id
        out = cli("trash-task", {"task_id": tid})
        assert out.get("ok") is True
        t = factory.get_task(tid)
        assert t["save_state"] == "trashed"
        assert_iso8601(t["saved_at"])
        assert "task_trashed" in _kinds(cli, tid)
        assert _row(cli, tid, "trashed") is not None
        assert _row(cli, tid) is None

    def test_archive_cancels_queued_requests(self, factory, cli):
        """同事务取消该任务 queued 请求（§5.15.2）。"""
        ready = factory.make_ready(title="归档取消排队")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        assert req is not None and req["status"] == "queued"
        cli("archive-task", {"task_id": tid})
        after = next(r for r in cli("list-requests", {})["requests"]
                     if r["request_id"] == req["request_id"])
        assert after["status"] == "cancelled"
        assert "request_cancelled" in _kinds(cli, tid)

    def test_archive_idempotent(self, factory, cli):
        """已归档再归档 → deduplicated，不重复写事件（§5.15.2 幂等）。"""
        ready = factory.make_ready(title="归档幂等")
        tid = ready.task.task_id
        cli("archive-task", {"task_id": tid})
        n_before = len([e for e in _events(cli, tid)
                        if e["kind"] == "task_archived"])
        out = cli("archive-task", {"task_id": tid})
        assert out.get("ok") is True
        assert out.get("deduplicated") is True, f"应标记幂等：{out}"
        n_after = len([e for e in _events(cli, tid)
                       if e["kind"] == "task_archived"])
        assert n_after == n_before == 1

    def test_trash_idempotent(self, factory, cli):
        ready = factory.make_ready(title="回收幂等")
        tid = ready.task.task_id
        cli("trash-task", {"task_id": tid})
        out = cli("trash-task", {"task_id": tid})
        assert out.get("deduplicated") is True
        assert factory.get_task(tid)["save_state"] == "trashed"

    @pytest.mark.parametrize("status", BLOCKED_STATUSES)
    def test_blocked_statuses_conflict(self, factory, cli, status):
        """planning/dispatching/prepared/submitting/running/in_review
        不可移出：conflict + 中文原因，save_state 保持 active（§5.15.2）。"""
        tid = None
        if status == "planning":
            task = factory.sync_task(title="规划中归档")
            tid = task.task_id
            factory.queue_plan(task)
            req = factory.find_request(tid, "plan")
            factory.claim("plan", request_id=req["request_id"])
        elif status == "dispatching":
            ready = factory.make_ready(title="派发中归档")
            tid = ready.task.task_id
            factory.enqueue_dispatch(tid)
            req = factory.find_request(tid, "dispatch")
            factory.claim("dispatch", request_id=req["request_id"])
        elif status == "prepared":
            ready = factory.make_ready(title="已备归档")
            tid = ready.task.task_id
            factory.enqueue_dispatch(tid)
            req = factory.find_request(tid, "dispatch")
            c = factory.claim("dispatch", request_id=req["request_id"])
            cli("dispatch-prepare", {
                "task_id": tid, "run_id": c["run_id"],
                "evidence": ui_evidence(
                    session_marker=c["context"]["session_marker"],
                    repository=c["context"]["repository"],
                    attachment_name=c["context"]["attachment_name"])})
        elif status == "submitting":
            ready = factory.make_ready(title="发送中归档")
            tid = ready.task.task_id
            factory.enqueue_dispatch(tid)
            req = factory.find_request(tid, "dispatch")
            c = factory.claim("dispatch", request_id=req["request_id"])
            cli("dispatch-prepare", {
                "task_id": tid, "run_id": c["run_id"],
                "evidence": ui_evidence(
                    session_marker=c["context"]["session_marker"],
                    repository=c["context"]["repository"],
                    attachment_name=c["context"]["attachment_name"])})
            cli("dispatch-begin-send", {"task_id": tid, "run_id": c["run_id"]})
        elif status == "running":
            tid = factory.make_running(title="执行中归档").task.task_id
        else:  # in_review
            tid = factory.make_in_review(title="评审中归档").task.task_id

        assert factory.get_task(tid)["status"] == status, "前置状态构造失败"
        out = cli("archive-task", {"task_id": tid}, expect=1)
        assert out["code"] == "conflict"
        assert out["error"], "冲突必须给原因（UI 展示）"
        assert any(token in out["error"] for token in
                   ("执行", "评审", "规划", "派发", "发送")), (
            f"冲突原因应说明正在进行的状态：{out['error']!r}")
        t = factory.get_task(tid)
        assert t["save_state"] == "active"
        assert t["saved_at"] is None
        assert "task_archived" not in _kinds(cli, tid)

    def test_blocked_unresolved_lost_request(self, factory, cli, tmp_env):
        """存在未解除失联请求（claimed 且租约过期）→ conflict。

        构造：running → monitor claim → 远端冲突使 task 转 needs_user
        （monitor 请求仍 claimed）→ 租约过期。此时 status=needs_user 本身
        可移出，唯独失联请求构成禁用条件。
        """
        running = factory.make_running(title="失联未解除")
        tid = running.task.task_id
        mreq = factory.find_request(tid, "monitor")
        m = factory.claim("monitor", request_id=mreq["request_id"])
        factory.kaneo.set_status(tid, "done")     # 远端被人为推进
        out = cli("advance-task", {
            "task_id": tid, "run_id": m["run_id"], "target": "in-review",
            "expected_current": "in-progress"}, expect=1)
        assert out["code"] == "remote_conflict"
        assert factory.get_task(tid)["status"] == "needs_user"
        expire_lease(tmp_env.db, m["request_id"])
        arch = cli("archive-task", {"task_id": tid}, expect=1)
        assert arch["code"] == "conflict"
        assert "失联" in arch["error"], f"应说明失联未解除：{arch['error']!r}"
        assert factory.get_task(tid)["save_state"] == "active"

        # 解除失联后即可移出：release_lost 只解除失联，任务停在 needs_user
        # （needs_user 的其他阻塞允许移出，见 test_needs_user_other_block_allowed）
        cli("resolve", {"task_id": tid, "action": "release_lost"})
        assert factory.get_task(tid)["status"] == "needs_user"
        ok = cli("archive-task", {"task_id": tid})
        assert ok.get("ok") is True
        assert factory.get_task(tid)["save_state"] == "archived"

    def test_blocked_send_unknown(self, factory, cli):
        """send_state='unknown'（发送不明）不可移出 → conflict（§5.15.2）。"""
        nu = factory.make_needs_user(send_state="unknown", title="发送不明归档")
        tid = nu.task.task_id
        out = cli("archive-task", {"task_id": tid}, expect=1)
        assert out["code"] == "conflict"
        assert "发送不明" in out["error"] or "不明" in out["error"]
        assert factory.get_task(tid)["save_state"] == "active"
        # trash 同样拒绝
        out2 = cli("trash-task", {"task_id": tid}, expect=1)
        assert out2["code"] == "conflict"
        # resolve restore 认定未发送 → ready → 可移出
        cli("resolve", {"task_id": tid, "action": "restore"})
        assert factory.get_task(tid)["status"] == "ready"
        ok = cli("archive-task", {"task_id": tid})
        assert ok.get("ok") is True
        # 恢复时建的 dispatch 请求已被取消（历史请求可以是 succeeded/failed）
        reqs = [r for r in cli("list-requests", {})["requests"]
                if r["task_id"] == tid and r["kind"] == "dispatch"]
        assert reqs, "resolve restore 应重建 dispatch 请求"
        assert all(r["status"] != "queued" for r in reqs), reqs
        assert any(r["status"] == "cancelled" for r in reqs), reqs

    @pytest.mark.parametrize("status", ["needs_plan", "ready", "done"])
    def test_allowed_statuses(self, factory, cli, status):
        """needs_plan/ready/done 与 needs_user 其他阻塞允许移出（§5.15.2）。"""
        if status == "needs_plan":
            tid = factory.sync_task(title="待规划归档").task_id
        elif status == "ready":
            tid = factory.make_ready(title="就绪归档").task.task_id
        else:
            running = factory.make_running(title="完成归档")
            tid = running.task.task_id
            cli("resolve", {"task_id": tid, "action": "mark_done"})
        assert factory.get_task(tid)["status"] == status
        assert cli("archive-task", {"task_id": tid}).get("ok") is True
        assert factory.get_task(tid)["save_state"] == "archived"

    def test_needs_user_other_block_allowed(self, factory, cli):
        """needs_user 的其他阻塞（如 reported）允许移出。"""
        task = factory.sync_task(title="阻塞可归档")
        tid = task.task_id
        factory.queue_plan(task)
        req = factory.find_request(tid, "plan")
        c = factory.claim("plan", request_id=req["request_id"])
        cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                       "outcome": "needs_user", "failure_code": "reported",
                       "failure_reason": "需求不清"})
        assert factory.get_task(tid)["status"] == "needs_user"
        assert cli("trash-task", {"task_id": tid}).get("ok") is True
        assert factory.get_task(tid)["save_state"] == "trashed"


# ---------------------------------------------------------------------------
# §5.15.2 restore-task
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_from_archived(self, factory, cli):
        ready = factory.make_ready(title="归档恢复")
        tid = ready.task.task_id
        cli("archive-task", {"task_id": tid})
        out = cli("restore-task", {"task_id": tid})
        assert out.get("ok") is True
        t = factory.get_task(tid)
        assert t["save_state"] == "active"
        assert t["saved_at"] is None
        # 恢复后默认暂停自动推进（§5.15.2）
        assert t["auto_advance_paused"] in (1, True)
        assert "task_restored" in _kinds(cli, tid)
        assert _row(cli, tid) is not None

    def test_restore_resume_auto(self, factory, cli):
        ready = factory.make_ready(title="恢复并自动")
        tid = ready.task.task_id
        cli("trash-task", {"task_id": tid})
        out = cli("restore-task", {"task_id": tid, "resume_auto": True})
        assert out.get("ok") is True
        t = factory.get_task(tid)
        assert t["save_state"] == "active"
        assert t["auto_advance_paused"] in (0, False)
        # 未暂停 → 可正常 enqueue
        assert factory.enqueue_dispatch(tid)["request"]["kind"] == "dispatch"

    def test_restore_active_is_idempotent(self, factory, cli):
        """对 active 任务恢复同样幂等（仍执行状态核验并返回任务）。"""
        ready = factory.make_ready(title="恢复幂等")
        tid = ready.task.task_id
        out = cli("restore-task", {"task_id": tid})
        assert out.get("ok") is True
        t = factory.get_task(tid)
        assert t["save_state"] == "active"
        assert t["saved_at"] is None
        assert t["auto_advance_paused"] in (0, False)

    def test_restore_does_not_touch_kaneo(self, factory, cli):
        """恢复不改 Kaneo 状态：远端列保持 to-do（§5.15.1）。"""
        ready = factory.make_ready(title="恢复不动远端")
        tid = ready.task.task_id
        before = factory.kaneo.get_task(tid)["status"]
        cli("archive-task", {"task_id": tid})
        cli("restore-task", {"task_id": tid})
        assert factory.kaneo.get_task(tid)["status"] == before

    def test_restore_remote_unavailable_does_not_block(self, factory, cli):
        """远端不可达 → warning 不阻断恢复（§5.15.2）。"""
        ready = factory.make_ready(title="恢复时远端挂")
        tid = ready.task.task_id
        cli("archive-task", {"task_id": tid})
        factory.kaneo.fail_next(500, count=50)   # 之后所有远端请求 500
        out = cli("restore-task", {"task_id": tid})
        assert out.get("ok") is True
        t = factory.get_task(tid)
        assert t["save_state"] == "active"
        if "warnings" in out:
            assert isinstance(out["warnings"], list)


# ---------------------------------------------------------------------------
# §5.15.3 list-tasks 过滤 / overview 只统计 active
# ---------------------------------------------------------------------------


class TestSaveStateFilters:
    def test_save_state_filter(self, factory, cli):
        active = factory.make_ready(title="活跃任务")
        archived = factory.make_ready(project="Music", title="归档任务")
        trashed = factory.make_ready(project="Library", title="回收任务")
        cli("archive-task", {"task_id": archived.task.task_id})
        cli("trash-task", {"task_id": trashed.task.task_id})

        default = {t["task_id"] for t in cli("list-tasks", {})["tasks"]}
        assert active.task.task_id in default
        assert archived.task.task_id not in default
        assert trashed.task.task_id not in default

        only_archived = cli("list-tasks", {"save_state": "archived"})["tasks"]
        assert [t["task_id"] for t in only_archived] == [archived.task.task_id]
        only_trashed = cli("list-tasks", {"save_state": "trashed"})["tasks"]
        assert [t["task_id"] for t in only_trashed] == [trashed.task.task_id]
        everything = {t["task_id"] for t in
                      cli("list-tasks", {"save_state": "all"})["tasks"]}
        assert {active.task.task_id, archived.task.task_id,
                trashed.task.task_id} <= everything

    def test_invalid_save_state_bad_request(self, cli):
        out = cli("list-tasks", {"save_state": "deleted"}, expect=1)
        assert out["code"] == "bad_request"

    def test_overview_counts_only_active(self, factory, cli, tmp_env):
        """overview.counts 只统计 active（§5.15.3）。

        running 任务不可经 archive-task 移出（前置拒绝），故用 `set_save_state`
        后门直接置 archived 以隔离「计数排除」——该后门的既定用途。
        """
        a = factory.make_ready(title="计数活跃")
        b = factory.make_running(project="Music", title="计数归档")
        set_save_state(tmp_env.db, b.task.task_id, "archived")
        counts = cli("overview", {})["counts"]
        board = counts["board"]
        total = sum(board.values())
        rows = cli("list-tasks", {})["tasks"]
        assert total == len(rows), (
            f"counts.board 合计 {total} 与 active 列表 {len(rows)} 不一致")
        assert all(t["save_state"] == "active" for t in rows)
        # 归档任务不计入任何列
        assert board.get("in_progress", 0) == 0
        assert board.get("todo", 0) == 1
        assert a.task.task_id in {t["task_id"] for t in rows}

    def test_board_column_filter_respects_save_state(self, factory, cli,
                                                    tmp_env):
        """board_column 过滤只在 active 集合内（§5.2 + §5.15.3）。"""
        running = factory.make_running(title="过滤与归档")
        tid = running.task.task_id
        assert cli("list-tasks", {"board_column": "in_progress"})["tasks"]
        set_save_state(tmp_env.db, tid, "archived")
        assert cli("list-tasks", {"board_column": "in_progress"})["tasks"] == []
        assert _row(cli, tid, "archived") is not None


# ---------------------------------------------------------------------------
# §5.15.3 调度入口排除（tick / claim / enqueue / sync-tasks）
# ---------------------------------------------------------------------------


def _complete_monitor(factory, cli, task_id):
    """完成该任务的 monitor 请求（claim + report succeeded）。"""
    req = factory.find_request(task_id, "monitor")
    assert req is not None, f"{task_id} 无 monitor 请求"
    c = factory.claim("monitor", request_id=req["request_id"])
    assert c["claimed"] is True
    cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                   "outcome": "succeeded", "note": "观测一次"})
    return c


class TestSchedulingExclusions:
    def test_tick_skips_archived_and_paused(self, factory, cli, tmp_env):
        """tick 不为 save_state != active 或 auto_advance_paused=1 的任务建
        monitor 请求（§5.15.3）；workspace 级 plan-scan 不受影响。

        注意顺序：先经 CLI 完成三个任务的 monitor 请求，再用 DB 后门置
        save_state 并回退 finished_at，最后才 tick——否则中间任意 CLI 都会
        跑 tick_inline 把「已到期」的 monitor 请求先建出来（时间后门的固有
        副作用），无法归因到本次 tick。
        """
        control = factory.make_running(title="tick 对照")
        archived = factory.make_running(project="Music", title="tick 归档")
        paused = factory.make_running(project="Library", title="tick 暂停")

        finished = {}
        for task in (control, archived, paused):
            c = _complete_monitor(factory, cli, task.task.task_id)
            finished[task.task.task_id] = c["request_id"]

        set_save_state(tmp_env.db, archived.task.task_id, "archived")
        set_save_state(tmp_env.db, paused.task.task_id, "active",
                       auto_advance_paused=1)
        for request_id in finished.values():
            rewind_finished_at(tmp_env.db, request_id, 3600)

        out = cli("tick", {})
        created = out.get("created") or []
        created_tasks = {c.get("task_id") for c in created}
        assert control.task.task_id in created_tasks, (
            f"对照组应再建 monitor 请求：{created}")
        assert archived.task.task_id not in created_tasks
        assert paused.task.task_id not in created_tasks
        # 没有为被排除任务新建任何请求
        for tid in (archived.task.task_id, paused.task.task_id):
            reqs = [r for r in cli("list-requests", {})["requests"]
                    if r["task_id"] == tid and r["kind"] == "monitor"]
            assert len(reqs) == 1, f"{tid} 不应有第二个 monitor 请求：{reqs}"
        # 被排除原因可在 skipped 中观察（task_saved）
        skipped = {s.get("task_id"): s.get("reason")
                   for s in (out.get("skipped") or [])}
        assert skipped.get(archived.task.task_id) == "task_saved"
        assert skipped.get(paused.task.task_id) == "task_saved"

    def test_claim_skips_archived_auto_pick(self, factory, cli, tmp_env):
        """自动取单跳过非 active / 暂停任务（视不存在）。"""
        ready = factory.make_ready(title="自动取单跳过")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        set_save_state(tmp_env.db, tid, "archived")
        out = cli("claim", {"stage": "dispatch",
                            "executor": EXECUTORS["dispatch"]})
        assert out["claimed"] is False
        assert out.get("reason") in ("empty", None), (
            f"自动取单应视不存在：{out}")
        # 库内请求仍 queued（未被领取）
        req = factory.find_request(tid, "dispatch")
        assert req is not None and req["status"] == "queued"

    def test_claim_by_request_id_reports_reason(self, factory, cli, tmp_env):
        """按 request_id 指定领取 → task_archived / task_paused（§5.15.3）。"""
        ready = factory.make_ready(title="指定领取归档")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        set_save_state(tmp_env.db, tid, "archived")
        out = cli("claim", {"stage": "dispatch",
                            "executor": EXECUTORS["dispatch"],
                            "request_id": req["request_id"]})
        assert out["claimed"] is False
        assert out.get("reason") == "task_archived", f"应给 task_archived：{out}"

        ready2 = factory.make_ready(project="Music", title="指定领取暂停")
        factory.enqueue_dispatch(ready2.task.task_id)
        req2 = factory.find_request(ready2.task.task_id, "dispatch")
        set_save_state(tmp_env.db, ready2.task.task_id, "active",
                       auto_advance_paused=1)
        out2 = cli("claim", {"stage": "dispatch",
                             "executor": EXECUTORS["dispatch"],
                             "request_id": req2["request_id"]})
        assert out2["claimed"] is False
        assert out2.get("reason") == "task_paused", f"应给 task_paused：{out2}"

    def test_enqueue_conflicts_for_non_active(self, factory, cli):
        """enqueue 对非 active / 暂停任务 → conflict（§5.15.3）。"""
        ready = factory.make_ready(title="入队冲突")
        tid = ready.task.task_id
        cli("archive-task", {"task_id": tid})
        out = cli("enqueue", {"task_id": tid}, expect=1)
        assert out["code"] == "conflict"
        assert out["error"]

    def test_enqueue_conflicts_when_auto_paused(self, factory, cli):
        """auto_advance_paused=1 → conflict，原因写明暂停自动推进。"""
        ready = factory.make_ready(title="暂停入队")
        tid = ready.task.task_id
        cli("trash-task", {"task_id": tid})
        cli("restore-task", {"task_id": tid})       # 默认 auto_advance_paused=1
        assert factory.get_task(tid)["auto_advance_paused"] in (1, True)
        out = cli("enqueue", {"task_id": tid}, expect=1)
        assert out["code"] == "conflict"
        assert "暂停自动推进" in out["error"], f"应说明暂停原因：{out['error']!r}"

    def test_sync_skips_non_active(self, factory, cli):
        """sync-tasks 整体跳过非 active 任务：不 upsert、不改状态/指纹、
        不建请求——后续同步不得把归档/回收站任务重新导入看板（§5.15.3）。"""
        ready = factory.make_ready(title="同步跳过归档")
        task = ready.task
        tid = task.task_id
        cli("archive-task", {"task_id": tid})
        before = factory.get_task(tid)

        payload = _sync_payload(task, kaneo_status="in-progress")
        payload["projects"][0]["tasks"][0]["requirements_sha256"] = "e" * 64
        out = cli("sync-tasks", payload)
        after = factory.get_task(tid)
        assert after["save_state"] == "archived", "不得重新导入看板"
        assert after["status"] == before["status"] == "ready"
        assert after["requirements_sha256"] == before["requirements_sha256"]
        # 未建新请求
        reqs = [r for r in cli("list-requests", {})["requests"]
                if r["task_id"] == tid]
        assert all(r["status"] != "queued" for r in reqs), reqs
        assert "requirements_changed" not in _kinds(cli, tid)
        # upserted 不计入被跳过任务
        assert out.get("upserted", 0) == 0, f"被跳过任务不应计入：{out}"


# ---------------------------------------------------------------------------
# §5.15.6 allowed_actions
# ---------------------------------------------------------------------------


class TestAllowedActionsSaveState:
    def test_active_has_archive_and_trash(self, factory, cli):
        ready = factory.make_ready(title="可归档动作")
        actions = set(factory.get_task(ready.task.task_id)["allowed_actions"])
        assert "archive" in actions
        assert "trash" in actions
        assert "restore" not in actions
        assert "resume-auto" not in actions

    def test_archived_has_restore_only(self, factory, cli):
        ready = factory.make_ready(title="归档动作")
        tid = ready.task.task_id
        cli("archive-task", {"task_id": tid})
        actions = set(factory.get_task(tid)["allowed_actions"])
        assert "restore" in actions
        assert "archive" not in actions
        assert "trash" not in actions

    def test_trashed_has_restore(self, factory, cli):
        ready = factory.make_ready(title="回收动作")
        tid = ready.task.task_id
        cli("trash-task", {"task_id": tid})
        actions = set(factory.get_task(tid)["allowed_actions"])
        assert "restore" in actions
        assert "archive" not in actions

    def test_resume_auto_action(self, factory, cli):
        ready = factory.make_ready(title="恢复自动动作")
        tid = ready.task.task_id
        cli("trash-task", {"task_id": tid})
        cli("restore-task", {"task_id": tid})
        actions = set(factory.get_task(tid)["allowed_actions"])
        assert "resume-auto" in actions
        assert "restore" not in actions

    def test_running_has_no_archive_action(self, factory, cli):
        """进行中不可归档 → allowed_actions 不含 archive（UI 置灰依据）。"""
        running = factory.make_running(title="执行中动作")
        actions = set(factory.get_task(running.task.task_id)["allowed_actions"])
        assert "archive" not in actions
        assert "trash" not in actions


# ---------------------------------------------------------------------------
# 重启与再次同步
# ---------------------------------------------------------------------------


class TestRestartAndResync:
    def test_state_survives_restart_and_no_reimport(self, factory, cli,
                                                    tmp_env):
        """归档 → 「重启」（全新核心进程再读库）→ 再次 sync-tasks 不重导入。"""
        ready = factory.make_ready(title="重启再同步")
        task = ready.task
        tid = task.task_id
        cli("archive-task", {"task_id": tid})
        saved_at = factory.get_task(tid)["saved_at"]

        # 重启模拟：全新进程读同一 DB（CLI 每次调用本就是独立进程）
        fresh = cli("overview", {})
        assert fresh["paths"]["db"] == str(tmp_env.db)
        rows = cli("list-tasks", {"save_state": "all"})["tasks"]
        row = next(t for t in rows if t["task_id"] == tid)
        assert row["save_state"] == "archived"
        assert row["saved_at"] == saved_at

        # 再次同步（相同 payload + 变化的需求指纹）→ 不重导入
        payload = _sync_payload(task, kaneo_status="in-progress")
        payload["projects"][0]["tasks"][0]["requirements_sha256"] = "a" * 64
        cli("sync-tasks", payload)
        after = factory.get_task(tid)
        assert after["save_state"] == "archived"
        assert after["saved_at"] == saved_at
        assert after["requirements_sha256"] == task.requirements_sha256
        assert "requirements_changed" not in _kinds(cli, tid)
        # 看板与计数均不含它
        assert _row(cli, tid) is None
        counts = cli("overview", {})["counts"]["board"]
        assert sum(counts.values()) == len(cli("list-tasks", {})["tasks"])

        # 恢复后可被同步正常接续（证明跳过只针对非 active）
        cli("restore-task", {"task_id": tid})
        cli("sync-tasks", _sync_payload(task, kaneo_status="to-do"))
        t = factory.get_task(tid)
        assert t["save_state"] == "active"
        assert t["kaneo_status"] == "to-do"

    def test_trash_restore_roundtrip_keeps_details(self, factory, cli):
        """回收站往返保留全部详情/计划/报告/执行记录（§5.15.1）。"""
        running = factory.make_running(title="往返保详情")
        tid = running.task.task_id
        before = factory.get_task(tid)
        plan_path = before["plan_path"]
        # running 不可移出 → 先 mark_done 再归档/回收/恢复
        cli("resolve", {"task_id": tid, "action": "mark_done"})
        cli("trash-task", {"task_id": tid})
        cli("restore-task", {"task_id": tid})
        after = factory.get_task(tid)
        assert after["plan_path"] == plan_path
        assert after["run_id"] == before["run_id"]
        assert after["session_marker"] == before["session_marker"]
        assert after["attempts"], "执行记录应保留"
        assert after["save_state"] == "active"


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
