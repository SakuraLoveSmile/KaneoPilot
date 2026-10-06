"""Dispatch sub-steps (protocol.md §5.10, §4.6): dispatch-prepare /
begin-send / confirm / fail. Mirrors legacy kaneo_dispatch.py semantics."""

from __future__ import annotations

from pathlib import Path

from . import bridge, constants, core, db, remote, util, git_delivery


def _entry(ctx, payload, allowed):
    """Load task, verify run ownership, claimed dispatch request, status."""
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    core.require_task_run(task, payload["run_id"])
    request = core.claimed_dispatch_request(
        conn, task["task_id"], payload["run_id"]
    )
    if task["status"] not in allowed:
        raise util.KanpilotError(
            "conflict",
            f"task {task['task_id']} is {task['status']}, "
            f"expected {'|'.join(allowed)}",
        )
    return task, request


def cmd_dispatch_prepare(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    with db.write_txn(conn):
        task, _request = _entry(ctx, payload, ("dispatching", "prepared"))
        evidence = payload["evidence"]
        if isinstance(evidence, dict) and \
                evidence.get("backend") in constants.EXEC_BACKENDS:
            # v1.6+（§5.18.3/§5.23.3）：执行后端证据分支
            # （marker/repository/backend/config 快照全等校验）。
            _validate_pi_prepare(conn, task, payload["run_id"], evidence)
        else:
            plan_basename = Path(task["plan_path"]).name
            core.validate_evidence(evidence, task, expect_message=False,
                                   plan_basename=plan_basename)
        db.update_task(
            conn, task["task_id"], now,
            status="prepared", ui_evidence=evidence,
        )
        db.add_event(
            conn, now, "dispatch_prepared", task_id=task["task_id"],
            request_id=_request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": payload["run_id"]},
        )
        return {"status": "prepared"}


def cmd_dispatch_begin_send(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    task, _request = _entry(ctx, payload, ("prepared",))
    if core.is_pr_delivery(task) or _manual_branch_delivery(task):
        # Recheck outside the transaction before any remote status mutation.
        _ensure_worktree(task, task["task_id"], payload["run_id"], conn)
    with db.write_txn(conn):
        task, _request = _entry(ctx, payload, ("prepared",))
        owner = f"begin-send:{payload['run_id']}"
        core.acquire_lifecycle(conn, owner)

    try:
        # Remote pre-check (no write txn held).
        try:
            live = remote.get_task(remote.get_client(), task["task_id"])
        except bridge.BridgeError as exc:
            raise util.KanpilotError("remote_unavailable", str(exc)) from exc
        live_task = live.get("task") or {}
        pre_ok = (
            live_task.get("projectId") == task["project_id"]
            and live_task.get("status") == task["original_todo_slug"]
            and (
                not task["requirements_sha256"]
                or live.get("requirements_sha256")
                == task["requirements_sha256"]
            )
        )
        if not pre_ok:
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                core.block_task(
                    conn, task, now,
                    f"remote changed before transition: status "
                    f"{live_task.get('status')} != {task['original_todo_slug']}",
                    "remote_conflict", "kaneo-dispatch",
                    send_state="unknown", lock_retained=1,
                    manual_recovery_required=1,
                )
            raise util.KanpilotError(
                "remote_conflict", "remote changed before transition"
            )
        with db.write_txn(conn):
            task = db.require_task(conn, task["task_id"])
            core.require_task_run(task, payload["run_id"])
            if task["status"] != "prepared":
                raise util.KanpilotError(
                    "conflict", "state changed during begin-send"
                )
            db.update_task(
                conn, task["task_id"], now,
                status="submitting", transition_started_at=now,
            )
        # Remote write + re-verify.
        try:
            remote.update_status(
                remote.get_client(), task["task_id"], task["project_id"],
                constants.IN_PROGRESS_SLUG, task["original_todo_slug"],
            )
            after = remote.get_task(remote.get_client(), task["task_id"])
            after_task = after.get("task") or {}
            verified = (
                after_task.get("status") == constants.IN_PROGRESS_SLUG
                and after_task.get("projectId") == task["project_id"]
                and (
                    not task["requirements_sha256"]
                    or after.get("requirements_sha256")
                    == task["requirements_sha256"]
                )
            )
            if not verified:
                raise bridge.BridgeError(
                    f"remote transition unverifiable: "
                    f"{after_task.get('status')}"
                )
        except bridge.BridgeError as exc:
            # The remote write may or may not have landed -> unknown.
            with db.write_txn(conn):
                task = db.require_task(conn, task["task_id"])
                core.block_task(
                    conn, task, now, str(exc), "remote_conflict",
                    "kaneo-dispatch", send_state="unknown",
                    lock_retained=1, manual_recovery_required=1,
                )
            raise util.KanpilotError(
                "remote_conflict",
                f"transition verification failed: {exc}",
            ) from exc
        with db.write_txn(conn):
            db.update_task(
                conn, task["task_id"], now,
                kaneo_status=constants.IN_PROGRESS_SLUG,
                kaneo_checked_at=now, transition_verified_at=now,
            )
            db.add_event(
                conn, now, "kaneo_transition", task_id=task["task_id"],
                actor="kaneo-dispatch",
                detail={"from": task["original_todo_slug"],
                        "to": constants.IN_PROGRESS_SLUG},
            )
        return {
            "status": "submitting",
            "action": "click visible Send once; verify message and attachment",
        }
    finally:
        with db.write_txn(conn):
            core.release_lifecycle(conn, owner)


def cmd_dispatch_confirm(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    # Idempotent: a confirmed send returns running again.
    task = db.require_task(conn, payload["task_id"])
    if (
        task["run_id"] == payload["run_id"]
        and task["status"] == "running"
        and task["sent"] == 1
    ):
        return {"status": "running", "already_confirmed": True}

    with db.write_txn(conn):
        task, request = _entry(ctx, payload, ("submitting", "running"))
        util.require(
            task["transition_verified_at"],
            "transition not verified",
            "conflict",
        )
        evidence = payload["evidence"]
        if isinstance(evidence, dict) and \
                evidence.get("backend") in constants.EXEC_BACKENDS:
            # v1.6+（§5.18.3/§5.23.3）：执行后端证据分支
            # （意图 + session 事件 + config；pi_desktop 另核 turn_id）。
            backend = evidence["backend"]
            round_row = _require_pi_round(
                conn, task["task_id"], payload["run_id"], backend
            )
            session = _validate_pi_confirm(
                conn, task, payload["run_id"], evidence, round_row
            )
            # session_id/session_title 以协议 session 事件为准（不采信证据
            # 单方字段之外的来源）。
            evidence = dict(evidence)
            evidence.setdefault("session_id", session.get("session_id"))
            evidence.setdefault("session_title", session.get("session_title"))
            # 外层已在 write_txn 内，直接回填轮次会话关联（§5.16.1）。
            round_store.upsert_discovered(
                conn, task["task_id"], payload["run_id"], now,
                backend=backend,
                session_id=evidence.get("session_id"),
                session_title=evidence.get("session_title"),
            )
        else:
            plan_basename = Path(task["plan_path"]).name
            core.validate_evidence(
                evidence, task, expect_message=True,
                plan_basename=plan_basename,
            )
        db.update_task(
            conn, task["task_id"], now,
            status="running", stage="monitor", sent=1, send_state="sent",
            sent_at=now, send_evidence=evidence,
            session_id=evidence.get("session_id"),
            session_title=evidence.get("session_title"),
            dispatch_lease_expires_at=None, lock_retained=0,
        )
        core.finish_request(
            conn, request, now, "succeeded", "succeeded",
            result={"outcome": "succeeded", "send_state": "sent"},
            send_state="sent", evidence=evidence,
        )
        interval = db.get_setting(
            conn, "monitor_interval_seconds",
            constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
        )
        db.create_request(
            conn, now, "monitor", f"monitor:{task['task_id']}",
            task_id=task["task_id"], requested_by="scheduler",
            not_before=util.plus_seconds(interval, ctx.now),
        )
        core.release_dispatch_serial(conn, payload["run_id"])
        db.add_event(
            conn, now, "dispatch_sent", task_id=task["task_id"],
            request_id=request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": payload["run_id"],
                    "session_id": evidence.get("session_id")},
        )
        db.add_event(
            conn, now, "status_change", task_id=task["task_id"],
            actor="kaneo-dispatch", detail={"from": "submitting", "to": "running"},
        )
        return {"status": "running"}


def cmd_dispatch_fail(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    send_state = payload["send_state"]
    if send_state not in ("not_sent", "unknown"):
        raise util.KanpilotError(
            "bad_request", "send_state must be not_sent|unknown"
        )
    # Idempotent: already recorded for this run.
    task = db.require_task(conn, payload["task_id"])
    if (
        task["run_id"] == payload["run_id"]
        and task["status"] == "needs_user"
    ):
        return {
            "status": "needs_user", "send_state": task["send_state"],
            "already_recorded": True,
        }

    with db.write_txn(conn):
        task, request = _entry(
            ctx, payload, ("dispatching", "prepared", "submitting")
        )
        failure_stage = task["status"]
        db.update_task(
            conn, task["task_id"], now,
            status="needs_user", send_state=send_state,
            sent=0 if send_state == "not_sent" else None,
            failure_reason=payload["reason"],
            failure_code="dispatch_failed",
            failure_stage=failure_stage,
            blocked_at=now, blocked_summary=payload["reason"],
            lock_retained=1 if send_state != "not_sent" else 0,
            manual_recovery_required=1 if send_state == "unknown" else 0,
        )
        core.finish_request(
            conn, request, now, "failed", "failed",
            result={"outcome": "failed", "reason": payload["reason"],
                    "send_state": send_state},
            note=payload["reason"], send_state=send_state,
        )
        core.release_dispatch_serial(conn, payload["run_id"])
        db.add_event(
            conn, now, "dispatch_failed", task_id=task["task_id"],
            request_id=request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": payload["run_id"], "send_state": send_state,
                    "reason": payload["reason"],
                    "failure_stage": failure_stage},
        )
        db.add_event(
            conn, now, "status_change", task_id=task["task_id"],
            actor="kaneo-dispatch",
            detail={"from": failure_stage, "to": "needs_user"},
        )
        return {"status": "needs_user", "send_state": send_state}


# ------------------------------------------------- v1.6 pi 分支（protocol §5.18.3）
#
# dispatch-prepare/dispatch-confirm 按证据是否含 backend ∈ EXEC_BACKENDS 分支：
# pi/pi_desktop 证据走 §5.18.3/§5.23.3 校验（marker/repository/backend/
# config 快照/协议会话事件），缺省证据仍走 core.validate_evidence §4.6
# 旧分支（历史轮次与既有测试夹具的兼容形态）。

import os
import subprocess
import sys
import time as _time

from . import (
    dsh_events, exec_config, headless_adapter as _headless,
    pi_desktop_backend, round_store,
)


def _require_pi_round(conn, task_id, run_id, backend="pi"):
    row = round_store.get_round(conn, task_id, run_id)
    if row is None or row["backend"] != backend:
        raise util.KanpilotError(
            "conflict",
            f"该执行轮次不是 {backend} 后端，不能使用 {backend} 派发证据",
        )
    return row


def _config_or_fail(evidence, round_row):
    config = round_store.round_view(round_row).get("config")
    if evidence.get("config") != config:
        raise util.KanpilotError(
            "evidence_invalid",
            "config 快照与该轮固化值不符（设置变更只影响新轮次）",
        )


def _validate_pi_prepare(conn, task, run_id, evidence):
    if not isinstance(evidence, dict):
        raise util.KanpilotError("evidence_invalid", "evidence must be an object")

    def check(condition, message):
        if not condition:
            raise util.KanpilotError("evidence_invalid", message)

    backend = evidence.get("backend")
    check(backend in constants.EXEC_BACKENDS, "backend not verified")
    round_row = _require_pi_round(conn, task["task_id"], run_id, backend)
    check(evidence.get("session_marker") == task["session_marker"],
          "session_marker not verified")
    check(evidence.get("repository") == task["repository"],
          "repository not verified")
    _config_or_fail(evidence, round_row)
    util.require(
        util.sha256_file(Path(task["plan_path"])) == task["plan_sha256"],
        "plan changed since dispatch validation", "evidence_invalid",
    )


def _validate_pi_confirm(conn, task, run_id, evidence, round_row):
    if not isinstance(evidence, dict):
        raise util.KanpilotError("evidence_invalid", "evidence must be an object")

    def check(condition, message):
        if not condition:
            raise util.KanpilotError("evidence_invalid", message)
    backend = evidence.get("backend")
    check(backend in constants.EXEC_BACKENDS, "backend not verified")
    check(backend == round_row["backend"], "backend 与轮次固化不符")
    check(evidence.get("session_marker") == task["session_marker"],
          "session_marker not verified")
    check(evidence.get("repository") == task["repository"],
          "repository not verified")
    check(evidence.get("protocol_seen") is True,
          "protocol session event not verified")
    _config_or_fail(evidence, round_row)
    check(bool(round_row["started_at"]), "启动意图未落盘（先 dispatch-start）")
    # events.jsonl 首个 session 事件存在且 session_id 匹配（§5.18.3）。
    source = _headless.resolve_source(conn)
    try:
        run_dir = source.run_dir(task["task_id"], run_id)
    except _headless.HeadlessError as exc:
        raise util.KanpilotError("evidence_invalid", exc.detail)
    events, _detail = dsh_events.iter_events(run_dir / dsh_events.EVENT_LOG_NAME)
    session = dsh_events.session_event(events)
    if session is None:
        raise util.KanpilotError(
            "evidence_invalid", "events.jsonl 尚无 session 初始化事件"
        )
    check(session.get("session_id") == evidence.get("session_id"),
          "session_id 与协议会话事件不符")
    if backend == "pi_desktop":
        # §5.23.3：dispatch-confirm 另核 turn_id 与协议会话事件一致。
        check(session.get("turn_id") == evidence.get("turn_id"),
              "turn_id 与协议会话事件不符")
    util.require(
        util.sha256_file(Path(task["plan_path"])) == task["plan_sha256"],
        "plan changed since dispatch validation", "evidence_invalid",
    )
    return session


def _pi_session_view(conn, task_id, run_id, backend="pi"):
    """读取 job/session 事件的 best-effort 会话视图（confirm 幂等路径用）。"""
    source = _headless.resolve_source(conn)
    try:
        run_dir = source.run_dir(task_id, run_id)
    except _headless.HeadlessError:
        return None, None
    job, _err = dsh_events.read_job(run_dir, task_id, run_id, backend=backend)
    events, _detail = dsh_events.iter_events(
        run_dir / dsh_events.EVENT_LOG_NAME
    )
    session = dsh_events.session_event(events) or {}
    return (
        session.get("session_id") or (job or {}).get("session_id"),
        session.get("session_title") or (job or {}).get("session_title"),
    )


def _register_started_worker(ctx, spec, proc, spec_path):
    """Close the detached bootstrap gap using only this newly created child."""
    if not spec.get("executor_instance_id"):
        return
    from . import process_owner
    try:
        registered = process_owner.register_spawned_worker(ctx, spec, proc.pid, spec_path=spec_path)
        if registered is None and proc.poll() is None:
            raise util.KanpilotError("validation_failed", "无法核验新 worker 进程启动时间")
    except util.KanpilotError:
        # The Popen object still owns this child; poll guards natural exit/reap.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        raise


def cmd_dispatch_start(ctx, payload: dict) -> dict:
    """dispatch-start（protocol §5.18.3）：可用性复核 → 意图落盘 → worktree →
    拉起 worker → 记 pid → 等首个 session 事件。同一轮次只启动一次进程。"""
    conn, now = ctx.conn, ctx.now_iso
    task, _request = _entry(ctx, payload, ("submitting",))
    task_id, run_id = task["task_id"], payload["run_id"]
    round_row = round_store.require_round(conn, task_id, run_id)
    backend = round_row["backend"]
    if backend == "pi_desktop":
        return _dispatch_start_pi_desktop(
            ctx, task, _request, run_id, round_row
        )
    if backend != "pi":
        raise util.KanpilotError(
            "conflict", "该执行轮次不是 pi/pi_desktop 后端，不能 dispatch-start"
        )

    # 1. 可用性复核先行（失败不落意图、不启动进程）。
    available, reason = exec_config.check_available(conn)
    if not available:
        raise util.KanpilotError("validation_failed", reason or "pi 执行环境不可用")

    # 2. 意图先落盘（单事务；非空 → 幂等返回，绝不创建第二个进程）。
    with db.write_txn(conn):
        fresh = round_store.require_round(conn, task_id, run_id)
        if fresh["started_at"]:
            session_id, _title = _pi_session_view(conn, task_id, run_id)
            return {
                "status": "started", "pid": fresh["pid"],
                "session_id": session_id,
                "worktree_path": fresh["worktree_path"],
                "deduplicated": True,
            }
        round_store.mark_intent(conn, task_id, run_id, now)
        config_snapshot = dict(
            round_store.round_view(round_store.require_round(conn, task_id, run_id))
            ["config"] or {}
        )

    # 3. worktree + spec + 拉起 worker（事务外进程操作）。
    worktree_path, worktree_mode = _ensure_worktree(task, task_id, run_id, conn)
    source = _headless.resolve_source(conn)
    try:
        run_dir = source.run_dir(task_id, run_id)
    except _headless.HeadlessError as exc:
        raise util.KanpilotError("validation_failed", exc.detail)
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "v": 1,
        "task_id": task_id,
        "run_id": run_id,
        "session_marker": task["session_marker"],
        "config": config_snapshot,
        "cwd": str(worktree_path),
        "worktree_mode": worktree_mode,
        "repository": task["repository"],
        "plan_path": task["plan_path"],
        "instruction": _execution_instruction(task, worktree_path, desktop=False),
        "runs_dir": str(run_dir),
        "pi_home": str(constants.pi_home()),
        "agent_md": _agent_md_path(conn, config_snapshot),
        "timeout_seconds": round_store.round_timeout_seconds(conn),
    }
    spec_path = run_dir / "spec.json"
    from . import process_owner
    process_owner.bind_spec(spec, ctx.db_path)
    spec_path.write_text(
        util.jdump(spec), encoding="utf-8"
    )
    log_path = run_dir / "worker.log"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(constants.core_root())
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "kanpilot.pi_worker",
                    "--spec", str(spec_path),
                ],
                cwd=str(constants.core_root()),
                stdout=log, stderr=log,
                start_new_session=True, env=env,
            )
    except OSError as exc:
        raise util.KanpilotError("internal", f"worker 拉起失败：{exc}")

    _register_started_worker(ctx, spec, proc, spec_path)

    # 4. 记 pid/worktree + event（意图已落盘，此处只是补记进程信息）。
    with db.write_txn(conn):
        round_store.mark_started(
            conn, task_id, run_id, now, pid=proc.pid,
            worktree_path=str(worktree_path),
        )
        db.add_event(
            conn, now, "round_started", task_id=task_id,
            request_id=_request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": run_id, "pid": proc.pid,
                    "worktree_path": str(worktree_path),
                    "worktree_mode": worktree_mode},
        )

    # 5. 等首个 session 事件（超时 → starting/session_id=null）。
    session_id = _wait_session_event(run_dir)
    return {
        "status": "started" if session_id else "starting",
        "pid": proc.pid,
        "session_id": session_id,
        "worktree_path": str(worktree_path),
        "deduplicated": False,
    }


