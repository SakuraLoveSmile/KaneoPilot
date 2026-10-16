"""Bounded, user-requested withdrawal of an unconsumed engine remediation."""
from __future__ import annotations

import time

from . import automation_policy, bridge, constants, core, db, engine_claim_scope, remote, round_store, util
from .engine import ladder, verdict

TASK_BEFORE_KEYS = ("status", "stage", "failure_code", "failure_stage", "failure_reason",
                    "blocked_at", "blocked_summary", "sent", "send_state", "sent_at", "lock_retained")
AUTOMATION_BEFORE_KEYS = ("plan_wanted", "plan_attempts", "replan_requested_at",
                          "replan_remote_status", "replan_worktree_sha256", "last_skip")
BINDING_KEYS = ("task_id", "execution_run_id", "plan_sha256", "commands_sha256",
                "worktree_path", "head_sha", "branch", "source_sha256")


def history(task, *, active=False):
    rows = db.task_extra(task).get("engine_fix_rounds")
    return [row for row in rows if isinstance(row, dict) and (not active or not row.get("reverted_at"))] if isinstance(rows, list) else []


def before(task):
    return {"task": {key: task[key] for key in TASK_BEFORE_KEYS},
            "delivery": core.delivery_of(task),
            "automation": {key: core.automation_of(task).get(key) for key in AUTOMATION_BEFORE_KEYS}}


def summary(conn, task):
    if not core.is_automation_managed(task):
        return None
    policy = automation_policy.policy_for(conn, task["project_id"]) or constants.AUTOMATION_DEFAULTS
    return {"used": len(history(task, active=True)), "max": min(ladder.MAX_FIX_ROUNDS, policy["max_fix_rounds"])}


def candidate(conn, task):
    """UI/transaction guard: database and in-memory data only, never Git or stat."""
    if (not db.task_is_active(task) or not core.is_automation_managed(task)
            or engine_claim_scope.effective_mode(conn, task["project_id"]) != "live"
            or task["status"] != "needs_user" or task["failure_stage"] != "plan"
            or task["failure_code"] not in ("branch_exists", "delivery_base_unknown")):
        return None
    rows = history(task, active=True)
    if not rows:
        return None
    row = rows[-1]
    receipt = db.task_extra(task).get("engine_verdict") or {}
    binding = receipt.get("binding")
    if (row.get("reason") != "verify_failed" or not row.get("at")
            or row.get("execution_run_id") != task["run_id"] or row.get("plan_sha256") != task["plan_sha256"]
            or receipt.get("execution_run_id") != task["run_id"] or receipt.get("plan_sha256") != task["plan_sha256"]
            or not isinstance(binding, dict) or not all(binding.get(key) for key in BINDING_KEYS)
            or binding["task_id"] != task["task_id"] or binding["execution_run_id"] != task["run_id"]
            or binding["plan_sha256"] != task["plan_sha256"]):
        return None
    at = util.parse_ts(row["at"])
    generated = util.parse_ts(task["plan_generated_at"])
    if at is None or (generated is not None and generated > at):
        return None
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
                    (task["task_id"],)).fetchone():
        return None
    # Parse legacy Z/+00:00 timestamps before comparing; lexical ordering is not chronological.
    for query in ("SELECT created_at FROM requests WHERE task_id=? AND kind='plan'",
                  "SELECT created_at FROM planner_inputs WHERE task_id=?"):
        for record in conn.execute(query, (task["task_id"],)):
            created = util.parse_ts(record["created_at"])
            if created is None or created >= at:
                return None
    return {"execution_run_id": task["run_id"], "plan_sha256": task["plan_sha256"],
            "expected_failure_code": task["failure_code"]}


def _conflict(message):
    raise util.KanpilotError("conflict", message)


def _restore_material(conn, task):
    row = history(task, active=True)[-1]
    saved = row.get("before")
    binding = (db.task_extra(task).get("engine_verdict") or {})["binding"]
    if saved is not None:
        if (not isinstance(saved, dict) or not isinstance(saved.get("task"), dict)
                or not all(key in saved["task"] for key in TASK_BEFORE_KEYS)
                or not isinstance(saved.get("delivery"), dict) or not isinstance(saved.get("automation"), dict)):
            _conflict("补救前快照不完整，不能安全撤回")
        return saved["delivery"], saved["task"]["sent_at"], saved["task"]["lock_retained"], saved["automation"].get("plan_attempts", 0)
    dispatched = None
    for event in conn.execute("SELECT at,detail_json FROM events WHERE task_id=? AND kind='dispatch_sent' ORDER BY id DESC", (task["task_id"],)):
        if (util.jload(event["detail_json"]) or {}).get("run_id") == task["run_id"]:
            dispatched = event["at"]
            break
    if not dispatched:
        _conflict("缺少原执行轮次的发送证据，不能安全撤回")
    if binding["branch"] == "HEAD":
        delivery = {"mode": "manual"}
    else:
        delivery = core.delivery_of(task)
        if (not core.same_branch_fix_ready(task, binding)[0]
                or delivery.get("head_sha") != binding["head_sha"]):
            _conflict("原分支工作树归属证据不足，不能安全撤回")
    return delivery, dispatched, 0, 0


