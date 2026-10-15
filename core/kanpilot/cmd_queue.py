"""Queue-management commands: enqueue / set-priority / set-paused / resolve /
sync-tasks (protocol.md §5.3/§5.4/§4.9/§4.10)."""

from __future__ import annotations

from typing import Optional

from . import constants, core, db, derive, util

# 非活跃 save_state -> 中文（§5.15.3 冲突原因）。
_SAVE_STATE_CN = {"archived": "归档", "trashed": "移入回收站", "purged": "永久从列表移除"}


def _infer_kind(task) -> str:
    kind = constants.ENQUEUE_INFER.get(task["status"])
    if kind is None:
        raise util.KanpilotError(
            "bad_request",
            f"cannot infer request kind for task status {task['status']}",
        )
    return kind


def _require_current_plan(task):
    if not task["plan_path"] or not task["plan_sha256"]:
        raise util.KanpilotError("conflict", "任务没有当前计划，请先完成新规划")


def _require_unsent_dispatch(task):
    if task["sent"] or task["send_state"] not in (None, "not_sent"):
        raise util.KanpilotError("conflict", "当前执行已发送或发送结果不明，不能重复派发旧计划")


def clear_review_result_inline(conn, task, now, actor="user"):
    """Release a previous Review result only for an explicitly requested new Review.

    The caller owns the transaction and authorization. Keep report files and
    historical Review entries; audit the old current pointers before clearing.
    """
    task = db.require_task(conn, task["task_id"])
    extra = db.task_extra(task)
    previous = {key: task[key] for key in ("review_report_path", "review_report_sha256", "review_key")}
    previous.update({key: extra.get(key) for key in ("review_conclusion", "review_registered_at")})
    if any(value is not None for value in previous.values()):
        db.add_event(conn, now, "review_result_cleared", task_id=task["task_id"], actor=actor,
                     detail={"execution_run_id": task["run_id"], "previous": previous})
        extra.pop("review_conclusion", None)
        extra.pop("review_registered_at", None)
        db.update_task(conn, task["task_id"], now, review_report_path=None,
                       review_report_sha256=None, review_key=None, extra_json=extra)
    return db.require_task(conn, task["task_id"])


def confirm_plan_inline(conn, task, now, *, expected_plan_sha256, actor, note=None):
    if not db.task_schedulable(task) or task["status"] != "ready":
        raise util.KanpilotError("conflict", "任务当前不可确认执行")
    _require_current_plan(task)
    _require_unsent_dispatch(task)
    if expected_plan_sha256 != task["plan_sha256"]:
        raise util.KanpilotError("conflict", "计划哈希与当前登记版本不符，请重新查看计划")
    row, created = db.create_request(conn, now, "dispatch", f"dispatch:{task['task_id']}",
                                    task_id=task["task_id"], payload={"note": note} if note else None,
                                    requested_by=actor)
    extra = db.task_extra(db.require_task(conn, task["task_id"]))
    if extra.pop("plan_execution_hold", None):
        db.update_task(conn, task["task_id"], now, extra_json=extra)
        db.add_event(conn, now, "resolved", task_id=task["task_id"], request_id=row["request_id"],
                     actor=actor, detail={"action": "confirm_plan", "plan_sha256": task["plan_sha256"]})
    return row, created


