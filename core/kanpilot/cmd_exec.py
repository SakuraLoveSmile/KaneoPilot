"""Executor-facing commands: claim / heartbeat / report / publish-plan /
register-review (protocol.md §4.2/§4.3/§4.5/§4.7/§5.5/§5.6/§5.7)."""

from __future__ import annotations

import sqlite3
import os
from pathlib import Path
from typing import Optional

from . import (
    cmd_read, constants, core, db, derive, dispatch_validate, exec_config,
    planfile, remote, round_store, runtime_operations, util, engine_claim_scope,
)


def planner_input_dir(conn, input_id: str) -> Path:
    """planner_inputs/<input_id>/ 目录（settings 覆盖 > env > 默认）。"""
    root = constants.planner_inputs_root(
        db.get_setting(conn, "planner_inputs_root")
    )
    return root / input_id


def request_backend(conn, row) -> Optional[str]:
    """Resolve the request's frozen role backend; old reviews stay legacy."""
    payload = db.request_payload(row)
    if row["stage"] == "plan":
        return payload.get("planner_backend") or constants.PLANNER_BACKEND
    if row["stage"] == "review":
        from . import review_config
        return review_config.backend_for_request(conn, payload)
    return None


def _backend_matches(conn, row, desktop):
    if row["stage"] not in ("plan", "review"):
        return not desktop
    if desktop:
        key = "planner_backend" if row["stage"] == "plan" else "review_backend"
        return db.request_payload(row).get(key) == "codex_desktop"
    return request_backend(conn, row) != "codex_desktop"


def desktop_claim_receipt(conn, role, metadata):
    receipts = db.get_setting(conn, "desktop_claim_receipts", {}) or {}
    receipt = receipts.get(metadata["operation_id"])
    if receipt is None:
        return None
    if receipt.get("role") != role or receipt.get("token_hash") != metadata["token_hash"]:
        raise util.KanpilotError("conflict", "Desktop claim operation identity conflict")
    return receipt["response"]


def _select_candidate(
    conn: sqlite3.Connection, stage: str, request_id: Optional[str], now, authorized_task_id=None,
    desktop=False,
) -> Optional[sqlite3.Row]:
    if request_id:
        row = db.get_request(conn, request_id)
        if row is None:
            raise util.KanpilotError(
                "not_found", f"request not found: {request_id}"
            )
        if authorized_task_id and row["task_id"] != authorized_task_id:
            raise util.KanpilotError("forbidden", "request is outside the authorized task")
        if row["stage"] != stage:
            raise util.KanpilotError(
                "bad_request",
                f"request {request_id} is stage {row['stage']}, not {stage}",
            )
        # 定点领取跳过 not_before：冷却期只约束自动取单（protocol §4.2-4）。
        # status 判定交给调用方：'claimed' → claimed:false（并发失败），
        # 其他非 queued → conflict。
        return row if engine_claim_scope.allows(conn, row, now) else None
    # §5.15.3：自动取单排除归档/回收站与恢复后暂停任务的请求；
    # plan-scan 等无任务请求（task_id IS NULL）不受影响。
    rows = conn.execute(
        "SELECT r.* FROM requests r "
        "LEFT JOIN tasks t ON t.task_id = r.task_id "
        "WHERE r.stage=? AND r.status='queued' "
        "AND (r.not_before IS NULL OR r.not_before <= ?) "
        "AND (? IS NULL OR r.task_id=?) "
        "AND (r.task_id IS NULL OR (t.save_state='active' "
        "AND t.auto_advance_paused=0)) "
        "ORDER BY r.priority ASC, r.created_at ASC",
        (stage, util.to_iso(now), authorized_task_id, authorized_task_id),
    ).fetchall()
    for row in rows:
        if not engine_claim_scope.allows(conn, row, now):
            continue
        if not _backend_matches(conn, row, desktop):
            continue
        # v2 §5.22.2：未授权（payload 无 manual_authorized）的 plan 请求
        # 自动取单跳过——等待用户在规划面板确认材料。
        if row["kind"] == "plan" and not db.request_payload(row).get(
            "manual_authorized"
        ):
            continue
        task = db.get_task(conn, row["task_id"]) if row["task_id"] else None
        # v2 §5.22.4：plan_execution_hold 未解除前 dispatch 不可领取。
        if (
            row["kind"] == "dispatch"
            and task is not None
            and db.task_extra(task).get("plan_execution_hold")
        ):
            continue
        return row
    return None


def _claim_row(
    conn: sqlite3.Connection,
    request: sqlite3.Row,
    task: Optional[sqlite3.Row],
    executor: str,
    lease_seconds: int,
    now,
) -> str:
    """Mark request claimed inside the current write txn; returns run_id."""
    now_iso = util.to_iso(now)
    run_id = util.new_uuid()
    payload = db.request_payload(request)
    if task is not None:
        payload["task_status_at_claim"] = task["status"]
    db.update_request(
        conn, request["request_id"], now_iso,
        status="claimed", run_id=run_id, claimed_by=executor,
        claimed_at=now_iso, heartbeat_at=now_iso,
        lease_expires_at=util.plus_seconds(lease_seconds, now),
        attempt_no=request["attempt_no"] + 1,
        payload_json=payload,
    )
    db.add_attempt(
        conn, request["request_id"], request["task_id"], run_id,
        request["stage"], request["kind"], now_iso, "claimed",
    )
    db.add_event(
        conn, now_iso, "claimed", task_id=request["task_id"],
        request_id=request["request_id"], actor=executor,
        detail={"run_id": run_id, "kind": request["kind"]},
    )
    return run_id


def cmd_claim(ctx, payload: dict, scope_id: Optional[str] = None, operation_id: Optional[str] = None,
              desktop_claim: Optional[dict] = None) -> dict:
    result = _claim(ctx, payload, scope_id, operation_id, desktop_claim)
    if not result.get("claimed") or payload.get("stage") != "review":
        return result
    request = db.get_request(ctx.conn, result["request_id"])
    task = db.require_task(ctx.conn, request["task_id"])
    if request_backend(ctx.conn, request) == "pi" and db.task_extra(task).get("review_supplements"):
        identity = dict(task_id=task["task_id"], request_id=result["request_id"], run_id=result["run_id"], expected_execution_run_id=task["run_id"])
        try:
            from . import review_resolution, pi_desktop_controller
            directory = pi_desktop_controller.invocation_dir("review", result["run_id"]) / "materials"
            directory.mkdir(parents=True, exist_ok=True)
            evidence = review_resolution.collect_evidence(ctx.conn, dict(task), task["run_id"], directory)
            frozen = {"files": evidence, "hashes": {path: util.sha256_file(Path(path)) for path in evidence.values()}}
            with db.write_txn(ctx.conn):
                core.require_review_identity(ctx.conn, identity, db.require_task(ctx.conn, task["task_id"]), util.now_dt())
                db.merge_request_payload(ctx.conn, result["request_id"], ctx.now_iso, pi_user_evidence=frozen)
            result["context"].update(materials=evidence)
            result["context"]["payload"] = db.request_payload(db.get_request(ctx.conn, result["request_id"]))
        except (util.KanpilotError, OSError, ValueError) as exc:
            detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
            cmd_report(ctx, dict(identity, outcome="needs_user", failure_code="review_blocked", note=detail))
            return {"claimed": False, "reason": "review_blocked", "request_id": result["request_id"],
                    "run_id": result["run_id"], "detail": detail}
    if request_backend(ctx.conn, request) != "pi":
        return result
    # The legacy pi runner receives a generic prompt and performs its own core
    # settlement. Prepare its fixed inputs deterministically after releasing SQL.
    from . import pi_desktop_review as review, pi_desktop_controller
    identity = dict(task_id=task["task_id"], request_id=result["request_id"], run_id=result["run_id"],
                    expected_execution_run_id=task["run_id"])
    prepared = None
    try:
        core.require_review_identity(ctx.conn, identity, task, util.now_dt())
        delivery = core.delivery_of(task)
        source = Path(delivery["worktree_path"]) if core.requires_branch_review(task) else Path(
            cmd_read.cmd_get_task(ctx, {"task_id": task["task_id"]})["task"]["execution_round"]["worktree_path"])
        task_dto = cmd_read.cmd_get_task(ctx, {"task_id": task["task_id"]})["task"]
        prepared = dict(request_id=result["request_id"], run_id=result["run_id"],
                        execution_run_id=task["run_id"], execution_worktree=str(source))
        snapshot = None if core.requires_branch_review(task) else review.review_snapshot.freeze(source)
        root = review.prepare_pooled_worktree(ctx.conn, ctx.db_path, prepared, task_dto, source, snapshot)
        directory = pi_desktop_controller.invocation_dir("review", result["run_id"])
        materials = review.collect_materials(db_path=ctx.db_path, conn=ctx.conn,
            materials=directory / "materials", task=task_dto,
            execution_worktree=source, execution_run_id=task["run_id"], snapshot=snapshot)
        prepared.update(review_root=str(root), materials=materials["files"])
        if snapshot is None:
            review._freeze_branch_job(prepared, task_dto, materials)
            review._guard_branch_job(prepared, task_dto)
        else:
            review._freeze_manual_job(prepared, snapshot, materials)
            review._guard_manual_job(prepared)
        from . import task_workspace
        owner_pid = os.environ.get("KANPILOT_EXECUTOR_OWNER_PID")
        task_workspace.pool_owner(ctx.conn, prepared, state="running",
                                  pid=int(owner_pid) if owner_pid and owner_pid.isdigit() else os.getpid())
        prepared["send_state"] = "sent"
        with db.write_txn(ctx.conn):
            core.require_review_identity(ctx.conn, identity,
                                         db.require_task(ctx.conn, task["task_id"]), util.now_dt())
            db.merge_request_payload(ctx.conn, result["request_id"], ctx.now_iso,
                                     pi_branch_review=prepared)
        result["context"].update(review_root=str(root), materials=materials["files"],
                                  **({"review_branch_snapshot": prepared["review_branch_snapshot"]}
                                     if prepared.get("review_branch_snapshot") else {}))
        result["context"]["payload"] = db.request_payload(db.get_request(ctx.conn, result["request_id"]))
        return result
    except (util.KanpilotError, OSError, ValueError) as exc:
        if isinstance(exc, util.KanpilotError) and exc.code == "worktree_pool_waiting":
            return {"claimed": False, "reason": exc.code, "request_id": result["request_id"]}
        detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
        cmd_report(ctx, dict(identity, outcome="needs_user", failure_code="review_blocked", note=detail))
        if prepared and prepared.get("pool_lease"):
            from . import task_workspace
            task_workspace.release_pool(ctx.db_path, prepared,
                                         pi_desktop_controller.invocation_dir("review", result["run_id"]))
        return {"claimed": False, "reason": "review_blocked", "request_id": result["request_id"],
                "run_id": result["run_id"], "detail": detail}


