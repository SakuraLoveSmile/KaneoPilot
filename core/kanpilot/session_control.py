"""Desktop-only, read-only Plus status and bounded unsent-plan recovery.

Recovery never sends a prompt: it restores the original authorized request to
the existing planner queue. A session, turn or uncertain send receipt blocks it.
"""

from __future__ import annotations

import errno
import json
import os

from . import (
    cmd_session, constants, core, db, harness_plus_adapter, headless_adapter,
    planner_inputs, runtime_client, util,
)

_PLUS_TIMEOUT = 2
_SEND_KEYS = {"session_id", "sessionId", "turn_id", "turnId", "currentTurnId", "current_turn_id"}
_ACTIVE_EVENT_TYPES = {"session", "turn_adopted", "user", "assistant_message",
                       "tool_call", "tool_result", "command_output",
                       "subagent_start", "subagent_end", "turn_end"}


def _sent_evidence(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SEND_KEYS and item:
                return True
            if key in ("send_state", "sendState") and item not in (None, "", "not_sent"):
                return True
            if key in ("sent", "accepted") and item is True:
                return True
            if (key in ("reason", "failure_code", "error_category") and isinstance(item, str)
                    and item.split(":", 1)[0] in ("send_unknown", "startup_unproven", "send_inflight")):
                return True
            if _sent_evidence(item):
                return True
    elif isinstance(value, list):
        return any(_sent_evidence(item) for item in value)
    return False


def _process_exited(pid) -> bool:
    """Only ESRCH proves absence. Live/reused PID, permission/unknown => block."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def _log_reason(directory):
    """A missing/empty startup log is unsent; malformed history is uncertain.

    Unlike the display reader this must not tolerate a truncated trailing event:
    the discarded bytes could contain the sole durable send evidence.
    """
    path = directory / "events.jsonl"
    try:
        path = util.inside(path, directory, "轮次日志")
        before = path.stat()
    except FileNotFoundError:
        return None
    except (OSError, util.KanpilotError):
        return "该请求的历史日志不可核验，不能重发"
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if (not isinstance(event, dict)
                        or not isinstance(event.get("seq"), int)
                        or isinstance(event["seq"], bool)
                        or not isinstance(event.get("type"), str)):
                    return "该请求的历史日志损坏，无法证明尚未发送"
                event_type = event["type"]
                if (event_type in _ACTIVE_EVENT_TYPES or _sent_evidence(event)
                        or event_type == "terminal" and event.get("outcome") == "completed"):
                    return "该请求的历史日志已有会话或发送活动，不能重发"
                if event_type not in ("system", "terminal"):
                    return "该请求的历史日志包含未知活动，不能安全重发"
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return "该请求的历史日志在检查期间变化，不能重发"
    except (OSError, ValueError):
        return "该请求的历史日志损坏或不可读，无法证明尚未发送"
    return None


def _context(conn, task_id, run_id):
    source = headless_adapter.resolve_source(conn)
    job, directory = cmd_session._identity_job(source, task_id, run_id)
    task = db.get_task(conn, task_id)
    request = db.get_request(conn, job.get("request_id")) if job else None
    return source, job, directory, task, request


def _capability_reason(view):
    if view.get("connected") is not True:
        return "无法连接该轮次的 Plus，请检查 Plus 是否已打开及本机控制接口"
    tools = view.get("supported_operations")
    if (view.get("available") is not True or not isinstance(tools, list)
            or not set(constants.PI_DESKTOP_PLAN_TOOLS).issubset(tools)):
        return "Plus 已连接，但缺少完整的规划控制能力"
    return None


def _authorization_reason(conn, task, request, job):
    payload = db.request_payload(request)
    if payload.get("manual_authorized") is not True or not payload.get("confirmed_at"):
        return "原规划材料尚未获得授权，请从准备规划确认材料"
    if payload.get("planner_backend") != "pi_desktop" or payload.get("planner_method", "builtin") != "builtin":
        return "该请求不是已授权的 Plus 内置规划请求"
    runtime = payload.get("runtime_config")
    if (not isinstance(runtime, dict)
            or any(not isinstance(runtime.get(key), str) or not runtime[key]
                   for key in constants.EXEC_PI_DESKTOP_KEYS)
            or runtime.get("permission_mode") not in (
                constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
                constants.PI_DESKTOP_PLANNER_PERMISSION_MODE,
            )
            or job.get("config") != runtime):
        return "该轮次的 Plus 配置快照无法核验"
    input_id = payload.get("input_id")
    manifest = payload.get("manifest")
    row = db.get_planner_input(conn, input_id) if isinstance(input_id, str) else None
    if (row is None or row["task_id"] != task["task_id"]
            or not isinstance(manifest, dict)
            or manifest.get("input_id") != input_id
            or not isinstance(manifest.get("task"), dict)
            or manifest["task"].get("task_id") != task["task_id"]
            or row["input_sha256"] != payload.get("planner_input_sha256")
            or manifest != db.planner_input_manifest(row)
            or job.get("input_id") != input_id
            or job.get("planner_input_sha256") != row["input_sha256"]):
        return "原规划材料的身份或授权指纹无法核验"
    try:
        if planner_inputs._manifest_sha(manifest) != row["input_sha256"]:
            return "原规划材料的授权指纹已变化"
    except (ValueError, TypeError):
        return "原规划材料格式无效"
    return None


def _start_reason(conn, source, job, task, request, now):
    if not job or not task:
        return "会话或任务身份无法核验"
    if job.get("backend") != "pi_desktop":
        return "该轮次不使用 Plus"
    if (request is None or request["task_id"] != task["task_id"]
            or request["run_id"] != job["run_id"]
            or request["stage"] != "plan" or request["kind"] != "plan"):
        return "该轮次已不是当前规划请求，不能从旧会话开始"
    latest = conn.execute(
        "SELECT request_id,run_id FROM attempts WHERE task_id=? AND kind='plan' "
        "ORDER BY id DESC LIMIT 1", (task["task_id"],)
    ).fetchone()
    if latest is None or latest["request_id"] != request["request_id"] or latest["run_id"] != job["run_id"]:
        return "该轮次已不是最新规划轮次"
    round_row = conn.execute(
        "SELECT save_state,backend,session_id FROM execution_rounds WHERE task_id=? AND run_id=?",
        (task["task_id"], job["run_id"]),
    ).fetchone()
    if not db.task_is_active(task) or round_row is None or round_row["save_state"] != "active":
        return "任务或会话已归档、移入回收站或移除"
    if round_row["backend"] != "pi_desktop" or round_row["session_id"]:
        return "轮次索引已有会话或后端不匹配，不能重复开始"
    if (db.task_auto_paused(task) or db.get_setting(conn, "paused_global", False)
            or "plan" in (db.get_setting(conn, "paused_stages", []) or [])):
        return "规划调度已暂停，请先解除暂停"
    other = conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND request_id<>? "
        "AND status IN ('queued','claimed') LIMIT 1",
        (task["task_id"], request["request_id"]),
    ).fetchone()
    if other:
        return "任务已有其他活跃请求，不能重复开始"
    if request["status"] not in ("claimed", "queued"):
        return "该规划请求已结束，请回到任务处理其结果"
    if request["status"] == "claimed" and not util.lease_lost(request["lease_expires_at"], now):
        return "规划执行端仍持有有效租约，正在等待启动或读取"
    if not _process_exited(job.get("pid")):
        return "原规划进程仍在运行或无法确认退出，不能重复开始"
    if any(job.get(key) for key in ("session_id", "turn_id", "send_state")):
        return "该轮次已创建会话或存在发送记录，不能重复发送"
    if task["send_state"] in ("sent", "unknown") or task["sent"]:
        return "任务存在已发送或发送结果不明的记录，不能重复发送"
    reason = _authorization_reason(conn, task, request, job)
    if reason:
        return reason
    payload = db.request_payload(request)
    original = payload.get("task_status_at_claim")
    if original not in constants.CLAIM_GUARDS["plan"]:
        return "领取前任务状态无法安全恢复"
    expected_status = original if request["status"] == "queued" else "planning"
    if task["status"] != expected_status:
        return "任务状态已变化，不能从该轮次恢复规划"
    for attempt in conn.execute("SELECT * FROM attempts WHERE request_id=?", (request["request_id"],)):
        if (attempt["send_state"] not in (None, "", "not_sent")
                or attempt["outcome"] in ("completed", "success", "succeeded", "sent", "send_unknown")
                or _sent_evidence(util.jload(attempt["evidence_json"]))):
            return "该请求的历史轮次有已发送或未知发送证据，不能重发"
        history, history_dir = cmd_session._identity_job(source, task["task_id"], attempt["run_id"])
        if (history is None or history.get("request_id") != request["request_id"]
                or history.get("backend") != "pi_desktop"):
            return "该请求的历史轮次无法核验，不能重发"
        if any(history.get(key) for key in ("session_id", "turn_id", "send_state")) or _sent_evidence(history):
            return "该请求的历史轮次有会话或发送记录，不能重发"
        if not _process_exited(history.get("pid")):
            return "该请求的历史进程仍在运行或无法确认退出"
        history_round = conn.execute(
            "SELECT session_id FROM execution_rounds WHERE task_id=? AND run_id=?",
            (task["task_id"], history["run_id"]),
        ).fetchone()
        if history_round is not None and history_round["session_id"]:
            return "该请求的历史轮次索引已有会话，不能重发"
        log_reason = _log_reason(history_dir)
        if log_reason:
            return log_reason
        scope_ids = {history.get("scope_id"), runtime_client.stable_scope_id(
            "planner", task_id=task["task_id"], run_id=history["run_id"])} - {None, ""}
        if any(conn.execute(
            "SELECT 1 FROM runtime_operations WHERE scope_id=? "
            "AND tool IN ('kp_runtime_session_create','kp_runtime_prompt') "
            "AND status<>'failed' LIMIT 1", (scope_id,),
        ).fetchone() for scope_id in scope_ids):
            return "该请求已有 Plus 控制收据，不能重复创建会话或发送"
    if _sent_evidence(util.jload(request["result_json"])):
        return "该请求存在已发送或未知发送结果，不能重发"
    for event in conn.execute("SELECT detail_json FROM events WHERE request_id=?", (request["request_id"],)):
        if _sent_evidence(util.jload(event["detail_json"])):
            return "该请求的历史事件含发送证据，不能重发"
    return None


def cmd_session_control_status(ctx, payload):
    task_id, run_id = payload["task_id"], payload["run_id"]
    source, job, _, task, request = _context(ctx.conn, task_id, run_id)
    reason = _start_reason(ctx.conn, source, job, task, request, ctx.now)
    result = {"task_id": task_id, "run_id": run_id,
              "request_id": job.get("request_id") if job else None,
              "backend": job.get("backend") if job else None,
              "state": "unavailable", "detail": "会话身份或来源不可用",
              "connected": False, "session_id": None, "turn_id": None,
              "can_start": False, "blocked_reason": reason,
              "start_label": "请求开始规划" if request is not None and request["kind"] == "plan" else "请求开始"}
    if not job or not task:
        return result
    if job.get("backend") != "pi_desktop":
        result.update(state="unsupported", detail="该轮次不使用 Plus 控制接口")
        return result
    # Status reads the immutable round connection, never today's global config.
    config = job.get("config")
    connection_file = config.get("connection_file") if isinstance(config, dict) else None
    if not isinstance(connection_file, str) or not connection_file:
        result.update(detail="该轮次没有可核验的 Plus 连接快照")
        return result
    session_id, turn_id = job.get("session_id"), job.get("turn_id")
    if not isinstance(session_id, str):
        session_id = None
    if not isinstance(turn_id, str):
        turn_id = None
    result.update(session_id=session_id, turn_id=turn_id)
    try:
        if session_id:
            view = harness_plus_adapter.plus_status(connection_file, session_id=session_id, timeout=_PLUS_TIMEOUT)
            if view.get("session_id") != session_id:
                result.update(state="disconnected", detail="Plus 返回的会话身份不匹配")
                return result
            raw = view.get("raw_status")
            inner = raw["status"] if isinstance(raw, dict) and isinstance(raw.get("status"), dict) else raw
            if not isinstance(inner, dict) or not isinstance(inner.get("isRunning"), bool):
                result.update(state="unavailable", detail="Plus 返回的运行状态不完整，无法确认当前状态")
                return result
            permission = view.get("pending_permission")
            state = ("waiting_permission" if isinstance(permission, dict) or bool(permission)
                     else "running" if view.get("is_running") else "idle")
            result.update(state=state, connected=True,
                          detail={"waiting_permission": "Plus 正在等待权限确认，请在 Plus 中处理",
                                  "running": "Plus 正在运行", "idle": "Plus 当前空闲"}[state])
            if isinstance(view.get("current_turn_id"), str):
                result["turn_id"] = view["current_turn_id"]
        else:
            view = harness_plus_adapter.plus_capabilities(connection_file, timeout=_PLUS_TIMEOUT)
            capability_reason = _capability_reason(view)
            if capability_reason:
                result.update(state="unsupported" if view.get("connected") is True else "disconnected",
                              connected=view.get("connected") is True,
                              detail=capability_reason, blocked_reason=capability_reason)
                return result
            lost = request is not None and request["status"] == "claimed" and util.lease_lost(request["lease_expires_at"], ctx.now)
            state = "executor_lost" if lost else "not_started"
            result.update(state=state, connected=True,
                          detail="规划执行端失联，尚无 Plus 会话记录" if lost else "Plus 已连接，尚未创建该轮次会话",
                          can_start=reason is None, blocked_reason=reason)
    except Exception:
        # Transport exceptions may contain tokens, URLs or permission content.
        result.update(state="disconnected", detail="无法读取该轮次的 Plus 状态，请检查 Plus 和本机控制接口")
    return result


def _verify_material_files(conn, payload):
    root = planner_inputs._inputs_root(conn).resolve()
    directory = util.inside(root / payload["input_id"], root, "规划材料")
    manifest_path = util.inside(directory / "manifest.json", directory, "规划材料清单")
    disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    if disk != payload["manifest"]:
        raise util.KanpilotError("conflict", "规划材料清单已变化，请重新准备材料")
    files = disk.get("files", [])
    if not isinstance(files, list):
        raise util.KanpilotError("conflict", "规划材料清单格式无效")
    for entry in files:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not entry["path"] or os.path.isabs(entry["path"])):
            raise util.KanpilotError("conflict", "规划材料文件路径无效")
        path = util.inside(directory / entry["path"], directory, "规划材料文件")
        if not path.is_file() or path.stat().st_size > constants.PLAN_INPUT_FILE_MAX_BYTES or util.sha256_file(path) != entry.get("sha256"):
            raise util.KanpilotError("conflict", "规划材料文件已变化，请重新准备材料")


def cmd_session_request_start(ctx, payload):
    conn = ctx.conn
    # Probe outside the write lock, then revalidate identities and every local
    # guard in the transaction. No prompt, create or configure call is made.
    source, probed_job, _, task, request = _context(conn, payload["task_id"], payload["run_id"])
    if request is None or request["request_id"] != payload["request_id"]:
        raise util.KanpilotError("stale_run", "规划请求身份已变化，请刷新会话")
    reason = _start_reason(conn, source, probed_job, task, request, util.now_dt())
    if reason:
        raise util.KanpilotError("conflict", reason)
    try:
        view = harness_plus_adapter.plus_capabilities(probed_job["config"]["connection_file"], timeout=_PLUS_TIMEOUT)
        reason = _capability_reason(view)
    except Exception:
        reason = "无法读取该轮次的 Plus 控制能力，请检查本机连接"
    if reason:
        raise util.KanpilotError("conflict", reason)
    with db.write_txn(conn):
        source, job, _, task, request = _context(conn, payload["task_id"], payload["run_id"])
        if job != probed_job:
            raise util.KanpilotError("conflict", "规划轮次在连接检查期间变化，请刷新会话")
        if request is None or request["request_id"] != payload["request_id"]:
            raise util.KanpilotError("stale_run", "规划请求身份已变化，请刷新会话")
        reason = _start_reason(conn, source, job, task, request, util.now_dt())
        if reason:
            raise util.KanpilotError("conflict", reason)
        try:
            _verify_material_files(conn, db.request_payload(request))
        except (OSError, ValueError) as exc:
            raise util.KanpilotError("conflict", "规划材料不可读取，请重新准备材料") from exc
        # Recheck the filesystem observation before committing: never queue if
        # job changed while material verification was in progress.
        _, current, _, _, _ = _context(conn, payload["task_id"], payload["run_id"])
        if current != job or not _process_exited(current.get("pid")):
            raise util.KanpilotError("conflict", "规划轮次在检查期间变化，请刷新会话")
        reason = _start_reason(conn, source, current, task, request, util.now_dt())
        if reason:
            raise util.KanpilotError("conflict", reason)
        duplicate = request["status"] == "queued"
        if not duplicate:
            now = util.now_iso()
            db.update_request(conn, request["request_id"], now, status="queued",
                              claimed_by=None, claimed_at=None, heartbeat_at=None,
                              lease_expires_at=None, not_before=None,
                              result_json=None, finished_at=None)
            # Keep payload, attempts and run_id. The retained run identifies
            # duplicate clicks; the regular claim allocates the next run_id.
            core.transition_task(conn, task, db.request_payload(request)["task_status_at_claim"], now,
                                 "desktop", detail={"via": "session-request-start", "run_id": job["run_id"]})
            db.add_event(conn, now, "session_start_requested", task_id=task["task_id"],
                         request_id=request["request_id"], actor="desktop", detail={"run_id": job["run_id"]})
        return {"request_id": request["request_id"], "stage": "plan", "queued": True,
                "deduplicated": duplicate}
