"""Remote-writing commands behind the lifecycle serial bit:
create-review-fix / advance-task / recheck (protocol.md §5.8/§5.9/§5.11)."""

from __future__ import annotations

from pathlib import Path

from . import (
    bridge, constants, core, db, derive, planfile, remote, util,
)


def _lifecycle_op(conn, label: str) -> str:
    owner = f"{label}:{util.new_uuid()}"
    core.acquire_lifecycle(conn, owner)
    return owner


def cmd_create_review_fix(ctx, payload: dict) -> dict:
    conn = ctx.conn
    dedupe_key = payload["dedupe_key"]
    util.require(
        len(dedupe_key) == 64
        and all(c in "0123456789abcdef" for c in dedupe_key),
        "dedupe_key must be a 64-char lowercase sha256",
        "bad_request",
    )
    util.require(
        payload["priority"] in bridge.VALID_PRIORITIES,
        "invalid priority",
        "bad_request",
    )
    # Local dedupe: an existing fix task for this key wins without any call.
    existing = conn.execute(
        "SELECT * FROM tasks WHERE plan_kind='review_fix' AND review_key=?",
        (dedupe_key,),
    ).fetchone()
    if existing is not None:
        return {
            "ok": True, "fix_task_id": existing["task_id"], "created": False,
            "source_sha256": existing["source_sha256"],
        }

    # Phase 1 (txn): local checks + acquire lifecycle serial atomically.
    with db.write_txn(conn):
        source_task = db.require_task(conn, payload["source_task_id"])
        owner = _lifecycle_op(conn, "create-review-fix")
    try:
        client = remote.get_client()
        result = remote.create_todo(
            client,
            source_task["project_id"],
            dedupe_key,
            {
                "title": payload["title"],
                "description": payload["description"],
                "priority": payload["priority"],
            },
        )
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    finally:
        with db.write_txn(conn):
            core.release_lifecycle(conn, owner)

    fix_task_id = str(result["task"]["id"])
    now = ctx.now_iso
    with db.write_txn(conn):
        task = db.get_task(conn, fix_task_id)
        if task is None:
            conn.execute(
                "INSERT INTO tasks(task_id, project_id, project_name, title, "
                "repository, stage, status, kaneo_status, kaneo_checked_at, "
                "requirements_sha256, source_sha256, plan_kind, "
                "review_source_task_id, review_key, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fix_task_id, source_task["project_id"],
                    source_task["project_name"], payload["title"],
                    source_task["repository"], "plan", "needs_plan",
                    result.get("todo_slug"), now,
                    result.get("requirements_sha256"),
                    result.get("source_sha256"), "review_fix",
                    source_task["task_id"], dedupe_key, now, now,
                ),
            )
            db.add_event(
                conn, now, "task_created", task_id=fix_task_id,
                actor="kaneo-review", detail={"via": "create-review-fix"},
            )
        # Record the link on the reviewed task.
        reviewed = db.require_task(conn, source_task["task_id"])
        db.update_task(
            conn, reviewed["task_id"], now, fix_task_id=fix_task_id,
            review_key=dedupe_key,
        )
        # Plan request carrying the review context (automation-contract §2).
        db.create_request(
            conn, now, "plan", f"plan:{fix_task_id}", task_id=fix_task_id,
            payload={
                "plan_kind": "review_fix",
                "review_key": dedupe_key,
                "review_source_task_id": source_task["task_id"],
                "fix_task_id": fix_task_id,
            },
            requested_by="kaneo-review",
        )
        db.add_event(
            conn, now, "fix_task_created", task_id=source_task["task_id"],
            actor="kaneo-review",
            detail={"fix_task_id": fix_task_id, "dedupe_key": dedupe_key,
                    "created": bool(result.get("created"))},
        )
        return {
            "ok": True, "fix_task_id": fix_task_id,
            "created": bool(result.get("created")),
            "source_sha256": result.get("source_sha256"),
        }