def require_pi_review_materials(ctx, payload, task):
    """Validate the default pi review's persisted inputs before core writes."""
    if not payload.get("request_id"):
        return
    request = db.get_request(ctx.conn, payload["request_id"])
    prepared = db.request_payload(request).get("pi_branch_review") if request is not None else None
    evidence = db.request_payload(request).get("pi_user_evidence") if request is not None else None
    if evidence:
        for path, sha in evidence["hashes"].items():
            util.require(Path(path).is_file() and not Path(path).is_symlink() and util.sha256_file(Path(path)) == sha,
                         "用户补充证据冻结材料已变化", "review_blocked")
    if prepared is not None:
        from . import pi_desktop_review as review
        if prepared.get("review_branch_snapshot"):
            review._guard_branch_job(prepared, dict(task))
        else:
            review._guard_manual_job(prepared)


def _claim(ctx, payload: dict, scope_id: Optional[str] = None, operation_id: Optional[str] = None,
              desktop_claim: Optional[dict] = None) -> dict:
    conn, now = ctx.conn, ctx.now
    scope_id = scope_id or payload.get("scope_id")
    operation_id = operation_id or payload.get("operation_id")
    stage, executor = payload["stage"], payload["executor"]
    if stage not in constants.STAGES:
        raise util.KanpilotError("bad_request", f"invalid stage {stage}")
    if desktop_claim is not None:
        if stage not in ("plan", "review") or not isinstance(desktop_claim, dict):
            raise util.bad_request("Desktop claim only supports plan/review")
        if not all(isinstance(desktop_claim.get(k), str) and desktop_claim[k] for k in ("operation_id", "token_hash")):
            raise util.bad_request("Desktop claim operation identity is required")
    lease_seconds = db.get_setting(
        conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS
    )

    dispatch_candidate = None
    with db.write_txn(conn):
        if desktop_claim is not None:
            cached = desktop_claim_receipt(conn, stage, desktop_claim)
            if cached is not None:
                return cached
            config = db.get_setting(conn, "desktop_queue_config", {}) or {}
            if not config.get("enabled", False):
                return {"claimed": False, "reason": "disabled"}
        if scope_id and operation_id:
            cached = runtime_operations.claim_gate(conn, scope_id, operation_id, payload)
            if cached is not None:
                return cached
        cmd_read.tick_inline(conn, now)
        if db.paused(conn, stage):
            return {"claimed": False, "reason": "paused"}
        request = _select_candidate(conn, stage, payload.get("request_id"), now, payload.get("authorized_task_id"), desktop=desktop_claim is not None)
        if request is not None and not _backend_matches(conn, request, desktop_claim is not None):
            return {"claimed": False, "reason": "backend_mismatch", "request_id": request["request_id"]}
        if stage == "review":
            from . import review_config
            while request is not None and request["status"] == "queued":
                candidate_task = db.get_task(conn, request["task_id"])
                if candidate_task is None or not db.task_schedulable(candidate_task) or not core.is_automation_managed(candidate_task) or request_backend(conn, request) in constants.AUTOMATION_REVIEW_BACKENDS:
                    break
                core.fail_request(conn, request, util.to_iso(now), "自动化 Review 后端需为 Plus/Claude/Codex", actor=executor)
                core.block_task(conn, candidate_task, util.to_iso(now), "自动化 Review 后端不支持", "review_backend_unsupported", executor, stage="review")
                if payload.get("request_id"):
                    return {"claimed": False, "reason": "review_backend_unsupported"}
                request = _select_candidate(conn, stage, None, now, payload.get("authorized_task_id"), desktop=desktop_claim is not None)
        # 定点领取：请求已在途 → already_claimed（先于 stage_busy/empty）。
        if request is not None and request["status"] == "claimed":
            return {
                "claimed": False, "reason": "already_claimed",
                "request_id": request["request_id"],
            }
        # 单飞（§4.2 + §5.22.1）：dispatch/plan 同 stage 同时只允许一个在途。
        if stage in ("dispatch", "plan") and conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE stage=? "
            "AND status='claimed'", (stage,)
        ).fetchone()["n"]:
            return {"claimed": False, "reason": "stage_busy"}
        if request is None:
            return {"claimed": False, "reason": "empty"}
        if request["status"] != "queued":
            raise util.KanpilotError(
                "conflict",
                f"request {request['request_id']} is {request['status']}",
            )
        task = (
            db.get_task(conn, request["task_id"])
            if request["task_id"]
            else None
        )
        if task is not None and request["kind"] == "plan":
            from . import task_workspace
            task_workspace.require_no_workspace_repair(conn, task["task_id"])
        # §5.15.3：归档/回收站/恢复后暂停的任务不可领取（调度入口排除）。
        if task is not None and not db.task_schedulable(task):
            return {
                "claimed": False,
                "reason": (
                    "task_archived"
                    if not db.task_is_active(task)
                    else "task_paused"
                ),
                "request_id": request["request_id"],
            }
        # v2 §5.20.1：purged 任务的请求一律拒绝。
        if db.task_is_purged(task):
            return {
                "claimed": False, "reason": "task_purged",
                "request_id": request["request_id"],
            }
        # v2 §5.22.2：未授权 plan 请求，定点 claim 同样拒绝。
        if request["kind"] == "plan" and not db.request_payload(request).get(
            "manual_authorized"
        ):
            return {
                "claimed": False, "reason": "unauthorized_plan",
                "request_id": request["request_id"],
            }
        # v2 §5.22.4：hold 未解除前 dispatch 不得领取（定点也一样）。
        if (
            request["kind"] == "dispatch"
            and task is not None
            and db.task_extra(task).get("plan_execution_hold")
        ):
            return {
                "claimed": False, "reason": "plan_hold",
                "request_id": request["request_id"],
            }
        if request["kind"] == "dispatch":
            if task is None:
                core.fail_request(
                    conn, request, util.to_iso(now),
                    "task missing for dispatch request", actor=executor,
                )
                return {
                    "claimed": False, "reason": "guard_failed",
                    "request_id": request["request_id"],
                    "detail": "task missing for dispatch request",
                }
            dispatch_candidate = (request["request_id"], task["task_id"])
        else:
            reason = core.guard_check(task, request["kind"])
            if reason is not None:
                core.fail_request(conn, request, util.to_iso(now), reason,
                                  actor=executor)
                return {
                    "claimed": False, "reason": "guard_failed",
                    "request_id": request["request_id"], "detail": reason,
                }
            if stage == "review" and task is not None and core.requires_branch_review(task):
                core.require_committed_review(task, verify_worktree=False)
            run_id = _claim_row(conn, request, task, executor, lease_seconds, now)
            if stage == "review" and task is not None and core.requires_branch_review(task):
                db.merge_request_payload(conn, request["request_id"], util.to_iso(now),
                                         review_branch_snapshot=core.branch_review_snapshot(task))
            if desktop_claim is not None:
                binding = dict(desktop_claim, role=stage, run_id=run_id)
                db.merge_request_payload(conn, request["request_id"], util.to_iso(now), desktop_claim=binding)
                _claimed_request(conn, request["request_id"], run_id, now)
            context = {
                "kind": request["kind"],
                "payload": db.request_payload(
                    db.get_request(conn, request["request_id"])
                ),
                "lease_seconds": lease_seconds,
            }
            if task is not None and request["kind"] == "plan":
                core.transition_task(
                    conn, task, "planning", util.to_iso(now), executor,
                    detail={"via": "claim"},
                )
                task = db.require_task(conn, task["task_id"])
                # v2 §5.22.3 + v4 §5.24.3：规划轮次登记索引（供会话/运行记录与
                # 日志路径使用）。backend 按请求固化的 planner_backend 写（缺省
                # codex，旧请求零迁移）；快照取该分支真值——codex 为
                # model_config，Plus 为 runtime_config（脱敏，无 token）。
                # claim 时即写对，避免 Plus 轮次在 runner 刷新前、或早期失败时
                # 谎称 codex（轮次元数据是执行证据的一部分）。
                plan_payload = context["payload"]
                plan_backend = plan_payload.get("planner_backend")
                if plan_backend not in constants.PLANNER_BACKENDS:
                    plan_backend = constants.PLANNER_BACKEND
                plan_snapshot = (
                    plan_payload.get("model_config")
                    if plan_backend == "codex"
                    else plan_payload.get("runtime_config")
                )
                round_store.upsert_on_claim(
                    conn, task["task_id"], run_id,
                    {"backend": plan_backend, "planner": plan_snapshot},
                    util.to_iso(now), backend=plan_backend,
                )
                input_id = context["payload"].get("input_id")
                context["input_id"] = input_id
                context["input_dir"] = str(
                    planner_input_dir(conn, input_id)
                ) if input_id else None
            if task is not None:
                context["task_id"] = task["task_id"]
                context["task"] = derive.task_summary(conn, task, now)
                if desktop_claim is not None and stage == "review":
                    round_store.upsert_on_claim(
                        conn, task["task_id"], run_id,
                        {"backend": "codex_desktop", "review": context["payload"].get("review_config")},
                        util.to_iso(now), backend="codex_desktop",
                    )
            res = {
                "claimed": True, "request_id": request["request_id"],
                "run_id": run_id, "context": context,
            }
            if desktop_claim is not None:
                receipts = db.get_setting(conn, "desktop_claim_receipts", {}) or {}
                receipts[desktop_claim["operation_id"]] = {
                    "role": stage, "token_hash": desktop_claim["token_hash"],
                    "request_id": request["request_id"], "run_id": run_id, "response": res,
                }
                db.set_setting(conn, "desktop_claim_receipts", receipts)
            if scope_id and operation_id:
                runtime_operations.complete_operation(
                    conn,
                    scope_id=scope_id,
                    operation_id=operation_id,
                    status="accepted",
                    result=res,
                )
            return res
    # ---- dispatch: validate outside the write txn, then re-claim ----------
    request_id, task_id = dispatch_candidate
    request = db.get_request(conn, request_id)
    task = db.require_task(conn, task_id)
    reason = core.guard_check(task, "dispatch")
    if reason is not None:
        # Plain status-guard miss: fail the request only (§4.2-5), the task
        # itself is not blocked — deterministic validation failures are.
        with db.write_txn(conn):
            request = db.get_request(conn, request_id)
            if request["status"] == "queued":
                core.fail_request(
                    conn, request, util.to_iso(now), reason, actor=executor
                )
        return {
            "claimed": False, "reason": "guard_failed",
            "request_id": request_id, "detail": reason,
            "code": "guard_failed",
        }
    try:
        checked = dispatch_validate.validate_dispatch(conn, task, now)
    except util.KanpilotError as exc:
        return dispatch_validate.handle_dispatch_guard_failure(
            conn, request, task, exc, util.to_iso(now), executor
        )

    with db.write_txn(conn):
        if scope_id and operation_id:
            cached = runtime_operations.claim_gate(conn, scope_id, operation_id, payload)
            if cached is not None:
                return cached
        request = db.get_request(conn, request_id)
        task = db.require_task(conn, task_id)
        if not engine_claim_scope.allows(conn, request, util.now_dt()):
            return {"claimed": False, "reason": "engine_project_scope"}
        # §4.2-4：并发竞争失败（校验期间被他方领取）→ already_claimed；
        # 其他非 queued 状态（failed/succeeded/cancelled）→ conflict。
        if request["status"] == "claimed":
            return {
                "claimed": False, "reason": "already_claimed",
                "request_id": request_id,
            }
        if request["status"] != "queued":
            raise util.KanpilotError(
                "conflict",
                f"request {request_id} is {request['status']}",
            )
        if db.paused(conn, stage):
            return {"claimed": False, "reason": "paused"}
        if conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE stage='dispatch' "
            "AND status='claimed'"
        ).fetchone()["n"]:
            return {"claimed": False, "reason": "stage_busy"}
        # Re-verify guard + plan hash inside the txn (queue may have moved).
        reason = core.guard_check(task, "dispatch")
        if reason is not None:
            core.fail_request(conn, request, util.to_iso(now), reason,
                              actor=executor)
            return {
                "claimed": False, "reason": "guard_failed",
                "request_id": request_id, "detail": reason,
                "code": "guard_failed",
            }
        # v2 §5.22.4：校验期间若有新 publish，hold 重新置位或哈希变化 →
        # 拒绝本次 claim（确认闸不被竞态绕过）。
        if db.task_extra(task).get("plan_execution_hold"):
            return {
                "claimed": False, "reason": "plan_hold",
                "request_id": request_id,
            }
        if (checked["plan_sha256"] != task["plan_sha256"]
                or util.sha256_file(Path(checked["plan_path"]))
                != checked["plan_sha256"]):
            core.fail_request(
                conn, request, util.to_iso(now),
                "plan changed between validation and claim", actor=executor,
            )
            return {
                "claimed": False, "reason": "guard_failed",
                "request_id": request_id,
                "detail": "plan changed between validation and claim",
                "code": "validation_failed",
            }
        repair = core.review_repair_of(task)
        if repair:
            util.require(repair.get("state") == "queued" and checked.get("review_repair") == repair,
                         "修复材料在领取期间已变化", "conflict")
        run_id = _claim_row(conn, request, task, executor, lease_seconds, now)
        if repair:
            db.set_extra(conn, task, util.to_iso(now), fix_request=dict(repair, state="running", repair_execution_run_id=run_id))
        session_marker = f"{task_id}/{run_id}"
        # v3（protocol §5.16.1/§5.23.3）：claim 按 exec_config.backend 固化
        # 轮次索引行与后端标识（该轮此后不受 set-exec-config 影响）。
        config_snapshot = checked["exec_config"]
        backend = checked["backend"]
        round_store.upsert_on_claim(
            conn, task_id, run_id, config_snapshot, util.to_iso(now),
            backend=backend,
        )
        db.update_task(
            conn, task_id, util.to_iso(now),
            status="dispatching", stage="dispatch",
            run_id=run_id, session_marker=session_marker,
            sent=0, send_state="not_sent",
            dispatch_lease_expires_at=util.plus_seconds(lease_seconds, now),
            original_todo_slug=checked["original_todo_slug"],
            kaneo_status=checked["kaneo_status"],
            kaneo_checked_at=util.to_iso(now),
            requirements_sha256=checked["requirements_sha256"],
            ui_evidence=None, send_evidence=None,
            transition_started_at=None, transition_verified_at=None,
            sent_at=None, session_id=None, session_title=None,
            **core.clear_block_fields(),
        )
        if repair.get("source_kind") == "merge_conflict":
            from . import task_ai_action
            task_ai_action.record_progress(conn, db.require_task(conn, task_id), util.to_iso(now),
                                           "running", execution_run_id=run_id)
        db.set_setting(conn, "dispatch_serial", run_id)
        db.add_event(
            conn, util.to_iso(now), "status_change", task_id=task_id,
            actor=executor,
            detail={"from": "ready", "to": "dispatching"},
        )
        context = {
            "task_id": task_id,
            "plan_path": checked["plan_path"],
            "attachment_name": checked["attachment_name"],
            "repository": checked["repository"],
            "project_id": checked["project_id"],
            "plan_kind": checked["plan_kind"],
            "session_marker": session_marker,
            # v3（§5.23.3）：指令形态按后端分支——pi 读计划文件，pi_desktop
            # 由 worker 以已核验计划正文自行组装 prompt（见 pi_desktop_worker）。
            "instruction": constants.DISPATCH_INSTRUCTION_PI.format(
                marker=session_marker, plan_path=checked["plan_path"]
            ),
            "original_todo_slug": checked["original_todo_slug"],
            "requirements_sha256": checked["requirements_sha256"],
            "source_sha256": checked["source_sha256"],
            "lease_seconds": lease_seconds,
            "backend": backend,
            "exec_config": config_snapshot,
        }
        from . import cmd_dispatch
        if cmd_dispatch._manual_branch_delivery(task):
            delivery = core.delivery_of(task)
            context["worktree_path"] = delivery["worktree_path"]
            context["instruction"] = cmd_dispatch._execution_instruction(dict(task, session_marker=session_marker), delivery["worktree_path"], desktop=(backend == "pi_desktop"), conn=conn)
        elif core.is_pr_delivery(task):
            delivery = core.delivery_of(task)
            context["instruction"] = cmd_dispatch.branch_instruction(conn, task).format(marker=session_marker, worktree=delivery["worktree_path"], branch=delivery["branch"], plan_ref="下面已核验的计划正文" if backend == "pi_desktop" else "计划文件 " + checked["plan_path"])
        res = {
            "claimed": True, "request_id": request_id,
            "run_id": run_id, "context": context,
        }
        if scope_id and operation_id:
            runtime_operations.complete_operation(
                conn,
                scope_id=scope_id,
                operation_id=operation_id,
                status="accepted",
                result=res,
            )
        return res


