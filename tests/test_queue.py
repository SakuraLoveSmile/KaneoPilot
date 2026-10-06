"""队列语义：幂等、优先级、暂停、领取竞争、守卫、心跳/租约、report、失联派生。

覆盖契约条款：protocol §1（调用方式/错误信封/DB 解析）、§4.1（幂等去重）、
§4.2（claim 事务）、§4.3（租约与失联）、§4.4（领取守卫）、§4.7（report）、
§4.8（tick）、§4.9（暂停）、§4.10（resolve release_lost）、§5.1（doctor）、
§5.3（enqueue/set-priority/set-paused）、§5.5（claim/heartbeat/report）；
state-model §3（request 转换）。
"""

import concurrent.futures
import json
import time

import pytest

try:
    from conftest import (
        CORE_BIN, EXECUTORS, LEASE_SECONDS_DEFAULT, assert_iso8601, db_exec,
        expire_lease, iso_now, make_due, parse_iso, rewind_finished_at,
    )
except ImportError:  # unittest discover 包上下文
    from tests.conftest import (
        CORE_BIN, EXECUTORS, LEASE_SECONDS_DEFAULT, assert_iso8601, db_exec,
        expire_lease, iso_now, make_due, parse_iso, rewind_finished_at,
    )


# ---------------------------------------------------------------------------
# §1 调用方式与错误信封
# ---------------------------------------------------------------------------


class TestInvocation:
    def test_json_flag_success_envelope(self, cli, fake_kaneo):
        # fake_kaneo 负责写 credentials.json，doctor 凭据检查才有对象
        out = cli("doctor", {"online": False})
        assert out["ok"] is True
        names = {c["name"] for c in out["checks"]}
        assert {"python", "db_writable", "credentials", "plan_root",
                "repos_root", "state_root", "kaneo_ping"} <= names
        ping = next(c for c in out["checks"] if c["name"] == "kaneo_ping")
        assert ping["skipped"] is True  # online 缺省 false → 跳过网络

    def test_stdin_payload(self, cli):
        # 无 --json 时从 stdin 读 JSON（可为空对象 {}）
        out = cli("overview", None)
        assert "counts" in out
        out2 = cli("set-paused", {"stage": None, "paused": True}, use_stdin=True)
        assert "paused" in out2
        cli("set-paused", {"stage": None, "paused": False})

    def test_unknown_command_bad_request(self, cli):
        out = cli("no-such-command", {}, expect=1)
        assert out["code"] == "bad_request"

    def test_missing_required_field_bad_request(self, cli):
        out = cli("get-task", {}, expect=1)
        assert out["code"] == "bad_request"

    def test_unknown_field_bad_request(self, cli):
        out = cli("overview", {"bogus_field": 1}, expect=1)
        assert out["code"] == "bad_request"

    def test_malformed_json_bad_request(self, cli):
        proc = cli.raw(["overview", "--json", "{not json"])
        assert proc.returncode == 1
        data = json.loads(proc.stdout)
        assert data["code"] == "bad_request"

    def test_error_envelope_shape(self, cli):
        out = cli("claim", {"stage": "dispatch"}, expect=1)
        # 缺 executor → bad_request；信封必须有 error + code
        assert set(out) >= {"error", "code"}
        assert out["code"] == "bad_request"

    def test_db_flag_overrides_env(self, cli, tmp_env):
        # --db 优先于 KANPILOT_DB（§1）
        db_b = tmp_env.app_support / "other.db"
        cli("set-paused", {"stage": None, "paused": True}, db=db_b)
        out_b = cli("overview", {}, db=db_b)
        assert out_b["paused"]["global"] is True
        out_env = cli("overview", {})  # KANPILOT_DB 指向默认 db
        assert out_env["paused"]["global"] is False


