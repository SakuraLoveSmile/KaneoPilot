"""Identity-bound direct Review repairs. No model invocation or remote writes."""
from pathlib import Path

from . import automation_policy, constants, core, db, git_delivery, project_config, util

IDENTITY = {
    "expected_execution_run_id": "source_execution_run_id",
    "expected_review_key": "review_key", "expected_report_sha256": "report_sha256",
    "expected_plan_sha256": "plan_sha256",
}


def candidate(task):
    value = core.review_repair_of(task)
    if value:
        return value
    extra = db.task_extra(task)
    old = extra.get("fix_request") or {}
    legacy_held = old.get("state") == "held" and not old.get("mode")
    if legacy_held:
        matching = [entry for entry in extra.get("review_history") or []
                    if entry.get("review_key") == old.get("review_key")
                    and entry.get("report_sha256") == old.get("report_sha256")
                    and entry.get("execution_run_id") == task["run_id"]
                    and entry.get("conclusion") == "changes_requested"]
        delivery = core.delivery_of(task)
        if (len(matching) != 1 or old.get("execution_run_id") != task["run_id"]
                or not all(old.get(key) for key in ("review_key", "report_path", "report_sha256"))
                or any(not old.get(key) or old[key] != delivery.get(key) for key in ("head_sha", "branch", "worktree_path"))):
            return None
    elif (extra.get("review_conclusion") != "changes_requested" or not task["review_report_path"]
            or not task["review_report_sha256"] or not task["run_id"] or not task["review_key"]):
        return None
    key = old["review_key"] if legacy_held else task["review_key"]
    # Historical rows remain untouched until the explicit start command.
    history = [h for h in extra.get("review_history") or []
               if h.get("review_key") == key and isinstance(h.get("fix"), dict)]
    fix = history[0]["fix"] if len(history) == 1 else {}
    delivery = core.delivery_of(task)
    return dict(mode="direct", state="held", task_id=task["task_id"], project_id=task["project_id"],
        repository=task["repository"], requirements_sha256=task["requirements_sha256"],
        source_execution_run_id=task["run_id"], review_key=key,
        report_path=old["report_path"] if legacy_held else task["review_report_path"],
        report_sha256=old["report_sha256"] if legacy_held else task["review_report_sha256"],
        plan_sha256=task["plan_sha256"], title=fix.get("title") or "修复评审发现的问题",
        description=fix.get("description") or "处理本轮评审报告中的问题，保留已有功能。",
        **({"review_run_id": old.get("review_run_id")} if legacy_held else {}),
        priority=fix.get("priority") or "high", **{k: delivery.get(k) for k in
        ("head_sha", "branch", "worktree_path", "base_branch", "base_sha")})


def _round_count(task):
    # Taking over a managed task must not reset the task's spent repair rounds.
    return max(int(core.automation_of(task).get("fix_round", 0)),
               int(db.task_extra(task).get("review_repair_round", 0)))


def _gate(conn, task, value, now):
    if task["status"] not in ("in_review", "needs_user") or task["stage"] != "review":
        return "任务当前不处于待修复评审"
    from . import parent_closeout
    closeout = parent_closeout.projection(conn, task, now)
    if closeout:
        return "历史二级修复任务需先收尾，请查看收尾校验"
    if not db.task_schedulable(task) or db.paused(conn, "dispatch"):
        return "任务或执行阶段已暂停"
    child = db.get_task(conn, task["fix_task_id"]) if task["fix_task_id"] else None
    if (child is not None and child["project_id"] == task["project_id"]
            and child["repository"] == task["repository"] and db.task_is_active(child)
            and child["status"] != "done"):
        return "历史修复子任务仍在处理，请先收尾"
    if not core.same_branch_fix_ready(task)[0] or not all(value.get(k) for k in ("head_sha", "branch", "worktree_path")):
        return core.FIX_BRANCH_UNAVAILABLE_DETAIL
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')", (task["task_id"],)).fetchone():
        return "当前评审或其他请求尚未结束"
    _, _, items, _ = project_config.load_raw_mappings(conn)
    mapping = items.get(task["project_id"]) or {}
    if not mapping.get("enabled", True) or mapping.get("repository") != task["repository"]:
        return "项目仓库映射已变化或停用"
    policy = automation_policy.policy_for(conn, task["project_id"]) or constants.AUTOMATION_DEFAULTS
    if core.is_automation_managed(task) and not policy.get("enabled"):
        return "项目自动处理已关闭"
    if core.is_automation_managed(task) and value.get("review_run_id"):
        settled = conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='review' AND run_id=? ORDER BY rowid DESC LIMIT 1",
            (task["task_id"], value["review_run_id"])).fetchone()
        if settled is None or settled["status"] != "succeeded" or (util.jload(settled["result_json"]) or {}).get("outcome") != "succeeded":
            return "原评审请求尚未成功结算"
    count = _round_count(task)
    if count >= policy["max_fix_rounds"]:
        return "本任务修复次数已用尽"
    if not core.automation_budget(task, policy, now)["ok"]:
        return "本任务时间或费用额度已用尽"
    return None