def cmd_mcp_claim(ctx, payload: dict) -> dict:
    """Atomic claim + runtime receipt write in a single core transaction."""
    scope_id = payload.get("scope_id")
    operation_id = payload.get("operation_id")
    if not scope_id or not operation_id:
        raise util.bad_request("mcp-claim requires scope_id and operation_id")

    if payload.get("stage") not in ("dispatch", "monitor"):
        raise util.bad_request("mcp-claim only supports dispatch/monitor")
    if payload.get("executor") != "kaneo-" + payload["stage"]:
        raise util.bad_request("executor does not match stage")
    result = cmd_claim(ctx, payload, scope_id=scope_id, operation_id=operation_id)
    # Successful claims wrote the receipt with the same transaction.
    # Store no-work results too, so lost replies never select another request.
    if not result.get("claimed"):
        with db.write_txn(ctx.conn):
            op = runtime_operations.begin_operation(
                ctx.conn, scope_id, operation_id, "core", "kp_task_claim", payload)
            if op["status"] == "accepted":
                return op["result"]
            runtime_operations.complete_operation(
                ctx.conn, scope_id, operation_id, result=result)
    return result


def _claimed_request(conn, request_id: str, run_id: str, now=None) -> sqlite3.Row:
    request = db.get_request(conn, request_id)
    if request is None:
        raise util.KanpilotError("not_found", f"request not found: {request_id}")
    if request["status"] != "claimed" or request["run_id"] != run_id:
        raise util.KanpilotError(
            "stale_run",
            f"request {request_id} status={request['status']} "
            f"run={request['run_id']}",
        )
    if request_backend(conn, request) == "codex_desktop":
        now = now or util.now_dt()
        expires = util.parse_ts(request["lease_expires_at"])
        if expires is None or expires <= now:
            raise util.KanpilotError("stale_run", "Desktop claim lease expired")
        task = db.get_task(conn, request["task_id"]) if request["task_id"] else None
        if task is None or not db.task_is_active(task) or db.task_is_purged(task):
            raise util.KanpilotError("stale_run", "Desktop task is no longer active")
        frozen = db.request_payload(request)
        if request["stage"] == "review" and (
            not frozen.get("expected_execution_run_id") or frozen["expected_execution_run_id"] != task["run_id"]
            or not frozen.get("plan_sha256") or frozen["plan_sha256"] != task["plan_sha256"]
        ):
            raise util.KanpilotError("stale_run", "Desktop Review execution or plan identity changed")
    return request


