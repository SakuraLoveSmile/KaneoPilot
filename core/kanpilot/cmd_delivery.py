"""Monitor-owned delivery commit, with all filesystem/Git IO outside transactions."""

from . import core, db, git_delivery, util


def _identity(ctx, payload, *, now=None):
    task = db.require_task(ctx.conn, payload["task_id"])
    core.require_stage_identity(ctx.conn, payload, task, now or ctx.now, stages=("monitor",))
    if task["run_id"] != payload["expected_execution_run_id"]:
        raise util.KanpilotError("stale_run", "交付对应的执行轮次已变更")
    if not core.requires_branch_review(task):
        raise util.KanpilotError("conflict", "任务未采用分支工作区")
    return task


def cmd_delivery_commit(ctx, payload) -> dict:
    for key in ("task_id", "request_id", "run_id", "expected_execution_run_id"):
        util.require(isinstance(payload.get(key), str) and payload[key].strip(),
                     f"缺少 {key}", "bad_request")
    conn, now = ctx.conn, ctx.now_iso
    execution_run_id = payload["expected_execution_run_id"]
    # Phase 1: freeze identity and delivery metadata only.
    with db.write_txn(conn):
        task = _identity(ctx, payload)
        delivery = core.delivery_of(task)
        existing = next((commit for commit in delivery.get("commits") or []
                         if commit.get("execution_run_id") == execution_run_id), None)
        if existing:
            core.require_committed_review(task, execution_run_id, verify_worktree=False)
        else:
            util.require(task["status"] == "running", "仅运行中的任务可以登记执行提交", "conflict")
        util.require(all(delivery.get(k) for k in ("branch", "head_sha", "worktree_path")),
                     "交付工作树登记不完整", "validation_failed")
        baseline = core.branch_review_snapshot(task)
        git_input = dict(delivery, repository=task["repository"])
        title = task["title"]
        fix_round = core.automation_of(task).get("fix_round", 0)

    # Phase 2: IO outside BEGIN IMMEDIATE, including the final HEAD check.
    failure, result = None, None
    try:
        if existing:
            result = {"sha": existing["sha"], "files": []}
        else:
            from . import engine_claim_scope
            from .engine import verdict
            live = engine_claim_scope.effective_mode(conn, task["project_id"]) == "live"
            verified = {}
            if live:
                receipt = db.task_extra(task).get("engine_verdict") or {}
                frozen = receipt.get("binding") or {}
                actual = verdict.binding(conn, task, allow_core_commit=True)
                if (receipt.get("verification_ok") is not True
                        or receipt.get("execution_run_id") != execution_run_id
                        or receipt.get("plan_sha256") != task["plan_sha256"]
                        or not receipt.get("index_source_sha256")
                        or dict(actual, head_sha=frozen.get("head_sha")) != frozen):
                    raise git_delivery.GitDeliveryError("delivery_commit_failed", "缺少与当前源码绑定的核心验证回执，未提交")
                verified = {"verified_source_sha256": receipt["binding"]["source_sha256"],
                            "verified_index_sha256": receipt["index_source_sha256"]}
            result = git_delivery.commit_worktree(
                git_input, task_id=payload["task_id"], title=title,
                execution_run_id=execution_run_id, fix_round=fix_round, **verified)
        git_delivery.verify_branch_worktree(
            git_input, require_clean=True, expected_head=result["sha"])
    except (git_delivery.GitDeliveryError, util.KanpilotError) as exc:
        failure = exc if isinstance(exc, git_delivery.GitDeliveryError) else git_delivery.GitDeliveryError("delivery_commit_failed", exc.message)

    # Phase 3: reject any concurrent lifecycle/delivery metadata change.
    with db.write_txn(conn):
        fresh = _identity(ctx, payload, now=util.now_dt())
        current = core.delivery_of(fresh)
        for commit in current.get("commits") or []:
            if commit.get("execution_run_id") == execution_run_id:
                if failure:
                    raise util.KanpilotError("validation_failed", failure.message)
                util.require(commit.get("sha") == result["sha"]
                             and core.branch_review_snapshot(fresh) == dict(baseline, head_sha=result["sha"]),
                             "提交期间交付登记已变更", "conflict")
                core.require_committed_review(fresh, execution_run_id, verify_worktree=False)
                return {"outcome": "already_committed", "sha": commit["sha"]}
        util.require(core.branch_review_snapshot(fresh) == baseline,
                     "提交期间交付登记已变更", "conflict")
        util.require(fresh["status"] == "running", "提交期间任务状态已变更", "conflict")
        if failure:
            code = failure.code if failure.code in ("delivery_blocked_files", "no_changes") else "delivery_commit_failed"
            core.block_task(conn, fresh, now, failure.message, code,
                            "kaneo-monitor", stage="delivery")
            value = {"outcome": "blocked", "failure_code": code, "message": failure.message}
            if failure.files is not None:
                value["files"] = failure.files
            return value
        commits = list(current.get("commits") or [])
        commits.append({"execution_run_id": execution_run_id, "sha": result["sha"],
                        "at": now, "files": len(result["files"]), "fix_round": fix_round})
        core.update_delivery(conn, fresh, now, commits=commits, head_sha=result["sha"])
        db.add_event(conn, now, "delivery_committed", task_id=payload["task_id"],
                     request_id=payload["request_id"], actor="kaneo-monitor",
                     detail=commits[-1])
    return {"outcome": "committed", "sha": result["sha"], "files": result["files"]}
