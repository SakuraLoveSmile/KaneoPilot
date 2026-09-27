"""Remote-writing commands behind the lifecycle serial bit:
create-review-fix / advance-task / recheck (protocol.md §5.8/§5.9/§5.11)."""

from __future__ import annotations

import json
from pathlib import Path

from . import (
    bridge, cmd_exec, cmd_read, constants, core, db, derive,
    dsh_events, headless_adapter as _headless, planfile, remote,
    round_store, util,
)


def _lifecycle_op(conn, label: str) -> str:
    owner = f"{label}:{util.new_uuid()}"
    core.acquire_lifecycle(conn, owner)
    return owner


def cmd_create_review_fix(ctx, payload: dict) -> dict:
    conn = ctx.conn
    dedupe_key = payload["dedupe_key"]
    util.require(
        isinstance(dedupe_key, str) and dedupe_key.strip()
        and "-->" not in dedupe_key and "<!--" not in dedupe_key,
        "dedupe_key must be a non-empty marker-safe string",
        "bad_request",
    )
    dedupe_key = dedupe_key.strip()
    util.require(
        payload["priority"] in bridge.VALID_PRIORITIES,
        "invalid priority",
        "bad_request",
    )
    # Local dedupe: an existing fix task for this key wins without any call.
    existing = conn.execute(
        "SELECT * FROM tasks WHERE plan_kind='review_fix' AND review_key=?",
        (dedupe_key,),
    ).fetchone()
    if existing is not None:
        # v2 §5.20.1：purged 行不算命中——静默返回已移除任务的 id 会误导
        # 调用方；报 conflict 由人工确认（不复活）。
        if db.task_save_state(existing) == "purged":
            raise util.KanpilotError(
                "conflict",
                "已有同名修复任务但已被永久从列表移除，请更换 dedupe_key",
            )
        return {
            "ok": True, "fix_task_id": existing["task_id"], "created": False,
            "source_sha256": existing["source_sha256"],
        }

    # Phase 1 (txn): local checks + acquire lifecycle serial atomically.
    with db.write_txn(conn):
        source_task = db.require_task(conn, payload["source_task_id"])
        owner = _lifecycle_op(conn, "create-review-fix")
    try:
        client = remote.get_client()
        result = remote.create_todo(
            client,
            source_task["project_id"],
            dedupe_key,
            {
                "title": payload["title"],
                "description": payload["description"],
                "priority": payload["priority"],
            },
        )
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    finally:
        with db.write_txn(conn):
            core.release_lifecycle(conn, owner)

    fix_task_id = str(result["task"]["id"])
    # fix task 的派发同样需要 source 快照：把 create_todo 返回的
    # task_context 以 canonical JSON 落盘（与 source_sha256 对应）。
    source = bridge.stable(
        {
            "task": result.get("task"),
            "comments": result.get("comments"),
            "relations": result.get("relations"),
            "activity": result.get("activity"),
        }
    )
    sources_dir = constants.sources_dir()
    sources_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = sources_dir / f"{fix_task_id}.json"
    snapshot_path.write_text(
        json.dumps(
            source, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ),
        encoding="utf-8",
    )
    now = ctx.now_iso
    with db.write_txn(conn):
        task = db.get_task(conn, fix_task_id)
        if task is None:
            conn.execute(
                "INSERT INTO tasks(task_id, project_id, project_name, title, "
                "repository, stage, status, kaneo_status, kaneo_checked_at, "
                "requirements_sha256, source_sha256, source_path, plan_kind, "
                "review_source_task_id, review_key, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fix_task_id, source_task["project_id"],
                    source_task["project_name"], payload["title"],
                    source_task["repository"], "plan", "needs_plan",
                    result.get("todo_slug"), now,
                    result.get("requirements_sha256"),
                    result.get("source_sha256"), str(snapshot_path), "review_fix",
                    source_task["task_id"], dedupe_key, now, now,
                ),
            )
            db.add_event(
                conn, now, "task_created", task_id=fix_task_id,
                actor="kaneo-review", detail={"via": "create-review-fix"},
            )
        # Record the link on the reviewed task.
        reviewed = db.require_task(conn, source_task["task_id"])
        db.update_task(
            conn, reviewed["task_id"], now, fix_task_id=fix_task_id,
            review_key=dedupe_key,
        )
        # v2 §5.22.2：不再自动创建 plan 请求——修复任务停在 needs_plan，
        # 规划材料确认后经 start-plans 提交（review_* 字段已在任务行上）。
        db.add_event(
            conn, now, "fix_task_created", task_id=source_task["task_id"],
            actor="kaneo-review",
            detail={"fix_task_id": fix_task_id, "dedupe_key": dedupe_key,
                    "created": bool(result.get("created"))},
        )
        return {
            "ok": True, "fix_task_id": fix_task_id,
            "created": bool(result.get("created")),
            "source_sha256": result.get("source_sha256"),
        }


