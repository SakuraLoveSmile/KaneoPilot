"""Project-level bounded auto-fix for hosted PRs (protocol §5.27.20).

The engine's deterministic step calls :func:`cmd_auto_fix_pr`; this module only
reads GitHub/SQLite and then starts the existing prepare + start AI-action chain
with ``authorized_by="policy"``. No model is called here.
"""
from . import (automation_policy, constants, core, db, gh_client, git_delivery, pr_auto_merge,
               project_config, util)

STATE_TITLES = {"waiting_rerun": "已重跑失败检查，等待新的检查结果",
                "started": "已按策略启动一次修复（执行端领取后才开始）",
                "suspended": "自动修复挂起",
                "filed": "评论已登记，待另立二级任务"}
CHECK_PREFIX = "检查未通过："
# 内部中性标记：没有需要处理的新评论，只记录已扫描（不对外展示）。
IDLE_STATE = "checked"
_MAX_FOLLOWUP = 8000
_FAILED_RUN = ("failure", "timed_out", "cancelled", "action_required", "startup_failure")
PUBLIC_KEYS = ("state", "reason", "attempt", "max", "head_sha", "updated_at",
               "followup", "comment")
_MARKERS = ("comments_checked_at", "comments_checked_head", "checked_notice",
            "rerun", "followup_key")


def _extra(task):
    return db.task_extra(task)


def history(task):
    return [item for item in _extra(task).get("ai_action_history") or [] if isinstance(item, dict)]


def policy_of(conn, task):
    return automation_policy.policy_for(conn, task["project_id"]) or constants.AUTOMATION_DEFAULTS


def attempt_count(task, kinds=None):
    """Attempts are derived from ai_action_history; there is no second counter."""
    kinds = set(kinds or constants.AUTO_FIX_KINDS)
    rows = [item for item in history(task)
            if item.get("authorized_by") == "policy" and item.get("kind") in kinds
            and item.get("state") != "discarded"]
    current = core.review_repair_of(task)
    if (current.get("authorized_by") == "policy" and current.get("source_kind") in kinds
            and current.get("state") != "discarded"
            and not any(item.get("action_id") == current.get("action_id") for item in rows)):
        rows.append(current)
    return len(rows)


def _check_trigger(delivery):
    return bool(pr_auto_merge.failed_checks_notice(delivery))


def _check_due(delivery):
    """Only a *new* delivery observation re-evaluates the same failing checks."""
    notice = pr_auto_merge.failed_checks_notice(delivery)
    marker = delivery.get("auto_fix") or {}
    return bool(notice) and marker.get("checked_notice") != notice.get("updated_at")


def _comments_due(delivery):
    marker = delivery.get("auto_fix") or {}
    sync = util.parse_ts(delivery.get("last_sync_at"))
    if not sync:
        return False  # 等一次真实交付观测，避免发布后立刻扫描
    if marker.get("comments_checked_head") != delivery.get("head_sha"):
        return True
    checked = util.parse_ts(marker.get("comments_checked_at"))
    return bool(not checked or checked < sync)


def watching(conn, task):
    """True when this project wants check observations for auto-fix."""
    policy = policy_of(conn, task)
    return bool(policy.get("enabled") and policy.get("auto_fix_pr")
                and "check_failure" in (policy.get("auto_fix_kinds") or ()))


def candidate(conn, task):
    """Cheap DB-only filter for the engine step (no network, no writes)."""
    policy = policy_of(conn, task)
    if not policy.get("enabled") or not policy.get("auto_fix_pr"):
        return False
    delivery = core.delivery_of(task)
    if (delivery.get("mode") != "pr" or delivery.get("pr_state") != "OPEN"
            or not isinstance(delivery.get("pr_number"), int)
            or not delivery.get("head_sha") or delivery.get("pushed_sha") != delivery.get("head_sha")):
        return False
    kinds = set(policy.get("auto_fix_kinds") or ())
    if "check_failure" in kinds and _check_due(delivery):
        return True
    return "review_comment" in kinds and _comments_due(delivery)


