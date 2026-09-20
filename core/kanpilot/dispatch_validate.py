"""Dispatch claim validation chain (protocol.md §4.5), equivalent to the
legacy kaneo_dispatch.py check(). Runs outside the write transaction —
remote checks never hold BEGIN IMMEDIATE."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import bridge, constants, core, db, planfile, remote, util


class DispatchCheckFailed(util.KanpilotError):
    """Deterministic validation failure -> request fails, task needs_user."""

    def __init__(self, message: str, code: str = "validation_failed"):
        super().__init__(code, message)


class DispatchRemoteFailed(util.KanpilotError):
    """Transient remote failure -> request fails, task unchanged."""

    def __init__(self, message: str):
        super().__init__("remote_unavailable", message)


def _require(condition, message, code="validation_failed"):
    if not condition:
        raise DispatchCheckFailed(message, code)


def validate_dispatch(conn: sqlite3.Connection, task, now) -> dict:
    """Run the full §4.5 chain; returns the dispatch claim context fields."""
    task_id = task["task_id"]
    plan_kind = task["plan_kind"] or "standard"

    # 1. plan file + frontmatter + registered hash -------------------------
    util.require(task["plan_path"], "task has no plan_path")
    try:
        meta, raw, path = planfile.load_plan(task["plan_path"], task_id)
    except util.KanpilotError as exc:
        raise DispatchCheckFailed(exc.message, exc.code)
    try:
        planfile.check_plan_meta(meta, plan_kind)
    except util.KanpilotError as exc:
        raise DispatchCheckFailed(exc.message)
    if meta.get("plan_kind") not in (None, plan_kind):
        raise DispatchCheckFailed("plan_kind frontmatter conflicts with task")
    plan_sha = util.sha256_bytes(raw)
    _require(
        task["plan_sha256"] == plan_sha,
        "published plan changed (plan_sha256 mismatch)",
    )
    _require(meta.get("source_sha256"), "plan missing source_sha256")

    # 2. source snapshot recompute -----------------------------------------
    util.require(task["source_path"], "task has no source snapshot")
    try:
        _source, source_sha, requirements = planfile.check_source_snapshot(
            task["source_path"], task_id, meta["project_id"]
        )
    except util.KanpilotError as exc:
        raise DispatchCheckFailed(exc.message)
    _require(
        source_sha == meta["source_sha256"],
        "original source mismatch (source_sha256)",
    )

    # 3. requirements fingerprint ------------------------------------------
    if meta.get("requirements_sha256"):
        _require(
            requirements == meta["requirements_sha256"],
            "plan requirements mismatch",
        )
    if task["requirements_sha256"]:
        _require(
            requirements == task["requirements_sha256"],
            "registered requirements baseline mismatch",
        )

    # 4. remote task + columns ----------------------------------------------
    try:
        live = remote.get_task(remote.get_client(), task_id)
    except bridge.BridgeError as exc:
        raise DispatchRemoteFailed(str(exc))
    except Exception as exc:  # defensive: stub clients
        raise DispatchRemoteFailed(str(exc))
    live_task = live.get("task") or {}
    _require(
        live_task.get("projectId") == meta["project_id"],
        "remote project mismatch",
    )
    _require(
        live_task.get("status") in constants.TODO_SLUGS,
        "remote task not To Do",
    )
    _require(
        live.get("requirements_sha256") == requirements,
        "remote requirements changed",
    )
    slugs = {
        str(item.get("slug"))
        for item in live.get("columns") or []
        if isinstance(item, dict)
    }
    _require(
        live_task.get("status") in slugs
        and constants.IN_PROGRESS_SLUG in slugs,
        "required columns absent",
    )

    # 5. plan_kind-specific --------------------------------------------------
    if plan_kind == "review_fix":
        report = util.inside(
            Path(meta["review_report_path"]),
            constants.review_reports_root(),
            "review_report_path",
        )
        util.require(report.is_file(), "review report missing")
        _require(
            util.sha256_file(report) == meta["review_report_sha256"],
            "review report changed",
        )
        source_task = db.get_task(conn, meta["review_source_task_id"])
        _require(source_task is not None, "reviewed task not registered")
        extra = db.task_extra(source_task)
        _require(
            source_task["fix_task_id"] == task_id
            and source_task["review_key"] == meta["review_key"],
            "review delivery mismatch (fix_task_id/review_key)",
        )
        _require(
            extra.get("repair_plan_path") == str(path)
            and extra.get("repair_plan_sha256") == plan_sha,
            "review delivery mismatch (repair_plan)",
        )
        _require(
            constants.REVIEW_MARKER_PREFIX + meta["review_key"] + " -->"
            in str(live_task.get("description", "")),
            "review marker missing in remote description",
        )
    else:
        _require(
            constants.REVIEW_MARKER_PREFIX
            not in str(live_task.get("description", "")),
            "review task requires review_fix plan",
        )

    # 6. conflicting send evidence -------------------------------------------
    _require(
        task["send_state"] not in ("unknown", "sent") and task["sent"] != 1,
        "conflicting send evidence; resolve required",
        "send_conflict",
    )

    return {
        "task_id": task_id,
        "plan_path": str(path),
        "plan_sha256": plan_sha,
        "attachment_name": path.name,
        "repository": meta["repository"],
        "project_id": meta["project_id"],
        "plan_kind": plan_kind,
        "original_todo_slug": live_task["status"],
        "requirements_sha256": requirements,
        "source_sha256": meta["source_sha256"],
        "kaneo_status": live_task["status"],
    }


def handle_dispatch_guard_failure(
    conn: sqlite3.Connection,
    request,
    task,
    exc: util.KanpilotError,
    now: str,
    actor: str,
) -> dict:
    """§4.2-5 + §4.5 failure bookkeeping; returns the claim response."""
    now_iso = now
    with db.write_txn(conn):
        request = db.get_request(conn, request["request_id"])
        task = db.get_task(conn, task["task_id"])
        if request["status"] == "queued":
            # Fail only when still ours: a competing claim may have taken it
            # while validation ran outside the transaction.
            core.fail_request(conn, request, now_iso, exc.message, actor=actor)
        # Deterministic failures block the task; remote_unavailable leaves it
        # untouched (protocol.md §4.5-4: not a blocking condition).
        if exc.code != "remote_unavailable" and task["status"] != "needs_user":
            core.block_task(
                conn, task, now_iso, exc.message, exc.code, actor,
                stage=task["status"], send_state=task["send_state"],
            )
    return {
        "claimed": False,
        "reason": "guard_failed",
        "request_id": request["request_id"],
        "error": exc.message,
        "code": exc.code,
    }
