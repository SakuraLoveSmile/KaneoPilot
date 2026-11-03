"""Global opt-in completed-task retention (protocol §5.15.7)."""
from __future__ import annotations

from datetime import timedelta

from . import cmd_archive, core, db, derive, release_delivery, util, verification_checks


SETTING_KEY = "auto_archive_config"
DEFAULT_CONFIG = {"enabled": False, "after_days": 7}


def _validate_config(value):
    if (not isinstance(value, dict) or type(value.get("enabled")) is not bool
            or type(value.get("after_days")) is not int
            or not 1 <= value["after_days"] <= 3650):
        raise util.bad_request("自动归档配置须包含 enabled 布尔值和 1–3650 的整数 after_days")
    return {"enabled": value["enabled"], "after_days": value["after_days"]}


def config_for(conn):
    value = db.get_setting(conn, SETTING_KEY)
    return dict(DEFAULT_CONFIG) if value is None else _validate_config(value)


def cmd_get_auto_archive_config(ctx, payload):
    return {"config": config_for(ctx.conn)}


def cmd_set_auto_archive_config(ctx, payload):
    config = _validate_config(payload)
    with db.write_txn(ctx.conn):
        previous = config_for(ctx.conn)
        db.set_setting(ctx.conn, SETTING_KEY, config)
        if previous != config:
            db.add_event(ctx.conn, ctx.now_iso, "auto_archive_config_changed", actor="user",
                         detail={"from": previous, "to": config})
    return {"config": config}


def _retention_start(conn, task, now):
    """Use the latest real completion/restore boundary, never mutable updated_at.

    An imported historical done without a trustworthy transition gets a durable
    first-observed anchor. The event identity invalidates that anchor after a
    later completion or an actual restore, including both in one tick interval.
    """
    source_id, source_at = None, None
    for event in conn.execute(
        "SELECT id,at,kind,detail_json FROM events WHERE task_id=? "
        "AND kind IN ('status_change','task_restored','task_reconnected') ORDER BY id DESC",
        (task["task_id"],),
    ):
        detail = util.jload(event["detail_json"])
        if not isinstance(detail, dict):
            continue
        if event["kind"] == "task_restored":
            if detail.get("from") not in ("archived", "trashed") or detail.get("to") != "active":
                continue
            source_id, source_at = event["id"], util.parse_ts(event["at"])
            break
        if event["kind"] == "status_change":
            if detail.get("from") == detail.get("to"):
                continue
            source_id = event["id"]
            if detail.get("to") == "done":
                source_at = util.parse_ts(event["at"])
            break
        # A reconnect starts a new lifecycle; any older completion is no longer
        # evidence for the current done row if its new transition is missing.
        source_id = event["id"]
        break

    stored = db.task_extra(task).get("auto_archive_retention")
    stored = stored if isinstance(stored, dict) else {}
    cached_at = util.parse_ts(stored.get("started_at"))
    if stored.get("source_event_id") == source_id and cached_at is not None:
        return cached_at
    started_at = source_at or now
    db.set_extra(conn, task, util.to_iso(now), auto_archive_retention={
        "started_at": util.to_iso(started_at), "source_event_id": source_id,
    })
    return started_at


def _has_pending_delivery(conn, task):
    delivery = core.delivery_of(task)
    if (delivery.get("mode") == "pr" and delivery.get("pr_number")
            and delivery.get("pr_state") not in ("MERGED", "CLOSED")
            and not delivery.get("merged_at")):
        return True
    if conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND kind='delivery' "
        "AND status IN ('queued','claimed') LIMIT 1", (task["task_id"],),
    ).fetchone() is not None:
        return True
    extra = db.task_extra(task)
    # A project's enabled policy alone does not create a release obligation for
    # old done rows. Inspect only this task's explicitly attached release intent.
    if any(extra.get(key) for key in ("release_ref", "external_build_repair_ref", "build_failure_hold")):
        release = release_delivery.projection(conn, task)
        if release and not release.get("is_historical") and release["state"] != "published":
            return True
    return False


def archive_due_inline(conn, now, *, project_ids=None):
    """Run local housekeeping in tick's existing writer transaction."""
    config = config_for(conn)
    if not config["enabled"] or db.get_setting(conn, "paused_global", False):
        return {"archived": [], "skipped": []}
    scope, params = "", ()
    if project_ids is not None:
        params = tuple(project_ids)
        scope = " AND project_id IN (%s)" % ",".join("?" for _ in params)
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE status='done' AND save_state='active'" + scope + " ORDER BY task_id",
        params,
    ).fetchall()
    archived, skipped = [], []
    for task in tasks:
        if derive.task_board_column(task, conn, now) != "done" or _has_pending_delivery(conn, task):
            continue
        conn.execute("SAVEPOINT auto_archive_task")
        try:
            # Check before persisting a missing historical anchor as those
            # writes must respect a verification operation's task ownership too.
            verification_checks.require_not_running(conn, task["task_id"])
            started_at = _retention_start(conn, task, now)
            if started_at + timedelta(days=config["after_days"]) <= now:
                result = cmd_archive.save_task_inline(
                    conn, task["task_id"], now, "archived", "task_archived",
                    expected_save_state="active", actor="scheduler",
                    event_detail={"source": "auto_archive", "after_days": config["after_days"],
                                  "retention_started_at": util.to_iso(started_at)},
                )
                if not result["deduplicated"]:
                    archived.append(task["task_id"])
        except util.KanpilotError as exc:
            conn.execute("ROLLBACK TO auto_archive_task")
            if exc.code != "conflict":
                raise
            skipped.append({"kind": "auto_archive", "task_id": task["task_id"], "reason": exc.message})
        finally:
            conn.execute("RELEASE auto_archive_task")
    return {"archived": archived, "skipped": skipped}
