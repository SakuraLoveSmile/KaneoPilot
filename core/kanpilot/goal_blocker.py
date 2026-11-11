"""User decisions for a durable Pi Goal hold; acceptance is never verification."""
from __future__ import annotations

from pathlib import Path

from . import (cmd_read, core, db, dsh_events, git_delivery, round_store,
               runtime_client, runtime_operations, util)

KEY = "goal_acceptance"
PENDING_KEY = "goal_decision_pending"
TOOL = "resolve-goal-blocker"
_REASON = {
    "report_missing": "未提交目标报告", "report_invalid": "目标报告尚不完整",
    "verdict_partial": "部分验收项尚未完成", "verdict_blocked": "目标被条件或材料阻塞",
    "verdict_unknown": "目标验收结果尚不能确认", "team_unsettled": "协作任务尚未收尾",
    "resume_failed": "上次继续执行未能启动",
}


def _identity(conn, task_id, run_id):
    task = db.require_task(conn, task_id)
    if task["run_id"] != run_id:
        raise util.KanpilotError("conflict", "任务已切换执行轮次，请刷新后重试")
    identity = cmd_read.goal_round_identity(conn, task_id, run_id)
    return task, identity


def _client(identity, *, read_only):
    row = identity["round"]
    config = util.jload(row["config_json"]) or {}
    client = runtime_client.RuntimeClient(
        role="execution", connection_file=identity["connection_file"],
        project_path=row["worktree_path"], runtime_config=config,
        task_id=identity["task"]["task_id"], run_id=row["run_id"],
        read_only=read_only, timeout=20)
    client.connect(required_tools=())
    return client


def _candidate(identity, client):
    """Old hosts expose holds on report summaries; bind them to the approved chain."""
    listing = client.goal_report_list(identity["session_id"])
    matches = [r for r in listing.get("reports") or [] if isinstance(r, dict)
               and r.get("proposalId") == identity["proposal_id"]
               and r.get("turnId") in identity["approved_turns"]
               and (not identity["execution_id"] or r.get("executionId") == identity["execution_id"])]
    if len(matches) > 1:
        raise util.KanpilotError("conflict", "同一批准目标有多份执行记录，不能猜测处理对象")
    if not matches:
        return None, None, False
    summary = matches[0]
    eid = summary.get("executionId")
    if not isinstance(eid, str) or not eid:
        raise util.KanpilotError("conflict", "目标记录缺少 executionId")
    supported = {"pi_goal_execution_get", "pi_goal_resume", "pi_goal_close"}.issubset(client.tools_list())
    if supported:
        result = client.goal_execution_get(identity["session_id"], execution_id=eid)
        execution, hold = result.get("execution") or {}, result.get("hold")
        if (execution.get("id") != eid or execution.get("sessionId") != identity["session_id"]
                or execution.get("proposalId") != identity["proposal_id"]):
            raise util.KanpilotError("conflict", "Pi 目标身份与本轮批准记录不符")
    else:
        execution, hold = summary, summary.get("hold")
    if hold is not None:
        if (not isinstance(hold, dict) or hold.get("executionId") != eid
                or hold.get("sessionId") != identity["session_id"]
                or type(hold.get("version")) is not int
                or hold.get("firstTurnId") not in identity["approved_turns"]):
            raise util.KanpilotError("conflict", "Pi 受阻目标身份或版本与本轮不符")
    return execution, hold, supported


def _strings(value):
    return [util.redact(s) for s in value if isinstance(s, str)] if isinstance(value, list) else []


