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
  （resume_auto=true 时 0）；``resume_auto=true`` 同时是 ``resolve
  {action:"dismiss"}`` 人工接管的**逆向入口**——清 ``manual_takeover``、
  交付模式从 manual 回 pr（仅当 PR 身份字段齐全），让 take_over → resume_auto
  → 自动交付到 done 闭环（scenarios.md 场景 16）。本地重算计划/来源哈希并尝试
  远端 get-task 观察（远端不可达→warning 不阻断）；返回刷新后 task。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import constants, core, db, derive, planfile, project_config, remote, util


_RECONNECT_SAVE_STATES = {"archived", "trashed", "purged"}
_RECONNECT_TERMINAL_OUTCOMES = {"completed", "failed", "error", "aborted", "timeout", "truncated"}


def _stored_execution_detail(conn, task, now) -> dict:
    """Read existing local evidence only; never refresh a runtime or write an index."""
    from . import cmd_session, dsh_events, headless_adapter, round_store
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    if row is None:
        return {}
    source = headless_adapter.resolve_source(conn)
    job, directory = cmd_session._identity_job(source, task["task_id"], task["run_id"])
    if job is None:
        return {}
    events, error = dsh_events.iter_events(directory / "events.jsonl")
    if error:
        return {}
    detail = dsh_events.round_detail(row, job, events) or {}
    detail["run_status"] = round_store.derive_run_status(row, job, now, round_store.round_timeout_seconds(conn))
    return detail


def reconnect_blocked_reason(conn, task, mapping, now, remote_task, execution_detail=None) -> Optional[str]:
    """Shared read-only eligibility; commands repeat it inside their write txn."""
    if task is None or db.task_save_state(task) not in _RECONNECT_SAVE_STATES:
        return "任务已在活跃看板，不能重新接入"
    if remote_task.get("id") != task["task_id"] or remote_task.get("projectId") != task["project_id"]:
        return "远端任务或项目身份已变化，请重新获取 To Do"
    if remote_task.get("status") not in constants.TODO_SLUGS:
        return "远端任务已不在 To Do，不能重新接入"
    if not mapping or mapping.get("mapping_state") != "available":
        return (mapping or {}).get("blocked_reason") or "项目仓库映射不可用，不能重新接入"
    if derive.lost_request_for_task(conn, task["task_id"], now) is not None:
        return "存在未解除的执行端失联请求，请先处理失联"
    if conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
        (task["task_id"],),
    ).fetchone() is not None:
        return "存在排队/领取中的请求，不能重新接入"
    if task["send_state"] == "unknown":
        return "发送结果不明，请先确认派发状态"
    if task["status"] in constants.ARCHIVE_BLOCKED_STATUSES:
        return "任务仍在规划、派发、执行或评审中，不能重新接入"
    if task["sent"] or task["send_state"] == "sent":
        detail = _stored_execution_detail(conn, task, now) if execution_detail is None else execution_detail
        if (detail.get("run_status") not in ("finished", "failed")
                or (detail.get("terminal") or {}).get("outcome") not in _RECONNECT_TERMINAL_OUTCOMES):
            return "旧执行轮次尚无可核验终态，请先确认会话结束并核验旧执行"
    return None


