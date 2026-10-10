"""Engine CLI command handlers (protocol §5.27).

Registered lazily from ``cli.COMMANDS`` via ``_automation_command`` so an
incomplete engine never breaks CLI import (the rehearsal "engine gate" relies
on calling → clean error, never a load-time ImportError).

- ``attention-list`` — read-only aggregation of the needs-you inbox (§5.27.3).
- ``engine-run`` / ``engine-status`` — the long-lived reconciler + lease
  health (§5.27.1 / §5.27.2); wired once ``loop``/``health`` land.
"""
from __future__ import annotations

import sys

from . import attention, health
from .. import db, derive, util


def _task_evidence(conn, task_id, since):
    """Structured evidence (§5.27.3): triggering event + failed request (redacted).

    Never includes tokens, connection-file contents, or Plus transcripts.
    """
    evidence = []
    event = conn.execute(
        "SELECT id, at, kind FROM events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    if event is not None:
        evidence.append({"kind": "event", "id": event["id"], "at": event["at"],
                         "detail": util.redact(event["kind"])})
    request = conn.execute(
        "SELECT request_id, kind, status, updated_at FROM requests "
        "WHERE task_id=? AND status='failed' ORDER BY updated_at DESC LIMIT 1",
        (task_id,)).fetchone()
    if request is not None:
        evidence.append({"kind": "request", "id": request["request_id"],
                         "at": request["updated_at"],
                         "detail": util.redact(f"{request['kind']} {request['status']}")})
    return evidence

def _needs_user_views(conn, now, *, include_historical):
    """Build attention task-views from ``needs_user`` rows (read-only).

    Archived/trashed blocks are only surfaced when ``include_historical`` is
    set (protocol §5.27.3); purged stays audit-only and is never listed.
    """
    states = ("active", "archived", "trashed") if include_historical else ("active",)
    placeholders = ",".join("?" * len(states))
    query = (
        "SELECT * FROM tasks WHERE status='needs_user' "
        f"AND save_state IN ({placeholders}) ORDER BY updated_at DESC"
    )
    views = []
    for row in conn.execute(query, states).fetchall():
        views.append({
            "task_id": row["task_id"],
            "project_name": row["project_name"],
            "title": row["title"],
            "status": row["status"],
            "save_state": db.task_save_state(row),
            "failure_code": row["failure_code"],
            "failure_stage": row["failure_stage"],
            "failure_reason": row["failure_reason"],
            "blocked_summary": row["blocked_summary"],
            "blocked_at": row["blocked_at"],
            "updated_at": row["updated_at"],
            # Authoritative action list (single source; attention never
            # recomputes it).
            "allowed_actions": derive.allowed_actions(conn, row, now),
            "evidence": _task_evidence(conn, row["task_id"],
                                       row["blocked_at"] or row["updated_at"]),
        })
    return views


def attention_items(conn, now, now_iso, *, save_state: str = "active",
                    include_historical: bool = False) -> list:
    """Single-source attention aggregation (§5.27.3 / A15).

    The one function behind both the ``attention-list`` command and the board
    ``attention`` section (``overview`` / ``list-tasks``), so every consumer
    reads the same inbox (A15 "same command, consistent read").
    """
    if save_state not in ("active", "archived", "trashed", "all"):
        raise util.bad_request("invalid save_state")
    historical = bool(include_historical) or save_state in ("archived", "trashed", "all")
    views = _needs_user_views(conn, now, include_historical=historical)
    return attention.collect(views, now_iso, include_historical=historical)


def cmd_attention_list(ctx, payload: dict) -> dict:
    """``attention-list {save_state?, include_historical?}`` (§5.27.3, read-only)."""
    save_filter = payload.get("save_state") or "active"
    if save_filter not in ("active", "archived", "trashed", "all"):
        raise util.bad_request("invalid save_state")
    items = attention_items(ctx.conn, ctx.now, ctx.now_iso, save_state=save_filter,
                            include_historical=bool(payload.get("include_historical")))
    return {"items": items}


def cmd_engine_status(ctx, payload: dict) -> dict:
    """``engine status {instance_id?}`` (§5.27.2, read-only)."""
    return health.engine_status(ctx.conn, ctx.now, instance_id=payload.get("instance_id"))


def cmd_engine_run(ctx, payload: dict) -> dict:
    """``engine run {stage?, executor_id?, lease_seconds?, mode?, force?}`` (§5.27.1, A12).

    Long-lived foreground process: single-instance lease + heartbeat +
    exponential backoff until SIGTERM/SIGINT. ``mode`` is the **process
    default** engine_mode (缺省 legacy, the strangler-safe default); a project
    with an explicit ``engine_mode`` overrides it, and ``force=true`` makes every
    project use ``mode`` (drills/debug).
    """
    import signal
    import threading

    from .loop import Reconciler

    mode = payload.get("mode") or "legacy"
    if mode not in ("legacy", "shadow", "live"):
        raise util.bad_request("mode 必须为 legacy|shadow|live")
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()

    installed = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            installed.append((signum, signal.signal(signum, _stop)))
        except (ValueError, OSError):
            pass
    reconciler = Reconciler(
        ctx.db_path, mode=mode,
        executor_id=payload.get("executor_id") or "kaneo-engine",
        enable_intake=bool(payload.get("intake", True)),
        enable_spawn=bool(payload.get("spawn", True)),
        force_mode=bool(payload.get("force", False)),
        log=lambda message: sys.stderr.write(message + "\n"))
    try:
        return reconciler.run(stage=payload.get("stage"),
                              lease_seconds=payload.get("lease_seconds"),
                              stop_event=stop)
    finally:
        for signum, previous in installed:
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError):
                pass


