"""Explicit, backed-up manual recovery of abandoned historical Plus leases.

This command is never called by the automatic reaper. Human authorization is
recorded separately from the missing Plus terminal proof.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile

from . import constants, db, headless_adapter, runtime_operations, util
from . import worktree_pool as pool

_TERMINAL = ("succeeded", "failed", "cancelled")
_LEASE_KEYS = ("slot_id", "holder_id", "holder_stage", "lease_token", "path")
_TOOL = "worktree-slot-recover"
_HARNESS = "core_manual"


def _require(value, message):
    util.require(value, message, "conflict")


def _dead(pid):
    _require(type(pid) is int and pid > 0, "原持有进程身份缺失")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    raise util.KanpilotError("conflict", "原持有进程仍存活或无法核实，未恢复槽位")


def _checkpoint(conn, row):
    requests = conn.execute("SELECT * FROM requests WHERE run_id=? AND stage=?",
                            (row["holder_id"], row["holder_stage"])).fetchall()
    _require(len(requests) == 1, "原请求缺失或身份不唯一")
    request = requests[0]
    payload = db.request_payload(request)
    config = payload.get("review_config") or {}
    backend = payload.get("planner_backend" if row["holder_stage"] == "plan" else "review_backend")
    if backend is None and isinstance(config, dict):
        backend = config.get("backend")
    _require(request["status"] in _TERMINAL and backend == "pi_desktop",
             "仅能人工恢复已终态的原 Plus 规划/评审请求")
    frozen = payload.get("pool_lease")
    _require(isinstance(frozen, dict) and all(frozen.get(k) == row[k] for k in _LEASE_KEYS),
             "原请求冻结租约不匹配")
    if row["holder_stage"] == "plan":
        paths = [headless_adapter.resolve_source(conn).run_dir(
            request["task_id"], row["holder_id"]) / "job.json"]
    else:
        paths = (constants.app_support() / "executor-sessions" / "review").glob("*/job.json")
    matches = []
    for path in paths:
        pool._no_links(path)
        try:
            job = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if (isinstance(job, dict) and job.get("request_id") == request["request_id"]
                and job.get("task_id") == request["task_id"]
                and job.get("run_id") == row["holder_id"]):
            matches.append((path, job))
    _require(len(matches) == 1, "原执行检查点缺失或身份不唯一")
    path, job = matches[0]
    lease = job.get("pool_lease")
    _require(job.get("backend") == "pi_desktop"
             and job.get("status") in ("finished", "failed", "cancelled")
             and job.get("pid") == row["owner_pid"]
             and isinstance(lease, dict) and all(lease.get(k) == row[k] for k in _LEASE_KEYS)
             and all(isinstance(row[k], str) and row[k] and job.get(k) == row[k]
                     for k in ("session_id", "turn_id"))
             and (row["holder_stage"] != "review" or
                  (job.get("stage") == "review" and job.get("invocation_id") == path.parent.name)),
             "原检查点终态、进程、会话或租约身份不符")
    return request, path, job


def _file_set(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                files.append(name)
                dirs.remove(name)
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                result[relative] = ("link", os.readlink(path))
            else:
                _require(stat.S_ISREG(mode), "槽位包含特殊文件，未恢复")
                result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(mode))
    return result


def _backup(row, directory):
    root = Path(directory).expanduser()
    _require(root.is_absolute(), "备份目录必须为绝对路径")
    pool._no_links(root)
    _require(not root.is_relative_to(Path(row["path"])), "备份不能位于待恢复槽位中")
    manifest_path = root / "manifest.json"
    pool._no_links(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    entries = [entry for entry in manifest.get("slots", [])
               if entry.get("slot_id") == row["slot_id"]]
    _require(len(entries) == 1, "备份清单缺少唯一槽位身份")
    entry = entries[0]
    _require(all(entry.get(k) == row[k] for k in ("holder_id", "path", "sha")),
             "备份不属于当前租约或冻结提交")
    name = entry.get("archive")
    _require(isinstance(name, str) and name and Path(name).name == name, "备份归档路径无效")
    archive = root / name
    pool._no_links(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    _require(digest == entry.get("archive_sha256"), "备份归档校验失败")
    slot = Path(row["path"])
    contents = {}
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            relative = PurePosixPath(member.name)
            _require(not relative.is_absolute() and ".." not in relative.parts,
                     "备份归档包含越界路径")
            # The original backup also retains the Git administrative index.
            if relative == PurePosixPath(slot.name + "-git-index"):
                _require(member.isfile(), "备份 Git 索引类型无效")
                continue
            _require(relative.parts and relative.parts[0] == slot.name,
                     "备份包含其他目录")
            if member.isdir():
                continue
            key = relative.relative_to(slot.name).as_posix()
            _require(key not in contents, "备份含重复文件")
            if member.issym():
                contents[key] = ("link", member.linkname)
            else:
                _require(member.isfile(), "备份包含不支持的文件类型")
                contents[key] = ("file", hashlib.sha256(handle.extractfile(member).read()).hexdigest(), member.mode)
    _require(contents == _file_set(slot), "备份与当前槽位文件不完整或不一致")
    return digest


def _preflight(conn, payload):
    row = pool._owned(conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")))
    _require(row["state"] == "leased" and row["owner_state"] == "unknown"
             and row["holder_stage"] in ("plan", "review"), "槽位不属于待人工恢复的未知旧租约")
    pool.validate_lease(conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")))
    _require(not pool._unresolved_operation(conn, row), "槽位仍有关联未决操作")
    _dead(row["owner_pid"])
    request, path, job = _checkpoint(conn, row)
    digest = _backup(row, payload["backup_dir"])
    result = {"slot_id": row["slot_id"], "holder_id": row["holder_id"],
              "request_id": request["request_id"], "path": row["path"],
              "state": "ready", "recovery_kind": "human_confirmed",
              "plus_terminal": "unknown", "backup_sha256": digest,
              "operation_id": payload["operation_id"], "reason": payload["reason"]}
    result["intent"] = {k: row[k] for k in
                        ("holder_stage", "owner_pid", "session_id", "turn_id", "sha")}
    result["intent"].update(task_id=request["task_id"], backup_dir=payload["backup_dir"])
    return row, request, path, job, result


def cmd_worktree_slot_recover(ctx, payload):
    from . import desktop_queue, task_workspace

    _require(bool(payload["reason"].strip()), "人工恢复原因不能为空")
    scope = "manual-worktree-recovery:" + payload["slot_id"]
    operation_id = payload["operation_id"]
    identity = {k: payload[k] for k in
                ("slot_id", "holder_id", "lease_token", "operation_id", "backup_dir", "reason")}
    existing = db.get_runtime_operation(ctx.conn, scope, operation_id)
    if existing is not None:
        _require(existing["request_sha256"] == runtime_operations.compute_request_sha256(identity)
                 and existing["tool"] == _TOOL and existing["harness_id"] == _HARNESS,
                 "人工恢复 operation_id 参数不匹配")
        if existing["status"] == "accepted":
            return util.jload(existing["result_json"])
        raise util.KanpilotError("conflict", "前次人工恢复结果尚未确认，保留现场，请先核对回执")
    _require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
                                 "AND status IN ('prepared','unknown') LIMIT 1", (scope,)).fetchone(),
             "该槽位已有未确认的人工恢复，保留现场")
    row, request, path, job, result = _preflight(ctx.conn, payload)
    if payload.get("dry_run", True):
        return result
    _require(payload.get("confirm") is True, "人工恢复需要 confirm=true")
    with desktop_queue._serialized(path.parent):
        row, request, path, job, result = _preflight(ctx.conn, payload)
        frozen_payload = db.request_payload(request)
        with db.write_txn(ctx.conn):
            _require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
                                         "AND status IN ('prepared','unknown') LIMIT 1", (scope,)).fetchone(),
                     "该槽位已有未确认的人工恢复，保留现场")
            _require(db.get_runtime_operation(ctx.conn, scope, operation_id) is None,
                     "人工恢复回执已变化，请重新核对")
            runtime_operations.begin_operation(ctx.conn, scope, operation_id, _HARNESS, _TOOL, identity)
            runtime_operations.complete_operation(ctx.conn, scope, operation_id,
                                                  status="unknown", result=result)
        # Never manufacture owner_stopped or a remote terminal observation.
        task_workspace.preserve_pool_artifacts(job, path.parent)
        desktop_queue._write(path, job)
        current = db.get_request(ctx.conn, request["request_id"])
        _require(current is not None and current["status"] in _TERMINAL
                 and current["run_id"] == row["holder_id"]
                 and current["stage"] == row["holder_stage"]
                 and current["task_id"] == request["task_id"]
                 and db.request_payload(current) == frozen_payload,
                 "保全期间原请求身份或冻结输入已变化")
        _dead(row["owner_pid"])
        fresh = pool.validate_lease(ctx.conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")))
        _require(all(fresh[k] == row[k] for k in
                     ("owner_state", "owner_pid", "session_id", "turn_id", "sha")),
                 "保全期间槽位持有者状态已变化")
        _backup(fresh, payload["backup_dir"])
        released = pool.release(ctx.conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")),
                                owner_stopped=True,
                                expected_owner={k: row[k] for k in
                                                ("owner_state", "owner_pid", "session_id", "turn_id", "sha")},
                                expected_request=request)
        result["state"] = released["state"]
        result["reason"] = released["reason"] or payload["reason"]
        job.update(pool_release_state=released["state"], pool_released=released["state"] == "free",
                   pool_manual_recovery={"scope_id": scope, "operation_id": operation_id,
                                         "plus_terminal": "unknown"})
        desktop_queue._write(path, job)
        with db.write_txn(ctx.conn):
            runtime_operations.complete_operation(ctx.conn, scope, operation_id, result=result)
        return result
