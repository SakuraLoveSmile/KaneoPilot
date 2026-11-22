"""Explicit rerun of the failed GitHub jobs that block a PR merge."""
from . import core, db, gh_client, git_delivery, release_github, runtime_operations, util

TOOL = "github_run_rerun"
HARNESS = "github"


def _failing_runs(task):
    """Read-only: the open PR of this task and the runs behind its failed checks."""
    from . import task_ai_check_failure
    delivery = core.delivery_of(task)
    notice = delivery.get("auto_merge") or {}
    if (delivery.get("mode") != "pr" or delivery.get("pr_state") != "OPEN"
            or not isinstance(delivery.get("pr_number"), int)):
        raise util.KanpilotError("conflict", "仅已交付到开放 PR 的任务可重跑失败的检查")
    if notice.get("state") != "blocked" or not str(notice.get("reason") or "").startswith("检查未通过："):
        raise util.KanpilotError("conflict", "当前没有可确认的失败检查，请先重新校验")
    if notice.get("head_sha") != delivery.get("head_sha"):
        raise util.KanpilotError("conflict", "合并门检查来自旧提交，请先重新校验")
    repo = git_delivery.require_github_origin(task["repository"])
    pr = gh_client.pr_view(repo, delivery["pr_number"], merge_checks=True)
    util.require(pr.get("state") == "OPEN" and pr.get("headRefOid") == delivery.get("head_sha")
                 and pr.get("headRefName") == delivery.get("branch")
                 and pr.get("url") == "https://github.com/" + repo + "/pull/" + str(delivery["pr_number"]),
                 "PR 已变化，请重新校验后再试", "conflict")
    runs = sorted({item["run_id"] for item in task_ai_check_failure._failing(pr, repo)
                   if item["run_id"]})
    util.require(runs, "当前 PR 的失败检查没有可重跑的工作流运行，请重新校验后再试", "conflict")
    return repo, delivery, runs


def cmd_rerun_checks(ctx, payload):
    """Rerun only the failed jobs of the exact run the failed checks belong to."""
    task = db.require_task(ctx.conn, payload["task_id"])
    repo, delivery, runs = _failing_runs(task)
    scope = "rerun-checks:" + task["task_id"]
    results = []
    for run_id in runs:
        run = release_github.run(repo, run_id)
        util.require(run.get("head_sha") == delivery.get("head_sha")
                     and run.get("status") == "completed"
                     and run.get("conclusion") in ("failure", "timed_out", "cancelled",
                                                    "action_required", "startup_failure"),
                     "失败的检查运行已变化，请重新校验后再试", "conflict")
        identity = {"task_id": task["task_id"], "repo": repo, "head_sha": delivery["head_sha"],
                    "run_id": run_id, "attempt": run.get("run_attempt")}
        operation_id = runtime_operations.compute_request_sha256(identity)
        previous = db.get_runtime_operation(ctx.conn, scope, operation_id)
        if previous is not None:
            # One rerun per exact run/attempt: replaying must not start another.
            results.append({"run_id": run_id, "status": previous["status"], "reused": True})
            continue
        with db.write_txn(ctx.conn):
            runtime_operations.begin_operation(ctx.conn, scope, operation_id, HARNESS, TOOL,
                                               identity, now=ctx.now_iso)
        try:
            gh_client.run_rerun_failed(repo, run_id)
        except gh_client.GhError as exc:
            # A dropped connection may still have started the rerun, so keep the
            # receipt unknown and never replay it automatically.
            status = "unknown" if exc.kind in ("transient", "timeout") else "rejected"
            with db.write_txn(ctx.conn):
                runtime_operations.complete_operation(ctx.conn, scope, operation_id,
                                                      status=status,
                                                      result={"reason": exc.message},
                                                      now=ctx.now_iso)
            raise util.KanpilotError(exc.kind, exc.message)
        with db.write_txn(ctx.conn):
            runtime_operations.complete_operation(ctx.conn, scope, operation_id,
                                                  status="accepted",
                                                  result={"run_id": run_id}, now=ctx.now_iso)
            db.add_event(ctx.conn, ctx.now_iso, "checks_rerun", task_id=task["task_id"],
                         actor="user", detail={"repo": repo, "run_id": run_id,
                                                "attempt": run.get("run_attempt")})
        results.append({"run_id": run_id, "status": "accepted"})
    return {"ok": True, "runs": results}