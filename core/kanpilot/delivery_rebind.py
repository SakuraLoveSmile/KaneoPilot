"""Explicit re-bind of a PR head that advanced by base-only commits.

A task branch created earlier keeps its reviewed commit; when GitHub merges the
current base into the branch (the "Update branch" button or a bot), the PR head
advances by a merge commit whose parents are the frozen head and a commit already
on the base branch. The frozen delivery head still identifies the reviewed change
set, so merge/sync must target the advanced head instead of reporting
"branch_diverged" forever.

The re-bind is CLI-only and requires an explicit confirmation. It proves the
advanced head is exactly the clean merge of the frozen head with a base-branch
commit (two parents + a `git merge-tree` identical tree + byte-identical reviewed
paths). It never rewrites review evidence: the frozen head stays the review anchor
and only `merge_head_sha` moves.
"""
from . import core, db, gh_client, git_delivery, util


def _changed_paths(worktree, base_sha, head_sha):
    raw = git_delivery._git(["diff", "--name-only", "-z", base_sha, head_sha], worktree, 30).stdout
    return sorted(path for path in raw.decode("utf-8", "surrogateescape").split("\0") if path)


def _blob(worktree, revision, path):
    try:
        return git_delivery._value(["rev-parse", revision + ":" + path], worktree)
    except git_delivery.GitDeliveryError:
        return None


def _is_ancestor(worktree, ancestor, descendant):
    try:
        git_delivery._git(["merge-base", "--is-ancestor", ancestor, descendant], worktree, 30)
        return True
    except git_delivery.GitDeliveryError:
        return False


def _merge_tree(worktree, left, right):
    try:
        raw = git_delivery._git(["merge-tree", "--write-tree", left, right], worktree, 60).stdout
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", "无法复算目标分支与冻结提交的干净合并：" + exc.message) from exc
    first = raw.decode().splitlines()[0].strip() if raw else ""
    util.require(len(first) in (40, 64) and all(c in "0123456789abcdef" for c in first),
                 "干净合并结果无法解析，不能重绑", "conflict")
    return first


def proof(delivery, *, expected_head_sha, expected_pr_head_sha, base_branch, timeout=120):
    worktree = delivery["worktree_path"]
    frozen = delivery.get("head_sha")
    util.require(frozen == expected_head_sha, "冻结交付提交已变化，请刷新", "conflict")
    util.require(delivery.get("pushed_sha") == frozen, "尚未推送冻结提交，不能重绑", "conflict")
    util.require(expected_pr_head_sha != frozen, "PR 头没有前进，无需重绑", "conflict")
    try:
        git_delivery._git(["fetch", "--prune", "origin"], worktree, timeout)
    except git_delivery.GitDeliveryError as exc:
        raise git_delivery._recode(exc, "git_transient", "获取远端分支失败") from exc
    head = git_delivery._value(["rev-parse", "--verify", expected_pr_head_sha + "^{commit}"], worktree)
    util.require(head == expected_pr_head_sha, "PR 头提交不可精确解析", "conflict")
    base_tip = git_delivery._value(["rev-parse", "--verify", "origin/" + base_branch + "^{commit}"], worktree)
    util.require(_is_ancestor(worktree, frozen, head), "PR 头已被重写，未包含冻结交付提交；不能重绑", "conflict")
    util.require(_is_ancestor(worktree, base_tip, head), "PR 头未包含当前目标分支，先更新分支后再重绑", "conflict")
    parents = git_delivery._value(["show", "-s", "--format=%P", head], worktree).split()
    util.require(len(parents) == 2 and frozen in parents,
                 "PR 头的推进不是「目标分支合入冻结提交」的合并提交；不能重绑", "conflict")
    base_parent = parents[1] if parents[0] == frozen else parents[0]
    util.require(base_parent != frozen and _is_ancestor(worktree, base_parent, base_tip),
                 "PR 头合入的目标提交不在当前目标分支历史中；不能重绑", "conflict")
    expected_tree = _merge_tree(worktree, frozen, base_parent)
    util.require(git_delivery._value(["rev-parse", head + "^{tree}"], worktree) == expected_tree,
                 "PR 头的内容不是冻结提交与目标分支的干净合并；不能重绑", "conflict")
    reviewed = _changed_paths(worktree, delivery["base_sha"], frozen)
    util.require(reviewed, "冻结交付没有可核对的源码差异，不能重绑", "validation_failed")
    drifted = [path for path in reviewed if _blob(worktree, head, path) != _blob(worktree, frozen, path)]
    util.require(not drifted, "已评审源码在新 head 上发生变化：" + "、".join(drifted[:5]), "conflict")
    try:
        git_delivery.verify_branch_worktree(delivery, require_clean=True, expected_head=frozen)
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", "任务工作树不干净或已变化，不能重绑：" + exc.message) from exc
    return dict(frozen_head=frozen, pr_head=head, base_branch=base_branch, base_tip=base_tip,
                base_parent=base_parent, expected_tree=expected_tree, reviewed_paths=reviewed)