def _agent_md_path(conn, config_snapshot) -> str:
    """exec_config.agent_preset → agents_dir/<name>.md 路径（不存在返回 ""，
    worker 将不带 --system-prompt 启动——可用性门已先验存在性）。"""
    preset = (config_snapshot or {}).get("agent_preset") or "default"
    path = constants.agents_dir() / f"{preset}.md"
    return str(path) if path.is_file() else ""


def _has_control_chars(value) -> bool:
    """名称是否含空白控制字符（T6：含则派发前确定性校验错误）。"""
    return any(ch.isspace() and ch not in (" ",) for ch in str(value or ""))


def _require_session_naming_capability(conn, config_snapshot) -> None:
    """T6 兼容门（§5.23.10）：`pi_session_create` schema 必须暴露 projectName。

    在**任何 session.create 之前**执行；旧 Plus → 明确「版本缺少工作区命名
    能力」，绝不静默派发出 UUID 名称。
    """
    try:
        record = pi_desktop_backend.load_connection(
            (config_snapshot or {}).get("connection_file")
        )
        client = pi_desktop_backend.MCPClient(
            record["url"], record["token"], timeout=10
        )
        client.connect()
        if not client.supports_session_naming():
            raise util.KanpilotError(
                "validation_failed",
                constants.PI_DESKTOP_NAMING_CAPABILITY_MISSING
                + "：PI-Desktop-Plus 的 pi_session_create 未暴露 "
                + "projectName，不能派发（不静默使用 UUID 名称，请升级 Plus）",
            )
    except util.KanpilotError as exc:
        if exc.code == "validation_failed" and \
                constants.PI_DESKTOP_NAMING_CAPABILITY_MISSING in exc.message:
            raise
        raise util.KanpilotError(
            "validation_failed",
            "无法确认 PI-Desktop-Plus 的工作区命名能力："
            + util.redact(exc.message),
        )

