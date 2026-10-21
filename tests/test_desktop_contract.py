"""桌面端只读契约：字段形状、display_status 派生、enqueue kind 推断、allowed_actions。

覆盖契约条款：protocol §2（显示态派生）、§4.1（幂等键）、§5.2
（overview/list-tasks/get-task/list-requests/list-events 字段形状）、
§5.3（enqueue 推断）、§4.10（allowed_actions 与 action 前置）；state-model §4
（事件 kind）；ui-spec §5（display_status 中文文案，与图标映射一一对应）。
"""

import pytest

try:
    from conftest import EXECUTORS, ISO_RE, expire_lease
except ImportError:  # unittest discover 包上下文
    from tests.conftest import EXECUTORS, ISO_RE, expire_lease


TASK_SUMMARY_KEYS = {
    "task_id", "project_id", "project_name", "title", "stage", "status",
    "display_status", "last_progress", "blocked_reason", "next_claim_at",
    "active_request", "kaneo_status", "kaneo_checked_at", "priority",
    "plan_path", "updated_at",
    # v1.3 新增（protocol §5.15.1 / §5.15.4）
    "board_column", "board_substatus", "lifecycle_column", "exception",
    "save_state", "saved_at", "auto_advance_paused",
}

TASK_DETAIL_EXTRA_KEYS = {
    "repository", "plan_sha256", "plan_kind", "requirements_sha256",
    "source_sha256", "source_path", "review_report_path",
    "review_report_sha256", "review_key", "review_source_task_id",
    "fix_task_id", "session_id", "session_title", "session_marker",
    "send_state", "sent", "run_id", "failure_code", "failure_stage",
    "manual_recovery_required", "allowed_actions", "attempts", "links",
}

REQUEST_KEYS = {
    "request_id", "idempotency_key", "task_id", "stage", "kind", "status",
    "display_status", "priority", "not_before", "requested_by", "run_id",
    "claimed_by", "claimed_at", "heartbeat_at", "lease_expires_at",
    "result", "attempt_no", "created_at", "updated_at",
}

EVENT_KEYS = {"id", "at", "task_id", "request_id", "kind", "actor", "detail"}

ACTIVE_REQUEST_KEYS = {"request_id", "kind", "status", "claimed_by",
                       "heartbeat_at", "lease_expires_at"}


# ---------------------------------------------------------------------------
# §5.2 字段形状
# ---------------------------------------------------------------------------


class TestFieldShapes:
    def test_overview_shape(self, cli):
        out = cli("overview", {})
        assert set(out["paused"]) >= {"global", "stages"}
        assert isinstance(out["paused"]["global"], bool)
        assert isinstance(out["paused"]["stages"], list)
        assert set(out["counts"]) >= {"tasks", "requests"}
        assert isinstance(out["counts"]["tasks"], dict)
        assert isinstance(out["counts"]["requests"], dict)
        assert "last_tick_at" in out
        # state-model §1.1：schema v6（worktree slots）
        assert out["schema_version"] == 6

    def test_list_tasks_summary_fields(self, factory, cli):
        task = factory.sync_task(title="形状检查")
        out = cli("list-tasks", {})
        t = next(x for x in out["tasks"] if x["task_id"] == task.task_id)
        assert TASK_SUMMARY_KEYS <= set(t), (
            f"TaskSummary 缺字段：{TASK_SUMMARY_KEYS - set(t)}")
        assert t["active_request"] is None or \
            ACTIVE_REQUEST_KEYS <= set(t["active_request"])

    def test_get_task_detail_fields(self, factory, cli):
        ready = factory.make_ready()
        t = cli("get-task", {"task_id": ready.task.task_id})["task"]
        missing = (TASK_SUMMARY_KEYS | TASK_DETAIL_EXTRA_KEYS) - set(t)
        assert not missing, f"TaskDetail 缺字段：{missing}"
        assert isinstance(t["allowed_actions"], list)
        assert isinstance(t["attempts"], list)
        assert set(t["links"]) >= {"kaneo_task_url", "plan_file", "report_file"}
        if t["attempts"]:
            a = t["attempts"][0]
            assert {"request_id", "run_id", "stage", "kind", "started_at",
                    "finished_at", "outcome", "send_state", "note",
                    "evidence"} <= set(a)

    def test_list_requests_fields(self, factory, cli):
        task = factory.sync_task()
        factory.queue_plan(task.task_id)
        reqs = cli("list-requests", {})["requests"]
        r = next(x for x in reqs if x["task_id"] == task.task_id)
        missing = REQUEST_KEYS - set(r)
        assert not missing, f"Request 缺字段：{missing}"
        assert ISO_RE.match(r["created_at"])
        # active 过滤
        active = cli("list-requests", {"status": "active"})["requests"]
        assert all(x["status"] in ("queued", "claimed") for x in active)

    def test_list_events_fields_and_order(self, factory, cli):
        task = factory.sync_task()
        evs = cli("list-events", {"task_id": task.task_id})["events"]
        assert evs, "sync 后应有事件"
        for e in evs:
            assert EVENT_KEYS <= set(e), f"event 缺字段：{e}"
            assert ISO_RE.match(e["at"])
        ids = [e["id"] for e in evs]
        assert ids == sorted(ids, reverse=True), "事件应倒序"