def cmd_engine_verdict(ctx, payload: dict) -> dict:
    """``engine-verdict {task_id, expected_execution_run_id, expected_plan_sha256, operation_id?}``

    Core-owned verdict (§5.27 / A10): non-empty effective diff (``.pi/**``
    excluded) AND every plan-declared verify command passing in a login shell
    with the project ``verify_env``. The receipt is recorded durably in
    ``extra.engine_verdict`` (auditable via get-task); it does not overwrite the
    interactive ``verification_checks`` receipt, whose output-file + binding
    freeze validation does not map onto this synchronous engine run.
    """
    from . import verdict

    task = db.require_task(ctx.conn, payload["task_id"])
    if (not db.task_is_active(task)
            or task["run_id"] != payload["expected_execution_run_id"]
            or task["plan_sha256"] != payload["expected_plan_sha256"]):
        raise util.KanpilotError("conflict", "当前任务、执行轮次或计划已变化")
    result = verdict.evaluate_task(ctx.conn, task)
    operation_id = payload.get("operation_id") or util.new_uuid()
    receipt = {
        "operation_id": operation_id,
        "execution_run_id": task["run_id"],
        "plan_sha256": task["plan_sha256"],
        "verification_ok": result["verification_ok"],
        "empty_diff": result["empty_diff"],
        "environment_failure": result["environment_failure"],
        "effective_changes": result["effective_changes"],
        "checks": result["checks"],
        "detail": result["detail"],
        "completed_at": ctx.now_iso,
    }
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task["task_id"])
        db.set_extra(ctx.conn, current, ctx.now_iso, engine_verdict=receipt)
    return {"ok": True, "operation_id": operation_id,
            "verification_ok": result["verification_ok"],
            "empty_diff": result["empty_diff"],
            "effective_changes": result["effective_changes"],
            "checks": result["checks"], "detail": result["detail"],
            "environment_failure": result["environment_failure"]}


# Backends whose sessions are deprecated (protocol §5.27.5): return unavailable.
_OLD_BACKENDS = {"dsh", "headless", "atif"}


