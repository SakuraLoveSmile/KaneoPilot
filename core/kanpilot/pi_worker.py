"""pi 轮次 worker（protocol.md §5.18，v1.6）。

CLI：``python -m kanpilot.pi_worker --spec <spec.json>``（dispatch-start 以
独立会话拉起，stdout/stderr 重定向到运行目录 worker.log，不占协议 stdout）。

spec.json（冻结，v1）::

    {"v": 1, "task_id", "run_id", "session_marker",
     "config": {"provider", "model", "reasoning_effort", "agent_preset"},
     "cwd": "<worktree 绝对路径>", "repository", "plan_path", "instruction",
     "runs_dir", "pi_home", "agent_md", "timeout_seconds"}

worker 职责（写文件，不写 SQLite——SQLite 只经 kanpilot 命令）：
1. 写 ``job.json``（身份三字段 + backend=pi + config + pid + status=running
   + started_at）；
2. 写 ``events.jsonl``：**首事件必须是 session 事件**（含 session_id/
   session_title）——dispatch-confirm 的核验凭据；随后 user/工具/消息事件；
3. 终止前写 terminal 事件 + 更新 job.json（status=finished|failed、
   exit_code、ended_at、detail）。崩溃/超时/截断由 job/events 现场供读取层
   判定（§5.18.4），worker 尽力记录但不回写 SQLite。

KANPILOT_PI_FAKE（隔离测试接缝，保留）：
- ``running``：写 job(running) + session/user 事件后挂起循环（供需存活进程的
  测试，测试负责清理）；
- ``finished``（默认 fake 值）：写 job(finished, exit_code=0) + session/user/
  assistant/terminal(completed) 事件后退出 0；
- ``failed``：写 job(failed, exit_code=1) + terminal(failed) 后退出 1；
- ``crash``：写 job(running) + session 事件后以退出码 3 结束**不写 terminal**
  （模拟崩溃/截断）。
  未设置 → 真实分支 run_real；未知值 → 按 finished 语义。

run_real（真实分支，protocol §5.18.1/§5.18.2/§5.18.4）：
- 校验 ``spec.cwd`` 存在，缺失 → job(failed, detail=cwd_missing) + session +
  terminal(failed)，不越权创建目录；
- 以 ``pi -p --mode json --session-dir <run_dir>/pi-sessions
  [--model p/m] [--thinking l] [--system-prompt <agent.md>] <instruction>``
  拉起独立 pi 进程（cwd=spec.cwd、PI_HOME=spec.pi_home）；
- 消费子进程 stdout 的 JSONL 事件行（**只**结构化事件，OSC/噪声行丢弃，
  绝不透传到本进程 stdout）：经 pi_backend.map_event 映射为轮次事件逐行
  flush 写 events.jsonl（seq 单调）；首个 session 事件落 job 的
  session_id/session_title；
- ``spec.timeout_seconds`` 到期仍运行 → 终止子进程组，job(failed,
  detail=timeout...) + terminal(failed, reason=timeout)；
- 子进程非 0/信号退出 → job(failed, exit_code, detail=crash...) +
  terminal(failed, reason=crash)；正常退出（exit 0 且已见 agent_end/
  turn_end）→ job(finished, exit_code=0) + terminal(completed)；
- 收尾写序恒为「job 先写、session 补齐、terminal 最后」，worker 自身崩溃
  时现场可按 §5.18.4 判定（截断/无 terminal）。
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import constants, dsh_events, pi_backend, util


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


class _EventLog:
    """events.jsonl 追加器（seq 单调递增；flush 每行，供读取层随时消费）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seq = 0
        # 追加已有日志时从既有最大 seq 续号（同轮次只允许一次启动，防御）。
        if self.path.is_file():
            events, _ = dsh_events.iter_events(self.path)
            if events:
                self.seq = max(e.get("seq", 0) for e in events)

    def emit(self, etype: str, ts: str, **fields) -> dict:
        self.seq += 1
        event = {"v": 1, "seq": self.seq, "ts": ts, "type": etype, **fields}
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
        return event