def _require_goal_gate(ctx, payload: dict, task) -> None:
    """T5-C 硬闸（§5.23.10）：pi_desktop 轮次的 in-progress → in-review。

    ``pi`` 后端维持 §5.18.4 旧口径（不受影响）；只有该任务**当前轮次**
    backend=pi_desktop 时要求携带当前 monitor claim 与 evaluation event ID，
    并在**远端更新前再次**只读复核：

    - 当前任务的执行轮次；
    - 报告身份/hash + 确定性报告底线；
    - 事件确由有效 monitor 请求登记；
    - ``decision=complete``；
    - 执行终态 ``completed + finished/0``。

    绕过 monitor 直接调用、旧判定或报告变化 → 一律 ``conflict``，不触碰远端。
    本函数只读（不发远端请求、不写状态、不占写事务）。
    """
    run_id = task["run_id"]
    if not run_id:
        # 历史轮次/测试夹具可能没有轮次索引行：无索引即无 pi_desktop 报告
        # 场景，维持 §5.18.4 旧接口（pi 后端不受影响）。
        return
    row = round_store.get_round(ctx.conn, task["task_id"], run_id)
    if row is None:
        return
    if row["backend"] != "pi_desktop":
        # pi（及历史只读后端）维持 §5.18.4 原接口与三证据口径。
        return
    monitor_request_id = payload.get("monitor_request_id")
    monitor_run_id = payload.get("monitor_run_id")
    evaluation_event_id = payload.get("evaluation_event_id")
    if not monitor_request_id or not monitor_run_id \
            or not evaluation_event_id:
        raise util.KanpilotError(
            "conflict",
            "pi_desktop 轮次进入评审必须携带当前 monitor claim"
            "（monitor_request_id/monitor_run_id）与 evaluation_event_id；"
            "绕过 monitor 直接推进被拒绝",
        )
    if isinstance(evaluation_event_id, bool) or not isinstance(
        evaluation_event_id, int
    ) or evaluation_event_id <= 0:
        raise util.bad_request("evaluation_event_id 必须为正整数")
    # 事件确由有效 monitor 请求登记（claim + 租约 + 事件归属）。
    cmd_exec.require_monitor_claim(
        ctx.conn, monitor_request_id, monitor_run_id, ctx.now
    )
    event = cmd_exec.goal_evaluation_event(ctx.conn, evaluation_event_id)
    if event is None:
        raise util.KanpilotError(
            "conflict", "没有找到该 evaluation event（旧判定或未登记）"
        )
    detail = event.get("detail") or {}
    if event.get("task_id") != task["task_id"]:
        raise util.KanpilotError(
            "conflict", "evaluation event 不属于该任务"
        )
    if detail.get("execution_run_id") != run_id:
        raise util.KanpilotError(
            "conflict", "evaluation event 的 execution_run_id 不是当前轮次"
        )
    if detail.get("monitor_request_id") != monitor_request_id \
            or detail.get("monitor_run_id") != monitor_run_id:
        raise util.KanpilotError(
            "conflict",
            "evaluation event 不是由本次 monitor claim 登记的（旧判定）",
        )
    if detail.get("decision") != "complete":
        raise util.KanpilotError(
            "conflict",
            "evaluation event 的 decision 不是 complete，不能进入评审",
        )

    # 执行终态 completed + finished/0（§5.23.10 T5-C）。
    job, _error = dsh_events.read_job(
        run_store_run_dir(ctx.conn, task["task_id"], run_id),
        task["task_id"], run_id, backend="pi_desktop",
    )
    events, _detail = dsh_events.iter_events(
        run_store_run_dir(ctx.conn, task["task_id"], run_id)
        / dsh_events.EVENT_LOG_NAME
    )
    terminal = dsh_events.terminal_event(events)
    if (
        not isinstance(job, dict)
        or job.get("status") != "finished"
        or job.get("exit_code") != 0
        or not isinstance(terminal, dict)
        or terminal.get("outcome") != "completed"
    ):
        raise util.KanpilotError(
            "conflict",
            "执行终态不是 completed + finished/0，不能进入评审"
            f"（job.status={None if not isinstance(job, dict) else job.get('status')!r}，"
            f"terminal={terminal}）",
        )

    # 只读重新取回同一报告：报告变化/身份不符 → conflict，不触碰远端。
    resolved = cmd_read.read_goal_report(ctx.conn, task["task_id"], run_id)
    if resolved["state"] != "ready":
        raise util.KanpilotError(
            "conflict",
            f"报告当前不可用（state={resolved['state']}）："
            f"{resolved.get('detail') or '无正文'}",
        )
    if resolved["report_sha256"] != detail.get("report_sha256"):
        raise util.KanpilotError(
            "conflict",
            "report_sha256 与当前读取的报告不符（报告已变化）",
        )
    if detail.get("plan_sha256") != task["plan_sha256"]:
        raise util.KanpilotError(
            "conflict", "evaluation event 的 plan_sha256 与任务当前计划不符"
        )
    reviews = detail.get("criterion_reviews") or []
    referenced = set(detail.get("evidence_refs") or [])
    cmd_read.verify_goal_completion(resolved["report"], reviews, referenced)


