"""Read-only delivery activity, bound to the current execution identity."""

from . import core, db, git_delivery, round_store, util


def cmd_execution_activity(ctx, payload) -> dict:
    for key in ("task_id", "run_id"):
        util.require(isinstance(payload.get(key), str) and payload[key].strip(),
                     "缺少 " + key, "bad_request")
    task = db.require_task(ctx.conn, payload["task_id"])
    util.require(task["run_id"] == payload["run_id"], "执行轮次已变更", "stale_run")
    round_store.require_round(ctx.conn, payload["task_id"], payload["run_id"])
    execution = ctx.conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND run_id=? AND stage='dispatch'",
        (payload["task_id"], payload["run_id"])).fetchone()
    util.require(execution is not None, "轮次不是执行轮次", "conflict")
    util.require(core.is_pr_delivery(task), "任务未采用 PR 交付", "conflict")
    delivery = dict(core.delivery_of(task), repository=task["repository"])
    util.require(all(delivery.get(key) for key in ("worktree_path", "branch", "head_sha")),
                 "交付工作树登记不完整", "validation_failed")
    try:
        activity = git_delivery.execution_activity(
            delivery, task_id=payload["task_id"], execution_run_id=payload["run_id"])
    except git_delivery.GitDeliveryError as exc:
        # This command's boundary never exposes Git paths or repository data.
        raise util.KanpilotError("observation_blocked", "执行改动集无法核验") from exc
    fresh = db.require_task(ctx.conn, payload["task_id"])
    util.require(fresh["run_id"] == payload["run_id"]
                 and fresh["repository"] == task["repository"]
                 and core.delivery_of(fresh) == core.delivery_of(task),
                 "执行或交付身份已变更", "stale_run")
    return activity