def _dispatch_start_pi_desktop(ctx, task, request, run_id, round_row) -> dict:
    """pi_desktop 分支 dispatch-start（§5.23.3）：连接探测 → 意图落盘 →
    worktree → 计划哈希复核 → 拉起 kanpilot.pi_desktop_worker。"""
    conn, now = ctx.conn, ctx.now_iso
    task_id = task["task_id"]

    # 1. 可用性复核（exec_config 按当前生效后端探测 pi_desktop 控制面：
    #    连接文件/token/tools/provider/model；失败不落意图、不启动进程）。
    config_snapshot = dict(round_store.round_view(round_row).get("config") or {})
    available, reason = exec_config.check_available(
        conn, config_snapshot=config_snapshot,
    )
    if not available:
        raise util.KanpilotError(
            "validation_failed",
            reason or "pi_desktop 执行环境不可用",
        )
    # T6 兼容门（§5.23.10）：**任何 session.create 之前**确认 Plus 暴露
    # `pi_session_create.inputSchema.properties.projectName`；旧 Plus 明确
    # 阻断（「版本缺少工作区命名能力」），绝不静默派发出 UUID 名称。
    _require_session_naming_capability(conn, config_snapshot)
    # T6 展示快照（§5.23.10）：新轮次固化 workspace/session 显示名；两者是
    # **展示快照**（旧 spec 缺字段仍按旧语义读取/续接，不重新生成 prompt），
    # 且**不得**用于去重、路径生成、权限判断或完成判定。
    workspace_display_name = task["project_name"]
    session_display_name = task["title"]
    if not workspace_display_name or not session_display_name:
        raise util.KanpilotError(
            "validation_failed",
            "任务登记的 project_name/title 缺失，无法固化派发展示名"
            "（不回退到 UUID 名称）",
        )
    if _has_control_chars(workspace_display_name) \
            or _has_control_chars(session_display_name):
        raise util.KanpilotError(
            "validation_failed",
            "任务登记的 project_name/title 含空白控制字符，拒绝派发",
        )

    # 2. 意图先落盘（同一轮次只允许一次真实启动；非空 → 幂等返回）。
    with db.write_txn(conn):
        fresh = round_store.require_round(conn, task_id, run_id)
        if fresh["started_at"]:
            if not fresh["pid"]:
                raise util.KanpilotError(
                    "conflict",
                    "该 pi_desktop 轮次已有启动意图但未完成拉起，需人工处理，不能重复启动",
                )
            session_id, _title = _pi_session_view(
                conn, task_id, run_id, backend="pi_desktop"
            )
            return {
                "status": "started" if session_id else "starting", "pid": fresh["pid"],
                "session_id": session_id,
                "worktree_path": fresh["worktree_path"],
                "deduplicated": True,
            }
        round_store.mark_intent(conn, task_id, run_id, now)

    run_dir = None
    try:
        # 3. worktree + 计划哈希复核 + spec + 拉起 worker（事务外进程操作）。
        source = _headless.resolve_source(conn)
        try:
            run_dir = source.run_dir(task_id, run_id)
        except _headless.HeadlessError as exc:
            raise util.KanpilotError("validation_failed", exc.detail)
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree_path, worktree_mode = _ensure_worktree(
            task, task_id, run_id, conn, require_isolated=True,
        )
        util.require(
            util.sha256_file(Path(task["plan_path"])) == task["plan_sha256"],
            "plan changed since dispatch validation", "validation_failed",
        )
        spec = {
            "v": 1,
            "task_id": task_id,
            "run_id": run_id,
            "session_marker": task["session_marker"],
            "backend": "pi_desktop",
            "config": config_snapshot,
            "cwd": str(worktree_path),
            "worktree_mode": worktree_mode,
            "repository": task["repository"],
            "plan_path": task["plan_path"],
            "plan_sha256": task["plan_sha256"],
            # 任务标识 + 计划哈希 提示头（§5.23.3）；计划正文由 worker 注入。
            "instruction": _execution_instruction(task, worktree_path, desktop=True),
            # T6（§5.23.10）：展示快照——旧 spec 缺这两字段仍按旧语义读取/
            # 续接（worker 只在两字段齐备时才在 pi_session_create 携带）。
            "workspace_display_name": workspace_display_name,
            "session_display_name": session_display_name,
            # 所有新轮次使用确定性命令验证；旧 spec 缺字段保持 Goal 报告口径。
            "completion_policy": "command_verification",
            "runs_dir": str(run_dir),
            "timeout_seconds": round_store.round_timeout_seconds(conn),
            "request_id": request["request_id"],
            "db_path": str(ctx.db_path),
        }
        spec_path = run_dir / "spec.json"
        from . import process_owner
        process_owner.bind_spec(spec, ctx.db_path)
        spec_path.write_text(util.jdump(spec), encoding="utf-8")
        log_path = run_dir / "worker.log"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(constants.core_root())
        try:
            with open(log_path, "ab") as log:
                proc = subprocess.Popen(
                    [
                        sys.executable, "-m", "kanpilot.pi_desktop_worker",
                        "--spec", str(spec_path),
                    ],
                    cwd=str(constants.core_root()),
                    stdout=log, stderr=log,
                    start_new_session=True, env=env,
                )
        except OSError as exc:
            raise util.KanpilotError("internal", f"worker 拉起失败：{exc}")

        _register_started_worker(ctx, spec, proc, spec_path)

    except (util.KanpilotError, OSError, subprocess.SubprocessError) as exc:
        from . import process_owner
        if isinstance(exc, process_owner.OwnerStoppedError):
            # Shutdown is local observation loss, not proof of failed execution.
            raise
        detail = util.redact(exc.message if isinstance(exc, util.KanpilotError) else str(exc))
        with db.write_txn(conn):
            conn.execute(
                "UPDATE execution_rounds SET run_status='failed', updated_at=? "
                "WHERE task_id=? AND run_id=?", (now, task_id, run_id),
            )
            db.add_event(conn, now, "round_start_failed", task_id=task_id,
                         request_id=request["request_id"], actor="kaneo-dispatch",
                         detail={"run_id": run_id, "reason": detail, "send_state": "not_sent"})
        if run_dir is not None and run_dir.is_dir():
            (run_dir / dsh_events.JOB_NAME).write_text(util.jdump({
                "task_id": task_id, "run_id": run_id, "backend": "pi_desktop",
                "status": "failed", "send_state": "not_sent", "pid": None,
                "config": config_snapshot, "detail": detail, "exit_code": 1,
                "started_at": now, "ended_at": now,
            }), encoding="utf-8")
        code = exc.code if isinstance(exc, util.KanpilotError) else "internal"
        raise util.KanpilotError(code, "pi_desktop 启动失败：" + detail) from exc

    # 4. 记 pid/worktree + event（意图已落盘，此处只是补记进程信息）。
    with db.write_txn(conn):
        round_store.mark_started(
            conn, task_id, run_id, now, pid=proc.pid,
            worktree_path=str(worktree_path),
        )
        db.add_event(
            conn, now, "round_started", task_id=task_id,
            request_id=request["request_id"], actor="kaneo-dispatch",
            detail={"run_id": run_id, "pid": proc.pid,
                    "worktree_path": str(worktree_path),
                    "worktree_mode": worktree_mode,
                    "backend": "pi_desktop"},
        )

    # 5. 等首个 session 事件（启动证明 gated；超时 → starting/null）。
    session_id = _wait_session_event(run_dir)
    return {
        "status": "started" if session_id else "starting",
        "pid": proc.pid,
        "session_id": session_id,
        "worktree_path": str(worktree_path),
        "deduplicated": False,
    }