def _view(task_id, run_id, execution=None, hold=None, supported=False):
    view = {"task_id": task_id, "run_id": run_id, "state": "none", "reasons": [],
            "progress": [], "limitations": [], "next_steps": [], "actions": []}
    if not hold:
        return view
    state = hold.get("state")
    view.update(state="blocked" if state == "blocked" else "accepted" if state == "accepted" else "none",
                session_id=hold["sessionId"], execution_id=hold["executionId"], version=hold["version"],
                title=util.redact(str((execution or {}).get("title") or "执行目标")))
    for reason in hold.get("reasons") or []:
        if not isinstance(reason, dict):
            continue
        code = str(reason.get("code") or "")
        view["reasons"].append(_REASON.get(code, "执行端出错" if code.startswith("agent_error:") else "目标需要人工处理"))
        for item in reason.get("tasks") or []:
            if isinstance(item, dict):
                view["reasons"].append(util.redact(str(item.get("title") or "协作任务") + " · " + str(item.get("status") or "未确认")))
    for item in (hold.get("progress") or {}).get("items") or []:
        if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("id", "label", "status")):
            view["progress"].append({k: util.redact(item[k]) for k in ("id", "label", "status")})
    draft = hold.get("draft") or {}
    view["limitations"] = _strings(draft.get("limitations"))
    for criterion in draft.get("criteria") or []:
        if isinstance(criterion, dict) and criterion.get("verdict") != "met":
            text = criterion.get("text") or criterion.get("id") or "未完成验收项"
            explanation = criterion.get("explanation")
            view["limitations"].append(util.redact(str(text) + ("：" + str(explanation) if explanation else "")))
    view["next_steps"] = _strings(draft.get("nextSteps"))
    if state == "blocked" and supported:
        view["actions"] = ["continue", "accept"]
    elif state == "blocked":
        view["detail"] = "当前 Pi 可显示受阻原因；操作接口尚未就绪，请升级 Pi 或打开原会话处理"
    elif state == "accepted":
        view["detail"] = "用户已接受当前结果（未验证）；仍须独立评审通过才能交付"
    return view


def cmd_get_goal_blocker(ctx, payload):
    base = _view(payload["task_id"], payload["run_id"])
    try:
        task, identity = _identity(ctx.conn, payload["task_id"], payload["run_id"])
        execution, hold, supported = _candidate(identity, _client(identity, read_only=True))
        view = _view(task["task_id"], task["run_id"], execution, hold, supported)
        if not db.task_is_active(task) or task["status"] != "needs_user":
            view["actions"] = []
        pending = db.task_extra(task).get(PENDING_KEY) or {}
        if (pending.get("request") or {}).get("run_id") == task["run_id"] and db.task_is_active(task):
            view["actions"] = ["reconcile"]
            view["detail"] = "目标决定已发出，Pilot 尚待核对并接续；核对不会重新发送决定或启动第二轮"
        elif view["state"] == "accepted" and not db.task_extra(task).get(KEY):
            view["detail"] = "Pi 已接受现状（未验证）；Pilot 尚无本轮人工接受回执，未据此进入评审"
        return view
    except (util.KanpilotError, OSError, ValueError, TypeError) as exc:
        return dict(base, state="unavailable", detail=util.redact(getattr(exc, "message", str(exc))))


def _action_request(task_id, run_id, session_id, execution_id, version, action, message=None):
    return {"task_id": task_id, "run_id": run_id, "session_id": session_id,
            "execution_id": execution_id, "expected_version": version, "action": action,
            "message": message or ""}


def authorize_runtime_goal_action(conn, scope, args):
    """An execution token alone cannot accept a goal or initiate another paid turn."""
    if scope.role != "execution" or not scope.task_id or not scope.run_id:
        raise util.KanpilotError("forbidden", "目标决定需要绑定的执行作用域")
    task = db.require_task(conn, scope.task_id)
    op = runtime_operations.get_operation(conn, scope.scope_id, args["authorization_operation_id"])
    request = _action_request(scope.task_id, scope.run_id, args["session_id"],
        args["execution_id"], args["expected_version"], args["action"], args.get("message"))
    pending = db.task_extra(task).get(PENDING_KEY) or {}
    if (task["run_id"] != scope.run_id or task["status"] != "needs_user" or not db.task_is_active(task)
            or pending.get("operation_id") != args["authorization_operation_id"]
            or pending.get("request") != request or pending.get("task_snapshot") != _task_snapshot(task)
            or op is None or op["tool"] != TOOL or op["status"] != "unknown"
            or op["request_sha256"] != runtime_operations.compute_request_sha256(request)):
        raise util.KanpilotError("forbidden", "没有匹配当前目标和版本的用户决定回执")


