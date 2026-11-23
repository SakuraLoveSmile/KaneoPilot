"""One read-only observation of a task's PR delivery for the desktop detail pane.

The「交付」tab needs the current PR state the moment it opens, while a delivery
sync would write receipts and reschedule polling. This command only reads: the
stored delivery, at most one `gh pr view`, and the merge gate quoted verbatim from
pr_auto_merge.eligibility. It never merges, retries, reruns or writes SQLite.
"""
import re

from . import automation_policy, core, db, gh_client, git_delivery, pr_auto_merge, util

READY_REASON = "检查与保护规则已满足，等待下一轮交付同步合并"
_PASSING_CONCLUSIONS = ("SUCCESS", "NEUTRAL", "SKIPPED")
_PENDING_STATES = ("PENDING", "EXPECTED")
_STATE_ORDER = {"failed": 0, "running": 1, "unknown": 2, "passed": 3}
_UNNAMED = "未命名检查"
_NAME_LIMIT, _WORKFLOW_LIMIT, _TEXT_LIMIT = 120, 80, 200
# 只链接本仓库的 Actions 页面；其它形态（含 query）一律 null，与 task_ai_check_failure 同一口径。
_RUN_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/([0-9]+)(?:/job/([0-9]+))?$")
_PR_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$")


def _text(value, limit=_TEXT_LIMIT):
    """Redacted, bounded free text; odd types become null instead of raising."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return gh_client.redact(str(value))[:limit] or None


def _stamp(value):
    """A check time; GitHub reports "0001-01-01T00:00:00Z" for a not-yet-finished check."""
    text = _text(value)
    if text is None:
        return None
    text = text.strip()
    return None if not text or text.startswith("0001-01-01") else text


def _pr_number(value):
    return value if type(value) is int and value > 0 else None


def _pr_url(value):
    """Only a verified GitHub PR page is linkable; anything else is not a URL."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _PR_URL.match(value) else None


def _notice(value):
    if not isinstance(value, dict) or not value:
        return None
    return {"state": _text(value.get("state")) or "unknown",
            "reason": _text(value.get("reason")) or "",
            "head_sha": _text(value.get("head_sha")) or "",
            "updated_at": _text(value.get("updated_at"))}


def _delivery_view(ctx, task, delivery):
    """The stored projection only; a missing policy means auto merge is off."""
    policy = automation_policy.policy_for(ctx.conn, task["project_id"]) or {}
    head = _text(delivery.get("head_sha"))
    notice = _notice(delivery.get("auto_merge"))
    return {"mode": _text(delivery.get("mode")),
            "pr_number": _pr_number(delivery.get("pr_number")),
            "pr_url": _pr_url(delivery.get("pr_url")),
            "pr_state": _text(delivery.get("pr_state")),
            "branch": _text(delivery.get("branch")),
            "base_branch": _text(delivery.get("base_branch")),
            "head_sha": head,
            "merged_at": _text(delivery.get("merged_at")),
            "last_sync_at": _text(delivery.get("last_sync_at")),
            "next_sync_at": _text(delivery.get("next_sync_at")),
            "auto_merge_enabled": policy.get("auto_merge_pr") is True,
            "notice": notice,
            "notice_matches_head": bool(notice and head and notice["head_sha"] == head)}


def _pr_view(pr, delivery):
    head = pr.get("headRefOid") if isinstance(pr.get("headRefOid"), str) else ""
    stored = delivery.get("head_sha") if isinstance(delivery.get("head_sha"), str) else None
    return {"state": _text(pr.get("state")) or "UNKNOWN",
            "is_draft": pr.get("isDraft") if type(pr.get("isDraft")) is bool else None,
            "mergeable": _text(pr.get("mergeable")),
            "merge_state_status": _text(pr.get("mergeStateStatus")),
            "review_decision": _text(pr.get("reviewDecision")),
            "head_sha": _text(head) or "",
            "base_ref_name": _text(pr.get("baseRefName")),
            "url": _pr_url(pr.get("url")) or "",
            "head_matches_delivery": bool(head) and head == stored}


def _check_state(raw):
    typename = raw.get("__typename")
    if typename == "CheckRun":
        if raw.get("status") != "COMPLETED":
            return "running"
        return "passed" if raw.get("conclusion") in _PASSING_CONCLUSIONS else "failed"
    if typename == "StatusContext":
        if raw.get("state") in _PENDING_STATES:
            return "running"
        return "passed" if raw.get("state") == "SUCCESS" else "failed"
    return "unknown"


