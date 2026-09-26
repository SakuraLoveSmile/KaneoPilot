"""pi 完整会话闭环集成测试（protocol §5.16.1 / §5.15.5 / §5.18，v1.6）。

T2 验收（集成，conftest 的 factory/cli 夹具；无真实模型调用、绝不启动真 pi）：
- 完整闭环：factory.make_ready → enqueue+claim（backend=pi）→
  dispatch-prepare（pi 证据，config 取 context.exec_config）→
  dispatch-begin-send → dispatch-start（断言 worktree/worktree_mode、
  deduplicated 幂等二次调用不创建第二个进程）→ dispatch-confirm
  （session_id 来自启动返回且与 events 首事件一致）→ running；
- get-session 的 round_detail（terminal/exit_code/backend/run_status）；
- session-messages 经 dsh_events 产出 §5.15.5 形状的
  user/assistant/tool_call/tool_result/command_output/system 消息；
- 轮次隔离：构造第二个轮次目录写不同事件 → 第一个轮次读取不受影响；
- 旧轮次迟到事件：向已完成轮次目录追加事件 → 另一轮次读取结果不变。

隔离：tmp_env 的 KANPILOT_PI_FAKE 清空 + KANPILOT_PI_BIN 指向本文件自写的
伪 pi（`pi -p --mode json` 事件流假端，--version/auth check 兼容 §5.17
能力门）——worker 走 run_real 全链路（spec 渲染 → 拉起子进程 → JSONL 消费
→ events.jsonl），模型调用零发生。
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
    """完整派发闭环到 running（§5.18.3），返回断言所需上下文。"""
    _prepare_env(cli, tmp_env, mode)
    ready = factory.make_ready()
    task_id = ready.task.task_id

    factory.enqueue_dispatch(task_id)
    req = factory.find_request(task_id, "dispatch")
    claim = factory.claim("dispatch", request_id=req["request_id"])
    assert claim["claimed"] is True, claim
    ctx = claim["context"]
    run_id = claim["run_id"]
    assert ctx["backend"] == "pi"
    assert set(ctx["exec_config"]) == {
        "backend", "provider", "model", "reasoning_effort", "agent_preset",
    }

    prep = cli("dispatch-prepare", {
        "task_id": task_id, "run_id": run_id,
        "evidence": _pi_evidence(ctx),
    })
    assert prep["status"] == "prepared"
    sent = cli("dispatch-begin-send",
               {"task_id": task_id, "run_id": run_id})
    assert sent["status"] == "submitting"

    started = cli("dispatch-start", {"task_id": task_id, "run_id": run_id})
    assert started["deduplicated"] is False
    assert started["status"] == "started", started
    assert started["pid"] and started["pid"] > 0
    assert started["session_id"], "伪 pi 应已产出 session 事件"

    # §5.18.3 幂等：同一轮次重复 dispatch-start 绝不创建第二个进程。
    again = cli("dispatch-start", {"task_id": task_id, "run_id": run_id})
    assert again["deduplicated"] is True
    assert again["pid"] == started["pid"]
    assert again["session_id"] == started["session_id"]

    confirm = cli("dispatch-confirm", {
        "task_id": task_id, "run_id": run_id,
        "evidence": _pi_evidence(
            ctx, session_id=started["session_id"], protocol_seen=True
        ),
    })
    assert confirm["status"] == "running"

    task = factory.get_task(task_id)
    assert task["status"] == "running"
    assert task["send_state"] == "sent"

    run_dir = _runs_root(cli) / task_id / run_id
    job = _wait_job_terminal(run_dir)
    return SimpleNamespace(
        ready=ready, task_id=task_id, run_id=run_id, ctx=ctx,
        started=started, run_dir=run_dir, job=job,
    )


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

        # dispatch-start：worktree/mode（非 git 夹具 → plain，cwd=仓库目录）。
        assert flow.started["worktree_path"] == str(
            tmp_env.repos_root / "StudyTool"
        )
        spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
        assert spec["v"] == 1
        assert spec["worktree_mode"] == "plain"
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