def _wait_observe_attach(run_dir, proc, previous_job):
    """等观察进程的接管证明（job.observation_resumed_at）或明确未接管。

    返回 ``(status, detail)``：``observing``（已接管且在跑）/``settled``（接管
    即见持久终态，进程已退出）/``deferred``（未接管，原因在
    ``job.resume_detail``）/``starting``（超时未见接管证明）。
    """
    path = Path(run_dir) / dsh_events.JOB_NAME
    baseline_resumed = (previous_job or {}).get("observation_resumed_at")
    baseline_deferred = (previous_job or {}).get("resume_deferred_at")
    deadline = _time.monotonic() + _start_wait_seconds()
    while True:
        current = None
        try:
            current = util.jload(path.read_text(encoding="utf-8"))
        except OSError:
            current = None
        if isinstance(current, dict):
            resumed = current.get("observation_resumed_at")
            deferred = current.get("resume_deferred_at")
            if deferred and deferred != baseline_deferred:
                return "deferred", current.get("resume_detail")
            if resumed and resumed != baseline_resumed:
                if proc.poll() is None:
                    return "observing", None
                return "settled", current.get("detail")
        if proc.poll() is not None:
            return ("exited", "观察进程已退出且未给出接管证明")
        if _time.monotonic() >= deadline:
            return "starting", "观察进程尚未证明接管"
        _time.sleep(0.1)


