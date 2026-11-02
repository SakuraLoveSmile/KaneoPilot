"""Offline, receipted relocation of one complete managed worktree root.

The old root is a compatibility alias for immutable evidence.  The configured
root, Git registrations and pool locations use the real destination.  No task,
round, contract, verification receipt, or historical lease is rewritten.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import uuid

from . import constants, db, runtime_operations, util, worktree_pool

_SCOPE = "worktree-root-relocation"
_TERMINAL = {"succeeded", "failed", "cancelled"}


def _require(condition, message):
    if not condition:
        raise util.KanpilotError("worktree_relocation_conflict", message)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json_sha(value):
    return _sha(util.jdump(value).encode("utf-8"))


def _git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"), timeout=120)
    _require(result.returncode == 0, "迁移 Git 操作失败：" +
             result.stderr.decode("utf-8", errors="replace")[-500:])
    return result.stdout


def _value(path, *args):
    return os.fsdecode(_git(path, *args)).strip()


def _absolute(value, field):
    _require(isinstance(value, str) and value and value == value.strip(), field + " 必须为绝对路径")
    path = Path(value).expanduser()
    _require(path.is_absolute() and ".." not in path.parts, field + " 必须为不含 .. 的绝对路径")
    path = Path(os.path.abspath(path))
    worktree_pool._no_links(path)
    return path


def _under(path, root):
    return path == root or root in path.parents


def _existing_parent(path):
    while not path.exists():
        path = path.parent
    return path


def _prefix(value, source, destination):
    if isinstance(value, str) and (value == str(source) or value.startswith(str(source) + os.sep)):
        return str(destination) + value[len(str(source)):]
    return value


def _manifest(root):
    """Rename-stable metadata covers ignored files too, without reading large builds."""
    entries = []
    markers = []
    def unreadable(error):
        raise util.KanpilotError("worktree_relocation_conflict", "无法完整盘点管理根：" + str(error)) from error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        directory = Path(directory)
        _require(".git" not in dirs, "管理根中包含主仓库，不能按 linked worktree 根迁移")
        for name in sorted(dirs + files):
            path = directory / name
            relative = str(path.relative_to(root))
            info = path.lstat()
            if name == ".git":
                _require(stat.S_ISREG(info.st_mode), "worktree .git 必须为普通文件")
                markers.append(directory)
                continue
            _require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode),
                     "管理根中包含不可迁移的特殊文件：" + relative)
            entry = [relative, stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_ino]
            if stat.S_ISREG(info.st_mode):
                entry.extend([info.st_size, info.st_mtime_ns])
            elif stat.S_ISLNK(info.st_mode):
                entry.append(os.readlink(path))
            entries.append(entry)
        dirs.sort()
    return sorted(entries), sorted(markers)


def _inventory(root):
    manifest, markers = _manifest(root)
    worktrees = []
    for path in markers:
        worktree_pool._no_links(path)
        common = Path(_value(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        listing = _git(path, "worktree", "list", "--porcelain", "-z").split(b"\0")
        registered = [os.fsdecode(v[9:]) for v in listing if v.startswith(b"worktree ")]
        _require(str(path) in registered and registered[0] != str(path), "工作树未按真实路径登记：" + str(path))
        _require(not _under(common, root), "Git common-dir 不得位于被迁移根内")
        gitdir = Path(_value(path, "rev-parse", "--absolute-git-dir"))
        index = gitdir / "index"
        _require(index.is_file() and not index.is_symlink(), "工作树 index 缺失或不安全")
        branch = subprocess.run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"],
                                capture_output=True, timeout=15)
        _require(branch.returncode in (0, 1), "无法核对工作树分支")
        worktrees.append({"relative": str(path.relative_to(root)), "repository": registered[0],
                          "common_dir": str(common), "gitdir": str(gitdir),
                          "head": _value(path, "rev-parse", "HEAD"),
                          "branch": os.fsdecode(branch.stdout).strip() if branch.returncode == 0 else None,
                          "index_sha256": _sha(index.read_bytes()),
                          "status_sha256": _sha(_git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")),
                          "gitfile_sha256": _sha((path / ".git").read_bytes())})
    _require(worktrees, "管理根内没有登记的 linked worktree")
    return {"manifest": manifest, "worktrees": worktrees}


def _alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_references(root):
    result = subprocess.run(["lsof", "-nP", "-Fpcn"], capture_output=True, text=True, timeout=60)
    _require(result.returncode in (0, 1), "无法核对运行进程的路径占用")
    references = []
    pid = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid != os.getpid():
            name = line[1:]
            if name == str(root) or name.startswith(str(root) + os.sep):
                references.append({"pid": pid, "path": name})
    processes = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                               text=True, timeout=30)
    _require(processes.returncode == 0, "无法核对调度应用是否已退出")
    for line in processes.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[1].startswith("/Applications/KaneoPilot.app/Contents/MacOS/"):
            references.append({"pid": int(fields[0]), "path": "KaneoPilot app is running"})
    return references


def _offline(conn, source, slots):
    from . import process_owner
    from .engine import health
    for row in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'task_workspace_preparing:%'"):
        _require(util.jload(row["value"]) is None, "存在尚未完成的任务工作区准备意图，须先恢复或完成再迁移")
    _require(not conn.execute("SELECT 1 FROM requests WHERE status='claimed' LIMIT 1").fetchone(),
             "存在已领取请求，须先完成或停止执行再迁移")
    instances = db.get_setting(conn, process_owner.SETTING, {}) or {}
    for entry in instances.values() if isinstance(instances, dict) else []:
        if not isinstance(entry, dict):
            continue
        active = (health._entry_active(entry, util.now_dt()) if "lease_expires_at" in entry
                  else process_owner.scheduler_active(entry))
        _require(not active and not process_owner._live_workers(entry), "存在活跃引擎、调度器或执行器")
    runs = db.get_setting(conn, "executor_runs", {}) or {}
    _require(not any(isinstance(run, dict) and _alive(run.get("pid"))
                     for run in runs.values()) if isinstance(runs, dict) else True,
             "存在存活执行端进程")
    for row in slots:
        _require(not _alive(row.get("owner_pid")), "槽位持有者进程仍存在，未迁移")
    _require(not _process_references(source), "源管理根仍被进程占用或 KaneoPilot 应用未退出")


def _job_candidates(conn, request, slot):
    from . import headless_adapter
    if slot["holder_stage"] == "plan":
        yield headless_adapter.resolve_source(conn).run_dir(request["task_id"], slot["holder_id"]) / "job.json"
    else:
        yield from (constants.app_support() / "executor-sessions" / "review").glob("*/job.json")
    # Desktop checkpoint directory, if this generation used the desktop backend.
    yield constants.app_support() / "runs" / str(request["task_id"]) / slot["holder_id"] / "job.json"


def _lease_changes(conn, slots, source, destination):
    changes = []
    identity = ("slot_id", "holder_id", "holder_stage", "lease_token", "path")
    for slot in slots:
        if slot["state"] != "leased":
            continue
        matches = []
        for row in conn.execute("SELECT * FROM requests WHERE run_id=? AND stage=?", (slot["holder_id"], slot["holder_stage"])):
            payload = db.request_payload(row)
            lease = payload.get("pool_lease")
            if isinstance(lease, dict) and all(lease.get(k) == slot[k] for k in identity):
                matches.append((dict(row), payload))
        _require(len(matches) == 1, "持有槽位没有唯一的同代请求，未迁移")
        request, payload = matches[0]
        _require(request["status"] in _TERMINAL, "槽位请求尚未结束，未迁移")
        jobs = []
        seen = set()
        for path in _job_candidates(conn, request, slot):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            worktree_pool._no_links(path)
            raw = path.read_bytes()
            try:
                job = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if (isinstance(job, dict) and job.get("request_id") == request["request_id"]
                    and job.get("task_id") == request["task_id"] and job.get("run_id") == slot["holder_id"]
                    and job.get("pool_lease") == payload["pool_lease"]):
                jobs.append((path, raw, job))
        _require(len(jobs) == 1, "持有槽位没有唯一且与请求一致的本地 job，未迁移")
        path, raw, job = jobs[0]
        _require(job.get("status") in {"failed", "finished", "cancelled", "completed", "error", "aborted", "timeout"}
                 and not _alive(job.get("pid")), "槽位 job 尚未停止本地运行，未迁移")
        # The approved location DTO lives outside contract/result.  Refuse a
        # future schema which embeds this identity inside a hashed object.
        for key in ("stage_contract", "stage_result"):
            value = job.get(key)
            if value is not None:
                _require(not _contains_pool_location(value, source), "冻结 contract/result 含槽位位置，需要专门迁移适配")
        if job.get("stage_contract") is not None:
            from . import stage_contract
            contract = stage_contract.validate_contract(job["stage_contract"])
            if job.get("stage_result") is not None:
                stage_contract.validate_result(job["stage_result"], contract)
        updated_payload = copy.deepcopy(payload)
        updated_job = copy.deepcopy(job)
        for value in (updated_payload["pool_lease"], updated_job["pool_lease"]):
            for key in ("path", "worktrees_root"):
                _require(isinstance(value.get(key), str), "槽位定位 DTO 缺少路径")
                value[key] = _prefix(value[key], source, destination)
        if "material_dir" in updated_job:
            updated_job["material_dir"] = _prefix(updated_job["material_dir"], source, destination)
        if "managed_material_paths" in updated_job:
            values = updated_job["managed_material_paths"]
            _require(isinstance(values, list) and all(isinstance(v, str) for v in values), "managed_material_paths 不是路径列表")
            updated_job["managed_material_paths"] = [_prefix(v, source, destination) for v in values]
        changes.append({"request_id": request["request_id"], "request_before": request["payload_json"],
                        "request_row_before": request,
                        "request_after": util.jdump(updated_payload), "job_path": str(path),
                        "job_before": raw, "job_after": util.jdump(updated_job).encode("utf-8"),
                        "job_mode": stat.S_IMODE(path.stat().st_mode)})
    return changes


def _contains_pool_location(value, source):
    if isinstance(value, dict):
        return any((key == "pool_lease" and isinstance(item, dict)) or _contains_pool_location(item, source)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_pool_location(item, source) for item in value)
    # Other old-root locations may deliberately remain in immutable contracts.
    return False


def _plan(conn, source, destination):
    configured = constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None)
    _require(configured.resolve() == source and not source.is_symlink(), "source_root 不是当前真实配置根")
    _require(source.is_dir(), "源管理根不存在")
    _require(not os.path.lexists(destination), "目的目录已存在，未覆盖")
    _require(not _under(destination, source) and not _under(source, destination), "源和目的根不得嵌套")
    parent = _existing_parent(destination)
    _require(parent.is_dir(), "目的路径的父级不是目录")
    _require(source.stat().st_dev == parent.stat().st_dev, "迁移必须在同一文件系统内原子改名")
    inventory = _inventory(source)
    slots = [dict(r) for r in conn.execute("SELECT * FROM worktree_slots ORDER BY slot_id")]
    for slot in slots:
        _require(slot["worktrees_root"] == str(source) and _under(Path(slot["path"]), source), "槽位不属于当前唯一管理根")
        worktree_pool._validate(slot)
    changes = _lease_changes(conn, slots, source, destination)
    _offline(conn, source, slots)
    signature = {"source_root": str(source), "destination_root": str(destination), "inventory": inventory,
                 "slots": slots, "leases": [{"request_id": r["request_id"],
                 "request_sha256": _json_sha(r["request_row_before"]),
                 "job_path": r["job_path"], "job_sha256": _sha(r["job_before"])} for r in changes]}
    return {"inventory": inventory, "slots": slots, "leases": changes, "audit_sha256": _json_sha(signature)}


def _repair(inventory, root):
    repositories = {}
    for item in inventory["worktrees"]:
        repositories.setdefault(item["repository"], []).append(str(root / item["relative"]))
    for repository, paths in repositories.items():
        _git(repository, "worktree", "repair", *paths)


def _write(path, content, mode=0o600):
    temporary = path.with_name(path.name + ".relocate-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _raw_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _restore_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _backup(ctx, plan, backup, settings):
    _require(not os.path.lexists(backup), "备份目录已存在，未覆盖")
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    destination = sqlite3.connect(str(backup / "database.sqlite"))
    try:
        ctx.conn.backup(destination)
        _require(destination.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                 "数据库备份完整性检查失败，未迁移")
    finally:
        destination.close()
    (backup / "database.sqlite").chmod(0o600)
    _write(backup / "database-integrity.json", util.jdump({"integrity_check": "ok",
        "sha256": _sha((backup / "database.sqlite").read_bytes())}).encode("utf-8"))
    _write(backup / "manifest.json", util.jdump(plan["inventory"]).encode("utf-8"))
    _write(backup / "settings.json", util.jdump(settings).encode("utf-8"))
    _write(backup / "slots.json", util.jdump(plan["slots"]).encode("utf-8"))
    for i, item in enumerate(plan["inventory"]["worktrees"]):
        folder = backup / ("git-%04d" % i)
        folder.mkdir(mode=0o700)
        gitdir = Path(item["gitdir"])
        for name in ("index", "gitdir", "HEAD"):
            if (gitdir / name).is_file():
                shutil.copy2(gitdir / name, folder / name)
    for i, change in enumerate(plan["leases"]):
        _write(backup / ("lease-%04d-request.json" % i), change["request_before"].encode("utf-8"))
        _write(backup / ("lease-%04d-job.json" % i), change["job_before"])
    _write(backup / "lease-mappings.json", util.jdump([
        {"request_id": change["request_id"], "job_path": change["job_path"],
         "request_before_sha256": _sha(change["request_before"].encode("utf-8")),
         "request_after_sha256": _sha(change["request_after"].encode("utf-8")),
         "job_before_sha256": _sha(change["job_before"]), "job_after_sha256": _sha(change["job_after"])}
        for change in plan["leases"]]).encode("utf-8"))


def _public(plan, source, destination, backup):
    return {"source_root": str(source), "destination_root": str(destination), "backup_dir": str(backup),
            "worktree_count": len(plan["inventory"]["worktrees"]),
            "repository_count": len({w["repository"] for w in plan["inventory"]["worktrees"]}),
            "pool_slot_count": len(plan["slots"]), "current_lease_count": len(plan["leases"]),
            "audit_sha256": plan["audit_sha256"]}


def cmd_worktree_relocate(ctx, payload):
    """Plan, then explicitly confirm a same-filesystem managed-root relocation."""
    source_raw = payload.get("source_root")
    destination = _absolute(payload.get("destination_root"), "destination_root")
    backup = _absolute(payload.get("backup_dir"), "backup_dir")
    operation_id = payload.get("operation_id")
    _require(isinstance(operation_id, str) and operation_id, "operation_id 必须为非空字符串")
    _require(isinstance(source_raw, str) and Path(source_raw).expanduser().is_absolute(), "source_root 必须为绝对路径")
    source = Path(os.path.abspath(Path(source_raw).expanduser()))
    _require(not _under(backup, source) and not _under(backup, destination), "备份目录必须在源和目的管理根之外")
    identity = {"source_root": str(source), "destination_root": str(destination), "backup_dir": str(backup)}
    previous = db.get_runtime_operation(ctx.conn, _SCOPE, operation_id)
    if previous is not None:
        _require(previous["request_sha256"] == runtime_operations.compute_request_sha256(identity), "operation_id 参数不同")
        _require(previous["status"] == "accepted", "此迁移已执行或失败，先核对其备份与回执")
        result = util.jload(previous["result_json"])
        _require(source.is_symlink() and source.resolve() == destination and destination.is_dir(), "已完成迁移的目录状态不符")
        _require(constants.worktrees_root(db.get_setting(ctx.conn, "worktrees_root")).resolve() == destination, "已完成迁移的配置根不符")
        current = _inventory(destination)
        _require(_json_sha(current) == result["inventory_sha256"], "完成后目录内容或 Git 身份已变化，未重做迁移")
        for slot in ctx.conn.execute("SELECT * FROM worktree_slots"):
            worktree_pool._validate(slot)
        return {**result, "replayed": True}
    source = _absolute(str(source), "source_root")
    plan = _plan(ctx.conn, source, destination)
    response = _public(plan, source, destination, backup)
    if payload.get("dry_run", True):
        return {**response, "status": "planned", "dry_run": True}
    _require(payload.get("confirm") is True, "执行迁移必须 confirm=true")
    _require(payload.get("expected_audit_sha256") == plan["audit_sha256"], "审计摘要已变化或缺失，未迁移")
    _require(not os.path.lexists(backup), "备份目录已存在，未覆盖")
    settings = {key: _raw_setting(ctx.conn, key) for key in ("worktrees_root", "paused_global")}
    projection_path = constants.app_support() / "worktree-slots.json"
    projection_before = projection_path.read_bytes() if projection_path.exists() else None
    _backup(ctx, plan, backup, settings)
    _write(backup / "operation.json", util.jdump({**identity, "operation_id": operation_id,
                                                "audit_sha256": plan["audit_sha256"]}).encode("utf-8"))
    try:
        with db.write_txn(ctx.conn):
            _require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND status IN ('prepared','unknown') LIMIT 1",
                                          (_SCOPE,)).fetchone(), "已有尚未完成的根迁移，先核对该回执")
            _require(db.get_runtime_operation(ctx.conn, _SCOPE, operation_id) is None,
                     "此迁移已由另一调用执行，请重新核对回执")
            _require(all(_raw_setting(ctx.conn, key) == value for key, value in settings.items()),
                     "迁移准备期间配置或暂停状态发生变化")
            runtime_operations.begin_operation(ctx.conn, _SCOPE, operation_id, "core", "worktree_root_relocate", identity)
            runtime_operations.mark_operation_unknown(ctx.conn, _SCOPE, operation_id)
            db.set_setting(ctx.conn, "paused_global", True)
    except BaseException:
        # db.write_txn predates this maintenance path and handles Exception.
        # Also release an interrupted pre-intent transaction without side effects.
        if ctx.conn.in_transaction:
            ctx.conn.execute("ROLLBACK")
        raise
    locked = False
    try:
        # This explicit offline maintenance transaction also excludes installed
        # writers which do not consult paused_global.  Git is intentionally
        # allowed inside this reservation; normal pool operations stay unchanged.
        ctx.conn.execute("BEGIN IMMEDIATE")
        locked = True
        guard = getattr(ctx.conn, "authorization_guard", None)
        if guard is not None:
            guard(ctx.conn)
        _require(_raw_setting(ctx.conn, "worktrees_root") == settings["worktrees_root"]
                 and db.get_setting(ctx.conn, "paused_global") is True,
                 "取得维护保留前配置或暂停状态发生变化")
        refreshed = _plan(ctx.conn, source, destination)
        _require(refreshed["audit_sha256"] == plan["audit_sha256"], "迁移准备期间目录或状态发生变化")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        _repair(plan["inventory"], destination)
        source.symlink_to(destination, target_is_directory=True)
        after = _inventory(destination)
        _require(after == plan["inventory"], "改名后内容或 Git 身份与迁前不符")
        for change in plan["leases"]:
            path = Path(change["job_path"])
            _require(path.read_bytes() == change["job_before"], "槽位 job 在迁移期间发生变化")
            _write(path, change["job_after"], change["job_mode"])
        for slot in plan["slots"]:
            current = ctx.conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (slot["slot_id"],)).fetchone()
            _require(current is not None and dict(current) == slot, "槽位代次或状态发生变化")
            ctx.conn.execute("UPDATE worktree_slots SET path=?,worktrees_root=? WHERE slot_id=?",
                             (_prefix(slot["path"], source, destination), str(destination), slot["slot_id"]))
        for change in plan["leases"]:
            current = ctx.conn.execute("SELECT * FROM requests WHERE request_id=?", (change["request_id"],)).fetchone()
            _require(current is not None and dict(current) == change["request_row_before"],
                     "槽位请求身份或状态在迁移期间发生变化")
            _require(Path(change["job_path"]).read_bytes() == change["job_after"], "槽位 job 在提交前发生变化")
            ctx.conn.execute("UPDATE requests SET payload_json=? WHERE request_id=?", (change["request_after"], change["request_id"]))
        db.set_setting(ctx.conn, "worktrees_root", str(destination))
        for slot in ctx.conn.execute("SELECT * FROM worktree_slots"):
            worktree_pool._validate(slot)
        worktree_pool._projection(ctx.conn)
        result = {**response, "status": "relocated", "dry_run": False,
                  "compatibility_alias": str(source), "inventory_sha256": _json_sha(after)}
        _write(backup / "result.json", util.jdump(result).encode("utf-8"))
        for change in plan["leases"]:
            _require(Path(change["job_path"]).read_bytes() == change["job_after"], "槽位 job 在提交前发生变化")
        _restore_setting(ctx.conn, "paused_global", settings["paused_global"])
        runtime_operations.complete_operation(ctx.conn, _SCOPE, operation_id, result=result)
        ctx.conn.execute("COMMIT")
        locked = False
        return result
    except BaseException as error:
        rollback_errors = []
        # A commit may have succeeded immediately before an interrupt.  Never
        # move files back against a durable accepted database state.
        if locked and not ctx.conn.in_transaction:
            rollback_errors.append("database: 事务已经结束，须核对持久化回执后恢复")
        elif not locked:
            rollback_errors.append("database: 未取得维护写保留，未执行文件回滚")
        else:
            # Inspect actual bytes instead of a post-write flag: an interrupt
            # can occur after os.replace/rename succeeded but before it returned.
            for change in reversed(plan["leases"]):
                try:
                    path = Path(change["job_path"])
                    current = path.read_bytes()
                    _require(current in (change["job_before"], change["job_after"]),
                             "job发生外部变动，不能回滚覆盖")
                    if current == change["job_after"]:
                        _write(path, change["job_before"], change["job_mode"])
                except BaseException as exc:
                    rollback_errors.append("job: " + str(exc))
            if not rollback_errors:
                try:
                    if destination.is_dir():
                        _require(not _process_references(destination), "新根出现运行占用，不能回滚搬迁")
                        if source.is_symlink() and source.resolve() == destination:
                            source.unlink()
                        _require(not os.path.lexists(source), "原根被重新占用，不能回迁")
                        destination.rename(source)
                        _repair(plan["inventory"], source)
                    _require(source.is_dir() and not source.is_symlink()
                             and not os.path.lexists(destination), "根目录状态不符，不能确认回迁")
                    _require(_inventory(source) == plan["inventory"], "回迁核验失败")
                except BaseException as exc:
                    rollback_errors.append("filesystem: " + str(exc))
            if not rollback_errors:
                try:
                    if projection_before is not None:
                        _write(projection_path, projection_before)
                    elif projection_path.exists():
                        projection_path.unlink()
                except BaseException as exc:
                    rollback_errors.append("projection: " + str(exc))
            try:
                ctx.conn.execute("ROLLBACK")
                locked = False
            except BaseException as exc:
                rollback_errors.append("database: " + str(exc))
        try:
            with db.write_txn(ctx.conn):
                if not rollback_errors:
                    _require(db.get_setting(ctx.conn, "paused_global") is True,
                             "暂停状态发生外部变动，不能回滚覆盖")
                    _restore_setting(ctx.conn, "paused_global", settings["paused_global"])
                else:
                    db.set_setting(ctx.conn, "paused_global", True)
                operation = db.get_runtime_operation(ctx.conn, _SCOPE, operation_id)
                if operation is None or operation["status"] != "accepted":
                    runtime_operations.fail_operation(ctx.conn, _SCOPE, operation_id,
                        result={"backup_dir": str(backup), "rolled_back": not rollback_errors,
                                "error": str(error), "rollback_errors": rollback_errors})
            _write(backup / "failure.json", util.jdump({"rolled_back": not rollback_errors,
                "error": str(error), "rollback_errors": rollback_errors}).encode("utf-8"))
        except BaseException as exc:
            rollback_errors.append("receipt: " + str(exc))
        if rollback_errors:
            raise util.KanpilotError("worktree_relocation_recovery_required",
                "迁移失败且回滚不完整；保持暂停，核对备份 " + str(backup) + "：" + "; ".join(rollback_errors)) from error
        raise util.KanpilotError("worktree_relocation_rolled_back", "迁移失败，已回滚；备份 " + str(backup) + "：" + str(error)) from error