def cmd_reconnect_task(ctx, payload: dict) -> dict:
    """Explicitly reconnect a removed To Do for a fresh, manually authorized plan."""
    conn, now = ctx.conn, ctx.now_iso
    task_id, expected = payload["task_id"], payload["expected_save_state"]
    if expected not in _RECONNECT_SAVE_STATES:
        raise util.bad_request("expected_save_state 必须为 archived、trashed 或 purged")
    task = db.require_task(conn, task_id)
    task_snapshot = dict(task)
    mapping = project_config.get_effective_mapping(conn, task["project_id"])
    execution_detail, round_snapshot = {}, None
    if task["sent"] or task["send_state"] == "sent":
        from . import cmd_session, round_store
        old_round = round_store.get_round(conn, task_id, task["run_id"])
        if old_round is None:
            raise util.KanpilotError("conflict", "旧执行轮次尚无可核验终态，请先恢复会话观察")
        view = cmd_session.cmd_get_session(ctx, {"task_id": task_id, "run_id": task["run_id"]})
        execution_detail = view.get("round_detail") or {}
        refreshed = round_store.get_round(conn, task_id, task["run_id"])
        round_snapshot = dict(refreshed) if refreshed is not None else None
        if round_snapshot is None:
            raise util.KanpilotError("conflict", "旧执行轮次证据已变化，请重新核验")
    try:
        live = remote.get_task(remote.get_client(), task_id)
    except Exception as exc:
        raise util.KanpilotError("remote_unavailable", "远端任务读取失败，请稍后重新获取 To Do") from exc
    remote_task = live.get("task") or {}
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        current = db.task_save_state(task)
        if current != expected:
            raise util.KanpilotError("conflict", "任务保存状态已变化，请重新获取 To Do")
        if dict(task) != task_snapshot:
            raise util.KanpilotError("conflict", "任务在核验期间已变化，请重新获取 To Do")
        if round_snapshot is not None:
            refreshed = round_store.get_round(conn, task_id, task["run_id"])
            if refreshed is None or dict(refreshed) != round_snapshot:
                raise util.KanpilotError("conflict", "旧执行轮次证据已变化，请重新核验")
        fresh_mapping = project_config.get_effective_mapping(conn, task["project_id"])
        if fresh_mapping != mapping:
            raise util.KanpilotError("conflict", "项目映射已变化，请重新获取 To Do")
        reason = reconnect_blocked_reason(conn, task, fresh_mapping, ctx.now, remote_task, execution_detail)
        if reason:
            raise util.KanpilotError("conflict", reason)
        # Snapshot the old live pointers for audit, without changing old requests,
        # rounds, source snapshots, plans or any files.
        pointers = dict(core.clear_block_fields())
        pointers.update(
            run_id=None, session_marker=None, session_id=None, session_title=None,
            send_state=None, sent=0, sent_at=None, dispatch_lease_expires_at=None,
            transition_started_at=None, transition_verified_at=None,
            ui_evidence=None, send_evidence=None, lock_retained=0,
            review_report_path=None, review_report_sha256=None, review_key=None,
            review_source_task_id=None, fix_task_id=None, last_progress=None,
            resolution=None,
            plan_path=None, plan_sha256=None, plan_kind=None, plan_generated_at=None,
            planner_model=None, reasoning_effort=None,
        )
        previous = {key: task[key] for key in pointers}
        previous.update({key: task[key] for key in (
            "plan_path", "plan_sha256", "source_path", "source_sha256",
            "requirements_sha256", "repository", "plan_kind",
        )})
        previous["delivery"] = core.delivery_of(task)
        db.update_task(
            conn, task_id, now, **pointers, save_state="active", saved_at=None,
            status="needs_plan", stage="plan", auto_advance_paused=1,
            kaneo_status=remote_task["status"], kaneo_checked_at=now,
        )
        db.set_extra(
            conn, db.require_task(conn, task_id), now,
            reconnect={"awaiting_plan": True, "reconnected_at": now},
        )
        # Manual takeover disables the old automation intent while retaining its
        # recorded costs. Old delivery identity is preserved in the event only;
        # a new manual plan uses the current mapped repository.
        core.update_automation(
            conn, task, now, manual_takeover=True, plan_wanted=False,
            replan_requested_at=None, replan_remote_status=None,
            replan_worktree_sha256=None,
        )
        db.set_extra(conn, db.require_task(conn, task_id), now, delivery={"mode": "manual"})
        db.add_event(
            conn, now, "task_reconnected", task_id=task_id, actor="user",
            detail={"from": current, "to": "active", "previous": previous},
        )
        return {"ok": True, "task": derive.task_detail(conn, db.require_task(conn, task_id), ctx.now)}


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


# PR 交付身份字段（delivery.mode="pr" 的最小可发布集合）。
_PR_DELIVERY_KEYS = ("branch", "base_branch", "base_sha", "head_sha",
                     "worktree_path")


def _manual_takeover_of(task) -> bool:
    """任务是否处于人工接管（``resolve {action:"dismiss"}`` 写入）。"""
    return core.automation_of(task).get("manual_takeover") is True


def _release_manual_takeover(conn, task, now) -> bool:
    """把人工接管的任务交回自动化（``restore-task {resume_auto:true}``）。

    ``dismiss`` 写 ``automation.manual_takeover=True`` + ``delivery.mode="manual"``
    （分支直提、不走 PR）并暂停自动推进。接管是用户决定，交回同样必须是显式动作：
    只有 ``resume_auto=true`` 才走这里，普通恢复/幂等恢复不动自动化归属。

    交付模式只在 PR 身份字段齐全时回 ``pr``——否则保持 manual，让用户显式处理，
    不造出「PR 模式但不可发布」的半态。返回是否有实际变更。
    """
    changed = False
    if _manual_takeover_of(task):
        core.update_automation(conn, task, now, manual_takeover=False)
        changed = True
    delivery = core.delivery_of(task)
    if delivery.get("mode") == "manual":
        if all(delivery.get(key) for key in _PR_DELIVERY_KEYS):
            core.update_delivery(conn, task, now, mode="pr")
            changed = True
    if changed:
        db.add_event(conn, now, "automation_resumed", task_id=task["task_id"],
                     actor="user", detail={"via": "resume_auto"})
    return changed


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
        takeover = _manual_takeover_of(task)
        if deduplicated and resume_auto and (db.task_auto_paused(task) or takeover):
            # Explicit resume on an active task must release its task-level pause
            # (and hand a manually taken-over task back to automation). An
            # ordinary idempotent restore does not authorize automatic work.
            if task["status"] == "in_review":
                from .cmd_queue import clear_review_result_inline
                clear_review_result_inline(conn, task, now, actor="user")
            db.update_task(conn, task_id, now, auto_advance_paused=0)
            if takeover:
                _release_manual_takeover(conn, db.require_task(conn, task_id), now)
            db.add_event(
                conn, now, "task_restored", task_id=task_id, actor="user",
                detail={"from": "active", "to": "active", "resume_auto": True},
            )
            deduplicated = False
        elif not deduplicated:
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
            if resume_auto:
                _release_manual_takeover(conn, db.require_task(conn, task_id), now)
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