def require_policy_start(conn, task, value, policy_revision, now):
    """Re-check policy, budget, pause and attempts while the writer lock is held."""
    policy = policy_of(conn, task)
    kinds = list(policy.get("auto_fix_kinds") or ())
    util.require(policy.get("enabled") and policy.get("auto_fix_pr") is True,
                 "项目自动修复已关闭", "conflict")
    util.require(policy.get("policy_revision") == policy_revision,
                 "自动修复策略已变化，请重新观测", "conflict")
    util.require(value.get("source_kind") in kinds, "该触发类型已不在自动修复范围内", "conflict")
    util.require(attempt_count(task, kinds) < int(policy.get("auto_fix_max_attempts") or 0),
                 "自动修复次数已用尽", "conflict")
    util.require(core.automation_budget(task, policy, now)["ok"], "本任务额度已用尽", "conflict")
    util.require(not db.paused(conn, "dispatch") and not db.paused(conn, "delivery")
                 and not db.get_setting(conn, "paused_global", False)
                 and db.task_schedulable(task) and not db.task_auto_paused(task),
                 "已暂停，未启动自动修复", "conflict")


def _durable_reason(conn, task, policy, now):
    """Durable suspends; transient in-flight work is reported as a plain skip."""
    if db.get_setting(conn, "paused_global", False):
        return "全局暂停中"
    if db.paused(conn, "dispatch") or db.paused(conn, "delivery"):
        return "自动推进已暂停"
    if not db.task_schedulable(task) or db.task_auto_paused(task):
        return "任务已暂停或不可调度"
    if core.automation_of(task).get("manual_takeover"):
        return "任务已转为人工处理"
    _, _, mappings, _ = project_config.load_raw_mappings(conn)
    mapping = mappings.get(task["project_id"]) or {}
    if not mapping.get("enabled", True) or mapping.get("repository") != task["repository"]:
        return "项目仓库映射已变化或停用"
    kinds = list(policy.get("auto_fix_kinds") or ())
    used, maximum = attempt_count(task, kinds), int(policy.get("auto_fix_max_attempts") or 0)
    if used >= maximum:
        return "自动修复次数已用尽（%d/%d）" % (used, maximum)
    if not core.automation_budget(task, policy, now)["ok"]:
        return "本任务时间或费用额度已用尽"
    current = core.review_repair_of(task)
    if current.get("state") in ("queued", "running", "awaiting_review"):
        if current.get("authorized_by") == "policy":
            return None  # 本轮自动修复正在跑，读侧已由投影显示 started
        return "已有 AI 处理在进行"
    return None


def _in_flight(conn, task):
    return conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                        (task["task_id"],)).fetchone() is not None


def _state_value(conn, task, delivery, state, reason, *, policy, marker=None, followup=None,
                 comment=None):
    value = {"state": state, "reason": reason, "attempt": attempt_count(task, policy.get("auto_fix_kinds")),
             "max": int(policy.get("auto_fix_max_attempts") or 0),
             "head_sha": delivery.get("head_sha")}
    previous = delivery.get("auto_fix") or {}
    for key in _MARKERS:
        if key in previous:
            value[key] = previous[key]
    value.update(marker or {})
    if followup is not None:
        value["followup"] = followup
        value["followup_key"] = followup.get("dedupe_key")
    if comment is not None:
        value["comment"] = comment
    return value


def _store(ctx, task, value):
    """Write the read-side receipt; identical observations stay silent."""
    previous = (core.delivery_of(task).get("auto_fix") or {})
    strip = lambda item: {key: value2 for key, value2 in item.items() if key != "updated_at"}
    if previous and strip(previous) == strip(value):
        return {"ok": True, "outcome": "unchanged", "state": value["state"],
                "reason": value["reason"]}
    value = dict(value, updated_at=ctx.now_iso)
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task["task_id"])
        util.require(core.delivery_of(fresh).get("head_sha") == value.get("head_sha"),
                     "交付提交已变化，未写入自动修复回执", "conflict")
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, auto_fix=value)
        if previous.get("state") != value["state"] or previous.get("reason") != value["reason"]:
            db.add_event(ctx.conn, ctx.now_iso, "auto_fix_state_changed", task_id=task["task_id"],
                         actor="automation",
                         detail={"state": value["state"], "reason": value["reason"][:200],
                                 "attempt": value.get("attempt"), "max": value.get("max"),
                                 "head_sha": value.get("head_sha")})
    return {"ok": True, "outcome": "recorded", "state": value["state"],
            "reason": value["reason"]}