def cmd_rebind_delivery_head(ctx, payload):
    util.require(payload.get("confirm") is True, "需要明确确认本次重绑", "bad_request")
    task_id = payload["task_id"]
    task = db.require_task(ctx.conn, task_id)
    delivery = core.delivery_of(task)
    expected_head, expected_pr = payload["expected_head_sha"], payload["expected_pr_head_sha"]
    util.require(task["status"] in ("needs_user", "in_review"), "仅等待合并或受阻的任务可重绑交付头", "conflict")
    util.require(db.task_is_active(task) and not db.task_auto_paused(task)
                 and not core.automation_of(task).get("manual_takeover", False),
                 "任务已归档、暂停或转为人工处理", "conflict")
    util.require(delivery.get("mode") == "pr" and type(delivery.get("pr_number")) is int,
                 "任务不处于 PR 交付阶段", "conflict")
    util.require(not db.paused(ctx.conn, "delivery"), "交付阶段已暂停", "conflict")
    util.require(not ctx.conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                                     (task_id,)).fetchone(), "当前任务仍有未结束请求，请稍后重试", "conflict")
    util.require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND status IN ('prepared','unknown')",
                                     ("pr-merge:" + task_id,)).fetchone(),
                 "已有 PR 合并请求结果待确认，请先重新核验", "conflict")
    reviewed = (db.task_extra(task).get("review_history") or [])
    latest = reviewed[-1] if reviewed and isinstance(reviewed[-1], dict) else {}
    util.require(latest.get("conclusion") == "pass" and latest.get("head_sha") == delivery.get("head_sha")
                 and any(row.get("sha") == delivery.get("head_sha") and row.get("execution_run_id") == task["run_id"]
                         for row in delivery.get("commits") or []),
                 "当前交付没有精确绑定本轮执行的正式评审通过记录", "validation_failed")
    repo = git_delivery.require_github_origin(task["repository"])
    try:
        pr = gh_client.pr_view(repo, delivery["pr_number"])
    except gh_client.GhError as exc:
        raise util.KanpilotError("remote_unavailable", exc.message) from exc
    expected_url = "https://github.com/" + repo + "/pull/" + str(delivery["pr_number"])
    util.require(pr.get("state") == "OPEN" and pr.get("url") == expected_url
                 and pr.get("headRefName") == delivery.get("branch")
                 and pr.get("baseRefName") == delivery.get("base_branch")
                 and pr.get("isCrossRepository") is False,
                 "PR 身份、分支或仓库已变化，不能重绑", "conflict")
    util.require(pr.get("headRefOid") == expected_pr, "PR 头与确认的提交不一致，请刷新后重试", "conflict")
    receipt = proof(dict(delivery, repository=task["repository"]),
                    expected_head_sha=expected_head, expected_pr_head_sha=expected_pr,
                    base_branch=delivery["base_branch"])
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task_id)
        util.require(core.delivery_of(fresh) == delivery, "交付登记在核验期间已变化，未重绑", "conflict")
        fields = core.clear_block_fields()
        fields["resolution"] = {"action": "rebind_delivery_head", "actor": "user",
                                "at": ctx.now_iso, "note": payload.get("note")}
        detail = {key: receipt[key] for key in ("frozen_head", "pr_head", "base_tip", "base_parent")}
        core.transition_task(ctx.conn, fresh, "in_review", ctx.now_iso, "user",
                             detail=dict(detail, via="rebind delivery head"), **fields)
        core.update_delivery(ctx.conn, db.require_task(ctx.conn, task_id), ctx.now_iso,
                             merge_head_sha=receipt["pr_head"],
                             head_advance=dict(receipt, verified_at=ctx.now_iso),
                             auto_merge_stall=None, next_sync_at=ctx.now_iso)
        db.add_event(ctx.conn, ctx.now_iso, "delivery_head_advanced", task_id=task_id,
                     actor="user", detail=receipt)
    from . import cmd_queue
    with db.write_txn(ctx.conn):
        task_now = db.require_task(ctx.conn, task_id)
        request, created = cmd_queue._new_cycle_request(ctx.conn, ctx.now_iso, task_now, "delivery",
                                                        requested_by="user")
    return {"ok": True, "task_id": task_id, "frozen_head_sha": expected_head,
            "merge_head_sha": receipt["pr_head"], "reviewed_paths": receipt["reviewed_paths"],
            "base_tip": receipt["base_tip"], "request_id": request["request_id"],
            "request_created": created}