def _require_idle_turn(identity):
    from . import harness_plus_adapter
    connection, job = identity["connection_file"], identity["job"]
    def status():
        view = harness_plus_adapter.plus_status(connection, session_id=identity["session_id"], timeout=2)
        if (view.get("session_id") != identity["session_id"] or view.get("is_running") is not False
                or view.get("pending_permission") or view.get("current_turn_id") not in (None, job["turn_id"])
                or (view.get("raw_status") or {}).get("planningState") in ("planning", "awaiting_approval")):
            raise util.KanpilotError("conflict", "Pi 会话仍在运行、等待授权或已有其它轮次")
        return (view.get("session_id"), view.get("current_turn_id"))
    before = status()
    turn = harness_plus_adapter.plus_turn_get(connection, session_id=identity["session_id"],
        turn_id=job["turn_id"], timeout=2, require_identity=True)
    if (turn.get("session_id") != identity["session_id"] or turn.get("turn_id") != job["turn_id"]
            or turn.get("status") not in ("completed", "error", "aborted")):
        raise util.KanpilotError("conflict", "Pi 当前轮次缺少持久终态")
    if status() != before:
        raise util.KanpilotError("conflict", "核对期间 Pi 当前轮次已变化")


def _accepted_report(conn, task, identity, client, execution_id):
    execution, hold, supported = _candidate(identity, client)
    if (not supported or not hold or hold.get("executionId") != execution_id
            or hold.get("state") != "accepted"):
        raise util.KanpilotError("conflict", "Pi 未确认同一目标已由用户接受")
    report = cmd_read.read_goal_report(conn, task["task_id"], task["run_id"], client=client)
    body = report.get("report") or {}
    if (report.get("state") != "ready" or report.get("execution_id") != execution_id
            or not report.get("report_sha256") or body.get("closure") != "user_accepted"):
        raise util.KanpilotError("conflict", "缺少同一目标的用户接受报告，未授权进入评审")
    return report, hold


def _binding(conn, task, **kwargs):
    from .engine import verdict
    return verdict.binding(conn, task, **kwargs)


def acceptance_current(conn, task, receipt=None, *, deadline=None, heartbeat=None):
    """Recheck user authorization, source bytes and live Pi closure; never verifies checks."""
    saved = db.task_extra(task).get(KEY) or {}
    if (not saved or saved.get("execution_run_id") != task["run_id"]
            or saved.get("plan_sha256") != task["plan_sha256"]
            or saved.get("verification_ok") is not False or saved.get("review_authorized") is not True
            or any(saved.get(key) != task[key] for key in ("requirements_sha256", "repository", "source_sha256"))):
        return False
    if receipt is not None and any(receipt.get(k) != saved.get(k) for k in
            ("acceptance_sha256", "binding", "report_sha256", "index_source_sha256", "execution_run_id", "plan_sha256")):
        return False
    try:
        _, identity = _identity(conn, task["task_id"], task["run_id"])
        if identity["session_id"] != saved.get("session_id") or identity["proposal_id"] != saved.get("proposal_id"):
            return False
        frozen = saved["binding"]
        actual = _binding(conn, task, allow_core_commit=True, deadline=deadline, heartbeat=heartbeat)
        if dict(actual, head_sha=frozen["head_sha"]) != frozen:
            return False
        _require_idle_turn(identity)
        report, hold = _accepted_report(conn, task, identity, _client(identity, read_only=True), saved["execution_id"])
        if hold.get("version") != saved.get("accepted_version") or report["report_sha256"] != saved["report_sha256"]:
            return False
        _require_idle_turn(identity)
        if not saved.get("evidence_sha256") or any(not Path(path).is_file() or util.sha256_file(Path(path)) != digest for path, digest in saved["evidence_sha256"].items()):
            return False
        return dict(_binding(conn, db.require_task(conn, task["task_id"]), allow_core_commit=True,
            deadline=deadline, heartbeat=heartbeat), head_sha=frozen["head_sha"]) == frozen
    except (util.KanpilotError, OSError, ValueError, TypeError, KeyError, git_delivery.GitDeliveryError):
        return False


def review_admission_current(conn, task, receipt, **kwargs):
    """Two explicit admissions: passed verification or a current human acceptance."""
    if receipt.get("admission_source") == "user_acceptance":
        return (receipt.get("verification_ok") is False and receipt.get("review_authorized") is True
                and acceptance_current(conn, task, receipt, **kwargs))
    from .engine import verdict
    return receipt.get("verification_ok") is True and verdict.receipt_evidence_current(conn, task, receipt, **kwargs)



