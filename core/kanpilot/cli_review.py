"""Read-only Claude/Codex Review with deterministic shared settlement."""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path

from . import (claude_cli, claude_planner, codex_planner, constants, core, db,
               pi_desktop_controller as control, pi_desktop_review as review,
               review_config, review_settle, util, task_workspace, worktree_pool)

STAGE = "review"


def launcher_argv(backend, config, directory, snapshot, materials, *, budget=5, provider=None):
    if backend == "claude":
        return claude_cli.build_argv(model=config["model"], effort=config["effort"],
                                    schema=review_settle.RESULT_SCHEMA, budget_usd=budget,
                                    add_dirs=[materials])
    schema = directory / "output-schema.json"
    control.write_json(schema, review_settle.RESULT_SCHEMA)
    return codex_planner.launcher_argv(
        {"plan_cwd": str(snapshot), "model_config": {
            "planner_model": config["model"], "reasoning_effort": config["reasoning_effort"]},
         "planner_method": "builtin"},
        schema, directory / "last-message.txt", [], provider=provider)


def run_codex(argv, *, prompt, cwd, env, timeout, log_path, out_path, on_event=None):
    """Stream events while renewing the lease, including silent process intervals."""
    deadline = time.monotonic() + timeout
    result = {"exit_code": None, "is_error": True, "structured_output": None,
              "total_cost_usd": None, "error": None}
    secrets = [v for k, v in env.items() if isinstance(v, str) and len(v) >= 8
               and any(marker in k.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))]
    proc = None
    buffer = bytearray()
    failed = False
    def consume(raw, log):
        nonlocal failed
        text = util.redact(raw.decode("utf-8", errors="replace"), secrets)
        log.write(text.rstrip() + "\n")
        log.flush()
        try:
            event = json.loads(text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") in ("turn.failed", "error"):
            failed = True
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") in ("error", "turn_failed") or item.get("error") is not None:
                failed = True
        if on_event:
            on_event(event)
    try:
        with Path(log_path).open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, cwd=str(cwd), env=env,
                                    start_new_session=True, bufsize=0)
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            while True:
                if on_event:
                    on_event(None)
                if time.monotonic() >= deadline:
                    result["error"] = "Codex Review 超时"
                    claude_cli._terminate(proc)
                    break
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.25)
                if not ready:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if buffer:
                        consume(buffer, log)
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer[:] = tail
                    consume(line, log)
            result["exit_code"] = proc.wait(timeout=5)
        if result["error"] is None and not failed and result["exit_code"] == 0:
            try:
                result["structured_output"] = json.loads(Path(out_path).read_text(encoding="utf-8"))
                result["is_error"] = False
            except (OSError, ValueError) as exc:
                result["error"] = f"Codex Review 缺少有效最终 JSON：{exc}"
        elif result["error"] is None:
            result["error"] = f"Codex Review 失败（退出码 {result['exit_code']}）"
        return result
    finally:
        if proc:
            if proc.poll() is None:
                claude_cli._terminate(proc)
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()


def _persisted(conn):
    entry = control.stored_setting(conn, "executor_runs").get(STAGE) or {}
    if entry.get("backend") not in ("claude", "codex") or not entry.get("invocation_id"):
        return None
    directory = control.invocation_dir(STAGE, entry["invocation_id"])
    try:
        job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (job, directory) if isinstance(job, dict) and job.get("stage") == STAGE else None


