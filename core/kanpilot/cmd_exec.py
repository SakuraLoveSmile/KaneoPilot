"""Executor-facing commands: claim / heartbeat / report / publish-plan /
register-review (protocol.md §4.2/§4.3/§4.5/§4.7/§5.5/§5.6/§5.7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import (
    cmd_read, constants, core, db, derive, dispatch_validate, planfile,
    remote, util,
)


def _select_candidate(
    conn: sqlite3.Connection, stage: str, request_id: Optional[str], now
) -> Optional[sqlite3.Row]:
    if request_id:
        row = db.get_request(conn, request_id)
        if row is None:
            raise util.KanpilotError(
                "not_found", f"request not found: {request_id}"
            )
        if row["stage"] != stage:
            raise util.KanpilotError(
                "bad_request",
                f"request {request_id} is stage {row['stage']}, not {stage}",
            )
        if row["status"] != "queued":
            raise util.KanpilotError(
                "conflict", f"request {request_id} is {row['status']}"
            )
        if not util.ts_due(row["not_before"], now):
            raise util.KanpilotError(
                "conflict", f"request {request_id} not due before {row['not_before']}"
            )
        return row
    return conn.execute(
        "SELECT * FROM requests WHERE stage=? AND status='queued' "
        "AND (not_before IS NULL OR not_before <= ?) "
        "ORDER BY priority ASC, created_at ASC LIMIT 1",
        (stage, util.to_iso(now)),
    ).fetchone()


def _claim_row(
    conn: sqlite3.Connection,
    request: sqlite3.Row,
    task: Optional[sqlite3.Row],
    executor: str,
    lease_seconds: int,
    now,
) -> str:
    """Mark request claimed inside the current write txn; returns run_id."""
    now_iso = util.to_iso(now)
    run_id = util.new_uuid()
    payload = db.request_payload(request)
    if task is not None:
        payload["task_status_at_claim"] = task["status"]
    db.update_request(
        conn, request["request_id"], now_iso,
        status="claimed", run_id=run_id, claimed_by=executor,
        claimed_at=now_iso, heartbeat_at=now_iso,
        lease_expires_at=util.plus_seconds(lease_seconds, now),
        attempt_no=request["attempt_no"] + 1,
        payload_json=payload,
    )
    db.add_attempt(
        conn, request["request_id"], request["task_id"], run_id,
        request["stage"], request["kind"], now_iso, "claimed",
    )
    db.add_event(
        conn, now_iso, "claimed", task_id=request["task_id"],
        request_id=request["request_id"], actor=executor,
        detail={"run_id": run_id, "kind": request["kind"]},
    )
    return run_id


def cmd_claim(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    stage, executor = payload["stage"], payload["executor"]
    if stage not in constants.STAGES:
        raise util.KanpilotError("bad_request", f"invalid stage {stage}")
    lease_seconds = db.get_setting(
        conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS
    )

    dispatch_candidate = None
    with db.write_txn(conn):
        cmd_read.tick_inline(conn, now)
        if db.paused(conn, stage):
            return {"claimed": False, "reason": "paused"}
        if stage == "dispatch" and conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE stage='dispatch' "
            "AND status='claimed'"
        ).fetchone()["n"]:
            return {"claimed": False, "reason": "stage_busy"}
        request = _select_candidate(conn, stage, payload.get("request_id"), now)
        if request is None:
            return {"claimed": False, "reason": "empty"}
        task = (
            db.get_task(conn, request["task_id"])
            if request["task_id"]
            else None
        )
        if request["kind"] == "dispatch":
            dispatch_candidate = (request["request_id"], task["task_id"])
        else:
            reason = core.guard_check(task, request["kind"])
            if reason is not None:
                core.fail_request(conn, request, util.to_iso(now), reason,
                                  actor=executor)
                return {
                    "claimed": False, "reason": "guard_failed",
                    "request_id": request["request_id"], "error": reason,
                    "code": "guard_failed",
                }
            run_id = _claim_row(conn, request, task, executor, lease_seconds, now)
            if task is not None and request["kind"] == "plan":
                core.transition_task(
                    conn, task, "planning", util.to_iso(now), executor,
                    detail={"via": "claim"},
                )
                task = db.require_task(conn, task["task_id"])
            context = {
                "kind": request["kind"],
                "payload": db.request_payload(
                    db.get_request(conn, request["request_id"])
                ),
                "lease_seconds": lease_seconds,
            }
            if task is not None:
                context["task_id"] = task["task_id"]
                context["task"] = derive.task_summary(conn, task, now)
            return {
                "claimed": True, "request_id": request["request_id"],
                "run_id": run_id, "context": context,
            }

    # ---- dispatch: validate outside the write txn, then re-claim ----------
    request_id, task_id = dispatch_candidate
    request = db.get_request(conn, request_id)
    task = db.require_task(conn, task_id)
    try:
        checked = dispatch_validate.validate_dispatch(conn, task, now)
    except util.KanpilotError as exc:
        return dispatch_validate.handle_dispatch_guard_failure(
            conn, request, task, exc, util.to_iso(now), executor
        )

    with db.write_txn(conn):
        request = db.get_request(conn, request_id)
        task = db.require_task(conn, task_id)
        if request["status"] != "queued":
            return {"claimed": False, "reason": "empty"}
        if db.paused(conn, stage):
            return {"claimed": False, "reason": "paused"}
        if conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE stage='dispatch' "
            "AND status='claimed'"
        ).fetchone()["n"]:
            return {"claimed": False, "reason": "stage_busy"}
        # Re-verify guard + plan hash inside the txn (queue may have moved).
        reason = core.guard_check(task, "dispatch")
        if reason is not None:
            core.fail_request(conn, request, util.to_iso(now), reason,
                              actor=executor)
            return {
                "claimed": False, "reason": "guard_failed",
                "request_id": request_id, "error": reason,
                "code": "guard_failed",
            }
        from .util import sha256_file
        if sha256_file(Path(checked["plan_path"])) != checked["plan_sha256"]:
            core.fail_request(
                conn, request, util.to_iso(now),
                "plan changed between validation and claim", actor=executor,
            )
            return {
                "claimed": False, "reason": "guard_failed",
                "request_id": request_id,
                "error": "plan changed between validation and claim",
                "code": "validation_failed",
            }
        run_id = _claim_row(conn, request, task, executor, lease_seconds, now)
        session_marker = f"{task_id}/{run_id}"
        db.update_task(
            conn, task_id, util.to_iso(now),
            status="dispatching", stage="dispatch",
            run_id=run_id, session_marker=session_marker,
            sent=0, send_state="not_sent",
            dispatch_lease_expires_at=util.plus_seconds(lease_seconds, now),
            original_todo_slug=checked["original_todo_slug"],
            kaneo_status=checked["kaneo_status"],
            kaneo_checked_at=util.to_iso(now),
            requirements_sha256=checked["requirements_sha256"],
            ui_evidence=None, send_evidence=None,
            transition_started_at=None, transition_verified_at=None,
            sent_at=None, session_id=None, session_title=None,
            **core.clear_block_fields(),
        )
        db.set_setting(conn, "dispatch_serial", run_id)
        db.add_event(
            conn, util.to_iso(now), "status_change", task_id=task_id,
            actor=executor,
            detail={"from": "ready", "to": "dispatching"},
        )
        context = {
            "task_id": task_id,
            "plan_path": checked["plan_path"],
            "attachment_name": checked["attachment_name"],
            "repository": checked["repository"],
            "project_id": checked["project_id"],
            "plan_kind": checked["plan_kind"],
            "session_marker": session_marker,
            "instruction": constants.DISPATCH_INSTRUCTION.format(
                marker=session_marker
            ),
            "original_todo_slug": checked["original_todo_slug"],
            "requirements_sha256": checked["requirements_sha256"],
            "source_sha256": checked["source_sha256"],
            "lease_seconds": lease_seconds,
        }
        return {
            "claimed": True, "request_id": request_id,
            "run_id": run_id, "context": context,
        }


def _claimed_request(conn, request_id: str, run_id: str) -> sqlite3.Row:
    request = db.get_request(conn, request_id)
    if request is None:
        raise util.KanpilotError("not_found", f"request not found: {request_id}")
    if request["status"] != "claimed" or request["run_id"] != run_id:
        raise util.KanpilotError(
            "stale_run",
            f"request {request_id} status={request['status']} "
            f"run={request['run_id']}",
        )
    return request


def cmd_heartbeat(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    with db.write_txn(conn):
        request = _claimed_request(conn, payload["request_id"], payload["run_id"])
        lease_seconds = db.get_setting(
            conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS
        )
        expires = util.plus_seconds(lease_seconds, now)
        db.update_request(
            conn, request["request_id"], util.to_iso(now),
            heartbeat_at=util.to_iso(now), lease_expires_at=expires,
        )
        if payload.get("progress") and request["task_id"]:
            db.update_task(
                conn, request["task_id"], util.to_iso(now),
                last_progress=payload["progress"],
            )
        db.add_event(
            conn, util.to_iso(now), "heartbeat", task_id=request["task_id"],
            request_id=request["request_id"], actor=request["claimed_by"],
        )
        return {"ok": True, "lease_expires_at": expires}


def cmd_report(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    outcome = payload["outcome"]
    if outcome not in ("succeeded", "failed", "needs_user"):
        raise util.KanpilotError("bad_request", f"invalid outcome {outcome}")
    with db.write_txn(conn):
        request = _claimed_request(conn, payload["request_id"], payload["run_id"])
        now_iso = util.to_iso(now)
        final = "succeeded" if outcome == "succeeded" else "failed"
        result = {
            "outcome": outcome,
            "note": payload.get("note"),
            "send_state": payload.get("send_state"),
            "kaneo_status": payload.get("kaneo_status"),
        }
        core.finish_request(
            conn, request, now_iso, final, outcome, result=result,
            note=payload.get("note"), send_state=payload.get("send_state"),
            evidence=payload.get("evidence"),
        )
        task = (
            db.get_task(conn, request["task_id"])
            if request["task_id"]
            else None
        )
        if payload.get("progress") and task is not None:
            db.update_task(
                conn, task["task_id"], now_iso,
                last_progress=payload["progress"],
            )
        if payload.get("kaneo_status") and task is not None:
            db.update_task(
                conn, task["task_id"], now_iso,
                kaneo_status=payload["kaneo_status"], kaneo_checked_at=now_iso,
            )
            db.add_event(
                conn, now_iso, "kaneo_observed", task_id=task["task_id"],
                actor=request["claimed_by"],
                detail={"to": payload["kaneo_status"], "via": "report"},
            )
        if payload.get("send_state") and task is not None:
            db.update_task(
                conn, task["task_id"], now_iso,
                send_state=payload["send_state"],
            )
        if outcome == "needs_user" and task is not None:
            core.block_task(
                conn, task, now_iso,
                payload.get("failure_reason") or payload.get("note")
                or "executor reported needs_user",
                payload.get("failure_code") or "reported",
                request["claimed_by"],
                send_state=payload.get("send_state", task["send_state"]),
            )
        db.add_event(
            conn, now_iso, "reported", task_id=request["task_id"],
            request_id=request["request_id"], actor=request["claimed_by"],
            detail=result,
        )
        # Scheduler follow-ups.
        if request["kind"] == "plan-scan" and outcome == "succeeded":
            db.set_setting(conn, "last_plan_scan_at", now_iso)
        if (
            request["kind"] == "monitor"
            and outcome in ("succeeded", "failed")
            and task is not None
            and task["status"] == "running"
        ):
            interval = db.get_setting(
                conn, "monitor_interval_seconds",
                constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
            )
            db.create_request(
                conn, now_iso, "monitor", f"monitor:{task['task_id']}",
                task_id=task["task_id"], requested_by="scheduler",
                not_before=util.plus_seconds(interval, now),
            )
        fresh = db.get_request(conn, request["request_id"])
        task_status = task["status"] if task is not None else None
        if outcome == "needs_user" and task is not None:
            task_status = "needs_user"
        return {
            "ok": True,
            "request_status": fresh["status"],
            "task_status": task_status,
        }


def cmd_publish_plan(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        if task["status"] in constants.DISPATCH_MID_STATUSES + ("running",):
            raise util.KanpilotError(
                "conflict",
                f"task {task['task_id']} is {task['status']}; "
                "refusing to overwrite dispatch inputs",
            )
        plan_kind = payload["plan_kind"]
        if plan_kind not in ("standard", "review_fix"):
            raise util.KanpilotError("bad_request", "invalid plan_kind")
        meta, raw, path = planfile.load_plan(
            payload["plan_path"], task["task_id"]
        )
        planfile.check_plan_meta(meta, plan_kind)
        if meta.get("plan_kind") not in (None, plan_kind):
            raise util.KanpilotError(
                "validation_failed", "plan_kind frontmatter conflicts"
            )
        util.require(
            meta.get("task_id") == task["task_id"], "plan task_id mismatch"
        )
        util.require(
            meta.get("source_sha256") == payload["source_sha256"],
            "source_sha256 payload/frontmatter mismatch",
        )
        util.require(
            meta.get("requirements_sha256") == payload["requirements_sha256"],
            "requirements_sha256 payload/frontmatter mismatch",
        )
        if task["source_sha256"]:
            util.require(
                task["source_sha256"] == payload["source_sha256"],
                "source_sha256 differs from task baseline",
            )
        if task["requirements_sha256"]:
            util.require(
                task["requirements_sha256"] == payload["requirements_sha256"],
                "requirements_sha256 differs from task baseline",
            )
        if task["repository"]:
            util.require(
                meta["repository"] == task["repository"],
                "plan repository differs from task repository",
            )
        plan_sha = util.sha256_bytes(raw)
        fields = {
            "plan_path": str(path), "plan_sha256": plan_sha,
            "plan_kind": plan_kind,
            "plan_generated_at": payload["generated_at"],
            "planner_model": payload["planner_model"],
            "reasoning_effort": payload["reasoning_effort"],
            "source_sha256": payload["source_sha256"],
            "requirements_sha256": payload["requirements_sha256"],
            "repository": meta["repository"],
        }
        if plan_kind == "review_fix":
            report = util.inside(
                Path(meta["review_report_path"]),
                constants.review_reports_root(),
                "review_report_path",
            )
            util.require(report.is_file(), "review report missing")
            util.require(
                util.sha256_file(report) == meta["review_report_sha256"],
                "review report sha256 mismatch",
            )
            fields.update(
                {
                    "review_report_path": str(report),
                    "review_report_sha256": meta["review_report_sha256"],
                    "review_key": meta["review_key"],
                    "review_source_task_id": meta["review_source_task_id"],
                }
            )
        task = db.require_task(conn, task["task_id"])
        core.transition_task(
            conn, task, "ready", ctx.now_iso,
            actor=payload.get("run_id") or "kaneo-plan",
            detail={"via": "publish-plan"}, **fields,
        )
        db.add_event(
            conn, ctx.now_iso, "plan_published", task_id=task["task_id"],
            actor="kaneo-plan",
            detail={"plan_path": str(path), "plan_sha256": plan_sha,
                    "plan_kind": plan_kind},
        )
        return {"ok": True, "task_status": "ready", "plan_sha256": plan_sha}


def cmd_register_review(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    conclusion = payload["conclusion"]
    if conclusion not in ("pass", "changes_requested", "blocked"):
        raise util.KanpilotError("bad_request", f"invalid conclusion {conclusion}")
    if conclusion == "changes_requested" and not (
        payload.get("fix_task_id") and payload.get("review_key")
    ):
        raise util.KanpilotError(
            "bad_request",
            "changes_requested requires fix_task_id and review_key",
        )
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        report = util.inside(
            Path(payload["report_path"]),
            constants.review_reports_root(),
            "report_path",
        )
        util.require(report.is_file(), f"review report missing: {report}")
        report_sha = util.sha256_file(report)
        fields = {
            "review_report_path": str(report),
            "review_report_sha256": report_sha,
        }
        extra_entries = {"review_conclusion": conclusion,
                         "review_registered_at": ctx.now_iso}
        if payload.get("review_key"):
            fields["review_key"] = payload["review_key"]
        if payload.get("fix_task_id"):
            fields["fix_task_id"] = payload["fix_task_id"]
            extra_entries["fix_task_source_sha256"] = payload.get(
                "fix_task_source_sha256"
            )
        if payload.get("repair_plan_path"):
            repair = util.inside(
                Path(payload["repair_plan_path"]),
                constants.plan_root(),
                "repair_plan_path",
            )
            util.require(repair.is_file(), "repair plan missing")
            extra_entries["repair_plan_path"] = str(repair)
            extra_entries["repair_plan_sha256"] = util.sha256_file(repair)
        db.update_task(conn, task["task_id"], ctx.now_iso, **fields)
        db.set_extra(
            conn, db.require_task(conn, task["task_id"]), ctx.now_iso,
            **extra_entries,
        )
        db.add_event(
            conn, ctx.now_iso, "review_registered", task_id=task["task_id"],
            actor="kaneo-review",
            detail={"conclusion": conclusion, "report_sha256": report_sha,
                    "review_key": payload.get("review_key"),
                    "fix_task_id": payload.get("fix_task_id")},
        )
        task = db.require_task(conn, task["task_id"])
        if conclusion == "blocked" and task["status"] != "needs_user":
            core.block_task(
                conn, task, ctx.now_iso, "review blocked", "review_blocked",
                "kaneo-review",
            )
        elif (
            conclusion == "pass"
            and payload.get("kaneo_status") in constants.DONE_SLUGS
            and task["status"] != "done"
        ):
            core.transition_task(
                conn, task, "done", ctx.now_iso, "kaneo-review",
                detail={"via": "register-review pass + kaneo done"},
                kaneo_status=payload["kaneo_status"],
                kaneo_checked_at=ctx.now_iso,
            )
        elif payload.get("kaneo_status"):
            db.update_task(
                conn, task["task_id"], ctx.now_iso,
                kaneo_status=payload["kaneo_status"],
                kaneo_checked_at=ctx.now_iso,
            )
        return {"ok": True, "report_sha256": report_sha}