def _task_snapshot(task):
    extra = db.task_extra(task)
    extra.pop(PENDING_KEY, None)
    return {"fields": {key: task[key] for key in ("status", "run_id", "plan_sha256", "requirements_sha256", "repository", "session_id")},
            "extra_sha256": util.sha256_text(util.jdump(extra))}


def _run_hashes(identity):
    paths = [identity["run_dir"] / name for name in ("job.json", "spec.json", "events.jsonl")]
    paths.append(Path(identity["task"]["plan_path"]))
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        raise util.KanpilotError("conflict", "执行材料不完整，不能确认目标决定")
    return {str(path): util.sha256_file(path) for path in paths}


def _finish_operation(ctx, scope, pending, response):
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, pending["request"]["task_id"])
        extra = db.task_extra(fresh)
        if (extra.get(PENDING_KEY) or {}).get("operation_id") == pending["operation_id"]:
            extra.pop(PENDING_KEY, None)
            db.update_task(ctx.conn, fresh["task_id"], ctx.now_iso, extra_json=extra)
        runtime_operations.complete_operation(ctx.conn, scope, pending["operation_id"], result=response,
            session_id=pending["request"]["session_id"], turn_id=response.get("turn_id"), now=ctx.now_iso)
    return response


def _settle_acceptance(ctx, task, identity, pending, client):
    request = pending["request"]
    _require_idle_turn(identity)
    report, accepted_hold = _accepted_report(ctx.conn, task, identity, client, request["execution_id"])
    if (accepted_hold["version"] != request["expected_version"] + 1
            or _binding(ctx.conn, task) != pending["binding"] or _run_hashes(identity) != pending["evidence_sha256"]):
        raise util.KanpilotError("conflict", "目标版本、执行证据或源码已变化，不能接续接受决定")
    acceptance = {"task_id": task["task_id"], "execution_run_id": task["run_id"],
        "session_id": identity["session_id"], "proposal_id": identity["proposal_id"],
        "execution_id": request["execution_id"], "plan_sha256": task["plan_sha256"],
        "requirements_sha256": task["requirements_sha256"], "repository": task["repository"],
        "source_sha256": task["source_sha256"],
        "expected_version": request["expected_version"], "accepted_version": accepted_hold["version"],
        "accepted_at": ctx.now_iso, "actor": "user", "operation_id": pending["operation_id"],
        "binding": pending["binding"], "index_source_sha256": pending["index_source_sha256"],
        "evidence_sha256": pending["evidence_sha256"],
        "report_sha256": report["report_sha256"], "report_id": report.get("report_id"),
        "execution_turn_id": report.get("turn_id"), "evidence_source": "goal_report",
        "admission_source": "user_acceptance", "review_authorized": True,
        "verification_ok": False, "empty_diff": False, "kind": "user_accepted",
        "environment_failure": False, "checks": [],
        "detail": "用户接受当前结果（未验证），需独立评审；原始失败与缺项保留",
        "blocked_snapshot": pending["blocked_snapshot"], "accepted_report": report.get("report")}
    acceptance["acceptance_sha256"] = util.sha256_text(util.jdump(acceptance))
    _require_idle_turn(identity)
    if _binding(ctx.conn, db.require_task(ctx.conn, task["task_id"])) != pending["binding"]:
        raise util.KanpilotError("conflict", "核对期间源码已变化，未进入评审")
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task["task_id"])
        if _task_snapshot(fresh) != pending["task_snapshot"]:
            raise util.KanpilotError("conflict", "操作期间任务已变化，未进入评审")
        db.set_extra(ctx.conn, fresh, ctx.now_iso, **{KEY: acceptance})
        core.transition_task(ctx.conn, db.require_task(ctx.conn, task["task_id"]), "running", ctx.now_iso, "user",
            detail={"via": TOOL, "action": "accept", "execution_id": request["execution_id"]}, **core.clear_block_fields())
        db.create_request(ctx.conn, ctx.now_iso, "monitor", "goal-acceptance:" + pending["operation_id"],
            task_id=task["task_id"], payload={"goal_acceptance": acceptance["acceptance_sha256"]}, requested_by="user")
        db.add_event(ctx.conn, ctx.now_iso, "goal_accepted", task_id=task["task_id"], actor="user",
            detail={"execution_run_id": task["run_id"], "execution_id": request["execution_id"],
                    "verification_ok": False, "acceptance_sha256": acceptance["acceptance_sha256"]})
    return {"task_id": task["task_id"], "run_id": task["run_id"], "session_id": identity["session_id"], "status": "accepted"}


