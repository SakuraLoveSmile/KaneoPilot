"""Codex 规划 runner（protocol.md §5.22.3，v2/T5）。

CLI：``python -m kanpilot.planner_runner [--request-id <id>] [--once]``
（App 侧经 ExecutorScheduler 以独立子进程拉起；stdout/stderr 重定向到
``<app_support>/executor-logs/planner-<ts>.log``）。

职责（手动规划链执行端）：
1. ``claim`` stage=plan executor=``kaneo-planner``（--request-id 定点领取
   或自动取单；未授权/无任务 → 结束）；
2. 在 ``<session_runs_root>/<task_id>/<run_id>/`` 写 ``job.json``
   （backend=codex）+ ``events.jsonl``（session 首事件；codex JSONL
   进度经 codex_planner.map_event 翻译）；原始 JSONL 落 ``codex.jsonl``；
3. 拉起 ``codex exec``（只读 sandbox；prompt 经 stdin；``--output-schema``
   约束最终输出；``-o`` 落最后消息）；心跳 ``heartbeat`` 间隔
   ``min(lease_seconds/3, PLANNER_HEARTBEAT_MAX_SECONDS)``；
   租约失效/心跳返回 stale_run → 终止子进程组、禁止发布；
4. 终态：schema 校验 ``-o`` 输出——ready → 生成带 frontmatter 的计划文件
   （元数据由本进程按真实输入生成，不信模型自报）→ ``publish-plan``；
   needs_input → 草稿落 ``plan-draft.md`` + ``report needs_user``；
   超时/崩溃/输出不合规 → ``report failed``（不自动重试）。
   publish-plan 自身在同事务结束请求（§5.6），成功路径不再 report。

超时：settings ``codex_timeout_seconds`` > ``executor_timeout_seconds`` >
1800s。单飞由调用方保证（同 executor_runner 约定）。

KANPILOT_CODEX_FAKE（隔离测试接缝，不拉起 codex）：
- ``ready``（默认 fake 值）：写假事件 + 合成 ready 输出 → publish-plan；
- ``needs_input``：写 questions 输出 → report needs_user；
- ``invalid``：写不合规输出 → report failed；
- ``fail``/``crash``：写 job(running) 后以非零退出/不写 terminal →
  report failed（crash 模拟子进程异常）。
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import cli, codex_planner, constants, db, headless_adapter, util
from . import dsh_events

DEFAULT_PLANNER_TIMEOUT_SECONDS = 1800


# ------------------------------------------------------------ 轮次记录（同 pi_worker 口径）

def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


class _EventLog:
    """events.jsonl 追加器（seq 单调；flush 每行）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seq = 0
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


# ------------------------------------------------------------ 计划文件生成

