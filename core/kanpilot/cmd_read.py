"""Read/diagnostic commands and the tick scheduler (protocol.md §5.1/§5.2/§4.8)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import timedelta
from typing import Optional

from . import constants, db, derive, remote, util


def tick_inline(conn: sqlite3.Connection, now) -> dict:
    """Business-cycle request top-up (protocol.md §4.8). Idempotent."""
    now_iso = util.to_iso(now)
    created: list = []
    skipped: list = []

    interval_hours = db.get_setting(
        conn, "plan_scan_interval_hours", constants.DEFAULT_PLAN_SCAN_INTERVAL_HOURS
    )
    monitor_interval = db.get_setting(
        conn, "monitor_interval_seconds",
        constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
    )

    # --- plan-scan: at most one active; re-scan once per interval ----------
    scan_key = "plan-scan:workspace"
    if db.active_request_for_key(conn, scan_key) is not None:
        skipped.append({"kind": "plan-scan", "reason": "active"})
    else:
        last_finished = conn.execute(
            "SELECT finished_at FROM requests WHERE kind='plan-scan' "
            "AND status IN ('succeeded','failed','cancelled') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        anchor = db.get_setting(conn, "last_plan_scan_at")
        candidates = [
            ts for ts in
            (last_finished["finished_at"] if last_finished else None, anchor)
            if ts
        ]
        last_at = max((util.parse_ts(ts) for ts in candidates), default=None)
        due = last_at is None or last_at + timedelta(hours=interval_hours) <= now
        if due:
            row, made = db.create_request(
                conn, now_iso, "plan-scan", scan_key,
                requested_by="scheduler",
            )
            if made:
                created.append(
                    {"request_id": row["request_id"], "kind": "plan-scan",
                     "task_id": None}
                )
        else:
            skipped.append({"kind": "plan-scan", "reason": "interval"})

    # --- monitor: every running task keeps a monitor cycle ------------------
    for task in conn.execute(
        "SELECT * FROM tasks WHERE status='running' ORDER BY task_id"
    ).fetchall():
        key = f"monitor:{task['task_id']}"
        if db.active_request_for_key(conn, key) is not None:
            skipped.append(
                {"kind": "monitor", "task_id": task["task_id"], "reason": "active"}
            )
            continue
        last = conn.execute(
            "SELECT finished_at FROM requests WHERE task_id=? AND "
            "kind='monitor' AND status IN ('succeeded','failed','cancelled') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        last_at = util.parse_ts(last["finished_at"]) if last else None
        if last_at is not None and last_at + timedelta(seconds=monitor_interval) > now:
            skipped.append(
                {"kind": "monitor", "task_id": task["task_id"], "reason": "interval"}
            )
            continue
        row, made = db.create_request(
            conn, now_iso, "monitor", key, task_id=task["task_id"],
            requested_by="scheduler",
        )
        if made:
            created.append(
                {"request_id": row["request_id"], "kind": "monitor",
                 "task_id": task["task_id"]}
            )

    # --- review: in_review tasks without a pending conclusion --------------
    for task in conn.execute(
        "SELECT * FROM tasks WHERE status='in_review' ORDER BY task_id"
    ).fetchall():
        key = f"review:{task['task_id']}"
        if db.active_request_for_key(conn, key) is not None:
            skipped.append(
                {"kind": "review", "task_id": task["task_id"], "reason": "active"}
            )
            continue
        if task["review_report_path"]:
            skipped.append(
                {"kind": "review", "task_id": task["task_id"],
                 "reason": "conclusion_pending"}
            )
            continue
        row, made = db.create_request(
            conn, now_iso, "review", key, task_id=task["task_id"],
            requested_by="scheduler",
        )
        if made:
            created.append(
                {"request_id": row["request_id"], "kind": "review",
                 "task_id": task["task_id"]}
            )

    db.set_setting(conn, "last_tick_at", now_iso)
    return {"created": created, "skipped": skipped}


def cmd_tick(ctx, payload: dict) -> dict:
    with db.write_txn(ctx.conn):
        return tick_inline(ctx.conn, ctx.now)


def cmd_overview(ctx, payload: dict) -> dict:
    conn = ctx.conn
    task_counts = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
    ).fetchall():
        task_counts[row["status"]] = row["n"]
    request_counts = {"queued": 0, "claimed": 0, "lost": 0}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM requests GROUP BY status"
    ).fetchall():
        request_counts[row["status"]] = row["n"]
    lost = 0
    for row in conn.execute(
        "SELECT lease_expires_at FROM requests WHERE status='claimed'"
    ).fetchall():
        if util.lease_lost(row["lease_expires_at"], ctx.now):
            lost += 1
    request_counts["lost"] = lost
    return {
        "paused": {
            "global": db.get_setting(conn, "paused_global", False),
            "stages": db.get_setting(conn, "paused_stages", []) or [],
        },
        "counts": {"tasks": task_counts, "requests": request_counts},
        "last_tick_at": db.get_setting(conn, "last_tick_at"),
        "schema_version": constants.SCHEMA_VERSION,
        "settings": {
            "lease_seconds": db.get_setting(
                conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS
            ),
            "monitor_interval_seconds": db.get_setting(
                conn, "monitor_interval_seconds",
                constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
            ),
            "plan_scan_interval_hours": db.get_setting(
                conn, "plan_scan_interval_hours",
                constants.DEFAULT_PLAN_SCAN_INTERVAL_HOURS,
            ),
            "plan_scan_anchor": db.get_setting(
                conn, "plan_scan_anchor", constants.DEFAULT_PLAN_SCAN_ANCHOR
            ),
            "migration": db.get_setting(conn, "migration"),
        },
        "paths": {
            "db": str(ctx.db_path),
            "plan_root": str(constants.plan_root()),
            "repos_root": str(constants.repos_root()),
            "app_support": str(constants.app_support()),
            "core": str(constants.core_root()),
        },
    }


def cmd_list_tasks(ctx, payload: dict) -> dict:
    clauses, params = [], []
    if payload.get("stage"):
        clauses.append("stage=?")
        params.append(payload["stage"])
    if payload.get("status"):
        clauses.append("status=?")
        params.append(payload["status"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = ctx.conn.execute(
        f"SELECT * FROM tasks{where} ORDER BY updated_at DESC", params
    ).fetchall()
    tasks = []
    for row in rows:
        if payload.get("needs_attention"):
            attention = row["status"] == "needs_user" or (
                derive.lost_request_for_task(ctx.conn, row["task_id"], ctx.now)
                is not None
            )
            if not attention:
                continue
        tasks.append(derive.task_summary(ctx.conn, row, ctx.now))
    return {"tasks": tasks}


def cmd_get_task(ctx, payload: dict) -> dict:
    task = db.require_task(ctx.conn, payload["task_id"])
    return {"task": derive.task_detail(ctx.conn, task, ctx.now)}


def cmd_list_requests(ctx, payload: dict) -> dict:
    status = payload.get("status")
    if status is not None and status not in (
        "queued", "claimed", "lost", "failed", "succeeded",
        "cancelled", "active",
    ):
        raise util.KanpilotError(
            "bad_request", f"invalid status filter {status}"
        )
    limit = payload.get("limit") or 200
    clauses, params = [], []
    if status and status not in ("active", "lost"):
        clauses.append("status=?")
        params.append(status)
    elif status == "active":
        clauses.append("status IN ('queued','claimed')")
    elif status == "lost":
        clauses.append("status='claimed'")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = ctx.conn.execute(
        f"SELECT * FROM requests{where} ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    views = []
    for row in rows:
        if status == "lost" and not util.lease_lost(
            row["lease_expires_at"], ctx.now
        ):
            continue
        views.append(derive.request_view(row, ctx.now))
    return {"requests": views}


def cmd_list_events(ctx, payload: dict) -> dict:
    limit = payload.get("limit") or 200
    if payload.get("task_id"):
        rows = ctx.conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (payload["task_id"], limit),
        ).fetchall()
    else:
        rows = ctx.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {
        "events": [
            {
                "id": row["id"],
                "at": row["at"],
                "task_id": row["task_id"],
                "request_id": row["request_id"],
                "kind": row["kind"],
                "actor": row["actor"],
                "detail": util.jload(row["detail_json"]),
            }
            for row in rows
        ]
    }


def cmd_doctor(ctx, payload: dict) -> dict:
    online = bool(payload.get("online"))
    checks = []

    checks.append({
        "name": "python",
        "ok": sys.version_info >= (3, 9),
        "detail": ".".join(str(part) for part in sys.version_info[:3]),
    })

    try:
        ctx.conn.execute("SELECT 1").fetchone()
        checks.append(
            {"name": "db_writable", "ok": True, "detail": str(ctx.db_path)}
        )
    except sqlite3.Error as exc:
        checks.append({"name": "db_writable", "ok": False, "detail": str(exc)})

    creds_ok, creds_detail = False, "missing"
    try:
        from . import bridge

        base_url, _token = bridge.load_credentials()
        creds_ok, creds_detail = True, f"present, baseUrl {base_url}"
    except Exception as exc:
        creds_detail = str(exc)
    checks.append({"name": "credentials", "ok": creds_ok, "detail": creds_detail})

    for name, path in (
        ("plan_root", constants.plan_root()),
        ("repos_root", constants.repos_root()),
        ("state_root", constants.app_support()),
    ):
        checks.append({"name": name, "ok": path.is_dir(), "detail": str(path)})

    if online:
        try:
            client = remote.get_client()
            client.request("/api/project?workspaceId=" + constants.WORKSPACE_ID)
            checks.append(
                {"name": "kaneo_ping", "ok": True, "detail": "HTTP 200",
                 "skipped": False}
            )
        except Exception as exc:
            checks.append(
                {"name": "kaneo_ping", "ok": False, "detail": str(exc),
                 "skipped": False}
            )
    else:
        checks.append(
            {"name": "kaneo_ping", "ok": True, "detail": "offline",
             "skipped": True}
        )
    return {"ok": all(item["ok"] for item in checks), "checks": checks}
