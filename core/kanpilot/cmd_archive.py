"""archive-task / trash-task / restore-task (protocol.md §5.15.2, v1.3).

保存状态（save_state）与生命周期正交：归档/回收站只影响调度台，不删除 Kaneo
任务、计划、报告或 Devin 会话，也不修改 Kaneo 状态。已完成（board_column=done）
与已归档（save_state=archived）是两个概念。

- 禁用条件（conflict + 中文原因）：task.status ∈ ARCHIVE_BLOCKED_STATUSES、
  存在未解除失联请求（claimed 且租约过期）、send_state='unknown'。
- 排队取消与领取竞争：取消在单个 BEGIN IMMEDIATE 事务内对该任务
  status='queued' 的请求执行 cancelled；事务内复核，若已被 claimed（执行端
  已领取）→ 中止并返回 conflict，不取消、不改保存状态。
- 幂等：已处于目标 save_state → {"ok": true, "deduplicated": true}，不重复写事件。
- restore-task：save_state→active、清 saved_at、auto_advance_paused 默认 1
  （resume_auto=true 时 0）；本地重算计划/来源哈希并尝试远端 get-task 观察
  （远端不可达→warning 不阻断）；返回刷新后 task（derive.task_detail）。
"""

from __future__ import annotations

from pathlib import Path

from . import constants, db, derive, planfile, remote, util


def _save_task(ctx, payload: dict, target_state: str, event_kind: str) -> dict:
    """archive-task / trash-task 共用：单事务内前置复核 + 取消排队 + 改状态。"""
    conn, now = ctx.conn, ctx.now_iso
    task_id = payload["task_id"]
    expected_save_state = payload.get("expected_save_state")
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        current = db.task_save_state(task)
        # v2 §5.20.1：purged 终态——不可归档/回收/恢复，一律 conflict。
        if current == "purged":
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除（终态），不能再变更保存状态"
            )
        # 幂等：已在目标 save_state，不重复写事件（§5.15.2）。
        if current == target_state:
            return {
                "ok": True, "deduplicated": True,
                "task": derive.task_detail(conn, task, ctx.now),
            }
        # 保存状态冲突核验：期望保存状态与当前不符则报告冲突
        if expected_save_state is not None and current != expected_save_state:
            raise util.KanpilotError(
                "conflict",
                f"任务保存状态冲突（期望 {expected_save_state}，实际为 {current}）",
            )
        # 前置复核（事务内）：禁用条件 → conflict + 中文原因。
        # derive 侧失联判定需要 datetime（ctx.now），DB 写入用 ISO 串（now）。
        reason = derive.archive_blocked_reason(conn, task, ctx.now)
        if reason is not None:
            raise util.KanpilotError("conflict", reason)
        # 排队取消与领取竞争：事务内复核，已被 claimed（执行端已领取）→ 中止，
        # 不取消、不改保存状态（§5.15.2）。
        claimed = conn.execute(
            "SELECT 1 FROM requests WHERE task_id=? AND status='claimed' LIMIT 1",
            (task_id,),
        ).fetchone()
        if claimed is not None:
            raise util.KanpilotError(
                "conflict", "任务已被执行端领取，暂不能移出看板"
            )
        # 取消该任务 queued 请求（同事务）。
        cancelled = db.cancel_queued_requests(conn, task_id, now)
        db.update_task(conn, task_id, now, save_state=target_state, saved_at=now)
        db.add_event(
            conn, now, event_kind, task_id=task_id, actor="user",
            detail={"from": current, "to": target_state,
                    "cancelled_requests": cancelled},
        )
        fresh = db.require_task(conn, task_id)
        return {
            "ok": True, "deduplicated": False,
            "cancelled_requests": cancelled,
            "task": derive.task_detail(conn, fresh, ctx.now),
        }


def cmd_archive_task(ctx, payload: dict) -> dict:
    return _save_task(ctx, payload, "archived", "task_archived")


def cmd_trash_task(ctx, payload: dict) -> dict:
    return _save_task(ctx, payload, "trashed", "task_trashed")


def _reverify(ctx, task) -> list:
    """restore 后本地重算计划/来源哈希 + 尝试远端 get-task 观察。
    哈希缺失/不符与远端不可达都只记 warning，不阻断恢复（§5.15.2）。"""
    conn, now = ctx.conn, ctx.now_iso
    warnings: list = []
    updates = {"kaneo_checked_at": now}

    if task["plan_path"]:
        try:
            path = Path(task["plan_path"]).expanduser()
            ok = path.is_file() and not path.is_symlink()
            if not (ok and util.sha256_file(path) == task["plan_sha256"]):
                warnings.append("计划文件缺失或已变更")
        except OSError as exc:
            warnings.append(f"计划文件核验失败：{exc}")
    if task["source_path"]:
        try:
            _source, source_sha, _req_sha = planfile.check_source_snapshot(
                task["source_path"], task["task_id"], task["project_id"]
            )
            if source_sha != task["source_sha256"]:
                warnings.append("来源快照已变更")
        except util.KanpilotError as exc:
            warnings.append(f"来源快照核验失败：{exc.message}")

    # 尝试远端观察（只读 get-task）；不可达 → warning，不阻断。
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
        observed = (live.get("task") or {}).get("status")
        if observed is not None:
            updates["kaneo_status"] = observed
    except Exception as exc:  # 远端不可达/不兼容：降级为 warning。
        warnings.append(f"远端不可达，未更新 Kaneo 观测：{exc}")

    with db.write_txn(conn):
        db.update_task(conn, task["task_id"], now, **updates)
    return warnings


