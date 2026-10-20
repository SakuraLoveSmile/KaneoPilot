"""Read-only CLI planning, preserving the existing planner publish contract."""
from __future__ import annotations

from . import plan_questions

import os
import time
from pathlib import Path

from . import (automation_policy, claude_cli, cli, codex_planner, constants,
               core, db, dsh_events, planner_runner, util, task_workspace)


def budget_for(conn, task) -> float:
    if not core.is_automation_managed(task):
        return 5.0
    policy = automation_policy.policy_for(conn, task["project_id"]) or constants.AUTOMATION_DEFAULTS
    budget = core.automation_budget(task, policy, util.now_iso())
    if not budget["ok"]:
        raise util.KanpilotError("auto_budget_exceeded", "本段自动化预算已用尽")
    remaining = budget["remaining_cost_usd"]
    return max(0.5, min(5.0, remaining if remaining is not None else 5.0))


def run_one(db_path, request_id: str, run_id: str, *, context: dict,
            task: dict, task_id: str, run_dir: Path, timeout,
            heartbeat_every, backend="claude") -> bool:
    lifecycle = {}
    try:
        return _run_one(db_path, request_id, run_id, context=context, task=task,
                        task_id=task_id, run_dir=run_dir, timeout=timeout,
                        heartbeat_every=heartbeat_every, lifecycle=lifecycle,
                        backend=backend)
    finally:
        job = lifecycle.get("job")
        if job and job.get("pool_lease"):
            task_workspace.release_pool(db_path, job, run_dir)
            planner_runner._write_json(run_dir / dsh_events.JOB_NAME, job)


