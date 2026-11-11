"""历史 pi 会话读取集成测试（protocol §5.16.1 / §5.15.5 / §5.18，v1.6）。

覆盖升级前已发送 pi 轮次的快照、启动幂等、会话正文、身份和轮次隔离。
历史任务/轮次及旧 JSONL 明确由测试夹具恢复；不会派发新的 pi 执行，
不会调用真实模型或访问生产数据。普通新执行的成功路径由 Codex 契约覆盖。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

# 伪 pi 可执行：pi --mode json 的 stdout JSONL 事件假端（T2 测试专用，
# 绝不发起真实模型调用）。事件词汇见 core/kanpilot/pi_backend.py map_event。
FAKE_PI_SOURCE = r'''#!/usr/bin/env python3
"""伪 pi：`pi -p --mode json` 事件流假端（T2 测试专用，绝不发起真实模型调用）。"""
import json
import os
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("0.1.5-fake")
        return 0
    if argv[:2] == ["auth", "check"]:
        return 0
    mode = os.environ.get("FAKE_PI_MODE") or "ok"
    if mode == "hang":
        time.sleep(120)
        return 0
    # 会话身份：--session-dir <run_dir>/pi-sessions 的上一级目录名 = run_id。
    session_dir = "."
    for i, arg in enumerate(argv):
        if arg == "--session-dir" and i + 1 < len(argv):
            session_dir = argv[i + 1]
    run_id = os.path.basename(os.path.dirname(os.path.abspath(session_dir)))
    session_id = f"fake-pi-{run_id}"
    instruction = argv[-1] if argv else ""

    print("pi: boot noise, not a protocol frame", flush=True)
    emit({"type": "session", "id": session_id, "name": "Fake pi",
          "cwd": os.getcwd(), "timestamp": 0})
    emit({"type": "message_end",
          "message": {"role": "user", "timestamp": 1,
                      "content": [{"type": "text", "text": instruction}]}})
    emit({"type": "message_end",
          "message": {"role": "assistant", "timestamp": 2,
                      "model": "deepseek-flash",
                      "content": [{"type": "text", "text": "执行完成。"}]}})
    emit({"type": "tool_execution_start", "toolName": "bash",
          "args": {"command": "ls"}, "timestamp": 3})
    emit({"type": "tool_execution_end", "toolName": "bash",
          "args": {"command": "ls"},
          "result": {"content": [{"type": "text", "text": "a\nb"}],
                     "exitCode": 0},
          "timestamp": 4})
    emit({"type": "tool_execution_start", "toolName": "read",
          "args": {"path": "a.txt"}, "timestamp": 5})
    emit({"type": "tool_execution_end", "toolName": "read",
          "result": {"content": [{"type": "text", "text": "read-ok"}]},
          "timestamp": 6})
    emit({"type": "agent_end", "timestamp": 7})
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

RUN2 = "runisolate0001"
BASE_MESSAGE_KEYS = {
    "id", "seq", "run_id", "type", "content", "revision", "source_at",
}
TOOL_MESSAGE_TYPES = {"tool_call", "tool_result", "command_output"}


# ------------------------------------------------------------------ 工具

def _prepare_env(cli, tmp_env, mode="ok") -> Path:
    """把 CLI/worker 环境切到伪 pi 全链路（run_real，无 FAKE 分支）。"""
    fake = tmp_env.root / "fake-pi"
    fake.write_text(FAKE_PI_SOURCE, encoding="utf-8")
    fake.chmod(0o755)
    cli.env["KANPILOT_PI_BIN"] = str(fake)
    cli.env["KANPILOT_PI_FAKE"] = ""  # 空串 → falsy → worker 走 run_real
    cli.env["FAKE_PI_MODE"] = mode
    return fake


def _runs_root(cli) -> Path:
    return Path(cli("overview", {})["paths"]["session_runs_root"])


def _wait_job_terminal(run_dir: Path, timeout=20.0) -> dict:
    """等 worker 收尾（job 先写、terminal 最后）——confirm 后 job 可能仍在
    running，读取断言前必须等到终态。"""
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        try:
            job = json.loads(
                (run_dir / "job.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            job = None
        if job and job.get("status") in ("finished", "failed"):
            return job
        time.sleep(0.05)
    log_tail = ""
    worker_log = run_dir / "worker.log"
    if worker_log.is_file():
        log_tail = worker_log.read_text(
            encoding="utf-8", errors="replace"
        )[-2000:]
    raise AssertionError(
        f"worker 未在 {timeout}s 内收尾：job={job}\nworker.log 尾部：\n{log_tail}"
    )


def _read_events(run_dir: Path) -> list:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def _pi_evidence(ctx, **extra) -> dict:
    evidence = {
        "session_marker": ctx["session_marker"],
        "repository": ctx["repository"],
        "backend": "pi",
        "config": ctx["exec_config"],
    }
    evidence.update(extra)
    return evidence


def _to_running(factory, cli, tmp_env, mode="ok") -> SimpleNamespace:
    """Restore a pre-upgrade, already-sent pi round and its immutable artifacts.

    New pi execution is rejected. These files model old on-disk transport for
    the reader/identity/isolation contract; no dispatch worker is launched.
    """
    import subprocess
    import sys
    from kanpilot import db, pi_backend
    fake = _prepare_env(cli, tmp_env, mode)
    running = factory.make_historical_running()
    ready = running.ready
    task_id, run_id = running.task.task_id, running.dispatch_run_id
    ctx = running.context
    task = factory.get_task(task_id)
    directory = _runs_root(cli) / task_id / run_id
    directory.mkdir(parents=True)
    worktree = task["delivery"]["worktree_path"]
    spec = {"v": 1, "task_id": task_id, "run_id": run_id, "backend": "pi",
            "worktree_mode": "branch", "cwd": worktree, "runs_dir": str(directory),
            "config": ctx["exec_config"], "pi_home": tmp_env.env["KANPILOT_PI_HOME"],
            "agent_md": str(Path(tmp_env.env["KANPILOT_AGENTS_DIR"]) / "default.md"),
            "timeout_seconds": 120, "plan_path": str(ready.plan.path),
            "instruction": "Historical execution plan: " + str(ready.plan.path)}
    (directory / "spec.json").write_text(json.dumps(spec))
    raw = subprocess.run([sys.executable, str(fake), "--session-dir",
        str(directory / "pi-sessions"), spec["instruction"]], capture_output=True,
        text=True, check=True, cwd=worktree, env=cli.env)
    events = []
    for line in raw.stdout.splitlines():
        try:
            mapped = pi_backend.map_event(json.loads(line))
        except ValueError:
            continue
        if mapped is None or mapped[0] == "turn_end":
            continue
        kind, timestamp, fields = mapped
        events.append({"v": 1, "seq": len(events)+1, "ts": timestamp,
                       "type": kind, **fields})
    events.append({"v": 1, "seq": len(events)+1, "ts": "2026-01-01T00:01:00+00:00",
                   "type": "terminal", "outcome": "completed", "reason": "completed"})
    (directory / "events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
    session_id = events[0]["session_id"]
    job = {"task_id": task_id, "run_id": run_id, "backend": "pi", "config": ctx["exec_config"],
           "session_id": session_id, "session_title": "Fake pi", "pid": 424242,
           "status": "finished", "exit_code": 0, "detail": None,
           "worktree_path": worktree, "started_at": "2026-01-01T00:00:00+00:00",
           "ended_at": "2026-01-01T00:01:00+00:00"}
    (directory / "job.json").write_text(json.dumps(job))
    conn = db.connect(tmp_env.db)
    try:
        conn.execute("UPDATE execution_rounds SET worktree_path=?, session_id=?, session_title='Fake pi', run_status='finished' WHERE task_id=? AND run_id=?",
                     (worktree, session_id, task_id, run_id))
        # Reconstruct the old crash window before confirm, then verify the
        # persisted startup identity remains idempotent without spawning pi.
        conn.execute("UPDATE tasks SET status='submitting' WHERE task_id=?", (task_id,))
        conn.execute("UPDATE requests SET status='claimed' WHERE request_id=?", (running.dispatch_request_id,))
    finally:
        conn.close()
    before = (directory / "job.json").read_bytes()
    again = cli("dispatch-start", {"task_id": task_id, "run_id": run_id})
    assert again["deduplicated"] is True and again["pid"] == job["pid"]
    assert again["session_id"] == session_id
    assert (directory / "job.json").read_bytes() == before
    conn = db.connect(tmp_env.db)
    try:
        conn.execute("UPDATE tasks SET status='running', session_id=?, session_title='Fake pi' WHERE task_id=?", (session_id, task_id))
        conn.execute("UPDATE requests SET status='succeeded' WHERE request_id=?", (running.dispatch_request_id,))
    finally:
        conn.close()
    return SimpleNamespace(ready=ready, task_id=task_id, run_id=run_id, ctx=ctx,
        started={"pid": job["pid"], "session_id": session_id, "worktree_path": worktree},
        run_dir=directory, job=job)


def _write_second_round(runs_root: Path, task_id: str) -> Path:
    """构造第二个轮次目录（不同事件），用于轮次隔离断言。"""
    run_dir = runs_root / task_id / RUN2
    run_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "task_id": task_id, "run_id": RUN2, "backend": "pi",
        "session_id": "sess-isolate-2", "session_title": "隔离轮次",
        "config": {"provider": "CloseAI", "model": "deepseek-flash",
                   "reasoning_effort": "xhigh",
                   "agent_preset": "default"},
        "pid": 424242, "status": "finished",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:01:00+00:00",
        "exit_code": 0, "detail": None,
    }
    (run_dir / "job.json").write_text(
        json.dumps(job, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    events = [
        {"v": 1, "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
         "type": "session", "session_id": "sess-isolate-2",
         "session_title": "隔离轮次"},
        {"v": 1, "seq": 2, "ts": "2026-01-01T00:00:01+00:00",
         "type": "user", "content": "第二轮专属指令"},
        {"v": 1, "seq": 3, "ts": "2026-01-01T00:00:02+00:00",
         "type": "assistant_message", "content": "第二轮专属回复"},
        {"v": 1, "seq": 4, "ts": "2026-01-01T00:01:00+00:00",
         "type": "terminal", "outcome": "completed", "reason": "ok"},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )
    return run_dir


# ------------------------------------------------------------------ 用例

class TestPiRoundLifecycle:
    def test_full_round_lifecycle_and_read_shapes(self, factory, cli,
                                                  tmp_env):
        flow = _to_running(factory, cli, tmp_env)
        task_id, run_id, run_dir = flow.task_id, flow.run_id, flow.run_dir

        # dispatch-start：worktree/mode（docs/state-model「新任务分支工作区」：
        # 首次规划 start-plans 经 task_workspace.prepare_for_submission 创建
        # <app_support>/worktrees/<task>/branch 固定分支工作树，登记 mode=manual
        # + workspace_version=1 + branch/worktree/base/head；cmd_dispatch.
        # _ensure_worktree 对 versioned 任务校验登记五字段后按 branch 模式
        # 使用权威登记路径启动——不再是 plain repos_root）。
        delivery = factory.get_task(task_id)["delivery"]
        assert delivery["mode"] == "manual", delivery
        assert delivery["workspace_version"] == 1, delivery
        from kanpilot import branch_naming
        assert branch_naming.matches(delivery["branch"], task_id), delivery
        assert delivery["branch"].startswith("feat/"), delivery
        assert delivery["base_branch"], delivery
        assert all(delivery.get(key) for key in
                   ("base_sha", "head_sha", "worktree_path")), delivery
        # 本轮尚未执行提交：head 仍冻结在登记的 base 提交上。
        assert delivery["head_sha"] == delivery["base_sha"], delivery
        # 登记路径＝默认 worktrees_root（<app_support>/worktrees）下
        # <task>/branch（task_workspace.prepare_for_submission :59）。
        assert Path(delivery["worktree_path"]) == (
            tmp_env.app_support / "worktrees" / task_id / "run").resolve()
        # dispatch-start 输出＝权威登记路径；真实固定分支工作树的 .git 是指向
        # 主仓 gitdir 的文件（linked worktree），不是 plain 仓库目录。
        assert flow.started["worktree_path"] == delivery["worktree_path"]
        assert (Path(delivery["worktree_path"]) / ".git").is_file()
        spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
        assert spec["v"] == 1
        assert spec["worktree_mode"] == "branch"
        assert spec["cwd"] == flow.started["worktree_path"]
        assert spec["runs_dir"] == str(run_dir)
        assert spec["config"] == flow.ctx["exec_config"]
        assert spec["pi_home"] == tmp_env.env["KANPILOT_PI_HOME"]
        assert spec["agent_md"] == str(
            Path(tmp_env.env["KANPILOT_AGENTS_DIR"]) / "default.md")
        assert spec["timeout_seconds"] > 0
        assert spec["plan_path"] in spec["instruction"]

        # events 首事件 = 启动返回的 session（dispatch-confirm 核验凭据）。
        events = _read_events(run_dir)
        assert events[0]["type"] == "session"
        assert events[0]["session_id"] == flow.started["session_id"]
        assert events[-1]["type"] == "terminal"

        # 轮次索引（§5.16.1）：backend/config 快照/启动意图/pid/worktree。
        rnd = cli("get-task", {"task_id": task_id})[
            "task"]["execution_round"]
        assert rnd["run_id"] == run_id
        assert rnd["backend"] == "pi"
        assert rnd["config"] == flow.ctx["exec_config"]
        assert rnd["started_at"] is not None
        assert rnd["pid"] == flow.started["pid"]
        assert rnd["worktree_path"] == flow.started["worktree_path"]
        assert rnd["session_id"] == flow.started["session_id"]

        # job 收尾现场（伪 pi 正常完成）。
        assert flow.job["status"] == "finished"
        assert flow.job["exit_code"] == 0
        assert flow.job["ended_at"] is not None
        assert flow.job["detail"] is None
        assert flow.job["backend"] == "pi"

        # get-session round_detail（§5.18.4）。
        sess = cli("get-session", {"task_id": task_id, "run_id": run_id})
        detail = sess["round_detail"]
        assert detail is not None, "get-session 应返回 round_detail"
        assert detail["backend"] == "pi"
        assert detail["run_status"] == "finished"
        assert detail["terminal"] == {
            "outcome": "completed", "reason": "completed",
        }
        assert detail["exit_code"] == 0
        assert detail["worktree_path"] == flow.started["worktree_path"]
        assert detail["pid"] == flow.started["pid"]
        assert detail["ended_at"] is not None
        assert detail["detail"] is None
        assert detail["config"] == flow.ctx["exec_config"]
        session = sess["session"]
        assert session["state"] == "associated"
        assert session["backend"] == "pi"
        assert session["save_state"] == "active"
        assert session["session_id"] == flow.started["session_id"]
        assert session["session_title"] == "Fake pi"

        # session-messages：§5.15.5 形状（经 dsh_events 产出）。
        resp = cli("session-messages", {"task_id": task_id, "run_id": run_id,
                                        "limit": 500})
        messages = resp["messages"]
        assert resp["has_more"] is False
        assert session["message_count"] == len(messages)

        types = {m["type"] for m in messages}
        assert types == {
            "system", "user", "assistant",
            "tool_call", "tool_result", "command_output",
        }, types
        assert [m["seq"] for m in messages] == sorted(
            m["seq"] for m in messages
        ), "消息必须按 seq 排序"
        assert len({m["id"] for m in messages}) == len(messages)

        for msg in messages:
            assert BASE_MESSAGE_KEYS <= set(msg), msg
            assert msg["run_id"] == run_id
            assert msg["id"].startswith("round:")
            assert isinstance(msg["revision"], int)
            assert isinstance(msg["source_at"], str)
            if msg["type"] in TOOL_MESSAGE_TYPES:
                assert set(msg["tool"]) == {
                    "name", "input", "output", "exit_code",
                }, msg
            else:
                assert "tool" not in msg, msg

        user = next(m for m in messages if m["type"] == "user")
        assert user["content"] == spec["instruction"]
        assistant = next(m for m in messages if m["type"] == "assistant")
        assert assistant["content"] == "执行完成。"
        tool_call = next(m for m in messages if m["type"] == "tool_call")
        assert tool_call["tool"]["name"] == "bash"
        command = next(
            m for m in messages if m["type"] == "command_output"
        )
        assert command["tool"]["name"] == "bash"
        assert command["tool"]["exit_code"] == 0
        assert command["tool"]["output"] is None
        assert "a\nb" in command["content"]
        tool_result = next(m for m in messages if m["type"] == "tool_result")
        assert tool_result["tool"]["name"] == "read"
        assert tool_result["tool"]["output"] == "read-ok"
        assert tool_result["tool"]["exit_code"] is None

    def test_round_isolation_and_late_events(self, factory, cli, tmp_env):
        flow = _to_running(factory, cli, tmp_env)
        task_id, run_id, run_dir = flow.task_id, flow.run_id, flow.run_dir
        baseline = cli("session-messages",
                       {"task_id": task_id, "run_id": run_id,
                        "limit": 500})["messages"]
        assert baseline, "第一轮应有消息基线"

        # 轮次隔离：构造第二个轮次目录（写不同事件）。
        runs_root = _runs_root(cli)
        round2_dir = _write_second_round(runs_root, task_id)
        assert round2_dir != run_dir

        sess2 = cli("get-session", {"task_id": task_id, "run_id": RUN2})
        assert sess2["round_detail"] is not None
        assert sess2["round_detail"]["backend"] == "pi"
        assert sess2["round_detail"]["terminal"] == {
            "outcome": "completed", "reason": "ok",
        }
        resp2 = cli("session-messages",
                    {"task_id": task_id, "run_id": RUN2, "limit": 500})
        messages2 = resp2["messages"]
        assert resp2["session"]["state"] == "associated"
        assert {m["run_id"] for m in messages2} == {RUN2}
        contents2 = {m["content"] for m in messages2}
        assert "第二轮专属指令" in contents2
        assert "第二轮专属回复" in contents2

        # 第一个轮次读取不受第二轮写入影响（完整基线不变）。
        again = cli("session-messages",
                    {"task_id": task_id, "run_id": run_id,
                     "limit": 500})["messages"]
        assert again == baseline, "第二轮目录写入不得扰动第一轮读取"

        # 旧轮次迟到事件：向已完成的第一轮追加事件。
        late = {
            "v": 1, "seq": 999, "ts": "2026-01-02T00:00:00+00:00",
            "type": "assistant_message", "content": "迟到的补记",
        }
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as stream:
            stream.write(json.dumps(late, ensure_ascii=False) + "\n")

        # 另一轮次（第二轮）的读取结果不变。
        messages2_again = cli(
            "session-messages", {"task_id": task_id, "run_id": RUN2,
                                 "limit": 500}
        )["messages"]
        assert messages2_again == messages2, (
            "向已完成轮次追加迟到事件不得改变另一轮次的读取结果"
        )
        # 自己的目录按 append-only 如实透出（迟到事件只影响本目录）。
        final1 = cli("session-messages",
                     {"task_id": task_id, "run_id": run_id,
                      "limit": 500})["messages"]
        assert len(final1) == len(baseline) + 1
        assert final1[-1]["id"] == "round:999"
        assert final1[-1]["content"] == "迟到的补记"
        assert final1[:-1] == baseline


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
