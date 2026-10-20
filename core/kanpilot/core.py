"""Shared state-transition helpers used by multiple command modules."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import constants, db, util


def stage_for_status(status: str, fallback: Optional[str] = None) -> str:
    if status in ("needs_plan", "planning"):
        return "plan"
    if status in ("ready",) + constants.DISPATCH_MID_STATUSES:
        return "dispatch"
    if status == "running":
        return "monitor"
    if status == "in_review":
        return "review"
    if status == "done":
        return "done"
    return fallback or "plan"  # needs_user keeps its current stage


def transition_task(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    new_status: str,
    now: str,
    actor: Optional[str],
    detail: Any = None,
    keep_stage: bool = False,
    **extra_fields: Any,
) -> None:
    """Write task.status (+derived stage), emit status_change event."""
    old = task["status"]
    fields: dict[str, Any] = {"status": new_status}
    if not (keep_stage or new_status == "needs_user"):
        fields["stage"] = stage_for_status(new_status, task["stage"])
    fields.update(extra_fields)
    db.update_task(conn, task["task_id"], now, **fields)
    event_detail = {"from": old, "to": new_status, "actor": actor}
    if detail:
        event_detail.update(detail)
    db.add_event(
        conn, now, "status_change", task_id=task["task_id"], actor=actor,
        detail=event_detail,
    )


def clear_block_fields() -> dict:
    return {
        "failure_code": None, "failure_reason": None, "failure_stage": None,
        "blocked_at": None, "blocked_summary": None,
        "manual_recovery_required": 0,
    }


def block_task(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    now: str,
    reason: Optional[str],
    code: Optional[str],
    actor: Optional[str],
    stage: Optional[str] = None,
    **extra_fields: Any,
) -> None:
    """Move task to needs_user recording failure_* fields (keeps stage)."""
    fields = clear_block_fields()
    fields.update(
        {
            "failure_reason": reason,
            "failure_code": code,
            "failure_stage": stage if stage is not None else constants.AUTOMATION_FAILURE_STAGES.get(code, task["status"]),
            "blocked_at": now,
            "blocked_summary": reason,
        }
    )
    fields.update(extra_fields)
    transition_task(
        conn, task, "needs_user", now, actor, detail={"reason": reason, "code": code},
        **fields,
    )


def guard_check(task: Optional[sqlite3.Row], kind: str) -> Optional[str]:
    """protocol.md §4.4 claim guard; returns failure reason or None."""
    allowed = constants.CLAIM_GUARDS[kind]
    if allowed is None:
        return None
    if task is None:
        return f"task missing for {kind} request"
    if task["status"] not in allowed:
        return (
            f"task {task['task_id']} status {task['status']} "
            f"not allowed for {kind} (need {'|'.join(sorted(allowed))})"
        )
    return None


def validate_evidence(
    evidence: Any,
    task: sqlite3.Row,
    expect_message: bool,
    plan_basename: str,
) -> None:
    """protocol.md §4.6 send-evidence checks (all fields from UI observation)."""
    if not isinstance(evidence, dict):
        raise util.KanpilotError("evidence_invalid", "evidence must be an object")
    def check(condition: bool, message: str) -> None:
        if not condition:
            raise util.KanpilotError("evidence_invalid", message)
    check(
        evidence.get("session_marker") == task["session_marker"],
        "session_marker not verified",
    )
    check(
        evidence.get("repository") == task["repository"],
        "repository not verified",
    )
    check(
        evidence.get("mode") == "Agent"
        and evidence.get("model") == "SWE-2 Max"
        and evidence.get("execution_mode") == "Local",
        "Agent/Local/model not verified",
    )
    check(
        evidence.get("attachment_name") == plan_basename
        and evidence.get("attachment_visible") is True,
        "attachment not verified",
    )
    check(evidence.get("instruction_visible") is True, "instruction not verified")
    if expect_message:
        check(
            evidence.get("message_visible") is True,
            "sent message not verified",
        )
    # Plan must be unchanged since claim validation.
    util.require(
        util.sha256_file(Path(task["plan_path"])) == task["plan_sha256"],
        "plan changed since dispatch validation",
        "evidence_invalid",
    )


def claimed_dispatch_request(
    conn: sqlite3.Connection, task_id: str, run_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND stage='dispatch' "
        "AND status='claimed' AND run_id=?",
        (task_id, run_id),
    ).fetchone()
    if row is None:
        raise util.KanpilotError(
            "stale_run", f"no claimed dispatch request for run {run_id}"
        )
    return row


def require_task_run(task: sqlite3.Row, run_id: str) -> None:
    if task["run_id"] != run_id:
        raise util.KanpilotError(
            "stale_run", f"task run_id mismatch (expected {task['run_id']})"
        )


def release_dispatch_serial(conn: sqlite3.Connection, run_id: Optional[str]) -> None:
    if run_id and db.get_setting(conn, "dispatch_serial", "") == run_id:
        db.set_setting(conn, "dispatch_serial", "")


def acquire_lifecycle(conn: sqlite3.Connection, owner: str) -> None:
    """Lifecycle serial bit: one remote write at a time (state-model §5)."""
    current = db.get_setting(conn, "lifecycle_owner", "") or ""
    if current and current != owner:
        raise util.KanpilotError(
            "busy", f"lifecycle serial held by {current}"
        )
    db.set_setting(conn, "lifecycle_owner", owner)


def release_lifecycle(conn: sqlite3.Connection, owner: str) -> None:
    if db.get_setting(conn, "lifecycle_owner", "") == owner:
        db.set_setting(conn, "lifecycle_owner", "")


def finish_request(
    conn: sqlite3.Connection,
    request: sqlite3.Row,
    now: str,
    final_status: str,
    outcome: str,
    result: Any = None,
    note: Optional[str] = None,
    send_state: Optional[str] = None,
    evidence: Any = None,
) -> None:
    """Terminal transition for a request + attempt closure + serial release."""
    db.update_request(
        conn, request["request_id"], now,
        status=final_status, finished_at=now,
        result_json=result if result is not None else {"outcome": outcome},
    )
    db.close_attempt(
        conn, request["request_id"], request["run_id"], now, outcome,
        send_state=send_state, evidence=evidence, note=note,
    )
    if request["stage"] == "dispatch":
        release_dispatch_serial(conn, request["run_id"])


def fail_request(
    conn: sqlite3.Connection,
    request: sqlite3.Row,
    now: str,
    reason: str,
    actor: Optional[str] = None,
    outcome: str = "failed",
) -> None:
    """Guard-failure path from protocol.md §4.2-5 (request -> failed)."""
    finish_request(
        conn, request, now, "failed", outcome,
        result={"outcome": outcome, "reason": reason}, note=reason,
    )
    db.add_event(
        conn, now, "reported", task_id=request["task_id"],
        request_id=request["request_id"], actor=actor,
        detail={"outcome": outcome, "via": "claim_guard", "reason": reason},
    )


def require_stage_identity(conn, payload, task, now, *, stages) -> Optional[sqlite3.Row]:
    """v4（protocol §5.24.5）新自动 Review 的登记归属与周期保护。

    仅在调用方给出 ``request_id`` 时生效——无身份的旧手动调用保持兼容，
    但不作为新自动链路入口。核对 request.stage=review、task_id、
    status=claimed、run_id（可选）与未过期 lease，以及任务当前执行轮次仍
    等于 ``expected_execution_run_id``（评审周期未被新 run 接管）。失配抛
    stale_run/conflict，调用方不写任何登记。
    """
    request_id = payload.get("request_id")
    if not request_id:
        return None
    request = db.get_request(conn, request_id)
    if request is None:
        raise util.KanpilotError("not_found", f"request not found: {request_id}")
    if request["stage"] not in stages:
        raise util.KanpilotError(
            "validation_failed",
            f"request {request_id} 阶段不符（stage={request['stage']}，允许={stages}）",
        )
    task_id = payload.get("task_id") or payload.get("source_task_id")
    if request["task_id"] != task_id:
        raise util.KanpilotError(
            "conflict",
            f"request {request_id} 属于任务 {request['task_id']}，"
            f"与 {task_id} 不符",
        )
    run_id = payload.get("run_id")
    if request["status"] != "claimed" or (run_id and request["run_id"] != run_id):
        raise util.KanpilotError(
            "stale_run",
            f"request {request_id} status={request['status']} "
            f"run={request['run_id']}",
        )
    if util.lease_lost(request["lease_expires_at"], now):
        raise util.KanpilotError(
            "stale_run",
            f"request {request_id} 租约已过期（{request['lease_expires_at']}）",
        )
    expected = payload.get("expected_execution_run_id")
    if expected and task is not None and task["run_id"] != expected:
        raise util.KanpilotError(
            "stale_run",
            f"任务当前执行轮次 {task['run_id']} 与本次评审对应的 {expected} "
            f"不符（评审周期已变）",
        )
    return request


def require_review_identity(conn, payload, task, now):
    request = require_stage_identity(conn, payload, task, now, stages=("review",))
    frozen = db.request_payload(request).get("review_branch_snapshot") if request is not None else None
    if task is not None and (requires_branch_review(task) or frozen is not None):
        util.require(request is not None, "分支评审必须携带领取身份", "validation_failed")
        # Already claimed historical PR reviews predate this field. Their
        # runner verifies the existing committed snapshot/materials on resume.
        if frozen is not None or delivery_of(task).get("workspace_version") == 1:
            util.require(requires_branch_review(task) and isinstance(frozen, dict)
                         and frozen == branch_review_snapshot(task),
                         "任务分支或固定评审版本已变更", "conflict")
        require_committed_review(task, verify_worktree=False)
    return request


def automation_of(task) -> dict:
    return dict(db.task_extra(task).get("automation") or {})


def review_repair_of(task) -> dict:
    extra = (task.get("extra") or {}) if isinstance(task, dict) and "extra_json" not in task else db.task_extra(task)
    value = extra.get("fix_request") or {}
    return dict(value) if isinstance(value, dict) and value.get("mode") == "direct" else {}


def delivery_of(task) -> dict:
    if isinstance(task, dict) and "extra_json" not in task:
        return dict(task.get("delivery") or (task.get("extra") or {}).get("delivery") or {})
    return dict(db.task_extra(task).get("delivery") or {})


def is_automation_managed(task) -> bool:
    value = automation_of(task)
    return bool(value) and not value.get("manual_takeover", False)


def is_pr_delivery(task) -> bool:
    return delivery_of(task).get("mode") == "pr"


def requires_branch_review(task) -> bool:
    delivery = delivery_of(task)
    return delivery.get("mode") == "pr" or delivery.get("workspace_version") == 1


FIX_BRANCH_UNAVAILABLE_DETAIL = "本轮改动在独立工作树，无法同分支修复"


def same_branch_fix_ready(task, binding=None) -> tuple:
    """Metadata-only guard. Filesystem/Git verification belongs to the caller."""
    delivery = delivery_of(task)
    reason = None
    if not requires_branch_review(task):
        reason = "detached"
    elif not delivery.get("branch") or not delivery.get("worktree_path"):
        reason = "no_branch_worktree"
    elif binding is not None and (binding.get("branch") != delivery["branch"]
                                  or binding.get("worktree_path") != delivery["worktree_path"]):
        reason = "worktree_mismatch"
    return reason is None, reason


def branch_review_snapshot(task) -> dict:
    delivery = delivery_of(task)
    return {**{key: delivery.get(key) for key in (
        "mode", "workspace_version", "branch", "base_branch", "base_sha", "head_sha", "worktree_path")},
        "execution_run_id": task["run_id"]}


def require_committed_review(task, execution_run_id=None, *, verify_worktree=True) -> None:
    """Branch Review requires this execution's clean, core-owned local commit.

    Filesystem verification belongs outside SQLite write transactions. Callers
    repeat the metadata guard inside their transaction with verify_worktree=False.
    """
    if not requires_branch_review(task):
        return
    delivery = delivery_of(task)
    run_id = execution_run_id or task["run_id"]
    util.require(run_id and task["run_id"] == run_id,
                 "分支评审执行轮次已变更", "stale_run")
    util.require(all(delivery.get(key) for key in (
        "branch", "base_branch", "base_sha", "head_sha", "worktree_path")),
        "分支评审工作区登记不完整", "validation_failed")
    util.require(any(commit.get("execution_run_id") == run_id
                     and commit.get("sha") == delivery["head_sha"]
                     for commit in delivery.get("commits") or []),
                 "本轮执行尚未完成核心本地提交，不能进入分支评审", "validation_failed")
    if verify_worktree:
        from . import git_delivery
        try:
            git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]),
                                               require_clean=True, expected_head=delivery["head_sha"])
        except git_delivery.GitDeliveryError as exc:
            raise util.KanpilotError("validation_failed", exc.message) from exc


def update_automation(conn, task, now, **changes):
    fresh = db.require_task(conn, task["task_id"])
    value = automation_of(fresh)
    value.update(changes)
    db.set_extra(conn, fresh, now, automation=value)
    return value


def update_delivery(conn, task, now, **changes):
    fresh = db.require_task(conn, task["task_id"])
    value = delivery_of(fresh)
    value.update(changes)
    db.set_extra(conn, fresh, now, delivery=value)
    return value


def is_user_replan(task):
    return (is_automation_managed(task) and is_pr_delivery(task)
            and bool(automation_of(task).get("replan_requested_at")))


def execution_retry_of(task):
    value = db.task_extra(task).get("execution_retry") or {}
    if (task["status"] in ("ready", "dispatching", "prepared", "submitting")
            and value.get("plan_sha256") == task["plan_sha256"]
            and value.get("plan_generated_at") == task["plan_generated_at"]
            and value.get("source_run_id")):
        return value
    return {}


def expected_remote_slugs(task):
    if review_repair_of(task):
        return set(constants.REVIEW_SLUGS) | {constants.IN_PROGRESS_SLUG}
    if execution_retry_of(task):
        return {constants.IN_PROGRESS_SLUG}
    slugs = set(constants.TODO_SLUGS)
    from . import review_resolution
    remediation = review_resolution.remediation_of(task)
    if (remediation and remediation.get("repository") == task["repository"]
            and remediation.get("requirements_sha256") == task["requirements_sha256"]
            and remediation.get("project_id") == task["project_id"]):
        slugs.add(remediation["remote_status"])
    if is_pr_delivery(task) and delivery_of(task).get("commits"):
        slugs.update(constants.REVIEW_SLUGS)
    if is_user_replan(task):
        accepted = automation_of(task).get("replan_remote_status")
        if accepted == constants.IN_PROGRESS_SLUG or accepted in constants.REVIEW_SLUGS:
            slugs.add(accepted)
    return slugs


def automation_budget(task, policy, now):
    a = automation_of(task)
    started = util.parse_ts(a.get("segment_started_at"))
    current = util.parse_ts(now) if isinstance(now, str) else now
    from .execution_questions import wait_seconds
    minutes = max(0, ((current - started).total_seconds() - wait_seconds(task,current,since=started)) / 60) if started else 0
    cost = max(0, float(a.get("cost_usd", 0)) - float(a.get("segment_cost_base_usd", 0)))
    limit = policy.get("max_task_cost_usd")
    exceeded = minutes >= policy.get("max_task_minutes", 240) or (limit is not None and cost >= limit)
    return {"ok": not exceeded, "exceeded": exceeded, "minutes": minutes, "cost_usd": cost,
            "remaining_cost_usd": None if limit is None else max(0, limit - cost)}


def start_fix_round_inline(conn, task, now, *, fix_request, actor="kaneo-review", new_segment=False):
    if fix_request.get("mode") == "direct":
        util.require(fix_request.get("source_kind") != "merge_conflict",
                     "合并冲突需从任务内 AI 处理入口确认", "conflict")
        from . import review_repair
        return review_repair.start_inline(conn, task, now, fix_request, actor)["review_repair"]
    a = automation_of(task)
    a.update(fix_round=int(a.get("fix_round", 0)) + 1, plan_wanted=True, plan_attempts=0,
             replan_requested_at=None, replan_remote_status=None, replan_worktree_sha256=None)
    if new_segment:
        a.update(segment_started_at=now, segment_cost_base_usd=a.get("cost_usd", 0))
    fix = dict(fix_request, state="open", fix_round=a["fix_round"])
    extra = db.task_extra(db.require_task(conn, task["task_id"]))
    extra.update(automation=a, fix_request=fix)
    for key in ("review_conclusion", "review_registered_at", "verification_override"):
        extra.pop(key, None)
    fields = clear_block_fields()
    fields.update(sent=0, send_state=None, sent_at=None, lock_retained=0,
                  review_report_path=None, review_report_sha256=None, review_key=None,
                  extra_json=extra)
    transition_task(conn, task, "needs_plan", now, actor, detail={"via": "same_task_fix"}, **fields)
    db.add_event(conn, now, "fix_round_started", task_id=task["task_id"], actor=actor,
                 detail={"fix_round": a["fix_round"], "review_key": fix.get("review_key")})
    return fix
