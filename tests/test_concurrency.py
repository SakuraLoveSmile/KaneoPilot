"""真实并发竞争测试（多进程 claim / enqueue / 归档取消）。

覆盖契约条款：protocol §4.1（幂等去重：重复点击不重复派发）、§4.2-3/§4.2-6
（claim 事务：并发两个 claim 只有一个成功；`BEGIN IMMEDIATE` 保证，不依赖
文件锁）、§4.2-4（按 request_id 指定领取时并发竞争失败 → `already_claimed`）、
§5.15.2（排队取消与领取竞争：取消在单个 `BEGIN IMMEDIATE` 事务内对该任务
`queued` 请求执行 `cancelled`，事务内复核状态，若已被 `claimed` → 中止并返回
`conflict`，不取消、不改保存状态）、§5.15.3；state-model §3（request 转换）、
§5（dispatch 串行位由 claim 事务内计数保证）。

全部用例用**真实多进程**：每个竞争者是独立 `python3 core/kanpilot.py` 进程，
用同一个临时 DB，靠启动闸门文件对齐起跑时刻——不用 mock、不用线程。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

try:
    from conftest import CORE_BIN, EXECUTORS, db_exec
except ImportError:  # unittest discover 包上下文
    from tests.conftest import CORE_BIN, EXECUTORS, db_exec


# 竞争进程：等闸门文件出现 → 调一次 CLI → 把 stdout 写结果文件
_WORKER = (
    "import json,os,subprocess,sys,time\n"
    "gate=os.environ['KP_GATE']\n"
    "deadline=time.time()+60\n"
    "while not os.path.exists(gate):\n"
    "    if time.time()>deadline: sys.exit(3)\n"
    "    time.sleep(0.002)\n"
    "p=subprocess.run([sys.executable,os.environ['KP_CORE'],sys.argv[1],\n"
    "    '--json',sys.argv[2],'--db',os.environ['KP_DB']],\n"
    "    capture_output=True,text=True)\n"
    "open(sys.argv[3],'w').write(p.stdout)\n"
)


def _spawn_race(tmp_env, tmp_path, jobs, *, timeout=180):
    """并发跑 jobs=[(command, payload), ...]，返回解析后的结果列表。

    jobs 全部用同一闸门对齐起跑；任一进程超时/无输出 → AssertionError。
    """
    gate = tmp_path / f"gate-{os.getpid()}-{time.time_ns()}"
    procs = []
    for index, (command, payload) in enumerate(jobs):
        out_path = tmp_path / f"race-{time.time_ns()}-{index}.json"
        env = dict(tmp_env.env)
        env["KP_CORE"] = CORE_BIN
        env["KP_DB"] = str(tmp_env.db)
        env["KP_GATE"] = str(gate)
        procs.append((index, command, payload, out_path, subprocess.Popen(
            [sys.executable, "-c", _WORKER, command, json.dumps(payload),
             str(out_path)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
    # 全部就位后开闸（进程已启动并在轮询闸门）
    time.sleep(0.4)
    gate.write_text("go", encoding="utf-8")

    results = [None] * len(jobs)
    deadline = time.time() + timeout
    for index, command, payload, out_path, proc in procs:
        remaining = max(1, int(deadline - time.time()))
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError(f"竞争进程超时：{command} {payload}")
        assert out_path.exists(), f"竞争进程无输出：{command} {payload}"
        text = out_path.read_text().strip()
        assert text, f"竞争进程输出为空：{command} {payload}"
        results[index] = json.loads(text)
    for _i, _c, _p, _out, proc in procs:
        if proc.poll() is None:  # pragma: no cover - 防御
            proc.kill()
    return results


def _requests(cli, task_id, kind=None):
    return [r for r in cli("list-requests", {})["requests"]
            if r["task_id"] == task_id and (kind is None or r["kind"] == kind)]


def _attempts(db_path, request_id):
    return db_exec(
        db_path,
        "SELECT outcome FROM attempts WHERE request_id=? ORDER BY id",
        (request_id,))


# ---------------------------------------------------------------------------
# §4.2 claim 事务：并发只有一个成功
# ---------------------------------------------------------------------------


class TestClaimRace:
    def test_auto_pick_dispatch_single_winner(self, factory, cli, tmp_env,
                                              tmp_path):
        """六个执行端同时自动领取 dispatch：只有一个成功（串行位），
        其余 stage_busy/empty/already_claimed；获胜请求 attempt_no==1、
        恰一条 claimed attempt，另一个请求保持 queued——不漏执行不误派发。"""
        a = factory.make_ready(title="竞争-派发A")
        b = factory.make_ready(project="Music", title="竞争-派发B")
        factory.enqueue_dispatch(a.task.task_id)
        factory.enqueue_dispatch(b.task.task_id)
        req_a = factory.find_request(a.task.task_id, "dispatch")
        req_b = factory.find_request(b.task.task_id, "dispatch")

        jobs = [("claim", {"stage": "dispatch",
                           "executor": f"{EXECUTORS['dispatch']}-{i}"})
                for i in range(6)]
        results = _spawn_race(tmp_env, tmp_path, jobs)

        winners = [r for r in results if r.get("claimed") is True]
        assert len(winners) == 1, f"应恰好一个获胜 claim：{results}"
        loser_reasons = {r.get("reason") for r in results
                         if r.get("claimed") is not True}
        # §4.2-4：并发领取失败一律 claimed:false。自动取单的失败方可能是
        # stage_busy（串行位被占）、empty（队列已空/被抢空），修复后也可能
        # 是 already_claimed（二段式重领复核时该请求已被胜者领取）。
        assert loser_reasons <= {"stage_busy", "empty", "already_claimed"}, (
            f"失败原因应属串行位/空队列/已被领取：{loser_reasons}")

        won_id = winners[0]["request_id"]
        assert won_id in (req_a["request_id"], req_b["request_id"])
        won_task = (a.task.task_id if won_id == req_a["request_id"]
                    else b.task.task_id)
        # 库内：获胜请求 claimed 一次；失败者仍 queued
        rows = {r["request_id"]: r for r in cli("list-requests",
                                                {})["requests"]}
        assert rows[won_id]["status"] == "claimed"
        assert rows[won_id]["attempt_no"] == 1
        other = req_b["request_id"] if won_id == req_a["request_id"] \
            else req_a["request_id"]
        assert rows[other]["status"] == "queued", "不得误派发第二个任务"
        assert [o for o, in _attempts(tmp_env.db, won_id)] == ["claimed"]
        # 任务侧：仅一个进入 dispatching
        assert factory.get_task(won_task)["status"] == "dispatching"
        other_task = (b.task.task_id if won_task == a.task.task_id
                      else a.task.task_id)
        assert factory.get_task(other_task)["status"] == "ready"
        # 串行位占用：串行再 claim 仍被拒
        again = cli("claim", {"stage": "dispatch",
                              "executor": EXECUTORS["dispatch"]})
        assert again["claimed"] is False
        assert again["reason"] == "stage_busy"

    def test_same_request_race_single_winner(self, factory, cli, tmp_env,
                                             tmp_path):
        """八个执行端按同一 request_id 争领：一个成功，其余
        `already_claimed`；请求只被领取一次（§4.2-4/§4.2-6）。"""
        ready = factory.make_ready(title="竞争-同一请求")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        jobs = [("claim", {"stage": "dispatch",
                           "executor": f"{EXECUTORS['dispatch']}-{i}",
                           "request_id": req["request_id"]})
                for i in range(8)]
        results = _spawn_race(tmp_env, tmp_path, jobs)

        winners = [r for r in results if r.get("claimed") is True]
        assert len(winners) == 1, f"应恰好一个获胜 claim：{results}"
        for r in results:
            if r.get("claimed") is not True:
                # §4.2-4：指定请求已被领取（并发竞争失败）→ already_claimed。
                # 主 agent 已修复二段式领取的重领复核（此前回落成 empty），
                # 故此处按契约收紧：只接受 already_claimed / stage_busy。
                assert r.get("reason") in ("already_claimed", "stage_busy"), (
                    f"失败原因应为 already_claimed/stage_busy：{r}")
        row = next(r for r in cli("list-requests", {})["requests"]
                   if r["request_id"] == req["request_id"])
        assert row["status"] == "claimed"
        assert row["attempt_no"] == 1
        assert [o for o, in _attempts(tmp_env.db, req["request_id"])] == \
            ["claimed"]
        # run_id 唯一：只有一个执行端拿到运行身份
        run_ids = {r.get("run_id") for r in results if r.get("claimed")}
        assert run_ids == {winners[0]["run_id"]}

    def test_monitor_race_then_single_report(self, factory, cli, tmp_env,
                                             tmp_path):
        """监控争领后只有获胜者能 report succeeded：任务保持 running、
        请求 succeeded 一次，不产生重复观测。"""
        running = factory.make_running(title="竞争-监控")
        tid = running.task.task_id
        req = factory.find_request(tid, "monitor")
        jobs = [("claim", {"stage": "monitor",
                           "executor": f"{EXECUTORS['monitor']}-{i}",
                           "request_id": req["request_id"]})
                for i in range(6)]
        results = _spawn_race(tmp_env, tmp_path, jobs)
        winners = [r for r in results if r.get("claimed") is True]
        assert len(winners) == 1, f"应恰好一个获胜 claim：{results}"
        win = winners[0]

        out = cli("report", {"request_id": win["request_id"],
                             "run_id": win["run_id"],
                             "outcome": "succeeded", "note": "观测"})
        assert out["ok"] is True
        assert out["task_status"] == "running"
        row = next(r for r in cli("list-requests", {})["requests"]
                   if r["request_id"] == req["request_id"])
        assert row["status"] == "succeeded"
        # 一次领取 = 一条 attempt，report 只闭合该 attempt（state-model §3）
        outcomes = [o for o, in _attempts(tmp_env.db, req["request_id"])]
        assert outcomes == ["succeeded"], outcomes
        # 失败者用获胜 run_id 之外的旧身份 report → stale_run
        for r in results:
            if r.get("claimed") is True or not r.get("run_id"):
                continue
            bad = cli("report", {"request_id": req["request_id"],
                                 "run_id": r["run_id"],
                                 "outcome": "succeeded"}, expect=1)
            assert bad["code"] in ("stale_run", "conflict", "bad_request")

    def test_enqueue_race_no_duplicate_dispatch(self, factory, cli, tmp_env,
                                                tmp_path):
        """六端同时 enqueue 同一任务：全部返回同一请求、库内只有一条活动
        dispatch 请求——重复点击不重复派发（§4.1）。"""
        ready = factory.make_ready(title="竞争-入队")
        tid = ready.task.task_id
        plan_sha = factory.get_task(tid)["plan_sha256"]
        jobs = [("enqueue", {"task_id": tid, "kind": "dispatch",
                             "confirm_plan": True,
                             "expected_plan_sha256": plan_sha})
                for _ in range(6)]
        results = _spawn_race(tmp_env, tmp_path, jobs)
        ids = {r["request"]["request_id"] for r in results}
        assert len(ids) == 1, f"应返回同一请求：{ids}"
        active = _requests(cli, tid, "dispatch")
        assert len(active) == 1, f"库内应只有一条活动 dispatch 请求：{active}"
        deduped = [r for r in results if r.get("deduplicated") is True]
        assert len(deduped) >= 1, "至少一个调用应被标记幂等"


# ---------------------------------------------------------------------------
# §5.15.2 排队取消与领取竞争（archive-task ↔ claim）
# ---------------------------------------------------------------------------


class TestCancelVsClaimRace:
    def _round(self, factory, cli, tmp_env, tmp_path, index):
        """一轮：一个 queued dispatch 请求 + 归档进程 vs 领取进程。"""
        ready = factory.make_ready(title=f"竞争-取消{index}")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)
        req = factory.find_request(tid, "dispatch")
        jobs = [
            ("archive-task", {"task_id": tid}),
            ("claim", {"stage": "dispatch",
                       "executor": EXECUTORS["dispatch"],
                       "request_id": req["request_id"]}),
        ]
        results = _spawn_race(tmp_env, tmp_path, jobs)
        archive_out, claim_out = results
        row = next(r for r in cli("list-requests", {})["requests"]
                   if r["request_id"] == req["request_id"])
        task = factory.get_task(tid)
        return tid, req, archive_out, claim_out, row, task

    def test_cancel_vs_claim_is_consistent(self, factory, cli, tmp_env,
                                           tmp_path):
        """归档取消与执行端领取竞争：结果必须二选一且自洽——

        - 领取先成功：请求 `claimed`（非 cancelled），任务 `dispatching`，
          归档返回 `conflict`（任务已被执行端领取），save_state 仍 active；
        - 归档先成功：请求 `cancelled`，save_state=`archived`，
          领取方不得得到 `claimed:true`。

        任何情况下请求不会「既取消又领取」，执行不漏派、不误派。
        """
        branches = {"claimed": 0, "cancelled": 0}
        for i in range(8):
            tid, req, archive_out, claim_out, row, task = self._round(
                factory, cli, tmp_env, tmp_path, i)
            status = row["status"]
            assert status in ("claimed", "cancelled"), (
                f"第 {i} 轮请求状态异常：{status}（archive={archive_out} "
                f"claim={claim_out}）")
            if status == "claimed":
                branches["claimed"] += 1
                assert claim_out.get("claimed") is True, (
                    f"第 {i} 轮领取方应成功：{claim_out}")
                assert row["finished_at"] is None
                assert task["status"] == "dispatching"
                assert task["save_state"] == "active"
                assert archive_out.get("code") == "conflict", (
                    f"第 {i} 轮归档应被拒：{archive_out}")
                assert [o for o, in
                        _attempts(tmp_env.db, req["request_id"])] == ["claimed"]
                # 收尾：释放串行位，避免影响后续轮次
                cli("dispatch-fail", {"task_id": tid,
                                      "run_id": claim_out["run_id"],
                                      "send_state": "not_sent",
                                      "reason": "竞争轮收尾"})
            else:
                branches["cancelled"] += 1
                assert claim_out.get("claimed") is not True, (
                    f"第 {i} 轮请求已取消，领取方不应成功：{claim_out}")
                assert task["save_state"] == "archived"
                assert archive_out.get("ok") is True, (
                    f"第 {i} 轮归档应成功：{archive_out}")
                # 取消后不得再被领取
                again = cli("claim", {"stage": "dispatch",
                                      "executor": EXECUTORS["dispatch"],
                                      "request_id": req["request_id"]},
                            expect=1)
                assert again["code"] in ("conflict", "bad_request"), (
                    f"已取消请求不得再领取：{again}")
        assert sum(branches.values()) == 8
        # 记录分支分布（竞争结果本身不保证两侧都出现）
        assert branches["claimed"] + branches["cancelled"] == 8

    def test_trash_cancel_vs_claim_is_consistent(self, factory, cli, tmp_env,
                                                 tmp_path):
        """trash-task 与领取竞争同样自洽（移入回收站同事务取消 queued）。"""
        claimed_seen = cancelled_seen = 0
        for i in range(4):
            ready = factory.make_ready(project="Music",
                                       title=f"竞争-回收{i}")
            tid = ready.task.task_id
            factory.enqueue_dispatch(tid)
            req = factory.find_request(tid, "dispatch")
            jobs = [
                ("trash-task", {"task_id": tid}),
                ("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"],
                           "request_id": req["request_id"]}),
            ]
            trash_out, claim_out = _spawn_race(tmp_env, tmp_path, jobs)
            row = next(r for r in cli("list-requests", {})["requests"]
                       if r["request_id"] == req["request_id"])
            task = factory.get_task(tid)
            if row["status"] == "claimed":
                claimed_seen += 1
                assert trash_out.get("code") == "conflict"
                assert task["save_state"] == "active"
                cli("dispatch-fail", {"task_id": tid,
                                      "run_id": claim_out["run_id"],
                                      "send_state": "not_sent",
                                      "reason": "竞争轮收尾"})
            else:
                cancelled_seen += 1
                assert row["status"] == "cancelled"
                assert trash_out.get("ok") is True
                assert task["save_state"] == "trashed"
                assert claim_out.get("claimed") is not True
        assert claimed_seen + cancelled_seen == 4


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