def view(conn, task, now):
    from . import parent_closeout
    if parent_closeout.projection(conn, task, now) is not None:
        return None
    value = candidate(task)
    if not value or value.get("source_kind") in ("merge_conflict", "build_failure"):
        return None
    policy = automation_policy.policy_for(conn, task["project_id"]) or constants.AUTOMATION_DEFAULTS
    state = value.get("state", "held")
    reason = _gate(conn, task, value, now) if state == "held" else None
    reason = reason or value.get("reason") or {"held": "评审发现问题，可直接启动修复会话",
        "queued": "修复会话待启动", "running": "修复中", "awaiting_review": "修复后待复审"}.get(state)
    if state == "running":
        value = dict(value, session_id=task["session_id"])
    return {**{k: value.get(k) for k in ("review_key", "report_path", "report_sha256", "plan_sha256",
        "source_execution_run_id", "repair_execution_run_id", "session_id")}, "state": state,
        "title": value.get("title"), "reason": reason, "round": value.get("fix_round", 0),
        "max_rounds": policy["max_fix_rounds"], "can_start": state == "held" and _gate(conn, task, value, now) is None}


def retry_of(task):
    """Only this frozen Review may reuse a previously confirmed repair retry."""
    value = core.review_repair_of(task)
    if isinstance(task, dict) and "extra_json" not in task:
        task = dict(task, extra_json=util.jdump(task.get("extra") or {}))
    retry = core.execution_retry_of(task)
    return retry if (value and not value.get("source_kind") and value.get("review_key")
                     and retry.get("review_repair_key") == value["review_key"]) else {}


def verify_frozen(task):
    value = core.review_repair_of(task)
    util.require(value and value.get("state") in ("held", "queued", "running", "awaiting_review"), "修复材料未冻结", "conflict")
    for key in ("task_id", "project_id", "repository", "requirements_sha256", "plan_sha256"):
        util.require(value.get(key) and value[key] == task[key], "修复任务、需求或计划身份已变化", "conflict")
    delivery = core.delivery_of(task)
    util.require(all(value.get(k) and value[k] == delivery.get(k) for k in ("branch", "worktree_path")), "修复分支身份已变化", "conflict")
    expected_run = value.get("repair_execution_run_id") if value.get("state") in ("running", "awaiting_review") else value.get("source_execution_run_id")
    recovery = value.get("verification_recovery")
    if value.get("source_kind") == "merge_conflict" and value.get("state") == "queued" and recovery:
        retry = core.execution_retry_of(task)
        util.require(task["run_id"] is None
                     and recovery.get("source_run_id")
                     and retry.get("source_run_id") == recovery["source_run_id"]
                     and value.get("repair_execution_run_id") == recovery["source_run_id"],
                     "补报验证的原执行轮次已变化", "conflict")
    elif value.get("state") == "queued" and retry_of(task):
        retry = retry_of(task)
        util.require(task["run_id"] is None and value.get("repair_execution_run_id")
                     and retry["source_run_id"] == value["repair_execution_run_id"],
                     "重试修复的原执行轮次已变化", "conflict")
    else:
        util.require(task["run_id"] == expected_run, "修复执行轮次已变化", "conflict")
    try:
        report = util.inside(Path(value["report_path"]), constants.review_reports_root(), "review report")
        util.require(report.is_file() and util.sha256_file(report) == value["report_sha256"]
                     and util.sha256_file(Path(task["plan_path"])) == value["plan_sha256"], "评审报告或原计划内容已变化", "conflict")
    except (OSError, KeyError) as exc:
        raise util.KanpilotError("conflict", "评审报告或原计划不可读取") from exc
    if value.get("source_kind") in ("merge_conflict", "build_failure"):
        from . import task_ai_action
        task_ai_action.verify_frozen(task, value)
    return dict(value)


