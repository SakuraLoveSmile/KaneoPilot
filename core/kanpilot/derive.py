"""Read-side derivations: display_status, lost detection, allowed_actions,
and the Task/Request/Attempt/Event JSON views from docs/protocol.md §5.2."""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from . import bridge, constants, db, util


def request_is_lost(request: sqlite3.Row, now) -> bool:
    return (
        request["status"] == "claimed"
        and util.lease_lost(request["lease_expires_at"], now)
    )


def request_display_status(request: sqlite3.Row, now) -> str:
    if request_is_lost(request, now):
        return "执行端失联"
    return request["status"]


def lost_request_for_task(
    conn: sqlite3.Connection, task_id: str, now
) -> Optional[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status='claimed' "
        "ORDER BY claimed_at DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        if util.lease_lost(row["lease_expires_at"], now):
            return row
    return None


def active_request_for_task(
    conn: sqlite3.Connection, task_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status IN ('queued','claimed') "
        "ORDER BY CASE status WHEN 'claimed' THEN 0 ELSE 1 END, "
        "priority ASC, created_at ASC LIMIT 1",
        (task_id,),
    ).fetchone()


def queued_request_for_task(
    conn: sqlite3.Connection, task_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status='queued' "
        "ORDER BY priority ASC, created_at ASC LIMIT 1",
        (task_id,),
    ).fetchone()


def task_display_status(
    conn: sqlite3.Connection, task: sqlite3.Row, now
) -> str:
    status = task["status"]
    if status == "needs_user":
        return "需人工"
    if status == "needs_plan":
        return "待规划"
    if status == "planning":
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "规划中"
    if status == "ready":
        queued = conn.execute(
            "SELECT 1 FROM requests WHERE task_id=? AND status='queued' "
            "AND kind='dispatch' LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        return "已排队" if queued else "就绪"
    if status in constants.DISPATCH_MID_STATUSES:
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "派发中"
    if status == "running":
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "执行中"
    if status == "in_review":
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "评审中"
    if status == "done":
        return "已归档"
    return status


def request_view(request: sqlite3.Row, now) -> dict:
    return {
        "request_id": request["request_id"],
        "idempotency_key": request["idempotency_key"],
        "task_id": request["task_id"],
        "stage": request["stage"],
        "kind": request["kind"],
        "status": request["status"],
        "display_status": request_display_status(request, now),
        "priority": request["priority"],
        "not_before": request["not_before"],
        "requested_by": request["requested_by"],
        "run_id": request["run_id"],
        "claimed_by": request["claimed_by"],
        "claimed_at": request["claimed_at"],
        "heartbeat_at": request["heartbeat_at"],
        "lease_expires_at": request["lease_expires_at"],
        "result": util.jload(request["result_json"]),
        "attempt_no": request["attempt_no"],
        "created_at": request["created_at"],
        "updated_at": request["updated_at"],
    }


def task_summary(conn: sqlite3.Connection, task: sqlite3.Row, now) -> dict:
    active = active_request_for_task(conn, task["task_id"])
    queued = queued_request_for_task(conn, task["task_id"])
    next_claim_at = None
    if queued is not None and queued["not_before"] is not None:
        next_claim_at = queued["not_before"]
    active_view = None
    if active is not None:
        active_view = {
            "request_id": active["request_id"],
            "kind": active["kind"],
            "status": active["status"],
            "display_status": request_display_status(active, now),
            "priority": active["priority"],
            "claimed_by": active["claimed_by"],
            "heartbeat_at": active["heartbeat_at"],
            "lease_expires_at": active["lease_expires_at"],
            "not_before": active["not_before"],
        }
    blocked_reason = None
    if task["status"] == "needs_user":
        blocked_reason = task["blocked_summary"] or task["failure_reason"]
    return {
        "task_id": task["task_id"],
        "project_id": task["project_id"],
        "project_name": task["project_name"],
        "title": task["title"],
        "stage": task["stage"],
        "status": task["status"],
        "display_status": task_display_status(conn, task, now),
        "last_progress": task["last_progress"],
        "blocked_reason": blocked_reason,
        "next_claim_at": next_claim_at,
        "active_request": active_view,
        "kaneo_status": task["kaneo_status"],
        "kaneo_checked_at": task["kaneo_checked_at"],
        "priority": active["priority"] if active is not None else task["priority"],
        "plan_path": task["plan_path"],
        "updated_at": task["updated_at"],
    }


def attempt_view(row: sqlite3.Row) -> dict:
    return {
        "request_id": row["request_id"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "kind": row["kind"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "outcome": row["outcome"],
        "send_state": row["send_state"],
        "note": row["note"],
        "evidence": util.jload(row["evidence_json"]),
    }


def allowed_actions(conn: sqlite3.Connection, task: sqlite3.Row, now) -> list:
    """Resolve-action availability per protocol.md §4.10 plus UI conveniences."""
    status = task["status"]
    send_state = task["send_state"]
    actions = ["recheck"]
    if status in constants.ENQUEUE_INFER:
        actions.append("enqueue")
    if queued_request_for_task(conn, task["task_id"]) is not None:
        actions.append("set-priority")
    if lost_request_for_task(conn, task["task_id"], now) is not None:
        actions.append("release_lost")
    if status == "needs_user":
        if send_state in (None, "not_sent"):
            actions.append("retry")
        if send_state == "unknown":
            actions.extend(["restore", "accept_duplicate", "mark_sent"])
        actions.extend(["replan", "dismiss"])
    if status in ("in_review", "needs_user", "running"):
        actions.append("mark_done")
    return actions


def task_detail(conn: sqlite3.Connection, task: sqlite3.Row, now) -> dict:
    detail = task_summary(conn, task, now)
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id=? ORDER BY id", (task["task_id"],)
    ).fetchall()
    detail.update(
        {
            "repository": task["repository"],
            "plan_sha256": task["plan_sha256"],
            "plan_kind": task["plan_kind"],
            "requirements_sha256": task["requirements_sha256"],
            "source_sha256": task["source_sha256"],
            "source_path": task["source_path"],
            "review_report_path": task["review_report_path"],
            "review_report_sha256": task["review_report_sha256"],
            "review_key": task["review_key"],
            "review_source_task_id": task["review_source_task_id"],
            "fix_task_id": task["fix_task_id"],
            "session_id": task["session_id"],
            "session_title": task["session_title"],
            "session_marker": task["session_marker"],
            "send_state": task["send_state"],
            "sent": task["sent"],
            "run_id": task["run_id"],
            "failure_code": task["failure_code"],
            "failure_reason": task["failure_reason"],
            "failure_stage": task["failure_stage"],
            "manual_recovery_required": task["manual_recovery_required"],
            "allowed_actions": allowed_actions(conn, task, now),
            "attempts": [attempt_view(row) for row in attempts],
            "links": task_links(task),
        }
    )
    return detail


def task_links(task: sqlite3.Row) -> dict:
    """Best-effort external links; kaneo_task_url derived from credentials."""
    links = {"kaneo_task_url": None, "plan_file": None, "report_file": None}
    if task["plan_path"]:
        links["plan_file"] = task["plan_path"]
    if task["review_report_path"]:
        links["report_file"] = task["review_report_path"]
    try:
        base_url, _token = bridge.load_credentials()
    except Exception:
        base_url = None
    if base_url:
        web = base_url.rstrip("/")
        if web.endswith("/api"):
            web = web[: -len("/api")]
        links["kaneo_task_url"] = f"{web}/task/{task['task_id']}"
    return links