def cmd_restore_task(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    task_id = payload["task_id"]
    resume_auto = bool(payload.get("resume_auto"))
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        current = db.task_save_state(task)
        # v2 §5.20.1：purged 终态不可恢复。
        if current == "purged":
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除（终态），不能恢复"
            )
        deduplicated = current == "active"
        if not deduplicated:
            # save_state→active、清 saved_at；恢复后默认暂停自动推进，
            # resume_auto=true 时不暂停（§5.15.2）。
            db.update_task(
                conn, task_id, now, save_state="active", saved_at=None,
                auto_advance_paused=0 if resume_auto else 1,
            )
            db.add_event(
                conn, now, "task_restored", task_id=task_id, actor="user",
                detail={"from": current, "to": "active",
                        "resume_auto": resume_auto},
            )
        fresh = db.require_task(conn, task_id)
    # 事务外核验（含远端观察）；幂等 active 也执行状态核验并返回任务。
    warnings = _reverify(ctx, fresh)
    detail = derive.task_detail(conn, db.require_task(conn, task_id), ctx.now)
    result = {"ok": True, "deduplicated": deduplicated, "task": detail}
    if warnings:
        result["warnings"] = warnings
    return result


# 保存状态 -> 中文（purged 场景文案；与 cmd_queue._SAVE_STATE_CN 同口径）。
_SAVE_STATE_CN = {"active": "活跃", "archived": "归档",
                  "trashed": "移入回收站", "purged": "永久从列表移除"}

# ----------------------------------------------------- v2 purge-task（§5.20）

def cmd_purge_task(ctx, payload: dict) -> dict:
    """purge-task {task_id, expected_save_state:"trashed"} →
    {ok, deduplicated}（protocol §5.20.2，v2）。

    仅从回收站（save_state=trashed）永久从列表移除；历史与文件全部保留。
    同事务复核：状态须为 trashed（否则 conflict）、无 queued/claimed 请求、
    无未解除失联、send_state != 'unknown'；关联 execution_rounds 一并标记
    purged（退出常规列表与统计）。已 purged → 幂等成功。"""
    conn, now = ctx.conn, ctx.now_iso
    task_id = payload["task_id"]
    expected = payload.get("expected_save_state")
    if expected is not None and expected != "trashed":
        raise util.bad_request(
            "purge-task 只能从回收站移除（expected_save_state 应为 trashed）"
        )
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        current = db.task_save_state(task)
        if current == "purged":
            return {"ok": True, "deduplicated": True}
        if current != "trashed":
            raise util.KanpilotError(
                "conflict",
                f"任务当前为{_SAVE_STATE_CN.get(current, current)}状态，"
                "只能对回收站中的任务执行永久移除（先移入回收站）",
            )
        # 安全条件复核（同事务，§5.20.2）。
        problems = []
        active_req = conn.execute(
            "SELECT request_id, status FROM requests WHERE task_id=? "
            "AND status IN ('queued','claimed')",
            (task_id,),
        ).fetchall()
        if active_req:
            problems.append("存在排队/领取中的请求")
        if derive.lost_request_for_task(conn, task_id, ctx.now) is not None:
            problems.append("存在未解除的执行端失联请求")
        if task["send_state"] == "unknown":
            problems.append("发送结果不明（send_state=unknown）")
        if problems:
            raise util.KanpilotError(
                "conflict",
                "不满足永久移除条件：" + "；".join(problems),
            )
        # 任务本体 + 关联轮次一并置 purged；历史/文件不动。
        db.update_task(
            conn, task_id, now, save_state="purged", saved_at=now
        )
        rounds = conn.execute(
            "SELECT run_id FROM execution_rounds WHERE task_id=? "
            "AND save_state != 'purged'",
            (task_id,),
        ).fetchall()
        for row in rounds:
            conn.execute(
                "UPDATE execution_rounds SET save_state='purged', "
                "saved_at=?, updated_at=? WHERE task_id=? AND run_id=?",
                (now, now, task_id, row["run_id"]),
            )
        db.add_event(
            conn, now, "task_purged", task_id=task_id, actor="user",
            detail={"from": current, "to": "purged",
                    "purged_rounds": len(rounds)},
        )
        return {"ok": True, "deduplicated": False}
