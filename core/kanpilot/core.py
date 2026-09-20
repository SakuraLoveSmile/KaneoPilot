"""Shared state-transition helpers used by multiple command modules."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import constants, db, util


def stage_for_status(status: str, fallback: Optional[str] = None) -> str:
    if status in ("needs_plan", "planning"):
        return "plan"
    if status in ("ready",) + constants.DISPATCH_MID_STATUSES:
        return "dispatch"
    if status == "running":
        return "monitor"
    if status == "in_review":
        return "review"
    if status == "done":
        return "done"
    return fallback or "plan"  # needs_user keeps its current stage


def transition_task(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    new_status: str,
    now: str,
    actor: Optional[str],
    detail: Any = None,
    keep_stage: bool = False,
    **extra_fields: Any,
) -> None:
    """Write task.status (+derived stage), emit status_change event."""
    old = task["status"]
    fields: dict[str, Any] = {"status": new_status}
    if not (keep_stage or new_status == "needs_user"):
        fields["stage"] = stage_for_status(new_status, task["stage"])
    fields.update(extra_fields)
    db.update_task(conn, task["task_id"], now, **fields)
    event_detail = {"from": old, "to": new_status, "actor": actor}
    if detail:
        event_detail.update(detail)
    db.add_event(
        conn, now, "status_change", task_id=task["task_id"], actor=actor,
        detail=event_detail,
    )


def clear_block_fields() -> dict:
    return {
        "failure_code": None, "failure_reason": None, "failure_stage": None,
        "blocked_at": None, "blocked_summary": None,
        "manual_recovery_required": 0,
    }


def block_task(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    now: str,
    reason: Optional[str],
    code: Optional[str],
    actor: Optional[str],
    stage: Optional[str] = None,
    **extra_fields: Any,
) -> None:
    """Move task to needs_user recording failure_* fields (keeps stage)."""
    fields = clear_block_fields()
    fields.update(
        {
            "failure_reason": reason,
            "failure_code": code,
            "failure_stage": stage if stage is not None else task["status"],
            "blocked_at": now,
            "blocked_summary": reason,
        }
    )
    fields.update(extra_fields)
    transition_task(
        conn, task, "needs_user", now, actor, detail={"reason": reason, "code": code},
        **fields,
    )


def guard_check(task: Optional[sqlite3.Row], kind: str) -> Optional[str]:
    """protocol.md §4.4 claim guard; returns failure reason or None."""
    allowed = constants.CLAIM_GUARDS[kind]
    if allowed is None:
        return None
    if task is None:
        return f"task missing for {kind} request"
    if task["status"] not in allowed:
        return (
            f"task {task['task_id']} status {task['status']} "
            f"not allowed for {kind} (need {'|'.join(sorted(allowed))})"
        )
    return None


def validate_evidence(
    evidence: Any,
    task: sqlite3.Row,
    expect_message: bool,
    plan_basename: str,
) -> None:
    """protocol.md §4.6 send-evidence checks (all fields from UI observation)."""
    if not isinstance(evidence, dict):
        raise util.KanpilotError("evidence_invalid", "evidence must be an object")
    def check(condition: bool, message: str) -> None:
        if not condition:
            raise util.KanpilotError("evidence_invalid", message)
    check(
        evidence.get("session_marker") == task["session_marker"],
        "session_marker not verified",
    )
    check(
        evidence.get("repository") == task["repository"],
        "repository not verified",
    )
    check(
        evidence.get("mode") == "Agent"
        and evidence.get("model") == "SWE-2 Max"
        and evidence.get("execution_mode") == "Local",
        "Agent/Local/model not verified",
    )
    check(
        evidence.get("attachment_name") == plan_basename
        and evidence.get("attachment_visible") is True,
        "attachment not verified",
    )
    check(evidence.get("instruction_visible") is True, "instruction not verified")
    if expect_message:
        check(
            evidence.get("message_visible") is True,
            "sent message not verified",
        )
    # Plan must be unchanged since claim validation.
    util.require(
        util.sha256_file(Path(task["plan_path"])) == task["plan_sha256"],
        "plan changed since dispatch validation",
        "evidence_invalid",
    )


def claimed_dispatch_request(
    conn: sqlite3.Connection, task_id: str, run_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND stage='dispatch' "
        "AND status='claimed' AND run_id=?",
        (task_id, run_id),
    ).fetchone()
    if row is None:
        raise util.KanpilotError(
            "stale_run", f"no claimed dispatch request for run {run_id}"
        )
    return row


def require_task_run(task: sqlite3.Row, run_id: str) -> None:
    if task["run_id"] != run_id:
        raise util.KanpilotError(
            "stale_run", f"task run_id mismatch (expected {task['run_id']})"
        )


def release_dispatch_serial(conn: sqlite3.Connection, run_id: Optional[str]) -> None:
    if run_id and db.get_setting(conn, "dispatch_serial", "") == run_id:
        db.set_setting(conn, "dispatch_serial", "")


def acquire_lifecycle(conn: sqlite3.Connection, owner: str) -> None:
    """Lifecycle serial bit: one remote write at a time (state-model §5)."""
    current = db.get_setting(conn, "lifecycle_owner", "") or ""
    if current and current != owner:
        raise util.KanpilotError(
            "busy", f"lifecycle serial held by {current}"
        )
    db.set_setting(conn, "lifecycle_owner", owner)


def release_lifecycle(conn: sqlite3.Connection, owner: str) -> None:
    if db.get_setting(conn, "lifecycle_owner", "") == owner:
        db.set_setting(conn, "lifecycle_owner", "")


def finish_request(
    conn: sqlite3.Connection,
    request: sqlite3.Row,
    now: str,
    final_status: str,
    outcome: str,
    result: Any = None,
    note: Optional[str] = None,
    send_state: Optional[str] = None,
    evidence: Any = None,
) -> None:
    """Terminal transition for a request + attempt closure + serial release."""
    db.update_request(
        conn, request["request_id"], now,
        status=final_status, finished_at=now,
        result_json=result if result is not None else {"outcome": outcome},
    )
    db.close_attempt(
        conn, request["request_id"], request["run_id"], now, outcome,
        send_state=send_state, evidence=evidence, note=note,
    )
    if request["stage"] == "dispatch":
        release_dispatch_serial(conn, request["run_id"])


def fail_request(
    conn: sqlite3.Connection,
    request: sqlite3.Row,
    now: str,
    reason: str,
    actor: Optional[str] = None,
    outcome: str = "failed",
) -> None:
    """Guard-failure path from protocol.md §4.2-5 (request -> failed)."""
    finish_request(
        conn, request, now, "failed", outcome,
        result={"outcome": outcome, "reason": reason}, note=reason,
    )
    db.add_event(
        conn, now, "reported", task_id=request["task_id"],
        request_id=request["request_id"], actor=actor,
        detail={"outcome": outcome, "via": "claim_guard", "reason": reason},
    )