def cmd_resolve_goal_blocker(ctx, payload):
    from . import cmd_request_continuation as continuation
    if payload.get("confirm") is not True or payload.get("action") not in ("continue", "accept"):
        raise util.bad_request("请选择继续或接受现状，并明确确认")
    if type(payload["expected_version"]) is not int or payload["expected_version"] < 1:
        raise util.bad_request("目标版本无效")
    message = payload.get("message") or ""
    if len(message.encode("utf-8")) > 65536:
        raise util.bad_request("补充材料超过 64 KiB")
    conn = ctx.conn
    task, identity = _identity(conn, payload["task_id"], payload["run_id"])
    scope = runtime_client.stable_scope_id("execution", task_id=task["task_id"], run_id=task["run_id"])
    op_id = payload.get("operation_id") or "goal:%s:%s:%s" % (payload["action"], payload["execution_id"], payload["expected_version"])
    request = _action_request(task["task_id"], task["run_id"], identity["session_id"],
        payload["execution_id"], payload["expected_version"], payload["action"], message)
    prior = runtime_operations.get_operation(conn, scope, op_id)
    if prior:
        if prior["request_sha256"] != runtime_operations.compute_request_sha256(request):
            raise util.KanpilotError("conflict", "操作编号已用于不同决定")
        if prior["status"] == "accepted":
            return dict(util.jload(prior["result_json"]), deduplicated=True)
        raise util.KanpilotError("goal_decision_unconfirmed", "上次目标决定结果未确认，请核对并接续；不会重复发送")
    if (not db.task_is_active(task) or task["status"] != "needs_user"
            or task["failure_code"] not in ("verification_unconfirmed", "verification_environment_failure",
                "verification_identity_unconfirmed", "execution_failed", "execution_interrupted")):
        raise util.KanpilotError("conflict", "当前任务不在可处理的执行阻塞状态")
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1", (task["task_id"],)).fetchone():
        raise util.KanpilotError("conflict", "任务已有活动请求，请等待后刷新")
    probe = _client(identity, read_only=True)
    execution, hold, supported = _candidate(identity, probe)
    if (not supported or not hold or hold.get("state") != "blocked"
            or hold.get("executionId") != payload["execution_id"]
            or hold.get("version") != payload["expected_version"]):
        raise util.KanpilotError("conflict", "目标已变化或当前 Pi 不支持操作，请刷新或在 Pi 中处理")
    _require_idle_turn(identity)
    if identity["job"].get("status") not in ("finished", "failed"):
        raise util.KanpilotError("conflict", "执行观察尚未收尾，请稍后再试")
    frozen = _binding(conn, task)
    pending = {"operation_id": op_id, "request": request, "binding": frozen,
        "index_source_sha256": git_delivery.expected_index_source_sha256(frozen["worktree_path"]),
        "task_snapshot": _task_snapshot(task), "evidence_sha256": _run_hashes(identity),
        "blocked_snapshot": hold, "execution_turn_id": identity["job"]["turn_id"], "job_snapshot": identity["job"], "created_at": ctx.now_iso}
    with db.write_txn(conn):
        fresh = db.require_task(conn, task["task_id"])
        if _task_snapshot(fresh) != pending["task_snapshot"]:
            raise util.KanpilotError("conflict", "确认期间任务已变化")
        if conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND tool=? AND status IN ('prepared','unknown') LIMIT 1", (scope, TOOL)).fetchone():
            raise util.KanpilotError("goal_decision_unconfirmed", "已有未确认目标决定，请核对并接续")
        runtime_operations.begin_operation(conn, scope, op_id, "pi_desktop", TOOL, request, ctx.now_iso)
        runtime_operations.mark_operation_unknown(conn, scope, op_id, ctx.now_iso)
        db.set_extra(conn, fresh, ctx.now_iso, **{PENDING_KEY: pending})
    try:
        client = _client(identity, read_only=False)
        # Last source/claim check before the external decision. Resume may then legitimately edit source.
        if (_binding(conn, db.require_task(conn, task["task_id"])) != frozen
                or _task_snapshot(db.require_task(conn, task["task_id"])) != pending["task_snapshot"]):
            raise util.KanpilotError("conflict", "发送前源码或任务已变化")
        result = client.goal_action(identity["session_id"], execution_id=payload["execution_id"],
            expected_version=payload["expected_version"], action=payload["action"], operation_id=op_id, message=message)
        if payload["action"] == "continue":
            new_turn = result.get("turnId") or result.get("turn_id")
            if not new_turn:
                raise util.KanpilotError("conflict", "Pi 未返回继续执行的 turn 身份")
            adopted = continuation.adopt_continuation(ctx, task_id=task["task_id"], run_id=task["run_id"],
                job=identity["job"], run_dir=identity["run_dir"], session_id=identity["session_id"],
                old_turn=identity["job"]["turn_id"], new_turn=new_turn, operation_id=op_id)
            response = {"task_id": task["task_id"], "run_id": task["run_id"], "session_id": identity["session_id"],
                        "status": "continued", "turn_id": new_turn, "observer": adopted.get("observer")}
        else:
            response = _settle_acceptance(ctx, task, identity, pending, client)
        return _finish_operation(ctx, scope, pending, response)
    except Exception as exc:
        raise util.KanpilotError("goal_decision_unconfirmed", "目标决定结果未确认，不会重复发送；请核对并接续或打开 Pi：" + util.redact(getattr(exc, "message", str(exc)))) from exc