def _marker(delivery, **fields):
    return {**{key: (delivery.get("auto_fix") or {}).get(key) for key in _MARKERS
               if key in (delivery.get("auto_fix") or {})}, **fields}


def _consumed(delivery, **fields):
    """Consume the current delivery observation; a new notice is required to retry.

    Every path that already did GitHub I/O must consume it, otherwise the engine
    would re-read the same observation every tick.
    """
    notice = pr_auto_merge.failed_checks_notice(delivery)
    return _marker(delivery, checked_notice=notice.get("updated_at"), **fields)


def _runs_of(repo, pr):
    """Current failing workflow runs of this PR head (run_id + attempt)."""
    from . import task_ai_check_failure
    failing = task_ai_check_failure._failing(pr, repo)
    runs, seen = [], set()
    for item in failing:
        if not item["run_id"] or item["run_id"] in seen:
            continue
        seen.add(item["run_id"])
        run = _run(repo, item["run_id"])
        runs.append({"run_id": item["run_id"], "attempt": run.get("run_attempt")})
    return runs


def _run(repo, run_id):
    from . import release_github
    run = release_github.run(repo, run_id)
    util.require(isinstance(run, dict) and run.get("head_sha"),
                 "GitHub 运行不可确认", "remote_unavailable")
    return run


def _rerun_outcome(repo, runs):
    """(state, detail): wait | failed | fixed."""
    if not runs:
        return "fixed", {}
    for item in runs:
        run = _run(repo, item["run_id"])
        attempt = run.get("run_attempt")
        if type(attempt) is not int or attempt <= item.get("attempt", -1):
            return "wait", {"run_id": item["run_id"], "attempt": attempt}
        if run.get("status") != "completed":
            return "wait", {"run_id": item["run_id"], "attempt": attempt}
        if run.get("conclusion") in _FAILED_RUN:
            return "failed", {"run_id": item["run_id"], "attempt": attempt}
    return "fixed", {}


def _identity(task, delivery, pr, repo):
    util.require(pr.get("number") == delivery.get("pr_number") and pr.get("state") == "OPEN"
                 and pr.get("isCrossRepository") is False
                 and pr.get("headRefOid") == delivery.get("head_sha")
                 and pr.get("headRefName") == delivery.get("branch")
                 and pr.get("baseRefName") == delivery.get("base_branch")
                 and pr.get("url") == "https://github.com/" + repo + "/pull/" + str(delivery["pr_number"]),
                 "PR 已变化，未执行自动修复", "conflict")


def _observed_identity(delivery, observed, repo):
    util.require(observed.get("repo") == repo and observed.get("number") == delivery.get("pr_number")
                 and observed.get("state") == "OPEN"
                 and observed.get("head_sha") == delivery.get("head_sha"),
                 "PR 评论所属提交已变化，未执行自动修复", "conflict")


