"""Explicitly resume observation of existing work, never implementation or a model."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import uuid

from . import core, db, git_delivery, headless_adapter, round_store, util, verify_evidence

_PROOF = "execution_continuation"
_BINDING = ("run_id", "session_id", "turn_id", "plan_sha256")
_VERIFY_IDENTITY = ("task_id", "source_run_id", "repository", "requirements_sha256",
                    "plan_sha256", "worktree_path", "source_sha256", "source_records_dir",
                    "commands", "requested_at")


def _active_request(conn, task_id):
    return conn.execute("SELECT * FROM requests WHERE task_id=? AND status IN ('queued','claimed') "
                        "ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()


def _eligible(conn, task, *, pending=False):
    from . import execution_questions
    proof = db.task_extra(task).get(_PROOF) or {}
    request = db.task_extra(task).get("execution_continuation_request") or {}
    continued = (task["status"] == "needs_user"
                 and request.get("run_id") == task["run_id"]
                 and request.get("status") == "sent"
                 and isinstance(request.get("new_turn"), str) and request["new_turn"])
    ordinary = ((task["status"] == "needs_user" and task["stage"] == "monitor"
                 and task["failure_code"] == "execution_failed")
                or (continued and task["failure_code"] in
                    ("verification_unconfirmed", "execution_interrupted")))
    resuming = (pending and proof.get("run_id") == task["run_id"]
                and proof.get("plan_sha256") == task["plan_sha256"]
                and task["status"] == "running" and task["stage"] == "monitor")
    return bool(db.task_is_active(task) and (ordinary or resuming)
                and task["sent"] == 1 and task["send_state"] == "sent"
                and task["plan_path"] and task["plan_sha256"]
                and not db.task_extra(task).get("plan_execution_hold")
                and not core.review_repair_of(task)
                and not execution_questions.open_records(task)
                and _active_request(conn, task["task_id"]) is None)


def candidate(conn, task, job, turn_id, job_sha256):
    """Local eligibility only; the status reader has separately proved finished."""
    from . import runtime_client, runtime_operations
    request = db.task_extra(task).get("execution_continuation_request") or {}
    if (request.get("run_id") == task["run_id"]
            and isinstance(request.get("new_turn"), str) and request["new_turn"]
            and request["new_turn"] != turn_id):
        return None
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    if (not _eligible(conn, task, pending=True) or row is None
            or row["save_state"] != "active" or row["backend"] != "pi_desktop"
            or job.get("backend") != "pi_desktop" or not row["worktree_path"]
            or not turn_id or job.get("session_id") != task["session_id"]
            or row["session_id"] != task["session_id"]
            or job.get("config") != util.jload(row["config_json"])
            or job.get("plan_sha256", task["plan_sha256"]) != task["plan_sha256"]
            or job.get("worktree_path", row["worktree_path"]) != row["worktree_path"]):
        return None
    scope = runtime_client.stable_scope_id("execution", task_id=task["task_id"], run_id=task["run_id"])
    if job.get("scope_id") != scope or not runtime_operations.session_owned(conn, scope, task["session_id"]):
        return None
    try:
        path = Path(task["plan_path"])
        if path.is_symlink() or util.sha256_file(path) != task["plan_sha256"]:
            return None
    except OSError:
        return None
    return {"session_id": task["session_id"], "turn_id": turn_id,
            "plan_sha256": task["plan_sha256"], "job_sha256": job_sha256,
            "detail": "接续已有成果并核对验证证据；不会重新实施或调用模型。证据完整通过后才进入评审。"}


def _response(task_id, run_id, status, detail, **extra):
    return dict(task_id=task_id, run_id=run_id, status=status, detail=detail, **extra)


def _same(proof, identity):
    return all(proof.get(key) == identity[key] for key in _BINDING)


def _queued(conn, task, proof):
    request = conn.execute("SELECT * FROM requests WHERE request_id=?", (proof.get("request_id"),)).fetchone()
    if (proof.get("status") != "queued" or proof.get("run_id") != task["run_id"]
            or proof.get("plan_sha256") != task["plan_sha256"] or request is None):
        return None
    if task["status"] == "needs_user":
        if verification_report_supplement(conn, task):
            return _response(task["task_id"], task["run_id"], "verification_required",
                             "已有成果已接续，验证证据尚未齐全；请在任务详情确认补齐验证报告。",
                             request_id=request["request_id"], deduplicated=True)
        return _response(task["task_id"], task["run_id"], "blocked",
                         task["failure_reason"] or "本次核对受阻，请查看任务详情。", deduplicated=True)
    return _response(task["task_id"], task["run_id"], "queued",
                     "已有成果已接续，验证证据核对已排队；通过后才会进入评审。",
                     request_id=request["request_id"], deduplicated=True)


def cmd_continue_execution(ctx, payload):
    """Bind user intent to a fresh finished turn and queue only its monitor."""
    from . import cmd_dispatch, cmd_queue, execution_status, session_control
    conn, task_id, run_id = ctx.conn, payload["task_id"], payload["run_id"]
    identity = {"run_id": run_id, "session_id": payload["expected_session_id"],
                "turn_id": payload["recovery_turn_id"], "plan_sha256": payload["expected_plan_sha256"],
                "job_sha256": payload["expected_job_sha256"]}
    if not all(isinstance(value, str) and value.strip() for value in identity.values()):
        raise util.bad_request("核对成果必须指定完整的当前轮次与证据身份")
    task = db.require_task(conn, task_id)
    if (not db.task_is_active(task) or task["run_id"] != run_id
            or task["plan_sha256"] != identity["plan_sha256"] or task["session_id"] != identity["session_id"]):
        raise util.KanpilotError("stale_run", "当前任务、执行轮次、会话或计划已变化，请重新核验")
    previous = db.task_extra(task).get(_PROOF) or {}
    same = _same(previous, identity)
    source = headless_adapter.resolve_source(conn)
    directory = source.run_dir(task_id, run_id)
    job_path = directory / "job.json"
    # Accepted clients may replay the original fingerprint; a new status
    # query instead binds the observer's current rewritten job bytes.
    if not same or identity["job_sha256"] != previous.get("job_sha256"):
        try:
            if util.sha256_file(job_path) != identity["job_sha256"]:
                raise util.KanpilotError("conflict", "核验后执行记录已变化，请重新核验")
        except OSError as exc:
            raise util.KanpilotError("conflict", "执行记录已不可读，请重新核验") from exc
    if same:
        result = _queued(conn, task, previous)
        if result:
            return result
    if not _eligible(conn, task, pending=same):
        raise util.KanpilotError("conflict", "当前阻塞、活动请求或执行身份不允许接续，请刷新任务")
    check = execution_status.cmd_check_execution_status(ctx, {"task_id": task_id, "run_id": run_id})
    fresh_candidate = check.get("continuation")
    if check.get("state") != "finished" or not fresh_candidate:
        # Only the actual still-live observer of the accepted turn may wait.
        if same and previous.get("status") == "starting":
            signature = source.source_signature(task_id, run_id)
            job = signature["job"] if signature else {}
            live = session_control.cmd_session_control_status(ctx, {"task_id": task_id, "run_id": run_id})
            pid = job.get("observe_pid") or job.get("pid")
            if (job.get("turn_id") == identity["turn_id"] and live.get("connected") is True
                    and live.get("session_id") == identity["session_id"]
                    and live.get("observed_turn_id") == identity["turn_id"]
                    and live.get("state") == "idle" and pid
                    and cmd_dispatch._observer_pid_alive(conn, task_id, run_id, pid, directory, job)):
                return _response(task_id, run_id, "starting", "观察续接仍在收尾，请稍后重新核验；尚未判定验证通过。", deduplicated=True)
        raise util.KanpilotError("conflict", "当前执行尚无可接续的已结束成果，请重新核验")
    if any(fresh_candidate.get(key) != identity[key] for key in ("session_id", "turn_id", "plan_sha256")):
        raise util.KanpilotError("conflict", "核验后 Plus 当前轮次已变化，请重新核验")
    if ((not same or identity["job_sha256"] != previous.get("job_sha256"))
            and fresh_candidate["job_sha256"] != identity["job_sha256"]):
        raise util.KanpilotError("conflict", "核验后本机执行证据已变化，请重新核验")
    from .engine import verdict
    binding = verdict.binding(conn, task)
    if same and binding["source_sha256"] != previous.get("source_sha256"):
        raise util.KanpilotError("conflict", "接续期间源码已变化，请先核对已有成果")
    token = uuid.uuid4().hex
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        current = db.task_extra(task).get(_PROOF) or {}
        if _same(current, identity):
            result = _queued(conn, task, current)
            if result:
                return result
            expires = util.parse_ts(current.get("expires_at"))
            if current.get("busy") and expires and expires > util.now_dt():
                return _response(task_id, run_id, "starting", "同一成果的接续正在处理中，请稍后重新核验。", deduplicated=True)
        if task["run_id"] != run_id or not _eligible(conn, task, pending=same):
            raise util.KanpilotError("conflict", "核对期间任务或请求已变化，请刷新后重试")
        if (task["session_id"] != identity["session_id"] or task["plan_sha256"] != identity["plan_sha256"]
                or (not same and util.sha256_file(job_path) != identity["job_sha256"])):
            raise util.KanpilotError("conflict", "核对期间执行证据已变化，请重新核验")
        proof = dict(identity, status="starting", token=token, busy=True,
                     expires_at=util.to_iso(util.now_dt() + timedelta(seconds=120)),
                     source_sha256=binding["source_sha256"],
                     previous_failure={key: task[key] for key in ("failure_code", "failure_reason", "failure_stage", "blocked_at")})
        if same:
            proof.update(job_sha256=previous["job_sha256"], previous_failure=previous["previous_failure"])
        db.set_extra(conn, task, ctx.now_iso, **{_PROOF: proof})
    try:
        signature = source.source_signature(task_id, run_id)
        job = signature["job"] if signature else None
        if not job:
            raise util.KanpilotError("conflict", "执行记录已不可读，未继续推进")
        if job.get("status") == "finished" and job.get("turn_id") == identity["turn_id"]:
            observed = {"status": "finished"}
        else:
            observed = cmd_dispatch.cmd_round_observe(ctx, {"task_id": task_id, "run_id": run_id,
                "executor": "user", "recovery_turn_id": identity["turn_id"]}, preserve_execution_failure=True)
        if observed.get("status") in ("starting", "finalizing", "observing"):
            return _response(task_id, run_id, "starting", "已有轮次正在接续观察或收尾，请稍后重新核验；没有重新执行任务。")
        if observed.get("status") not in ("finished", "settled"):
            return _response(task_id, run_id, "blocked", util.redact(observed.get("detail") or "观察续接尚未确认，未继续推进。"))
        # This re-read requires completed/0, a dead observer and current Plus
        # identity. A failed recovered turn never acquires a monitor success.
        settled = execution_status.cmd_check_execution_status(ctx, {"task_id": task_id, "run_id": run_id})
        if settled.get("state") != "finished":
            return _response(task_id, run_id, "blocked", settled.get("summary") or "接续成果尚无可信完成记录。")
        live = session_control.cmd_session_control_status(ctx, {"task_id": task_id, "run_id": run_id})
        if (live.get("connected") is not True or live.get("state") != "idle"
                or live.get("session_id") != identity["session_id"]
                or live.get("observed_turn_id") != identity["turn_id"]):
            raise util.KanpilotError("conflict", "接续期间 Plus 会话或当前轮次已变化，未继续推进")
        current_task = db.require_task(conn, task_id)
        if verdict.binding(conn, current_task) != binding:
            raise util.KanpilotError("conflict", "接续期间计划、工作树或源码已变化，未继续推进")
        with db.write_txn(conn):
            current_task = db.require_task(conn, task_id)
            current = db.task_extra(current_task).get(_PROOF) or {}
            if (current.get("token") != token or current_task["run_id"] != run_id
                    or current_task["plan_sha256"] != identity["plan_sha256"]
                    or current_task["session_id"] != identity["session_id"]
                    or not _eligible(conn, current_task, pending=True)
                    or verdict.binding(conn, current_task) != binding):
                raise util.KanpilotError("conflict", "接续期间任务、阻塞或活动请求已变化，未继续推进")
            if current_task["status"] == "needs_user":
                core.transition_task(conn, current_task, "running", ctx.now_iso, "user",
                    detail={"via": "continue-execution", "run_id": run_id}, **core.clear_block_fields())
            request, _ = cmd_queue._new_cycle_request(conn, ctx.now_iso, current_task, "monitor", requested_by="user")
            db.set_extra(conn, db.require_task(conn, task_id), ctx.now_iso,
                         **{_PROOF: dict(current, status="queued", request_id=request["request_id"], busy=False)})
            db.add_event(conn, ctx.now_iso, "execution_continued", task_id=task_id,
                         request_id=request["request_id"], actor="user",
                         detail=dict(identity, previous_failure=proof["previous_failure"],
                                     source_sha256=binding["source_sha256"]))
        return _response(task_id, run_id, "queued", "已有成果已接续，验证证据核对已排队；通过后才会进入评审。",
                         request_id=request["request_id"], deduplicated=False)
    finally:
        with db.write_txn(conn):
            task = db.require_task(conn, task_id)
            proof = db.task_extra(task).get(_PROOF) or {}
            if proof.get("token") == token and proof.get("busy"):
                db.set_extra(conn, task, util.now_iso(), **{_PROOF: dict(proof, busy=False)})


def verification_report_supplement(conn, task):
    """Only explicit continuation or its verification-only successor gets this mode."""
    if (not db.task_is_active(task) or task["status"] != "needs_user" or task["stage"] != "monitor"
            or task["failure_code"] not in ("verification_unconfirmed", "verification_environment_failure")
            or core.review_repair_of(task)):
        return False
    proof = db.task_extra(task).get(_PROOF) or {}
    if (proof.get("status") == "queued" and proof.get("run_id") == task["run_id"]
            and proof.get("plan_sha256") == task["plan_sha256"]):
        return True
    retry = core.execution_retry_of(task)
    frozen = retry.get("verification_only") or {}
    if (frozen.get("task_id") != task["task_id"]
            or frozen.get("plan_sha256") != task["plan_sha256"]
            or frozen.get("source_run_id") != retry.get("source_run_id")
            or frozen.get("source_run_id") == task["run_id"]):
        return False
    signature = headless_adapter.resolve_source(conn).source_signature(task["task_id"], task["run_id"])
    current = (signature["job"].get("verification_only") or {}) if signature else {}
    return bool(current.get("identity_sha256") and current.get("identity_sha256") == frozen.get("identity_sha256"))


def _verification_identity(frozen):
    return util.sha256_text(util.jdump({key: frozen.get(key) for key in _VERIFY_IDENTITY}))


def verification_body(frozen, plan_path):
    raw = Path(plan_path).read_bytes()
    declaration = verify_evidence.parse_verify_block(raw.decode("utf-8"))
    util.require(util.sha256_bytes(raw) == frozen.get("plan_sha256") and declaration["ok"]
                 and declaration["commands"] == frozen.get("commands"), "补验证的冻结计划或命令已变化", "validation_failed")
    return ("## 仅补齐已有成果的验证报告\n\n"
            "保留当前工作树的已有实现，不重新实施原计划，不修复或修改源码，不重新规划。"
            "只核对来源执行记录；缺少可核实记录的命令必须实际运行，按原冻结命令提交真实结构化验证报告。"
            "验证失败或环境、材料不足时诚实报告 failed/blocked，不顺手修代码；需要修改应交回用户选择重新执行。"
            "不得新建或切换工作树/分支，不得提交、推送、合并、clean、reset或删除成果。\n\n"
            "来源执行轮次：" + frozen["source_run_id"] + "\n"
            "来源记录目录：" + frozen["source_records_dir"] + "\n\n"
            "### 必须逐项核实的原计划验证命令\n\n```verify\n"
            + "\n".join(frozen["commands"]) + "\n```\n\n"
            "每条命令均需真实结果、独立整数退出码和当前源码身份；不以助手文字宣称通过。")


def freeze_verification(task, materials, now):
    raw = Path(task["plan_path"]).read_bytes()
    declaration = verify_evidence.parse_verify_block(raw.decode("utf-8"))
    util.require(declaration["ok"] and util.sha256_bytes(raw) == task["plan_sha256"], "原计划缺少可信验证命令", "validation_failed")
    frozen = {"task_id": task["task_id"], "source_run_id": task["run_id"],
              "repository": task["repository"], "requirements_sha256": task["requirements_sha256"],
              "plan_sha256": task["plan_sha256"], "worktree_path": materials["worktree"]["worktree_path"],
              "source_sha256": git_delivery.source_state_sha256(materials["worktree"]["worktree_path"]),
              "source_records_dir": str(materials["source"].run_dir(task["task_id"], task["run_id"])),
              "commands": declaration["commands"], "requested_at": now}
    frozen["identity_sha256"] = _verification_identity(frozen)
    frozen["body"] = verification_body(frozen, task["plan_path"])
    frozen["body_sha256"] = util.sha256_text(frozen["body"])
    return frozen


def validate_verification(frozen, *, task=None, spec=None, check_source=False):
    owner = spec or task
    util.require(isinstance(frozen, dict), "补验证的冻结身份不符", "validation_failed")
    workspace = spec["cwd"] if spec is not None else frozen.get("worktree_path")
    util.require(isinstance(frozen, dict) and owner is not None
                 and frozen.get("task_id") == owner["task_id"]
                 and frozen.get("repository") == owner["repository"]
                 and frozen.get("plan_sha256") == owner["plan_sha256"]
                 and frozen.get("worktree_path") == workspace
                 and frozen.get("identity_sha256") == _verification_identity(frozen),
                 "补验证的冻结身份不符", "validation_failed")
    if task is not None:
        util.require(frozen.get("requirements_sha256") == task["requirements_sha256"],
                     "补验证的需求身份已变化", "validation_failed")
    body = verification_body(frozen, owner["plan_path"])
    util.require(frozen.get("body") == body and frozen.get("body_sha256") == util.sha256_text(body),
                 "补验证的冻结正文不符", "validation_failed")
    if spec is not None:
        util.require(frozen.get("source_run_id") != spec["run_id"], "补验证必须使用新的执行轮次", "validation_failed")
    if check_source:
        util.require(git_delivery.source_state_sha256(workspace) == frozen.get("source_sha256"),
                     "补验证确认后源码已变化，请重新核对成果", "verification_identity_unconfirmed")
    return body


def freeze_verification_spec(task, spec):
    frozen = core.execution_retry_of(task).get("verification_only")
    if frozen is not None:
        validate_verification(frozen, task=task, check_source=True)
        validate_verification(frozen, spec=spec, check_source=True)
        spec["verification_only"] = dict(frozen)