def _yaml_scalar(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _plan_file_path(project_name: Optional[str], title: str) -> Path:
    root = constants.plan_root()
    directory = root / project_name if project_name else root
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe = "".join(
        ch if (ch.isalnum() or ch in " _-[]【】（）()") else "_"
        for ch in (title or "plan")
    ).strip()[:80] or "plan"
    path = directory / f"{safe}_{stamp}.md"
    counter = 1
    while path.exists():
        path = directory / f"{safe}_{stamp}-{counter}.md"
        counter += 1
    return path


def _strip_model_frontmatter(markdown: str) -> str:
    """模型若在正文开头自带 frontmatter，剥掉（元数据由本进程生成）。"""
    text = markdown.lstrip("\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4:].lstrip("\n")
    return markdown


def write_plan_file(
    context: dict, manifest: dict, plan_markdown: str, generated_at: str,
    task_row=None,
) -> Path:
    """按本轮真实输入生成 frontmatter + 模型 plan_markdown 正文 → PLAN_ROOT。

    ``task_row``：DB 原始任务行（review_fix 必填字段 review_report_* 不在
    claim context 的 TaskSummary 里，由调用方按 task_id 拉取）。
    """
    task = context.get("task") or {}
    req_payload = context.get("payload") or {}
    manifest_task = manifest.get("task") or {}
    task_id = context.get("task_id") or manifest_task.get("task_id")
    project_name = (
        task.get("project_name") or manifest_task.get("project_name")
    )
    project_id = (
        manifest_task.get("project_id")
        or constants.PROJECTS.get(project_name or "")
    )
    repository = (
        task.get("repository") or manifest_task.get("repository") or ""
    )
    model_config = context.get("payload", {}).get("model_config") or {}
    plan_kind = task.get("plan_kind") or manifest_task.get("plan_kind") \
        or "standard"
    meta = {
        "task_id": task_id,
        "project_id": project_id,
        "project_name": project_name,
        "repository": repository,
        "source_sha256": manifest.get("source_sha256")
        or (task.get("source_sha256") or ""),
        "requirements_sha256": manifest.get("requirements_sha256")
        or (task.get("requirements_sha256") or ""),
        "planner_model": model_config.get("planner_model") or "",
        "reasoning_effort": model_config.get("reasoning_effort") or "",
        "implementation_status": "未开始",
        "generated_at": generated_at,
        "plan_kind": plan_kind,
        "planner_input_sha256": context.get("payload", {}).get(
            "planner_input_sha256") or "",
    }
    if plan_kind == "review_fix":
        row = task_row or {}
        meta.update({
            # 优先 DB 任务行（含 review_report_*）；回落请求 payload
            # （_submit_one 写入 review_key/review_source_task_id）。
            "review_report_path": (
                row["review_report_path"]
                if row and row["review_report_path"]
                else ""
            ),
            "review_report_sha256": (
                row["review_report_sha256"]
                if row and row["review_report_sha256"]
                else ""
            ),
            "review_source_task_id": (
                (row["review_source_task_id"] if row else None)
                or req_payload.get("review_source_task_id") or ""
            ),
            "review_key": (
                (row["review_key"] if row else None)
                or req_payload.get("review_key") or ""
            ),
        })
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    body = _strip_model_frontmatter(plan_markdown or "")
    path = _plan_file_path(project_name, task.get("title") or task_id or "plan")
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return path


# ------------------------------------------------------------ 输出契约校验

def parse_output(path: Path) -> dict:
    """读取 -o 落盘的最终消息并校验输出契约（§5.22.3）。

    返回规范化 dict；不合规 → util.KanpilotError(validation_failed)。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise util.KanpilotError(
            "validation_failed", f"规划输出不可读：{exc}"
        ) from exc
    text = text.strip()
    if not text:
        raise util.KanpilotError("validation_failed", "规划输出为空")
    try:
        data = json.loads(text)
    except ValueError:
        # 宽容：模型可能把 JSON 包在 markdown 围栏里。
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except ValueError as exc:
                raise util.KanpilotError(
                    "validation_failed", f"规划输出不是 JSON：{exc}"
                ) from exc
        else:
            raise util.KanpilotError(
                "validation_failed", "规划输出不是 JSON"
            )
    if not isinstance(data, dict):
        raise util.KanpilotError("validation_failed", "规划输出不是对象")
    status = data.get("status")
    if status not in ("ready", "needs_input"):
        raise util.KanpilotError(
            "validation_failed", f"规划输出 status 非法：{status!r}"
        )
    questions = data.get("questions")
    if not isinstance(questions, list):
        questions = []
    questions = [str(q) for q in questions if str(q).strip()]
    plan_markdown = data.get("plan_markdown")
    if not isinstance(plan_markdown, str):
        plan_markdown = ""
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    if status == "ready":
        if not plan_markdown.strip():
            raise util.KanpilotError(
                "validation_failed", "ready 输出缺少 plan_markdown"
            )
        if questions:
            raise util.KanpilotError(
                "validation_failed", "ready 输出仍有未决 questions"
            )
    return {
        "status": status,
        "plan_markdown": plan_markdown,
        "questions": questions,
        "evidence": [str(e) for e in evidence],
    }


# ------------------------------------------------------------ 子进程

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


def _has_data(fd) -> bool:
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        return bool(ready)
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------ 单轮执行

def _timeout_seconds(conn) -> int:
    for key in ("codex_timeout_seconds", "executor_timeout_seconds"):
        value = db.get_setting(conn, key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return DEFAULT_PLANNER_TIMEOUT_SECONDS


def _heartbeat(db_path: Path, request_id: str, run_id: str,
               progress: Optional[str]) -> None:
    cli.run("heartbeat", {
        "request_id": request_id, "run_id": run_id,
        "progress": progress or "planning",
    }, db_path)


def _report(db_path: Path, request_id: str, run_id: str, outcome: str,
            note: str, failure_code: Optional[str] = None,
            evidence: Optional[dict] = None) -> None:
    payload = {
        "request_id": request_id, "run_id": run_id,
        "outcome": outcome, "note": note,
    }
    if failure_code:
        payload["failure_code"] = failure_code
        payload["failure_reason"] = note
    if evidence is not None:
        payload["evidence"] = evidence
    cli.run("report", payload, db_path)


def _mark_round_finished(conn, task_id: str, run_id: str, status: str,
                         now: str) -> None:
    conn.execute(
        "UPDATE execution_rounds SET run_status=?, updated_at=? "
        "WHERE task_id=? AND run_id=?",
        (status, now, task_id, run_id),
    )
    conn.commit()


def run_one(db_path: Path, request_id: Optional[str] = None) -> bool:
    """领取并执行一个规划轮次。返回 True=处理了一个请求（可继续领取）。"""
    claim = cli.run("claim", {
        "stage": "plan", "executor": constants.PLANNER_EXECUTOR_ID,
        **({"request_id": request_id} if request_id else {}),
    }, db_path)
    if not claim.get("claimed"):
        return False

    req_id, run_id = claim["request_id"], claim["run_id"]
    context = claim.get("context") or {}
    req_payload = context.get("payload") or {}
    task = context.get("task") or {}
    task_id = context.get("task_id") or task.get("task_id")
    lease_seconds = int(context.get("lease_seconds")
                        or constants.DEFAULT_LEASE_SECONDS)
    # §5.22.3：心跳 ≤ min(lease/3, 60)（不再设下限，短租约须更密）。
    heartbeat_every = min(
        lease_seconds / 3.0,
        float(constants.PLANNER_HEARTBEAT_MAX_SECONDS))


    conn = db.connect(db_path)
    try:
        source = headless_adapter.resolve_source(conn)
        run_dir = source.run_dir(task_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        timeout = _timeout_seconds(conn)
        # 轮次索引行已由 claim 写入（backend=codex）；此处标记进程启动。
        conn.execute(
            "UPDATE execution_rounds SET run_status='running', pid=?, "
            "started_at=COALESCE(started_at, ?), updated_at=? "
            "WHERE task_id=? AND run_id=?",
            (os.getpid(), util.now_iso(), util.now_iso(), task_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()

    job = {
        "task_id": task_id, "run_id": run_id, "backend": "codex",
        "session_id": None, "session_title": None,
        "config": req_payload.get("model_config"),
        "input_id": context.get("input_id"),
        "planner_input_sha256": req_payload.get("planner_input_sha256"),
        "pid": os.getpid(), "status": "running",
        "started_at": util.now_iso(), "ended_at": None,
        "exit_code": None, "detail": None,
    }
    _write_json(run_dir / dsh_events.JOB_NAME, job)
    log = _EventLog(run_dir / dsh_events.EVENT_LOG_NAME)
    codex_log_path = run_dir / constants.PLANNER_CODEX_LOG_NAME
    manifest = req_payload.get("manifest") or {}
    input_dir = context.get("input_dir")

    def finalize(status: str, exit_code, detail: str, outcome: str,
                 reason: str) -> None:
        job.update({
            "status": status, "exit_code": exit_code,
            "ended_at": util.now_iso(), "detail": detail,
        })
        _write_json(run_dir / dsh_events.JOB_NAME, job)
        log.emit("terminal", util.now_iso(), outcome=outcome, reason=reason)
        conn2 = db.connect(db_path)
        try:
            _mark_round_finished(
                conn2, task_id, run_id,
                "finished" if status == "finished" else "failed",
                util.now_iso(),
            )
        finally:
            conn2.close()

    fake = os.environ.get("KANPILOT_CODEX_FAKE")
    if fake:
        return _run_fake(db_path, run_dir, job, log, context, manifest,
                         req_id, run_id, fake, finalize)

    # ---- 真实 codex exec ----
    schema_path = run_dir / "output-schema.json"
    out_path = run_dir / "last-message.txt"
    _write_json(schema_path, codex_planner.output_schema())
    images = codex_planner.image_files(
        manifest, Path(input_dir) if input_dir else None
    )
    prompt = codex_planner.build_prompt(
        task, manifest, Path(input_dir) if input_dir else None
    )
    job_for_argv = {
        "model_config": req_payload.get("model_config"),
        "repository": task.get("repository")
        or (manifest.get("task") or {}).get("repository"),
    }
    argv = codex_planner.launcher_argv(
        job_for_argv, schema_path, out_path, images)
    env = codex_planner.child_env()

    log.emit("session", util.now_iso(),
             session_id=None, session_title=f"codex plan {task_id}")
    log.emit("user", util.now_iso(), content=prompt)

    deadline = time.monotonic() + timeout
    next_heartbeat = time.monotonic() + heartbeat_every
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        finalize("failed", 1, f"launch failed: {exc}", "failed",
                 "launch_failed")
        _report(db_path, req_id, run_id, "failed",
                f"codex 拉起失败：{exc}", failure_code="planner_failed")
        return True
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    except (OSError, BrokenPipeError):
        pass

    state = {"eof": False, "turn_end": False, "stale": False}
    buffer = bytearray()
    fd = proc.stdout.fileno()
    raw_log = open(codex_log_path, "ab")

    def handle_line(raw: bytes) -> None:
        raw_log.write(raw + b"\n")
        raw_log.flush()
        line = raw.strip()
        if not line:
            return
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        parsed = codex_planner.map_event(message)
        if parsed is None:
            return
        etype, ts, fields = parsed
        if etype == "session":
            job["session_id"] = fields.get("session_id")
            job["session_title"] = fields.get("session_title")
        if etype == "turn_end":
            state["turn_end"] = True
        log.emit(etype, ts or util.now_iso(), **fields)

    def drain() -> None:
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                return
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            handle_line(line)

    timed_out = False
    heartbeat_failures = 0
    while not state["eof"]:
        now_m = time.monotonic()
        if now_m >= deadline:
            timed_out = True
            break
        if now_m >= next_heartbeat:
            try:
                _heartbeat(db_path, req_id, run_id, "planning")
                heartbeat_failures = 0
            except util.KanpilotError as exc:
                # stale_run / 失联 → 终止并禁止发布；瞬时错误（busy/锁）
                # 连续三次才放弃，避免一次抖动杀掉整轮。
                if exc.code in ("stale_run", "conflict", "not_found"):
                    state["stale"] = True
                    job["detail"] = f"heartbeat failed: {exc}"
                    break
                heartbeat_failures += 1
                if heartbeat_failures >= 3:
                    state["stale"] = True
                    job["detail"] = f"heartbeat failed x3: {exc}"
                    break
            except Exception as exc:  # 网络/IO 级错误同样三次熔断
                heartbeat_failures += 1
                if heartbeat_failures >= 3:
                    state["stale"] = True
                    job["detail"] = f"heartbeat failed x3: {exc}"
                    break
            next_heartbeat = now_m + heartbeat_every
        if proc.poll() is not None and not _has_data(fd):
            state["eof"] = True
            break
        ready, _, _ = select.select(
            [fd], [], [], min(0.5, max(0.05, deadline - now_m))
        )
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                state["eof"] = True
                continue
            buffer.extend(chunk)
            drain()
    if buffer:
        handle_line(bytes(buffer))
    raw_log.close()

    if state["stale"]:
        _terminate(proc)
        finalize("failed", proc.returncode, "lease lost (stale_run)",
                 "failed", "stale_run")
        return True  # 不汇报：请求所有权已失
    if timed_out:
        _terminate(proc)
        finalize("failed", proc.returncode, f"timeout after {timeout:g}s",
                 "failed", "timeout")
        _report(db_path, req_id, run_id, "failed",
                f"规划超时（{timeout:g}s）", failure_code="planner_timeout")
        return True

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    returncode = proc.returncode
    try:
        proc.stdout.close()
    except OSError:
        pass

    if returncode not in (0, None):
        finalize("failed", returncode, f"crash: exit {returncode}",
                 "failed", "crash")
        _report(db_path, req_id, run_id, "failed",
                f"codex 异常退出（{returncode}）",
                failure_code="planner_failed",
                evidence={"codex_log": str(codex_log_path)})
        return True

    # exit 0 不等于成功：须通过输出契约校验（§5.22.3）。
    try:
        output = parse_output(out_path)
    except util.KanpilotError as exc:
        finalize("failed", returncode, str(exc), "failed",
                 "invalid_output")
        _report(db_path, req_id, run_id, "failed",
                f"规划输出不合规：{exc.message}",
                failure_code="planner_output_invalid",
                evidence={"codex_log": str(codex_log_path)})
        return True

    if output["status"] == "needs_input":
        draft_path = run_dir / "plan-draft.md"
        if output["plan_markdown"].strip():
            draft_path.write_text(output["plan_markdown"], encoding="utf-8")
        finalize("finished", 0, "needs_input", "completed", "needs_input")
        _report(db_path, req_id, run_id, "needs_user",
                "规划需要补充材料：" + "；".join(output["questions"] or ["见草稿"]),
                failure_code="reported",
                evidence={
                    "questions": output["questions"],
                    "draft_path": str(draft_path),
                    "evidence": output["evidence"],
                })
        return True

    # ready → 生成计划文件 → publish-plan（同事务结束请求）。
    generated_at = util.now_iso()
    task_row = None
    plan_kind = (task.get("plan_kind")
                 or (manifest.get("task") or {}).get("plan_kind")
                 or "standard")
    if plan_kind == "review_fix" and task_id:
        conn = db.connect(db_path)
        try:
            task_row = db.get_task(conn, task_id)
        finally:
            conn.close()
    try:
        plan_path = write_plan_file(context, manifest,
                                    output["plan_markdown"], generated_at,
                                    task_row=task_row)
    except OSError as exc:
        finalize("failed", 0, f"plan write failed: {exc}", "failed",
                 "plan_write_failed")
        _report(db_path, req_id, run_id, "failed",
                f"计划文件写入失败：{exc}", failure_code="planner_failed")
        return True

    model_config = req_payload.get("model_config") or {}
    try:
        cli.run("publish-plan", {
            "task_id": task_id,
            "plan_path": str(plan_path),
            "plan_kind": plan_kind,
            "source_sha256": manifest.get("source_sha256")
            or task.get("source_sha256") or "",
            "requirements_sha256": manifest.get("requirements_sha256")
            or task.get("requirements_sha256") or "",
            "generated_at": generated_at,
            "planner_model": model_config.get("planner_model") or "",
            "reasoning_effort": model_config.get("reasoning_effort") or "",
            "request_id": req_id,
            "run_id": run_id,
            "planner_input_sha256": req_payload.get("planner_input_sha256")
            or "",
        }, db_path)
    except util.KanpilotError as exc:
        finalize("failed", 0, f"publish failed: {exc.message}", "failed",
                 "publish_failed")
        _report(db_path, req_id, run_id, "failed",
                f"计划发布被拒：{exc.message}", failure_code="planner_failed")
        return True
    finalize("finished", 0, None, "completed", "plan_published")
    return True


# ------------------------------------------------------------ fake 接缝

def _run_fake(db_path: Path, run_dir: Path, job: dict, log: _EventLog,
              context: dict, manifest: dict, req_id: str, run_id: str,
              mode: str, finalize) -> bool:
    task = context.get("task") or {}
    task_id = context.get("task_id") or task.get("task_id")
    now = util.now_iso()
    log.emit("session", now, session_id=f"fake-codex-{run_id[:8]}",
             session_title="Fake codex · 规划")
    log.emit("user", now, content="fake plan prompt")
    job["session_id"] = f"fake-codex-{run_id[:8]}"
    out_path = run_dir / "last-message.txt"

    if mode == "crash":
        job.update({"status": "failed", "exit_code": 3,
                    "ended_at": util.now_iso(), "detail": "fake crash"})
        _write_json(run_dir / dsh_events.JOB_NAME, job)
        _report(db_path, req_id, run_id, "failed", "fake crash",
                failure_code="planner_failed")
        return True

    if mode == "needs_input":
        output = {
            "status": "needs_input", "plan_markdown": "# 草稿\n待定",
            "questions": ["需要确认目标版本范围"], "evidence": [],
        }
    elif mode == "invalid":
        out_path.write_text("not a json", encoding="utf-8")
        finalize("failed", 0, "invalid output", "failed", "invalid_output")
        _report(db_path, req_id, run_id, "failed",
                "规划输出不合规：fake invalid",
                failure_code="planner_output_invalid")
        return True
    elif mode == "fail":
        finalize("failed", 1, "fake failure", "failed", "crash")
        _report(db_path, req_id, run_id, "failed", "fake failure",
                failure_code="planner_failed")
        return True
    else:  # ready（默认）
        output = {
            "status": "ready",
            "plan_markdown": "# 实施计划\n\n1. 步骤一\n2. 步骤二\n",
            "questions": [], "evidence": ["manifest.json"],
        }

    out_path.write_text(json.dumps(output, ensure_ascii=False),
                        encoding="utf-8")
    log.emit("assistant_message", util.now_iso(),
             content=json.dumps(output, ensure_ascii=False))
    log.emit("turn_end", util.now_iso(), will_retry=False)
    parsed = parse_output(out_path)

    if parsed["status"] == "needs_input":
        finalize("finished", 0, "needs_input", "completed", "needs_input")
        _report(db_path, req_id, run_id, "needs_user",
                "规划需要补充材料：" + "；".join(parsed["questions"]),
                failure_code="reported",
                evidence={"questions": parsed["questions"]})
        return True

    generated_at = util.now_iso()
    plan_path = write_plan_file(context, manifest,
                                parsed["plan_markdown"], generated_at)
    req_payload = context.get("payload") or {}
    model_config = req_payload.get("model_config") or {}
    cli.run("publish-plan", {
        "task_id": task_id,
        "plan_path": str(plan_path),
        "plan_kind": task.get("plan_kind")
        or (manifest.get("task") or {}).get("plan_kind") or "standard",
        "source_sha256": manifest.get("source_sha256")
        or task.get("source_sha256") or "",
        "requirements_sha256": manifest.get("requirements_sha256")
        or task.get("requirements_sha256") or "",
        "generated_at": generated_at,
        "planner_model": model_config.get("planner_model") or "",
        "reasoning_effort": model_config.get("reasoning_effort") or "",
        "request_id": req_id,
        "run_id": run_id,
        "planner_input_sha256": req_payload.get("planner_input_sha256") or "",
    }, db_path)
    finalize("finished", 0, None, "completed", "plan_published")
    return True


# ------------------------------------------------------------ 入口

def _record_run(conn, **fields) -> None:
    """把本次运行合并进 settings ``executor_runs["plan"]``（executor-status
    单飞依据：App 侧 ExecutorScheduler 先查 running pid 再拉起）。"""
    runs = db.get_setting(conn, "executor_runs")
    if not isinstance(runs, dict):
        runs = {}
    entry = dict(runs.get("plan") or {})
    entry.update(fields)
    entry["stage"] = "plan"
    runs["plan"] = entry
    db.set_setting(conn, "executor_runs", runs)
    conn.commit()


def run_loop(db_path: Path, request_id: Optional[str], once: bool) -> int:
    conn = db.connect(db_path)
    try:
        log_dir = constants.app_support() / "executor-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / ("plan-%s.log" % util.now_iso()
                         .replace(":", "").replace("-", ""))
        _record_run(conn, pid=os.getpid(), started_at=util.now_iso(),
                    log=str(log), exit_code=None, finished_at=None)
    finally:
        conn.close()
    handled = 0
    rc = 0
    try:
        while True:
            try:
                progressed = run_one(db_path, request_id=request_id)
            except util.KanpilotError as exc:
                print(f"planner_runner: {exc.code}: {exc.message}",
                      file=sys.stderr)
                rc = 1
                break
            except Exception as exc:  # pragma: no cover - defensive
                print(f"planner_runner: internal: {exc}", file=sys.stderr)
                rc = 1
                break
            if not progressed:
                break
            handled += 1
            request_id = None  # 定点领取只生效一次
            if once:
                break
    finally:
        conn = db.connect(db_path)
        try:
            _record_run(conn, exit_code=rc, finished_at=util.now_iso())
        finally:
            conn.close()
    return rc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kanpilot.planner_runner")
    parser.add_argument("--request-id", dest="request_id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", dest="db_path")
    args = parser.parse_args(argv)
    db_path = Path(args.db_path).expanduser() if args.db_path \
        else constants.default_db_path()
    return run_loop(db_path, args.request_id, args.once)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
