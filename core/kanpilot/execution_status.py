"""A current-run observation only: no reconciliation, model or database writes."""

from __future__ import annotations

from . import cmd_session, db, headless_adapter, session_control, util


_LABELS = {
    "running": "仍在运行", "waiting_permission": "等待权限确认",
    "finished": "执行轮次已结束", "failed": "执行轮次已失败",
    "aborted": "执行轮次已中止", "not_started": "尚未开始执行",
    "unknown": "状态未确认",
}
_BINDING = ("run_id", "session_id", "session_marker")
_JOB_BINDING = ("task_id", "run_id", "backend", "session_id", "turn_id", "config")
_JOB_STATE = ("status", "ended_at", "exit_code", "observation_resumed_at")
_TERMINAL_NOTE = "这只确认执行轮次已结束；需求完成仍以验证、评审与人工验收为准。"


def _text(value):
    return util.redact(value)[:4096] if isinstance(value, str) and value else None


def _check(result, name, label, state, detail):
    result["checks"].append({"name": name, "label": label,
                             "state": state, "detail": _text(detail) or "暂无可核验说明"})


def _state(result, state, summary, next_step):
    result.update(state=state, label=_LABELS[state], summary=summary, next_step=next_step)


def _unknown(result, summary):
    _state(result, "unknown", summary,
           "请重新核验或查看执行会话与原始记录；状态确认前不要重复启动执行。")


def _fingerprint(row, fields):
    return tuple(row[name] for name in fields)


def _terminal_state(job, detail):
    """Do not confuse timeout-derived failure with a durable terminal result."""
    status, terminal = job.get("status"), detail.get("terminal") or {}
    if (type(job.get("exit_code")) is not int or util.parse_ts(job.get("ended_at")) is None
            or detail.get("observer_alive") is not False):
        return None
    if job.get("backend") == "codex":
        from . import codex_execution
        try:
            codex_execution.require_settled(job)
        except util.KanpilotError:
            return None
    elif not session_control._process_exited(job.get("observe_pid") or job.get("pid")):
        return None
    outcome = terminal.get("outcome")
    if status == "finished" and job["exit_code"] == 0 and outcome == "completed":
        return "finished"
    if status == "failed" and job["exit_code"] != 0 and outcome == "failed":
        return "failed"
    if status == "aborted" and job["exit_code"] != 0 and outcome in ("aborted", "failed"):
        return "aborted"
    return None