# ---------------------------------------------------------------------------
# §4.1 幂等与去重
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_enqueue_dedup_repeat_click(self, factory, cli):
        """重复点击不重复派发：同 key 已有活动请求 → 返回既有 + deduplicated。"""
        ready = factory.make_ready()
        tid = ready.task.task_id
        r1 = factory.enqueue_dispatch(tid)
        assert r1["deduplicated"] is False
        r2 = factory.enqueue_dispatch(tid)
        assert r2["deduplicated"] is True
        assert r2["request"]["request_id"] == r1["request"]["request_id"]
        # 仍只有一个活动请求
        reqs = cli("list-requests", {"status": "active"})["requests"]
        assert len([r for r in reqs if r["task_id"] == tid]) == 1

    def test_enqueue_idempotency_key_format(self, factory, cli):
        """默认 key = '<kind>:<task_id>'。"""
        ready = factory.make_ready()
        tid = ready.task.task_id
        r = factory.enqueue_dispatch(tid)
        req = r["request"]
        assert req["kind"] == "dispatch"
        assert req["idempotency_key"] == f"dispatch:{tid}"

    def test_enqueue_explicit_kind_key(self, factory, cli):
        running = factory.make_running()
        tid = running.task.task_id
        r = cli("enqueue", {"task_id": tid})
        assert r["request"]["kind"] == "monitor"
        assert r["request"]["idempotency_key"] == f"monitor:{tid}"

    def test_plan_request_key(self, factory, cli):
        """start-plans 建 plan 请求，key=plan:<task_id>；同材料幂等，
        不同材料冲突。"""
        task = factory.sync_task()
        req_id, input_id, input_sha = factory.queue_plan(task.task_id)
        req = factory.find_request(task.task_id, "plan")
        assert req is not None
        assert req["request_id"] == req_id
        assert req["idempotency_key"] == f"plan:{task.task_id}"
        assert req["stage"] == "plan"
        # 同 input_sha256 再提交 → deduplicated
        again = cli("start-plans", {
            "items": [{"input_id": input_id, "input_sha256": input_sha}]})
        assert again["results"][0].get("deduplicated") is True
        assert again["results"][0]["request_id"] == req_id
        # 新材料（supplement 改变 input 指纹）→ conflict
        prep2 = cli("prepare-plan-input", {
            "task_id": task.task_id, "supplement": "补充材料 v2"})
        other = cli("start-plans", {
            "items": [{"input_id": prep2["input_id"],
                       "input_sha256": prep2["input_sha256"]}]})
        assert other["results"][0]["code"] == "conflict"


# ---------------------------------------------------------------------------
# §5.3 set-priority + §4.2-4 排序
# ---------------------------------------------------------------------------


class TestPriority:
    def test_priority_ordering(self, factory, cli):
        """priority ASC → created_at ASC：低数值先领。"""
        a = factory.sync_task(title="任务A")
        b = factory.sync_task(title="任务B", project="Music")
        factory.queue_plan(a.task_id)
        factory.queue_plan(b.task_id)
        req_a = factory.find_request(a.task_id, "plan")
        req_b = factory.find_request(b.task_id, "plan")
        assert req_a and req_b
        cli("set-priority", {"request_id": req_b["request_id"], "priority": 1})
        # 第一次 claim 应拿到 priority=1 的 B（尽管 A 先创建）
        c1 = cli("claim", {"stage": "plan", "executor": EXECUTORS["plan"]})
        assert c1["claimed"] is True
        assert c1["request_id"] == req_b["request_id"]
        # v2 §5.22.1：plan 单飞——先收尾 B 的领取才能领 A
        cli("report", {"request_id": c1["request_id"],
                       "run_id": c1["run_id"], "outcome": "failed",
                       "note": "测试收尾，释放 plan stage"})
        # 第二次拿到 A（priority=100，created_at 早）
        c2 = cli("claim", {"stage": "plan", "executor": EXECUTORS["plan"]})
        assert c2["claimed"] is True
        assert c2["request_id"] == req_a["request_id"]

    def test_set_priority_by_task_id(self, factory, cli):
        task = factory.sync_task()
        factory.queue_plan(task.task_id)
        req = factory.find_request(task.task_id, "plan")
        out = cli("set-priority", {"task_id": task.task_id, "priority": 5})
        reqs = cli("list-requests", {"status": "queued"})["requests"]
        found = next(r for r in reqs if r["request_id"] == req["request_id"])
        assert found["priority"] == 5

    def test_set_priority_not_found(self, cli):
        out = cli("set-priority",
                  {"task_id": "no-such-task", "priority": 1}, expect=1)
        assert out["code"] == "not_found"