def _base_job(spec: dict) -> dict:
    return {
        "task_id": spec["task_id"],
        "run_id": spec["run_id"],
        "backend": "pi",
        "session_id": None,
        "session_title": None,
        "config": spec.get("config"),
        "pid": os.getpid(),
        "status": "running",
        "started_at": util.now_iso(),
        "ended_at": None,
        "exit_code": None,
        "detail": None,
    }


def run_fake(spec: dict, mode: str) -> int:
    """KANPILOT_PI_FAKE 隔离接缝（契约见模块 docstring）。"""
    run_dir = Path(spec["runs_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    job_path = run_dir / dsh_events.JOB_NAME
    job = _base_job(spec)
    log = _EventLog(run_dir / dsh_events.EVENT_LOG_NAME)
    now = util.now_iso()
    session_id = f"fake-session-{spec['run_id'][:8]}"
    if mode == "running":
        _write_json(job_path, job)
        log.emit("session", now, session_id=session_id,
                 session_title="Fake pi · 运行中")
        log.emit("user", now, content=spec.get("instruction") or "")
        while True:  # 测试负责终止该进程
            time.sleep(60)
    if mode == "crash":
        _write_json(job_path, job)
        log.emit("session", now, session_id=session_id,
                 session_title="Fake pi · 崩溃")
        return 3  # 不写 terminal、不更新 job → 模拟崩溃/截断现场
    failed = mode == "failed"
    job.update({
        "status": "failed" if failed else "finished",
        "ended_at": util.now_iso(),
        "exit_code": 1 if failed else 0,
        "session_id": session_id,
        "session_title": "Fake pi · 已完成" if not failed else "Fake pi · 失败",
        "detail": "fake failure" if failed else None,
    })
    _write_json(job_path, job)
    log.emit("session", now, session_id=session_id,
             session_title=job["session_title"])
    log.emit("user", now, content=spec.get("instruction") or "")
    if not failed:
        log.emit("assistant_message", util.now_iso(), content="执行完成。")
        log.emit("command_output", util.now_iso(), name="exec",
                 input="{}", output="ok\nExit code: 0", exit_code=0)
    log.emit("terminal", util.now_iso(),
             outcome="failed" if failed else "completed",
             reason="fake failure" if failed else "ok")
    return 1 if failed else 0


def run_real(spec: dict) -> int:
    """真实 pi 桥接（protocol §5.18，契约见模块 docstring）。

    生命周期：job(running) 启动即写 → 拉起 ``pi -p --mode json``
    独立进程 → 逐行消费 stdout JSONL 事件 → 按终态收尾
    （job 先写、terminal 最后）。
    """
    run_dir = Path(spec["runs_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    job_path = run_dir / dsh_events.JOB_NAME
    log = _EventLog(run_dir / dsh_events.EVENT_LOG_NAME)
    job = _base_job(spec)
    session_seen = {"written": False}

    def emit_session(session_id, session_title) -> None:
        """首事件必须是 session（已写则不再重复）。"""
        if session_seen["written"]:
            return
        session_seen["written"] = True
        job["session_id"] = session_id
        job["session_title"] = session_title
        log.emit("session", util.now_iso(),
                 session_id=session_id, session_title=session_title)

    def finalize(status: str, exit_code, detail, outcome: str,
                 reason: str) -> int:
        """收尾写序：job 先写（现场可判）→ 补 session → terminal 最后。"""
        job.update({
            "status": status,
            "exit_code": exit_code,
            "ended_at": util.now_iso(),
            "detail": detail,
        })
        _write_json(job_path, job)
        emit_session(job.get("session_id"), job.get("session_title"))
        log.emit("terminal", util.now_iso(), outcome=outcome, reason=reason)
        return 0 if status == "finished" else 1

    # ---- 1. worktree/cwd 校验（spec 已给目录，不越权创建）。
    cwd_value = spec.get("cwd")
    cwd = Path(cwd_value) if cwd_value else None
    if cwd is None or not cwd.is_dir():
        return finalize("failed", 1, f"cwd_missing: {cwd_value}",
                        "failed", "cwd_missing")

    # ---- 2. 启动准备 + job(running) 启动即写（先于任何事件/子进程）。
    timeout = float(spec.get("timeout_seconds")
                    or constants.DEFAULT_PI_ROUND_TIMEOUT_SECONDS)
    if timeout <= 0:
        timeout = float(constants.DEFAULT_PI_ROUND_TIMEOUT_SECONDS)
    agent_md = spec.get("agent_md")
    agent_md_path = Path(agent_md) if agent_md else None
    try:
        argv = pi_backend.launcher_argv(spec, agent_md_path)
        env = pi_backend.child_env(spec)
    except OSError as exc:
        return finalize("failed", 1, f"launch failed: {exc}",
                        "failed", "launch_failed")

    _write_json(job_path, job)
    deadline = time.monotonic() + timeout
    try:
        # stdout=PIPE：事件帧只被本进程消费；stderr 继承（dispatch-start 已
        # 重定向到 worker.log，pi 引导噪声不占协议通道）。
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=None,
            env=env, start_new_session=True, bufsize=0,
        )
    except OSError as exc:
        return finalize("failed", 1, f"launch failed: {exc}",
                        "failed", "launch_failed")

    # ---- 3. 事件消费（JSONL → events.jsonl）。
    state = {"eof": False, "agent_end": False}
    buffer = bytearray()
    fd = proc.stdout.fileno()

    def handle_line(raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            print(f"pi_backend: 忽略非协议 stdout 行：{line[:160]!r}",
                  file=sys.stderr)
            return
        parsed = pi_backend.map_event(message)
        if parsed is None:
            return
        etype, ts, fields = parsed
        if etype == "session":
            state["session_seen"] = True
            # 帧路径已写首事件：置闭包守卫，finalize 不再补写第二个
            # session（首事件恒为且仅为一条 session）。
            session_seen["written"] = True
            job["session_id"] = fields.get("session_id")
            job["session_title"] = fields.get("session_title")
        if etype == "turn_end":
            # pi 的 agent_end 事件（映射为 turn_end）= 整个回合结束。
            state["agent_end"] = True
        log.emit(etype, ts or util.now_iso(), **fields)

    def drain_lines() -> None:
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                return
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            handle_line(line)

    # ---- 4. 泵读至 EOF / 超时。
    timed_out = False
    while not state["eof"]:
        if proc.poll() is not None and not _has_data(fd):
            state["eof"] = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([fd], [], [], min(0.2, remaining))
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                state["eof"] = True
                continue
            buffer.extend(chunk)
            drain_lines()
    # 尾缓冲内可能还有未换行结尾的最后一行。
    if buffer:
        handle_line(bytes(buffer))

    if timed_out:
        _terminate(proc)
        return finalize("failed", proc.returncode,
                        f"timeout after {timeout:g}s", "failed", "timeout")

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    returncode = proc.returncode
    try:
        proc.stdout.close()
    except OSError:
        pass

    # ---- 5. 终态判定（§5.18.4：崩溃/超时/未结束 → 明确异常）。
    if returncode not in (0, None):
        label = (f"signal {-returncode}" if returncode < 0
                 else f"exit code {returncode}")
        return finalize("failed", returncode, f"crash: {label}",
                        "failed", "crash")
    if not state["agent_end"]:
        # exit 0 但未见 agent_end：pi 可能早期退出（provider 认证失败等）。
        return finalize("failed", returncode,
                        "pi 进程退出但未产出 agent_end（疑似启动失败）",
                        "failed", "early_exit")
    return finalize("finished", 0, None, "completed", "completed")


def _has_data(fd) -> bool:
    """非阻塞探测 fd 是否还有可读数据（proc 已退出时排干残留输出）。"""
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        return bool(ready)
    except (OSError, ValueError):
        return False


def _terminate(proc, grace: float = 5.0) -> None:
    """终止子进程组（SIGTERM → 宽限 → SIGKILL），并回收。"""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kanpilot.pi_worker")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if spec.get("v") != 1:
        print(f"unsupported spec version: {spec.get('v')}", file=sys.stderr)
        return 2
    for key in ("task_id", "run_id", "runs_dir"):
        if not spec.get(key):
            print(f"spec missing {key}", file=sys.stderr)
            return 2
    fake = os.environ.get("KANPILOT_PI_FAKE")
    if fake:
        try:
            return run_fake(spec, fake)
        except KeyboardInterrupt:
            return 130
    return run_real(spec)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