def _observer_pid_alive(conn, task_id, run_id, pid, run_dir, job):
    from . import process_owner
    registered = process_owner.registered_worker_alive(conn, task_id, run_id, pid)
    if registered is not None:
        return registered
    # Historical owner records may already have been pruned on App restart.
    # An explicit manual observer copy supersedes the startup App ownership
    # only for that observer's own PID; its legacy liveness semantics remain.
    path = Path(run_dir) / "spec.json"
    observer_path = Path(run_dir) / "observer-spec.json"
    if pid and pid == job.get("observe_pid") and observer_path.is_file():
        path = observer_path
    try:
        spec = util.jload(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        spec = None
    if (isinstance(spec, dict) and spec.get("task_id") == task_id
            and spec.get("run_id") == run_id and spec.get("executor_instance_id")):
        return False
    return util.pid_alive(pid)


def cmd_round_observe(ctx, payload: dict) -> dict:
    """round-observe（protocol §5.23.7）：pi_desktop 轮次的观察者续接。

    观察进程退出（应用重启/崩溃/被清理）而 PI-Desktop 会话仍在运行时，重新
    拉起 worker 的 ``--resume`` 分支只读接管观察。**绝不新建会话、绝不重发
    prompt、绝不清空既有证据**；观察进程仍存活时幂等返回，不重复拉起。
    """
    conn, now = ctx.conn, ctx.now_iso
    task = db.require_task(conn, payload["task_id"])
    task_id, run_id = task["task_id"], payload["run_id"]
    if task["run_id"] != run_id:
        raise util.KanpilotError(
            "conflict", "只允许续接该任务当前轮次的观察进程"
        )
    if task["status"] not in ("running", "in_review", "needs_user"):
        raise util.KanpilotError(
            "conflict", f"任务处于 {task['status']}，无需续接观察进程"
        )
    row = round_store.require_round(conn, task_id, run_id)
    if row["backend"] != "pi_desktop":
        raise util.KanpilotError(
            "validation_failed", "只有 pi_desktop 轮次需要本机观察进程"
        )
    source = _headless.resolve_source(conn)
    try:
        run_dir = source.run_dir(task_id, run_id)
    except _headless.HeadlessError as exc:
        raise util.KanpilotError("validation_failed", exc.detail)
    job, error = dsh_events.read_job(
        run_dir, task_id, run_id, backend="pi_desktop"
    )
    if job is None:
        raise util.KanpilotError(
            "validation_failed", "运行记录不可读：" + (error or "未知原因")
        )
    if job.get("status") == "finished":
        return {"status": "finished", "pid": None,
                "detail": "轮次已按 completed 收尾，无需续接"}
    # 「已确认发送」的硬证据 = events.jsonl 的 session 事件（发送成功 + 启动
    # 证明共同产生）＋持久会话身份；send_state 只作粗筛（旧版假失败会把它写成
    # unknown，续接成功后由 worker 修回 sent）。
    events, _detail = dsh_events.iter_events(
        run_dir / dsh_events.EVENT_LOG_NAME
    )
    if dsh_events.session_event(events) is None:
        raise util.KanpilotError(
            "validation_failed",
            "该轮次没有 session 事件（未确认发送/启动），不能续接观察",
        )
    if not job.get("session_id") or not job.get("turn_id"):
        raise util.KanpilotError(
            "validation_failed",
            "该轮次缺少持久会话身份（session_id/turn_id），不能续接观察",
        )
    if job.get("send_state") not in ("sent", "unknown"):
        raise util.KanpilotError(
            "validation_failed",
            f"该轮次发送状态不可续接（send_state={job.get('send_state')!r}）",
        )
    # 幂等：仍存活的观察进程（原 worker 或上一次续接的进程）不再拉起第二个。
    alive = row["pid"] if _observer_pid_alive(conn, task_id, run_id, row["pid"], run_dir, job) else None
    if alive is None and _observer_pid_alive(conn, task_id, run_id, job.get("observe_pid"), run_dir, job):
        alive = job.get("observe_pid")
    if alive is not None:
        return {"status": "observing", "pid": alive, "deduplicated": True}
    spec_path = run_dir / "spec.json"
    if not spec_path.is_file():
        raise util.KanpilotError(
            "validation_failed", "缺少 spec.json，无法续接观察进程"
        )
    # Preserve the immutable startup spec. This explicit, already validated
    # resume gets its own observer copy, bound to the new App or manual CLI.
    from . import process_owner
    try:
        spec = util.jload(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise util.KanpilotError("validation_failed", "spec.json 不可读，无法登记观察者归属")
    if not isinstance(spec, dict) or spec.get("task_id") != task_id or spec.get("run_id") != run_id:
        raise util.KanpilotError("conflict", "spec.json 与当前执行轮次身份不符")
    if "KANPILOT_EXECUTOR_INSTANCE_ID" in os.environ:
        process_owner.bind_spec(spec, ctx.db_path)
    else:
        if spec.get("executor_instance_id"):
            spec["original_executor_instance_id"] = spec["executor_instance_id"]
            spec["requires_goal_identity"] = True
        spec.pop("executor_instance_id", None)
    spec_path = run_dir / "observer-spec.json"
    spec_path.write_text(util.jdump(spec), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(constants.core_root())
    try:
        with open(run_dir / "worker.log", "ab") as log:
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "kanpilot.pi_desktop_worker",
                    "--spec", str(spec_path), "--resume",
                ],
                cwd=str(constants.core_root()),
                stdout=log, stderr=log,
                start_new_session=True, env=env,
            )
    except OSError as exc:
        raise util.KanpilotError("internal", f"观察进程拉起失败：{exc}")

    _register_started_worker(ctx, spec, proc, spec_path)

    status, detail = _wait_observe_attach(run_dir, proc, job)
    reopened = False
    with db.write_txn(conn):
        if status == "observing":
            round_store.mark_started(conn, task_id, run_id, now, pid=proc.pid)
            round_store.upsert_discovered(
                conn, task_id, run_id, now, backend="pi_desktop",
                run_status="running",
            )
            # §5.23.8：本轮已被重新接管观察（例如瞬时模型错误后同一会话自动
            # 重开的新轮次），任务不得继续停在由此产生的执行失败上。只解除
            # 本轮的 execution_failed 阻塞；其它 needs_user 原因原样保留。
            fresh = db.require_task(conn, task_id)
            if (fresh["status"] == "needs_user"
                    and fresh["failure_code"] == "execution_failed"
                    and fresh["run_id"] == run_id):
                fields = core.clear_block_fields()
                core.transition_task(
                    conn, fresh, "running", now,
                    payload.get("executor") or "kaneo-monitor",
                    detail={"via": "round-observe", "reopened": True},
                    **fields,
                )
                reopened = True
        db.add_event(
            conn, now, "round_observe", task_id=task_id,
            actor=payload.get("executor") or "kaneo-monitor",
            detail={"run_id": run_id, "pid": proc.pid, "status": status,
                    "detail": detail, "reopened": reopened},
        )
    return {
        "status": status, "pid": proc.pid, "detail": detail,
        "run_id": run_id, "reopened": reopened,
    }


def _start_wait_seconds() -> float:
    raw = os.environ.get("KANPILOT_PI_START_WAIT")
    try:
        return max(0.0, float(raw)) if raw else float(
            constants.DEFAULT_PI_START_WAIT_SECONDS
        )
    except ValueError:
        return float(constants.DEFAULT_PI_START_WAIT_SECONDS)


def _wait_session_event(run_dir):
    deadline = _time.monotonic() + _start_wait_seconds()
    path = Path(run_dir) / dsh_events.EVENT_LOG_NAME
    while True:
        events, _detail = dsh_events.iter_events(path)
        session = dsh_events.session_event(events)
        if session is not None:
            return session.get("session_id")
        if _time.monotonic() >= deadline:
            return None
        _time.sleep(0.1)


def _manual_branch_delivery(task):
    delivery = core.delivery_of(task)
    return (core.automation_of(task).get("manual_takeover") is True
            and bool(delivery.get("branch")) and bool(delivery.get("worktree_path")))


def _execution_instruction(task, worktree_path, *, desktop):
    if _manual_branch_delivery(task):
        plan_ref = "下面已核验的计划正文" if desktop else f"计划文件 {task['plan_path']}"
        return (f"任务标识：{task['session_marker']}。当前目录 {worktree_path} 是本任务保留的分支 "
                f"{core.delivery_of(task)['branch']} 的工作树。直接在此完整执行{plan_ref}，"
                "保留已有提交和未提交改动；不要另建 worktree 或分支，也不要切换分支。"
                "按计划使用 subagent，完成验证并报告结果。")
    if core.is_pr_delivery(task):
        return constants.DISPATCH_INSTRUCTION_BRANCH.format(
            marker=task["session_marker"], worktree=str(worktree_path),
            branch=core.delivery_of(task)["branch"],
            plan_ref="下面已核验的计划正文" if desktop else f"计划文件 {task['plan_path']}")
    if not desktop:
        return constants.DISPATCH_INSTRUCTION_PI.format(
            marker=task["session_marker"], plan_path=task["plan_path"])
    return (f"任务标识：{task['session_marker']}。计划哈希：{task['plan_sha256']}。"
            "完整执行以下已核验的计划正文，保留现有改动，按计划使用 "
            "subagent 和独立 worktree，完成验证并报告结果。")


def _ensure_worktree(task, task_id, run_id, conn, *, require_isolated=False):
    """protocol §5.18.1：worktrees_root/<task>/<run>；git 仓库 → 独立
    worktree（已存在复用），非 git / git 失败 → 直接用 task.repository 作
    cwd（worktree_mode="plain"，不阻断测试夹具与非 git 目录）。"""
    manual_branch = _manual_branch_delivery(task)
    if core.is_pr_delivery(task) or manual_branch:
        delivery = dict(core.delivery_of(task), repository=task["repository"])
        user_replan = core.is_user_replan(task)
        state_sha = core.automation_of(task).get("replan_worktree_sha256")
        if not manual_branch:
            util.require(bool(delivery.get("head_sha")), "交付 HEAD 登记缺失", "validation_failed")
        if user_replan:
            util.require(bool(state_sha), "重新规划缺少工作树状态指纹", "validation_failed")
            util.require(task["send_state"] not in ("unknown", "sent") and task["sent"] != 1,
                         "重新规划仍有发送证据冲突，必须先完成发送恢复", "send_conflict")
        try:
            git_delivery.verify_branch_worktree(
                delivery, require_clean=not (manual_branch or user_replan),
                expected_head=None if manual_branch else delivery.get("head_sha"))
            if user_replan:
                util.require(git_delivery.worktree_state_sha256(delivery) == state_sha,
                             "重新规划后工作树内容已变化，请重新准备规划材料", "validation_failed")
        except git_delivery.GitDeliveryError as exc:
            raise util.KanpilotError("validation_failed", exc.message) from exc
        return Path(delivery["worktree_path"]), "branch"
    root = constants.worktrees_root(
        db.get_setting(conn, "worktrees_root") or None
    )
    path = root / task_id / run_id
    repo = Path(task["repository"]).expanduser()
    if path.exists():
        if require_isolated:
            try:
                def git_path(directory, option):
                    result = subprocess.run(
                        ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", option],
                        capture_output=True, text=True, timeout=10, check=True,
                    )
                    return Path(result.stdout.strip()).resolve()
                valid = (path.resolve() != repo.resolve()
                         and git_path(path, "--show-toplevel") == path.resolve()
                         and git_path(path, "--git-common-dir") == git_path(repo, "--git-common-dir"))
            except (subprocess.SubprocessError, OSError):
                valid = False
            util.require(valid, "已有执行目录不是本仓库独立 worktree", "validation_failed")
        return path, "reuse"
    repo_is_git = (repo / ".git").exists()
    if repo_is_git:
        try:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach",
                 str(path), "HEAD"],
                capture_output=True, text=True, timeout=60, check=True,
            )
            return path, "git"
        except (subprocess.SubprocessError, OSError) as exc:
            if require_isolated:
                raise util.KanpilotError("validation_failed", "无法创建独立 Git worktree，未启动执行") from exc
            # Historical pi behavior is unchanged; desktop fails closed.
            return repo, "plain"
    if require_isolated:
        raise util.KanpilotError("validation_failed", "Pi-Desktop-Plus 执行要求 Git 仓库与独立 worktree")
    return repo, "plain"
