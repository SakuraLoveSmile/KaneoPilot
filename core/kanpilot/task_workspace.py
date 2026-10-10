"""Task branch registration and frozen planning workspace identity."""
import os
import re
from pathlib import Path

from . import constants, core, db, git_delivery, util


def payload_fields(delivery):
    fields = {"plan_cwd": delivery.get("worktree_path"),
              "delivery_mode": delivery.get("mode"),
              "delivery_head_sha": delivery.get("head_sha")}
    if delivery.get("workspace_version") == 1:
        fields.update(workspace_version=1, delivery_branch=delivery.get("branch"),
                      delivery_base_sha=delivery.get("base_sha"))
    return fields


def prepare_for_submission(conn, task_id, repository, task=None):
    """Outside a write transaction, prepare first-time local task registration.

    A historical request or execution keeps its frozen, unversioned contract.
    PR delivery and manual takeover already own their branch and are reused.
    """
    delivery = core.delivery_of(task) if task is not None else {}
    fingerprint = util.jdump(delivery)
    if delivery.get("workspace_version") == 1:
        try:
            git_delivery.verify_branch_worktree(dict(delivery, repository=repository),
                                               require_clean=False,
                                               expected_head=delivery.get("head_sha"))
            state = git_delivery.worktree_state_sha256(dict(delivery, repository=repository))
        except git_delivery.GitDeliveryError as exc:
            raise util.KanpilotError("worktree_invalid", exc.message) from exc
        return {"delivery": delivery, "fingerprint": fingerprint, "state_sha256": state}
    if delivery.get("branch") or (task is not None and
            (task["plan_path"] or task["run_id"] or conn.execute(
                "SELECT 1 FROM requests WHERE task_id=? LIMIT 1", (task_id,)).fetchone())):
        return None
    base_sha, base_branch = None, None
    if task is not None and task["plan_kind"] == "review_fix" and task["review_source_task_id"]:
        source = db.require_task(conn, task["review_source_task_id"])
        util.require(Path(source["repository"]).expanduser().resolve() == Path(repository).expanduser().resolve(),
                     "修复任务与原评审仓库不符", "worktree_invalid")
        reviews = db.task_extra(source).get("review_history") or []
        matching = [review for review in reviews if isinstance(review, dict)
                    and review.get("review_key") == task["review_key"] and review.get("head_sha")]
        util.require(bool(matching), "原评审没有固定提交，须先完成固定分支评审", "worktree_invalid")
        base_sha = matching[-1]["head_sha"]
        base_branch = core.delivery_of(source).get("branch")
        util.require(bool(base_branch), "原评审任务缺少分支登记", "worktree_invalid")
    root = constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None)
    intent_key = "task_workspace_preparing:" + task_id
    intent = db.get_setting(conn, intent_key)
    try:
        if intent is None:
            git_delivery._local_task_id(task_id)
            branch = constants.DELIVERY_BRANCH_PREFIX + task_id
            path = root.expanduser().resolve() / task_id / "branch"
            refs = git_delivery._value(["for-each-ref", "--format=%(refname)",
                                       "refs/heads/" + branch, "refs/remotes/origin/" + branch], repository)
            util.require(not refs and not os.path.lexists(path),
                         "任务分支或工作树路径已存在且未登记归属，未覆盖现有成果", "worktree_invalid")
            identity = {
                "mode": "manual", "workspace_version": 1,
                "branch": branch, "worktree_path": str(path),
                "base_branch": base_branch or git_delivery._value(["rev-parse", "--abbrev-ref", "HEAD"], repository),
                "base_sha": base_sha or git_delivery._value(["rev-parse", "--verify", "HEAD^{commit}"], repository)}
            identity["head_sha"] = identity["base_sha"]
            intent = {"repository": str(Path(repository).expanduser().resolve()), "delivery": identity}
            # Record ownership before filesystem mutation. Keep this on any later
            # submission rejection or crash so recovery can identify its tree.
            with db.write_txn(conn):
                existing_intent = db.get_setting(conn, intent_key)
                if existing_intent is None:
                    db.set_setting(conn, intent_key, intent)
                else:
                    intent = existing_intent
        util.require(isinstance(intent, dict)
                     and intent.get("repository") == str(Path(repository).expanduser().resolve()),
                     "准备中的任务工作区仓库不符", "worktree_invalid")
        delivery = intent.get("delivery")
        util.require(isinstance(delivery, dict)
                     and delivery.get("branch") == constants.DELIVERY_BRANCH_PREFIX + task_id
                     and delivery.get("worktree_path") == str(root.expanduser().resolve() / task_id / "branch")
                     and delivery.get("base_sha") == delivery.get("head_sha")
                     and (base_sha is None or delivery.get("base_sha") == base_sha),
                     "准备中的任务工作区身份不符", "worktree_invalid")
        if os.path.lexists(delivery["worktree_path"]):
            git_delivery.verify_branch_worktree(dict(delivery, repository=repository),
                                               require_clean=False, expected_head=delivery["head_sha"])
        else:
            created = git_delivery.prepare_local_branch_worktree(
                repository, task_id, worktrees_root=root,
                base_sha=delivery["base_sha"], base_branch=delivery["base_branch"])
            util.require(created == delivery, "创建的任务工作区与准备登记不符", "worktree_invalid")
        state = git_delivery.worktree_state_sha256(dict(delivery, repository=repository))
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("worktree_invalid", exc.message) from exc
    return {"delivery": delivery, "fingerprint": fingerprint, "state_sha256": state,
            "intent_key": intent_key}