def verify_source_terminal(conn, task, now=None):
    """A closed request does not prove the old execution stopped in Plus."""
    from types import SimpleNamespace
    from . import cmd_queue
    value = core.review_repair_of(task) or candidate(task)
    util.require(value is not None, "没有待修复评审", "conflict")
    if value.get("source_kind") in ("merge_conflict", "build_failure"):
        from . import task_ai_action
        return task_ai_action.verify_source_terminal(conn, task, value)
    current = now or util.now_dt()
    row, detail = cmd_queue._require_execution_terminal(
        SimpleNamespace(conn=conn, now=current, now_iso=util.to_iso(current)),
        task, value["source_execution_run_id"])
    util.require(row["run_status"] == "finished" and (detail.get("terminal") or {}).get("outcome") == "completed",
                 "原执行尚无可信完成终态，不能启动修复会话", "conflict")
    return row


def start_inline(conn, task, now, value, actor):
    """Caller owns transaction and has verified frozen files, Git and remote."""
    reason = _gate(conn, task, value, now)
    util.require(reason is None, reason or "修复不可启动", "conflict")
    extra = db.task_extra(task)
    if core.is_automation_managed(task):
        automation = core.automation_of(task)
        automation.update(fix_round=_round_count(task) + 1, plan_wanted=False,
                          replan_requested_at=None, replan_remote_status=None,
                          replan_worktree_sha256=None)
        extra["automation"] = automation
        count = automation["fix_round"]
    else:
        count = _round_count(task) + 1
        extra["review_repair_round"] = count
    value = dict(value, state="queued", fix_round=count, queued_at=now,
                 authorized_by=actor, authorized_at=now)
    value.pop("reason", None)
    extra["fix_request"] = value
    # A new Review repair supersedes any retry of the preceding execution.
    extra.pop("execution_retry", None)
    for key in ("review_conclusion", "review_registered_at", "registered_review_identity", "verification_override", "plan_execution_hold"):
        extra.pop(key, None)
    core.transition_task(conn, task, "ready", now, actor, detail={"via": "direct_review_repair"},
        sent=0, send_state=None, sent_at=None, lock_retained=0, review_report_path=None,
        review_report_sha256=None, review_key=None, extra_json=extra, **core.clear_block_fields())
    request, _ = db.create_request(conn, now, "dispatch", "dispatch:" + task["task_id"], task_id=task["task_id"],
        requested_by=actor, payload={"review_repair_key": value["review_key"]})
    db.add_event(conn, now, "fix_round_started", task_id=task["task_id"], actor=actor,
        detail={"mode": "direct", "fix_round": count, "review_key": value["review_key"]})
    return {"ok": True, "request_id": request["request_id"], "review_repair": value}


def cmd_start_review_repair(ctx, payload):
    task = db.require_task(ctx.conn, payload["task_id"])
    value = candidate(task)
    util.require(not value or value.get("source_kind") not in ("merge_conflict", "build_failure"),
                 "合并冲突需从任务内 AI 处理入口确认", "conflict")
    util.require(value is not None and all(payload.get(k) == value.get(v) for k, v in IDENTITY.items()), "修复材料身份已变化，请刷新后重试", "conflict")
    if value.get("state") != "held":
        verify_frozen(task)
        return {"ok": True, "already_started": True, "review_repair": value}
    reason = _gate(ctx.conn, task, value, ctx.now)
    util.require(reason is None, reason or "修复不可启动", "conflict")
    # Validate a virtual frozen value for historical/manual candidates without writing.
    virtual = dict(task)
    virtual["extra_json"] = util.jdump(dict(db.task_extra(task), fix_request=value))
    verify_frozen(virtual)
    verify_source_terminal(ctx.conn, virtual, ctx.now)
    try:
        git_delivery.verify_branch_worktree(dict(core.delivery_of(task), repository=task["repository"]), require_clean=True, expected_head=value["head_sha"])
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", exc.message) from exc
    from . import review_resolution
    review_resolution._remote(task)
    snapshot = dict(task)
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task["task_id"])
        util.require(dict(current) == snapshot, "任务已变化，请刷新后重试", "conflict")
        return start_inline(ctx.conn, current, ctx.now_iso, value, "automation" if core.is_automation_managed(current) else "user")


def try_auto_start(ctx, task_id):
    task = db.require_task(ctx.conn, task_id)
    value = core.review_repair_of(task)
    if (not core.is_automation_managed(task) or value.get("state") != "held"
            or value.get("source_kind") in ("merge_conflict", "build_failure")):
        return
    try:
        return cmd_start_review_repair(ctx, dict(task_id=task_id,
            **{k: value.get(v) for k, v in IDENTITY.items()}))
    except util.KanpilotError as exc:
        # Keep held repair and its identity recoverable; never replay a send.
        with db.write_txn(ctx.conn):
            current = db.require_task(ctx.conn, task_id)
            fix = core.review_repair_of(current)
            if fix == value:
                db.set_extra(ctx.conn, current, ctx.now_iso, fix_request=dict(fix, reason=exc.message))
