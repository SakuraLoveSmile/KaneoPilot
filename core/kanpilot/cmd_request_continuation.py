"""request-continuation：向同一 Pi Desktop Plus 会话发送继续当前 Goal 的指令。

发送一条新的用户消息（新 turn），沿用原会话和目标；拿到新 turn 后同轮重开
job、将任务恢复 running，并接回观察者。Goal 受阻决定通过专用宿主 resume
接口发送，也复用本模块的接续步骤。协议见 protocol §5.23.11。

发送槽由 runtime_operations 抢占；响应丢失记 unknown，绝不重发。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from . import (
    constants, core, db, dsh_events, headless_adapter as headless,
    round_store, runtime_client, runtime_operations, util,
)
from . import cmd_dispatch

CONTINUE_FAILURES = frozenset(
    {"verification_unconfirmed", "execution_failed", "execution_interrupted"}
)
REQUEST_KEY = "execution_continuation_request"
SCOPE_TOOL = "request-continuation"
PROMPT = (
    "继续执行当前 Goal 目标：上一轮已收尾，但目标验收仍未完成或受阻。"
    "沿用原计划、原工作树与冻结的验证要求，补齐未完成项或失败证据；"
    "不要重新规划，不要更换目标。完成后重新提交结构化 Goal 报告，"
    "并确保每条冻结 verify 都有原生命令与整数退出码证据。"
)


def continue_execution_view(conn, task) -> dict:
    """只读投影（get-task）：按钮是否可发送继续指令，或已发送等待新 turn。"""
    run_id = task["run_id"]
    view = {"eligible": False, "reason": None, "state": "none",
            "run_id": run_id, "turn_id": None}
    if not run_id:
        view["reason"] = "该任务没有执行轮次"
        return view
    request = db.task_extra(task).get(REQUEST_KEY) or {}
    if request.get("run_id") == run_id and request.get("new_turn"):
        observing = task["status"] == "running"
        view.update(state="observing" if observing else "sent",
                    turn_id=request.get("new_turn"),
                    reason="已接管新轮次观察" if observing
                    else "已发送继续指令，正在接管观察")
        return view
    if task["status"] != "needs_user":
        view["reason"] = "任务不在需人工处理状态"
        return view
    if task["failure_code"] not in CONTINUE_FAILURES:
        view["reason"] = "当前阻塞类型不支持继续执行"
        return view
    row = round_store.get_round(conn, task["task_id"], run_id)
    if row is None or row["backend"] != "pi_desktop":
        view["reason"] = "当前轮次不是 Pi Desktop 执行"
        return view
    if row["run_status"] not in ("finished", "failed"):
        view["reason"] = "执行轮次尚未收尾"
        return view
    if not row["session_id"]:
        view["reason"] = "执行轮次没有可继续的会话"
        return view
    if conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
        (task["task_id"],),
    ).fetchone():
        view["reason"] = "已有排队或领取中的请求"
        return view
    scope = runtime_client.stable_scope_id(
        "execution", task_id=task["task_id"], run_id=run_id)
    pending = conn.execute(
        "SELECT 1 FROM runtime_operations WHERE scope_id=? AND tool=? "
        "AND status IN ('prepared','unknown') LIMIT 1",
        (scope, SCOPE_TOOL),
    ).fetchone()
    if pending:
        view["reason"] = "上一次继续发送结果未确认，请先核对 Plus 会话"
        return view
    view.update(eligible=True, state="ready")
    return view


def _read_job(run_dir: Path, task_id: str, run_id: str):
    job, error = dsh_events.read_job(run_dir, task_id, run_id, backend="pi_desktop")
    if job is None:
        raise util.KanpilotError(
            "validation_failed", "运行记录不可读：" + (error or "未知原因"))
    return job


def _read_spec(run_dir: Path) -> dict:
    try:
        spec = util.jload((run_dir / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise util.KanpilotError("validation_failed", "spec.json 不可读")
    if not isinstance(spec, dict):
        raise util.KanpilotError("validation_failed", "spec.json 不是对象")
    return spec


def _client(spec: dict, task_id: str, run_id: str, *, read_only: bool):
    config = spec.get("config") or {}
    if config.get("permission_mode") != "auto":
        raise util.KanpilotError(
            "forbidden", "继续执行仅适用于已保存的全自动权限")
    connection_file = config.get("connection_file")
    cwd = spec.get("cwd")
    if not isinstance(connection_file, str) or not connection_file:
        raise util.KanpilotError("validation_failed", "本轮缺少 Plus 连接文件")
    if not isinstance(cwd, str) or not cwd:
        raise util.KanpilotError("validation_failed", "本轮缺少工作目录")
    client = runtime_client.RuntimeClient(
        role="execution", connection_file=connection_file,
        project_path=cwd, runtime_config=config, task_id=task_id,
        run_id=run_id, timeout=20,
        lease_seconds=spec.get("timeout_seconds"), read_only=read_only)
    client.connect(required_tools=constants.PI_DESKTOP_REQUIRED_TOOLS)
    return client


def cmd_request_continuation(ctx, payload: dict) -> dict:
    """向同一 Plus 会话发送继续指令并接回观察；一次发送，不重发。"""
    if payload.get("confirm") is not True:
        raise util.KanpilotError("bad_request", "继续执行需要显式确认")
    task_id, run_id = payload["task_id"], payload["run_id"]
    conn, now = ctx.conn, ctx.now_iso

    task = db.require_task(conn, task_id)
    view = continue_execution_view(conn, task)
    if not view["eligible"]:
        if view["state"] in ("sent", "observing"):
            return {"status": "continued", "deduplicated": True,
                    "turn_id": view.get("turn_id")}
        raise util.KanpilotError("conflict", view["reason"] or "当前不可继续执行")
    if task["run_id"] != run_id:
        raise util.KanpilotError("conflict", "只允许继续当前执行轮次")

    source = headless.resolve_source(conn)
    try:
        run_dir = source.run_dir(task_id, run_id)
    except headless.HeadlessError as exc:
        raise util.KanpilotError("validation_failed", exc.detail)
    job = _read_job(run_dir, task_id, run_id)
    spec = _read_spec(run_dir)
    row = round_store.require_round(conn, task_id, run_id)
    if (job.get("task_id") != task_id or job.get("run_id") != run_id
            or job.get("backend") != "pi_desktop"):
        raise util.KanpilotError("conflict", "job.json 身份与当前轮次不符")
    if job.get("status") not in ("finished", "failed"):
        raise util.KanpilotError("conflict", "执行轮次尚未收尾")
    if job.get("send_state") not in ("sent", "unknown"):
        raise util.KanpilotError("conflict", "执行轮次没有已确认的发送记录")
    session_id, old_turn = job.get("session_id"), job.get("turn_id")
    if not session_id or not old_turn:
        raise util.KanpilotError("conflict", "执行轮次缺少会话/turn 身份")
    if (spec.get("plan_path") != task["plan_path"]
            or spec.get("plan_sha256") != task["plan_sha256"]
            or (spec.get("config") or {}) != util.jload(row["config_json"])):
        raise util.KanpilotError("conflict", "计划或执行配置快照已变化，不能继续同一目标")
    events, _detail = dsh_events.iter_events(run_dir / dsh_events.EVENT_LOG_NAME)
    session_event = dsh_events.session_event(events)
    if session_event is None or session_event.get("session_id") != session_id:
        raise util.KanpilotError("conflict", "缺少可核验的 session 事件")
    if not dsh_events.terminal_event(events):
        raise util.KanpilotError("conflict", "上一轮没有持久终态，不能确认已收尾")

    try:
        probe = _client(spec, task_id, run_id, read_only=True)
        status = probe.agent_status(session_id)
        inner = status.get("status") if isinstance(status.get("status"), dict) else status
        current = inner.get("currentTurnId") if isinstance(inner, dict) else None
        if isinstance(current, str) and current and current != old_turn:
            raise util.KanpilotError(
                "conflict", "Plus 会话已有后续 turn，请先在会话页核对")
        record = probe.turn_get(session_id, old_turn, require_identity=True)
        if str(record.get("status") or "").lower() not in ("completed", "error", "aborted"):
            raise util.KanpilotError("conflict", "当前 turn 尚未持久收尾")
    except util.KanpilotError:
        raise
    except (OSError, ValueError) as exc:
        raise util.KanpilotError("remote_unavailable", "Plus 控制面不可达：" + str(exc))

    scope = runtime_client.stable_scope_id(
        "execution", task_id=task_id, run_id=run_id)
    operation_id = payload.get("operation_id") or uuid.uuid4().hex
    reservation = runtime_operations.reserve_downstream(
        conn, scope, operation_id, "kaneo-request-continuation", SCOPE_TOOL,
        {"task_id": task_id, "run_id": run_id, "session_id": session_id,
         "turn_id": old_turn})
    if not reservation["send"]:
        state = reservation["status"]
        if state in ("accepted", "completed") and reservation.get("turn_id"):
            return {"status": "sent", "deduplicated": True,
                    "turn_id": reservation["turn_id"]}
        raise util.KanpilotError(
            "conflict",
            "上一次继续发送结果未确认（%s），不会重复发送；请核对 Plus 会话" % state)

    try:
        client = _client(spec, task_id, run_id, read_only=False)
        result = client.agent_prompt(session_id, PROMPT)
        new_turn = result.get("turnId")
        if not new_turn:
            raise util.KanpilotError("internal", "继续指令未返回 turnId")
    except Exception as exc:  # noqa: BLE001 - 不重发
        detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
        runtime_operations.mark_operation_unknown(conn, scope, operation_id, now)
        raise util.KanpilotError(
            "remote_unavailable",
            "继续指令发送结果未确认，已停止且不会重发：" + detail)

    runtime_operations.complete_operation(
        conn, scope, operation_id, status="accepted",
        result={"turn_id": new_turn, "session_id": session_id},
        session_id=session_id, turn_id=new_turn, now=now)

    return adopt_continuation(ctx, task_id=task_id, run_id=run_id, job=job,
        run_dir=run_dir, session_id=session_id, old_turn=old_turn, new_turn=new_turn,
        operation_id=operation_id)


def adopt_continuation(ctx, *, task_id, run_id, job, run_dir, session_id, old_turn,
                       new_turn, operation_id):
    """Adopt a host-started continuation; never starts another model turn."""
    conn, now = ctx.conn, ctx.now_iso
    adoption = {"operation_id": operation_id, "task_id": task_id, "run_id": run_id,
                "session_id": session_id, "from_turn": old_turn, "new_turn": new_turn}
    prepared = job.get("continuation_adoption") == adoption and job.get("turn_id") == new_turn
    if not prepared:
        # 同轮重开：把已收尾的 job 绑定到新 turn，清掉旧终态字段，再由
        # round-observe 拉起 worker --resume 观察新 turn；旧 terminal 由
        # observation_resumed_at 标记 superseded。
        previous = {key: job.get(key) for key in
                    ("status", "turn_id", "send_state", "observation_resumed_at",
                     "started_at", "ended_at", "exit_code", "detail", "pid",
                     "observe_pid")}
        history = list(job.get("turn_adopted") or [])
        history.append({"from": old_turn, "to": new_turn,
                        "code": "manual_continue", "at": now})
        job["continue_previous"] = previous
        job["turn_adopted"] = history[-5:]
        job["continuation_started_at"] = now
        job["observation_resumed_at"] = now
        job["status"] = "running"
        job["turn_id"] = new_turn
        job["send_state"] = "sent"
        job["ended_at"] = None
        job["exit_code"] = None
        job["detail"] = None
        job["pid"] = None
        job["observe_pid"] = None
        for key in ("observation_finalizing", "heartbeat_stopped", "resume_previous"):
            job.pop(key, None)
        job["continuation_adoption"] = adoption
        job_path = run_dir / dsh_events.JOB_NAME
        tmp = job_path.with_name(job_path.name + ".continue.tmp")
        tmp.write_text(util.jdump(job), encoding="utf-8")
        os.replace(tmp, job_path)
    with db.write_txn(conn):
        fresh = db.require_task(conn, task_id)
        if fresh["run_id"] != run_id or fresh["status"] != "needs_user":
            raise util.KanpilotError("conflict", "任务状态已变化，无法继续执行")
        core.transition_task(
            conn, fresh, "running", now, "user",
            detail={"via": "request-continuation", "run_id": run_id,
                    "turn_id": new_turn, "previous_turn_id": old_turn},
            **core.clear_block_fields())
        db.set_extra(conn, fresh, now, **{REQUEST_KEY: {
            "run_id": run_id, "session_id": session_id,
            "from_turn": old_turn, "new_turn": new_turn,
            "operation_id": operation_id, "status": "sent", "sent_at": now}})
        db.add_event(conn, now, "execution_continuation_sent", task_id=task_id,
                     actor="user", detail={"run_id": run_id, "from_turn": old_turn,
                                           "to_turn": new_turn,
                                           "operation_id": operation_id})
        conn.execute(
            "UPDATE execution_rounds SET run_status='running', updated_at=? "
            "WHERE task_id=? AND run_id=?", (now, task_id, run_id))

    try:
        observer = cmd_dispatch.cmd_round_observe(
            ctx, {"task_id": task_id, "run_id": run_id, "executor": "user"})
    except util.KanpilotError as exc:
        observer = {"status": "error", "detail": exc.message}
    return {"status": "continued", "turn_id": new_turn,
            "session_id": session_id, "observer": observer}