# ---------------------------------------------------------------------------
# §4.9 暂停
# ---------------------------------------------------------------------------


class TestPause:
    def test_global_pause_blocks_claim(self, factory, cli):
        task = factory.sync_task()
        factory.queue_plan(task.task_id)
        cli("set-paused", {"stage": None, "paused": True})
        c = cli("claim", {"stage": "plan", "executor": EXECUTORS["plan"]})
        assert c["claimed"] is False
        assert c["reason"] == "paused"
        ov = cli("overview", {})
        assert ov["paused"]["global"] is True
        cli("set-paused", {"stage": None, "paused": False})
        c2 = cli("claim", {"stage": "plan", "executor": EXECUTORS["plan"]})
        assert c2["claimed"] is True

    def test_stage_pause_only_blocks_that_stage(self, factory, cli):
        ready = factory.make_ready()
        running = factory.make_running(project="Music", title="监控目标")
        cli("set-paused", {"stage": "dispatch", "paused": True})
        factory.enqueue_dispatch(ready.task.task_id)
        c = cli("claim", {"stage": "dispatch", "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is False and c["reason"] == "paused"
        # monitor 不受影响
        mreq = factory.find_request(running.task.task_id, "monitor")
        c2 = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"],
                           "request_id": mreq["request_id"]})
        assert c2["claimed"] is True
        ov = cli("overview", {})
        assert "dispatch" in ov["paused"]["stages"]
        assert ov["paused"]["global"] is False

    def test_pause_does_not_cancel_claimed(self, factory, cli):
        """暂停只阻止新领取，不影响已 claimed（§4.9 / automation §3）。"""
        running = factory.make_running()
        mreq = factory.find_request(running.task.task_id, "monitor")
        c = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"],
                          "request_id": mreq["request_id"]})
        assert c["claimed"] is True
        cli("set-paused", {"stage": None, "paused": True})
        # 已 claimed 的仍可心跳/回报
        hb = cli("heartbeat", {"request_id": c["request_id"],
                               "run_id": c["run_id"], "progress": "仍在跑"})
        assert hb["ok"] is True
        rep = cli("report", {"request_id": c["request_id"],
                             "run_id": c["run_id"], "outcome": "succeeded"})
        assert rep["ok"] is True


# ---------------------------------------------------------------------------
# §4.2 claim 竞争与守卫（§4.4）
# ---------------------------------------------------------------------------


class TestClaimBasics:
    def test_claim_empty_queue(self, cli):
        """空队列 → claimed:false（reason 未冻结，只断言不领取）。"""
        out = cli("claim", {"stage": "monitor",
                            "executor": EXECUTORS["monitor"]})
        assert out["claimed"] is False
        assert "reason" in out

    def test_claim_returns_run_context(self, factory, cli):
        """非 dispatch claim 返回 run_id 与阶段上下文（§5.5）。"""
        task = factory.sync_task()
        factory.queue_plan(task.task_id)
        req = factory.find_request(task.task_id, "plan")
        c = cli("claim", {"stage": "plan", "executor": EXECUTORS["plan"],
                          "request_id": req["request_id"]})
        assert c["claimed"] is True
        assert c["run_id"]
        ctx = c["context"]
        assert ctx["task_id"] == task.task_id or \
            ctx.get("task", {}).get("task_id") == task.task_id
        assert ctx["lease_seconds"] > 0


