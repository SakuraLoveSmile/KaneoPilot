"""Recheck real command evidence without executing code or overriding results."""

import json
from pathlib import Path

from . import core, db, derive, dsh_events, execution_contract, harnesses, round_store, stage_evidence, util, verify_evidence


def _conflict(message):
    raise util.KanpilotError("conflict", message)


def goal_coverage_detail(coverage):
    """Explain a ready report's exact gaps without changing its verdict."""
    groups = {}
    failures = []
    for row in coverage.get("rows") or []:
        command = util.redact(str(row.get("command") or ""))
        reason = row.get("unconfirmed_reason")
        if reason:
            groups.setdefault(reason, []).append(command)
        elif type(row.get("exit_code")) is int and row["exit_code"] != 0:
            failures.append(command + "（退出码 " + str(row["exit_code"]) + "）")
    parts = []
    absent = groups.get("missing_record") or []
    if absent:
        parts.append("缺少 " + str(len(absent)) + " 项检查记录：" + "；".join(absent))
    for reason, label in (("missing_exit_code", "以下检查缺少整数退出码"),
                          ("invalid_exit_code", "以下检查退出码类型无效"),
                          ("duplicate_records", "以下命令有重复检查记录"),
                          ("source_mismatch", "以下检查源码身份不符"),
                          ("unknown_provenance", "以下检查来源不可确认"),
                          ("missing_result", "以下检查缺少结果"),
                          ("unknown_result", "以下检查结果无效"),
                          ("contradictory_result", "以下检查状态与退出码不一致")):
        if groups.get(reason):
            parts.append(label + "：" + "；".join(groups[reason]))
    if failures:
        parts.append("验证命令执行失败：" + "；".join(failures))
    return ("Plus 报告已就绪，但" + "；".join(parts or ["冻结验证命令尚未全部确认通过"])
            + "。需由 Plus 在新的执行轮次补齐验证报告。")


def candidate(conn, task):
    """Cheap UI eligibility; the command independently requires all evidence."""
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    return bool(db.task_is_active(task) and task["status"] == "needs_user"
                and task["stage"] == "monitor"
                and task["failure_code"] in ("verification_unconfirmed", "verification_environment_failure",
                                             "verification_identity_unconfirmed")
                and task["sent"] == 1 and task["send_state"] == "sent"
                and task["plan_path"] and task["plan_sha256"]
                and row is not None and row["save_state"] == "active"
                and row["backend"] == "pi_desktop"
                and not conn.execute("SELECT 1 FROM requests WHERE task_id=? "
                                     "AND status IN ('queued','claimed') LIMIT 1",
                                     (task["task_id"],)).fetchone())


def request_candidate(conn, task, request):
    """Only the failed monitor that produced the current block gets an entry."""
    if (task is None or request["kind"] != "monitor" or request["status"] != "failed"
            or request["task_id"] != task["task_id"] or not candidate(conn, task)
            or not request["finished_at"] or request["finished_at"] != task["blocked_at"]):
        return False
    latest = conn.execute("SELECT request_id FROM requests WHERE task_id=? AND kind='monitor' "
                          "ORDER BY created_at DESC, rowid DESC LIMIT 1", (task["task_id"],)).fetchone()
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    claimed = util.parse_ts(request["claimed_at"])
    started = util.parse_ts(row["started_at"] or row["created_at"])
    reported = conn.execute("SELECT detail_json FROM events WHERE task_id=? AND request_id=? "
                            "AND kind='reported' AND at=? ORDER BY id DESC LIMIT 1",
                            (task["task_id"], request["request_id"], request["finished_at"])).fetchone()
    # Monitor run_id identifies its claim, not the execution round. The claim
    # must postdate this execution's start, so an old failure cannot acquire an
    # entry when a newer execution becomes the task's current round.
    return bool(latest and latest["request_id"] == request["request_id"]
                and claimed is not None and started is not None and claimed >= started
                and reported and (util.jload(reported["detail_json"]) or {}).get("outcome") == "needs_user")


