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


def confirm_plan_inline(conn, task, now, *, expected_plan_sha256, actor, note=None):
    if not db.task_schedulable(task) or task["status"] != "ready":
        raise util.KanpilotError("conflict", "任务当前不可确认执行")
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
    conn, now = ctx.conn, ctx.now_iso
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
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
        if task["status"] not in allowed:
            raise util.KanpilotError(
                "bad_request",
                f"kind {kind} requires task status "
                f"{'|'.join(sorted(allowed))}, got {task['status']}",
            )
        extra = db.task_extra(task)
        hold = bool(extra.get("plan_execution_hold"))
        confirm_plan = bool(payload.get("confirm_plan"))
        expected_sha = payload.get("expected_plan_sha256")
        if kind == "dispatch":
            if hold and not confirm_plan:
                raise util.KanpilotError("conflict", "计划已发布但尚未确认执行（需要 confirm_plan:true）")
            if hold or expected_sha is not None:
                row, created = confirm_plan_inline(conn, task, now, expected_plan_sha256=expected_sha, actor="user", note=payload.get("note"))
                return {"request": derive.request_view(row, ctx.now), "deduplicated": not created}
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
    if kind == "dispatch" or task["send_state"] is not None:
        return "ready", "dispatch"
    if kind == "monitor":
        return "running", "monitor"
    if kind == "review":
        return "in_review", "review"
    return "needs_plan", "plan"


_AUTO_RESUME_TARGETS = {
    "delivery_blocked_files": ("running", "monitor"), "delivery_commit_failed": ("running", "monitor"),
    "branch_exists": ("needs_plan", None), "delivery_base_unknown": ("needs_plan", None),
    "review_blocked": ("in_review", "review"), "review_backend_unsupported": ("in_review", "review"),
    "branch_diverged": ("in_review", "delivery"), "gh_unavailable": ("in_review", "delivery"), "pr_closed": ("in_review", "delivery"),
}


def _resolution_snapshot(task):
    return tuple(task[key] for key in ("status", "run_id", "failure_code", "failure_stage", "project_id", "requirements_sha256", "sent", "send_state", "lock_retained", "extra_json"))


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
    conn, now = ctx.conn, ctx.now_iso
    action = payload["action"]
    note = payload.get("note")
    if action not in constants.VALID_RESOLVE_ACTIONS:
        raise util.KanpilotError("bad_request", f"invalid action {action}")
    accepted_remote = _accept_remote_for_resolution(ctx, payload)
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
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
            db.add_event(conn, now, "requirements_accepted", task_id=task["task_id"], actor="user")
            task = db.require_task(conn, task["task_id"])
        resolution = {"action": action, "note": note, "at": now, "actor": "user"}
        new_request = None

        if action in ("resume", "accept_verification"):
            if status != "needs_user":
                raise util.KanpilotError("conflict", "当前状态不可继续")
            code = task["failure_code"]
            from . import automation_policy, review_config
            if action == "accept_verification":
                if code != "verification_unconfirmed" or not task["run_id"]:
                    raise util.KanpilotError("conflict", "仅待确认验证的当前执行轮次可接受")
                db.set_extra(conn, task, now, verification_override={"execution_run_id": task["run_id"], "actor": "user", "at": now, "note": note})
                target, kind = "running", "monitor"
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
                core.update_automation(conn, task, now, segment_started_at=now, segment_cost_base_usd=a.get("cost_usd", 0), **({"plan_wanted": True, "plan_attempts": 0} if target == "needs_plan" else {}))
            task = db.require_task(conn, task["task_id"])
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            if kind == "review":
                fields.update(review_report_path=None, review_report_sha256=None, review_key=None)
                extra = db.task_extra(db.require_task(conn, task["task_id"]))
                extra.pop("review_conclusion", None)
                extra.pop("review_registered_at", None)
                fields["extra_json"] = extra
            core.transition_task(conn, task, target, now, "user", detail={"via": "resolve " + action}, **fields)
            if kind == "delivery":
                core.update_delivery(conn, task, now, next_sync_at=now)
            if kind:
                new_request, _ = _new_cycle_request(conn, now, task, kind)

        elif action == "retry":
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
            fields = core.clear_block_fields()
            fields["resolution"] = resolution
            core.transition_task(
                conn, task, "needs_plan", now, "user",
                detail={"via": "resolve replan"}, **fields,
            )
            if core.is_automation_managed(task):
                a = core.automation_of(task)
                core.update_automation(conn, task, now, plan_wanted=True, plan_attempts=0,
                                       segment_started_at=now, segment_cost_base_usd=a.get("cost_usd", 0),
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
            if core.is_automation_managed(task):
                core.update_automation(conn, task, now, manual_takeover=True, plan_wanted=False)
                core.update_delivery(conn, task, now, mode="manual")
                db.add_event(conn, now, "automation_stopped", task_id=task["task_id"], actor="user", detail={"via": "manual_takeover"})
                task = db.require_task(conn, task["task_id"])
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
