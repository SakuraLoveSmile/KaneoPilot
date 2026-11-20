"""User-accepted residual Review risk, bound to one exact Review (KP-MG03 #4).

A manual task can be stuck after Review returns ``changes_requested`` and the
repair rounds are spent. Accepting the residual risk freezes that decision for
the exact reviewed head; the existing delivery chain may then be continued as
if the review had passed. Nothing is pushed here and no automatic behaviour is
widened. v1 covers only the manual first delivery (no PR yet); PR dead ends are
handled by ``discard-task-ai-action`` (KP-MG02).
"""
from . import constants, core, db, git_delivery, project_config, review_repair, util

KEY = "review_risk_acceptance"
_EVIDENCE_KEYS = ("engine_verdict", "plan_verify", "review_history")
_IDENTITY_FIELDS = ("review_key", "report_path", "report_sha256", "head_sha",
                    "execution_run_id")
_MAX_NOTE = 500


def _digest(value):
    return util.sha256_text(util.jdump(value))


def _task_keys():
    from . import review_delivery
    return review_delivery._TASK_KEYS


def _branch_keys():
    from . import review_delivery
    return review_delivery._BRANCH_KEYS


def _latest_review_request(conn, task):
    return conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='review' "
                        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                        (task["task_id"],)).fetchone()


def _review_entry(extra):
    history = extra.get("review_history") or []
    return history[-1] if history and isinstance(history[-1], dict) else {}


def _valid_receipt(value):
    return (isinstance(value, dict) and value.get("authorized_by") == "user"
            and value.get("authorized_at") and value.get("id")
            and value["id"] == _digest({key: item for key, item in value.items() if key != "id"}))


def acceptance(task):
    """The stored receipt, only while every frozen identity field still matches."""
    extra = db.task_extra(task)
    value = extra.get(KEY) or {}
    delivery = core.delivery_of(task)
    if not _valid_receipt(value):
        return None
    if (core.is_automation_managed(task) or core.automation_of(task).get("manual_takeover")
            or extra.get("review_conclusion") != "changes_requested"
            or extra.get("fix_request")
            or task["review_key"] != value.get("review_key")
            or task["review_report_path"] != value.get("report_path")
            or task["review_report_sha256"] != value.get("report_sha256")
            or task["plan_sha256"] != value.get("plan_sha256")
            or task["run_id"] != value.get("execution_run_id")
            or delivery.get("head_sha") != value.get("head_sha")
            or value.get("task") != {key: task[key] for key in _task_keys()}
            or value.get("branch") != {key: delivery.get(key) for key in _branch_keys()}
            or value.get("fix_round") != review_repair._round_count(task)
            or value.get("evidence_identity") !=
            _digest({key: extra.get(key) for key in _EVIDENCE_KEYS})):
        return None
    review = _review_entry(extra)
    identity = extra.get("registered_review_identity") or {}
    if (review.get("conclusion") != "changes_requested"
            or any(review.get(key) != value.get(key) for key in _IDENTITY_FIELDS)
            or review.get("plan_sha256", task["plan_sha256"]) != value.get("plan_sha256")
            or identity.get("request_id") != value.get("review_request_id")
            or identity.get("run_id") != value.get("review_run_id")
            or identity.get("report_sha256") != value.get("report_sha256")):
        return None
    return dict(value)


def identity(task):
    """Stable delivery identity for a valid acceptance (same shape as a pass identity)."""
    value = acceptance(task)
    if value is None:
        return None
    return dict(kind="risk_acceptance", request_id=value["review_request_id"],
                run_id=value["review_run_id"], plan_sha256=task["plan_sha256"],
                report_sha256=task["review_report_sha256"], review_key=task["review_key"])


def projection(task):
    value = acceptance(task)
    if value is None:
        return None
    findings = value.get("findings") or []
    return dict(authorized_at=value.get("authorized_at"), note=value.get("note") or "",
                head_sha=value.get("head_sha"), review_key=value.get("review_key"),
                findings=findings, findings_count=len(findings))


def _findings(fix):
    if not isinstance(fix, dict):
        return []
    items = []
    for key in ("title", "description"):
        text = str(fix.get(key) or "").strip()
        if text:
            items.append(text[:500])
    return items[:3]