def _check_flow(ctx, task, delivery, policy, repo):
    from . import cmd_rerun_checks, task_ai_check_failure, task_ai_action
    pr = gh_client.pr_view(repo, delivery["pr_number"], merge_checks=True)
    _identity(task, delivery, pr, repo)
    if pr.get("mergeable") == "CONFLICTING":
        return _store(ctx, task, _state_value(ctx.conn, task, delivery, "suspended",
                      "PR 存在合并冲突，需手动处理一次性合并冲突", policy=policy,
                      marker=_consumed(delivery)))
    marker = delivery.get("auto_fix") or {}
    rerun = marker.get("rerun") if marker.get("head_sha") == delivery["head_sha"] else None
    if not isinstance(rerun, dict):
        try:
            runs = _runs_of(repo, pr)
        except util.KanpilotError:
            return {"ok": True, "outcome": "skipped", "reason": "当前 PR 没有失败的检查"}
        if runs:
            # One model-free rerun per head; receipts keep it exactly-once per run/attempt.
            cmd_rerun_checks.cmd_rerun_checks(ctx, {"task_id": task["task_id"]}, actor="automation")
            value = _state_value(ctx.conn, task, delivery, "waiting_rerun",
                                 "已重跑失败检查，等待新的检查结果", policy=policy,
                                 marker=_consumed(delivery,
                                                  rerun={"head_sha": delivery["head_sha"],
                                                         "runs": runs, "at": ctx.now_iso}))
            return _store(ctx, task, value)
        rerun = {"head_sha": delivery["head_sha"], "runs": [], "at": ctx.now_iso}
    if rerun.get("runs"):
        outcome, _detail = _rerun_outcome(repo, rerun["runs"])
        if outcome == "wait":
            return _store(ctx, task, _state_value(ctx.conn, task, delivery, "waiting_rerun",
                          "已重跑失败检查，等待新的检查结果", policy=policy,
                          marker=_consumed(delivery)))
        if outcome == "fixed":
            return {"ok": True, "outcome": "skipped", "reason": "重跑后检查已通过"}
    # Consume this observation before the (possibly failing) prepare so the next
    # tick does not re-read GitHub for the same notice.
    previous = delivery.get("auto_fix") or {}
    _store(ctx, task, _state_value(ctx.conn, task, delivery, previous.get("state") or IDLE_STATE,
                                  previous.get("reason") or "准备启动检查修复", policy=policy,
                                  marker=_consumed(delivery)))
    value = task_ai_check_failure.prepare(ctx, task)
    task_ai_action.start_policy(ctx, task, value, policy_revision=policy["policy_revision"])
    return {"ok": True, "outcome": "started", "kind": "check_failure",
            "action_id": value["action_id"]}


def handled_comment_ids(task):
    from . import task_ai_discard
    result = set()
    for item in history(task):
        if (item.get("kind") == "review_comment" and item.get("state") != task_ai_discard.STATE):
            result.update(item.get("comment_ids") or [])
    current = core.review_repair_of(task)
    if current.get("kind") == "review_comment":
        result.update(current.get("comment_ids") or [])
    return result


def _trusted_authors(policy):
    authors = {str(login).lower() for login in (policy.get("auto_fix_trusted_authors") or ())}
    authors.add(gh_client.viewer_login().lower())
    return sorted(authors)


def _comment_flow(ctx, task, delivery, policy, repo):
    from . import task_ai_action, task_ai_review_comment
    observed = gh_client.pr_comments(repo, delivery["pr_number"])
    _observed_identity(delivery, observed, repo)
    trusted = _trusted_authors(policy)
    handled = handled_comment_ids(task)
    accepted, filed = task_ai_review_comment.select(
        observed, trusted_authors=trusted,
        newer_than=_extra(task).get("review_registered_at"), skip_ids=handled)
    marker = _marker(delivery, comments_checked_at=delivery.get("last_sync_at") or ctx.now_iso,
                     comments_checked_head=delivery.get("head_sha"))
    if accepted:
        previous = delivery.get("auto_fix") or {}
        _store(ctx, task, _state_value(ctx.conn, task, delivery,
                                      previous.get("state") if previous.get("state") in constants.AUTO_FIX_STATES else IDLE_STATE,
                                      previous.get("reason") or "准备处理新评论", policy=policy, marker=marker))
        value = task_ai_review_comment.prepare(ctx, task, observed=observed,
                                               trusted_authors=trusted, skip_ids=handled)
        task_ai_action.start_policy(ctx, task, value, policy_revision=policy["policy_revision"])
        return {"ok": True, "outcome": "started", "kind": "review_comment",
                "action_id": value["action_id"], "comments": value["comment_ids"]}
    if filed:
        item = filed[0]
        followup = {"dedupe_key": "pr-comment:" + str(item.get("id")),
                    "title": "处理 PR 评论：" + str(item.get("author") or "未知") + " 的评论",
                    "description": ("来自 " + str(item.get("url")) + "\n\n"
                                    + str(item.get("body") or "")[:_MAX_FOLLOWUP])}
        comment = {"id": item.get("id"), "author": item.get("author"), "url": item.get("url"),
                   "kind": item.get("kind")}
        value = _state_value(ctx.conn, task, delivery, "filed",
                             "评论不在可自动修复范围（已过期或早于当前评审），建议另立二级任务",
                             policy=policy, marker=marker, followup=followup, comment=comment)
        return _store(ctx, task, value)
    previous = delivery.get("auto_fix") or {}
    state = previous.get("state")
    if state not in constants.AUTO_FIX_STATES:
        state, reason = IDLE_STATE, "没有需要处理的新评论"
    else:
        reason = previous.get("reason") or "没有需要处理的新评论"
    return _store(ctx, task, _state_value(ctx.conn, task, delivery, state, reason,
                                         policy=policy, marker=marker))