class TestClaimRace:
    def test_concurrent_claim_single_request(self, factory, cli, tmp_env):
        """两个进程同时 claim 同一请求：只有一个成功（事务保证）。"""
        running = factory.make_running()
        mreq = factory.find_request(running.task.task_id, "monitor")

        def do_claim(_):
            return cli("claim", {"stage": "monitor",
                                 "executor": EXECUTORS["monitor"],
                                 "request_id": mreq["request_id"]})

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(do_claim, range(2)))
        wins = [r for r in results if r.get("claimed") is True]
        assert len(wins) == 1, f"应恰好一个 claim 成功：{results}"
        for r in results:
            if r.get("claimed") is not True:
                assert r.get("claimed") is False
        # run_id 唯一
        assert wins[0]["run_id"]

    def test_concurrent_claim_queue_drain(self, factory, cli, tmp_env):
        """两个 queued monitor 请求 + 并发 claim：两条都被领走或一胜一空，不重复。"""
        r1 = factory.make_running(title="监控A")
        r2 = factory.make_running(project="Music", title="监控B")
        # dispatch-confirm 建的 monitor 请求带 not_before 冷却，先置到期
        for r in (r1, r2):
            mreq = factory.find_request(r.task.task_id, "monitor")
            make_due(tmp_env.db, mreq["request_id"])

        def do_claim(_):
            return cli("claim", {"stage": "monitor",
                                 "executor": EXECUTORS["monitor"]})

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(do_claim, range(2)))
        claimed_ids = {r["request_id"] for r in results if r.get("claimed")}
        assert len(claimed_ids) == len([r for r in results if r.get("claimed")])
        assert len(claimed_ids) >= 1  # 至少一个成功
        assert len(claimed_ids) <= 2  # 不重复领取同一请求


class TestClaimGuard:
    def test_guard_wrong_task_status(self, factory, cli):
        """review 守卫要求 in_review：任务已 done → guard_failed（§4.4）。"""
        ir = factory.make_in_review()
        tid = ir.task.task_id
        # enqueue review（in_review 合法推断）
        r = cli("enqueue", {"task_id": tid})
        assert r["request"]["kind"] == "review"
        # 先把任务推到 done（远端 done + advance）
        factory.kaneo.set_status(tid, "in-review")
        cli("advance-task", {"task_id": tid, "target": "done",
                             "expected_current": "in-review"})
        assert factory.get_task(tid)["status"] == "done"
        # 此时 claim review → 守卫失败
        c = cli("claim", {"stage": "review", "executor": EXECUTORS["review"],
                          "request_id": r["request"]["request_id"]})
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"
        assert c["request_id"] == r["request"]["request_id"]
        # 请求被标记 failed
        req = cli("list-requests", {"status": "failed"})["requests"]
        assert any(x["request_id"] == r["request"]["request_id"] for x in req)

    def test_claim_by_request_id_validates_stage(self, factory, cli):
        """指定 request_id 仍校验 stage/queued（§4.2-4）。"""
        task = factory.sync_task()
        factory.queue_plan(task.task_id)
        req = factory.find_request(task.task_id, "plan")
        c = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"],
                          "request_id": req["request_id"]}, expect=1)
        assert c["code"] in ("bad_request", "conflict", "guard_failed")


# ---------------------------------------------------------------------------
# §4.3 租约 / §5.5 heartbeat / §4.7 report
# ---------------------------------------------------------------------------