def _gate(conn, task, payload):
    from . import review_delivery, release_delivery
    extra, delivery = db.task_extra(task), core.delivery_of(task)
    if core.is_automation_managed(task):
        return "仅手动任务可接受遗留风险"
    if core.automation_of(task).get("manual_takeover"):
        return "任务已转为人工处理"
    if task["status"] != "in_review" or task["stage"] != "review":
        return "任务当前不处于待交付的评审状态"
    if db.task_auto_paused(task) or db.paused(conn, "delivery"):
        return "任务或交付阶段已暂停"
    if task["sent"] != 1 or task["send_state"] != "sent":
        return "任务尚未成功发送，不能接受遗留风险"
    if (delivery.get("workspace_version") != 1 or delivery.get("mode") != "manual"
            or any(delivery.get(key) for key in ("pr_number", "pushed_sha", "merged_at", "stage_contracts"))):
        return "接受遗留风险仅用于尚未创建 PR 的手动分支交付"
    if extra.get("review_conclusion") != "changes_requested":
        return "当前评审结论不是要求改动"
    fix = review_repair.candidate(task)
    if (not fix or fix.get("state") != "held" or fix.get("source_kind") in constants.AI_ACTION_KINDS
            or fix.get("head_sha") != delivery.get("head_sha")):
        return "没有可接受的本轮评审改动"
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                    (task["task_id"],)).fetchone():
        return "当前评审或其他请求尚未结束"
    _, _, items, _ = project_config.load_raw_mappings(conn)
    mapping = items.get(task["project_id"]) or {}
    if not mapping.get("enabled", True) or mapping.get("repository") != task["repository"]:
        return "项目仓库映射已变化或停用"
    if not release_delivery._policy(conn, task)[0]["enabled"]:
        return "项目发布策略未启用，无需风险接受"
    if not review_delivery._execution_active(conn, task):
        return "当前执行轮次尚未成功收尾"
    expected = dict(review_key=payload.get("expected_review_key"),
                    report_sha256=payload.get("expected_report_sha256"),
                    head_sha=payload.get("expected_head_sha"),
                    plan_sha256=payload.get("expected_plan_sha256"))
    current = dict(review_key=fix.get("review_key"), report_sha256=fix.get("report_sha256"),
                   head_sha=fix.get("head_sha"), plan_sha256=fix.get("plan_sha256"))
    if any(expected.get(key) != value for key, value in current.items()):
        return "评审或交付身份已变化，请刷新后重试"
    try:
        git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]),
                                            require_clean=True, expected_head=delivery["head_sha"])
    except git_delivery.GitDeliveryError as exc:
        return exc.message
    request = _latest_review_request(conn, task)
    identity = extra.get("registered_review_identity") or {}
    if (request is None or request["status"] != "succeeded" or not request["finished_at"]
            or identity.get("request_id") != request["request_id"]
            or identity.get("report_sha256") != task["review_report_sha256"]):
        return "本轮评审尚未成功结算"
    return None


def _receipt(conn, task, note, now):
    extra, delivery = db.task_extra(task), core.delivery_of(task)
    request = _latest_review_request(conn, task)
    identity = extra.get("registered_review_identity") or {}
    value = dict(
        authorized_by="user", authorized_at=now, note=(note or "").strip()[: _MAX_NOTE],
        task={key: task[key] for key in _task_keys()},
        branch={key: delivery.get(key) for key in _branch_keys()},
        review_key=task["review_key"], report_path=task["review_report_path"],
        report_sha256=task["review_report_sha256"], head_sha=delivery["head_sha"],
        plan_sha256=task["plan_sha256"], requirements_sha256=task["requirements_sha256"],
        execution_run_id=task["run_id"], fix_round=review_repair._round_count(task),
        review_request_id=request["request_id"],
        review_run_id=identity.get("run_id"),
        review_result_sha256=_digest(util.jload(request["result_json"])),
        findings=_findings(review_repair.candidate(task)),
        evidence_identity=_digest({key: extra.get(key) for key in _EVIDENCE_KEYS}))
    value["id"] = _digest(value)
    return value


def cmd_accept_review_risk(ctx, payload):
    util.require(payload.get("confirm") is True, "接受遗留风险需要显式确认", "bad_request")
    note = payload.get("note")
    util.require(note is None or (isinstance(note, str) and len(note) <= _MAX_NOTE),
                 "note 需为不超过 500 字的文本", "bad_request")
    task = db.require_task(ctx.conn, payload["task_id"])
    existing = acceptance(task)
    if existing is not None and existing.get("head_sha") == payload.get("expected_head_sha"):
        return {"ok": True, "deduplicated": True, "review_risk": projection(task)}
    reason = _gate(ctx.conn, task, payload)
    util.require(reason is None, reason or "不可接受遗留风险", "conflict")
    snapshot = dict(task)
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task["task_id"])
        util.require(dict(current) == snapshot, "任务已变化，请刷新后重试", "conflict")
        reason = _gate(ctx.conn, current, payload)
        util.require(reason is None, reason or "不可接受遗留风险", "conflict")
        value = _receipt(ctx.conn, current, note, ctx.now_iso)
        # Accepting the risk replaces this review's repair path; the review history stays.
        db.set_extra(ctx.conn, current, ctx.now_iso, fix_request=None, **{KEY: value})
        db.add_event(ctx.conn, ctx.now_iso, "review_risk_accepted", task_id=task["task_id"],
                     actor="user", detail={"head_sha": value["head_sha"],
                                           "review_key": value["review_key"],
                                           "findings": len(value["findings"])})
    return {"ok": True, "review_risk": projection(db.require_task(ctx.conn, task["task_id"]))}


def cmd_revoke_review_risk_acceptance(ctx, payload):
    util.require(payload.get("confirm") is True, "撤销风险接受需要显式确认", "bad_request")
    task = db.require_task(ctx.conn, payload["task_id"])
    value = db.task_extra(task).get(KEY) or {}
    util.require(_valid_receipt(value), "没有可撤销的风险接受", "conflict")
    util.require(value.get("head_sha") == payload.get("expected_head_sha"),
                 "交付头已变化，请刷新后重试", "conflict")
    util.require(not ctx.conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
                                      (task["task_id"],)).fetchone(), "当前交付请求尚未结束")
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task["task_id"])
        util.require((db.task_extra(current).get(KEY) or {}).get("id") == value["id"],
                     "风险接受已变化，请刷新后重试", "conflict")
        db.set_extra(ctx.conn, current, ctx.now_iso, **{KEY: None})
        db.add_event(ctx.conn, ctx.now_iso, "review_risk_acceptance_revoked",
                     task_id=task["task_id"], actor="user", detail={"head_sha": value["head_sha"]})
    return {"ok": True}
