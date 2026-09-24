"""执行端 runner（protocol.md §5.17/§5.18，v1.6）：以 pi CLI 拉起单轮
领取-执行-汇报循环。

CLI：``python -m kanpilot.executor_runner --stage <dispatch|monitor|review>``
（ExecutorScheduler 以独立子进程拉起；stdout/stderr 重定向到
``<app_support>/executor-logs/<stage>-<ts>.log``）。

职责：
1. 渲染 ``core/executor_prompts/<stage>.md`` 为本次 prompt（{PREAMBLE}
   展开为 _preamble.md；{KANPILOT}/{BRIDGE}/{CORE_ROOT}/{DOCS}/
   {APP_SUPPORT} 替换为真实路径）；
2. 拉起 ``pi -p --mode json --session-dir <app_support>/executor-sessions/
   <stage>/ --no-extensions --no-context-files``，stdin 关闭，stdout 落
   日志文件（事件流不进本进程 stdout）；
3. 超时（settings ``executor_timeout_seconds``，缺省 1800s）→ 终止进程组；
4. 单飞由调用方保证（ExecutorScheduler 先 executor-status 查在途 pid）。

退出码：0=正常结束（无请求立即结束也属正常）；1=拉起失败/超时/异常。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import constants, db

# v2（§5.22.3）：plan 改由 codex planner_runner 承担，不再经 pi executor。
STAGES = ("dispatch", "monitor", "review")
DEFAULT_TIMEOUT_SECONDS = 1800  # 30min：一轮领取循环上限


def _kanpilot_path() -> str:
    return str(constants.core_root() / "kanpilot.py")


def _bridge_path() -> str:
    """Kaneo 只读桥脚本路径（KANPILOT_BRIDGE 覆盖；缺省沿用旧部署位置）。"""
    override = os.environ.get("KANPILOT_BRIDGE")
    if override:
        return override
    return str(
        Path.home() / ".codex" / "skills" / "kaneo-scheduled" / "scripts"
        / "kaneo_bridge.py"
    )


def render_prompt(stage: str) -> str:
    """executor_prompts/<stage>.md → 本次执行端 prompt（{PREAMBLE} 展开为
    _preamble.md 内容）。"""
    base = Path(__file__).resolve().parent.parent / "executor_prompts"
    text = (base / f"{stage}.md").read_text(encoding="utf-8")
    preamble = (base / "_preamble.md").read_text(encoding="utf-8")
    text = text.replace("{PREAMBLE}", preamble)
    return text.replace("{KANPILOT}", _kanpilot_path()) \
               .replace("{BRIDGE}", _bridge_path()) \
               .replace("{CORE_ROOT}", str(constants.core_root())) \
               .replace("{DOCS}", str(constants.core_root().parent / "docs")) \
               .replace("{APP_SUPPORT}", str(constants.app_support()))


def _log_path(stage: str) -> Path:
    log_dir = constants.app_support() / "executor-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{stage}-{stamp}.log"


def _sessions_dir(stage: str) -> Path:
    path = constants.app_support() / "executor-sessions" / stage
    path.mkdir(parents=True, exist_ok=True)
    return path


def _timeout_seconds(conn) -> int:
    value = db.get_setting(conn, "executor_timeout_seconds")
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _record_run(conn, stage: str, **fields) -> dict:
    """把本次运行记录合并进 settings ``executor_runs[stage]``（供
    executor-status 读：stage/pid/started_at/log/exit_code）。"""
    runs = db.get_setting(conn, "executor_runs")
    if not isinstance(runs, dict):
        runs = {}
    entry = dict(runs.get(stage) or {})
    entry.update(fields)
    runs[stage] = entry
    db.set_setting(conn, "executor_runs", runs)
    conn.commit()
    return entry


def run_stage(stage: str) -> int:
    if stage not in STAGES:
        print(f"unknown stage: {stage}", file=sys.stderr)
        return 2

    conn = db.connect(constants.default_db_path())
    timeout = _timeout_seconds(conn)
    prompt = render_prompt(stage)
    sessions_dir = _sessions_dir(stage)
    log_path = _log_path(stage)
    _record_run(
        conn, stage,
        pid=os.getpid(),
        started_at=datetime.now(timezone.utc).isoformat(),
        log=str(log_path),
        exit_code=None,
        finished_at=None,
    )

    argv = [
        constants.pi_bin(),
        "-p",
        "--mode", "json",
        "--session-dir", str(sessions_dir),
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        prompt,
    ]
    env = dict(os.environ)
    env["PI_HOME"] = str(constants.pi_home())
    env.setdefault("KANPILOT_DB", str(constants.default_db_path()))

    deadline = time.monotonic() + timeout
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log,
                env=env, start_new_session=True,
            )
    except OSError as exc:
        print(f"pi 拉起失败：{exc}", file=sys.stderr)
        _record_run(conn, stage, exit_code=-1,
                    finished_at=datetime.now(timezone.utc).isoformat())
        return 1

    try:
        proc.wait(timeout=max(1, int(deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        _terminate(proc)
        print(f"executor {stage} 超时（{timeout}s）已终止：{log_path}",
              file=sys.stderr)
        _record_run(conn, stage, exit_code=-2,
                    finished_at=datetime.now(timezone.utc).isoformat())
        return 1

    rc = proc.returncode
    _record_run(conn, stage, exit_code=rc,
                finished_at=datetime.now(timezone.utc).isoformat())
    if rc != 0:
        print(f"executor {stage} 退出码 {rc}：{log_path}", file=sys.stderr)
        return 1
    return 0


def _terminate(proc, grace: float = 5.0) -> None:
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
    parser = argparse.ArgumentParser(prog="kanpilot.executor_runner")
    parser.add_argument("--stage", required=True, choices=STAGES)
    args = parser.parse_args(argv)
    return run_stage(args.stage)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
