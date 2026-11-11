"""Retention sweep for task stage trees (protocol §5.29.6).

Only ``plan``/``review`` trees of terminal tasks are archived and removed; the
execution tree, ``meta.json`` and ``artifacts/`` stay. Every deletion needs a
clean, validated tree and an accepted receipt; dirty or unvalidated trees are
quarantined instead of removed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from . import constants, db, runtime_operations, util, worktree_pool

_SCOPE = "worktree-retention"
_TOOL = "worktree_stage_retention"
_TERMINAL = ("done", "cancelled")


def _days(conn, override=None):
    value = override if override is not None else db.get_setting(
        conn, "worktree_retention_days", constants.DEFAULT_WORKTREE_RETENTION_DAYS)
    if type(value) is not int or not 1 <= value <= 90:
        raise util.bad_request("worktree_retention_days 必须为1–90整数")
    return value


def _parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _task_stages(conn, task_id):
    return conn.execute("SELECT * FROM worktree_slots WHERE task_id=? AND task_id IS NOT NULL "
                        "ORDER BY stage", (task_id,)).fetchall()


def _age_days(latest, now):
    parsed = _parse(latest)
    if parsed is None:
        return None
    delta = now - parsed
    return max(0.0, delta.total_seconds() / 86400.0)


def candidates(conn, now_iso, days=None):
    """Read-only retention inventory: eligible tasks plus blocked reasons."""
    limit = _days(conn, days)
    now = _parse(now_iso) or util.now_dt()
    result = []
    for task in conn.execute("SELECT * FROM tasks ORDER BY task_id").fetchall():
        rows = _task_stages(conn, task["task_id"])
        if not rows:
            continue
        latest = max(row["updated_at"] or "" for row in rows)
        age = _age_days(latest, now)
        blocked = []
        if task["status"] not in _TERMINAL:
            blocked.append({"code": "task_not_terminal", "detail": task["status"]})
        for row in rows:
            if row["state"] != "free":
                blocked.append({"code": "stage_not_free", "stage": row["stage"],
                                "detail": row["state"]})
        active = conn.execute("SELECT COUNT(*) FROM requests WHERE task_id=? "
                              "AND status IN ('queued','claimed')", (task["task_id"],)).fetchone()[0]
        if active:
            blocked.append({"code": "request_active", "detail": active})
        result.append({
            "task_id": task["task_id"], "title": task["title"], "status": task["status"],
            "stages": {row["stage"]: {"state": row["state"], "path": row["path"],
                                      "updated_at": row["updated_at"]} for row in rows},
            "last_used_at": latest, "age_days": round(age, 1) if age is not None else None,
            "retention_days": limit,
            "expired": age is not None and age >= limit and not blocked,
            "blocked": blocked,
        })
    return {"days": limit, "tasks": result}


def _archive(root: Path, task_id: str, folders: list[Path]) -> dict:
    archives = root / ".archives"
    archives.mkdir(parents=True, exist_ok=True)
    target = archives / (task_id + "-" + datetime.now().strftime("%Y%m%d") + ".tar.gz")
    if target.exists():
        target = archives / (task_id + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz")
    with tarfile.open(target, "w:gz") as handle:
        for folder in folders:
            handle.add(folder, arcname=str(Path(task_id) / folder.name))
    with tarfile.open(target, "r:gz") as handle:
        members = handle.getnames()
    if not members:
        target.unlink(missing_ok=True)
        raise util.KanpilotError("worktree_retention_failed", "归档包为空，未删除阶段树")
    return {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "members": len(members)}


def _sweep_task(ctx, task_id, rows, days):
    root = Path(rows[0]["worktrees_root"])
    folders = [Path(row["path"]) for row in rows if Path(row["path"]).is_dir()]
    if not folders:
        # Directories already gone: forget the records without inventing an archive.
        with db.write_txn(ctx.conn):
            ctx.conn.execute("DELETE FROM worktree_slots WHERE task_id=?", (task_id,))
        return {"task_id": task_id, "deleted": [], "archived": None, "forgotten": True}
    for row in rows:
        try:
            path = worktree_pool._validate(row)
            if worktree_pool._git(path, "status", "--porcelain=v1", "-z",
                                  "--untracked-files=all"):
                raise util.KanpilotError("worktree_invalid", "阶段树有未登记改动")
        except Exception as exc:
            try:
                worktree_pool.quarantine(ctx.conn, row["slot_id"], row["holder_id"],
                                         row["lease_token"], "retention: " + str(exc))
            except Exception:
                pass
            return {"task_id": task_id, "deleted": [], "archived": None,
                    "quarantined": str(exc)}
    worktree_pool.write_meta(ctx.conn, task_id)
    archive = _archive(root, task_id, folders)
    for folder in folders:
        shutil.rmtree(folder)
    with db.write_txn(ctx.conn):
        ctx.conn.execute("DELETE FROM worktree_slots WHERE task_id=?", (task_id,))
    db.add_event(ctx.conn, ctx.now_iso, "worktree_stage_retention", task_id=task_id,
                 detail={"archive": archive["path"], "days": days,
                         "deleted": [folder.name for folder in folders]})
    return {"task_id": task_id, "deleted": [str(folder) for folder in folders],
            "archived": archive}


def sweep(ctx, *, days=None, confirm=False):
    """Archive and remove expired stage trees. ``confirm`` is required for deletion."""
    inventory = candidates(ctx.conn, ctx.now_iso, days)
    eligible = [item for item in inventory["tasks"] if item["expired"]]
    public = {"days": inventory["days"],
              "eligible": [{"task_id": item["task_id"], "age_days": item["age_days"]}
                           for item in eligible],
              "skipped": [{"task_id": item["task_id"], "blocked": item["blocked"]}
                          for item in inventory["tasks"] if not item["expired"]],
              "dry_run": not confirm}
    if not confirm:
        return public
    # Maintenance pass also refreshes the human-readable meta.json for every
    # task that owns stage trees, expired or not.
    for item in inventory["tasks"]:
        worktree_pool.write_meta(ctx.conn, item["task_id"])
    results = []
    for item in eligible:
        operation_id = "retain:" + item["task_id"] + ":" + datetime.now().strftime("%Y%m%d")
        previous = db.get_runtime_operation(ctx.conn, _SCOPE, operation_id)
        if previous is not None and previous["status"] == "accepted":
            results.append({**util.jload(previous["result_json"]), "deduplicated": True})
            continue
        with db.write_txn(ctx.conn):
            if db.get_runtime_operation(ctx.conn, _SCOPE, operation_id) is None:
                runtime_operations.begin_operation(ctx.conn, _SCOPE, operation_id, "core", _TOOL,
                                                   {"task_id": item["task_id"], "days": inventory["days"]})
                runtime_operations.mark_operation_unknown(ctx.conn, _SCOPE, operation_id)
        rows = _task_stages(ctx.conn, item["task_id"])
        try:
            result = _sweep_task(ctx, item["task_id"], rows, inventory["days"])
        except Exception as exc:
            with db.write_txn(ctx.conn):
                runtime_operations.complete_operation(ctx.conn, _SCOPE, operation_id,
                                                      status="unknown",
                                                      result={"task_id": item["task_id"],
                                                              "error": str(exc)})
            results.append({"task_id": item["task_id"], "error": str(exc)})
            continue
        with db.write_txn(ctx.conn):
            runtime_operations.complete_operation(ctx.conn, _SCOPE, operation_id, result=result)
        results.append(result)
    return {**public, "dry_run": False, "results": results}


def cmd_worktree_retention(ctx, payload):
    execute = payload.get("dry_run") is False
    if execute and payload.get("confirm") is not True:
        raise util.bad_request("执行保留期回收必须 confirm=true")
    return sweep(ctx, days=payload.get("days"), confirm=execute)


def auto_sweep(conn, now_iso):
    """Engine-side hourly sweep; never runs inside a caller transaction."""
    last = db.get_setting(conn, "last_worktree_retention_at")
    parsed, now = _parse(last), (_parse(now_iso) or util.now_dt())
    if parsed is not None and (now - parsed).total_seconds() < 3600:
        return {"skipped": "interval"}
    ctx = _Context(conn, now_iso)
    result = sweep(ctx, confirm=True)
    with db.write_txn(conn):
        db.set_setting(conn, "last_worktree_retention_at", now_iso)
    return result


class _Context:
    def __init__(self, conn, now_iso):
        self.conn = conn
        self.now_iso = now_iso
