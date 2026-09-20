"""Queue-management commands: enqueue / set-priority / set-paused / resolve /
sync-tasks (protocol.md §5.3/§5.4/§4.9/§4.10)."""

from __future__ import annotations

from typing import Optional

from . import constants, core, db, derive, util


def _infer_kind(task) -> str:
    kind = constants.ENQUEUE_INFER.get(task["status"])
    if kind is None:
        raise util.KanpilotError(
            "bad_request",
            f"cannot infer request kind for task status {task['status']}",
        )
    return kind


def cmd_enqueue(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        kind = payload.get("kind") or _infer_kind(task)
        if kind not in constants.REQUEST_KINDS or kind == "plan-scan":
            raise util.KanpilotError("bad_request", f"invalid kind {kind}")
        allowed = constants.CLAIM_GUARDS[kind]
        if task["status"] not in allowed:
            raise util.KanpilotError(
                "bad_request",
                f"kind {kind} requires task status "
                f"{'|'.join(sorted(allowed))}, got {task['status']}",
            )
        key = f"{kind}:{task['task_id']}"
        request_payload = {"note": payload.get("note")} if payload.get("note") else None
        row, created = db.create_request(
            conn, now, kind, key, task_id=task["task_id"],
            payload=request_payload, requested_by="user",
        )
        return {
            "request": derive.request_view(row, ctx.now),
            "deduplicated": not created,
        }


def cmd_set_priority(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    priority = payload["priority"]
    with db.write_txn(conn):
        request = None
        if payload.get("request_id"):
            request = db.get_request(conn, payload["request_id"])
            if request is None:
                raise util.KanpilotError(
                    "not_found", f"request not found: {payload['request_id']}"
                )
            if request["status"] != "queued":
                raise util.KanpilotError(
                    "conflict",
                    f"request {request['request_id']} is {request['status']}, "
                    "only queued requests accept priority changes",
                )
        elif payload.get("task_id"):
            request = derive.queued_request_for_task(conn, payload["task_id"])
            if request is None:
                raise util.KanpilotError(
                    "not_found",
                    f"no queued request for task {payload['task_id']}",
                )
        else:
            raise util.KanpilotError(
                "bad_request", "set-priority needs request_id or task_id"
            )
        db.update_request(conn, request["request_id"], now, priority=priority)
        db.add_event(
            conn, now, "priority_changed", task_id=request["task_id"],
            request_id=request["request_id"], actor="user",
            detail={"priority": priority},
        )
        return {
            "ok": True,
            "request": derive.request_view(
                db.get_request(conn, request["request_id"]), ctx.now
            ),
        }


def cmd_set_paused(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    stage, paused = payload["stage"], payload["paused"]
    if stage is not None and stage not in constants.STAGES:
        raise util.KanpilotError("bad_request", f"invalid stage {stage}")
    with db.write_txn(conn):
        if stage is None:
            db.set_setting(conn, "paused_global", bool(paused))
        else:
            stages = db.get_setting(conn, "paused_stages", []) or []
            stages = [item for item in stages if item != stage]
            if paused:
                stages.append(stage)
            db.set_setting(conn, "paused_stages", stages)
        db.add_event(
            conn, now, "paused" if paused else "unpaused", actor="user",
            detail={"stage": stage},
        )
        return {
            "paused": {
                "global": db.get_setting(conn, "paused_global", False),
                "stages": db.get_setting(conn, "paused_stages", []) or [],
            }
        }


def _new_cycle_request(
    conn, now: str, task, kind: str, requested_by: str = "user"
):
    key = f"{kind}:{task['task_id']}"
    return db.create_request(
        conn, now, kind, key, task_id=task["task_id"], requested_by=requested_by
    )


def _retry_target(conn, task) -> tuple[str, str]:
    """Decide (task_status, request kind) for resolve retry."""
    last = conn.execute(
        "SELECT kind FROM requests WHERE task_id=? AND status IN "
        "('failed','cancelled') ORDER BY updated_at DESC LIMIT 1",
        (task["task_id"],),
    ).fetchone()
    kind = last["kind"] if last else None
    if kind == "dispatch" or task["send_state"] is not None:
        return "ready", "dispatch"
    if kind == "monitor":
        return "running", "monitor"
    if kind == "review":
        return "in_review", "review"
    return "needs_plan", "plan"


def cmd_resolve(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    action = payload["action"]
    note = payload.get("note")
    if action not in constants.VALID_RESOLVE_ACTIONS:
        raise util.KanpilotError("bad_request", f"invalid action {action}")
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        status, send_state = task["status"], task["send_state"]

        def need(condition: bool, message: str) -> None:
            if not condition:
                raise util.KanpilotError("bad_request", message)

        resolution = {"action": action, "note": note, "at": now, "actor": "user"}
        new_request = None

        if action == "retry":
            need(
                status == "needs_user" and send_state in (None, "not_sent"),
                "retry requires needs_user with send_state not_sent "
                "(or a non-dispatch block)",
            )
            target_status, kind = _retry_target(conn, task)
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            core.transition_task(
                conn, task, target_status, now, "user",
                detail={"via": "resolve retry"}, **fields,
            )
            new_request, _ = _new_cycle_request(conn, now, task, kind)

        elif action == "restore":
            need(
                status == "needs_user" and send_state == "unknown",
                "restore requires needs_user with send_state unknown",
            )
            fields = core.clear_block_fields()
            fields.update(
                {"sent": 0, "send_state": "not_sent", "lock_retained": 0,
                 "resolution": resolution}
            )
            core.transition_task(
                conn, task, "ready", now, "user",
                detail={"via": "resolve restore"}, **fields,
            )
            new_request, _ = _new_cycle_request(conn, now, task, "dispatch")

        elif action == "accept_duplicate":
            need(
                status == "needs_user" and send_state == "unknown",
                "accept_duplicate requires needs_user with send_state unknown",
            )
            resolution["duplicate_risk_accepted"] = True
            fields = core.clear_block_fields()
            fields.update(
                {"sent": 0, "send_state": "not_sent", "lock_retained": 0,
                 "resolution": resolution}
            )
            core.transition_task(
                conn, task, "ready", now, "user",
                detail={"via": "resolve accept_duplicate",
                        "duplicate_risk_accepted": True},
                **fields,
            )
            new_request, _ = _new_cycle_request(conn, now, task, "dispatch")

        elif action == "mark_sent":
            need(
                status == "needs_user" and send_state == "unknown",
                "mark_sent requires needs_user with send_state unknown",
            )
            fields = core.clear_block_fields()
            fields.update(
                {"sent": 1, "send_state": "sent", "sent_at": now,
                 "lock_retained": 0, "resolution": resolution}
            )
            core.transition_task(
                conn, task, "running", now, "user",
                detail={"via": "resolve mark_sent"}, **fields,
            )
            new_request, _ = _new_cycle_request(conn, now, task, "monitor")

        elif action == "replan":
            need(status == "needs_user", "replan requires needs_user")
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            core.transition_task(
                conn, task, "needs_plan", now, "user",
                detail={"via": "resolve replan"}, **fields,
            )
            new_request, _ = _new_cycle_request(conn, now, task, "plan")

        elif action == "release_lost":
            lost = derive.lost_request_for_task(conn, task["task_id"], ctx.now)
            need(lost is not None, "release_lost requires a claimed request "
                                   "with an expired lease")
            core.finish_request(
                conn, lost, now, "cancelled", "cancelled",
                result={"outcome": "cancelled", "via": "release_lost"},
                note="released after executor loss",
            )
            db.add_event(
                conn, now, "request_cancelled", task_id=task["task_id"],
                request_id=lost["request_id"], actor="user",
                detail={"run_id": lost["run_id"], "via": "release_lost"},
            )
            prior = db.request_payload(lost).get("task_status_at_claim")
            kind = lost["kind"]
            if kind == "dispatch" and task["status"] in (
                "ready",) + constants.DISPATCH_MID_STATUSES:
                # Mid-dispatch loss: keep send evidence, block for a human.
                if task["status"] != "needs_user":
                    core.block_task(
                        conn, task, now,
                        "dispatch executor lost mid-flight", "executor_lost",
                        "user",
                    )
            else:
                # plan/monitor/review mid-flight -> restore pre-claim status;
                # a task already past dispatch keeps its current status.
                target = task["status"]
                if kind != "dispatch" and task["status"] not in (
                    "needs_user", "done"
                ):
                    if prior in constants.TASK_STATUSES:
                        target = prior
                if target != task["status"]:
                    core.transition_task(
                        conn, task, target, now, "user",
                        detail={"via": "resolve release_lost",
                                "restored_to": target},
                        resolution=resolution,
                    )
                if target in (constants.CLAIM_GUARDS.get(kind) or ()):
                    new_request, _ = _new_cycle_request(
                        conn, now, task, kind
                    )

        elif action == "mark_done":
            need(
                status in ("in_review", "needs_user", "running"),
                f"mark_done not allowed from status {status}",
            )
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            core.transition_task(
                conn, task, "done", now, "user",
                detail={"via": "resolve mark_done"}, **fields,
            )

        elif action == "dismiss":
            need(status == "needs_user", "dismiss requires needs_user")
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            core.transition_task(
                conn, task, "ready", now, "user",
                detail={"via": "resolve dismiss"}, **fields,
            )

        db.add_event(
            conn, now, "resolved", task_id=task["task_id"], actor="user",
            detail=resolution,
        )
        fresh = db.require_task(conn, task["task_id"])
        return {
            "ok": True,
            "task_status": fresh["status"],
            "request": (
                derive.request_view(new_request, ctx.now)
                if new_request is not None
                else None
            ),
        }


def cmd_sync_tasks(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    executor = payload["executor"]
    upserted = 0
    created_requests: list = []
    changed: list = []
    with db.write_txn(conn):
        for project in payload["projects"]:
            if not isinstance(project, dict):
                raise util.KanpilotError("bad_request", "project entry must be object")
            project_id = project.get("project_id")
            project_name = project.get("project_name")
            if not project_id or not project_name:
                raise util.KanpilotError(
                    "bad_request", "project needs project_id/project_name"
                )
            for item in project.get("tasks") or []:
                if not isinstance(item, dict):
                    raise util.KanpilotError(
                        "bad_request", "task entry must be an object"
                    )
                task_id = item.get("task_id")
                if not task_id:
                    raise util.KanpilotError("bad_request", "task needs task_id")
                observed = item.get("kaneo_status")
                task = db.get_task(conn, task_id)
                if task is None:
                    # A task first observed already done is archived directly
                    # (state-model §2: 任意 -> done); otherwise it needs a plan.
                    initial = (
                        "done" if observed in constants.DONE_SLUGS
                        else "needs_plan"
                    )
                    fields = {
                        "task_id": task_id, "project_id": project_id,
                        "project_name": project_name,
                        "title": item.get("title"),
                        "repository": item.get("repository")
                        or project.get("repository"),
                        "stage": core.stage_for_status(initial),
                        "status": initial,
                        "kaneo_status": observed, "kaneo_checked_at": now,
                        "requirements_sha256": item.get("requirements_sha256"),
                        "source_sha256": item.get("source_sha256"),
                        "source_path": item.get("source_path"),
                        "created_at": now, "updated_at": now,
                    }
                    columns = ", ".join(fields)
                    conn.execute(
                        f"INSERT INTO tasks({columns}) VALUES "
                        f"({','.join('?' for _ in fields)})",
                        tuple(fields.values()),
                    )
                    upserted += 1
                    db.add_event(
                        conn, now, "task_created", task_id=task_id,
                        actor=executor, detail={"via": "sync-tasks"},
                    )
                    if initial == "needs_plan":
                        row, made = db.create_request(
                            conn, now, "plan", f"plan:{task_id}",
                            task_id=task_id, requested_by=executor,
                        )
                        if made:
                            created_requests.append(
                                {"request_id": row["request_id"],
                                 "kind": "plan", "task_id": task_id}
                            )
                    changed.append({"task_id": task_id, "from": None,
                                    "to": initial})
                    continue

                upserted += 1
                task_id = task["task_id"]
                previous_status = task["status"]
                observed_changed = observed != task["kaneo_status"]
                updates: dict = {"kaneo_checked_at": now}
                if observed_changed:
                    updates["kaneo_status"] = observed
                    db.add_event(
                        conn, now, "kaneo_observed", task_id=task_id,
                        actor=executor,
                        detail={"from": task["kaneo_status"], "to": observed},
                    )
                # Titles/sources are metadata, safe to refresh in any status.
                if item.get("title") is not None:
                    updates["title"] = item["title"]
                if item.get("repository") or project.get("repository"):
                    updates["repository"] = (
                        item.get("repository") or project.get("repository")
                    )

                if task["status"] in constants.DISPATCH_MID_STATUSES:
                    # Never overwrite mid-dispatch state/fingerprints.
                    db.update_task(conn, task_id, now, **updates)
                    if observed_changed:
                        changed.append(
                            {"task_id": task_id, "from": previous_status,
                             "to": previous_status,
                             "kaneo_status": observed}
                        )
                    continue

                new_requirements = item.get("requirements_sha256")
                requirements_changed = (
                    new_requirements is not None
                    and new_requirements != task["requirements_sha256"]
                )
                # Source snapshot fields refresh independently: requirements
                # drive replanning, the snapshot is the dispatch-time baseline.
                new_source = item.get("source_sha256")
                new_path = item.get("source_path")
                if new_source is not None and new_source != task["source_sha256"]:
                    updates["source_sha256"] = new_source
                if new_path is not None and new_path != task["source_path"]:
                    updates["source_path"] = new_path
                if requirements_changed:
                    updates["requirements_sha256"] = new_requirements
                    db.update_task(conn, task_id, now, **updates)
                    db.add_event(
                        conn, now, "requirements_changed", task_id=task_id,
                        actor=executor,
                        detail={
                            "from": task["requirements_sha256"],
                            "to": new_requirements,
                        },
                    )
                    task = db.require_task(conn, task_id)
                    if task["status"] != "needs_plan":
                        core.transition_task(
                            conn, task, "needs_plan", now, executor,
                            detail={"via": "requirements_changed"},
                        )
                    row, made = db.create_request(
                        conn, now, "plan", f"plan:{task_id}", task_id=task_id,
                        requested_by=executor,
                    )
                    if made:
                        created_requests.append(
                            {"request_id": row["request_id"], "kind": "plan",
                             "task_id": task_id}
                        )
                    changed.append(
                        {"task_id": task_id, "from": previous_status,
                         "to": "needs_plan"}
                    )
                    continue

                meta_keys = [
                    key for key, value in updates.items()
                    if key not in ("kaneo_checked_at", "kaneo_status")
                    and value != task[key]
                ]
                db.update_task(conn, task_id, now, **updates)
                if meta_keys:
                    db.add_event(
                        conn, now, "task_updated", task_id=task_id,
                        actor=executor, detail={"fields": meta_keys},
                    )
                if observed in constants.DONE_SLUGS and task["status"] != "done":
                    task = db.require_task(conn, task_id)
                    core.transition_task(
                        conn, task, "done", now, executor,
                        detail={"via": "kaneo_observed", "kaneo_status": observed},
                    )
                    changed.append(
                        {"task_id": task_id, "from": previous_status, "to": "done"}
                    )
                elif (
                    observed in constants.REVIEW_SLUGS
                    and task["status"] == "running"
                ):
                    task = db.require_task(conn, task_id)
                    core.transition_task(
                        conn, task, "in_review", now, executor,
                        detail={"via": "kaneo_observed", "kaneo_status": observed},
                        review_report_path=None, review_report_sha256=None,
                    )
                    changed.append(
                        {"task_id": task_id, "from": previous_status,
                         "to": "in_review"}
                    )
                elif observed_changed:
                    changed.append(
                        {"task_id": task_id, "from": previous_status,
                         "to": task["status"], "kaneo_status": observed}
                    )
    return {
        "upserted": upserted,
        "created_requests": created_requests,
        "changed": changed,
    }