def cmd_reconcile_goal_blocker(ctx, payload):
    """Recover local settlement using durable host facts; never repeats an external action."""
    from . import cmd_request_continuation as continuation
    task, identity = _identity(ctx.conn, payload["task_id"], payload["run_id"])
    pending = db.task_extra(task).get(PENDING_KEY) or {}
    request = pending.get("request") or {}
    if request.get("task_id") != task["task_id"] or request.get("run_id") != task["run_id"]:
        raise util.KanpilotError("conflict", "本轮没有待核对的目标决定")
    scope = runtime_client.stable_scope_id("execution", task_id=task["task_id"], run_id=task["run_id"])
    op = runtime_operations.get_operation(ctx.conn, scope, pending["operation_id"])
    if not op or op["tool"] != TOOL or op["request_sha256"] != runtime_operations.compute_request_sha256(request):
        raise util.KanpilotError("conflict", "目标决定的持久回执不匹配")
    if op["status"] == "accepted":
        return dict(util.jload(op["result_json"]), deduplicated=True)
    if not db.task_is_active(task):
        raise util.KanpilotError("conflict", "任务不在活跃状态，未接续")
    # A crash after local settlement needs only the final operation acknowledgement.
    accepted = db.task_extra(task).get(KEY) or {}
    adopted = db.task_extra(task).get(continuation.REQUEST_KEY) or {}
    if request["action"] == "accept" and accepted.get("operation_id") == pending["operation_id"]:
        if not acceptance_current(ctx.conn, task):
            raise util.KanpilotError("conflict", "接受后的执行或源码已变化，未接续")
        response = dict(task_id=task["task_id"], run_id=task["run_id"], session_id=identity["session_id"], status="accepted")
    elif request["action"] == "continue" and adopted.get("operation_id") == pending["operation_id"]:
        response = dict(task_id=task["task_id"], run_id=task["run_id"], session_id=identity["session_id"],
                        status="continued", turn_id=adopted.get("new_turn"))
    else:
        if _task_snapshot(task) != pending["task_snapshot"]:
            raise util.KanpilotError("conflict", "待核对决定的任务或计划已变化，未接续")
        client = _client(identity, read_only=True)
        if request["action"] == "accept":
            response = _settle_acceptance(ctx, task, identity, pending, client)
        else:
            execution, hold, supported = _candidate(identity, client)
            old_segments = pending["blocked_snapshot"].get("segments") or []
            segments = (hold or {}).get("segments") or []
            # Exactly one host-owned new segment proves this blocked Goal resumed once.
            if (not supported or not hold or hold.get("executionId") != request["execution_id"]
                    or hold.get("resumeCount") != pending["blocked_snapshot"].get("resumeCount", 0) + 1
                    or len(segments) != len(old_segments) + 1 or segments[:len(old_segments)] != old_segments
                    or hold.get("version", 0) < request["expected_version"] + 1):
                raise util.KanpilotError("goal_decision_unconfirmed", "Pi 尚无唯一可核对的继续执行记录；不会重发")
            new_turn = segments[-1].get("turnId")
            status = client.agent_status(identity["session_id"])
            inner = status.get("status") if isinstance(status.get("status"), dict) else status
            current = inner.get("currentTurnId") or inner.get("current_turn_id")
            if not new_turn or current != new_turn:
                raise util.KanpilotError("conflict", "Pi 已有其它轮次或缺少新轮次身份，未接续")
            old_turn = pending["execution_turn_id"]
            adoption = {"operation_id": pending["operation_id"], "task_id": task["task_id"],
                "run_id": task["run_id"], "session_id": identity["session_id"],
                "from_turn": old_turn, "new_turn": new_turn}
            half_adopted = identity["job"].get("continuation_adoption") == adoption and identity["job"].get("turn_id") == new_turn
            hashes = _run_hashes(identity)
            if half_adopted:
                stable_keys = ("task_id", "run_id", "backend", "session_id", "goal_approval", "config", "plan_sha256")
                if (any(identity["job"].get(key) != pending["job_snapshot"].get(key) for key in stable_keys)
                        or any(hashes.get(path) != digest for path, digest in pending["evidence_sha256"].items()
                               if Path(path).name != "job.json")):
                    raise util.KanpilotError("conflict", "半完成接续的持久身份已变化，未接续")
            elif hashes != pending["evidence_sha256"] or identity["job"].get("turn_id") != old_turn:
                raise util.KanpilotError("conflict", "原执行记录已变化，未接续")
            adopted_result = continuation.adopt_continuation(ctx, task_id=task["task_id"], run_id=task["run_id"],
                job=identity["job"], run_dir=identity["run_dir"], session_id=identity["session_id"],
                old_turn=old_turn, new_turn=new_turn, operation_id=pending["operation_id"])
            response = dict(task_id=task["task_id"], run_id=task["run_id"], session_id=identity["session_id"],
                            status="continued", turn_id=new_turn, observer=adopted_result.get("observer"))
    return _finish_operation(ctx, scope, pending, response)


