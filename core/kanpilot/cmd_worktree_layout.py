"""One-off, receipted migration to the task stage-tree layout (protocol §5.29.6).

Offline maintenance only: no claim, send, resume or execution. It moves each
task's execution worktree from ``<task>/branch`` to ``<task>/run``, archives the
legacy pooled slots to ``<worktrees_root>/.archives`` and forgets the schema 6
pool rows. Historic request payloads, contracts and receipts are never rewritten.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

from . import constants, db, runtime_operations, util, worktree_pool

_SCOPE = "worktree-layout-migrate"
_TOOL = "worktree_layout_migrate"
_HARNESS = "core"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require(condition, message: str) -> None:
    util.require(condition, message, "worktree_migration_conflict")


def _absolute(value, field: str) -> Path:
    _require(isinstance(value, str) and value.strip() and value == value.strip(),
             field + " 必须为绝对路径")
    path = Path(value).expanduser()
    _require(path.is_absolute() and ".." not in path.parts, field + " 必须为不含 .. 的绝对路径")
    return Path(os.path.abspath(path))


def _root(conn) -> Path:
    return constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None).expanduser().resolve()


def _git(path: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, timeout=120,
                          env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))


def _value(path: Path, *args) -> str:
    result = _git(path, *args)
    _require(result.returncode == 0, "Git 读取失败：" + result.stderr.decode("utf-8", "replace")[-300:])
    return result.stdout.decode("utf-8", "replace").strip()


def _moves(conn, root: Path) -> tuple[list[dict], list[dict]]:
    moves, blockers = [], []
    for raw in conn.execute("SELECT * FROM tasks ORDER BY task_id").fetchall():
        task = dict(raw)
        extra = db.task_extra(raw)
        delivery = extra.get("delivery") or {}
        path_value = delivery.get("worktree_path")
        if not isinstance(path_value, str) or not path_value:
            continue
        source = Path(path_value)
        if source.name != "branch" or source.parent.parent != root:
            continue
        target = source.parent / "run"
        entry = {"task_id": task["task_id"], "repository": task.get("repository"),
                 "branch": delivery.get("branch"), "head_sha": delivery.get("head_sha"),
                 "from": str(source), "to": str(target),
                 "task_fingerprint": _sha(util.jdump(dict(task)))}
        moves.append(entry)
        if not source.is_dir():
            blockers.append({"code": "move_source_missing", "task_id": task["task_id"],
                             "detail": str(source)})
            continue
        if os.path.lexists(target):
            blockers.append({"code": "move_target_exists", "task_id": task["task_id"],
                             "detail": str(target)})
        if not task.get("repository") or not Path(task["repository"]).is_dir():
            blockers.append({"code": "move_repository_missing", "task_id": task["task_id"],
                             "detail": str(task.get("repository"))})
            continue
        repository = Path(task["repository"])
        registered = _git(repository, "worktree", "list", "--porcelain", "-z")
        registered_paths = {os.fsdecode(item[9:]) for item in registered.stdout.split(b"\0")
                            if item.startswith(b"worktree ")}
        if registered.returncode != 0 or str(source.resolve()) not in registered_paths:
            blockers.append({"code": "move_not_git_worktree", "task_id": task["task_id"],
                             "detail": str(source)})
            continue
        if _value(source, "rev-parse", "--show-toplevel") != str(source.resolve()):
            blockers.append({"code": "move_not_git_worktree", "task_id": task["task_id"],
                             "detail": "worktree top-level 不符"})
            continue
        if entry["branch"] and _value(source, "rev-parse", "--abbrev-ref", "HEAD") != entry["branch"]:
            blockers.append({"code": "move_head_mismatch", "task_id": task["task_id"],
                             "detail": str(entry["branch"])})
        if entry["head_sha"] and _value(source, "rev-parse", "HEAD") != entry["head_sha"]:
            blockers.append({"code": "move_head_mismatch", "task_id": task["task_id"],
                             "detail": str(entry["head_sha"])})
    return moves, blockers


def _inventory(conn, root: Path) -> dict:
    from .engine import health

    moves, blockers = _moves(conn, root)
    if health.active_instance_count(conn, util.now_iso()):
        blockers.append({"code": "engine_active", "detail": "本机引擎仍在运行，先停止引擎"})
    leased = conn.execute("SELECT COUNT(*) FROM worktree_slots WHERE state='leased'").fetchone()[0]
    if leased:
        blockers.append({"code": "stage_tree_leased", "detail": f"{leased} 个阶段树仍被租用"})
    active = conn.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued','claimed')").fetchone()[0]
    if active:
        blockers.append({"code": "request_active", "detail": f"{active} 个请求排队或执行中"})
    pending = conn.execute(
        "SELECT COUNT(*) FROM runtime_operations WHERE status IN ('prepared','unknown') "
        "AND (scope_id LIKE 'worktree%' OR scope_id LIKE 'worktree-slot:%' OR tool LIKE 'worktree%')"
    ).fetchone()[0]
    if pending:
        blockers.append({"code": "operation_pending", "detail": f"{pending} 个工作树相关操作尚未确认"})
    pool = root / "pool"
    legacy = [dict(row) for row in conn.execute(
        "SELECT slot_id,path,state FROM worktree_slots WHERE task_id IS NULL ORDER BY path").fetchall()]
    for row in legacy:
        if row["state"] == "leased":
            blockers.append({"code": "legacy_row_leased", "detail": row["path"]})
    inventory = {"root": str(root), "moves": moves,
                 "pool": str(pool) if pool.is_dir() else None,
                 "legacy_rows": legacy, "blockers": blockers}
    inventory["audit_sha256"] = _sha(util.jdump(inventory))
    return inventory


def _public(inventory: dict) -> dict:
    return {"root": inventory["root"],
            "moves": [{"task_id": item["task_id"], "from": item["from"], "to": item["to"]}
                      for item in inventory["moves"]],
            "pool": inventory["pool"],
            "legacy_rows": len(inventory["legacy_rows"]),
            "blockers": inventory["blockers"],
            "audit_sha256": inventory["audit_sha256"]}


def _backup(directory: Path, inventory: dict, conn) -> None:
    directory.mkdir(parents=True)
    (directory / "audit.json").write_text(util.jdump(inventory) + "\n", encoding="utf-8")
    tasks = [dict(row) for row in conn.execute(
        "SELECT task_id,extra_json FROM tasks WHERE extra_json LIKE '%\"delivery\"%' ORDER BY task_id")]
    (directory / "tasks.json").write_text(util.jdump(tasks) + "\n", encoding="utf-8")
    (directory / "worktree_slots.json").write_text(
        util.jdump([dict(row) for row in conn.execute("SELECT * FROM worktree_slots ORDER BY path")]) + "\n",
        encoding="utf-8")


def _move_worktree(entry: dict) -> None:
    repository = Path(entry["repository"]).expanduser().resolve()
    result = _git(repository, "worktree", "move", entry["from"], entry["to"])
    _require(result.returncode == 0,
             "移动工作树失败：" + result.stderr.decode("utf-8", "replace")[-300:])
    target = Path(entry["to"])
    _require(target.is_dir(), "移动后的工作树目录不存在：" + entry["to"])
    if entry["branch"]:
        _require(_value(target, "rev-parse", "--abbrev-ref", "HEAD") == entry["branch"],
                 "移动后的工作树分支不符：" + entry["to"])


def _update_delivery(conn, entry: dict, now: str) -> None:
    with db.write_txn(conn):
        task = db.require_task(conn, entry["task_id"])
        _require(_sha(util.jdump(dict(task))) == entry["task_fingerprint"],
                 "任务记录在迁移期间变化，未改写登记：" + entry["task_id"])
        extra = db.task_extra(task)
        delivery = extra.get("delivery") or {}
        _require(delivery.get("worktree_path") == entry["from"],
                 "任务工作区登记与预览不符，未改写：" + entry["task_id"])
        delivery["worktree_path"] = entry["to"]
        extra["delivery"] = delivery
        db.update_task(conn, entry["task_id"], now, extra_json=extra)


def _archive_pool(root: Path, pool: Path, date: str) -> dict:
    archives = root / ".archives"
    archives.mkdir(parents=True, exist_ok=True)
    target = archives / ("pool-legacy-" + date + ".tar.gz")
    _require(not target.exists(), "归档包已存在，未覆盖：" + str(target))
    with tarfile.open(target, "w:gz") as handle:
        handle.add(pool, arcname="pool")
    with tarfile.open(target, "r:gz") as handle:
        members = handle.getnames()
    _require(len(members) > 0, "归档包为空，未删除旧池：" + str(target))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    shutil.rmtree(pool)
    return {"path": str(target), "sha256": digest, "members": len(members)}


def _verify_final(conn, root: Path, result: dict) -> None:
    for entry in result.get("moved", []):
        _require(not os.path.lexists(entry["from"]), "已迁移的工作树旧路径仍存在：" + entry["from"])
        _require(Path(entry["to"]).is_dir(), "已迁移的工作树目录缺失：" + entry["to"])
    if result.get("pool_archived"):
        _require(not (root / "pool").exists(), "旧池目录仍存在")
        _require(Path(result["pool_archived"]["path"]).is_file(), "旧池归档缺失")
    _require(conn.execute("SELECT COUNT(*) FROM worktree_slots WHERE task_id IS NULL").fetchone()[0] == 0,
             "旧池记录仍存在")


def cmd_worktree_layout_migrate(ctx, payload):
    root = _root(ctx.conn)
    backup = _absolute(payload.get("backup_dir"), "backup_dir")
    operation_id = payload.get("operation_id")
    _require(isinstance(operation_id, str) and operation_id.strip() == operation_id and operation_id,
             "operation_id 必须为非空字符串")
    _require(not backup.is_relative_to(root), "备份目录不能位于工作树根内")
    identity = {"root": str(root), "backup_dir": str(backup), "operation_id": operation_id}
    previous = db.get_runtime_operation(ctx.conn, _SCOPE, operation_id)
    if previous is not None:
        _require(previous["request_sha256"] == runtime_operations.compute_request_sha256(identity),
                 "operation_id 参数不同")
        _require(previous["status"] == "accepted", "前次迁移尚未确认完成，先核对回执")
        result = util.jload(previous["result_json"])
        _verify_final(ctx.conn, root, result)
        return {**result, "replayed": True}
    inventory = _inventory(ctx.conn, root)
    response = _public(inventory)
    if payload.get("dry_run", True):
        return {**response, "status": "blocked" if inventory["blockers"] else "planned",
                "dry_run": True}
    _require(payload.get("confirm") is True, "执行迁移必须 confirm=true")
    _require(payload.get("expected_audit_sha256") == inventory["audit_sha256"],
             "审计摘要已变化或缺失，未迁移")
    _require(not inventory["blockers"], "迁移前置条件未满足：" +
             "、".join(sorted({item["code"] for item in inventory["blockers"]})))
    _require(not os.path.lexists(backup), "备份目录已存在，未覆盖")
    _backup(backup, inventory, ctx.conn)
    with db.write_txn(ctx.conn):
        _require(db.get_runtime_operation(ctx.conn, _SCOPE, operation_id) is None,
                 "此迁移已由另一调用执行，请核对回执")
        _require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
                                      "AND status IN ('prepared','unknown') LIMIT 1",
                                      (_SCOPE,)).fetchone(), "已有尚未完成的布局迁移")
        runtime_operations.begin_operation(ctx.conn, _SCOPE, operation_id, _HARNESS, _TOOL, identity)
        result = {"root": str(root), "backup_dir": str(backup), "identified": identity,
                  "moved": [], "pool_archived": None, "legacy_rows_deleted": 0}
        runtime_operations.mark_operation_unknown(ctx.conn, _SCOPE, operation_id)
    try:
        for entry in inventory["moves"]:
            _move_worktree(entry)
            _update_delivery(ctx.conn, entry, ctx.now_iso)
            result["moved"].append({"task_id": entry["task_id"], "from": entry["from"],
                                    "to": entry["to"]})
        if inventory["pool"]:
            result["pool_archived"] = _archive_pool(root, Path(inventory["pool"]),
                                                    datetime.now().strftime("%Y%m%d"))
        with db.write_txn(ctx.conn):
            result["legacy_rows_deleted"] = ctx.conn.execute(
                "DELETE FROM worktree_slots WHERE task_id IS NULL").rowcount
        worktree_pool.projection(ctx.conn)
    except Exception as exc:
        with db.write_txn(ctx.conn):
            runtime_operations.complete_operation(ctx.conn, _SCOPE, operation_id,
                                                  status="unknown",
                                                  result={**result, "error": str(exc)})
        if isinstance(exc, util.KanpilotError):
            raise
        raise util.KanpilotError("worktree_migration_failed",
                                 "迁移中断，已保留现场：" + str(exc)) from exc
    with db.write_txn(ctx.conn):
        runtime_operations.complete_operation(ctx.conn, _SCOPE, operation_id, result=result)
    return {**result, "ok": True}