def _project_plus(result, ctx, payload, job, detail, session):
    control = session_control.cmd_session_control_status(ctx, payload)
    expected_session = job.get("session_id")
    if (control.get("task_id") != payload["task_id"] or control.get("run_id") != payload["run_id"]
            or control.get("backend") != "pi_desktop"
            or control.get("session_id") != expected_session):
        _check(result, "plus", "Plus 当前状态", "unknown", "返回的会话身份不匹配，未采信。")
        _unknown(result, "无法确认当前 Plus 会话的身份。")
        return
    state = control.get("state")
    if control.get("connected") is not True or state not in (
            "running", "waiting_permission", "idle", "not_started"):
        _check(result, "plus", "Plus 当前状态", "unknown", control.get("detail"))
        _unknown(result, "无法读取绑定会话的实时状态；原失败记录仍保留。")
        return
    if state == "not_started" and not expected_session and not job.get("turn_id"):
        _check(result, "plus", "Plus 当前状态", "confirmed", "Plus 已连接，尚无该轮次的会话记录。")
        _state(result, "not_started", "尚未发现本轮的 Plus 会话。",
               "请查看执行会话与派发记录，按现有允许的操作处理；本次核验不会启动执行。")
        return
    current, original = control.get("observed_turn_id"), job.get("turn_id")
    if not expected_session or not isinstance(current, str) or not current or not original:
        _check(result, "plus", "Plus 当前状态", "unknown", "缺少本轮或当前轮次身份，未采信空闲或运行标记。")
        _unknown(result, "Plus 已连接，但轮次身份尚未完整确认。")
        return
    continued = control.get("continued_state")
    continuation_fields = any(control.get(key) is not None for key in (
        "continued_state", "continued_turn_id", "continued_ended_at", "continued_detail"))
    if current != original or continuation_fields:
        valid = (job.get("status") == "failed" and current != original
                 and control.get("continued_turn_id") == current
                 and continued in ("running", "waiting_permission", "finished", "failed", "aborted")
                 and bool(control.get("continued_detail"))
                 and util.parse_ts(control.get("original_turn_ended_at")) is not None)
        if continued in ("finished", "failed", "aborted"):
            valid = valid and state == "idle" and util.parse_ts(control.get("continued_ended_at")) is not None
        elif continued == "running":
            valid = valid and state == "running"
        elif continued == "waiting_permission":
            # A verified Goal proposal awaiting approval is idle at the session
            # level, while _continued_status correctly exposes its pending turn.
            valid = valid and state in ("waiting_permission", "idle")
        if not valid:
            _check(result, "plus", "Plus 当前状态", "unknown", "会话存在后续轮次，但其身份或结束证据尚未确认。")
            _unknown(result, "无法确认后续 Plus 轮次与本任务的关系。")
            return
        _check(result, "plus", "Plus 后续轮次", "confirmed", control["continued_detail"])
        if continued in ("finished", "failed", "aborted"):
            if (detail.get("observer_alive") is not False
                    or not session_control._process_exited(job.get("observe_pid") or job.get("pid"))):
                _unknown(result, "Plus 后续轮次已结束，但本机观察进程尚未确认退出。")
                return
            _state(result, continued, control["continued_detail"] + "；原失败记录保留。",
                   _TERMINAL_NOTE + "如需继续，请按任务当前允许的恢复操作处理。")
            return current if continued == "finished" else None
        else:
            _state(result, continued, control["continued_detail"] + "；原失败记录保留。",
                   "请在 Plus 中处理当前确认。" if continued == "waiting_permission"
                   else "请继续观察当前会话，避免重复启动执行。")
            if continued == "waiting_permission" and state == "idle":
                result["label"] = "等待确认"
        return
    _check(result, "plus", "Plus 当前状态", "confirmed", control.get("detail"))
    if state in ("running", "waiting_permission"):
        _state(result, state, "绑定的 Plus 轮次仍在运行。" if state == "running"
               else "绑定的 Plus 轮次正在等待权限确认。",
               "请继续观察当前会话，避免重复启动执行。" if state == "running"
               else "请在 Plus 中处理权限确认，然后重新核验。")
        return
    terminal = _terminal_state(job, detail)
    if terminal:
        _state(result, terminal, "Plus 当前空闲，原轮次的结束记录与进程退出证据一致。",
               _TERMINAL_NOTE + "如需继续，请按任务当前允许的恢复操作处理。")
        return current if terminal == "finished" else None
    else:
        _unknown(result, "Plus 当前空闲，但原轮次的本地结束记录或进程退出证据尚不完整。")