def _snapshot(conn, task_id, run_id):
    row = round_store.get_round(conn, task_id, run_id)
    return {"task": dict(db.require_task(conn, task_id)),
            "round": dict(row) if row is not None else None,
            "requests": [dict(r) for r in conn.execute(
                "SELECT * FROM requests WHERE task_id=? ORDER BY rowid", (task_id,))],
            "events": [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))]}


def _read_run_evidence(conn, task):
    """Trusted local run identity and frozen plan; transcript tool results are not matched here."""
    from . import cmd_session, headless_adapter, planfile
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    job, directory = cmd_session._identity_job(
        headless_adapter.resolve_source(conn), task["task_id"], task["run_id"])
    if row is None or job is None:
        _conflict("当前轮次缺少可信本机执行记录")
    hashes = {}

    def read(path):
        try:
            # A symlink cannot substitute evidence from outside this run.
            path = util.inside(path, directory, "verification evidence")
            raw = path.read_bytes()
        except (OSError, util.KanpilotError) as exc:
            raise util.KanpilotError("conflict", "本机验证证据不可读或已越界") from exc
        hashes[str(path)] = util.sha256_bytes(raw)
        return raw

    try:
        job = json.loads(read(directory / "job.json"))
        spec = json.loads(read(directory / "spec.json"))
    except (ValueError, UnicodeError) as exc:
        raise util.KanpilotError("conflict", "执行记录或固化配置解析失败") from exc
    if not isinstance(job, dict) or not isinstance(spec, dict):
        _conflict("执行记录或固化配置不是对象")
    contract = execution_contract.validate_spec(spec, job=job)
    if contract is not None:
        util.require(contract["inputs"]["requirements_sha256"] == task["requirements_sha256"],
                     "执行阶段契约的需求身份已变化", "verification_identity_unconfirmed")
        request = conn.execute("SELECT task_id,run_id,stage FROM requests WHERE request_id=?",
                               (contract["request_id"],)).fetchone()
        util.require(request is not None and request["task_id"] == task["task_id"]
                     and request["run_id"] == task["run_id"] and request["stage"] == "dispatch",
                     "执行阶段契约的领取身份不可确认", "verification_identity_unconfirmed")
    if (any(record.get("task_id") != task["task_id"]
            or record.get("run_id") != task["run_id"]
            or record.get("backend") != row["backend"]
            or record.get("plan_sha256") != task["plan_sha256"]
            or record.get("completion_policy") != "command_verification"
            for record in (job, spec))
            or spec.get("plan_path") != task["plan_path"]
            or spec.get("repository") != task["repository"]
            or not row["worktree_path"] or spec.get("cwd") != row["worktree_path"]
            or spec.get("runs_dir") != str(directory)
            or spec.get("config") != util.jload(row["config_json"])
            or job.get("config") != spec.get("config")):
        _conflict("本机记录、固化执行配置或当前计划身份不符")
    session_id = job.get("session_id")
    if (not isinstance(session_id, str) or not session_id.strip()
            or session_id != row["session_id"] or session_id != task["session_id"]
            or not isinstance(job.get("turn_id"), str) or not job["turn_id"].strip()
            or job.get("send_state") != "sent"):
        _conflict("当前执行会话或发送身份不完整")
    if row["backend"] == "codex":
        from . import codex_execution
        if job.get("pid") != row["pid"] or job.get("observe_pid") not in (None, job.get("pid")):
            _conflict("Codex worker 进程登记与持久记录不符")
        process_alive = codex_execution.job_alive(job)
    else:
        process_alive = any(util.pid_alive(pid) for pid in
                            (job.get("observe_pid"), job.get("pid"), row["pid"]))
    if (job.get("status") != "finished"
            or type(job.get("exit_code")) is not int or job["exit_code"] != 0
            or process_alive):
        _conflict("当前轮次尚无 finished/0 终态或观察进程仍存活")
    raw_events = read(directory / dsh_events.EVENT_LOG_NAME)
    try:
        # The display reader tolerates partial tails; a recovery decision cannot.
        events = [json.loads(line) for line in raw_events.decode("utf-8").splitlines()
                  if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise util.KanpilotError("conflict", "执行事件不完整或解析失败") from exc
    if (not events or any(not isinstance(event, dict)
            or type(event.get("seq")) is not int for event in events)
            or events[0]["seq"] != 1
            or any(right["seq"] != left["seq"] + 1 for left, right in zip(events, events[1:]))):
        _conflict("执行事件缺失或序列身份不完整")
    session = dsh_events.session_event(events)
    terminal = dsh_events.terminal_event(events)
    if (not session or session.get("session_id") != session_id
            or not terminal or terminal.get("outcome") != "completed"
            or events[-1].get("type") != "terminal"
            or any(event.get("task_id") not in (None, task["task_id"])
                   or event.get("run_id") not in (None, task["run_id"])
                   or event.get("session_id") not in (None, session_id)
                   for event in events)):
        _conflict("执行事件的任务、轮次、会话身份或 completed 终态不符")
    try:
        meta, raw, path = planfile.load_plan(task["plan_path"], task["task_id"])
    except (util.KanpilotError, OSError, ValueError) as exc:
        raise util.KanpilotError("conflict", "当前计划文件不可核验") from exc
    if (util.sha256_bytes(raw) != task["plan_sha256"]
            or any(meta.get(key) != task[key]
                   for key in ("project_id", "repository", "requirements_sha256"))):
        _conflict("计划文件、项目或需求身份已变化")
    hashes[str(path)] = util.sha256_bytes(raw)
    declaration = db.task_extra(task).get("plan_verify") or {}
    parsed = verify_evidence.parse_verify_block(raw.decode("utf-8"))
    if (declaration.get("plan_sha256") != task["plan_sha256"]
            or not parsed["ok"] or declaration.get("commands") != parsed.get("commands")):
        _conflict("当前计划缺少一致的固化 verify 命令声明")
    _require_unchanged(hashes)
    return hashes, job, events, declaration


def _require_unchanged(hashes):
    try:
        if any(util.sha256_file(Path(path)) != digest for path, digest in hashes.items()):
            _conflict("读取期间本机执行证据或计划文件已变化")
    except OSError as exc:
        raise util.KanpilotError("conflict", "读取期间本机执行证据或计划文件已不可读") from exc


def _read_evidence(conn, task, *, require_pass=True):
    """Transcript-matched evidence: legacy and shadow audit only. Live verdicts read the Goal report."""
    hashes, job, events, declaration = _read_run_evidence(conn, task)
    # Only Plus tool results from this execution window can establish a pass.
    # Historical core-run receipts and manual overrides are audit data only.
    messages = dsh_events.to_messages(events)
    if job.get("verification_evidence_version") == 1:
        result = verify_evidence.match_execution_verification(
            declaration["commands"], messages, job["turn_id"])
    else:
        selected, window = verify_evidence.execution_window(messages, job["turn_id"])
        result = verify_evidence.match_verification(
            declaration["commands"], verify_evidence.extract_command_results(selected))
        result.update(window)
        if window.get("window_basis") == "unscoped_goal_window":
            result.update(ok=False, missing=list(declaration["commands"]))
    if require_pass and not result["ok"]:
        _conflict("本轮 Plus 验证命令仍缺失、失败或结果未知；请让 Plus 重新执行并验证，未恢复推进")
    _require_unchanged(hashes)
    return hashes, job, result


def read_goal_evidence(conn, task):
    """Plus's structured Goal report for this round, checked against the frozen commands.

    Transcript tool results are never read. Run identity, terminal state and file
    hashes come from the trusted local run; ``cmd_read.read_goal_report`` checks
    the report's own identity.
    """
    from . import cmd_read
    hashes, job, _events, declaration = _read_run_evidence(conn, task)
    _require_live_terminal(job)
    report = cmd_read.read_goal_report(conn, task["task_id"], task["run_id"])
    if report.get("state") != "ready":
        raise util.KanpilotError(
            "verification_unconfirmed",
            "Plus 结构化报告未就绪（" + str(report.get("state")) + "）："
            + util.redact(str(report.get("detail") or "")))
    # The round's execution turn and the report's turn must both belong to the approved chain.
    approved = {str(item) for item in (report.get("identity") or {}).get("approved_turns") or ()}
    if (str(job["turn_id"]) not in approved or not report.get("turn_id")
            or str(report["turn_id"]) not in approved):
        _conflict("Goal 报告的执行轮次不属于本轮已批准的执行轮次")
    commands = declaration["commands"]
    nonmet = report.get("verdict") != "met"
    if job.get("stage_contract_required") is True or "stage_contract" in job or nonmet:
        contract = job.get("stage_contract")
        adapter = harnesses.get_runtime_adapter(contract["harness_id"] if contract else "pi_desktop")
        coverage = stage_evidence.match_checks(
            contract["verification_commands"] if contract else commands, adapter.verification_checks(
                report, source_sha256=job.get("verification_source_sha256")),
            expected_source_sha256=job.get("verification_source_sha256"))
    else:
        coverage = verify_evidence.match_goal_checks(commands, (report.get("report") or {}).get("checks"))
    # A truthful partial/blocked report with complete, consistent command
    # failures is failure evidence for the quality/environment ladder. It
    # can never satisfy coverage.ok, so recovery and closeout still reject it.
    # Missing or contradictory records remain unconfirmed even if another
    # command failed; met retains the existing completion floor unchanged.
    floor = cmd_read.goal_report_floor_reason(report)
    failed_report = (report.get("structured") is True
                     and report.get("execution_status") == "completed"
                     and report.get("verdict") in ("partial", "blocked")
                     and not coverage["missing"] and bool(coverage["failed"]))
    if floor and not failed_report:
        raise util.KanpilotError("verification_unconfirmed", util.redact(floor))
    _require_unchanged(hashes)
    return {"hashes": hashes, "job": job, "report": report, "commands": commands, "coverage": coverage}


def _require_live_terminal(job):
    from . import harness_plus_adapter
    connection = (job.get("config") or {}).get("connection_file")
    if not connection:
        _conflict("当前 Plus 轮次缺少固化连接配置")
    def read_status():
        status = harness_plus_adapter.plus_status(connection, session_id=job["session_id"], timeout=2)
        planning = (status.get("raw_status") or {}).get("planningState")
        if (status.get("session_id") != job["session_id"]
                or status.get("is_running") is not False or status.get("pending_permission")
                or planning in ("planning", "awaiting_approval")
                or status.get("current_turn_id") not in (None, job["turn_id"])):
            _conflict("Plus 会话仍在运行、等待授权或已有后续轮次，未恢复推进")
        return (status.get("session_id"), status.get("current_turn_id"), planning)

    before = read_status()
    turn = harness_plus_adapter.plus_turn_get(connection, session_id=job["session_id"],
        turn_id=job["turn_id"], timeout=2, require_identity=True)
    if (turn.get("session_id") != job["session_id"]
            or turn.get("turn_id") != job["turn_id"] or turn.get("status") != "completed"):
        _conflict("Plus 当前执行轮次缺少可信 completed 终态")
    # A manual continuation can start while the old persistent turn is read.
    # Keep these network reads outside SQLite's write transaction. The Plus
    # host has no cross-system CAS, so this narrows rather than removes that gap.
    if read_status() != before:
        _conflict("核验期间 Plus 会话或当前轮次已变化，未恢复推进")


def _deduplicated(conn, task, run_id):
    event = conn.execute("SELECT detail_json FROM events WHERE task_id=? "
                         "AND kind='verification_reverified' ORDER BY id DESC LIMIT 1",
                         (task["task_id"],)).fetchone()
    detail = util.jload(event["detail_json"]) if event else None
    if not detail or detail.get("execution_run_id") != run_id:
        return None
    if task["run_id"] != run_id or task["plan_sha256"] != detail.get("plan_sha256"):
        _conflict("当前执行轮次或计划已变化，不能恢复旧验证")
    # A later block must be freshly verified, never masked by an earlier success.
    if task["status"] == "needs_user":
        return None
    request = db.get_request(conn, detail.get("monitor_request_id"))
    return {"ok": True, "deduplicated": True, "task_status": task["status"],
            "request": derive.request_view(request, util.now_dt()) if request is not None else None}


def recover(ctx, payload):
    run_id = payload.get("expected_execution_run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise util.bad_request("reverify 必须指定期望的当前执行轮次")
    conn, task_id = ctx.conn, payload["task_id"]
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        if not db.task_is_active(task) or task["run_id"] != run_id:
            _conflict("任务不在活跃列表或当前执行轮次已变化")
        repeated = _deduplicated(conn, task, run_id)
        if repeated:
            return repeated
        if not candidate(conn, task):
            _conflict("仅当前待确认验证且没有活动请求的执行轮次可重新核验")
        snapshot = _snapshot(conn, task_id, run_id)
    evidence = read_goal_evidence(conn, task)
    hashes, coverage = evidence["hashes"], evidence["coverage"]
    if not coverage["ok"]:
        _conflict(goal_coverage_detail(coverage))
    # Receipt validation also reads Git and process state. Keep those reads
    # outside the SQLite write transaction, as with the live Plus checks.
    again = read_goal_evidence(conn, db.require_task(conn, task_id))
    if (again["hashes"] != hashes
            or again["report"]["report_sha256"] != evidence["report"]["report_sha256"]):
        _conflict("核验期间本机执行证据、计划文件或 Goal 报告已变化")
    with db.write_txn(conn):
        current = db.require_task(conn, task_id)
        if not db.task_is_active(current):
            _conflict("核验期间任务已移出活跃列表")
        repeated = _deduplicated(conn, current, run_id)
        if repeated:
            return repeated
        if _snapshot(conn, task_id, run_id) != snapshot or not candidate(conn, current):
            _conflict("核验期间任务、轮次或请求已变化，请刷新后重试")
        try:
            if any(util.sha256_file(Path(path)) != digest for path, digest in hashes.items()):
                _conflict("核验期间本机执行证据或计划文件已变化")
        except OSError as exc:
            raise util.KanpilotError("conflict", "核验期间本机执行证据或计划文件已不可读") from exc
        resolution = {"action": "reverify", "actor": "user", "at": ctx.now_iso,
                      "note": payload.get("note"), "execution_run_id": run_id}
        previous = {key: current[key] for key in
                    ("failure_code", "failure_reason", "failure_stage", "blocked_at")}
        core.transition_task(conn, current, "running", ctx.now_iso, "user",
            detail={"via": "resolve reverify", "execution_run_id": run_id},
            **core.clear_block_fields(), resolution=resolution)
        from . import cmd_queue
        request, _ = cmd_queue._new_cycle_request(conn, ctx.now_iso, current, "monitor")
        db.add_event(conn, ctx.now_iso, "verification_reverified", task_id=task_id,
            request_id=request["request_id"], actor="user",
            detail={"execution_run_id": run_id, "plan_sha256": current["plan_sha256"],
                    "monitor_request_id": request["request_id"], "matched": coverage["matched"],
                    "evidence_sha256": hashes, "report_sha256": evidence["report"]["report_sha256"],
                    "previous": previous})
        db.add_event(conn, ctx.now_iso, "resolved", task_id=task_id, actor="user", detail=resolution)
        return {"ok": True, "deduplicated": False, "task_status": "running",
                "request": derive.request_view(request, ctx.now)}