def cmd_auto_fix_pr(ctx, payload):
    """``auto-fix-pr {task_id, expected_head_sha, expected_policy_revision}``.

    Deterministic: no model call happens here. The model turn starts only
    through ``task_ai_action.start_policy`` after prepare succeeds.
    """
    from . import task_ai_action
    task = db.require_task(ctx.conn, payload["task_id"])
    policy = policy_of(ctx.conn, task)
    if not policy.get("enabled") or not policy.get("auto_fix_pr"):
        return {"ok": True, "outcome": "skipped", "reason": "自动修复未开启"}
    delivery = core.delivery_of(task)
    util.require(core.is_automation_managed(task) and task["status"] == "in_review",
                 "任务不在可自动修复状态", "conflict")
    util.require(delivery.get("mode") == "pr" and delivery.get("pr_state") == "OPEN"
                 and isinstance(delivery.get("pr_number"), int)
                 and delivery.get("head_sha") and delivery.get("pushed_sha") == delivery.get("head_sha")
                 and delivery.get("head_sha") == payload.get("expected_head_sha"),
                 "交付身份已变化，未执行自动修复", "conflict")
    revision = payload.get("expected_policy_revision")
    util.require(isinstance(revision, int) and not isinstance(revision, bool),
                 "缺少自动修复策略版本", "bad_request")
    if revision != policy.get("policy_revision"):
        return _store(ctx, task, _state_value(ctx.conn, task, delivery, "suspended",
                      "自动化策略已更新，等待下一次观测", policy=policy))
    kinds = set(policy.get("auto_fix_kinds") or ())
    check_ready = "check_failure" in kinds and _check_due(delivery)
    comments_due = "review_comment" in kinds and _comments_due(delivery)
    if not check_ready and not comments_due:
        return {"ok": True, "outcome": "skipped", "reason": "没有新的触发"}
    reason = _durable_reason(ctx.conn, task, policy, ctx.now)
    if reason:
        return _store(ctx, task, _state_value(ctx.conn, task, delivery, "suspended", reason,
                                             policy=policy))
    if _in_flight(ctx.conn, task):
        return {"ok": True, "outcome": "skipped", "reason": "请求在途，稍后再评估"}
    repo = git_delivery.require_github_origin(task["repository"])
    try:
        if check_ready:
            return _check_flow(ctx, task, delivery, policy, repo)
        return _comment_flow(ctx, task, delivery, policy, repo)
    except gh_client.GhError as exc:
        raise util.KanpilotError("remote_unavailable", exc.message) from exc
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", exc.message) from exc


def project_delivery(task, delivery):
    """Read-side projection (protocol §5.27.20) for get-task / list-tasks."""
    current = core.review_repair_of(task)
    value = delivery.get("auto_fix")
    if (current.get("authorized_by") == "policy"
            and current.get("source_kind") in constants.AUTO_FIX_KINDS
            and current.get("state") in ("queued", "running", "awaiting_review")):
        marker = value if isinstance(value, dict) else {}
        projected = {"state": "started", "state_title": STATE_TITLES["started"],
                     "reason": "已创建一次修复请求，执行端领取后才会开始",
                     "attempt": attempt_count(task),
                     "max": (marker.get("max") if isinstance(marker, dict) else None),
                     "head_sha": current.get("head_sha") or delivery.get("head_sha"),
                     "updated_at": marker.get("updated_at") if isinstance(marker, dict) else None}
        return dict(delivery, auto_fix=projected)
    if (not isinstance(value, dict) or value.get("state") not in constants.AUTO_FIX_STATES
            or value.get("head_sha") != delivery.get("head_sha")):
        clean = dict(delivery)
        clean.pop("auto_fix", None)
        return clean
    projected = {key: value.get(key) for key in PUBLIC_KEYS if key in value}
    projected["state_title"] = STATE_TITLES.get(value.get("state"), "自动修复状态未知")
    return dict(delivery, auto_fix=projected)