def cmd_check_execution_status(ctx, payload):
    """Observe only a current task/run, preserving even reader caches verbatim."""
    task_id, run_id = payload["task_id"], payload["run_id"]
    task = db.require_task(ctx.conn, task_id)
    if not run_id or task["run_id"] != run_id:
        raise util.KanpilotError("stale_run", "执行轮次已变化，请刷新任务后重新核验。")
    binding = _fingerprint(task, _BINDING)
    source = headless_adapter.resolve_source(ctx.conn)
    before = source.source_signature(task_id, run_id)
    result = {"task_id": task_id, "run_id": run_id, "backend": None,
              "checked_at": ctx.now_iso, "original_failure": _text(task["failure_reason"]),
              "checks": []}
    _unknown(result, "尚未取得可核验的执行状态。")
    continuation_turn = None
    _check(result, "binding", "当前执行轮次", "confirmed", "本次核验绑定任务当前轮次。")
    view = cmd_session._get_session_status(ctx, payload, persist=False)
    session, detail = view.get("session") or {}, view.get("round_detail") or {}
    connection = view.get("connection") or {}
    job = before["job"] if before else None
    valid = (job is not None and session.get("task_id") == task_id
             and session.get("run_id") == run_id and session.get("state") == "associated"
             and connection.get("state") != "unavailable" and not connection.get("detail")
             and detail.get("backend") == job.get("backend")
             and session.get("session_id") == job.get("session_id")
             and (not task["session_id"] or task["session_id"] == job.get("session_id")))
    if valid:
        result["backend"] = job["backend"]
        if job.get("status") in ("failed", "aborted"):
            result["original_failure"] = (_text(job.get("detail"))
                                           or _text((detail.get("terminal") or {}).get("reason"))
                                           or result["original_failure"])
        _check(result, "record", "本轮运行记录", "confirmed", "任务、执行轮次、会话身份与当前文件来源一致。")
        alive = detail.get("observer_alive")
        if (job["backend"] != "codex" and alive is False
                and not session_control._process_exited(job.get("observe_pid") or job.get("pid"))):
            alive = None
        process_label = "本机执行进程" if job["backend"] == "codex" else "本机观察进程"
        process_detail = ("执行进程仍存活。" if alive is True else "执行进程已退出。")
        process_state = "confirmed" if type(alive) is bool else "unknown"
        if job["backend"] != "codex":
            process_detail = ("记录的观察进程 PID 仍存在，归属尚未核验；执行状态见运行端实时结果。" if alive is True
                              else "观察进程已退出。")
            if alive is True:
                process_state = "unknown"
        if type(alive) is not bool:
            process_detail = "进程身份或存活状态无法确认。"
        _check(result, "process", process_label, process_state, process_detail)
        if job["backend"] == "pi_desktop":
            continuation_turn = _project_plus(result, ctx, payload, job, detail, session)
        elif job["backend"] == "codex":
            terminal = _terminal_state(job, detail)
            if job.get("status") == "running" and alive is True:
                _state(result, "running", "已核对本轮 worker/CLI 的出生身份，执行进程仍在运行。",
                       "请继续观察执行会话，避免重复启动执行。")
            elif terminal:
                _state(result, terminal, "本轮的持久结束记录与进程退出证据一致。", _TERMINAL_NOTE)
            else:
                _unknown(result, "运行文件的状态与进程活动或结束证据尚不能共同确认本轮结果。")
        else:
            _unknown(result, "该历史运行端缺少可核验的实时进程身份，当前状态仅保留为历史记录。")
    else:
        _check(result, "record", "本轮运行记录", "unknown",
               connection.get("detail") or "运行记录缺失、身份不一致或尚未完整导出。")
        _unknown(result, "无法确认当前运行记录；已有报错不能代替实时执行状态。")
    fresh = db.require_task(ctx.conn, task_id)
    after_source = headless_adapter.resolve_source(ctx.conn)
    if (_fingerprint(fresh, _BINDING) != binding
            or after_source.runs_root.resolve() != source.runs_root.resolve()):
        raise util.KanpilotError("stale_run", "执行轮次或会话绑定已变化，请刷新任务后重新核验。")
    after = after_source.source_signature(task_id, run_id)
    if before and after:
        if any(before["job"].get(key) != after["job"].get(key) for key in _JOB_BINDING):
            raise util.KanpilotError("stale_run", "执行会话身份已变化，请刷新任务后重新核验。")
        if any(before["job"].get(key) != after["job"].get(key) for key in _JOB_STATE):
            _unknown(result, "执行记录在核验期间发生变化，请重新核验当前状态。")
        elif (result["state"] in ("finished", "failed", "aborted")
              and before["signature"] != after["signature"]):
            _unknown(result, "结束证据在核验期间发生变化，请重新核验当前状态。")
    elif before:
        _unknown(result, "执行记录在核验期间变为不可读，请重新核验。")
    if result["state"] == "finished" and continuation_turn and after:
        from . import execution_continuation
        try:
            job_sha = util.sha256_file(source.run_dir(task_id, run_id) / "job.json")
            if source.source_signature(task_id, run_id) == after:
                continuation = execution_continuation.candidate(ctx.conn, fresh, job, continuation_turn, job_sha)
                if continuation is not None:
                    result["continuation"] = continuation
                    result["next_step"] = continuation["detail"]
        except OSError:
            _unknown(result, "执行记录在核验期间变为不可读，请重新核验。")
    result["checked_at"] = util.now_iso()
    return result