def run_store_run_dir(conn, task_id: str, run_id: str):
    """本轮运行记录目录（只读解析；不可用 → 确定性 conflict）。"""
    source = _headless.resolve_source(conn)
    try:
        return source.run_dir(task_id, run_id)
    except _headless.HeadlessError as exc:
        raise util.KanpilotError(
            "conflict", f"运行记录目录不可用：{exc.detail}"
        )


def cmd_advance_task(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    target, expected = payload["target"], payload["expected_current"]
    if target not in ("in-review", "done"):
        raise util.KanpilotError("bad_request", f"invalid target {target}")
    if expected not in ("in-progress", "in-review"):
        raise util.KanpilotError(
            "bad_request", f"invalid expected_current {expected}"
        )
    if (target, expected) not in (
        ("in-review", "in-progress"),
        ("done", "in-progress"),
        ("done", "in-review"),
    ):
        raise util.KanpilotError(
            "bad_request", f"cannot advance {expected} -> {target}"
        )

    actor = payload.get("run_id") or "executor"
    # Phase 1 (txn): local checks + acquire lifecycle serial.
    with db.write_txn(conn):
        task = db.require_task(conn, payload["task_id"])
        if target == "in-review" and expected == "in-progress":
            # T5-C（§5.23.10）：pi_desktop 轮次进入评审前的硬闸——必须在
            # **远端更新前**复核 monitor claim、判定事件、报告底线与执行终态。
            # 硬闸只读且绝不触碰远端；失败一律 conflict（不写本地、不改远端）。
            _require_goal_gate(ctx, payload, task)
        if payload.get("run_id"):
            claimed = conn.execute(
                "SELECT * FROM requests WHERE task_id=? AND status='claimed' "
                "AND run_id=?",
                (task["task_id"], payload["run_id"]),
            ).fetchone()
            if claimed is None:
                raise util.KanpilotError(
                    "stale_run", "run_id does not match a claimed request"
                )
        if target == "done" and expected == "in-progress":
            # Review 前置守卫（§5.9 v1.2）：执行完成不得跳过 Review
            # 直接完成——须先在当前周期登记 review_conclusion='pass'。
            conclusion = db.task_extra(task).get("review_conclusion")
            if conclusion != "pass":
                raise util.KanpilotError(
                    "conflict",
                    f"task {task['task_id']} needs a registered pass review "
                    "before advancing in-progress -> done "
                    f"(review_conclusion={conclusion})",
                )
        owner = _lifecycle_op(conn, "advance-task")

    wrote_remote = False
    try:
        # Phase 2 (no txn): remote verify -> update -> re-verify.
        try:
            client = remote.get_client()
            live = remote.get_task(client, task["task_id"])
        except bridge.BridgeError as exc:
            raise util.KanpilotError("remote_unavailable", str(exc)) from exc
        live_task = live.get("task") or {}
        conflict = (
            live_task.get("projectId") != task["project_id"]
            or live_task.get("status") != expected
            or (
                task["requirements_sha256"]
                and live.get("requirements_sha256") != task["requirements_sha256"]
            )
        )
        if conflict:
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                db.update_task(
                    conn, task["task_id"], now,
                    kaneo_status=live_task.get("status"),
                    kaneo_checked_at=now,
                )
                core.block_task(
                    conn, task, now,
                    f"remote conflict: expected {expected}, observed "
                    f"{live_task.get('status')}",
                    "remote_conflict", actor,
                )
            raise util.KanpilotError(
                "remote_conflict",
                f"task {task['task_id']} remote status "
                f"{live_task.get('status')} != {expected}",
            )
        verify_error = None
        verified = False
        try:
            remote.update_status(
                client, task["task_id"], task["project_id"],
                target, expected,
            )
            wrote_remote = True
            after = remote.get_task(client, task["task_id"])
            after_task = after.get("task") or {}
            verified = (
                after_task.get("status") == target
                and after_task.get("projectId") == task["project_id"]
                and (
                    not task["requirements_sha256"]
                    or after.get("requirements_sha256")
                    == task["requirements_sha256"]
                )
            )
        except bridge.BridgeError as exc:
            verify_error = exc
        if not verified:
            # Deterministic post-write mismatch -> remote_conflict; transport
            # failure after a write attempt -> uncertain -> remote_unavailable.
            if verify_error is None:
                code, message = "remote_conflict", (
                    f"remote transition unverifiable: status "
                    f"{after_task.get('status')} != {target}"
                )
            else:
                code, message = "remote_unavailable", str(verify_error)
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                if wrote_remote:
                    core.block_task(conn, task, now, message, code, actor)
            raise util.KanpilotError(code, message)
    finally:
        with db.write_txn(conn):
            core.release_lifecycle(conn, owner)

    # Phase 3 (txn): local transition + follow-up request.
    with db.write_txn(conn):
        task = db.require_task(conn, task["task_id"])
        new_status = "in_review" if target == "in-review" else "done"
        extra = {"kaneo_status": target, "kaneo_checked_at": now}
        if new_status == "in_review":
            # New review cycle: clear any earlier registered conclusion —
            # report columns and the extra_json conclusion keys alike;
            # fix_task_id/review_key 历史关联保留（§5.9）。
            extra["review_report_path"] = None
            extra["review_report_sha256"] = None
            extra_json = db.task_extra(task)
            for key in ("review_conclusion", "review_registered_at"):
                extra_json.pop(key, None)
            extra["extra_json"] = extra_json
        core.transition_task(
            conn, task, new_status, now, actor,
            detail={"via": "advance-task", "target": target,
                    "evidence_note": payload.get("evidence_note")},
            **extra,
        )
        db.add_event(
            conn, now, "kaneo_transition", task_id=task["task_id"],
            actor=actor, detail={"from": expected, "to": target},
        )
        if new_status == "in_review":
            db.create_request(
                conn, now, "review", f"review:{task['task_id']}",
                task_id=task["task_id"], requested_by="scheduler",
            )
        return {
            "ok": True, "kaneo_status": target, "task_status": new_status,
        }


def cmd_recheck(ctx, payload: dict) -> dict:
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    offline = bool(payload.get("offline"))
    checks = []

    if task["plan_path"]:
        try:
            path = Path(task["plan_path"]).expanduser()
            ok = path.is_file() and not path.is_symlink()
            sha_ok = ok and util.sha256_file(path) == task["plan_sha256"]
            checks.append({
                "name": "plan_sha256", "ok": bool(sha_ok),
                "detail": "match" if sha_ok else "missing/changed",
            })
        except OSError as exc:
            checks.append(
                {"name": "plan_sha256", "ok": False, "detail": str(exc)}
            )
    if task["source_path"]:
        try:
            _s, source_sha, req_sha = planfile.check_source_snapshot(
                task["source_path"], task["task_id"], task["project_id"]
            )
            checks.append({
                "name": "source_sha256", "ok": source_sha == task["source_sha256"],
                "detail": "match" if source_sha == task["source_sha256"]
                else "changed",
            })
            checks.append({
                "name": "requirements_sha256",
                "ok": req_sha == task["requirements_sha256"],
                "detail": "match" if req_sha == task["requirements_sha256"]
                else "changed",
            })
        except util.KanpilotError as exc:
            checks.append(
                {"name": "source_snapshot", "ok": False, "detail": exc.message}
            )

    observed_status, observed_req = None, None
    if not offline:
        try:
            live = remote.get_task(remote.get_client(), task["task_id"])
            live_task = live.get("task") or {}
            observed_status = live_task.get("status")
            observed_req = live.get("requirements_sha256")
            checks.append({
                "name": "remote_status", "ok": True,
                "detail": str(observed_status),
            })
            checks.append({
                "name": "remote_requirements",
                "ok": observed_req == task["requirements_sha256"],
                "detail": "match" if observed_req == task["requirements_sha256"]
                else "changed",
            })
        except Exception as exc:
            checks.append(
                {"name": "remote", "ok": False, "detail": str(exc)}
            )

    now = ctx.now_iso
    with db.write_txn(conn):
        updates = {"kaneo_checked_at": now}
        if observed_status is not None:
            updates["kaneo_status"] = observed_status
        db.update_task(conn, task["task_id"], now, **updates)
        db.add_event(
            conn, now, "rechecked", task_id=task["task_id"], actor="user",
            detail={"offline": offline,
                    "ok": all(item["ok"] for item in checks)},
        )
    return {
        "task_id": task["task_id"], "checks": checks,
        "kaneo_status": observed_status,
        "requirements_sha256": observed_req,
        "ok": all(item["ok"] for item in checks),
    }