def _run_one(db_path, request_id, run_id, *, context, task, task_id, run_dir,
             timeout, heartbeat_every, lifecycle, backend="claude"):
    if backend == "qoder":
        from . import qoder_cli as adapter
        title = "Qoder"
    else:
        adapter = claude_cli
        title = "Claude"
    payload = context.get("payload") or {}
    manifest = payload.get("manifest") or {}
    config = payload.get("model_config") or {}
    log = planner_runner._EventLog(run_dir / dsh_events.EVENT_LOG_NAME,
                                   redact=util.redact)
    job = {"task_id": task_id, "request_id": request_id, "run_id": run_id, "backend": backend,
           "config": config, "input_id": context.get("input_id"),
           "planner_input_sha256": payload.get("planner_input_sha256"),
           "planner_method": payload.get("planner_method") or "builtin",
           "session_id": None, "session_title": f"{title} · 规划",
           "pid": os.getpid(), "status": "running", "started_at": util.now_iso(),
           "ended_at": None, "exit_code": None, "detail": None, "send_state": "not_sent"}
    lifecycle["job"] = job
    planner_runner._write_json(run_dir / dsh_events.JOB_NAME, job)

    def finish(status, code, detail, reason):
        job.update(status=status, exit_code=code, detail=detail, ended_at=util.now_iso())
        planner_runner._write_json(run_dir / dsh_events.JOB_NAME, job)
        log.emit("terminal", util.now_iso(),
                 outcome="completed" if status == "finished" else "failed", reason=reason)
        conn = db.connect(db_path)
        try:
            planner_runner._mark_round_finished(conn, task_id, run_id, status, util.now_iso())
        finally:
            conn.close()

    def fail(detail, reason="planner_failed", code=1):
        finish("failed", code, detail, reason)
        planner_runner._report(db_path, request_id, run_id, "failed", detail,
                               failure_code=reason)
        return True

    if job["planner_method"] != "builtin":
        return fail(f"{title} 规划只支持 builtin；spec_plan 是 Codex 插件运行方式")
    if not config.get("planner_model") or not config.get("reasoning_effort"):
        return fail(f"{title} 请求缺少固化的模型配置")
    if backend == "qoder":
        from . import planner_config
        error = planner_config.validate_qoder_block({
            "model": config["planner_model"], "reasoning_effort": config["reasoning_effort"]})
        if error:
            return fail(error)
        capability = adapter.capabilities()
        if not capability["available"]:
            return fail(capability["detail"])
    conn = db.connect(db_path)
    try:
        raw_task = db.require_task(conn, task_id)
        budget = budget_for(conn, raw_task) if backend == "claude" else None
    except util.KanpilotError as exc:
        return fail(exc.message, exc.code)
    finally:
        conn.close()
    input_dir = Path(context["input_dir"]) if context.get("input_dir") else None
    conn = db.connect(db_path)
    try:
        cwd = task_workspace.prepare_plan_pool(conn, db_path, raw_task, payload, job, run_dir / dsh_events.JOB_NAME)
    except (util.KanpilotError, OSError) as exc:
        if isinstance(exc, util.KanpilotError) and exc.code == "worktree_pool_waiting":
            finish("finished", 0, exc.message, exc.code)
            return True
        return fail(exc.message if isinstance(exc, util.KanpilotError) else str(exc), "plan_worktree_failed")
    finally:
        conn.close()
    prompt_task = dict(task, fix_mode=payload.get("fix_mode"), plan_cwd=str(cwd))
    prompt = codex_planner.build_prompt(prompt_task, manifest, input_dir)
    job["worktree_path"] = str(cwd)
    if backend == "qoder":
        try:
            argv, launch_env = adapter.prepare_launch(model=config["planner_model"],
                reasoning_effort=config["reasoning_effort"],
                add_dirs=(input_dir,) if input_dir else ())
        except util.KanpilotError as exc:
            return fail(exc.message, exc.code)
    else:
        argv = adapter.build_argv(model=config["planner_model"],
                                  effort=config["reasoning_effort"],
                                  schema=codex_planner.output_schema(), budget_usd=budget,
                                  add_dirs=(input_dir,) if input_dir else ())
    log.emit("user", util.now_iso(), content=prompt)
    next_heartbeat = time.monotonic() + heartbeat_every

    def on_event(event):
        nonlocal next_heartbeat
        if time.monotonic() >= next_heartbeat:
            planner_runner._heartbeat(db_path, request_id, run_id, "planning")
            task_workspace.heartbeat_pool(db_path, job)
            next_heartbeat = time.monotonic() + heartbeat_every
        if event is None:
            return
        parsed = adapter.map_event(event)
        if parsed:
            parsed = dict(parsed)
            kind, ts = parsed.pop("type"), parsed.pop("ts")
            if kind == "session":
                job["session_id"] = parsed.get("session_id")
                planner_runner._write_json(run_dir / dsh_events.JOB_NAME, job)
            log.emit(kind, ts, **parsed)

    result = {}
    try:
        job["send_state"] = "unknown"
        conn = db.connect(db_path)
        try:
            task_workspace.pool_owner(conn, job, state="running", pid=os.getpid())
        finally:
            conn.close()
        planner_runner._write_json(run_dir / dsh_events.JOB_NAME, job)
        result = adapter.run(argv, prompt=prompt, cwd=cwd,
                             env=launch_env if backend == "qoder" else adapter.child_env(), timeout=timeout,
                             log_path=run_dir / f"{backend}.jsonl", on_event=on_event,
                             **({"schema": codex_planner.output_schema()} if backend == "qoder" else {}))
        job["owner_stopped"] = True
    except util.KanpilotError as exc:
        job["owner_stopped"] = True
        finish("failed", None, exc.message, exc.code)
        if exc.code not in ("stale_run", "conflict", "not_found"):
            planner_runner._report(db_path, request_id, run_id, "failed", exc.message,
                                   failure_code="planner_failed")
        return True
    finally:
        try:
            cli.run("record-cost", {"task_id": task_id, "request_id": request_id,
                    "run_id": run_id, "stage": "plan", "source": backend,
                    "cost_usd": result.get("total_cost_usd")}, db_path)
        except util.KanpilotError as exc:
            if exc.code not in ("stale_run", "conflict", "not_found"):
                raise
    if result.get("is_error"):
        reason = "planner_timeout" if result.get("subtype") == "timeout" else "planner_failed"
        return fail(result.get("error") or f"{title} 规划失败", reason, result.get("exit_code"))
    out_path = run_dir / "last-message.txt"
    planner_runner._write_json(out_path, result.get("structured_output"))
    try:
        output = planner_runner.parse_output(out_path)
    except util.KanpilotError as exc:
        return fail(f"规划输出不合规：{exc.message}", "planner_output_invalid", 0)
    if output["status"] == "needs_input":
        draft = run_dir / "plan-draft.md"
        draft.write_text(output["plan_markdown"], encoding="utf-8")
        finish("finished", 0, "needs_input", "needs_input")
        planner_runner._report(db_path, request_id, run_id, "needs_user",
             "规划需要补充材料：" + plan_questions.summary(output["questions"]),
             failure_code="reported", evidence={"questions": output["questions"], "plan_markdown": output["plan_markdown"],
             "draft_path": str(draft), "evidence": output["evidence"]})
        return True
    generated_at = util.now_iso()
    kind = task.get("plan_kind") or (manifest.get("task") or {}).get("plan_kind") or "standard"
    try:
        plan_path = planner_runner.write_plan_file(context, manifest,
             output["plan_markdown"], generated_at, task_row=raw_task,
             model_config=config)
        job["plan_path"] = str(plan_path)
        cli.run("publish-plan", {"task_id": task_id, "plan_path": str(plan_path),
            "plan_kind": kind, "source_sha256": manifest.get("source_sha256") or task.get("source_sha256") or "",
            "requirements_sha256": manifest.get("requirements_sha256") or task.get("requirements_sha256") or "",
            "generated_at": generated_at, "planner_model": config["planner_model"],
            "reasoning_effort": config["reasoning_effort"], "request_id": request_id,
            "run_id": run_id, "planner_input_sha256": payload.get("planner_input_sha256") or ""}, db_path)
    except (util.KanpilotError, OSError) as exc:
        return fail(f"计划发布失败：{exc}")
    finish("finished", 0, None, "plan_published")
    return True
