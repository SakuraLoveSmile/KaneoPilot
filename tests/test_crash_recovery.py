"""崩溃恢复：执行端进程被杀（kill -9 模拟）→ 租约过期失联 → 不自动释放/重发 →
resolve release_lost 解除；dispatch 串行位在 crash 后仍阻止新派发。

覆盖契约条款：protocol §4.2-3/§4.2-6（claimed 状态持久、run_id）、§4.3
（租约与失联：不因 PID/进程退出/时间流逝自动释放）、§4.10（release_lost
前置与效果）、§5.10（dispatch 子步骤）；state-model §3（request 转换）、
§5（串行位为持久操作租约，非进程锁）；automation-contract §3（关闭/中断
不意味着操作结束）。
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

try:
    from conftest import CORE_BIN, EXECUTORS, expire_lease, make_due, ui_evidence
except ImportError:  # unittest discover 包上下文
    from tests.conftest import CORE_BIN, EXECUTORS, expire_lease, make_due, ui_evidence


# 驱动进程：claim 一次 → 把结果写文件 → 睡眠（模拟执行端工作中被杀）
_DRIVER = (
    "import json,os,subprocess,sys,time;"
    "p=subprocess.run([sys.executable,os.environ['KP_CORE'],'claim','--json',"
    "sys.argv[1],'--db',os.environ['KP_DB']],capture_output=True,text=True);"
    "open(sys.argv[2],'w').write(p.stdout);"
    "sys.stdout.flush();"
    "time.sleep(300)"
)


def _claim_then_sleep(tmp_env, payload, out_path):
    env = dict(tmp_env.env)
    env["KP_CORE"] = CORE_BIN
    env["KP_DB"] = str(tmp_env.db)
    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, json.dumps(payload), str(out_path)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等 claim 结果落盘
    deadline = time.time() + 30
    while time.time() < deadline:
        if out_path.exists() and out_path.read_text().strip():
            break
        time.sleep(0.05)
    assert out_path.exists(), "驱动进程未产出 claim 结果"
    return proc, json.loads(out_path.read_text())


class TestProcessDeathKeepsClaim:
    def test_claimed_survives_sigkill(self, factory, cli, tmp_env, tmp_path):
        """kill -9 执行端：请求保持 claimed，不自动释放（state-model §5）。"""
        running = factory.make_running()
        tid = running.task.task_id
        mreq = factory.find_request(tid, "monitor")
        make_due(tmp_env.db, mreq["request_id"])   # monitor 冷却期后门
        out_path = tmp_path / "claim.json"
        proc, res = _claim_then_sleep(
            tmp_env,
            {"stage": "monitor", "executor": EXECUTORS["monitor"],
             "request_id": mreq["request_id"]},
            out_path)
        assert res.get("claimed") is True, f"claim 未成功：{res}"
        proc.kill()          # SIGKILL
        proc.wait(timeout=10)
        # 进程死亡不改变库内状态
        req = next(r for r in cli("list-requests", {})["requests"]
                   if r["request_id"] == res["request_id"])
        assert req["status"] == "claimed"
        # 租约过期 → 派生 lost，仍不释放
        expire_lease(tmp_env.db, res["request_id"])
        t = factory.get_task(tid)
        assert t["display_status"] == "执行端失联"
        assert t["active_request"]["status"] == "claimed"
        # 无新 monitor 请求自动产生
        reqs = [r for r in cli("list-requests", {})["requests"]
                if r["task_id"] == tid and r["kind"] == "monitor"]
        assert len(reqs) == 1
        # release_lost → cancelled + 回原状态 + 新请求
        cli("resolve", {"task_id": tid, "action": "release_lost",
                        "note": "崩溃释放"})
        req2 = next(r for r in cli("list-requests", {})["requests"]
                    if r["request_id"] == res["request_id"])
        assert req2["status"] == "cancelled"
        assert factory.get_task(tid)["status"] == "running"
        new_req = factory.find_request(tid, "monitor")
        assert new_req and new_req["request_id"] != res["request_id"]
        # 旧 run_id 再 report → stale_run / 错误
        out = cli("report", {"request_id": res["request_id"],
                             "run_id": res["run_id"],
                             "outcome": "succeeded"}, expect=1)
        assert out["code"] in ("stale_run", "conflict", "bad_request")


class TestDispatchCrashBlocksSerial:
    def test_lost_dispatch_claim_blocks_new_dispatch(self, factory, cli,
                                                     tmp_env, tmp_path):
        """dispatch claim 崩溃失联：串行位仍占，新 dispatch claim 被拒，
        直到 resolve release_lost（§4.2-3 / state-model §5）。"""
        r1 = factory.make_ready(title="崩溃的派发")
        r2 = factory.make_ready(project="Music", title="排队的派发")
        factory.enqueue_dispatch(r1.task.task_id)
        factory.enqueue_dispatch(r2.task.task_id)
        req1 = factory.find_request(r1.task.task_id, "dispatch")
        out_path = tmp_path / "dclaim.json"
        proc, res = _claim_then_sleep(
            tmp_env,
            {"stage": "dispatch", "executor": EXECUTORS["dispatch"],
             "request_id": req1["request_id"]},
            out_path)
        assert res.get("claimed") is True, f"dispatch claim 未成功：{res}"
        proc.kill()
        proc.wait(timeout=10)
        expire_lease(tmp_env.db, res["request_id"])
        # 失联派生
        t1 = factory.get_task(r1.task.task_id)
        assert t1["display_status"] == "执行端失联"
        assert t1["status"] == "dispatching"   # 库内仍 dispatching
        # 新 dispatch claim 被串行位拒绝
        c2 = cli("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"]})
        assert c2["claimed"] is False
        assert c2["reason"] == "stage_busy"
        # release_lost → 请求 cancelled；dispatch 中途 → needs_user（send_state 保持）
        cli("resolve", {"task_id": r1.task.task_id, "action": "release_lost"})
        t1b = factory.get_task(r1.task.task_id)
        assert t1b["status"] == "needs_user"
        assert t1b["send_state"] == "not_sent"
        cancelled = next(r for r in cli("list-requests", {})["requests"]
                         if r["request_id"] == res["request_id"])
        assert cancelled["status"] == "cancelled"
        # 串行位已释放：第二个任务的 dispatch 可领取
        req2 = factory.find_request(r2.task.task_id, "dispatch")
        c3 = cli("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"],
                           "request_id": req2["request_id"]})
        assert c3["claimed"] is True
        assert c3["context"]["task_id"] == r2.task.task_id

    def test_prepared_crash_keeps_send_state(self, factory, cli, tmp_env,
                                             tmp_path):
        """prepared 阶段崩溃：release_lost → needs_user，send_state 保持 not_sent，
        retry 后回到 ready 可重派。"""
        ready = factory.make_ready(title="prepared 崩溃")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        out_path = tmp_path / "dclaim2.json"
        proc, res = _claim_then_sleep(
            tmp_env,
            {"stage": "dispatch", "executor": EXECUTORS["dispatch"],
             "request_id": req["request_id"]},
            out_path)
        assert res.get("claimed") is True
        # 推进到 prepared 后杀进程
        ev = ui_evidence(
            session_marker=res["context"]["session_marker"],
            repository=res["context"]["repository"],
            attachment_name=res["context"]["attachment_name"])
        cli("dispatch-prepare", {"task_id": tid, "run_id": res["run_id"],
                                 "evidence": ev})
        assert factory.get_task(tid)["status"] == "prepared"
        proc.kill()
        proc.wait(timeout=10)
        expire_lease(tmp_env.db, res["request_id"])
        assert factory.get_task(tid)["display_status"] == "执行端失联"
        cli("resolve", {"task_id": tid, "action": "release_lost"})
        t = factory.get_task(tid)
        assert t["status"] == "needs_user"
        assert t["send_state"] == "not_sent"
        # retry → ready + 新 dispatch 请求 → 可重新走完整链
        cli("resolve", {"task_id": tid, "action": "retry"})
        assert factory.get_task(tid)["status"] == "ready"
        req2 = factory.find_request(tid, "dispatch")
        assert req2 is not None
        # v2：resolve 不清 plan_execution_hold——confirm_plan 入队解除（幂等
        # 复用 retry 建的请求），此后才可领取
        factory.enqueue_dispatch(tid)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"],
                          "request_id": req2["request_id"]})
        assert c["claimed"] is True


class TestPlanCrash:
    def test_plan_claim_lost_returns_needs_plan(self, factory, cli, tmp_env,
                                                tmp_path):
        """plan claim 失联 → release_lost → 回 needs_plan；v2 不自动重建
        plan 请求（§5.22.2），等规划面板手动提交。"""
        task = factory.sync_task(title="规划崩溃")
        req_id, _i, _s = factory.queue_plan(task.task_id)
        req = factory.find_request(task.task_id, "plan")
        out_path = tmp_path / "pclaim.json"
        proc, res = _claim_then_sleep(
            tmp_env,
            {"stage": "plan", "executor": EXECUTORS["plan"],
             "request_id": req["request_id"]},
            out_path)
        assert res.get("claimed") is True
        proc.kill()
        proc.wait(timeout=10)
        expire_lease(tmp_env.db, res["request_id"])
        t = factory.get_task(task.task_id)
        assert t["display_status"] == "执行端失联"
        assert t["status"] == "planning"
        cli("resolve", {"task_id": task.task_id, "action": "release_lost"})
        t2 = factory.get_task(task.task_id)
        assert t2["status"] == "needs_plan"
        # v2 §5.22.2：失联释放不自动重建规划请求
        assert factory.find_request(task.task_id, "plan") is None


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