def cmd_advance_task(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    target, expected = payload["target"], payload["expected_current"]
    if target not in ("in-review", "done"):
        raise util.KanpilotError("bad_request", f"invalid target {target}")
    if expected not in ("in-progress", "in-review"):
        raise util.KanpilotError(
            "bad_request", f"invalid expected_current {expected}"
        )
    if (target, expected) not in (
        ("in-review", "in-progress"),
        ("done", "in-progress"),
        ("done", "in-review"),
    ):
        raise util.KanpilotError(
            "bad_request", f"cannot advance {expected} -> {target}"
        )

    actor = payload.get("run_id") or "executor"
    # Phase 1 (txn): local checks + acquire lifecycle serial.
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        if payload.get("run_id"):
            claimed = conn.execute(
                "SELECT * FROM requests WHERE task_id=? AND status='claimed' "
                "AND run_id=?",
                (task["task_id"], payload["run_id"]),
            ).fetchone()
            if claimed is None:
                raise util.KanpilotError(
                    "stale_run", "run_id does not match a claimed request"
                )
        owner = _lifecycle_op(conn, "advance-task")

    wrote_remote = False
    try:
        # Phase 2 (no txn): remote verify -> update -> re-verify.
        try:
            client = remote.get_client()
            live = remote.get_task(client, task["task_id"])
        except bridge.BridgeError as exc:
            raise util.KanpilotError("remote_unavailable", str(exc)) from exc
        live_task = live.get("task") or {}
        conflict = (
            live_task.get("projectId") != task["project_id"]
            or live_task.get("status") != expected
            or (
                task["requirements_sha256"]
                and live.get("requirements_sha256") != task["requirements_sha256"]
            )
        )
        if conflict:
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                db.update_task(
                    conn, task["task_id"], now,
                    kaneo_status=live_task.get("status"),
                    kaneo_checked_at=now,
                )
                core.block_task(
                    conn, task, now,
                    f"remote conflict: expected {expected}, observed "
                    f"{live_task.get('status')}",
                    "remote_conflict", actor,
                )
            raise util.KanpilotError(
                "remote_conflict",
                f"task {task['task_id']} remote status "
                f"{live_task.get('status')} != {expected}",
            )
        verify_error = None
        verified = False
        try:
            remote.update_status(
                client, task["task_id"], task["project_id"],
                target, expected,
            )
            wrote_remote = True
            after = remote.get_task(client, task["task_id"])
            after_task = after.get("task") or {}
            verified = (
                after_task.get("status") == target
                and after_task.get("projectId") == task["project_id"]
                and (
                    not task["requirements_sha256"]
                    or after.get("requirements_sha256")
                    == task["requirements_sha256"]
                )
            )
        except bridge.BridgeError as exc:
            verify_error = exc
        if not verified:
            # Deterministic post-write mismatch -> remote_conflict; transport
            # failure after a write attempt -> uncertain -> remote_unavailable.
            if verify_error is None:
                code, message = "remote_conflict", (
                    f"remote transition unverifiable: status "
                    f"{after_task.get('status')} != {target}"
                )
            else:
                code, message = "remote_unavailable", str(verify_error)
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                if wrote_remote:
                    core.block_task(conn, task, now, message, code, actor)
            raise util.KanpilotError(code, message)
    finally:
        with db.write_txn(conn):
            core.release_lifecycle(conn, owner)

    # Phase 3 (txn): local transition + follow-up request.
    with db.write_txn(conn):
        task = db.require_task(conn, task["task_id"])
        new_status = "in_review" if target == "in-review" else "done"
        extra = {"kaneo_status": target, "kaneo_checked_at": now}
        if new_status == "in_review":
            # New review cycle: clear any earlier registered conclusion.
            extra["review_report_path"] = None
            extra["review_report_sha256"] = None
        core.transition_task(
            conn, task, new_status, now, actor,
            detail={"via": "advance-task", "target": target,
                    "evidence_note": payload.get("evidence_note")},
            **extra,
        )
        db.add_event(
            conn, now, "kaneo_transition", task_id=task["task_id"],
            actor=actor, detail={"from": expected, "to": target},
        )
        if new_status == "in_review":
            db.create_request(
                conn, now, "review", f"review:{task['task_id']}",
                task_id=task["task_id"], requested_by="scheduler",
            )
        return {
            "ok": True, "kaneo_status": target, "task_status": new_status,
        }


def cmd_recheck(ctx, payload: dict) -> dict:
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    offline = bool(payload.get("offline"))
    checks = []

    if task["plan_path"]:
        try:
            path = Path(task["plan_path"]).expanduser()
            ok = path.is_file() and not path.is_symlink()
            sha_ok = ok and util.sha256_file(path) == task["plan_sha256"]
            checks.append({
                "name": "plan_sha256", "ok": bool(sha_ok),
                "detail": "match" if sha_ok else "missing/changed",
            })
        except OSError as exc:
            checks.append(
                {"name": "plan_sha256", "ok": False, "detail": str(exc)}
            )
    if task["source_path"]:
        try:
            _s, source_sha, req_sha = planfile.check_source_snapshot(
                task["source_path"], task["task_id"], task["project_id"]
            )
            checks.append({
                "name": "source_sha256", "ok": source_sha == task["source_sha256"],
                "detail": "match" if source_sha == task["source_sha256"]
                else "changed",
            })
            checks.append({
                "name": "requirements_sha256",
                "ok": req_sha == task["requirements_sha256"],
                "detail": "match" if req_sha == task["requirements_sha256"]
                else "changed",
            })
        except util.KanpilotError as exc:
            checks.append(
                {"name": "source_snapshot", "ok": False, "detail": exc.message}
            )

    observed_status, observed_req = None, None
    if not offline:
        try:
            live = remote.get_task(remote.get_client(), task["task_id"])
            live_task = live.get("task") or {}
            observed_status = live_task.get("status")
            observed_req = live.get("requirements_sha256")
            checks.append({
                "name": "remote_status", "ok": True,
                "detail": str(observed_status),
            })
            checks.append({
                "name": "remote_requirements",
                "ok": observed_req == task["requirements_sha256"],
                "detail": "match" if observed_req == task["requirements_sha256"]
                else "changed",
            })
        except Exception as exc:
            checks.append(
                {"name": "remote", "ok": False, "detail": str(exc)}
            )

    now = ctx.now_iso
    with db.write_txn(conn):
        updates = {"kaneo_checked_at": now}
        if observed_status is not None:
            updates["kaneo_status"] = observed_status
        db.update_task(conn, task["task_id"], now, **updates)
        db.add_event(
            conn, now, "rechecked", task_id=task["task_id"], actor="user",
            detail={"offline": offline,
                    "ok": all(item["ok"] for item in checks)},
        )
    return {
        "task_id": task["task_id"], "checks": checks,
        "kaneo_status": observed_status,
        "requirements_sha256": observed_req,
        "ok": all(item["ok"] for item in checks),
    }
