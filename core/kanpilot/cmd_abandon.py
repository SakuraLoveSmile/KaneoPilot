"""abandon-task: mark a stuck task as abandoned without changing task status/stage."""
from __future__ import annotations

from typing import Optional

from . import constants, core, db, derive, util


def cmd_abandon_task(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    task_id = payload["task_id"]
    note = payload.get("note")
    target_state = payload.get("save_state", "archived")
    # 已永久移除（purged）的遗留任务仍可登记放弃决定：状态已终、无需改状态，
    # 只为让「已按 abandon 处置」在 DB 可查（A3）。默认仍拒绝，需显式 opt-in。
    allow_purged = bool(payload.get("allow_purged", False))
    if target_state not in ("archived", "trashed"):
        raise util.KanpilotError(
            "bad_request", "save_state must be archived or trashed"
        )

    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        purged = db.task_is_purged(task)
        if purged and not allow_purged:
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除，不能再放弃"
            )
        current = db.task_save_state(task)
        extra = db.task_extra(task)
        resolution = extra.get("resolution") or {}
        if (
            not purged
            and current == target_state
            and resolution.get("action") == "abandoned"
        ):
            return {
                "ok": True,
                "deduplicated": True,
                "task": derive.task_detail(conn, task, ctx.now),
            }
        if not purged:
            conn.execute(
                "UPDATE requests SET status='cancelled', finished_at=? "
                "WHERE task_id=? AND status='queued'",
                (now, task_id),
            )
        extra["resolution"] = {
            "action": "abandoned",
            "note": note,
            "at": now,
            "actor": "user",
            "purged": purged,
        }
        db.update_task(
            conn, task_id, now,
            save_state=current if purged else target_state,
            extra_json=extra,
        )
        db.add_event(
            conn, now, "task_abandoned", task_id=task_id, actor="user",
            detail={"save_state": target_state, "note": note},
        )
        task = db.require_task(conn, task_id)
        return {
            "ok": True,
            "deduplicated": False,
            "task": derive.task_detail(conn, task, ctx.now),
        }