def revert(ctx, payload):
    from . import cmd_queue
    from .execution_questions import guard_active
    for key in ("expected_execution_run_id", "expected_plan_sha256"):
        if not payload.get(key):
            raise util.bad_request("撤回自动补救必须提供 " + key)
    task = db.require_task(ctx.conn, payload["task_id"])
    chosen = candidate(ctx.conn, task)
    if (chosen is None or chosen["execution_run_id"] != payload["expected_execution_run_id"]
            or chosen["plan_sha256"] != payload["expected_plan_sha256"]):
        _conflict("补救已被消费或任务、执行轮次、计划已变化，不能撤回")
    snapshot = (cmd_queue._resolution_snapshot(task), tuple(task[key] for key in TASK_BEFORE_KEYS))
    execution_round = round_store.get_round(ctx.conn, task["task_id"], task["run_id"])
    round_snapshot = dict(execution_round) if execution_round is not None else None
    delivery, sent_at, retained, attempts = _restore_material(ctx.conn, task)
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
    except (bridge.BridgeError, util.KanpilotError) as exc:
        _conflict("远端任务不可核验，未撤回：" + util.redact(str(exc)))
    current = live.get("task") or {}
    if (current.get("id") != task["task_id"] or current.get("projectId") != task["project_id"]
            or current.get("status") not in ("in-progress", "in-review", "done")):
        _conflict("远端任务项目或状态已变化，不能撤回")
    restored = dict(task)
    restored["extra_json"] = util.jdump(dict(db.task_extra(task), delivery=delivery))
    try:
        actual = verdict.binding(ctx.conn, restored, deadline=time.monotonic() + automation_policy.git_probe_timeout())
    except (util.KanpilotError, OSError, verdict.git_delivery.GitDeliveryError) as exc:
        _conflict("原计划或执行工作树不可核验，未撤回：" + util.redact(str(exc)))
    receipt = db.task_extra(task)["engine_verdict"]
    if actual != receipt["binding"]:
        _conflict("原计划或执行工作树已变化，不能撤回")
    reasons = verdict.environment_reasons(receipt.get("checks"))
    code = "verification_environment_failure" if reasons else "verification_unconfirmed"
    detail = verdict.environment_detail(reasons) if reasons else "验证未全部通过，请查看各条独立退出码与输出"
    now, actor = ctx.now_iso, payload.get("actor", "user")
    resolution = {"action": "revert_engine_fix", "note": payload.get("note"), "at": now, "actor": actor}
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task["task_id"])
        guard_active(fresh)
        current_round = round_store.get_round(ctx.conn, fresh["task_id"], fresh["run_id"])
        if (snapshot != (cmd_queue._resolution_snapshot(fresh), tuple(fresh[key] for key in TASK_BEFORE_KEYS))
                or candidate(ctx.conn, fresh) != chosen
                or (dict(current_round) if current_round is not None else None) != round_snapshot):
            _conflict("准备撤回期间任务或补救记录已变化")
        rows = history(fresh)
        active_index = max(index for index, row in enumerate(rows) if not row.get("reverted_at"))
        rows[active_index] = dict(rows[active_index], reverted_at=now, reverted_by=actor)
        automation = core.automation_of(fresh)
        automation.update(plan_attempts=attempts, plan_wanted=False)
        for key in ("replan_requested_at", "replan_remote_status", "replan_worktree_sha256", "last_skip"):
            automation.pop(key, None)
        old_delivery = core.delivery_of(fresh)
        orphan = ({key: old_delivery.get(key) for key in ("branch", "worktree_path")}
                  if any(old_delivery.get(key) != delivery.get(key) for key in ("branch", "worktree_path"))
                  and any(old_delivery.get(key) for key in ("branch", "worktree_path")) else None)
        db.set_extra(ctx.conn, fresh, now, delivery=delivery, automation=automation, engine_fix_rounds=rows)
        core.transition_task(ctx.conn, fresh, "needs_user", now, actor,
                             stage="monitor", failure_stage="monitor", failure_code=code,
                             failure_reason=detail, blocked_summary=detail, blocked_at=now,
                             sent=1, send_state="sent", sent_at=sent_at, lock_retained=retained,
                             resolution=resolution)
        db.add_event(ctx.conn, now, "engine_fix_reverted", task_id=task["task_id"], actor=actor,
                     detail=dict(chosen, restored_failure_code=code, environment_reasons=reasons, orphaned_branch=orphan))
        db.add_event(ctx.conn, now, "resolved", task_id=task["task_id"], actor=actor, detail=resolution)
    return {"ok": True, "task_status": "needs_user", "request": None}
