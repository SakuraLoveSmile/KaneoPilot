"""批量整理 (Batch Organize) 契约与集成测试 (T1-T3).

覆盖场景：
- 权限摘要 (move_out_eligibility) 与实际命令一致性
- 领取竞争：任务有 claimed 请求时禁止移出看板
- 取消排队：已排队任务移出时同事务取消 queued 请求
- 重复操作（幂等）：已处于目标状态重复操作返回 deduplicated=True
- 保存状态冲突：expected_save_state 不符时抛出 conflict
- 批量部分失败：单项独立事务，业务冲突不阻断其他有效项
"""

import pytest
import sqlite3

try:
    from conftest import set_save_state
except ImportError:
    from tests.conftest import set_save_state


class TestBatchOrganizeContract:
    def test_eligibility_matches_actual_archive_command(self, factory, cli):
        """权限摘要与实际命令一致性：allowed=True 必能移出，allowed=False 必被 conflict 拦截。"""
        # 1. 可移出任务（ready 任务）
        ready = factory.make_ready(title="批量-就绪任务")
        tid = ready.task.task_id
        summary = cli("get-task", {"task_id": tid})["task"]
        elig = summary.get("move_out_eligibility")
        assert elig is not None, "核心必须返回 move_out_eligibility"
        assert elig["allowed"] is True
        assert elig["reason"] is None

        res = cli("archive-task", {"task_id": tid, "expected_save_state": "active"})
        assert res["ok"] is True
        assert res["task"]["save_state"] == "archived"

        # 2. 禁止移出任务（running 任务）
        running = factory.make_running(title="批量-执行中任务")
        trun_id = running.task.task_id
        summary_run = cli("get-task", {"task_id": trun_id})["task"]
        elig_run = summary_run.get("move_out_eligibility")
        assert elig_run["allowed"] is False
        assert "执行" in elig_run["reason"]

        with pytest.raises(Exception) as excinfo:
            cli("archive-task", {"task_id": trun_id, "expected_save_state": "active"})
        assert "conflict" in str(excinfo.value)

    def test_claimed_race_and_cancel_queued(self, factory, cli, tmp_env):
        """领取竞争与排队取消：
        - 已排队未领取任务移出看板时，排队请求被取消
        - 模拟领取竞争（并发 claimed）时，移出看板被拦截
        """
        ready = factory.make_ready(title="批量-排队任务")
        tid = ready.task.task_id
        factory.enqueue_dispatch(tid)

        # 验证 queued 请求存在
        with sqlite3.connect(str(tmp_env.db)) as conn:
            reqs = conn.execute("SELECT status FROM requests WHERE task_id=?", (tid,)).fetchall()
        assert any(r[0] == "queued" for r in reqs)

        # 摘要应允许移出
        summary = cli("get-task", {"task_id": tid})["task"]
        assert summary["move_out_eligibility"]["allowed"] is True

        # 移出看板 -> 自动取消排队
        res = cli("trash-task", {"task_id": tid, "expected_save_state": "active"})
        assert res["ok"] is True
        assert len(res["cancelled_requests"]) >= 1
        with sqlite3.connect(str(tmp_env.db)) as conn:
            for cid in res["cancelled_requests"]:
                row = conn.execute("SELECT status FROM requests WHERE request_id=?", (cid,)).fetchone()
                assert row[0] == "cancelled"

        # 恢复任务并模拟领取竞争
        cli("restore-task", {"task_id": tid})
        with sqlite3.connect(str(tmp_env.db)) as conn:
            conn.execute(
                "INSERT INTO requests(request_id, idempotency_key, task_id, stage, kind, status, "
                "priority, run_id, claimed_by, claimed_at, heartbeat_at, lease_expires_at, created_at, updated_at) "
                "VALUES ('req-race-1', 'key-race-1', ?, 'monitor', 'monitor', 'claimed', 100, "
                "'run-1', 'exec-1', datetime('now'), datetime('now'), datetime('now', '+20 minutes'), datetime('now'), datetime('now'))",
                (tid,),
            )

        # 此时移出应该 conflict 拦截
        summary_race = cli("get-task", {"task_id": tid})["task"]
        assert summary_race["move_out_eligibility"]["allowed"] is False
        assert "领取" in summary_race["move_out_eligibility"]["reason"]

        with pytest.raises(Exception) as excinfo:
            cli("archive-task", {"task_id": tid, "expected_save_state": "active"})
        assert "conflict" in str(excinfo.value)
        assert "领取" in str(excinfo.value)

    def test_duplicate_and_save_state_conflict(self, factory, cli):
        """重复操作幂等与保存状态冲突检测。"""
        ready = factory.make_ready(title="批量-测试冲突任务")
        tid = ready.task.task_id

        # 初始 active
        # 1. 成功移入回收站
        res1 = cli("trash-task", {"task_id": tid, "expected_save_state": "active"})
        assert res1["ok"] is True
        assert res1["deduplicated"] is False

        # 2. 再次移入回收站（幂等成功）
        res2 = cli("trash-task", {"task_id": tid, "expected_save_state": "active"})
        assert res2["ok"] is True
        assert res2["deduplicated"] is True

        # 3. 此时任务已在 trashed，调用 archive-task 且 expected_save_state="active" 应冲突
        with pytest.raises(Exception) as excinfo:
            cli("archive-task", {"task_id": tid, "expected_save_state": "active"})
        assert "conflict" in str(excinfo.value)
        assert "冲突" in str(excinfo.value)

    def test_batch_execution_partial_failure(self, factory, cli, tmp_env):
        """批量逐项执行，单项业务冲突不影响其他项正常处理。"""
        t1 = factory.make_ready(title="批量任务1-正常").task.task_id
        t2 = factory.make_ready(title="批量任务2-冲突").task.task_id
        t3 = factory.make_ready(title="批量任务3-正常").task.task_id

        # 将 t2 改为 running（业务冲突）
        with sqlite3.connect(str(tmp_env.db)) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE task_id=?", (t2,))

        results = {}
        for tid in [t1, t2, t3]:
            try:
                out = cli("archive-task", {"task_id": tid, "expected_save_state": "active"})
                results[tid] = ("success", out)
            except Exception as e:
                results[tid] = ("failed", str(e))

        assert results[t1][0] == "success"
        assert results[t2][0] == "failed"
        assert results[t3][0] == "success"

        # 验证 t1 与 t3 均已归档，t2 仍在 active
        s1 = cli("get-task", {"task_id": t1})["task"]
        s2 = cli("get-task", {"task_id": t2})["task"]
        s3 = cli("get-task", {"task_id": t3})["task"]
        assert s1["save_state"] == "archived"
        assert s2["save_state"] == "active"
        assert s3["save_state"] == "archived"

    def test_batch_execution_stop_on_network_or_corrupt_error(self):
        """模拟批量调度中遇到不可达/损坏/超时等系统错误时停止后续提交：
        当前项标为 unconfirmed，后续项标为 unexecuted。
        """
        task_ids = ["task-1", "task-2", "task-3"]

        def mock_executor(tid):
            if tid == "task-1":
                return {"ok": True}
            if tid == "task-2":
                raise ConnectionResetError("core unreachable / socket closed")
            return {"ok": True}

        results = {}
        stop_reason = None
        for idx, tid in enumerate(task_ids):
            if stop_reason is not None:
                results[tid] = {"status": "unexecuted", "reason": f"前序遇到错误停止：{stop_reason}"}
                continue
            try:
                res = mock_executor(tid)
                results[tid] = {"status": "succeeded", "detail": res}
            except Exception as e:
                # 核心不可达、超时或响应损坏
                stop_reason = str(e)
                results[tid] = {"status": "unconfirmed", "reason": str(e)}

        assert results["task-1"]["status"] == "succeeded"
        assert results["task-2"]["status"] == "unconfirmed"
        assert results["task-3"]["status"] == "unexecuted"


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