def cmd_session_view(ctx, payload: dict) -> dict:
    """``session-view {task_id?, run_id?, status_only?}`` (§5.27.5, read-only).

    Minimal status overview of a task's execution round. ``status_only=true``
    (default intent) never reads messages — only the registered round/task
    state. Deprecated backends (dsh/headless/ATIF) return
    ``connection.state="unavailable"`` + detail「旧后端会话已弃用」. Live Plus
    connectivity is probed by ``session-control-status`` / ``doctor --live``,
    not here.
    """
    from .. import derive, round_store

    task_id = payload.get("task_id")
    run_id = payload.get("run_id")
    if not task_id and run_id:
        found = ctx.conn.execute(
            "SELECT task_id FROM tasks WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
        task_id = found["task_id"] if found else None
    if not task_id:
        raise util.bad_request("task_id 或 run_id 至少提供一个")
    task = db.get_task(ctx.conn, task_id)
    if task is None:
        raise util.KanpilotError("not_found", "任务不存在")
    run_id = run_id or task["run_id"]
    if not run_id:
        raise util.bad_request("该任务无当前执行轮次，且未提供 run_id")
    row = round_store.get_round(ctx.conn, task_id, run_id)
    backend = row["backend"] if row else None
    run_status = row["run_status"] if row else "unknown"
    session_id = row["session_id"] if row else None
    save_state = row["save_state"] if row else None
    sread = ctx.conn.execute(
        "SELECT state, last_seq FROM session_reads WHERE session_key=?",
        (f"{task_id}/{run_id}",)).fetchone()
    if isinstance(backend, str) and backend.lower() in _OLD_BACKENDS:
        connection = {"state": "unavailable", "detail": "旧后端会话已弃用"}
        state = "unavailable"
    elif not session_id:
        connection = {"state": "degraded", "detail": "会话尚未建立"}
        state = sread["state"] if sread else "pending"
    else:
        connection = {"state": "ok",
                      "detail": f"后端 {backend}，运行状态 {run_status}"}
        state = sread["state"] if sread else "associated"
    session = {
        "task_id": task_id, "run_id": run_id, "backend": backend,
        "run_status": run_status, "session_id": session_id, "turn_id": None,
        "save_state": save_state, "state": state, "connection": connection,
        "allowed_actions": derive.allowed_actions(ctx.conn, task, ctx.now),
    }
    if task["status"] == "needs_user":
        session["blocked_reason"] = task["blocked_summary"] or task["failure_reason"]
    if not payload.get("status_only") and sread is not None:
        session["cursor"] = sread["last_seq"]
    return {"session": session}


def cmd_set_project_engine_mode(ctx, payload: dict) -> dict:
    """``set-project-engine-mode {project_id, engine_mode, expected_revision?}`` (A12).

    Persist a project's ``engine_mode`` (legacy|shadow|live, default legacy) at
    the **mapping entry level** — ``project_mappings.items[pid].engine_mode`` —
    *not* inside ``automation``. ``set-project-automation`` rebuilds the
    ``automation`` sub-dict from a fixed key whitelist (``validate_automation``),
    so anything stored inside it is silently dropped on the next automation
    write. Entry-level keys survive both ``set-project-mapping`` (it spreads
    ``existing_entry``) and ``set-project-automation`` (it only replaces the
    ``automation`` key). No new column, no schema bump; reversible — old rows
    lack the key and fall back to the process default.
    """
    from .. import project_config

    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise util.bad_request("project_id 必须为非空字符串")
    project_id = project_id.strip()
    mode = payload.get("engine_mode", "legacy")
    if mode not in ("legacy", "shadow", "live"):
        raise util.bad_request("engine_mode 必须为 legacy|shadow|live")
    expected = payload.get("expected_revision")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
        raise util.bad_request("expected_revision 须为非负整数")
    with db.write_txn(ctx.conn):
        revision, workspace, items, _ = project_config.load_raw_mappings(ctx.conn)
        if project_id not in items:
            raise util.KanpilotError("not_found", "项目映射不存在")
        if expected is not None and expected != revision:
            raise util.KanpilotError("conflict", "project_mappings revision 不符")
        entry = dict(items[project_id])
        entry["engine_mode"] = mode
        entry["updated_at"] = ctx.now_iso
        items[project_id] = entry
        db.set_setting(ctx.conn, project_config.SETTINGS_KEY,
                       {"version": project_config.CURRENT_VERSION,
                        "revision": revision + 1,
                        "workspace_id": workspace, "items": items})
    return {"project_id": project_id, "engine_mode": mode,
            "revision": revision + 1}


def _validate_verify_env(raw) -> dict:
    """``verify_env`` must be a flat str→str dict with no credential-ish keys."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise util.bad_request("verify_env 必须为对象（字符串→字符串）")
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise util.bad_request("verify_env 键必须为非空字符串")
        if not isinstance(value, str):
            raise util.bad_request(f"verify_env[{key}] 必须为字符串")
        normalized = key.strip()
        if normalized.upper() in _VERIFY_ENV_FORBIDDEN:
            raise util.bad_request(
                f"verify_env 不得包含凭据类键：{normalized}"
                "（凭据走凭据文件，不进入 verify_env）")
        result[normalized] = value
    if len(result) > 64:
        raise util.bad_request("verify_env 最多 64 个键")
    return result


_VERIFY_ENV_FORBIDDEN = frozenset({
    "TOKEN", "ACCESS_TOKEN", "ACCESSTOKEN", "REFRESH_TOKEN", "API_KEY",
    "APIKEY", "SECRET", "PASSWORD", "CREDENTIALS", "AUTHORIZATION",
    "KANEO_TOKEN", "KANEO_ACCESS_TOKEN", "PI_DESKTOP_TOKEN",
})


def cmd_set_project_verify_env(ctx, payload: dict) -> dict:
    """``set-project-verify_env {project_id, verify_env, expected_revision?}``.

    Persist the project ``verify_env`` at the mapping entry level
    (``project_mappings.items[pid].verify_env``) for the same reason as
    ``engine_mode``: ``validate_automation`` drops unknown keys inside
    ``automation``. Read by ``engine.verdict.project_verify_env``.
    """
    from .. import project_config

    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise util.bad_request("project_id 必须为非空字符串")
    project_id = project_id.strip()
    value = _validate_verify_env(payload.get("verify_env"))
    expected = payload.get("expected_revision")
    if expected is not None and (isinstance(expected, bool)
                                 or not isinstance(expected, int) or expected < 0):
        raise util.bad_request("expected_revision 须为非负整数")
    with db.write_txn(ctx.conn):
        revision, workspace, items, _ = project_config.load_raw_mappings(ctx.conn)
        if project_id not in items:
            raise util.KanpilotError("not_found", "项目映射不存在")
        if expected is not None and expected != revision:
            raise util.KanpilotError("conflict", "project_mappings revision 不符")
        entry = dict(items[project_id])
        entry["verify_env"] = value
        entry["updated_at"] = ctx.now_iso
        items[project_id] = entry
        db.set_setting(ctx.conn, project_config.SETTINGS_KEY,
                       {"version": project_config.CURRENT_VERSION,
                        "revision": revision + 1,
                        "workspace_id": workspace, "items": items})
    return {"project_id": project_id, "verify_env": value,
            "revision": revision + 1}