# ---------------------------------------------------------------------------
# §2 display_status 派生（与 ui-spec §5 文案一致）
# ---------------------------------------------------------------------------


class TestDisplayStatus:
    def test_matrix(self, factory, cli, tmp_env):
        """§2 显示态派生全表（与 ui-spec §5 中文文案一致），逐态即时断言。"""
        fget = lambda tid: factory.get_task(tid)["display_status"]

        t = factory.sync_task(title="待规划任务")
        assert fget(t.task_id) == "待规划"
        req_id, _i, _s = factory.queue_plan(t.task_id)
        pc = factory.claim("plan", request_id=req_id)
        assert fget(t.task_id) == "规划中"
        # v2 §5.22.1：plan 单飞——收尾释放 stage 供后续任务规划
        cli("report", {"request_id": pc["request_id"],
                       "run_id": pc["run_id"], "outcome": "failed",
                       "note": "矩阵测试收尾"})

        ready = factory.make_ready(project="Music", title="就绪任务")
        # v2 §5.22.4：ready+hold → 待确认执行（确认后入队）
        assert fget(ready.task.task_id) == "待确认执行"
        factory.enqueue_dispatch(ready.task.task_id)
        assert fget(ready.task.task_id) == "已排队"
        dreq = factory.find_request(ready.task.task_id, "dispatch")
        d = factory.claim("dispatch", request_id=dreq["request_id"])
        assert d["claimed"] is True
        assert fget(ready.task.task_id) == "派发中"
        # 释放串行位便于后续任务派发
        cli("dispatch-fail", {"task_id": ready.task.task_id,
                              "run_id": d["run_id"], "send_state": "not_sent",
                              "reason": "矩阵测试"})
        cli("resolve", {"task_id": ready.task.task_id, "action": "dismiss"})

        running = factory.make_running(title="执行任务")
        assert fget(running.task.task_id) == "执行中"

        ir = factory.make_in_review(title="评审任务")
        assert fget(ir.task.task_id) == "评审中"

        nu = factory.make_needs_user(send_state="not_sent", title="阻塞任务")
        assert fget(nu.task.task_id) == "需人工"

        mreq = factory.find_request(running.task.task_id, "monitor")
        mc = factory.claim("monitor", request_id=mreq["request_id"])
        expire_lease(tmp_env.db, mc["request_id"])
        assert fget(running.task.task_id) == "执行端失联"

        cli("resolve", {"task_id": ir.task.task_id, "action": "mark_done"})
        assert fget(ir.task.task_id) == "已完成"
        assert factory.get_task(ir.task.task_id)["save_state"] == "active"
        cli("archive-task", {"task_id": ir.task.task_id})
        assert fget(ir.task.task_id) == "已归档"
        assert factory.get_task(ir.task.task_id)["save_state"] == "archived"

    def test_long_chinese_title(self, factory, cli):
        """40+ 字长中文标题不报错、原样返回（ui-spec §8 基线）。"""
        title = ("这是一个非常长的中文任务标题用来验证列表截断"
                 "与接口字段不会引发任何编码或长度问题超过四十字")
        task = factory.sync_task(title=title)
        t = factory.get_task(task.task_id)
        assert t["title"] == title
        lst = cli("list-tasks", {})["tasks"]
        assert any(x["title"] == title for x in lst)

    def test_needs_attention_filter(self, factory, cli):
        nu = factory.make_needs_user(send_state="not_sent")
        factory.make_ready(project="Music", title="普通就绪")
        out = cli("list-tasks", {"needs_attention": True})
        ids = {t["task_id"] for t in out["tasks"]}
        assert nu.task.task_id in ids
        # ready 不应算 needs_attention（无阻塞无失联）
        out_all = {t["task_id"]: t for t in cli("list-tasks", {})["tasks"]}
        for t in out["tasks"]:
            assert t["display_status"] in ("需人工", "执行端失联") or \
                t["status"] == "needs_user"

    def test_status_filter(self, factory, cli):
        ready = factory.make_ready()
        factory.make_running(project="Music", title="过滤任务")
        out = cli("list-tasks", {"status": "ready"})
        assert all(t["status"] == "ready" for t in out["tasks"])
        assert any(t["task_id"] == ready.task.task_id for t in out["tasks"])