def accepted_delivery_evidence(conn, task, hashes, *, require_review_pass=True):
    """Exact user scope plus a matching independent pass; reports remain unverified."""
    saved = db.task_extra(task).get(KEY) or {}
    if not saved:
        return None
    from . import planfile, review_delivery
    from .engine import verdict
    receipt = db.task_extra(task).get("engine_verdict") or {}
    if (not review_admission_current(conn, task, receipt)
            or not verdict.valid_committed_receipt(conn, task, receipt)
            or require_review_pass and review_delivery._pass(task) is None):
        raise util.KanpilotError("conflict", "当前人工接受、执行提交或独立评审通过凭据不匹配")
    if not task["source_path"] or not task["source_sha256"] or not task["requirements_sha256"]:
        raise util.KanpilotError("conflict", "任务缺少冻结需求来源身份")
    source = Path(task["source_path"])
    before = util.sha256_file(source)
    _, source_sha, requirements = planfile.check_source_snapshot(source, task["task_id"], task["project_id"])
    if (source_sha, requirements) != (task["source_sha256"], task["requirements_sha256"]) or util.sha256_file(source) != before:
        raise util.KanpilotError("conflict", "冻结需求来源已变化")
    report_path = Path(task["review_report_path"])
    if report_path.is_symlink() or util.sha256_file(report_path) != task["review_report_sha256"]:
        raise util.KanpilotError("conflict", "独立评审报告字节已变化")
    hashes.update(saved["evidence_sha256"])
    hashes[str(source)] = before
    hashes[str(report_path)] = task["review_report_sha256"]
    return {"task_id": task["task_id"], "run_id": task["run_id"], "plan_sha256": task["plan_sha256"],
        "requirements_sha256": task["requirements_sha256"], "head_sha": core.delivery_of(task)["head_sha"],
        "source_sha256": saved["index_source_sha256"], "report_sha256": saved["report_sha256"],
        "report_id": saved.get("report_id"), "commands": (db.task_extra(task).get("plan_verify") or {}).get("commands") or [],
        "checks": (saved.get("accepted_report") or {}).get("checks") or [],
        "exact_coverage": False, "user_accepted": True, "acceptance_sha256": saved["acceptance_sha256"],
        "verification_ok": False, "acceptance": saved, "acceptance_snapshot": saved}
