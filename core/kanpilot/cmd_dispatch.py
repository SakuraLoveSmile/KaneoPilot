"""Dispatch sub-steps (protocol.md §5.10, §4.6): dispatch-prepare /
begin-send / confirm / fail. Mirrors legacy kaneo_dispatch.py semantics."""

from __future__ import annotations

from pathlib import Path

from . import bridge, constants, core, db, remote, util


def _entry(ctx, payload, allowed):
    """Load task, verify run ownership, claimed dispatch request, status."""
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    core.require_task_run(task, payload["run_id"])
    request = core.claimed_dispatch_request(
        conn, task["task_id"], payload["run_id"]
    )
    if task["status"] not in allowed:
        raise util.KanpilotError(
            "conflict",
            f"task {task['task_id']} is {task['status']}, "
            f"expected {'|'.join(allowed)}",
        )
    return task, request


def cmd_dispatch_prepare(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    with db.write_txn(conn):
        task, _request = _entry(ctx, payload, ("dispatching", "prepared"))
        evidence = payload["evidence"]
        plan_basename = Path(task["plan_path"]).name
        core.validate_evidence(evidence, task, expect_message=False,
                               plan_basename=plan_basename)
        db.update_task(
            conn, task["task_id"], now,
            status="prepared", ui_evidence=evidence,
        )
        db.add_event(
            conn, now, "dispatch_prepared", task_id=task["task_id"],
            request_id=_request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": payload["run_id"]},
        )
        return {"status": "prepared"}


def cmd_dispatch_begin_send(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    with db.write_txn(conn):
        task, _request = _entry(ctx, payload, ("prepared",))
        owner = f"begin-send:{payload['run_id']}"
        core.acquire_lifecycle(conn, owner)

    try:
        # Remote pre-check (no write txn held).
        try:
            live = remote.get_task(remote.get_client(), task["task_id"])
        except bridge.BridgeError as exc:
            raise util.KanpilotError("remote_unavailable", str(exc)) from exc
        live_task = live.get("task") or {}
        pre_ok = (
            live_task.get("projectId") == task["project_id"]
            and live_task.get("status") == task["original_todo_slug"]
            and (
                not task["requirements_sha256"]
                or live.get("requirements_sha256")
                == task["requirements_sha256"]
            )
        )
        if not pre_ok:
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                core.block_task(
                    conn, task, now,
                    f"remote changed before transition: status "
                    f"{live_task.get('status')} != {task['original_todo_slug']}",
                    "remote_conflict", "kaneo-dispatch",
                    send_state="unknown", lock_retained=1,
                    manual_recovery_required=1,
                )
            raise util.KanpilotError(
                "remote_conflict", "remote changed before transition"
            )
        with db.write_txn(conn):
            task = db.require_task(conn, task["task_id"])
            core.require_task_run(task, payload["run_id"])
            if task["status"] != "prepared":
                raise util.KanpilotError(
                    "conflict", "state changed during begin-send"
                )
            db.update_task(
                conn, task["task_id"], now,
                status="submitting", transition_started_at=now,
            )
        # Remote write + re-verify.
        try:
            remote.update_status(
                remote.get_client(), task["task_id"], task["project_id"],
                constants.IN_PROGRESS_SLUG, task["original_todo_slug"],
            )
            after = remote.get_task(remote.get_client(), task["task_id"])
            after_task = after.get("task") or {}
            verified = (
                after_task.get("status") == constants.IN_PROGRESS_SLUG
                and after_task.get("projectId") == task["project_id"]
                and (
                    not task["requirements_sha256"]
                    or after.get("requirements_sha256")
                    == task["requirements_sha256"]
                )
            )
            if not verified:
                raise bridge.BridgeError(
                    f"remote transition unverifiable: "
                    f"{after_task.get('status')}"
                )
        except bridge.BridgeError as exc:
            # The remote write may or may not have landed -> unknown.
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                core.block_task(
                    conn, task, now, str(exc), "remote_conflict",
                    "kaneo-dispatch", send_state="unknown",
                    lock_retained=1, manual_recovery_required=1,
                )
            raise util.KanpilotError(
                "remote_conflict",
                f"transition verification failed: {exc}",
            ) from exc
        with db.write_txn(conn):
            db.update_task(
                conn, task["task_id"], now,
                kaneo_status=constants.IN_PROGRESS_SLUG,
                kaneo_checked_at=now, transition_verified_at=now,
            )
            db.add_event(
                conn, now, "kaneo_transition", task_id=task["task_id"],
                actor="kaneo-dispatch",
                detail={"from": task["original_todo_slug"],
                        "to": constants.IN_PROGRESS_SLUG},
            )
        return {
            "status": "submitting",
            "action": "click visible Send once; verify message and attachment",
        }
    finally:
        with db.write_txn(conn):
            core.release_lifecycle(conn, owner)


def cmd_dispatch_confirm(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    # Idempotent: a confirmed send returns running again.
    task = db.require_task(conn, payload["task_id"])
    if (
        task["run_id"] == payload["run_id"]
        and task["status"] == "running"
        and task["sent"] == 1
    ):
        return {"status": "running", "already_confirmed": True}

    with db.write_txn(conn):
        task, request = _entry(ctx, payload, ("submitting", "running"))
        util.require(
            task["transition_verified_at"],
            "transition not verified",
            "conflict",
        )
        plan_basename = Path(task["plan_path"]).name
        core.validate_evidence(
            payload["evidence"], task, expect_message=True,
            plan_basename=plan_basename,
        )
        evidence = payload["evidence"]
        db.update_task(
            conn, task["task_id"], now,
            status="running", stage="monitor", sent=1, send_state="sent",
            sent_at=now, send_evidence=evidence,
            session_id=evidence.get("session_id"),
            session_title=evidence.get("session_title"),
            dispatch_lease_expires_at=None, lock_retained=0,
        )
        core.finish_request(
            conn, request, now, "succeeded", "succeeded",
            result={"outcome": "succeeded", "send_state": "sent"},
            send_state="sent", evidence=evidence,
        )
        interval = db.get_setting(
            conn, "monitor_interval_seconds",
            constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
        )
        db.create_request(
            conn, now, "monitor", f"monitor:{task['task_id']}",
            task_id=task["task_id"], requested_by="scheduler",
            not_before=util.plus_seconds(interval, ctx.now),
        )
        core.release_dispatch_serial(conn, payload["run_id"])
        db.add_event(
            conn, now, "dispatch_sent", task_id=task["task_id"],
            request_id=request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": payload["run_id"],
                    "session_id": evidence.get("session_id")},
        )
        db.add_event(
            conn, now, "status_change", task_id=task["task_id"],
            actor="kaneo-dispatch", detail={"from": "submitting", "to": "running"},
        )
        return {"status": "running"}


def cmd_dispatch_fail(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    send_state = payload["send_state"]
    if send_state not in ("not_sent", "unknown"):
        raise util.KanpilotError(
            "bad_request", "send_state must be not_sent|unknown"
        )
    # Idempotent: already recorded for this run.
    task = db.require_task(conn, payload["task_id"])
    if (
        task["run_id"] == payload["run_id"]
        and task["status"] == "needs_user"
    ):
        return {
            "status": "needs_user", "send_state": task["send_state"],
            "already_recorded": True,
        }

    with db.write_txn(conn):
        task, request = _entry(
            ctx, payload, ("dispatching", "prepared", "submitting")
        )
        failure_stage = task["status"]
        db.update_task(
            conn, task["task_id"], now,
            status="needs_user", send_state=send_state,
            sent=0 if send_state == "not_sent" else None,
            failure_reason=payload["reason"],
            failure_code="dispatch_failed",
            failure_stage=failure_stage,
            blocked_at=now, blocked_summary=payload["reason"],
            lock_retained=1 if send_state != "not_sent" else 0,
            manual_recovery_required=1 if send_state == "unknown" else 0,
        )
        core.finish_request(
            conn, request, now, "failed", "failed",
            result={"outcome": "failed", "reason": payload["reason"],
                    "send_state": send_state},
            note=payload["reason"], send_state=send_state,
        )
        core.release_dispatch_serial(conn, payload["run_id"])
        db.add_event(
            conn, now, "dispatch_failed", task_id=task["task_id"],
            request_id=request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": payload["run_id"], "send_state": send_state,
                    "reason": payload["reason"],
                    "failure_stage": failure_stage},
        )
        db.add_event(
            conn, now, "status_change", task_id=task["task_id"],
            actor="kaneo-dispatch",
            detail={"from": failure_stage, "to": "needs_user"},
        )
        return {"status": "needs_user", "send_state": send_state}
