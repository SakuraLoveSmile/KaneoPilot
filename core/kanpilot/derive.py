"""Read-side derivations: display_status, board_column, lost detection,
allowed_actions, and the Task/Request/Attempt/Event JSON views from
docs/protocol.md §2/§5.2."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import bridge, constants, db, util


def request_is_lost(request: sqlite3.Row, now) -> bool:
    return (
        request["status"] == "claimed"
        and util.lease_lost(request["lease_expires_at"], now)
    )


def request_display_status(request: sqlite3.Row, now) -> str:
    if request_is_lost(request, now):
        return "执行端失联"
    return request["status"]


def lost_request_for_task(
    conn: sqlite3.Connection, task_id: str, now
) -> Optional[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status='claimed' "
        "ORDER BY claimed_at DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        if util.lease_lost(row["lease_expires_at"], now):
            return row
    return None


def active_request_for_task(
    conn: sqlite3.Connection, task_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status IN ('queued','claimed') "
        "ORDER BY CASE status WHEN 'claimed' THEN 0 ELSE 1 END, "
        "priority ASC, created_at ASC LIMIT 1",
        (task_id,),
    ).fetchone()


def queued_request_for_task(
    conn: sqlite3.Connection, task_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status='queued' "
        "ORDER BY priority ASC, created_at ASC LIMIT 1",
        (task_id,),
    ).fetchone()


def queued_plan_request_for_task(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND kind='plan' AND status='queued' "
        "ORDER BY priority ASC, created_at ASC LIMIT 1", (task_id,),
    ).fetchone()


def _review_waiting_paused(conn, task):
    return db.task_auto_paused(task) and conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND kind='review' AND status='claimed'",
        (task["task_id"],),
    ).fetchone() is None


def task_display_status(
    conn: sqlite3.Connection, task: sqlite3.Row, now
) -> str:
    status = task["status"]
    if status == "needs_user":
        return "需人工"
    if status == "needs_plan":
        return "待规划"
    if status == "planning":
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "规划中"
    if status == "ready":
        queued = conn.execute(
            "SELECT 1 FROM requests WHERE task_id=? AND status='queued' "
            "AND kind='dispatch' LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        if queued:
            return "已排队"
        # v2（§5.22.4）：publish-plan 成功后待人工确认执行。
        if db.task_extra(task).get("plan_execution_hold"):
            return "待确认执行"
        return "就绪"
    if status in constants.DISPATCH_MID_STATUSES:
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "派发中"
    if status == "running":
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "执行中"
    if status == "in_review":
        if _review_waiting_paused(conn, task):
            return "待评审"
        return "执行端失联" if lost_request_for_task(conn, task["task_id"], now) else "评审中"
    if status == "done":
        return "已归档"
    return status


# ------------------------------------------------------------ board view

# task.status / failure_stage / task.stage -> board_column（同一口径：
# failure_stage 记录的是阻塞发生时的 task.status，task.stage 只做缺省回退，
# 见 protocol.md §2「看板列」与 state-model.md §2.1）。
_BOARD_COLUMN_FOR = {
    "needs_plan": "needs_plan", "planning": "needs_plan", "plan": "needs_plan",
    "ready": "todo", "dispatching": "todo", "prepared": "todo",
    "submitting": "todo", "dispatch": "todo",
    "running": "in_progress", "monitor": "in_progress",
    "in_review": "in_review", "review": "in_review",
    "done": "done",
}

# needs_user 按 failure_stage 归列后的子状态词表（阻塞归列）。
_BLOCKED_SUBSTATUS = {
    "needs_plan": "规划受阻",
    "todo": "派发受阻",
    "in_progress": "执行受阻",
    "in_review": "评审受阻",
    "done": "已完成",
}

# failed 列按 failure_code 的子状态词表；未识别码兜底「失败」。
_FAILED_SUBSTATUS = {
    "plan_failed": "规划失败",
    "validation_failed": "派发失败",
    "dispatch_failed": "派发失败",
    "execution_failed": "执行失败",
}

# 失联 blocked_reason 里的请求 kind 中文名。
_REQUEST_KIND_CN = {
    "plan": "规划",
    "dispatch": "派发",
    "monitor": "监控",
    "review": "评审",
    "delivery": "交付",
}

# 归档/回收站禁用状态 -> 中文（protocol.md §5.15.2）。
_SAVE_STATUS_CN = {
    "planning": "规划",
    "dispatching": "派发", "prepared": "派发", "submitting": "派发",
    "running": "执行",
    "in_review": "评审",
}

# exception.kind -> 中文 label（protocol.md §5.15.4）；kind='failed' 的 label
# 由 failure_code 经 _FAILED_SUBSTATUS 派生（规划失败/派发失败/执行失败/失败）。
_EXCEPTION_LABEL = {
    "send_unknown": "发送不明",
    "manual_block": "需人工处理",
    "executor_lost": "执行端失联",
    "review_blocked": "评审受阻",
    "observation_blocked": "监控异常",
    "review_rejected": "评审未通过·待修复",
}


def task_lifecycle_column(task: sqlite3.Row) -> str:
    """纯生命周期位置（protocol.md §5.15.4），无异常逻辑：
    needs_plan|planning→needs_plan；ready|dispatching|prepared|submitting→todo；
    running→in_progress；in_review→in_review；done→done；
    needs_user 按 failure_stage 归列，缺失回退 task.stage（同 v1.2 口径）。"""
    status = task["status"]
    if status != "needs_user":
        return _BOARD_COLUMN_FOR.get(status, "todo")
    column = _BOARD_COLUMN_FOR.get(task["failure_stage"] or "")
    if column is None:
        column = _BOARD_COLUMN_FOR.get(task["stage"], "todo")
    return column


def task_board_column(
    task: sqlite3.Row,
    conn: Optional[sqlite3.Connection] = None,
    now=None,
) -> str:
    """六列唯一分列依据（protocol.md §5.15.4）：异常一律进第六列 failed，
    正常列不再混入异常卡片。传入 conn/now 时检测失联（规则 3）；仅传 task
    行时只做行内派生（needs_user、评审未通过、status 直映射）。"""
    status = task["status"]
    if status == "needs_user":
        return "failed"
    if (
        status == "in_review"
        and db.task_extra(task).get("review_conclusion") == "changes_requested"
    ):
        return "failed"
    if (
        conn is not None and now is not None
        and lost_request_for_task(conn, task["task_id"], now) is not None
    ):
        return "failed"
    return task_lifecycle_column(task)


def task_exception_kind(
    conn: sqlite3.Connection, task: sqlite3.Row, now
) -> Optional[str]:
    """结构化异常 kind 判定（protocol.md §5.15.4），优先级自上而下：
    失联→executor_lost；评审未通过→review_rejected；needs_user 时
    send_state=unknown→send_unknown（优先于一切 code）、observation_blocked、
    review_blocked、failure_code∈失败集→failed、其余→manual_block。无异常 None。"""
    if lost_request_for_task(conn, task["task_id"], now) is not None:
        return "executor_lost"
    status = task["status"]
    if (
        status == "in_review"
        and db.task_extra(task).get("review_conclusion") == "changes_requested"
    ):
        return "review_rejected"
    if status == "needs_user":
        if task["send_state"] == "unknown":
            return "send_unknown"
        code = task["failure_code"]
        if code == "observation_blocked":
            return "observation_blocked"
        if code == "review_blocked":
            return "review_blocked"
        if code in constants.BOARD_FAILURE_CODES:
            return "failed"
        return "manual_block"
    return None


def _exception_detail(
    conn: sqlite3.Connection, task: sqlite3.Row, now, kind: str
) -> Optional[str]:
    if kind == "executor_lost":
        lost = lost_request_for_task(conn, task["task_id"], now)
        kind_cn = _REQUEST_KIND_CN.get(lost["kind"], lost["kind"])
        return f"执行端失联（{kind_cn}请求租约过期）"
    if kind == "review_rejected":
        return "评审未通过·待修复"
    # needs_user 各 kind：阻塞摘要优先，回退失败原因（同 blocked_reason）。
    return task["blocked_summary"] or task["failure_reason"]


def task_exception(
    conn: sqlite3.Connection, task: sqlite3.Row, now
) -> Optional[dict]:
    """结构化异常信息（protocol.md §5.15.4）；无异常返回 None。"""
    kind = task_exception_kind(conn, task, now)
    if kind is None:
        return None
    failure_code = task["failure_code"]
    if kind == "failed":
        label = _FAILED_SUBSTATUS.get(failure_code, "失败")
    else:
        label = _EXCEPTION_LABEL[kind]
    return {
        "kind": kind,
        "label": label,
        "severity": "failed" if kind == "failed" else "blocked",
        "detail": _exception_detail(conn, task, now, kind),
        "failure_code": failure_code,
        "fix_task_id": task["fix_task_id"] if kind == "review_rejected" else None,
    }


def archive_blocked_reason(
    conn: sqlite3.Connection, task: sqlite3.Row, now
) -> Optional[str]:
    """protocol.md §5.15.2 归档/回收站禁用条件；None 表示可移出看板。
    task.status ∈ ARCHIVE_BLOCKED_STATUSES、存在未解除失联请求、
    send_state='unknown' 或存在 claimed 请求（执行端已领取）任一即禁用（返回中文原因）。"""
    status = task["status"]
    if status in constants.ARCHIVE_BLOCKED_STATUSES:
        return f"任务正在{_SAVE_STATUS_CN.get(status, status)}中，暂不能移出看板"
    if lost_request_for_task(conn, task["task_id"], now) is not None:
        return "存在未解除的执行端失联请求，请先处理失联后再移出看板"
    if task["send_state"] == "unknown":
        return "发送结果不明，请先确认派发状态后再移出看板"
    claimed = conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND status='claimed' LIMIT 1",
        (task["task_id"],),
    ).fetchone()
    if claimed is not None:
        return "任务已被执行端领取，暂不能移出看板"
    return None


def move_out_eligibility(
    conn: sqlite3.Connection, task: sqlite3.Row, now
) -> dict:
    """T1 契约：任务摘要移出看板权限统一由核心计算。"""
    reason = archive_blocked_reason(conn, task, now)
    return {
        "allowed": reason is None,
        "reason": reason,
    }


def task_board_substatus(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    column: str,
    exception: Optional[dict] = None,
) -> str:
    """board_substatus 中文词表（protocol.md §2/§5.15.4）：
    第六列（failed）取 exception.label；前五列维持 v1.2 词表。"""
    if column == "failed":
        return exception["label"] if exception else "失败"
    if task["status"] == "needs_user":
        # needs_user 恒归 failed 列；此处仅防御性回退。
        return _BLOCKED_SUBSTATUS.get(column, "需人工处理")
    if column == "needs_plan":
        return "规划中" if task["status"] == "planning" else "待规划"
    if column == "todo":
        if task["status"] in constants.DISPATCH_MID_STATUSES:
            return "派发中"
        queued = conn.execute(
            "SELECT 1 FROM requests WHERE task_id=? AND status='queued' "
            "AND kind='dispatch' LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        return "已排队" if queued else "就绪"
    if column == "in_progress":
        return "执行中"
    if column == "in_review":
        return "待评审" if _review_waiting_paused(conn, task) else "评审中"
    return "已完成"  # done


def task_last_checked_at(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    active: Optional[sqlite3.Row],
) -> Optional[str]:
    """max(最近一条 monitor/review attempt finished_at、活动请求
    heartbeat_at、kaneo_checked_at)；全无则 null。"""
    row = conn.execute(
        "SELECT finished_at FROM attempts WHERE task_id=? "
        "AND kind IN ('monitor','review') AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (task["task_id"],),
    ).fetchone()
    candidates = (
        row["finished_at"] if row is not None else None,
        active["heartbeat_at"] if active is not None else None,
        task["kaneo_checked_at"],
    )
    best = None
    for raw in candidates:
        parsed = util.parse_ts(raw)
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return util.to_iso(best) if best is not None else None


def task_review_summary(task: sqlite3.Row) -> Optional[dict]:
    """当前周期已登记的评审结论（protocol.md §2）；新一轮 in_review
    起始由 advance-task/sync-tasks 清空 review_conclusion。"""
    extra = db.task_extra(task)
    if extra.get("review_conclusion") is None:
        return None
    return {
        "conclusion": extra["review_conclusion"],
        "registered_at": extra.get("review_registered_at"),
        "fix_task_id": task["fix_task_id"],
        "review_key": task["review_key"],
        "report_path": task["review_report_path"],
    }


def request_view(request: sqlite3.Row, now) -> dict:
    return {
        "request_id": request["request_id"],
        "idempotency_key": request["idempotency_key"],
        "task_id": request["task_id"],
        "stage": request["stage"],
        "kind": request["kind"],
        "status": request["status"],
        "display_status": request_display_status(request, now),
        "priority": request["priority"],
        "not_before": request["not_before"],
        "requested_by": request["requested_by"],
        "run_id": request["run_id"],
        "claimed_by": request["claimed_by"],
        "claimed_at": request["claimed_at"],
        "heartbeat_at": request["heartbeat_at"],
        "lease_expires_at": request["lease_expires_at"],
        "result": util.jload(request["result_json"]),
        "attempt_no": request["attempt_no"],
        "created_at": request["created_at"],
        "updated_at": request["updated_at"],
        # v2 §5.22.2：plan 请求是否已确认材料（UI「等待确认材料」标识）。
        "manual_authorized": (
            request["kind"] == "plan"
            and bool(db.request_payload(request).get("manual_authorized"))
        ),
    }


def task_summary(conn: sqlite3.Connection, task: sqlite3.Row, now) -> dict:
    active = active_request_for_task(conn, task["task_id"])
    queued = queued_request_for_task(conn, task["task_id"])
    queued_plan = queued_plan_request_for_task(conn, task["task_id"])
    next_claim_at = None
    if queued is not None and queued["not_before"] is not None:
        next_claim_at = queued["not_before"]
    active_view = None
    if active is not None:
        active_view = {
            "request_id": active["request_id"],
            "kind": active["kind"],
            "status": active["status"],
            "display_status": request_display_status(active, now),
            "priority": active["priority"],
            "claimed_by": active["claimed_by"],
            "heartbeat_at": active["heartbeat_at"],
            "lease_expires_at": active["lease_expires_at"],
            "not_before": active["not_before"],
        }
    board_column = task_board_column(task, conn, now)
    exception = task_exception(conn, task, now)
    blocked_reason = exception["detail"] if exception is not None else None
    from . import core, automation_policy
    automation = core.automation_of(task)
    policy = automation_policy.policy_for(conn, task["project_id"]) or {}
    automation.update({k: policy.get(k) for k in ("max_fix_rounds", "max_task_minutes", "max_task_cost_usd")}) if automation else None
    view = {
        "automation": automation or None,
        "delivery": core.delivery_of(task) or None,
        "review_history": db.task_extra(task).get("review_history") or [],
        "task_id": task["task_id"],
        "queued_plan_request_id": queued_plan["request_id"] if queued_plan is not None else None,
        "project_id": task["project_id"],
        "project_name": task["project_name"],
        "title": task["title"],
        "stage": task["stage"],
        "status": task["status"],
        "display_status": task_display_status(conn, task, now),
        "board_column": board_column,
        "lifecycle_column": task_lifecycle_column(task),
        "exception": exception,
        "board_substatus": task_board_substatus(
            conn, task, board_column, exception
        ),
        "last_checked_at": task_last_checked_at(conn, task, active),
        "review_summary": task_review_summary(task),
        "last_progress": task["last_progress"],
        "blocked_reason": blocked_reason,
        "next_claim_at": next_claim_at,
        "active_request": active_view,
        "kaneo_status": task["kaneo_status"],
        "kaneo_checked_at": task["kaneo_checked_at"],
        "priority": active["priority"] if active is not None else task["priority"],
        "plan_path": task["plan_path"],
        "updated_at": task["updated_at"],
        "save_state": db.task_save_state(task),
        "saved_at": task["saved_at"],
        "auto_advance_paused": task["auto_advance_paused"],
        "move_out_eligibility": move_out_eligibility(conn, task, now),
        # v2 §5.22.4：计划已发布但未授权执行（就绪待确认闸）。
        "plan_execution_hold": bool(
            db.task_extra(task).get("plan_execution_hold")
        ),
        # v2 §5.20.1：purged 任务只读审计详情。
        "read_only": db.task_is_purged(task),
    }
    # B5: desktop consumers decode these core-owned fields instead of rebuilding
    # the automation state machine in Swift. The attention item uses precisely
    # the same evidence/actions builder as attention-list.
    from .engine import decide, attention
    phase, condition = decide.phase_condition(task["status"]) or (None, None)
    if task["status"] == "in_review" and (
            db.task_extra(task).get("review_conclusion") == "pass"
            or core.delivery_of(task).get("pr_number")
            or core.delivery_of(task).get("pushed_sha")):
        phase, condition = "delivery", "syncing"
    view.update(phase=phase, condition=condition, attention=None)
    if task["status"] == "needs_user" and db.task_is_active(task):
        from .engine.commands import _task_evidence
        source = dict(view, failure_code=task["failure_code"],
                      failure_stage=task["failure_stage"],
                      failure_reason=task["failure_reason"],
                      blocked_summary=task["blocked_summary"],
                      blocked_at=task["blocked_at"],
                      allowed_actions=allowed_actions(conn, task, now),
                      evidence=_task_evidence(conn, task["task_id"],
                                              task["blocked_at"] or task["updated_at"]))
        view["attention"] = attention.build_item(source, util.to_iso(now))
    if db.task_is_purged(task) and not view["blocked_reason"]:
        view["blocked_reason"] = "任务已永久从列表移除（只读审计）"
    return view


def attempt_view(row: sqlite3.Row) -> dict:
    return {
        "request_id": row["request_id"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "kind": row["kind"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "outcome": row["outcome"],
        "send_state": row["send_state"],
        "note": row["note"],
        "evidence": util.jload(row["evidence_json"]),
    }


def allowed_actions(conn: sqlite3.Connection, task: sqlite3.Row, now) -> list:
    """Resolve-action availability per protocol.md §4.10 plus §5.15.6
    archive/trash/restore/resume-auto and v2 §5.20/§5.22 gating."""
    status = task["status"]
    send_state = task["send_state"]
    if db.task_is_purged(task):
        # v2 §5.20.1：purged 终态无任何动作（read_only 审计视图）。
        return []
    actions = ["recheck"]
    if not db.task_is_active(task):
        # 归档任务：仅提供恢复（§5.15.6）；回收站任务追加永久移除（v2）。
        actions.append("restore")
        if db.task_save_state(task) == "trashed":
            actions.append("purge")
    else:
        # 可移出看板时才提供归档/回收站（§5.15.6）。
        if archive_blocked_reason(conn, task, now) is None:
            actions.extend(["archive", "trash"])
        hold = bool(db.task_extra(task).get("plan_execution_hold"))
        dispatch_sent = bool(task["sent"]) or send_state not in (None, "not_sent")
        if status == "ready" and hold and not dispatch_sent:
            # v2 §5.22.4：待确认执行——以 confirm_dispatch 替代普通排队。
            actions.append("confirm_dispatch")
        elif (status in constants.ENQUEUE_INFER and status != "needs_plan"
              and not (status == "ready" and dispatch_sent)
              and not (status == "in_review" and db.task_auto_paused(task))):
            actions.append("enqueue")
        from . import review_recovery
        from . import review_resolution
        if review_resolution.candidate(conn, task) is not None:
            actions.append("resolve_review_blocker")
        if review_recovery.candidate(conn, task) is not None:
            actions.append("recover_review")
        if status == "needs_plan":
            # v2 §5.22.2：规划改为手动门控——面板准备材料后经 start-plans。
            actions.append("prepare_plan")
        if queued_request_for_task(conn, task["task_id"]) is not None:
            actions.append("set-priority")
        if queued_plan_request_for_task(conn, task["task_id"]) is not None:
            actions.append("cancel-request")
        if lost_request_for_task(conn, task["task_id"], now) is not None:
            actions.append("release_lost")
        if status == "needs_user":
            from . import core, cmd_queue
            code = task["failure_code"]
            if code == "verification_unconfirmed":
                actions.append("accept_verification")
                from . import verification_recovery
                if verification_recovery.candidate(conn, task):
                    actions.append("run_verification_checks")
                    actions.append("reverify")
            if cmd_queue.execution_retry_candidate(conn, task):
                actions.append("resume")
            if core.is_automation_managed(task) and (code in cmd_queue._AUTO_RESUME_TARGETS or code in ("fix_rounds_exhausted", "auto_budget_exceeded", "automation_stopped", "remote_conflict")):
                if code != "remote_conflict" or task["failure_stage"] != "dispatch":
                    actions.append("resume")
            from . import plan_answers
            clarification = plan_answers.candidate(conn, task)
            if clarification is not None:
                actions.append("answer_plan_questions")
            if clarification is None and send_state in (None, "not_sent") and code not in constants.AUTOMATION_FAILURE_CODES:
                actions.append("retry")
            elif clarification is None and send_state == "sent" and not core.is_automation_managed(task) and cmd_queue._retry_target(conn, task)[1] == "review":
                actions.append("retry")
            if send_state == "unknown":
                actions.extend(["restore", "accept_duplicate", "mark_sent"])
            if not (core.is_automation_managed(task) and send_state == "unknown"):
                actions.append("replan")
            actions.append("dismiss")
        if status in ("in_review", "needs_user", "running"):
            actions.append("mark_done")
    from . import core  # noqa: F401  (局部导入，避免与 bridge/db 形成循环)
    # resume-auto is the reverse entry of ``resolve {action:"dismiss"}``
    # (plan D4: manual_takeover is a *reversible* pause). It must be offered
    # whenever the task is under manual takeover, not only when it is also
    # paused: ``dismiss`` sets ``auto_advance_paused=1`` only for review-block
    # failures, so a non-review takeover (e.g. empty-diff remediation
    # exhausted) lands in ``ready`` unpaused and the desktop button would
    # never appear — leaving the one-way door D4 diagnoses half open.
    if db.task_auto_paused(task) or core.automation_of(task).get("manual_takeover"):
        actions.append("resume-auto")
    return actions


def task_detail(conn: sqlite3.Connection, task: sqlite3.Row, now) -> dict:
    detail = task_summary(conn, task, now)
    from . import review_recovery
    detail["review_recovery"] = review_recovery.candidate(conn, task)
    from . import review_resolution
    detail["review_resolution"] = review_resolution.candidate(conn, task)
    if detail["review_resolution"] is not None:
        try:
            path = util.inside(Path(task["review_report_path"]), constants.review_reports_root(), "review report")
            raw, _sha = review_resolution._read(path, task["review_report_sha256"])
            detail["review_resolution"]["summary"] = review_resolution.report_summary(raw)
        except (util.KanpilotError, OSError):
            detail["review_resolution"]["summary"] = "原评审报告不可核验，请检查报告文件后再处置。"
    from . import plan_answers
    detail["plan_clarification"] = plan_answers.view(conn, task)
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id=? ORDER BY id", (task["task_id"],)
    ).fetchall()
    detail.update(
        {
            "extra": db.task_extra(task),
            "repository": task["repository"],
            "plan_sha256": task["plan_sha256"],
            "plan_kind": task["plan_kind"],
            "requirements_sha256": task["requirements_sha256"],
            "source_sha256": task["source_sha256"],
            "source_path": task["source_path"],
            "review_report_path": task["review_report_path"],
            "review_report_sha256": task["review_report_sha256"],
            "review_key": task["review_key"],
            "review_source_task_id": task["review_source_task_id"],
            "fix_task_id": task["fix_task_id"],
            "session_id": task["session_id"],
            "session_title": task["session_title"],
            "session_marker": task["session_marker"],
            "send_state": task["send_state"],
            "sent": task["sent"],
            "run_id": task["run_id"],
            "failure_code": task["failure_code"],
            "failure_reason": task["failure_reason"],
            "failure_stage": task["failure_stage"],
            "manual_recovery_required": task["manual_recovery_required"],
            "allowed_actions": allowed_actions(conn, task, now),
            "attempts": [attempt_view(row) for row in attempts],
            "links": task_links(task),
            # v1.5（protocol §5.18.5）：当前轮次索引视图（worktree_path /
            # backend / run_status / config 快照）；无轮次（历史任务）→ null。
            "execution_round": _execution_round(conn, task),
            "verification_checks": _verification_checks(task),
        }
    )
    return detail


def _verification_checks(task):
    from . import verification_checks
    return verification_checks.public_state(task)


def _execution_round(conn: sqlite3.Connection, task: sqlite3.Row):
    from . import round_store  # 局部导入（骨架期避免循环）

    if not task["run_id"]:
        return None
    return round_store.round_view(
        round_store.get_round(conn, task["task_id"], task["run_id"])
    )


def task_links(task: sqlite3.Row) -> dict:
    """Best-effort external links; kaneo_task_url derived from credentials."""
    links = {"kaneo_task_url": None, "plan_file": None, "report_file": None}
    if task["plan_path"]:
        links["plan_file"] = task["plan_path"]
    if task["review_report_path"]:
        links["report_file"] = task["review_report_path"]
    try:
        base_url, _token = bridge.load_credentials()
    except Exception:
        base_url = None
    if base_url:
        web = base_url.rstrip("/")
        if web.endswith("/api"):
            web = web[: -len("/api")]
        links["kaneo_task_url"] = f"{web}/task/{task['task_id']}"
    return links


# ================================================================ v2 请求详情

_REQUEST_KIND_LABEL = {
    "plan-scan": "看板扫描",
    "plan": "规划",
    "dispatch": "派发",
    "monitor": "监控",
    "review": "评审",
    "delivery": "交付",
}


def request_allowed_actions(
    conn: sqlite3.Connection, request: sqlite3.Row, task, now
) -> list:
    """get-request 的合法动作词表（protocol §5.21.1）：核心按当前任务状态与
    失败阶段派生，UI 只渲染不自判。"""
    if db.task_is_purged(task):
        return []
    status = request["status"]
    kind = request["kind"]
    actions = ["view"]
    if status == "queued":
        actions.append("set-priority")
        if kind == "plan":
            actions.append("cancel-request")
            if not request_is_authorized_plan(request):
                actions.append("prepare_plan")
        return actions
    if status == "claimed":
        if request_is_lost(request, now):
            actions.append("release_lost")
        return actions
    if status == "failed":
        if kind == "plan-scan":
            actions.append("fetch_todos")
            return actions
        if kind == "plan":
            actions.append("prepare_plan")
        if task is not None and task["status"] == "needs_user":
            send_state = task["send_state"]
            from . import verification_recovery
            if verification_recovery.request_candidate(conn, task, request):
                actions.append("reverify")
            if send_state in (None, "not_sent") and kind != "plan":
                actions.append("retry")
            actions.append("replan")
            if lost_request_for_task(conn, task["task_id"], now) is not None:
                actions.append("release_lost")
        elif task is not None and send_state_is_unknown(task):
            pass  # 发送不明只经任务级决议，不给请求级重试。
        return actions
    if status in ("succeeded", "cancelled"):
        if kind == "plan-scan" and request["task_id"] is None:
            actions.append("fetch_todos")
        return actions
    return actions


def send_state_is_unknown(task) -> bool:
    return task is not None and task["send_state"] == "unknown"


def request_is_authorized_plan(request: sqlite3.Row) -> bool:
    """v2 §5.22.2：payload.manual_authorized 缺失即未授权（等待确认材料）。"""
    if request["kind"] != "plan":
        return False
    return bool(db.request_payload(request).get("manual_authorized"))


def request_attention_state(
    conn: sqlite3.Connection, request: sqlite3.Row, task, now
) -> str:
    """actionable|historical|informational（protocol §5.21.1）。"""
    status = request["status"]
    # §5.21.1：purged 任务的所有请求归历史（只读审计），不冒充当前异常。
    if db.task_is_purged(task):
        return "historical"
    if status in ("queued", "claimed"):
        return "actionable"
    if status == "failed":
        # 当前失败需处理：任务仍阻塞且指向本条失败链路。
        if task is not None and task["status"] == "needs_user":
            return "actionable"
        if (
            task is not None
            and task["send_state"] == "unknown"
            and request["kind"] == "dispatch"
        ):
            return "actionable"
        return "historical"
    if status == "cancelled":
        return "historical"
    return "informational"  # succeeded


def request_blocked_reason(
    conn: sqlite3.Connection, request: sqlite3.Row, task, now
) -> Optional[str]:
    """请求级中文阻塞原因（§5.21.1）；None 表示无。"""
    if db.task_is_purged(task):
        return "任务已永久从列表移除（只读审计）"
    status = request["status"]
    if status == "queued":
        if request["kind"] == "plan" and not request_is_authorized_plan(request):
            return "等待确认材料：请在规划面板准备并提交本轮规划输入"
        if (
            request["kind"] == "dispatch"
            and task is not None
            and db.task_extra(task).get("plan_execution_hold")
        ):
            return "计划已发布但未确认执行：请先在任务详情确认并安排执行"
    if status == "claimed" and request_is_lost(request, now):
        return "执行端失联：租约已过期，请处理失联后再继续"
    if request["kind"] == "plan-scan":
        return "看板扫描已停用（v2 起规划为手动触发），可经规划面板获取 To Do"
    return None


def request_log_path(
    conn: sqlite3.Connection, request: sqlite3.Row
) -> Optional[str]:
    """该请求关联轮次 events.jsonl 的绝对路径（execution_rounds/运行记录）。"""
    run_id = request["run_id"]
    task_id = request["task_id"]
    if not run_id or not task_id:
        return None
    from . import round_store  # 局部导入避免循环

    row = round_store.get_round(conn, task_id, run_id)
    if row is None:
        return None
    configured = db.get_setting(conn, "session_runs_root") or ""
    if isinstance(configured, str) and configured:
        from pathlib import Path as _P

        root = _P(configured).expanduser()
    else:
        from pathlib import Path as _P

        root = constants.default_session_runs_root()
    candidate = _P(root) / task_id / run_id / constants.PLANNER_EVENTS_NAME
    return str(candidate) if candidate.is_file() else None


def request_detail(
    conn: sqlite3.Connection, request: sqlite3.Row, now
) -> dict:
    """get-request 响应（protocol §5.21.1）。"""
    task = (
        db.get_task(conn, request["task_id"]) if request["task_id"] else None
    )
    purged_task = db.task_is_purged(task)
    task_summary_view = (
        task_summary(conn, task, now)
        if task is not None and not purged_task
        else None
    )
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE request_id=? ORDER BY id",
        (request["request_id"],),
    ).fetchall()
    actions = request_allowed_actions(conn, request, task, now)
    attention = request_attention_state(conn, request, task, now)
    blocked = request_blocked_reason(conn, request, task, now)
    # 终态且无可执行动作（除纯查看/重新获取外）→ 只读。
    read_only = purged_task or (
        request["status"] in ("succeeded", "cancelled", "failed")
        and not any(
            action not in ("view", "fetch_todos") for action in actions
        )
    )
    return {
        "request": request_view(request, now),
        "task": task_summary_view,
        "attempts": [attempt_view(row) for row in attempts],
        "allowed_actions": actions,
        "blocked_reason": blocked,
        "read_only": read_only,
        "log_path": request_log_path(conn, request),
        "attention_state": attention,
        "payload": db.request_payload(request) or None,
    }


# ===================================================== v2 事件视图（§5.21.2）

_EVENT_KIND_CN = {
    "automation_adopted": "自动接收任务", "auto_confirm_skipped": "跳过自动确认",
    "fix_round_started": "同任务修复轮开始", "delivery_committed": "分支改动已提交",
    "delivery_pushed": "交付分支已推送", "pr_opened": "PR 已创建", "kaneo_comment_posted": "PR 链接已回写",
    "pr_merged": "PR 已合并", "pr_closed": "PR 已关闭", "cost_recorded": "运行成本登记",
    "requirements_accepted": "已接受远端需求", "automation_stopped": "自动处理已停止",
    "execution_retry_requested": "已确认沿用计划重新执行",
    "lost_request_released": "失联观察请求已释放",

    "task_created": "任务创建",
    "task_updated": "任务信息更新",
    "requirements_changed": "需求指纹变化",
    "kaneo_observed": "远端状态观测",
    "plan_published": "计划发布",
    "enqueued": "请求入队",
    "claimed": "请求领取",
    "heartbeat": "执行心跳",
    "status_change": "任务状态变化",
    "dispatch_prepared": "派发准备",
    "kaneo_transition": "远端状态迁移",
    "dispatch_sent": "派发已发送",
    "dispatch_failed": "派发失败",
    "reported": "执行端汇报",
    "review_registered": "评审登记",
    "fix_task_created": "修复任务创建",
    "resolved": "人工决议",
    "paused": "自动推进暂停",
    "unpaused": "自动推进恢复",
    "priority_changed": "优先级调整",
    "migrated": "数据迁移",
    "rechecked": "重新校验",
    "request_cancelled": "请求取消",
    "failed_request_dismissed": "失败记录已移除",
    "task_archived": "任务归档",
    "task_trashed": "任务移入回收站",
    "task_restored": "任务恢复",
    "review_stage_recovered": "评审阶段已恢复",
    "review_result_cleared": "旧评审结果已留存",
    "review_repair_prepared": "评审修复材料已准备",
    "verification_checks_started": "验证检查已开始",
    "verification_checks_completed": "验证检查已完成",
    "review_supplemented": "评审证据已补充",
    "task_reconnected": "任务重新接入待规划",
    "session_trashed": "会话移入回收站",
    "session_restored": "会话恢复",
    "round_started": "轮次启动",
    "exec_config_updated": "执行配置更新",
    "task_purged": "任务永久从列表移除",
    "session_purged": "会话永久从列表移除",
    "plan_input_prepared": "规划材料就绪",
    "plan_started": "规划已排队",
    "plan_ready": "规划产出就绪",
    "plan_needs_input": "规划需要补充输入",
    "plan_answers_submitted": "规划答案已提交",
    # v3 T5（§5.23.10）：Goal 报告完成判定留痕。
    "goal_report_evaluated": "Goal 报告完成判定",
    # v3 修复：以下 kind 由核心自身发出（cmd_dispatch/cmd_config/cmd_planner），
    # 但此前未登记在本表，记录页把它们显示成「未识别事件（…）」。表必须覆盖
    # 核心实际写入的全部 kind，未知 kind 仍由调用方原样保留（§5.21.2）。
    "round_observe": "观察者续接",
    "round_start_failed": "轮次启动失败",
    "review_config_updated": "评审配置更新",
    "planner_config_updated": "规划配置更新",
}

_EVENT_RESULTS = {
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "needs_user": "needs_user",
}


def _event_summary(kind: str, detail) -> str:
    """中文一句话摘要；未知 kind → 中性文案 + 原始类型（§5.21.2）。"""
    if not isinstance(detail, dict):
        detail = {}
    label = _EVENT_KIND_CN.get(kind)
    if label is None:
        return f"未识别事件（{kind}）"
    if kind == "status_change":
        return f"任务状态：{detail.get('from')} → {detail.get('to')}"
    if kind == "enqueued":
        kind_cn = _REQUEST_KIND_LABEL.get(detail.get("kind"), detail.get("kind"))
        return f"请求入队：{kind_cn or detail.get('kind')}"
    if kind == "claimed":
        kind_cn = _REQUEST_KIND_LABEL.get(detail.get("kind"), detail.get("kind"))
        return f"执行端领取（{kind_cn or '请求'}）"
    if kind == "reported":
        outcome = detail.get("outcome")
        note = detail.get("note")
        base = {"succeeded": "执行成功", "failed": "执行失败",
                "needs_user": "需要人工"}.get(outcome, "执行端汇报")
        return f"{base}：{note}" if note else base
    if kind == "kaneo_observed":
        return f"远端观测：{detail.get('from')} → {detail.get('to')}"
    if kind == "kaneo_transition":
        return f"远端迁移：{detail.get('from')} → {detail.get('to')}"
    if kind == "plan_published":
        return f"计划发布（{detail.get('plan_kind') or 'standard'}）"
    if kind == "dispatch_failed":
        return f"派发失败：{detail.get('reason') or detail.get('send_state') or ''}"
    if kind == "review_registered":
        return f"评审登记：{detail.get('conclusion')}"
    if kind == "resolved":
        return f"人工决议：{detail.get('action')}"
    if kind == "requirements_changed":
        return "需求指纹变化，需重新规划"
    if kind == "fix_task_created":
        return f"创建修复任务 {detail.get('fix_task_id')}"
    if kind == "failed_request_dismissed":
        return "已从待处理移除失败记录，历史证据保留"
    if kind == "request_cancelled":
        return f"请求取消（{detail.get('reason') or detail.get('via') or ''}）"
    if kind in ("paused", "unpaused"):
        return f"{'暂停' if kind == 'paused' else '恢复'}自动推进（{detail.get('stage') or '全局'}）"
    if kind == "priority_changed":
        return f"优先级调整为 {detail.get('priority')}"
    if kind == "task_purged":
        return "任务永久从列表移除（保留历史与文件）"
    if kind == "session_purged":
        return "会话永久从列表移除（保留历史与文件）"
    if kind == "plan_input_prepared":
        return f"规划材料就绪（{detail.get('input_id', '')[:8]}）"
    if kind == "plan_started":
        return "规划请求已排队（手动授权）"
    if kind == "plan_ready":
        return "规划产出计划，等待确认执行"
    if kind == "plan_answers_submitted":
        return detail.get("note") or "用户答案已保存，等待继续规划"
    if kind == "plan_needs_input":
        return "规划需要补充输入"
    if kind == "goal_report_evaluated":
        decision = detail.get("decision")
        base = {"complete": "判定完成", "incomplete": "判定未完成",
                "uncertain": "判定不确定"}.get(decision, "判定")
        reason = detail.get("reason")
        return f"Goal 报告{base}：{reason}" if reason else f"Goal 报告{base}"
    reason = detail.get("reason") or detail.get("detail")
    return f"{label}：{reason}" if reason else label


def event_view(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    task_cache: Optional[dict] = None,
    request_cache: Optional[dict] = None,
) -> dict:
    """list-events 扩展视图（§5.21.2）：原字段 + task_title/project_name/
    stage/result/summary。标题回填：任务实体优先 → 来源快照 → 占位。"""
    task_cache = task_cache if task_cache is not None else {}
    request_cache = request_cache if request_cache is not None else {}
    task_id = row["task_id"]
    request_id = row["request_id"]

    task = None
    if task_id is not None:
        if task_id not in task_cache:
            task_cache[task_id] = db.get_task(conn, task_id)
        task = task_cache[task_id]
    request = None
    if request_id is not None:
        if request_id not in request_cache:
            request_cache[request_id] = db.get_request(conn, request_id)
        request = request_cache[request_id]

    # 标题回填（§5.21.2）：仍保留的任务实体（非 purged）→ source_path 快照
    # → 占位短 id；purged 任务实体视为已退出保留集合。
    task_title = None
    project_name = None
    if task is not None and not db.task_is_purged(task):
        task_title = task["title"]
        project_name = task["project_name"]
    if not task_title and task is not None and task["source_path"]:
        try:
            snapshot = util.jload(
                __import__("pathlib").Path(task["source_path"]).read_text(
                    encoding="utf-8"
                )
            )
            if isinstance(snapshot, dict):
                snap_task = snapshot.get("task")
                if isinstance(snap_task, dict):
                    task_title = snap_task.get("title")
        except Exception:
            pass
    if not project_name and task is not None and task["source_path"]:
        try:
            snapshot = util.jload(
                __import__("pathlib").Path(task["source_path"]).read_text(
                    encoding="utf-8"
                )
            )
            if isinstance(snapshot, dict):
                snap_task = snapshot.get("task")
                if isinstance(snap_task, dict):
                    project_name = (
                        snap_task.get("project_name")
                        or snap_task.get("projectName")
                    )
        except Exception:
            pass
    if not task_title and task_id:
        task_title = f"未保留标题 · {str(task_id)[:8]}"
    if not project_name and task is not None and task["project_id"]:
        project_name = task["project_id"]
    if not project_name and task_id:
        project_name = "未保留项目"

    # stage：请求 stage 优先，回退任务 stage。
    stage = None
    if request is not None and request["stage"]:
        stage = request["stage"]
    elif task is not None:
        stage = task["stage"]

    # result：关联请求终态 > 事件语义 > None。
    result = None
    if request is not None and request["status"] in _EVENT_RESULTS:
        result = _EVENT_RESULTS[request["status"]]
    elif request is not None and request["status"] == "claimed" and \
            util.lease_lost(request["lease_expires_at"], util.now_dt()):
        result = "lost"
    if result == "failed" and request is not None:
        # needs_user 存储形态是 status=failed + result.outcome=needs_user。
        prev = util.jload(request["result_json"]) or {}
        if prev.get("outcome") == "needs_user":
            result = "needs_user"

    if row["kind"] == "plan_answers_submitted":
        # This event records the completed user decision, independent of the
        # newly queued planner request's later result.
        stage, result = "plan", "succeeded"
    detail = util.jload(row["detail_json"])
    return {
        "id": row["id"],
        "at": row["at"],
        "task_id": task_id,
        "request_id": request_id,
        "kind": row["kind"],
        "actor": row["actor"],
        "detail": detail,
        "task_title": task_title,
        "project_name": project_name,
        "stage": stage,
        "result": result,
        "summary": _event_summary(row["kind"], detail),
    }