def cmd_heartbeat(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    with db.write_txn(conn):
        request = _claimed_request(conn, payload["request_id"], payload["run_id"], now)
        lease_seconds = db.get_setting(
            conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS
        )
        expires = util.plus_seconds(lease_seconds, now)
        db.update_request(
            conn, request["request_id"], util.to_iso(now),
            heartbeat_at=util.to_iso(now), lease_expires_at=expires,
        )
        if payload.get("progress") and request["task_id"]:
            db.update_task(
                conn, request["task_id"], util.to_iso(now),
                last_progress=payload["progress"],
            )
        db.add_event(
            conn, util.to_iso(now), "heartbeat", task_id=request["task_id"],
            request_id=request["request_id"], actor=request["claimed_by"],
        )
        return {"ok": True, "lease_expires_at": expires}


def cmd_report(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    outcome = payload["outcome"]
    if outcome not in ("succeeded", "failed", "needs_user"):
        raise util.KanpilotError("bad_request", f"invalid outcome {outcome}")
    with db.write_txn(conn):
        request = _claimed_request(conn, payload["request_id"], payload["run_id"], now)
        if request["stage"] == "review" and payload.get("expected_execution_run_id"):
            core.require_review_identity(conn, payload,
                                         db.require_task(conn, request["task_id"]), now)
        now_iso = util.to_iso(now)
        final = "succeeded" if outcome == "succeeded" else "failed"
        result = {
            "outcome": outcome,
            "note": payload.get("note"),
            "send_state": payload.get("send_state"),
            "kaneo_status": payload.get("kaneo_status"),
            # v6: persisted so the engine's ladder-1 reconciler can detect a
            # transient failure and re-enqueue with backoff.
            "failure_code": payload.get("failure_code"),
        }
        core.finish_request(
            conn, request, now_iso, final, outcome, result=result,
            note=payload.get("note"), send_state=payload.get("send_state"),
            evidence=payload.get("evidence"),
        )
        task = (
            db.get_task(conn, request["task_id"])
            if request["task_id"]
            else None
        )
        # v2 §5.20.1：purged 任务拒绝写入口（claimed 请求属历史残留）。
        if db.task_is_purged(task):
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除，不能再汇报"
            )
        if payload.get("progress") and task is not None:
            db.update_task(
                conn, task["task_id"], now_iso,
                last_progress=payload["progress"],
            )
        if payload.get("kaneo_status") and task is not None:
            db.update_task(
                conn, task["task_id"], now_iso,
                kaneo_status=payload["kaneo_status"], kaneo_checked_at=now_iso,
            )
            db.add_event(
                conn, now_iso, "kaneo_observed", task_id=task["task_id"],
                actor=request["claimed_by"],
                detail={"to": payload["kaneo_status"], "via": "report"},
            )
        if payload.get("send_state") and task is not None:
            db.update_task(
                conn, task["task_id"], now_iso,
                send_state=payload["send_state"],
            )
        if (
            outcome == "failed"
            and request["kind"] == "plan"
            and task is not None
            and task["status"] == "planning"
        ):
            # v2 §5.22.3：规划失败（超时/崩溃/不合规/发布被拒）→ 回 needs_plan，
            # 等用户在规划面板重新准备材料提交（不自动重试/自动建请求）。
            core.transition_task(
                conn, task, "needs_plan", now_iso,
                request["claimed_by"] or "kaneo-planner",
                detail={
                    "via": "plan_report_failed",
                    "failure_code": payload.get("failure_code"),
                },
                failure_code=payload.get("failure_code") or "planner_failed",
                failure_reason=payload.get("failure_reason")
                or payload.get("note"),
            )
            task = db.get_task(conn, task["task_id"])
            if core.is_automation_managed(task):
                attempts = core.automation_of(task).get("plan_attempts", 0)
                if attempts < 2:
                    core.update_automation(conn, task, now_iso, plan_wanted=True)
                else:
                    core.block_task(conn, task, now_iso, payload.get("note") or "自动规划两次失败", "planner_failed", request["claimed_by"], stage="plan")
                task = db.get_task(conn, task["task_id"])
        if outcome == "needs_user" and task is not None:
            # §4.7 (v1.2): an already-blocked task keeps its original
            # failure_stage/failure_code unless the payload refines the code.
            already_blocked = task["status"] == "needs_user"
            core.block_task(
                conn, task, now_iso,
                payload.get("failure_reason") or payload.get("note")
                or task["failure_reason"]
                or "executor reported needs_user",
                payload.get("failure_code")
                or (task["failure_code"] if already_blocked else None)
                or "reported",
                request["claimed_by"],
                stage=(
                    task["failure_stage"] or task["stage"]
                    if already_blocked else None
                ),
                send_state=payload.get("send_state", task["send_state"]),
            )
        if outcome == "needs_user" and task is not None and request["kind"] == "plan":
            from . import plan_answers
            plan_answers.register(conn, db.require_task(conn, task["task_id"]), request, now_iso, payload.get("evidence"))
        if (
            outcome == "succeeded"
            and request["kind"] == "monitor"
            and task is not None
            and task["status"] == "needs_user"
            and task["failure_code"] == "observation_blocked"
        ):
            # §5.15.4：监控连接恢复（monitor 观测成功）自动清除观测异常，
            # 任务回 running；发送不明/人工阻塞/失联不适用此自动解除。
            core.transition_task(
                conn, task, "running", now_iso, request["claimed_by"],
                detail={"via": "monitor_recovered"},
                **core.clear_block_fields(),
            )
            db.add_event(
                conn, now_iso, "resolved", task_id=task["task_id"],
                request_id=request["request_id"], actor=request["claimed_by"],
                detail={"action": "observation_recovered", "via": "monitor"},
            )
            task = db.get_task(conn, task["task_id"])
        db.add_event(
            conn, now_iso, "reported", task_id=request["task_id"],
            request_id=request["request_id"], actor=request["claimed_by"],
            detail=result,
        )
        # Scheduler follow-ups.
        if request["kind"] == "plan-scan" and outcome == "succeeded":
            db.set_setting(conn, "last_plan_scan_at", now_iso)
        # An explicitly authorized repair in a legacy project is also served by
        # engine-only installations, whose broad business tick only covers live
        # projects. Keep just this run's observation chain alive.
        if task is not None and outcome == "succeeded" and request["kind"] in ("dispatch", "monitor"):
            current = db.require_task(conn, task["task_id"])
            repair = core.review_repair_of(current)
            if (current["status"] == "running" and repair.get("state") == "running"
                    and repair.get("authorized_by") == "user"
                    and repair.get("repair_execution_run_id") == current["run_id"]):
                db.create_request(conn, now_iso, "monitor", "monitor:" + current["task_id"],
                    task_id=current["task_id"], requested_by="scheduler",
                    not_before=util.plus_seconds(util.monitor_interval(conn), now) if request["kind"] == "monitor" else None)
        # Only this explicitly authorized action continues through delivery in
        # legacy/shadow engines; its pass receipt survives fix_request cleanup.
        if task is not None and outcome == "succeeded" and request["kind"] in ("review", "delivery"):
            current = db.require_task(conn, task["task_id"])
            if engine_claim_scope.action_delivery_allowed(conn, current, allow_paused=True):
                from . import task_ai_action
                continuation = task_ai_action.delivery_continuation(current, allow_paused=True)
                expected_review = continuation["review_identity"]
                if request["kind"] == "review":
                    current_request = (request["request_id"] == expected_review["request_id"]
                                       and request["run_id"] == expected_review["run_id"])
                else:
                    current_request = engine_claim_scope.action_delivery_allowed(conn, current, request, allow_paused=True)
                if current_request:
                    db.create_request(conn, now_iso, "delivery", "delivery:" + current["task_id"],
                        task_id=current["task_id"], requested_by="scheduler",
                        not_before=core.delivery_of(current).get("next_sync_at") if request["kind"] == "delivery" else None,
                        payload={"task_ai_action_id": continuation["action"]["action_id"],
                                 "expected_execution_run_id": current["run_id"],
                                 "expected_head_sha": continuation["head_sha"],
                                 "expected_plan_sha256": current["plan_sha256"]})
        # Other monitor cycles retain the existing scoped tick policy.
        fresh = db.get_request(conn, request["request_id"])
        task_status = task["status"] if task is not None else None
        if outcome == "needs_user" and task is not None:
            task_status = "needs_user"
        response = {"ok": True, "request_status": fresh["status"], "task_status": task_status}
    if outcome == "succeeded" and request["kind"] == "review" and task is not None:
        from . import review_repair
        review_repair.try_auto_start(ctx, task["task_id"])
        response["task_status"] = db.require_task(conn, task["task_id"])["status"]
    return response


def _publish_expected_model(req_payload: dict) -> Optional[dict]:
    """publish-plan 的模型快照（§5.22.6 codex / §5.24.3 Plus）。

    实现下沉到 ``planfile.expected_model_snapshot``——规划 runner 写 frontmatter
    与本处校验必须同一口径，否则 Plus 计划会被两边互相拒收。
    """
    return planfile.expected_model_snapshot(req_payload)


def cmd_publish_plan(ctx, payload: dict) -> dict:
    """v2（protocol §5.6）：事务内复核 —— 任务活跃、请求为本任务当前
    claimed 规划请求且 run_id/租约/planner_input_sha256 一致；同一事务登记
    计划字段 + task→ready + 置 plan_execution_hold + 结束请求 succeeded。"""
    conn, now = ctx.conn, ctx.now
    now_iso = util.to_iso(now)
    # §5.6 事务外核验（不占写锁）：远端复验任务仍为 To Do 且需求指纹未变；
    # 远端不可达 → remote_unavailable（不写本地状态）。
    try:
        live = remote.get_task(remote.get_client(), payload["task_id"])
    except Exception as exc:
        raise util.KanpilotError(
            "remote_unavailable", f"远端复验失败：{exc}"
        )
    live_task = live.get("task") or {}
    if live_task.get("status") not in core.expected_remote_slugs(db.require_task(conn, payload["task_id"])):
        raise util.KanpilotError(
            "conflict",
            f"远端任务已不在 To Do（{live_task.get('status')}），拒绝发布",
        )
    live_req = live.get("requirements_sha256")
    if live_req and live_req != payload["requirements_sha256"]:
        raise util.KanpilotError(
            "conflict", "远端需求已变化（requirements_sha256 不符），拒绝发布"
        )
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        # §5.20.1：purged 终态拒绝写入口。
        if db.task_is_purged(task):
            raise util.KanpilotError(
                "conflict", "任务已永久从列表移除，不能再发布计划"
            )
        request = db.get_request(conn, payload["request_id"])
        if request is None:
            raise util.KanpilotError(
                "not_found", f"request not found: {payload['request_id']}"
            )
        if request["task_id"] != task["task_id"]:
            raise util.KanpilotError(
                "conflict", "请求不属于该任务，不能发布计划"
            )
        if request["kind"] != "plan":
            raise util.KanpilotError(
                "bad_request", "publish-plan 只接受规划请求"
            )
        # §5.6 幂等：同一 run 重复发布同一计划文件 → deduplicated（网络
        # 重试/重复提交不改状态、不重复写事件）。
        if request["status"] == "succeeded":
            prev = util.jload(request["result_json"]) or {}
            plan_sha_retry = util.sha256_file(Path(payload["plan_path"]))
            if (request["run_id"] == payload["run_id"]
                    and prev.get("plan_sha256") == plan_sha_retry):
                return {
                    "ok": True, "deduplicated": True,
                    "task_status": task["status"],
                    "plan_sha256": plan_sha_retry,
                    "plan_execution_hold": bool(
                        db.task_extra(task).get("plan_execution_hold")),
                }
        request = _claimed_request(
            conn, payload["request_id"], payload["run_id"], now
        )
        if util.lease_lost(request["lease_expires_at"], now):
            raise util.KanpilotError(
                "stale_run", "规划请求租约已过期（失联），请先处理失联"
            )
        req_payload = db.request_payload(request)
        expected_input_sha = req_payload.get("planner_input_sha256")
        supplied_input_sha = payload.get("planner_input_sha256")
        if expected_input_sha and supplied_input_sha != expected_input_sha:
            raise util.KanpilotError(
                "conflict",
                "规划输入指纹与本请求登记材料不符，拒绝发布",
            )
        if task["status"] not in ("planning", "needs_plan", "needs_user"):
            raise util.KanpilotError(
                "conflict",
                f"task {task['task_id']} is {task['status']}; "
                "refusing to overwrite dispatch inputs",
            )
        plan_kind = payload["plan_kind"]
        if plan_kind not in ("standard", "review_fix"):
            raise util.KanpilotError("bad_request", "invalid plan_kind")
        meta, raw, path = planfile.load_plan(
            payload["plan_path"], task["task_id"]
        )
        # §5.22.6 + §5.24.3 + T1：frontmatter 的 planner_model/reasoning_effort 必须
        # 匹配本请求固化的运行快照；项目与仓库必须匹配固化的 project_snapshot。
        req_project_snapshot = req_payload.get("project_snapshot")
        manifest_task = (req_payload.get("manifest") or {}).get("task") or {}
        task_dict = dict(task)
        expected_project = req_project_snapshot or (
            {
                "project_id": manifest_task.get("project_id") or task_dict.get("project_id"),
                "project_name": manifest_task.get("project_name") or task_dict.get("project_name"),
                "repository": manifest_task.get("repository") or task_dict.get("repository"),
            } if (manifest_task.get("project_id") or task_dict.get("project_id")) else None
        )
        planfile.check_plan_meta(
            meta, plan_kind,
            expected_model=_publish_expected_model(req_payload),
            expected_project=expected_project,
        )
        if meta.get("plan_kind") not in (None, plan_kind):
            raise util.KanpilotError(
                "validation_failed", "plan_kind frontmatter conflicts"
            )
        util.require(
            meta.get("task_id") == task["task_id"], "plan task_id mismatch"
        )
        util.require(
            meta.get("source_sha256") == payload["source_sha256"],
            "source_sha256 payload/frontmatter mismatch",
        )
        util.require(
            meta.get("requirements_sha256") == payload["requirements_sha256"],
            "requirements_sha256 payload/frontmatter mismatch",
        )
        if task["source_sha256"]:
            util.require(
                task["source_sha256"] == payload["source_sha256"],
                "source_sha256 differs from task baseline",
            )
        if task["requirements_sha256"]:
            util.require(
                task["requirements_sha256"] == payload["requirements_sha256"],
                "requirements_sha256 differs from task baseline",
            )
        if task["repository"]:
            util.require(
                meta["repository"] == task["repository"],
                "plan repository differs from task repository",
            )
        plan_sha = util.sha256_bytes(raw)
        fields = {
            "plan_path": str(path), "plan_sha256": plan_sha,
            "plan_kind": plan_kind,
            "plan_generated_at": payload["generated_at"],
            "planner_model": payload["planner_model"],
            "reasoning_effort": payload["reasoning_effort"],
            "source_sha256": payload["source_sha256"],
            "requirements_sha256": payload["requirements_sha256"],
            "repository": meta["repository"],
        }
        if plan_kind == "review_fix" and meta.get("fix_mode") != "same_task":
            report = util.inside(
                Path(meta["review_report_path"]),
                constants.review_reports_root(),
                "review_report_path",
            )
            util.require(report.is_file(), "review report missing")
            util.require(
                util.sha256_file(report) == meta["review_report_sha256"],
                "review report sha256 mismatch",
            )
            fields.update(
                {
                    "review_report_path": str(report),
                    "review_report_sha256": meta["review_report_sha256"],
                    "review_key": meta["review_key"],
                    "review_source_task_id": meta["review_source_task_id"],
                }
            )
        if meta.get("fix_mode") == "same_task":
            fix = db.task_extra(task).get("fix_request") or {}
            util.require(core.is_automation_managed(task), "same_task 仅用于自动化任务")
            util.require(meta.get("review_source_task_id") == task["task_id"] and meta.get("fix_round") == core.automation_of(task).get("fix_round"), "same_task 修复轮身份不符")
            for key in ("review_report_path", "review_report_sha256", "review_key"):
                util.require(meta.get(key) == fix.get(key.replace("review_report_", "report_")), "same_task " + key + " 不符")
            report = util.inside(Path(meta["review_report_path"]), constants.review_reports_root(), "review_report_path")
            util.require(report.is_file() and util.sha256_file(report) == meta["review_report_sha256"], "修复报告哈希不符")
        task = db.require_task(conn, task["task_id"])
        extra = db.task_extra(task)
        # v2 §5.22.4：发布与执行分离——计划登记后置 hold，等用户确认。
        from . import verify_evidence
        verification = verify_evidence.parse_verify_block(raw.decode("utf-8"))
        extra["plan_verify"] = {"plan_sha256": plan_sha, **({"commands": verification["commands"]} if verification["ok"] else {"error": verification["error"]})}
        extra["plan_execution_hold"] = True
        fields["extra_json"] = extra
        core.transition_task(
            conn, task, "ready", now_iso,
            actor=request["claimed_by"] or "kaneo-planner",
            detail={"via": "publish-plan"}, **fields,
        )
        # 成功路径结束请求（同一事务闭合 attempt），不再另调 report。
        core.finish_request(
            conn, db.get_request(conn, request["request_id"]), now_iso,
            "succeeded", "succeeded",
            result={"outcome": "succeeded", "plan_sha256": plan_sha},
            note="plan published",
        )
        # v2 §5.22.5：review_fix 计划写回原评审任务的 repair_plan_*。
        if plan_kind == "review_fix" and meta.get("fix_mode") != "same_task":
            source = db.get_task(conn, meta["review_source_task_id"])
            if source is not None and not db.task_is_purged(source):
                db.set_extra(
                    conn, source, now_iso,
                    repair_plan_path=str(path),
                    repair_plan_sha256=plan_sha,
                )
        db.add_event(
            conn, now_iso, "plan_published", task_id=task["task_id"],
            request_id=request["request_id"],
            actor=request["claimed_by"] or "kaneo-planner",
            detail={"plan_path": str(path), "plan_sha256": plan_sha,
                    "plan_kind": plan_kind,
                    "planner_input_sha256": supplied_input_sha},
        )
        return {"ok": True, "task_status": "ready", "plan_sha256": plan_sha,
                "plan_execution_hold": True}


def cmd_register_review(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now
    conclusion = payload["conclusion"]
    if conclusion not in ("pass", "changes_requested", "blocked"):
        raise util.KanpilotError("bad_request", f"invalid conclusion {conclusion}")
    if conclusion == "changes_requested" and not (
        payload.get("review_key") and (payload.get("fix_task_id") or (payload.get("fix_mode") == "same_task" and payload.get("fix")))
    ):
        raise util.KanpilotError(
            "bad_request",
            "changes_requested requires fix_task_id and review_key",
        )
    # Git IO must finish before acquiring SQLite's write lock. The transaction
    # below repeats identity/frozen-version checks before recording a result.
    review_task = db.require_task(conn, payload["task_id"])
    core.require_review_identity(conn, payload, review_task, now)
    core.require_committed_review(review_task)
    require_pi_review_materials(ctx, payload, review_task)
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        # v4（§5.24.5）：新自动链路必须带 request_id/run_id/expected_execution_run_id；
        # 无身份的旧手动调用保持兼容（guard 直接返回 None）。
        core.require_review_identity(conn, payload, task, now)
        report = util.inside(
            Path(payload["report_path"]),
            constants.review_reports_root(),
            "report_path",
        )
        util.require(report.is_file(), f"review report missing: {report}")
        report_sha = util.sha256_file(report)
        from . import task_ai_action
        action_fix = core.review_repair_of(task)
        task_ai_action.record_review_result(conn, task, ctx.now_iso, conclusion)
        task = db.require_task(conn, task["task_id"])
        if (conclusion == "changes_requested" and payload.get("fix_mode") != "same_task"
                and not core.is_automation_managed(task)):
            fix_task = db.get_task(conn, payload["fix_task_id"])
            # Historical identity-free manual calls may name a remote fix that
            # has never been imported. New settlements must bind a local child.
            util.require(fix_task is not None or not (payload.get("request_id") or payload.get("run_id")),
                         "review fix binding child not registered")
            if fix_task is not None:
                util.require(fix_task["plan_kind"] == "review_fix"
                             and fix_task["review_source_task_id"] == task["task_id"]
                             and fix_task["review_key"] == payload["review_key"]
                             and fix_task["project_id"] == task["project_id"]
                             and fix_task["repository"] == task["repository"],
                             "review fix binding does not match source/key/project/repository")
                db.update_task(conn, fix_task["task_id"], ctx.now_iso,
                               review_report_path=str(report), review_report_sha256=report_sha)
        if core.is_automation_managed(task) or payload.get("fix_mode") == "same_task":
            if payload.get("fix_task_id") or (conclusion == "changes_requested" and payload.get("fix_mode") != "same_task"):
                raise util.KanpilotError("conflict", "自动化任务必须在同任务内修复")
            if not all(payload.get(k) for k in ("request_id", "run_id", "expected_execution_run_id", "review_key")):
                raise util.KanpilotError("bad_request", "自动化 Review 缺少身份或 review_key")
            extra = db.task_extra(task)
            history = list(extra.get("review_history") or [])
            if any(h.get("review_key") == payload["review_key"] for h in history):
                return {"ok": True, "already_registered": True, "report_sha256": report_sha}
            fix = payload.get("fix")
            if conclusion == "changes_requested":
                if not isinstance(fix, dict) or set(fix) != {"title", "description", "priority"} or not all(isinstance(fix[k], str) and fix[k].strip() for k in fix) or fix["priority"] not in constants.REVIEW_FIX_PRIORITIES:
                    raise util.KanpilotError("bad_request", "修复请求字段不合法")
            elif fix is not None:
                raise util.KanpilotError("bad_request", "非修复结论不能带 fix")
            entry = {"review_key": payload["review_key"], "conclusion": conclusion,
                     "report_path": str(report), "report_sha256": report_sha,
                     "execution_run_id": task["run_id"], "head_sha": core.delivery_of(task).get("head_sha"),
                     "registered_at": ctx.now_iso, "fix_round": (core.automation_of(task).get("fix_round", 0)
                         if core.is_automation_managed(task) else db.task_extra(task).get("review_repair_round", 0)), "fix": fix}
            entry.update({key: core.delivery_of(task).get(key)
                          for key in ("branch", "base_branch", "base_sha", "worktree_path")})
            history.append(entry)
            db.set_extra(conn, task, ctx.now_iso, review_history=history[-20:])
            task = db.require_task(conn, task["task_id"])
            if conclusion == "changes_requested":
                fix_request = dict(entry, **fix, mode="direct", review_run_id=payload["run_id"],
                    source_execution_run_id=task["run_id"], task_id=task["task_id"],
                    project_id=task["project_id"], repository=task["repository"],
                    requirements_sha256=task["requirements_sha256"], plan_sha256=task["plan_sha256"],
                    created_at=ctx.now_iso, state="held")
                # Never replace task.run_id while this Review remains claimed.
                db.set_extra(conn, task, ctx.now_iso, fix_request=fix_request)
            if conclusion == "pass":
                db.set_extra(conn, task, ctx.now_iso, fix_request=None)
        elif core.requires_branch_review(task):
            from . import review_settle
            key = payload.get("review_key") or review_settle.review_key(task["task_id"], task["run_id"], report_sha)
            history = list(db.task_extra(task).get("review_history") or [])
            if not any(entry.get("review_key") == key for entry in history):
                entry = {"review_key": key, "conclusion": conclusion,
                                "report_path": str(report), "report_sha256": report_sha,
                                **core.branch_review_snapshot(task),
                                "registered_at": ctx.now_iso,
                                "fix_task_id": payload.get("fix_task_id")}
                if action_fix.get("source_kind") == "merge_conflict":
                    entry["fix_round"] = core.automation_of(task).get("fix_round", 0)
                history.append(entry)
                db.set_extra(conn, task, ctx.now_iso, review_history=history[-20:])
            payload = dict(payload, review_key=key)
        if conclusion == "pass" and core.review_repair_of(db.require_task(conn, task["task_id"])):
            db.set_extra(conn, db.require_task(conn, task["task_id"]), ctx.now_iso, fix_request=None)
        fields = {
            "review_report_path": str(report),
            "review_report_sha256": report_sha,
        }
        extra_entries = {"review_conclusion": conclusion,
                         "review_registered_at": ctx.now_iso}
        if payload.get("request_id") and payload.get("run_id"):
            extra_entries["registered_review_identity"] = {
                "request_id": payload["request_id"], "run_id": payload["run_id"],
                "execution_run_id": task["run_id"], "report_sha256": report_sha,
                "plan_sha256": task["plan_sha256"]}
        if payload.get("review_key"):
            fields["review_key"] = payload["review_key"]
        if payload.get("fix_task_id"):
            fields["fix_task_id"] = payload["fix_task_id"]
            extra_entries["fix_task_source_sha256"] = payload.get(
                "fix_task_source_sha256"
            )
        if payload.get("repair_plan_path"):
            repair = util.inside(
                Path(payload["repair_plan_path"]),
                constants.plan_root(),
                "repair_plan_path",
            )
            util.require(repair.is_file(), "repair plan missing")
            extra_entries["repair_plan_path"] = str(repair)
            extra_entries["repair_plan_sha256"] = util.sha256_file(repair)
        db.update_task(conn, task["task_id"], ctx.now_iso, **fields)
        db.set_extra(
            conn, db.require_task(conn, task["task_id"]), ctx.now_iso,
            **extra_entries,
        )
        if conclusion == "pass" and action_fix.get("source_kind") == "merge_conflict":
            task_ai_action.register_delivery_continuation(conn, db.require_task(conn, task["task_id"]),
                                                          ctx.now_iso, action_fix)
        db.add_event(
            conn, ctx.now_iso, "review_registered", task_id=task["task_id"],
            actor="kaneo-review",
            detail={"conclusion": conclusion, "report_sha256": report_sha,
                    "review_key": payload.get("review_key"),
                    "fix_task_id": payload.get("fix_task_id")},
        )
        task = db.require_task(conn, task["task_id"])
        if conclusion == "blocked" and task["status"] != "needs_user":
            core.block_task(
                conn, task, ctx.now_iso, "review blocked", "review_blocked",
                "kaneo-review",
            )
        elif (
            conclusion == "pass"
            and payload.get("kaneo_status") in constants.DONE_SLUGS
            and task["status"] != "done"
        ):
            core.transition_task(
                conn, task, "done", ctx.now_iso, "kaneo-review",
                detail={"via": "register-review pass + kaneo done"},
                kaneo_status=payload["kaneo_status"],
                kaneo_checked_at=ctx.now_iso,
            )
        elif payload.get("kaneo_status"):
            db.update_task(
                conn, task["task_id"], ctx.now_iso,
                kaneo_status=payload["kaneo_status"],
                kaneo_checked_at=ctx.now_iso,
            )
        return {"ok": True, "report_sha256": report_sha}


# ================================= Goal 完成判定登记（T5，protocol §5.23.10）
MONITOR_EXECUTOR_ID = "kaneo-monitor"
GOAL_EVALUATION_EVENT = "goal_report_evaluated"
GOAL_EVALUATION_DECISIONS = ("complete", "incomplete", "uncertain")


def require_monitor_claim(conn, request_id: str, run_id: str,
                          now) -> sqlite3.Row:
    """核对 monitor 请求仍由 ``kaneo-monitor`` 领取且租约有效（T5-C 第 1 步）。

    任一不符 → ``stale_run``（不登记、不放行）。
    """
    row = _claimed_request(conn, request_id, run_id)
    if row["kind"] != "monitor" or row["stage"] != "monitor":
        raise util.KanpilotError(
            "stale_run",
            f"request {request_id} 不是 monitor 请求（kind={row['kind']}），"
            "不能登记 Goal 完成判定",
        )
    if row["claimed_by"] != MONITOR_EXECUTOR_ID:
        raise util.KanpilotError(
            "stale_run",
            f"monitor 请求未由 {MONITOR_EXECUTOR_ID} 领取"
            f"（claimed_by={row['claimed_by']!r}）",
        )
    if util.lease_lost(row["lease_expires_at"], now):
        raise util.KanpilotError(
            "stale_run", "monitor 请求租约已过期（失联），不能登记完成判定"
        )
    return row


def goal_evaluation_signature(payload: dict) -> dict:
    """幂等签名：同一 task/execution_run/plan/report → 复用原判定事件。"""
    return {
        "task_id": payload["task_id"],
        "execution_run_id": payload["execution_run_id"],
        "plan_sha256": payload["plan_sha256"],
        "report_sha256": payload["report_sha256"],
    }


def find_goal_evaluation(conn, signature: dict):
    """按签名查找既有 ``goal_report_evaluated`` 事件（**不重复调用 AI**）。"""
    for row in conn.execute(
        "SELECT id, at, task_id, request_id, actor, detail_json FROM events "
        "WHERE kind=? AND task_id=? ORDER BY id DESC",
        (GOAL_EVALUATION_EVENT, signature["task_id"]),
    ).fetchall():
        detail = util.jload(row["detail_json"])
        if not isinstance(detail, dict):
            continue
        if (
            detail.get("execution_run_id") == signature["execution_run_id"]
            and detail.get("plan_sha256") == signature["plan_sha256"]
            and detail.get("report_sha256") == signature["report_sha256"]
        ):
            return {
                "event_id": row["id"],
                "at": row["at"],
                "request_id": row["request_id"],
                "actor": row["actor"],
                "detail": detail,
            }
    return None


def goal_evaluation_event(conn, event_id):
    """读取一条 ``goal_report_evaluated`` 事件（硬闸按 ID 复核用）。"""
    row = conn.execute(
        "SELECT id, at, task_id, request_id, actor, detail_json FROM events "
        "WHERE id=? AND kind=?",
        (event_id, GOAL_EVALUATION_EVENT),
    ).fetchone()
    if row is None:
        return None
    detail = util.jload(row["detail_json"])
    return {
        "event_id": row["id"],
        "at": row["at"],
        "task_id": row["task_id"],
        "request_id": row["request_id"],
        "actor": row["actor"],
        "detail": detail if isinstance(detail, dict) else {},
    }


def _evaluation_response(event: dict, *, deduplicated: bool) -> dict:
    detail = event.get("detail") or {}
    return {
        "ok": True,
        "deduplicated": deduplicated,
        "event_id": event.get("event_id"),
        "registered_at": event.get("at"),
        "task_id": detail.get("task_id"),
        "execution_run_id": detail.get("execution_run_id"),
        "monitor_run_id": detail.get("monitor_run_id"),
        "monitor_request_id": detail.get("monitor_request_id"),
        "plan_sha256": detail.get("plan_sha256"),
        "report_id": detail.get("report_id"),
        "report_sha256": detail.get("report_sha256"),
        "decision": detail.get("decision"),
        "reason": detail.get("reason"),
        "criterion_reviews": detail.get("criterion_reviews") or [],
        "report_state": detail.get("report_state"),
        "integrity_kind": detail.get("integrity_kind"),
        "execution_status": detail.get("execution_status"),
        "verdict": detail.get("verdict"),
        "report_turn_id": detail.get("report_turn_id"),
        "report_session_id": detail.get("report_session_id"),
        # §5.23.10 T5-C：两种轮次身份不可互换。
        "execution_run_id_is_monitor_run_id": (
            detail.get("execution_run_id") == detail.get("monitor_run_id")
        ),
    }


def cmd_record_goal_evaluation(ctx, payload: dict) -> dict:
    """``record-goal-evaluation``（protocol §5.23.10 T5-C）。

    1. 先核对 monitor 请求仍由 kaneo-monitor 领取且**租约有效**；
    2. 只读**重新取回同一报告**并核对当前任务/执行轮次/计划/报告 hash；
    3. ``decision=complete`` 时确定性核对报告底线（status=ready /
       integrity=structured / execution=completed / verdict=met / 审查项
       非空且 ID 唯一且全 met / 引用 ID 存在于报告）；
    4. 通过后写 ``goal_report_evaluated`` 事件（AI 判定、理由、引用 ID +
       两种轮次身份）。

    幂等：同 ``task_id``/``execution_run_id``/``plan_sha256``/
    ``report_sha256`` 已有判定 → 返回原事件，不重复调用 AI。新的 monitor
    请求可引用它，但**必须自己仍是有效 claim**（每次调用都先核对）。
    """
    from . import pi_desktop_backend

    conn, now = ctx.conn, ctx.now
    task_id = payload["task_id"]
    execution_run_id = payload["execution_run_id"]
    monitor_request_id = payload["monitor_request_id"]
    monitor_run_id = payload["monitor_run_id"]
    plan_sha256 = payload["plan_sha256"]
    report_sha256 = payload["report_sha256"]
    report_id = payload.get("report_id")
    decision = payload["decision"]
    reason = payload.get("reason")
    if decision not in GOAL_EVALUATION_DECISIONS:
        raise util.bad_request(f"decision 非法：{decision}")
    reviews, referenced = cmd_read.collect_criterion_reviews(
        payload.get("criterion_reviews")
    )
    if decision == "complete" and not (
        isinstance(reason, str) and reason.strip()
    ):
        raise util.bad_request("decision=complete 必须给出 reason")

    with db.write_txn(conn):
        # 1. monitor claim 与租约（幂等路径同样要求，不能被旧判定绕过）。
        require_monitor_claim(conn, monitor_request_id, monitor_run_id, now)
        task = db.require_task(conn, task_id)
        if task["run_id"] != execution_run_id:
            raise util.KanpilotError(
                "conflict",
                "execution_run_id 不是该任务的当前执行轮次"
                f"（当前 {task['run_id']}）",
            )
        signature = goal_evaluation_signature(payload)
        existing = find_goal_evaluation(conn, signature)
        if existing is not None:
            return _evaluation_response(existing, deduplicated=True)

    # 2. 只读重新取回同一报告并核对当前任务/执行轮次/计划/报告 hash。
    resolved = cmd_read.read_goal_report(
        ctx.conn, task_id, execution_run_id
    )
    if plan_sha256 != (resolved["identity"]["plan_sha256"] or ""):
        raise util.KanpilotError(
            "conflict", "plan_sha256 与任务当前登记计划哈希不符"
        )
    if resolved["state"] != "ready":
        raise util.KanpilotError(
            "conflict",
            f"报告当前不可用（state={resolved['state']}）："
            f"{resolved.get('detail') or '无正文'}",
        )
    if resolved["report_sha256"] != report_sha256:
        # 报告自上次读取后被改写：判定必须绑定**当时的**报告。
        raise util.KanpilotError(
            "conflict",
            "report_sha256 与当前读取的报告不符（报告已变化）",
        )
    if report_id and resolved.get("report_id") \
            and str(report_id) != str(resolved["report_id"]):
        raise util.KanpilotError(
            "conflict", "report_id 与当前读取的报告不符"
        )

    report = resolved["report"]
    if decision == "complete":
        cmd_read.verify_goal_completion(report, reviews, referenced,
                                        report_state=resolved["state"])

    detail = {
        "task_id": task_id,
        "execution_run_id": execution_run_id,
        "monitor_run_id": monitor_run_id,
        "monitor_request_id": monitor_request_id,
        "plan_sha256": plan_sha256,
        "report_id": resolved.get("report_id") or report_id,
        "report_sha256": report_sha256,
        "decision": decision,
        "reason": reason,
        "criterion_reviews": reviews,
        "evidence_refs": sorted(referenced),
        "report_state": resolved["state"],
        "integrity_kind": pi_desktop_backend.report_integrity_kind(report),
        "execution_status": resolved["execution_status"],
        "verdict": resolved["verdict"],
        "report_turn_id": resolved["turn_id"],
        "report_session_id": resolved["session_id"],
        "report_proposal_id": resolved["proposal_id"],
        "critical_bottom_line": decision == "complete",
    }
    with db.write_txn(conn):
        require_monitor_claim(conn, monitor_request_id, monitor_run_id, now)
        signature = goal_evaluation_signature(payload)
        existing = find_goal_evaluation(conn, signature)
        if existing is not None:
            return _evaluation_response(existing, deduplicated=True)
        db.add_event(
            conn, ctx.now_iso, GOAL_EVALUATION_EVENT, task_id=task_id,
            request_id=monitor_request_id, actor=MONITOR_EXECUTOR_ID,
            detail=detail,
        )
        row = conn.execute(
            "SELECT id FROM events WHERE kind=? AND task_id=? "
            "ORDER BY id DESC LIMIT 1",
            (GOAL_EVALUATION_EVENT, task_id),
        ).fetchone()
        event = {"event_id": row["id"], "at": ctx.now_iso, "detail": detail}
    return _evaluation_response(event, deduplicated=False)