def verify_plan_workspace(task, payload):
    """Return registered read-only project root; None means historical contract.

    New requests never accept a missing/changed identity or fall back to primary.
    Dirty content is allowed only if its frozen planning state still matches.
    """
    if "workspace_version" not in payload:
        return None
    delivery = core.delivery_of(task)
    util.require(payload.get("workspace_version") == 1
                 and delivery.get("workspace_version") == 1,
                 "规划工作区版本与登记不符", "worktree_invalid")
    expected = payload_fields(delivery)
    util.require(all(payload.get(key) == value and value is not None
                     for key, value in expected.items()),
                 "规划工作树、分支或提交与任务登记不符", "worktree_invalid")
    try:
        identity = dict(delivery, repository=task["repository"])
        git_delivery.verify_branch_worktree(identity, require_clean=False,
                                           expected_head=payload["delivery_head_sha"])
        state = payload.get("worktree_state_sha256")
        util.require(bool(state) and git_delivery.worktree_state_sha256(identity) == state,
                     "规划排队后任务工作树内容已变化，请重新准备规划", "worktree_invalid")
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("worktree_invalid", exc.message) from exc
    return Path(delivery["worktree_path"])


def prepare_dirty_plan_snapshot(task, payload, *, worktrees_root, run_id):
    """Copy the frozen task state into an isolated detached planning worktree.

    Plan proposals/materials may be written there without changing task成果.
    Existing paths are never patched or overwritten on recovery.
    """
    from . import review_snapshot
    source = verify_plan_workspace(task, payload)
    util.require(source is not None and isinstance(run_id, str)
                 and re.fullmatch(r"[A-Za-z0-9_-]+", run_id),
                 "规划快照轮次身份无效", "worktree_invalid")
    root = Path(worktrees_root).expanduser().resolve() / task["task_id"] / ("plan-state-" + run_id)
    util.require(not os.path.lexists(root), "规划快照路径已存在，未覆盖旧成果", "worktree_invalid")
    try:
        frozen = review_snapshot.freeze(source)
        util.require(frozen["head"] == payload["delivery_head_sha"],
                     "规划快照 HEAD 与登记不符", "worktree_invalid")
        verify_plan_workspace(task, payload)
        root.parent.mkdir(parents=True, exist_ok=True)
        git_delivery._git(["worktree", "add", "--detach", str(root), frozen["head"]],
                          task["repository"], 60)
        review_snapshot.populate(root, frozen)
        review_snapshot.verify_source(source, frozen)
        verify_plan_workspace(task, payload)
    except (git_delivery.GitDeliveryError, OSError) as exc:
        raise util.KanpilotError("worktree_invalid", "规划快照创建失败：" + str(exc)) from exc
    except util.KanpilotError as exc:
        raise util.KanpilotError("worktree_invalid", exc.message) from exc
    return root