class TestHeartbeatReport:
    def _claimed_monitor(self, factory, cli):
        running = factory.make_running()
        mreq = factory.find_request(running.task.task_id, "monitor")
        c = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"],
                          "request_id": mreq["request_id"]})
        assert c["claimed"] is True
        return running, mreq, c

    def test_heartbeat_extends_lease(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        req_before = next(r for r in cli("list-requests", {})["requests"]
                          if r["request_id"] == c["request_id"])
        time.sleep(0.05)
        hb = cli("heartbeat", {"request_id": c["request_id"],
                               "run_id": c["run_id"]})
        assert hb["ok"] is True
        assert_iso8601(hb["lease_expires_at"])
        assert parse_iso(hb["lease_expires_at"]) >= parse_iso(
            req_before["lease_expires_at"])

    def test_heartbeat_writes_progress(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        cli("heartbeat", {"request_id": c["request_id"], "run_id": c["run_id"],
                          "progress": "编译中 50%"})
        t = factory.get_task(running.task.task_id)
        assert t["last_progress"] == "编译中 50%"

    def test_heartbeat_stale_run(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        out = cli("heartbeat", {"request_id": c["request_id"],
                                "run_id": "wrong-run-id"}, expect=1)
        assert out["code"] == "stale_run"

    def test_report_stale_run(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        out = cli("report", {"request_id": c["request_id"],
                             "run_id": "wrong-run-id",
                             "outcome": "succeeded"}, expect=1)
        assert out["code"] == "stale_run"

    def test_report_succeeded_updates_task(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        rep = cli("report", {"request_id": c["request_id"],
                             "run_id": c["run_id"], "outcome": "succeeded",
                             "progress": "Devin 仍在执行"})
        assert rep["ok"] is True
        assert rep["request_status"] == "succeeded"
        t = factory.get_task(running.task.task_id)
        assert t["status"] == "running"
        assert t["last_progress"] == "Devin 仍在执行"

    def test_report_failed_keeps_task_status(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        rep = cli("report", {"request_id": c["request_id"],
                             "run_id": c["run_id"], "outcome": "failed",
                             "note": "锁屏无法观察"})
        assert rep["request_status"] == "failed"
        t = factory.get_task(running.task.task_id)
        assert t["status"] == "running"  # task 保持原状态

    def test_report_needs_user_blocks_task(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        rep = cli("report", {"request_id": c["request_id"],
                             "run_id": c["run_id"], "outcome": "needs_user",
                             "note": "会话消失，需人工"})
        assert rep["request_status"] == "failed"
        t = factory.get_task(running.task.task_id)
        assert t["status"] == "needs_user"
        assert t["failure_reason"] or t.get("blocked_reason") or t.get("failure_code")

    def test_report_writes_event(self, factory, cli):
        running, mreq, c = self._claimed_monitor(factory, cli)
        cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                       "outcome": "succeeded"})
        evs = cli("list-events", {"task_id": running.task.task_id})["events"]
        kinds = {e["kind"] for e in evs}
        assert "reported" in kinds


# ---------------------------------------------------------------------------
# §4.3 失联派生 + §4.10 release_lost
# ---------------------------------------------------------------------------


class TestLostDerivation:
    def test_lost_display_no_auto_release(self, factory, cli, tmp_env):
        """租约过期 → 显示失联；不自动释放、不自动重发。"""
        running = factory.make_running()
        mreq = factory.find_request(running.task.task_id, "monitor")
        c = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"],
                          "request_id": mreq["request_id"]})
        expire_lease(tmp_env.db, c["request_id"])
        # list-tasks 派生 display_status
        t = factory.get_task(running.task.task_id)
        assert t["status"] == "running"            # 库内状态不变
        assert t["display_status"] == "执行端失联"
        assert t["active_request"]["status"] == "claimed"  # 仍 claimed，未释放
        # list-requests 失联派生
        reqs = cli("list-requests", {})["requests"]
        lost_req = next(r for r in reqs
                        if r["request_id"] == c["request_id"])
        assert lost_req["status"] == "claimed"
        assert lost_req["display_status"] == "执行端失联"
        lost_view = cli("list-requests", {"status": "lost"})["requests"]
        assert any(r["request_id"] == c["request_id"] for r in lost_view)
        # overview 计数含 lost
        ov = cli("overview", {})
        assert ov["counts"]["requests"].get("lost", 0) >= 1
        # 不自动重发：tick + claim 不为此 task 建新请求
        # （本测试仅此一个 running 任务，无其他可领 monitor 请求）
        cli("tick", {})
        c2 = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"]})
        assert c2["claimed"] is False
        # resolve release_lost 才解除
        res = cli("resolve", {"task_id": running.task.task_id,
                              "action": "release_lost", "note": "测试释放"})
        reqs = cli("list-requests", {})["requests"]
        old = next(r for r in reqs if r["request_id"] == c["request_id"])
        assert old["status"] == "cancelled"
        # monitor 中途 → 回原 status（running）并建新请求
        t2 = factory.get_task(running.task.task_id)
        assert t2["status"] == "running"
        new_req = factory.find_request(running.task.task_id, "monitor")
        assert new_req is not None
        assert new_req["request_id"] != c["request_id"]
        evs = cli("list-events", {"task_id": running.task.task_id})["events"]
        kinds = {e["kind"] for e in evs}
        assert "resolved" in kinds
        assert "request_cancelled" in kinds

    def test_release_lost_requires_lost(self, factory, cli):
        """无失联请求时 release_lost → bad_request（§4.10 前置条件）。"""
        running = factory.make_running()
        out = cli("resolve", {"task_id": running.task.task_id,
                              "action": "release_lost"}, expect=1)
        assert out["code"] == "bad_request"


# ---------------------------------------------------------------------------
# §4.8 tick 业务周期
# ---------------------------------------------------------------------------


class TestTick:
    def test_tick_creates_monitor(self, factory, cli):
        """v2：tick 只为 running 建 monitor（不再建 plan-scan）。"""
        running = factory.make_running()
        out = cli("tick", {})
        kinds = {c["kind"] for c in out["created"]}
        assert "monitor" in kinds or \
            factory.find_request(running.task.task_id, "monitor") is not None
        # 无 plan-scan 阶段请求
        reqs = cli("list-requests", {})["requests"]
        assert not [r for r in reqs if r["kind"] == "plan-scan"]
        # 幂等：立即再 tick 不重复建 monitor
        out2 = cli("tick", {})
        assert not [c for c in out2["created"]
                    if c.get("task_id") == running.task.task_id
                    and c["kind"] == "monitor"]

    def test_tick_review_request_for_in_review(self, factory, cli):
        ir = factory.make_in_review()
        cli("tick", {})
        req = factory.find_request(ir.task.task_id, "review")
        assert req is not None
        assert req["idempotency_key"] == f"review:{ir.task.task_id}"

    def test_monitor_interval_not_before(self, factory, cli, tmp_env):
        """monitor 完成 + interval 内 → tick 不重建；过期后重建（§4.8）。"""
        running = factory.make_running()
        mreq = factory.find_request(running.task.task_id, "monitor")
        c = cli("claim", {"stage": "monitor", "executor": EXECUTORS["monitor"],
                          "request_id": mreq["request_id"]})
        cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                       "outcome": "succeeded"})
        # 刚完成 → interval(900s) 内 tick 不重建
        out = cli("tick", {})
        assert not [x for x in out["created"]
                    if x.get("task_id") == running.task.task_id
                    and x["kind"] == "monitor"]
        # 把完成时间推到 1000s 前 → tick 应重建
        rewind_finished_at(tmp_env.db, c["request_id"], seconds=1000)
        out2 = cli("tick", {})
        created = [x for x in out2["created"]
                   if x.get("task_id") == running.task.task_id
                   and x["kind"] == "monitor"]
        assert created, f"interval 过后 tick 未重建 monitor：{out2}"


# ---------------------------------------------------------------------------
# §4.10 resolve 通用行为（非 dispatch 部分）
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_replan_from_needs_user(self, factory, cli):
        nu = factory.make_needs_user(send_state="not_sent")
        res = cli("resolve", {"task_id": nu.task.task_id, "action": "replan"})
        t = factory.get_task(nu.task.task_id)
        assert t["status"] == "needs_plan"
        # v2：replan 回 needs_plan，不自动建 plan 请求（待规划面板提交）
        assert factory.find_request(nu.task.task_id, "plan") is None
        evs = cli("list-events", {"task_id": nu.task.task_id})["events"]
        assert any(e["kind"] == "resolved" for e in evs)

    def test_resolve_mark_done(self, factory, cli):
        running = factory.make_running()
        cli("resolve", {"task_id": running.task.task_id, "action": "mark_done"})
        assert factory.get_task(running.task.task_id)["status"] == "done"

    def test_resolve_dismiss_clears_block(self, factory, cli):
        nu = factory.make_needs_user(send_state="not_sent")
        cli("resolve", {"task_id": nu.task.task_id, "action": "dismiss"})
        t = factory.get_task(nu.task.task_id)
        assert t["status"] == "ready"
        # dismiss 不自动建请求
        assert factory.find_request(nu.task.task_id, "dispatch") is None

    def test_resolve_invalid_action_bad_request(self, factory, cli):
        ready = factory.make_ready()
        out = cli("resolve", {"task_id": ready.task.task_id,
                              "action": "restore"}, expect=1)
        assert out["code"] == "bad_request"


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
