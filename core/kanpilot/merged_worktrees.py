"""Close local Git resources owned by a verified, settled PR delivery."""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import cmd_worktree as audit
from . import constants, core, db, git_delivery, release_delivery, release_verification, runtime_operations, util, worktree_pool as pool

KEY = "local_worktree_closeout"
SCOPE = "merged-worktree-closeout"


def _require(condition, message):
    util.require(condition, message, "worktree_closeout_blocked")


def _settled(conn, task, identity, *, preview=False):
    delivery = core.delivery_of(task)
    enrolled = delivery.get(KEY)
    _require(task["status"] == "done" and (enrolled == identity or (preview and enrolled is None))
             and delivery.get("pr_state") == "MERGED" and delivery.get("merged_at") == identity["merged_at"]
             and task["repository"] == identity["repository"] and delivery.get("base_branch") == identity["base"]
             and core.delivery_merge_head(delivery) == identity["merged_head"]
             and delivery.get("branch") == identity["branch"] and delivery.get("base_branch") != identity["branch"]
             and delivery.get("head_sha") == identity["head"] and delivery.get("pr_number") == identity["pr"]
             and delivery.get("worktree_path") == identity["path"], "PR、任务或本地交付身份已变化")
    _require(release_delivery.completion_gate(conn, task) is None, "发布仍需核对交付证据")
    _require(not release_verification._unsettled_children(conn, task["task_id"]), "关联修复仍在使用任务成果")
    rounds = conn.execute("SELECT run_status FROM execution_rounds WHERE task_id=?", (task["task_id"],))
    _require(all(row["run_status"] in audit._ROUND_TERMINAL for row in rounds), "执行轮次仍未结束")
    parent = task["review_source_task_id"]
    if parent:
        _require(db.require_task(conn, parent)["status"] == "done", "原任务仍需使用修复证据")
    _require(not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                              (task["task_id"],)).fetchone(), "任务仍有活动请求")


def _inventory(conn, task, identity):
    repo = Path(task["repository"]).resolve()
    tid = task["task_id"]
    root = pool.project_root(conn, repo, tid).resolve()
    run = Path(identity["path"])
    _require(identity["branch"] == "kaneo/" + tid and run == root / tid / "run",
             "执行工作树与标准任务分支登记不符")
    result = {}
    for slot in conn.execute("SELECT * FROM worktree_slots WHERE task_id=?", (tid,)):
        _require(slot["state"] == "free" and slot["stage"] in ("plan", "review", "run")
                 and Path(slot["repository"]).resolve() == repo and Path(slot["worktrees_root"]) == root,
                 "阶段仍被占用或登记归属已变化")
        path = root / tid / slot["stage"]
        _require(path == Path(slot["path"]), "阶段工作树路径与登记不符")
        if slot["stage"] != "run":
            # Retain any unique detached commit, even in an otherwise clean tree.
            pool._git(repo, "merge-base", "--is-ancestor", slot["sha"], identity["head"])
            result[path] = (None, slot["sha"])
    result[run] = (identity["branch"], identity["head"])
    ref = db.task_extra(task).get("release_verification_ref") or {}
    scope = release_verification._scope(tid)
    attempts = {row["operation_id"].removeprefix("worktree:") for row in conn.execute(
        "SELECT operation_id FROM runtime_operations WHERE scope_id=? AND tool='release_verify_worktree'", (scope,))}
    if ref.get("verification_id"):
        attempts.add(ref["verification_id"])
    for attempt in sorted(attempts):
        _require(re.fullmatch(r"[a-zA-Z0-9_-]+", attempt), "发布验证编号无效")
        value = release_verification._load(conn, tid, attempt)
        context = value["context"]
        expected = release_verification._worktree_path(conn, dict(value, worktree_path=None))
        terminal = ("verified",) if attempt == ref.get("verification_id") else ("verified", "failed", "blocked")
        _require(value.get("state") in terminal and Path(context["repository"]).resolve() == repo
                 and expected == Path(value.get("worktree_path") or ""), "发布验证树仍被使用或归属不符")
        proof = db.get_runtime_operation(conn, scope, "worktree:" + attempt)
        creation = {"repository": str(repo), "path": str(expected), "target_sha": context["target_sha"]}
        _require(proof is not None and proof["status"] == "accepted" and proof["tool"] == "release_verify_worktree"
                 and util.jload(proof["result_json"]) == creation, "发布验证树缺少创建回执")
        result[expected] = (None, context["target_sha"])
    return repo, result


