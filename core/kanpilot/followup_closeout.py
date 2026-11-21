"""在交付链之外落地的二级任务：人工确认收口（KP-MG06）。

评审可把超出原任务范围的发现另立为二级任务（KP-MG04）。二级任务用尽自己的
修复轮次后停在 `changes_requested`，而它的修复可能已经通过另一条 PR 合入目标
分支——那条支线没有本任务的 delivery 记录，父任务的发布批次于是永远等不到
「合并和验证证据」。

本命令让用户显式收口这种任务，但只读核对先行：任务当前分支头改动的每条路径，
在目标分支当前 tip 与这个分支头上的 blob 必须完全一致；引用 PR 时还要求它
`MERGED` 到同一目标分支且 PR 头就是该分支头。核对通过才写回执并把任务置为
`done`，发布批次据此接受「已由人工确认、内容已在目标分支」这一证据。
"""
from __future__ import annotations

from . import core, db, gh_client, git_delivery, util


def _changed_paths(worktree, base_sha, head_sha):
    raw = git_delivery._git(["diff", "--name-only", "-z", base_sha, head_sha], worktree, 30).stdout
    return sorted(path for path in raw.decode("utf-8", "surrogateescape").split("\0") if path)


def _blob(worktree, revision, path):
    try:
        return git_delivery._value(["rev-parse", revision + ":" + path], worktree)
    except git_delivery.GitDeliveryError:
        return None


def settled(task):
    """A confirmed manual closeout is this task's merge evidence for the release gate."""
    value = db.task_extra(task).get("followup_closeout") or {}
    return bool(value.get("verified_at") and value.get("head_sha") and value.get("reviewed_paths"))


def candidate(conn, task, now=None):
    """SQLite-only eligibility; files and the remote stay mandatory in the command."""
    if not task["review_source_task_id"]:
        return None
    if not db.task_is_active(task) or db.task_is_purged(task):
        return None
    if task["status"] not in ("in_review", "needs_user") or task["stage"] not in ("review", "delivery"):
        return None
    delivery = core.delivery_of(task)
    if delivery.get("merged_at") or delivery.get("mode") != "pr":
        return None
    if not all(delivery.get(key) for key in ("head_sha", "branch", "worktree_path", "base_branch", "base_sha")):
        return None
    if db.task_extra(task).get("followup_closeout"):
        return None
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                    (task["task_id"],)).fetchone():
        return None
    return {"head_sha": delivery["head_sha"], "branch": delivery["branch"],
            "base_branch": delivery["base_branch"], "pr_number": delivery.get("pr_number")}


def prove(task, *, pr_number=None, expected_head=None, timeout=120):
    """Read-only proof that this task's work already sits on the target branch."""
    delivery = core.delivery_of(task)
    worktree = delivery["worktree_path"]
    base = delivery["base_branch"]
    try:
        git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]),
                                            require_clean=True, expected_head=None)
        head = git_delivery._value(["rev-parse", "HEAD"], worktree)
        git_delivery._git(["fetch", "--prune", "origin"], worktree, timeout)
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", "工作树或远端不满足收口条件：" + exc.message) from exc
    if expected_head:
        # 调用方可能传入更早的交付头（收尾提交会让分支头前进）；只要它仍是本分支祖先，
        # 身份就没有变化——分支被重写时 is-ancestor 会失败并拒绝。
        try:
            confirmed = git_delivery._value(["rev-parse", expected_head + "^{commit}"], worktree)
            git_delivery._git(["merge-base", "--is-ancestor", confirmed, head], worktree, 30)
        except git_delivery.GitDeliveryError as exc:
            raise util.KanpilotError("conflict", "确认的提交不属于本任务分支，请刷新后重试") from exc
    try:
        base_tip = git_delivery._value(["rev-parse", "origin/" + base + "^{commit}"], worktree)
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", "无法读取目标分支最新提交") from exc
    paths = _changed_paths(worktree, delivery["base_sha"], head)
    util.require(bool(paths), "任务分支没有可核对的源码差异，不能收口", "validation_failed")
    drifted = [path for path in paths
               if _blob(worktree, base_tip, path) != _blob(worktree, head, path)]
    util.require(not drifted,
                 "目标分支上的内容与任务分支头不一致：" + "、".join(drifted[:5]), "conflict")
    receipt = {"head_sha": head, "base_branch": base, "base_tip": base_tip,
               "reviewed_paths": paths}
    if pr_number is not None:
        repo = git_delivery.require_github_origin(task["repository"])
        try:
            pr = gh_client.pr_view(repo, pr_number, merge_commit=True)
        except gh_client.GhError as exc:
            raise util.KanpilotError("remote_unavailable", exc.message) from exc
        util.require(pr.get("state") == "MERGED" and pr.get("baseRefName") == base,
                     "引用的 PR 未合并到目标分支，不能收口", "conflict")
        util.require(pr.get("headRefOid") == head,
                     "引用的 PR 头不是本任务当前分支头，不能收口", "conflict")
        merge = pr.get("mergeCommit") or {}
        receipt.update(pr_number=pr["number"], pr_url=pr["url"],
                       merged_at=pr.get("mergedAt"), merge_commit=merge.get("oid"))
    return receipt


def cmd_confirm_followup_closeout(ctx, payload):
    util.require(payload.get("confirm") is True, "收口需要显式确认", "bad_request")
    task = db.require_task(ctx.conn, payload["task_id"])
    value = candidate(ctx.conn, task, ctx.now)
    util.require(value is not None, "当前任务不满足人工收口条件", "conflict")
    pr_number = payload.get("pr_number", value.get("pr_number"))
    if pr_number is not None:
        util.require(type(pr_number) is int and pr_number > 0, "pr_number 需为正整数", "bad_request")
    receipt = prove(task, pr_number=pr_number, expected_head=payload.get("expected_head_sha"))
    note = (payload.get("note") or "").strip()[:500] or None
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task["task_id"])
        util.require(candidate(ctx.conn, fresh, ctx.now) == value,
                     "任务状态在核对期间已变化，请刷新后重试", "conflict")
        extra = db.task_extra(fresh)
        record = dict(receipt, verified_at=ctx.now_iso, actor="user", note=note)
        extra["followup_closeout"] = record
        fields = core.clear_block_fields()
        fields["resolution"] = {"action": "confirm_closeout", "actor": "user", "at": ctx.now_iso,
                                "head_sha": receipt["head_sha"], "base_branch": receipt["base_branch"],
                                "pr_number": receipt.get("pr_number"), "note": note}
        core.transition_task(ctx.conn, fresh, "done", ctx.now_iso, "user",
                             detail={"via": "confirm followup closeout", **receipt},
                             extra_json=extra, **fields)
        db.add_event(ctx.conn, ctx.now_iso, "followup_closeout_confirmed",
                     task_id=fresh["task_id"], actor="user", detail=record)
    return {"ok": True, "task_id": task["task_id"], "status": "done", "closeout": receipt}