def run_stage(stage, *, conn, db_path, timeout, log_path, record_run, resume=False,
              request_id=None, config_override=None):
    if stage != STAGE:
        return 2
    pending = _persisted(conn)
    if resume:
        if pending is None:
            return 1
        job, directory = pending
        if job.get("status") != "finished":
            review.validate_job_pool(job)
        log = control.EventLog(directory / "events.jsonl")
        def record(**fields):
            record_run(**dict(fields, backend=job["backend"]))
        if job.get("status") == "finished":
            return int(job.get("exit_code") or 0)
        # A crashed CLI run is never resent. Only an already saved result settles.
        if job.get("conclusion"):
            return review._settle(db_path=db_path, job=job, job_path=directory / "job.json",
                                  log=log, record_run=record)
        return review._blocked_no_pass(db_path=db_path, job=job, job_path=directory / "job.json",
                                       log=log, record_run=record, detail="CLI Review 中断且无已保存结论（不重发）")
    if pending and pending[0].get("status") != "finished":
        request = review._request_state(db_path, pending[0].get("request_id"))
        if request and request.get("status") == "claimed" and review._lease_valid(request):
            return 1
    if config_override is None:
        from . import cmd_exec
        candidate = cmd_exec._select_candidate(conn, STAGE, request_id, util.now_dt())
        if candidate is not None:
            request_id = candidate["request_id"]
            config_override = review_config.for_request(conn, db.request_payload(candidate))
    config = (review_config.snapshot(conn, config_override=config_override)
              if config_override is not None else review_config.snapshot(conn))
    backend = config.get("backend")
    if backend not in ("claude", "codex"):
        return 2
    invocation = util.new_uuid()
    directory = control.invocation_dir(STAGE, invocation)
    job_path = directory / "job.json"
    log = control.EventLog(directory / "events.jsonl")
    job = review._new_job(config, invocation, directory, Path(log_path))
    job["backend"] = backend
    def record(**fields):
        record_run(**dict(fields, backend=backend))
    def save():
        control.write_json(job_path, job)
        control.record_run_entry(record, STAGE, job)
    def blocked(detail):
        return review._blocked_no_pass(db_path=db_path, job=job, job_path=job_path,
                                       log=log, record_run=record, detail=detail)
    save()
    claim = review.core_call(db_path, "claim", {"stage": STAGE, "executor": review.EXECUTOR_ID,
                             **({"request_id": request_id} if request_id else {})})
    if not claim.get("claimed"):
        review._close(job=job, job_path=job_path, log=log, record_run=record,
                      status="finished", exit_code=0, detail=claim.get("reason"), reason="no_request")
        return 0
    context = claim.get("context") or {}
    if isinstance((context.get("payload") or {}).get("review_config"), dict):
        config = review_config.snapshot(conn, config_override=review_config.for_request(conn, context["payload"]))
        if config.get("backend") != backend:
            raise util.KanpilotError("conflict", "Review request backend differs from selected runner")
        job["config"] = config
    job.update(request_id=claim["request_id"], run_id=claim["run_id"],
               task_id=context.get("task_id") or (context.get("task") or {}).get("task_id"))
    save()
    facts = review._facts(db_path, job["task_id"], job["request_id"])
    problem = review._identity_problem(facts, job)
    if problem:
        return blocked(f"review 身份核对失败：{problem}")
    task = facts["task"]
    job["execution_run_id"] = task["run_id"]
    worktree = Path(task["execution_round"]["worktree_path"])
    job["execution_worktree"] = str(worktree)
    try:
        frozen_snapshot = review.review_snapshot.freeze(worktree) if not core.requires_branch_review(task) else None
        snapshot = review.prepare_pooled_worktree(conn, db_path, job, task, worktree, frozen_snapshot)
        save()
    except (util.KanpilotError, OSError) as exc:
        if isinstance(exc, util.KanpilotError) and exc.code == "worktree_pool_waiting":
            review._close(job=job, job_path=job_path, log=log, record_run=record,
                          status="finished", exit_code=0, detail=exc.message, reason=exc.code)
            return 0
        return blocked(exc.message if isinstance(exc, util.KanpilotError) else str(exc))
    try:
        materials = review.collect_materials(db_path=db_path, conn=conn, materials=directory / "materials",
                                             task=task, execution_worktree=worktree,
                                             execution_run_id=job["execution_run_id"], snapshot=frozen_snapshot)
        if frozen_snapshot is not None:
            review.review_snapshot.verify_root(snapshot, frozen_snapshot)
            review._freeze_manual_job(job, frozen_snapshot, materials)
        else:
            review._freeze_branch_job(job, task, materials)
        budget = claude_planner.budget_for(conn, review_settle.raw_task(task)) if backend == "claude" else None
        provider = codex_planner.codex_provider.resolve_provider(constants.codex_home()) if backend == "codex" else None
        if provider and not provider.auth_ready:
            return blocked(f"Codex 认证未就绪：{provider.auth_detail}")
        argv = launcher_argv(backend, config, directory, snapshot, directory / "materials", budget=budget, provider=provider)
    except (util.KanpilotError, OSError) as exc:
        return blocked(exc.message if isinstance(exc, util.KanpilotError) else str(exc))
    prompt = (Path(__file__).resolve().parents[1] / "executor_prompts" / "review_cli.md").read_text(encoding="utf-8")
    instruction = review.review_instruction(prompt, job=job, review_root=snapshot, materials=materials, facts=facts)
    job.update(review_root=str(snapshot), materials=materials["files"])
    try:
        if job.get("review_branch_snapshot"):
            review._guard_branch_job(job, task)
    except (util.KanpilotError, OSError) as exc:
        return blocked(exc.message if isinstance(exc, util.KanpilotError) else str(exc))
    job["send_state"] = "unknown"
    task_workspace.pool_owner(conn, job, state="running")
    save()
    interval = review._heartbeat_interval(facts.get("request"))
    next_heartbeat = time.monotonic() + interval
    def on_event(event):
        nonlocal next_heartbeat
        if time.monotonic() >= next_heartbeat:
            review.core_call(db_path, "heartbeat", {"request_id": job["request_id"], "run_id": job["run_id"]})
            if job.get("pool_lease"):
                worktree_pool.heartbeat(conn, **task_workspace.pool_identity(job["pool_lease"]))
            next_heartbeat = time.monotonic() + interval
        if event is None:
            return
        mapped = claude_cli.map_event(event) if backend == "claude" else codex_planner.map_event(event)
        if mapped:
            if isinstance(mapped, dict):
                fields = dict(mapped)
                etype, ts = fields.pop("type"), fields.pop("ts", util.now_iso())
                log.emit(etype, ts, **fields)
            else:
                etype, ts, fields = mapped
                log.emit(etype, ts, **fields)
    result = {}
    run_error = None
    try:
        if backend == "claude":
            result = claude_cli.run(argv, prompt=instruction, cwd=snapshot, env=claude_cli.child_env(),
                                    timeout=timeout, log_path=directory / "claude.jsonl", on_event=on_event)
        else:
            result = run_codex(argv, prompt=instruction, cwd=snapshot, env=codex_planner.child_env(provider),
                               timeout=timeout, log_path=directory / "codex.jsonl",
                               out_path=directory / "last-message.txt", on_event=on_event)
    except util.KanpilotError as exc:
        run_error = exc
    except (OSError, subprocess.SubprocessError) as exc:
        run_error = exc
    finally:
        try:
            review.core_call(db_path, "record-cost", {"task_id": job["task_id"], "request_id": job["request_id"],
                                                     "run_id": job["run_id"], "stage": STAGE, "source": backend,
                                                     "cost_usd": result.get("total_cost_usd") if backend == "claude" else None})
        except util.KanpilotError as exc:
            if exc.code not in ("stale_run", "not_found", "conflict"):
                raise
    # Normal returns and callback KanpilotError both pass the adapter finally
    # block, which terminates and waits its actual handle. A timeout/OSError
    # raised by that cleanup has no such proof and conservatively retains it.
    if run_error is None or isinstance(run_error, util.KanpilotError):
        job["owner_stopped"] = True
        save()
    if run_error is not None:
        if isinstance(run_error, util.KanpilotError) and run_error.code in ("stale_run", "not_found", "conflict"):
            return review._stop_no_write(job=job, job_path=job_path, log=log, record_run=record,
                                         detail=run_error.message)
        return blocked(f"CLI Review 无法运行：{run_error}")
    if result.get("is_error"):
        return blocked(result.get("error") or f"{backend} Review 失败")
    try:
        validated = review_settle.validate_result(result.get("structured_output"))
    except util.KanpilotError as exc:
        return blocked(f"review_result_invalid: {exc.message}")
    job.update(validated, send_state="sent", cost_usd=result.get("total_cost_usd") if backend == "claude" else None)
    save()
    return review._settle(db_path=db_path, job=job, job_path=job_path, log=log, record_run=record)
