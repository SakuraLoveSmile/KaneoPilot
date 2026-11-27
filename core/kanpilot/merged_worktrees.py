"""Close local Git resources owned by a verified, settled PR delivery."""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
import uuid
from pathlib import Path

from . import cmd_worktree as audit
from . import constants, core, db, git_delivery, release_delivery, release_verification, runtime_operations, util, worktree_pool as pool

KEY = "local_worktree_closeout"
SCOPE = "merged-worktree-closeout"


def _require(condition, message):
    util.require(condition, message, "worktree_closeout_blocked")


def _settled(conn, task, identity):
    delivery = core.delivery_of(task)
    _require(task["status"] == "done" and delivery.get(KEY) == identity
             and delivery.get("pr_state") == "MERGED" and delivery.get("merged_at") == identity["merged_at"]
             and task["repository"] == identity["repository"] and delivery.get("base_branch") == identity["base"]
             and core.delivery_merge_head(delivery) == identity["merged_head"]
             and delivery.get("branch") == identity["branch"] and delivery.get("base_branch") != identity["branch"]
             and delivery.get("head_sha") == identity["head"] and delivery.get("pr_number") == identity["pr"]
             and delivery.get("worktree_path") == identity["path"], "PR、任务或本地交付身份已变化")
    _require(release_delivery.completion_gate(conn, task) is None, "发布仍需核对交付证据")
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
    if ref.get("verification_id"):
        value = release_verification._load(conn, tid, ref["verification_id"])
        context = value["context"]
        # Compute the normal path independently of the recorded value.
        expected = release_verification._worktree_path(conn, dict(value, worktree_path=None))
        _require(value.get("state") == "verified" and Path(context["repository"]).resolve() == repo
                 and expected == Path(value.get("worktree_path") or ""), "发布验证树仍被使用或归属不符")
        proof = db.get_runtime_operation(conn, release_verification._scope(tid), "worktree:" + ref["verification_id"])
        creation = {"repository": str(repo), "path": str(expected), "target_sha": context["target_sha"]}
        _require(proof is not None and proof["status"] == "accepted" and proof["tool"] == "release_verify_worktree"
                 and util.jload(proof["result_json"]) == creation, "发布验证树缺少创建回执")
        result[expected] = (None, context["target_sha"])
    return repo, result


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
    _require(not pool._git(path, "status", "--porcelain=v1", "-z", "--untracked-files=no",
                           "--ignore-submodules=none"), "工作树有未提交的已跟踪改动")
    return True


def _remove_tree(repo, path, branch, head, check, record_archive):
    if not _check_tree(repo, path, branch, head, audit._cwd_listing()):
        return
    extras = audit._file_plan(path)
    _require(all(item["rel"].startswith(".pi/") or git_delivery.is_build_artifact(item["rel"])
                 for item in extras), "工作树有未确认归属的未跟踪或忽略文件")
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


def close(conn, task, identity, now):
    tid = task["task_id"]
    operation = tid + ":" + str(identity["pr"]) + ":" + identity["head"]

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


def sweep(conn, now):
    # Two engines or a restarting process may observe the same pending receipt.
    # The kernel releases this local maintenance lock if the owner exits.
    lock_path = constants.app_support() / "merged-worktree-cleanup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _sweep(conn, now)


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