def cmd_enqueue(ctx, payload: dict) -> dict:
    from .execution_questions import guard_active
    conn, now = ctx.conn, ctx.now_iso
    initial = db.require_task(conn, payload["task_id"])
    guard_active(initial)
    if (payload.get("kind") or constants.ENQUEUE_INFER.get(initial["status"])) == "review":
        core.require_committed_review(initial)
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        guard_active(task)
        # §5.20.1（v2）：purged 终态拒绝一切写入口。
        if db.task_is_purged(task):
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除，不能再排队"
            )
        # §5.15.3：归档/回收站、恢复后暂停自动推进的任务不排队。
        if not db.task_is_active(task):
            raise util.KanpilotError(
                "conflict",
                f"任务已{_SAVE_STATE_CN.get(db.task_save_state(task))}，"
                "不在活跃看板，请先恢复后再排队",
            )
        if db.task_auto_paused(task):
            raise util.KanpilotError(
                "conflict", "任务已暂停自动推进，请先恢复自动推进"
            )
        kind = payload.get("kind") or _infer_kind(task)
        if kind not in constants.REQUEST_KINDS or kind == "plan-scan":
            raise util.KanpilotError("bad_request", f"invalid kind {kind}")
        if kind == "plan":
            # v2 §5.22.2：plan 请求只能经规划面板（prepare-plan-input +
            # start-plans）手动授权创建；enqueue 不再直接排队规划。
            raise util.KanpilotError(
                "bad_request",
                "规划需在规划面板确认材料后经 start-plans 提交，"
                "enqueue 不支持 plan",
            )
        allowed = constants.CLAIM_GUARDS[kind]
        if kind == "review":
            core.require_committed_review(task, verify_worktree=False)
        if task["status"] not in allowed:
            # 阶梯 1 退避重入队的例外：dispatch 请求在 claim 之后（status=dispatching）
            # 瞬时失败时，计划早已确认、prompt 也未发送（下面两个 _require 保证），
            # 重入队是正确恢复。`ready` 门本用于「计划未确认不得派发」，这里由它们兜底。
            # 凭据是 payload 里的 retry_base——只有引擎的退避重入队会带它。
            backoff_retry = (kind == "dispatch"
                             and task["status"] == "dispatching"
                             and isinstance(payload.get("retry_base"), int))
            if not backoff_retry:
                raise util.KanpilotError(
                    "bad_request",
                    f"kind {kind} requires task status "
                    f"{'|'.join(sorted(allowed))}, got {task['status']}",
                )
        extra = db.task_extra(task)
        hold = bool(extra.get("plan_execution_hold"))
        confirm_plan = bool(payload.get("confirm_plan"))
        expected_sha = payload.get("expected_plan_sha256")
        # A14: who is enqueueing — the panel/CLI user ("user", the default) or
        # the reconciler/runners ("automation"). On an unattended run the only
        # ``actor=user`` events must be the task creations themselves, so an
        # automated caller has to be able to say so instead of inheriting the
        # hard-coded "user" below (same pair as ``prepare-plan-input``).
        actor = payload.get("actor", "user")
        if actor not in ("user", "automation"):
            raise util.bad_request("actor 必须为 user 或 automation")
        if kind == "dispatch":
            _require_current_plan(task)
            _require_unsent_dispatch(task)
            if hold and not confirm_plan:
                raise util.KanpilotError("conflict", "计划已发布但尚未确认执行（需要 confirm_plan:true）")
            if hold or expected_sha is not None:
                row, created = confirm_plan_inline(conn, task, now, expected_plan_sha256=expected_sha, actor=actor, note=payload.get("note"))
                return {"request": derive.request_view(row, ctx.now), "deduplicated": not created}
        key = f"{kind}:{task['task_id']}"
        # 阶梯 1 退避重试的「已花费次数」必须随请求落库：commands_for(RETRY)
        # 重入队的是**新**请求（attempt_no 归零），不传递的话 _transient_retry
        # 每次读回 None、spent=0，state-model §8 的 ≤2/阶段 上限永远达不到
        # （无限退避重试）。只有引擎的退避重入队会带它（同时是 cmd_enqueue
        # 放行 dispatching 状态重入队的凭据）。legacy 语义不变：无 note 时仍写
        # NULL，不改成 {}。
        request_payload = {}
        if payload.get("note"):
            request_payload["note"] = payload["note"]
        if isinstance(payload.get("retry_base"), int):
            request_payload["retry_base"] = payload["retry_base"]
        if not request_payload:
            request_payload = None
        # requested_by doubles as the ``enqueued`` event actor (db.create_request).
        # Legacy callers that only set not_before keep the historical "engine".
        requested_by = "automation" if actor == "automation" else (
            "engine" if payload.get("not_before") else "user")
        row, created = db.create_request(
            conn, now, kind, key, task_id=task["task_id"],
            payload=request_payload, requested_by=requested_by,
            not_before=payload.get("not_before"),
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


def cmd_cancel_request(ctx, payload: dict) -> dict:
    """Cancel an unclaimed plan while retaining its frozen inputs and history."""
    conn, now = ctx.conn, ctx.now_iso
    with db.write_txn(conn):
        request = db.get_request(conn, payload["request_id"])
        if request is None:
            raise util.KanpilotError("not_found", "规划请求不存在")
        if request["task_id"] != payload["task_id"]:
            raise util.KanpilotError("conflict", "规划请求与任务不匹配，请刷新后重试")
        task = db.require_task(conn, payload["task_id"])
        if db.task_is_purged(task):
            raise util.KanpilotError("conflict", "任务已永久从列表移除，不能终止请求")
        if request["kind"] != "plan":
            raise util.KanpilotError("conflict", "仅已排队的规划请求允许手动终止")
        result = util.jload(request["result_json"]) or {}
        duplicate = (request["status"] == "cancelled"
                     and isinstance(result, dict)
                     and result.get("reason") == "user_cancelled")
        if not duplicate:
            # Read and write under BEGIN IMMEDIATE, just like claim: whichever
            # wins owns the state transition. Never stop a claimed executor.
            if request["status"] != "queued":
                raise util.KanpilotError(
                    "conflict", "规划请求已被领取或已结束，不能终止排队；请刷新查看当前状态",
                )
            db.update_request(conn, request["request_id"], now,
                              status="cancelled", finished_at=now,
                              result_json={"outcome": "cancelled", "reason": "user_cancelled"})
            if core.automation_of(task):
                core.update_automation(conn, task, now, plan_wanted=False)
                task = db.require_task(conn, task["task_id"])
            if task["status"] == "needs_plan":
                core.block_task(conn, task, now, "已手动终止排队规划",
                                "plan_cancelled", "user", stage="plan")
            db.add_event(conn, now, "request_cancelled", task_id=task["task_id"],
                         request_id=request["request_id"], actor="user",
                         detail={"reason": "user_cancelled", "kind": "plan"})
        return {
            "ok": True, "cancelled": True, "deduplicated": duplicate,
            "request": derive.request_view(db.get_request(conn, request["request_id"]), ctx.now),
            "task": derive.task_summary(conn, db.require_task(conn, task["task_id"]), ctx.now),
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
    if kind == "review":
        return "in_review", "review"
    if kind == "dispatch" or task["send_state"] is not None:
        return "ready", "dispatch"
    if kind == "monitor":
        return "running", "monitor"
    return "needs_plan", "plan"


_AUTO_RESUME_TARGETS = {
    "verification_environment_failure": ("running", "monitor"),
    "delivery_blocked_files": ("running", "monitor"), "delivery_commit_failed": ("running", "monitor"),
    "branch_exists": ("needs_plan", None), "delivery_base_unknown": ("needs_plan", None),
    "review_blocked": ("in_review", "review"), "review_backend_unsupported": ("in_review", "review"),
    "branch_diverged": ("in_review", "delivery"), "gh_unavailable": ("in_review", "delivery"), "pr_closed": ("in_review", "delivery"),
    "gh_missing": ("in_review", "delivery"), "gh_timeout": ("in_review", "delivery"), "gh_not_logged_in": ("in_review", "delivery"),
}


def _resolution_snapshot(task):
    return tuple(task[key] for key in ("status", "stage", "run_id", "failure_code", "failure_stage", "project_id", "repository", "plan_path", "plan_sha256", "plan_generated_at", "source_path", "source_sha256", "requirements_sha256", "session_id", "sent", "send_state", "lock_retained", "extra_json"))


def execution_retry_candidate(conn, task, *, engine_empty=False):
    """Cheap UI gate; current terminal and external identities are checked on resolve."""
    from . import round_store
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    return bool(db.task_is_active(task) and task["status"] == "needs_user"
                and task["stage"] == "monitor" and (task["failure_code"] == "execution_failed"
                     or (engine_empty and task["failure_code"] == "verification_unconfirmed"))
                and task["failure_stage"] in ("monitor", "running")
                and task["sent"] == 1 and task["send_state"] == "sent"
                and task["plan_path"] and task["plan_sha256"]
                and not db.task_extra(task).get("plan_execution_hold")
                and row is not None and row["save_state"] == "active"
                and row["backend"] in constants.EXEC_BACKENDS
                and not conn.execute("SELECT 1 FROM requests WHERE task_id=? "
                                     "AND status IN ('queued','claimed') LIMIT 1",
                                     (task["task_id"],)).fetchone())


def _require_execution_terminal(ctx, task, expected):
    from . import cmd_session, headless_adapter, round_store
    source = headless_adapter.resolve_source(ctx.conn)
    signature = source.source_signature(task["task_id"], expected)
    view = cmd_session.cmd_get_session(ctx, {"task_id": task["task_id"], "run_id": expected, "status_only": True})
    detail = view.get("round_detail") or {}
    terminal = detail.get("terminal") or {}
    if (detail.get("run_status") not in ("failed", "finished")
            or terminal.get("outcome") not in ("completed", "failed", "error", "aborted", "timeout", "truncated")
            or detail.get("observer_alive") is not False
            or detail.get("send_state") == "unknown"):
        raise util.KanpilotError("conflict", "旧执行轮次尚无可核验失败终态，请先检查会话状态，不能重复执行")
    row = round_store.require_round(ctx.conn, task["task_id"], expected)
    if row["backend"] == "pi_desktop":
        from . import harness_plus_adapter
        config = util.jload(row["config_json"]) or {}
        session_id = row["session_id"]
        turn_id = detail.get("turn_id")
        if not config.get("connection_file") or not session_id or not turn_id:
            raise util.KanpilotError("conflict", "旧 Plus 会话身份或配置不完整，请先恢复会话观察")
        status = harness_plus_adapter.plus_status(config["connection_file"], session_id=session_id, timeout=2)
        if (status.get("is_running") is not False or status.get("pending_permission")
                or status.get("current_turn_id") not in (None, turn_id)):
            raise util.KanpilotError("conflict", "Plus 会话仍有运行或接续轮次，请先检查 Plus 状态，不能重复执行")
        client, _record = harness_plus_adapter._get_client(config["connection_file"], timeout=2)
        record = client.turn_get(session_id, turn_id)
        turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
        if (turn.get("sessionId", turn.get("session_id")) != session_id
                or turn.get("turnId", turn.get("turn_id")) != turn_id
                or turn.get("status") not in ("completed", "error", "aborted")):
            raise util.KanpilotError("conflict", "Plus 当前轮次没有可核验失败终态，不能重复执行")
    if row["save_state"] != "active":
        raise util.KanpilotError("conflict", "旧执行会话已移出正常列表，请先恢复会话")
    current = round_store.get_round(ctx.conn, task["task_id"], expected)
    if (not signature or source.source_signature(task["task_id"], expected) != signature
            or current is None or dict(current) != dict(row)):
        raise util.KanpilotError("conflict", "旧执行轮次证据已变化，请重新检查会话状态")
    return row, detail


def verify_execution_retry_terminal(conn, task, now=None):
    """Recheck a queued retry before dispatch; never prompt or start observation."""
    from types import SimpleNamespace
    retry = core.execution_retry_of(task)
    if retry:
        current = now or util.now_dt()
        _require_execution_terminal(SimpleNamespace(conn=conn, now=current, now_iso=util.to_iso(current)),
                                    task, retry["source_run_id"])


def _prepare_execution_retry(ctx, payload):
    task = db.require_task(ctx.conn, payload["task_id"])
    engine_empty = _engine_empty_retry(task, payload)
    if (payload["action"] == "resume" and payload.get("expected_execution_run_id")
            and task["failure_code"] != "execution_failed" and not engine_empty):
        raise util.KanpilotError("conflict", "当前阻塞原因已变化，请刷新后重新处理")
    if payload["action"] != "resume" or (task["failure_code"] != "execution_failed" and not engine_empty):
        return None
    from pathlib import Path
    from . import bridge, git_delivery, headless_adapter, remote
    expected = payload.get("expected_execution_run_id")
    if not expected:
        raise util.KanpilotError("conflict", "重新执行必须确认当前失败轮次")
    if expected != task["run_id"]:
        raise util.KanpilotError("stale_run", "所选会话已不是当前失败轮次，请刷新后重试")
    if not execution_retry_candidate(ctx.conn, task, engine_empty=engine_empty):
        raise util.KanpilotError("conflict", "当前任务不能沿用计划重新执行，请先确认旧轮次和发送状态")
    snapshot = _resolution_snapshot(task)
    source = headless_adapter.resolve_source(ctx.conn)
    signature = source.source_signature(task["task_id"], expected)
    row, detail = _require_execution_terminal(ctx, task, expected)
    if not task["requirements_sha256"]:
        raise util.KanpilotError("conflict", "旧计划需求身份缺失，请重新规划")
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    live_task = live.get("task") or {}
    if (live_task.get("id") != task["task_id"] or live_task.get("projectId") != task["project_id"]
            or live.get("requirements_sha256") != task["requirements_sha256"]):
        raise util.KanpilotError("remote_conflict", "远端任务或需求已变化，请重新规划，不能沿用旧计划")
    if live_task.get("status") != constants.IN_PROGRESS_SLUG:
        raise util.KanpilotError("remote_conflict", "重新执行要求 Kaneo 任务仍为 In Progress，请先检查远端状态")
    try:
        if util.sha256_file(Path(task["plan_path"])) != task["plan_sha256"]:
            raise util.KanpilotError("conflict", "已确认计划内容已变化，请重新规划")
        delivery = dict(core.delivery_of(task), repository=task["repository"])
        branch_delivery = core.requires_branch_review(task) or (core.automation_of(task).get("manual_takeover") is True
                                                       and delivery.get("branch") and delivery.get("worktree_path"))
        if not branch_delivery:
            if not row["worktree_path"] or detail.get("worktree_path") != row["worktree_path"]:
                raise util.KanpilotError("conflict", "旧执行工作树身份缺失，不能创建新目录替代旧改动")
            delivery = {"repository": task["repository"], "worktree_path": row["worktree_path"],
                        "head_sha": git_delivery._value(["rev-parse", "HEAD"], row["worktree_path"])}
        elif core.automation_of(task).get("manual_takeover") is True:
            git_delivery.verify_branch_worktree(delivery, require_clean=False, expected_head=None)
            delivery["head_sha"] = git_delivery._value(["rev-parse", "HEAD"], delivery["worktree_path"])
        if row["worktree_path"] != delivery.get("worktree_path"):
            raise util.KanpilotError("conflict", "旧轮次工作树与任务登记不符，不能重新执行")
        worktree_sha = git_delivery.worktree_state_sha256(delivery, detached=not bool(branch_delivery))
    except (OSError, git_delivery.GitDeliveryError) as exc:
        raise util.KanpilotError("conflict", "旧计划或执行工作树不可核验，请先恢复原执行材料") from exc
    if not signature or source.source_signature(task["task_id"], expected) != signature:
        raise util.KanpilotError("conflict", "旧执行轮次证据已变化，请重新检查会话状态")
    return {"snapshot": snapshot, "round_snapshot": dict(row), "source_signature": signature,
            "source": source, "worktree": delivery, "detached": not bool(branch_delivery),
            "worktree_sha256": worktree_sha}


def _engine_empty_retry(task, payload):
    fix = payload.get("engine_fix") or {}
    receipt = db.task_extra(task).get("engine_verdict") or {}
    return (payload.get("actor") == "automation" and fix.get("reason") == "empty_diff"
            and receipt.get("kind") == "empty_diff" and receipt.get("empty_diff") is True
            and receipt.get("execution_run_id") == task["run_id"]
            and receipt.get("plan_sha256") == task["plan_sha256"] and bool(receipt.get("binding")))


def _engine_fix_guard(ctx, payload, *, check_binding=True, prepared=None):
    """Only a bound core quality verdict can authorize a new automation round."""
    from . import automation_policy, engine_claim_scope, exec_config, project_config
    from .engine import fallback, ladder, verdict
    fix = payload.get("engine_fix")
    if not isinstance(fix, dict) or payload.get("actor") != "automation":
        raise util.bad_request("引擎补救必须由 automation 发起")
    task = db.require_task(ctx.conn, payload["task_id"])
    rounds = db.task_extra(task).get("engine_fix_rounds") or []
    prior = next((row for row in rounds if row.get("execution_run_id") == fix.get("execution_run_id")), None)
    if prior:
        return {"deduplicated": True, "prior": prior}
    _, _, items, _ = project_config.load_raw_mappings(ctx.conn)
    entry = items.get(task["project_id"]) or {}
    mode = engine_claim_scope.effective_mode(ctx.conn, task["project_id"])
    receipt = db.task_extra(task).get("engine_verdict") or {}
    policy = automation_policy.policy_for(ctx.conn, task["project_id"]) or {}
    if (mode != "live" or not core.is_automation_managed(task) or not policy.get("enabled")
            or db.get_setting(ctx.conn, "paused_global", False)
            or task["status"] != "needs_user" or task["stage"] != "monitor"
            or task["failure_code"] != "verification_unconfirmed"
            or task["send_state"] != "sent" or task["sent"] != 1
            or not db.task_is_active(task)
            or fix.get("execution_run_id") != task["run_id"]
            or payload.get("expected_execution_run_id") != task["run_id"]
            or fix.get("plan_sha256") != task["plan_sha256"]
            or receipt.get("execution_run_id") != task["run_id"]
            or receipt.get("plan_sha256") != task["plan_sha256"]
            or receipt.get("verification_ok") or receipt.get("environment_failure")
            or receipt.get("kind") not in ("empty_diff", "verify_failed")
            or fix.get("reason") != receipt.get("kind") or not receipt.get("binding")
            or ctx.conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                                (task["task_id"],)).fetchone()):
        raise util.KanpilotError("conflict", "自动补救的任务、失败轮次或核心回执已变化")
    if check_binding and verdict.binding(ctx.conn, task) != receipt["binding"]:
        raise util.KanpilotError("verification_identity_unconfirmed", "失败回执的计划或工作树已变化")
    reason = fix["reason"]
    block = None
    if not core.automation_budget(task, policy, util.now_dt())["ok"]:
        block = "auto_budget_exceeded"
    elif len(rounds) >= min(ladder.MAX_FIX_ROUNDS, policy.get("max_fix_rounds", 2)):
        block = "fix_rounds_exhausted"
    elif reason == "empty_diff" and any(row.get("reason") == "empty_diff"
         and row.get("plan_sha256") == task["plan_sha256"] for row in rounds):
        block = "fix_rounds_exhausted"
    configured = fallback.configured_model(entry) if reason == "empty_diff" else None
    config_fingerprint = util.jdump({"mapping": entry, "exec_config": exec_config.load(ctx.conn)})
    if prepared is not None:
        if prepared["config_fingerprint"] != config_fingerprint:
            raise util.KanpilotError("conflict", "补救模型或项目配置在探针后已变化")
        plan = prepared["plan"]
    elif reason == "empty_diff" and not block:
        primary, primary_error = fallback.primary_snapshot(ctx.conn) if configured else (None, None)
        plan = fallback.plan_retry(configured, primary=primary,
                                   connection_file=(primary or {}).get("connection_file"))
        if primary_error:
            plan = {"model": "primary", "snapshot": None, "validation_failed": True,
                    "probe_detail": primary_error, "events": []}
    else:
        plan = {"model": "primary", "snapshot": None, "validation_failed": False, "events": []}
    if plan.get("validation_failed"):
        block = block or "validation_failed"
    if block is None and payload["action"] != ("resume" if reason == "empty_diff" else "replan"):
        raise util.bad_request("自动补救动作与核心判定不符")
    return {"snapshot": _resolution_snapshot(task), "block": block, "plan": plan,
            "config_fingerprint": config_fingerprint}


def _record_engine_fix(conn, task, now, fix, plan):
    from .engine import fallback
    rows = db.task_extra(task).get("engine_fix_rounds") or []
    rows.append({"execution_run_id": fix["execution_run_id"], "plan_sha256": fix["plan_sha256"],
                 "reason": fix["reason"], "model": plan.get("model", "primary"), "at": now})
    entries = {"engine_fix_rounds": rows}
    if plan.get("snapshot") is not None:
        entries[fallback.MODEL_OVERRIDE_KEY] = fallback.override_payload(plan["snapshot"], task)
    db.set_extra(conn, task, now, **entries)
    for event in plan.get("events") or []:
        db.add_event(conn, now, event["kind"], task_id=task["task_id"], actor="automation",
                     detail=event["detail"])
    db.add_event(conn, now, "engine_fix_started", task_id=task["task_id"], actor="automation", detail=rows[-1])


def _accept_remote_for_resolution(ctx, payload):
    task = db.require_task(ctx.conn, payload["task_id"])
    if not core.is_automation_managed(task) or not (payload["action"] == "replan" or (payload["action"] == "resume" and task["failure_code"] == "remote_conflict")):
        return None
    round_snapshot = None
    if payload["action"] == "replan":
        if task["send_state"] == "unknown":
            raise util.KanpilotError("conflict", "发送结果不明，请先处理发送恢复或明确接受重复风险")
        if task["sent"] or task["send_state"] == "sent":
            from . import cmd_session, round_store
            old_round = round_store.get_round(ctx.conn, task["task_id"], task["run_id"]) if task["run_id"] else None
            if old_round is None:
                raise util.KanpilotError("conflict", "旧执行轮次尚无可核验终态，请先恢复会话观察，再重新规划")
            else:
                view = cmd_session.cmd_get_session(ctx, {"task_id": task["task_id"], "run_id": task["run_id"]})
                detail = view.get("round_detail") or {}
                terminal = detail.get("terminal") or {}
                if (detail.get("run_status") not in ("finished", "failed")
                        or terminal.get("outcome") not in ("completed", "failed", "error", "aborted", "timeout", "truncated")):
                    raise util.KanpilotError("conflict", "旧执行轮次尚无可核验终态，请先确认会话结束并恢复观察，再重新规划")
                refreshed = round_store.get_round(ctx.conn, task["task_id"], task["run_id"])
                if refreshed is None:
                    raise util.KanpilotError("conflict", "旧执行轮次证据已变化，请重新核验")
                round_snapshot = dict(refreshed)
    from . import remote, bridge
    stage = task["failure_stage"] or task["stage"]
    if stage == "dispatch" and payload["action"] == "resume":
        raise util.KanpilotError("conflict", "派发中途需使用发送恢复动作")
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    t = live.get("task") or {}
    if t.get("projectId") != task["project_id"]:
        raise util.KanpilotError("remote_conflict", "远端任务项目已变化")
    if payload["action"] == "resume":
        required = {"monitor": {"in-progress", "in-review", "done"}, "delivery": {"in-review", "done"}, "review": {"in-review", "done"}}.get(stage)
        if required and t.get("status") not in required:
            raise util.KanpilotError("conflict", "请先在 Kaneo 调整到 " + "/".join(sorted(required)) + " 再继续")
    return {"requirements_sha256": live.get("requirements_sha256"), "kaneo_status": t.get("status"), "stage": stage, "snapshot": _resolution_snapshot(task), "round_snapshot": round_snapshot}


def cmd_resolve(ctx, payload: dict) -> dict:
    from .execution_questions import guard_active
    conn, now = ctx.conn, ctx.now_iso
    guard_active(db.require_task(conn, payload["task_id"]))
    action = payload["action"]
    note = payload.get("note")
    # A14: resolve is a user-action surface today (no automation caller), but the
    # actor is explicit so an unattended caller would be recorded honestly
    # instead of silently inheriting "user".
    actor = payload.get("actor", "user")
    if actor not in ("user", "automation"):
        raise util.bad_request("actor 必须为 user 或 automation")
    if action not in constants.VALID_RESOLVE_ACTIONS:
        raise util.KanpilotError("bad_request", f"invalid action {action}")
    if action == "recover_review":
        from . import review_recovery
        return review_recovery.recover(ctx, payload)
    if action == "reverify":
        from . import verification_recovery
        return verification_recovery.recover(ctx, payload)
    engine_fix = _engine_fix_guard(ctx, payload) if payload.get("engine_fix") else None
    if engine_fix and engine_fix.get("deduplicated"):
        return {"ok": True, "deduplicated": True, "task_status": db.require_task(conn, payload["task_id"])["status"], "request": None}
    if engine_fix and engine_fix.get("block"):
        with db.write_txn(conn):
            guarded = _engine_fix_guard(ctx, payload, check_binding=False, prepared=engine_fix)
            task = db.require_task(conn, payload["task_id"])
            guard_active(task)
            if _resolution_snapshot(task) != engine_fix["snapshot"]:
                raise util.KanpilotError("conflict", "自动补救处置状态已变化")
            code = guarded["block"]
            if not code:
                raise util.KanpilotError("conflict", "自动补救上限或探针结果已变化")
            for event in engine_fix["plan"].get("events") or []:
                db.add_event(conn, now, event["kind"], task_id=task["task_id"], actor=actor, detail=event["detail"])
            core.block_task(conn, task, now, engine_fix["plan"].get("probe_detail") or code, code,
                            actor, stage="monitor")
        return {"ok": True, "task_status": "needs_user", "request": None}
    execution_retry = _prepare_execution_retry(ctx, payload)
    accepted_remote = _accept_remote_for_resolution(ctx, payload)
    if engine_fix:
        if accepted_remote and (accepted_remote["requirements_sha256"] != db.require_task(conn, payload["task_id"])["requirements_sha256"]
                                or accepted_remote["kaneo_status"] != constants.IN_PROGRESS_SLUG):
            raise util.KanpilotError("remote_conflict", "自动补救要求原需求未变且 Kaneo 仍为 In Progress")
        _engine_fix_guard(ctx, payload, prepared=engine_fix)  # external worktree/plan recheck after I/O
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        guard_active(task)
        if engine_fix:
            guarded = _engine_fix_guard(ctx, payload, check_binding=False, prepared=engine_fix)
            if (guarded.get("deduplicated") or guarded.get("block")
                    or _resolution_snapshot(task) != engine_fix["snapshot"]):
                raise util.KanpilotError("conflict", "自动补救处置状态已变化")
        # §5.20.1（v2）：purged 终态拒绝一切写入口。
        if db.task_is_purged(task):
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除，不能再处理"
            )
        # §5.15.3：非活跃（归档/回收站）任务不处理 resolve。
        if not db.task_is_active(task):
            raise util.KanpilotError(
                "conflict",
                f"任务已{_SAVE_STATE_CN.get(db.task_save_state(task))}，"
                "不在活跃看板，请先恢复后再处理",
            )
        status, send_state = task["status"], task["send_state"]

        if execution_retry:
            from pathlib import Path
            from . import git_delivery, round_store
            old_round = round_store.get_round(conn, task["task_id"], task["run_id"])
            if (execution_retry["snapshot"] != _resolution_snapshot(task)
                    or not execution_retry_candidate(conn, task, engine_empty=_engine_empty_retry(task, payload))
                    or old_round is None or dict(old_round) != execution_retry["round_snapshot"]
                    or execution_retry["source"].source_signature(task["task_id"], task["run_id"])
                    != execution_retry["source_signature"]):
                raise util.KanpilotError("conflict", "当前失败轮次或处置状态已变化，请刷新后重试")
            try:
                if (util.sha256_file(Path(task["plan_path"])) != task["plan_sha256"]
                        or git_delivery.worktree_state_sha256(execution_retry["worktree"], detached=execution_retry["detached"])
                        != execution_retry["worktree_sha256"]):
                    raise util.KanpilotError("conflict", "计划或工作树内容已变化，请重新确认后再执行")
            except (OSError, git_delivery.GitDeliveryError) as exc:
                raise util.KanpilotError("conflict", "计划或工作树已不可核验，未重新执行") from exc

        def need(condition: bool, message: str) -> None:
            if not condition:
                raise util.KanpilotError("bad_request", message)

        if accepted_remote:
            if task["status"] != "needs_user" or accepted_remote["snapshot"] != _resolution_snapshot(task):
                raise util.KanpilotError("conflict", "任务处置状态已变化")
            if accepted_remote.get("round_snapshot") is not None:
                from . import round_store
                old_round = round_store.get_round(conn, task["task_id"], task["run_id"])
                if old_round is None or dict(old_round) != accepted_remote["round_snapshot"]:
                    raise util.KanpilotError("conflict", "旧执行轮次证据已变化，请重新核验")
            db.update_task(conn, task["task_id"], now, requirements_sha256=accepted_remote["requirements_sha256"], kaneo_status=accepted_remote["kaneo_status"], kaneo_checked_at=now)
            db.add_event(conn, now, "requirements_accepted", task_id=task["task_id"], actor=actor)
            task = db.require_task(conn, task["task_id"])
        resolution = {"action": action, "note": note, "at": now, "actor": actor}
        new_request = None

        if action in ("resume", "accept_verification"):
            if status != "needs_user":
                raise util.KanpilotError("conflict", "当前状态不可继续")
            code = task["failure_code"]
            from . import automation_policy, review_config
            if action == "accept_verification":
                from . import engine_claim_scope
                if engine_claim_scope.effective_mode(conn, task["project_id"]) == "live":
                    raise util.KanpilotError("conflict", "Live 项目由核心执行验证，请重新运行验证检查或修复计划；不能人工跳过验证")
                if code != "verification_unconfirmed" or not task["run_id"]:
                    raise util.KanpilotError("conflict", "仅待确认验证的当前执行轮次可接受")
                db.set_extra(conn, task, now, verification_override={"execution_run_id": task["run_id"], "actor": "user", "at": now, "note": note})
                target, kind = "running", "monitor"
            elif execution_retry:
                target, kind = "ready", "dispatch"
                extra = db.task_extra(task)
                extra["execution_retry"] = {
                    "source_run_id": task["run_id"], "requested_at": now,
                    "plan_sha256": task["plan_sha256"],
                    "plan_generated_at": task["plan_generated_at"],
                    "worktree": execution_retry["worktree"],
                    "detached": execution_retry["detached"],
                    "worktree_sha256": execution_retry["worktree_sha256"],
                }
                extra.pop("verification_override", None)
                db.update_task(conn, task["task_id"], now, extra_json=extra)
                db.add_event(conn, now, "execution_retry_requested", task_id=task["task_id"], actor=actor,
                             detail={"source_run_id": task["run_id"], "plan_sha256": task["plan_sha256"],
                                     "previous": {key: task[key] for key in
                                                  ("session_marker", "session_id", "session_title", "sent_at", "ui_evidence", "send_evidence")}})
            elif code in ("fix_rounds_exhausted", "auto_budget_exceeded", "automation_stopped"):
                if not core.is_automation_managed(task) or not (automation_policy.policy_for(conn, task["project_id"]) or {}).get("enabled"):
                    raise util.KanpilotError("conflict", "请先重新开启项目自动处理")
                fix = db.task_extra(task).get("fix_request")
                if not fix or fix.get("state") != "held":
                    raise util.KanpilotError("conflict", "没有待继续的修复请求")
                core.start_fix_round_inline(conn, task, now, fix_request=fix, actor="user", new_segment=True)
                target, kind = "needs_plan", None
            else:
                if not core.is_automation_managed(task):
                    raise util.KanpilotError("conflict", "该任务不受自动化管理")
                if code == "remote_conflict" and accepted_remote:
                    kind = accepted_remote["stage"]
                    target = {"monitor": "running", "delivery": "in_review", "review": "in_review", "plan": "needs_plan"}.get(kind)
                    kind = None if kind == "plan" else kind
                    if target is None: raise util.KanpilotError("conflict", "请使用发送恢复动作")
                elif code in _AUTO_RESUME_TARGETS:
                    target, kind = _AUTO_RESUME_TARGETS[code]
                else:
                    raise util.KanpilotError("conflict", "当前原因不允许继续")
                if code == "review_backend_unsupported" and review_config.backend(conn) not in constants.AUTOMATION_REVIEW_BACKENDS:
                    raise util.KanpilotError("conflict", "请先更改 Review 后端为 Plus/Claude/Codex")
            task = db.require_task(conn, task["task_id"])
            if core.is_automation_managed(task):
                a = core.automation_of(task)
                core.update_automation(conn, task, now, **({"segment_started_at": now, "segment_cost_base_usd": a.get("cost_usd", 0)} if actor == "user" else {}), **({"plan_wanted": True, "plan_attempts": 0} if target == "needs_plan" else {}))
            task = db.require_task(conn, task["task_id"])
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            if execution_retry:
                core.release_dispatch_serial(conn, task["run_id"])
                fields.update(sent=0, send_state=None, sent_at=None, lock_retained=0,
                              run_id=None, session_marker=None, session_id=None, session_title=None,
                              dispatch_lease_expires_at=None, original_todo_slug=None,
                              ui_evidence=None, send_evidence=None, transition_started_at=None,
                              transition_verified_at=None, last_progress=None)
            if kind == "review":
                task = clear_review_result_inline(conn, task, now)
            core.transition_task(conn, task, target, now, actor, detail={"via": "resolve " + action}, **fields)
            if kind == "delivery":
                core.update_delivery(conn, task, now, next_sync_at=now)
            if kind:
                new_request, _ = _new_cycle_request(conn, now, task, kind, requested_by=actor)

        elif action == "retry":
            from . import plan_answers
            if plan_answers.candidate(conn, task) is not None:
                raise util.KanpilotError("conflict", "规划正在等待用户答案，请在任务详情回答问题并继续规划")
            target_status, kind = _retry_target(conn, task)
            need(
                status == "needs_user" and (send_state in (None, "not_sent")
                                           or (kind == "review" and send_state == "sent"
                                               and not core.is_automation_managed(task))),
                "retry requires needs_user with send_state not_sent "
                "(or a non-dispatch block)",
            )
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            if kind == "review":
                task = clear_review_result_inline(conn, task, now)
            core.transition_task(
                conn, task, target_status, now, "user",
                detail={"via": "resolve retry"}, **fields,
            )
            if kind == "plan":
                # v2 §5.22.2：规划改手动门控——只恢复状态，等用户经规划
                # 面板提交材料；不再自动创建 plan 请求。
                new_request = None
            else:
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
            extra = db.task_extra(task)
            extra.pop("execution_retry", None)
            db.update_task(conn, task["task_id"], now, extra_json=extra)
            task = db.require_task(conn, task["task_id"])
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            core.transition_task(
                conn, task, "needs_plan", now, actor,
                detail={"via": "resolve replan"}, **fields,
            )
            if core.is_automation_managed(task):
                a = core.automation_of(task)
                core.update_automation(conn, task, now, plan_wanted=True, plan_attempts=0,
                                       **({"segment_started_at": now, "segment_cost_base_usd": a.get("cost_usd", 0)} if actor == "user" else {}),
                                       replan_requested_at=now,
                                       replan_remote_status=(accepted_remote or {}).get("kaneo_status"),
                                       replan_worktree_sha256=None)
                db.update_task(conn, task["task_id"], now, sent=0, send_state=None, sent_at=None, lock_retained=0)
            new_request = None

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
                if kind == "plan":
                    if core.is_automation_managed(task) and core.automation_of(task).get("plan_attempts", 0) < 2:
                        core.update_automation(conn, task, now, plan_wanted=True)
                    # v2 §5.22.2：手动任务不自动重建规划请求。
                    new_request = None
                elif target in (constants.CLAIM_GUARDS.get(kind) or ()):
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
            review_block = (task["failure_stage"] == "review"
                            or task["stage"] == "review"
                            or task["failure_code"] == "review_blocked")
            if core.is_automation_managed(task):
                core.update_automation(conn, task, now, manual_takeover=True, plan_wanted=False)
                core.update_delivery(conn, task, now, mode="manual")
                db.add_event(conn, now, "automation_stopped", task_id=task["task_id"], actor="user", detail={"via": "manual_takeover"})
                task = db.require_task(conn, task["task_id"])
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            if review_block:
                fields["auto_advance_paused"] = 1
            core.transition_task(
                conn, task, "in_review" if review_block else "ready", now, "user",
                detail={"via": "resolve dismiss"}, **fields,
            )

        if engine_fix:
            _record_engine_fix(conn, db.require_task(conn, task["task_id"]), now,
                               payload["engine_fix"], engine_fix["plan"])
        db.add_event(
            conn, now, "resolved", task_id=task["task_id"], actor=actor,
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
                if task is not None:
                    from . import verification_checks
                    if verification_checks._alive(verification_checks._record(task)):
                        # Verification owns only this task; refresh the others.
                        continue
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
                    # v2 §5.22.2：不再自动创建 plan 请求——新任务停在
                    # needs_plan，等待用户在规划面板手动提交。
                    changed.append({"task_id": task_id, "from": None,
                                    "to": initial})
                    continue

                # §5.15.3：归档/回收站任务整体跳过（不 upsert/不改状态/指纹/
                # 不建请求），后续同步不得把归档/回收站任务重新导入看板。
                if not db.task_is_active(task):
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

                if core.is_automation_managed(task) or task["status"] in constants.DISPATCH_MID_STATUSES:
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
                    # v2 §5.22.2：需求变化仍更新状态与证据，但不自动建
                    # plan 请求——等待规划面板手动提交。
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
                    # 新一轮评审周期起始（§2 review_summary「下同」）：
                    # 与 advance-task 同口径清上一周期结论。
                    extra_json = db.task_extra(task)
                    for key in ("review_conclusion", "review_registered_at"):
                        extra_json.pop(key, None)
                    core.transition_task(
                        conn, task, "in_review", now, executor,
                        detail={"via": "kaneo_observed", "kaneo_status": observed},
                        review_report_path=None, review_report_sha256=None,
                        extra_json=extra_json,
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


def cmd_dismiss_failed_requests(ctx, payload: dict) -> dict:
    """仅隐藏待处理中的历史失败；保留请求、attempts、任务与证据。"""
    ids = payload.get("request_ids")
    if (not isinstance(ids, list) or not ids or len(ids) > 200
            or any(not isinstance(x, str) or not x for x in ids)):
        raise util.bad_request("request_ids 须为 1 至 200 个非空请求 ID")
    ids = list(dict.fromkeys(ids))
    with db.write_txn(ctx.conn):
        rows = []
        for rid in ids:
            row = db.get_request(ctx.conn, rid)
            if row is None:
                raise util.KanpilotError("not_found", "失败请求不存在")
            if row["status"] != "failed":
                raise util.KanpilotError("conflict", "仅能移除已失败请求，请刷新列表")
            rows.append(row)
        stored = db.get_setting(ctx.conn, "dismissed_failed_requests", {})
        dismissed = dict(stored) if isinstance(stored, dict) else {}
        changed = []
        for row in rows:
            rid = row["request_id"]
            if rid in dismissed:
                continue
            dismissed[rid] = ctx.now_iso
            changed.append(rid)
            db.add_event(ctx.conn, ctx.now_iso, "failed_request_dismissed",
                         task_id=row["task_id"], request_id=rid, actor="user",
                         detail={"hidden_from": "pending", "history_retained": True})
        if changed:
            db.set_setting(ctx.conn, "dismissed_failed_requests", dismissed)
    return {"ok": True, "dismissed_count": len(changed)}