def _check_file_users(path):
    # Also detect an editor or worker with a file open from another cwd.
    try:
        scan = subprocess.run([audit._LSOF, "-nP", "-t", "+D", str(path)],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("worktree_closeout_blocked", "无法核实工作树文件占用") from exc
    _require(scan.returncode == 1 and not scan.stdout and not scan.stderr, "工作树文件仍被使用或无法核实占用")


def _check_tree(repo, path, branch, head, cwds):
    pool._no_links(path)
    registered = audit._registered(pool._common(repo))
    if not os.path.lexists(path):
        _require(path not in registered, "目录已丢失但 Git 仍登记该工作树")
        return False
    _require(path != repo and path in registered and pool._common(path) == pool._common(repo)
             and Path(pool._value(path, "rev-parse", "--show-toplevel")).resolve() == path
             and pool._value(path, "rev-parse", "HEAD") == head
             and pool._value(path, "rev-parse", "--abbrev-ref", "HEAD") == (branch or "HEAD")
             and not audit._locked(path), "工作树身份、分支、提交或锁定状态已变化")
    _require(not audit._cwd_reasons(path, cwds), "工作树仍有活动进程或无法核实占用")
    _check_file_users(path)
    _require(not pool._git(path, "status", "--porcelain=v1", "-z", "--untracked-files=no",
                           "--ignore-submodules=none"), "工作树有未提交的已跟踪改动")
    return True


def _approved_extras(path):
    extras = audit._file_plan(path)
    _require(all(item["group"] == "ignored" or item["rel"].startswith(".pi/")
                 or git_delivery.is_build_artifact(item["rel"]) for item in extras),
             "工作树有未确认归属的未跟踪文件")
    return extras


def _remove_tree(repo, path, branch, head, check, record_archive):
    if not _check_tree(repo, path, branch, head, audit._cwd_listing()):
        return
    extras = _approved_extras(path)
    if extras:
        day = util.now_iso()[:10]
        archive_id = uuid.uuid4().hex
        directory = constants.app_support() / "KaneoPilot-archives" / day / archive_id
        archive = audit._write_archive(path, archive_id, day, directory, head)
        manifest = audit._verify_archive(path, archive)
        record_archive(archive)
        with tempfile.TemporaryDirectory() as scratch:
            audit._content_proof(path, manifest, scratch)
        check()
        _check_tree(repo, path, branch, head, audit._cwd_listing())
        audit._discard(path, manifest, restore_tracked=False)
    check()
    _check_tree(repo, path, branch, head, audit._cwd_listing())
    _require(not audit._file_plan(path), "删除前出现新的未跟踪文件")
    pool._git(repo, "worktree", "remove", str(path))
    _require(not os.path.lexists(path) and path not in audit._registered(pool._common(repo)),
             "Git 工作树回收结果未确认")


def _operation(tid, identity):
    return tid + ":" + str(identity["pr"]) + ":" + identity["head"]


def close(conn, task, identity, now):
    tid = task["task_id"]
    operation = _operation(tid, identity)

    def check():
        _settled(conn, db.require_task(conn, tid), identity)

    existing = db.get_runtime_operation(conn, SCOPE, operation)
    if existing is not None and existing["status"] == "accepted":
        return
    check()
    repo, trees = _inventory(conn, task, identity)
    ref = "refs/heads/" + identity["branch"]
    _require(re.fullmatch(r"[0-9a-f]{40,64}", identity["head"]), "交付提交无效")
    with db.write_txn(conn):
        runtime_operations.begin_operation(conn, SCOPE, operation, "core", "local_merge_cleanup", identity)

    def record_archive(archive):
        with db.write_txn(conn):
            db.add_event(conn, now, "merged_worktree_archived", task_id=tid, detail=archive)

    for path, (branch, head) in trees.items():
        _remove_tree(repo, path, branch, head, check, record_archive)
    check()
    actual = pool._value(repo, "for-each-ref", "--format=%(refname) %(objectname)", ref)
    if actual:
        _require(actual == ref + " " + identity["head"], "任务分支产生了新的提交")
        registered = pool._git(repo, "worktree", "list", "--porcelain", "-z").split(b"\x00")
        _require(os.fsencode("branch " + ref) not in registered, "任务分支仍被工作树使用")
        pool._git(repo, "update-ref", "-d", ref, identity["head"])
    _require(not pool._value(repo, "for-each-ref", "--format=%(refname)", ref), "本地任务分支回收未确认")
    with db.write_txn(conn):
        check()
        conn.execute("DELETE FROM worktree_slots WHERE task_id=? AND state='free'", (tid,))
        result = {"paths": [str(path) for path in trees], "branch": identity["branch"], "pr": identity["pr"]}
        runtime_operations.complete_operation(conn, SCOPE, operation, result=result, now=now)
        db.add_event(conn, now, "merged_worktrees_removed", task_id=tid, detail=result)
    pool.projection(conn)


@contextmanager
def _maintenance_lock():
    # Released by the kernel on exit, also while a receipt is incomplete.
    path = constants.app_support() / "merged-worktree-cleanup.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def sweep(conn, now):
    with _maintenance_lock() as acquired:
        if acquired:
            _sweep(conn, now)


def merge_identity(task, delivery, pr):
    return {"pr": pr["number"], "branch": delivery["branch"], "head": delivery["head_sha"],
            "path": delivery["worktree_path"], "repository": task["repository"], "base": pr["baseRefName"],
            "merged_head": pr["headRefOid"], "merged_at": pr["mergedAt"]}


def cmd_close_merged_worktrees(ctx, payload):
    """Explicit reconciliation of one historical PR. No broad enrollment."""
    from . import cmd_delivery_pr, gh_client
    tid = payload["task_id"]
    task = db.require_task(ctx.conn, tid)
    delivery = core.delivery_of(task)
    _require(task["status"] == "done" and delivery.get("mode") == "pr" and delivery.get("pr_number")
             and delivery.get("head_sha") == payload["expected_head_sha"], "任务尚未完成或交付提交已变化")
    try:
        repo = cmd_delivery_pr._repository(task, delivery)
        pr = gh_client.pr_view(repo, delivery["pr_number"], merge_commit=True)
        cmd_delivery_pr._verify_pr_identity(pr, repo, delivery, delivery.get("pushed_sha"))
    except (gh_client.GhError, git_delivery.GitDeliveryError) as exc:
        raise util.KanpilotError("worktree_closeout_blocked", util.redact(str(exc))) from exc
    merge = (pr.get("mergeCommit") or {}).get("oid") or ""
    _require(pr["state"] == "MERGED" and re.fullmatch(r"[0-9a-f]{40,64}", merge),
             "GitHub 未确认标准 PR 合并提交")
    identity = merge_identity(task, delivery, pr)
    pool._git(task["repository"], "merge-base", "--is-ancestor", identity["head"], merge)
    _settled(ctx.conn, task, identity, preview=True)
    previous = db.get_runtime_operation(ctx.conn, SCOPE, _operation(tid, identity))
    if previous is not None and previous["status"] == "accepted":
        return dict(util.jload(previous["result_json"]), deduplicated=True, dry_run=not payload.get("confirm"))
    repository, trees = _inventory(ctx.conn, task, identity)
    with _maintenance_lock() as acquired:
        _require(acquired, "另一个本地工作树收尾正在进行")
        cwds = audit._cwd_listing()
        for path, (branch, head) in trees.items():
            if _check_tree(repository, path, branch, head, cwds):
                _approved_extras(path)
        result = {"pr": pr["number"], "branch": identity["branch"], "paths": list(map(str, trees))}
        if not payload.get("confirm"):
            return dict(result, dry_run=True)
        with db.write_txn(ctx.conn):
            fresh = db.require_task(ctx.conn, tid)
            _settled(ctx.conn, fresh, identity, preview=True)
            core.update_delivery(ctx.conn, fresh, ctx.now_iso, **{KEY: identity})
        close(ctx.conn, db.require_task(ctx.conn, tid), identity, ctx.now_iso)
        return dict(result, dry_run=False)


def _sweep(conn, now):
    for task in conn.execute("SELECT * FROM tasks WHERE status='done' AND extra_json LIKE ?",
                             ('%"' + KEY + '"%',)).fetchall():
        identity = core.delivery_of(task).get(KEY)
        if not identity:
            continue
        try:
            close(conn, task, identity, now)
        except (OSError, ValueError, util.KanpilotError, git_delivery.GitDeliveryError) as exc:
            reason = util.redact(str(exc))
            last = db.get_setting(conn, KEY + ":" + task["task_id"])
            if reason != last:
                with db.write_txn(conn):
                    db.set_setting(conn, KEY + ":" + task["task_id"], reason)
                    db.add_event(conn, now, "merged_worktree_cleanup_blocked", task_id=task["task_id"],
                                 detail={"reason": reason, "path": identity["path"]})