def _run_url(value, repo):
    if not repo or not isinstance(value, str):
        return None
    value = value.strip()
    match = _RUN_URL.match(value)
    return value if match and match.group(1) == repo else None


def _check_item(raw, repo):
    """Unknown or malformed entries stay visible as an unknown check, never an error."""
    if not isinstance(raw, dict):
        return {"name": _UNNAMED, "workflow": None, "state": "unknown", "conclusion": None,
                "url": None, "started_at": None, "completed_at": None}
    # StatusContext has no `name`: the required context name is the branch check name.
    name = raw.get("name") or raw.get("context")
    return {"name": _text(name, _NAME_LIMIT) or _UNNAMED,
            "workflow": _text(raw.get("workflowName"), _WORKFLOW_LIMIT),
            "state": _check_state(raw),
            "conclusion": (_text(raw.get("conclusion"))
                           if raw.get("__typename") == "CheckRun" else None),
            "url": _run_url(raw.get("detailsUrl") or raw.get("targetUrl"), repo),
            "started_at": _stamp(raw.get("startedAt")),
            "completed_at": _stamp(raw.get("completedAt"))}


def _newest(entries):
    """completedAt first, then startedAt, then the entry GitHub listed first."""
    chosen, stamp = entries[0], None
    for item in entries:
        current = util.parse_ts(item["completed_at"]) or util.parse_ts(item["started_at"])
        if current is not None and (stamp is None or current > stamp):
            chosen, stamp = item, current
    return chosen


def _latest(entries):
    """The round a checks tab shows for one name.

    A round that is still running is the current one: GitHub can list a superseded
    round whose completion time is later than the current round's start, so raw
    timestamps alone would hide the run the user is watching. Within one kind,
    completedAt, then startedAt, then list order decides.
    """
    running = [item for item in entries if item["state"] == "running"]
    return _newest(running or entries)


def _checks(pr, repo):
    """Group check runs per (workflow, name); the summary counts the grouped view."""
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return None, None  # 未读到检查结果，不冒充「没有检查」
    groups = {}
    for raw in rollup:
        item = _check_item(raw, repo)
        groups.setdefault((item["workflow"] or "", item["name"]), []).append(item)
    checks = []
    for entries in groups.values():
        latest = dict(_latest(entries))
        latest["superseded_runs"] = len(entries) - 1
        checks.append(latest)
    checks.sort(key=lambda item: (_STATE_ORDER[item["state"]], item["workflow"] or "", item["name"]))
    summary = {"total": len(checks), "passed": 0, "failed": 0, "running": 0, "unknown": 0}
    for item in checks:
        summary[item["state"]] += 1
    return checks, summary


def _gate(pr):
    """Quote the runner's own allow-list instead of deriving a second verdict."""
    blocked = pr_auto_merge.eligibility(pr)
    if not blocked:
        return {"state": "ready", "reason": READY_REASON, "ready": True}
    state, reason = blocked
    return {"state": _text(state) or "unknown", "reason": _text(reason) or "", "ready": False}


def cmd_get_delivery_status(ctx, payload, *, reader=None) -> dict:
    """Read one task's delivery/PR state: no SQLite write, no GitHub write, no sync."""
    task = db.require_task(ctx.conn, payload["task_id"])
    delivery = core.delivery_of(task)
    result = {"task_id": task["task_id"], "observed_at": ctx.now_iso, "live": False,
              "error": None, "delivery": _delivery_view(ctx, task, delivery),
              "pr": None, "gate": None, "checks": None, "checks_summary": None}
    number = _pr_number(delivery.get("pr_number"))
    # 非 PR 交付、尚未开 PR、已合并或已关闭：没有可读的现场，这不是读取失败。
    if (not core.is_pr_delivery(task) or number is None or delivery.get("merged_at")
            or delivery.get("pr_state") in ("MERGED", "CLOSED")):
        return result
    try:
        repo = git_delivery.require_github_origin(task["repository"])
        pr = (reader or gh_client.pr_view)(repo, number, merge_checks=True)
    except gh_client.GhError as exc:
        return {**result, "error": {"code": _text(exc.kind) or "gh_failed",
                                    "message": _text(exc.message) or ""}}
    except git_delivery.GitDeliveryError as exc:
        return {**result, "error": {"code": _text(exc.code) or "git_failed",
                                    "message": _text(exc.message) or ""}}
    if not isinstance(pr, dict):
        return {**result, "error": {"code": "transient", "message": "GitHub PR 状态不可读取"}}
    checks, summary = _checks(pr, repo)
    return {**result, "live": True, "pr": _pr_view(pr, delivery), "gate": _gate(pr),
            "checks": checks, "checks_summary": summary}
