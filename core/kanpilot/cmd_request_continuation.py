"""request-continuation：向同一 Pi Desktop Plus 会话发送继续当前 Goal 的指令。

只发送一条新的用户消息（新 turn），**不**新建会话、不重新规划、不改 job/任务
状态；随后由桌面轮询 ``check-execution-status``，新 turn 结束后复用现有
``continue-execution``（接续成果 → 排队核验）链路。协议见 protocol §5.23.11。

发送槽由 runtime_operations 抢占；响应丢失记 unknown，绝不重发。

``reconcile-continuation`` 只读核对 Plus 转录后，经用户显式确认才回收该发送槽：
分类为「未送达」则把 unknown 记为 rejected（槽位释放，可再次发送）；分类为
「已送达」则按转录里的真实 turn 走与发送成功相同的同轮重开；无法归因时保持
unknown 并交由人工核对，绝不猜测、绝不重发。
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
    pending = _pending_operations(conn, scope)
    if pending:
        # A lost response never resends; it must be reconciled against the
        # Plus transcript first (reconcile-continuation).
        view["reason"] = "上一次继续发送结果未确认，请先核对 Plus 会话"
        view["reconcile"] = {
            "available": len(pending) == 1,
            "operation_id": pending[0]["operation_id"] if len(pending) == 1 else None,
            "created_at": pending[0]["created_at"] if len(pending) == 1 else None,
        }
        return view
    view.update(eligible=True, state="ready")
    return view


def _pending_operations(conn, scope):
    """Unconfirmed continue sends for this scope, oldest first."""
    return conn.execute(
        "SELECT operation_id, status, created_at FROM runtime_operations "
        "WHERE scope_id=? AND tool=? AND status IN ('prepared','unknown') "
        "ORDER BY created_at, operation_id", (scope, SCOPE_TOOL)).fetchall()


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


def _require_round(conn, task, run_id):
    """Identity gates shared by sending and by reconciling a lost send."""
    source = headless.resolve_source(conn)
    try:
        run_dir = source.run_dir(task["task_id"], run_id)
    except headless.HeadlessError as exc:
        raise util.KanpilotError("validation_failed", exc.detail)
    job = _read_job(run_dir, task["task_id"], run_id)
    spec = _read_spec(run_dir)
    row = round_store.require_round(conn, task["task_id"], run_id)
    if (job.get("task_id") != task["task_id"] or job.get("run_id") != run_id
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
    return {"run_dir": run_dir, "job": job, "spec": spec, "row": row,
            "session_id": session_id, "turn_id": old_turn}


def _plus_inner(status):
    inner = status.get("status") if isinstance(status.get("status"), dict) else status
    return inner if isinstance(inner, dict) else {}


_MARKER = PROMPT.split("：")[0]
_SCAN_PAGE, _SCAN_PAGES = 20, 40


def _page(client, session_id, before=None):
    """One bounded transcript page; ``None`` when the control plane truncates it."""
    page = client.session_get(session_id, message_before=before,
                              message_limit=_SCAN_PAGE)
    return None if page.get("truncated") else (page.get("session") or {})


def _turn_opener(client, session_id, turn_id):
    """``(found, complete, is_continue)`` for the user message that opened a turn."""
    before = None
    for _page_no in range(_SCAN_PAGES):
        session = _page(client, session_id, before)
        if session is None:
            return False, False, False
        for row in session.get("messages") or ():
            if (isinstance(row, dict) and row.get("role") == "user"
                    and row.get("turnId") == turn_id):
                return True, True, _MARKER in str(row.get("content") or "")
        first = session.get("message_start")
        if not session.get("has_more_before") or not isinstance(first, int) or first <= 0:
            return False, True, False
        before = first
    return False, False, False


def _reconcile_classify(client, session_id, old_turn, inner):
    """Read-only attribution of one unconfirmed continue send.

    ``not_sent``  — 会话没有更新的轮次，尾部转录里也没有本任务的继续指令：
                    可以回收发送槽（可再次发送）。
    ``delivered`` — 更新的轮次正是由这条继续指令发起：按真实 turn 接管观察。
    ``unknown``   — 其余情况（新轮次无法归因、会话仍在运行、转录不可读或
                    超出分页上限）：保持发送槽与任务现场，交人工核对。
    """
    current = inner.get("currentTurnId")
    running = inner.get("isRunning") is True
    newer = (isinstance(current, str) and current and current != old_turn)
    if running and not newer:
        return ("unknown", None, "Plus 会话仍在运行同一轮次，无法确认继续指令的去向")
    if newer:
        found, complete, is_continue = _turn_opener(client, session_id, current)
        if found and is_continue:
            return ("delivered", current, "本轮次的发起消息就是本任务的继续指令")
        if found:
            return ("unknown", None, "新轮次不是由本任务的继续指令发起，不改写其归属")
        if not complete:
            return ("unknown", None, "存在新轮次，但转录分页不完整，无法确认其来源")
        return ("unknown", None, "存在新轮次，但转录中找不到它的发起消息")
    # No newer turn: a lost send can only have left a stray tail message.
    session = _page(client, session_id)
    if session is None:
        return ("unknown", None, "转录读取不完整，无法确认继续指令是否已送达")
    for row in session.get("messages") or ():
        if isinstance(row, dict) and row.get("role") == "user" \
                and _MARKER in str(row.get("content") or ""):
            return ("unknown", None, "尾部转录已有继续指令消息，但会话没有对应轮次")
    if session.get("has_more_after"):
        return ("unknown", None, "转录尚未读到最新分页，无法确认继续指令是否已送达")
    return ("not_sent", None, "Plus 会话没有新轮次，最新转录中也没有本任务的继续指令")


def _adopt(ctx, task_id, run_id, state, new_turn, operation_id, now, via):
    """Bind the finished run to ``new_turn`` and hand observation to the worker."""
    conn = ctx.conn
    job, run_dir, old_turn = state["job"], state["run_dir"], state["turn_id"]
    # 同轮重开：把已收尾的 job 绑定到新 turn，清掉旧终态字段，再由
    # round-observe 拉起 worker --resume 观察新 turn；旧 terminal 由
    # observation_resumed_at 标记 superseded。
    previous = {key: job.get(key) for key in
                ("status", "turn_id", "send_state", "observation_resumed_at",
                 "started_at", "ended_at", "exit_code", "detail", "pid",
                 "observe_pid")}
    history = list(job.get("turn_adopted") or [])
    history.append({"from": old_turn, "to": new_turn, "code": via, "at": now})
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
            detail={"via": via, "run_id": run_id,
                    "turn_id": new_turn, "previous_turn_id": old_turn},
            **core.clear_block_fields())
        db.set_extra(conn, fresh, now, **{REQUEST_KEY: {
            "run_id": run_id, "session_id": state["session_id"],
            "from_turn": old_turn, "new_turn": new_turn,
            "operation_id": operation_id, "status": "sent", "sent_at": now}})
        db.add_event(conn, now, "execution_continuation_sent", task_id=task_id,
                     actor="user", detail={"run_id": run_id, "from_turn": old_turn,
                                           "to_turn": new_turn,
                                           "operation_id": operation_id, "via": via})
        conn.execute(
            "UPDATE execution_rounds SET run_status='running', updated_at=? "
            "WHERE task_id=? AND run_id=?", (now, task_id, run_id))

    try:
        observer = cmd_dispatch.cmd_round_observe(
            ctx, {"task_id": task_id, "run_id": run_id, "executor": "user"})
    except util.KanpilotError as exc:
        observer = {"status": "error", "detail": exc.message}
    return observer


def cmd_request_continuation(ctx, payload: dict) -> dict:
    """向同一 Plus 会话发送继续指令；一次发送，不重发。"""
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

    state = _require_round(conn, task, run_id)
    run_dir, job, spec = state["run_dir"], state["job"], state["spec"]
    session_id, old_turn = state["session_id"], state["turn_id"]

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
        status = reservation["status"]
        if status in ("accepted", "completed") and reservation.get("turn_id"):
            return {"status": "sent", "deduplicated": True,
                    "turn_id": reservation["turn_id"]}
        raise util.KanpilotError(
            "conflict",
            "上一次继续发送结果未确认（%s），不会重复发送；请先核对 Plus 会话" % status)

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

    observer = _adopt(ctx, task_id, run_id, state, new_turn, operation_id, now,
                      "manual_continue")
    return {"status": "continued", "turn_id": new_turn,
            "session_id": session_id, "observer": observer}


def cmd_reconcile_continuation(ctx, payload: dict) -> dict:
    """核对未确认的继续发送，并在用户确认后回收发送槽（不重发）。"""
    task_id, run_id = payload["task_id"], payload["run_id"]
    conn, now = ctx.conn, ctx.now_iso
    task = db.require_task(conn, task_id)
    if task["run_id"] != run_id:
        raise util.KanpilotError("stale_run", "所选轮次已不是当前执行轮次，请刷新后重试")
    view = continue_execution_view(conn, task)
    entry = view.get("reconcile") or {}
    if not entry.get("operation_id"):
        raise util.KanpilotError(
            "conflict", view.get("reason") or "当前没有待核对的继续发送")
    if task["status"] != "needs_user" or not db.task_is_active(task):
        raise util.KanpilotError("conflict", "任务不在需人工处理状态，不能核对继续发送")
    scope = runtime_client.stable_scope_id("execution", task_id=task_id, run_id=run_id)
    operation_id = entry["operation_id"]
    operation = runtime_operations.get_operation(conn, scope, operation_id)
    if operation is None or operation["status"] not in ("prepared", "unknown"):
        raise util.KanpilotError("conflict", "该继续发送已有终态记录，不需要再核对")

    state = _require_round(conn, task, run_id)
    session_id, old_turn = state["session_id"], state["turn_id"]
    try:
        client = _client(state["spec"], task_id, run_id, read_only=True)
        inner = _plus_inner(client.agent_status(session_id))
        if not (session_id and inner.get("sessionId") in (None, session_id)):
            raise util.KanpilotError("conflict", "Plus 返回的会话身份与当前轮次不符")
        classification, turn, detail = _reconcile_classify(
            client, session_id, old_turn, inner)
    except util.KanpilotError:
        raise
    except (OSError, ValueError) as exc:
        raise util.KanpilotError("remote_unavailable", "Plus 控制面不可达：" + str(exc))

    report = {"task_id": task_id, "run_id": run_id, "operation_id": operation_id,
              "operation_status": operation["status"], "session_id": session_id,
              "original_turn_id": old_turn, "classification": classification,
              "turn_id": turn, "detail": detail, "applied": False}
    if payload.get("dry_run", True) is not False:
        return report
    if classification == "unknown":
        # 归因不明时保持现场：不回收发送槽、不改任务，交人工核对。
        report["status"] = "unconfirmed"
        return report
    if payload.get("confirm") is not True:
        raise util.KanpilotError("bad_request", "核对结果需要显式确认才能回收发送槽")

    with db.write_txn(conn):
        fresh = db.require_task(conn, task_id)
        current_op = runtime_operations.get_operation(conn, scope, operation_id)
        if (fresh["run_id"] != run_id or fresh["status"] != "needs_user"
                or current_op is None
                or current_op["status"] not in ("prepared", "unknown")):
            raise util.KanpilotError("conflict", "核对期间任务或发送记录已变化，请刷新后重试")
        if classification == "not_sent":
            runtime_operations.complete_operation(
                conn, scope, operation_id, status="rejected",
                result={"reconciled": "not_sent", "session_id": session_id,
                        "turn_id": old_turn, "detail": detail},
                session_id=session_id, turn_id=old_turn, now=now)
            db.add_event(conn, now, "execution_continuation_reconciled", task_id=task_id,
                         actor="user", detail={"run_id": run_id, "operation_id": operation_id,
                                               "classification": "not_sent", "detail": detail})
        elif classification == "delivered":
            runtime_operations.complete_operation(
                conn, scope, operation_id, status="accepted",
                result={"reconciled": "delivered", "session_id": session_id,
                        "turn_id": turn, "detail": detail},
                session_id=session_id, turn_id=turn, now=now)
            db.add_event(conn, now, "execution_continuation_reconciled", task_id=task_id,
                         actor="user", detail={"run_id": run_id, "operation_id": operation_id,
                                               "classification": "delivered", "turn_id": turn,
                                               "detail": detail})

    report["applied"] = True
    if classification == "delivered":
        report["observer"] = _adopt(ctx, task_id, run_id, state, turn, operation_id, now,
                                    "reconciled_continue")
        report["status"] = "continued"
    elif classification == "not_sent":
        report["status"] = "released"
    else:
        report["status"] = "unconfirmed"
    return report