# ---------------------------------------------------------------------------
# §5.3 enqueue kind 推断
# ---------------------------------------------------------------------------

class TestEnqueueInference:
    def test_infer_needs_plan(self, factory, cli):
        """v2：needs_plan 走规划面板——enqueue 推断不再建 plan 请求，
        返回 bad_request（提示经 start-plans 手动提交）。"""
        task = factory.sync_task()
        out = cli("enqueue", {"task_id": task.task_id}, expect=1)
        assert out["code"] == "bad_request"
        # 完整链仍可走通：queue_plan → claim → publish → confirm_plan 派发
        pub = factory.publish(task)
        assert pub.resp["task_status"] == "ready"
        r = factory.enqueue_dispatch(task.task_id)
        assert r["request"]["kind"] == "dispatch"

    def test_infer_ready(self, factory, cli):
        """kind 缺省仍由状态推断为 dispatch（hold 任务走 confirm_plan 门）。"""
        ready = factory.make_ready()
        tid = ready.task.task_id
        r = cli("enqueue", {"task_id": tid, "confirm_plan": True,
                            "expected_plan_sha256":
                            factory.get_task(tid)["plan_sha256"]})
        assert r["request"]["kind"] == "dispatch"

    def test_infer_running(self, factory, cli):
        running = factory.make_running()
        r = cli("enqueue", {"task_id": running.task.task_id})
        assert r["request"]["kind"] == "monitor"

    def test_infer_in_review(self, factory, cli):
        ir = factory.make_in_review()
        r = cli("enqueue", {"task_id": ir.task.task_id})
        assert r["request"]["kind"] == "review"

    def test_infer_done_bad_request(self, factory, cli):
        running = factory.make_running()
        cli("resolve", {"task_id": running.task.task_id, "action": "mark_done"})
        out = cli("enqueue", {"task_id": running.task.task_id}, expect=1)
        assert out["code"] == "bad_request"


# ---------------------------------------------------------------------------
# §4.10 allowed_actions 随状态变化
# ---------------------------------------------------------------------------


class TestAllowedActions:
    def test_ready_actions(self, factory, cli):
        ready = factory.make_ready()
        t = factory.get_task(ready.task.task_id)
        actions = set(t["allowed_actions"])
        # v2 §5.22.4：ready+hold → confirm_dispatch 取代 enqueue
        assert "confirm_dispatch" in actions
        assert "enqueue" not in actions
        assert "recheck" in actions
        # 无阻塞 → 无 restore/mark_sent/release_lost
        assert "release_lost" not in actions
        assert "mark_sent" not in actions

    def test_queued_has_priority_action(self, factory, cli):
        ready = factory.make_ready()
        factory.enqueue_dispatch(ready.task.task_id)
        t = factory.get_task(ready.task.task_id)
        actions = set(t["allowed_actions"])
        assert "set-priority" in actions or "enqueue" in actions

    def test_needs_user_not_sent(self, factory, cli):
        nu = factory.make_needs_user(send_state="not_sent")
        t = factory.get_task(nu.task.task_id)
        actions = set(t["allowed_actions"])
        assert "retry" in actions
        assert "replan" in actions
        assert "dismiss" in actions
        assert "restore" not in actions    # not_sent 不给 restore

    def test_needs_user_unknown(self, factory, cli):
        nu = factory.make_needs_user(send_state="unknown")
        t = factory.get_task(nu.task.task_id)
        actions = set(t["allowed_actions"])
        # ui-spec §4.1：unknown 只给三专用键 + replan/mark_done/dismiss/recheck
        assert {"restore", "accept_duplicate", "mark_sent"} <= actions
        assert "retry" not in actions

    def test_lost_has_release_lost(self, factory, cli, tmp_env):
        running = factory.make_running()
        mreq = factory.find_request(running.task.task_id, "monitor")
        c = factory.claim("monitor", request_id=mreq["request_id"])
        expire_lease(tmp_env.db, c["request_id"])
        t = factory.get_task(running.task.task_id)
        assert "release_lost" in set(t["allowed_actions"])

    def test_done_minimal_actions(self, factory, cli):
        running = factory.make_running()
        cli("resolve", {"task_id": running.task.task_id, "action": "mark_done"})
        t = factory.get_task(running.task.task_id)
        actions = set(t["allowed_actions"])
        assert "enqueue" not in actions
        assert "retry" not in actions


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
