"""Bounded detached worktrees. SQLite owns leases; Git never runs in a txn."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from . import constants, db, review_snapshot, runtime_operations, util


def _git(path, *args, content=None):
    try:
        result = subprocess.run(["git", "-c", "core.filemode=true", "-C", str(path), *args],
                                input=content, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("worktree_invalid", "槽位Git操作失败：" + str(exc)) from exc
    if result.returncode:
        raise util.KanpilotError("worktree_invalid", "槽位Git操作失败：" +
                                 result.stderr.decode("utf-8", errors="replace")[-400:])
    return result.stdout


def _value(path, *args):
    return _git(path, *args).decode("utf-8").strip()


def _common(path):
    value = Path(_value(path, "rev-parse", "--git-common-dir"))
    return (value if value.is_absolute() else Path(path) / value).resolve()


def repository_identity(repository):
    """The first registered worktree is the canonical primary checkout."""
    repository = Path(repository).expanduser().resolve()
    common = _common(repository)
    records = _git(repository, "worktree", "list", "--porcelain", "-z").split(b"\0")
    first = next((os.fsdecode(item[9:]) for item in records if item.startswith(b"worktree ")), None)
    if first is None:
        raise util.KanpilotError("worktree_invalid", "无法识别真实主仓库")
    primary = Path(first).resolve()
    if _common(primary) != common or Path(_value(primary, "rev-parse", "--show-toplevel")).resolve() != primary:
        raise util.KanpilotError("worktree_invalid", "真实主仓库身份不一致")
    return {"repository": str(primary), "git_common_dir": str(common),
            "repo_key": util.sha256_bytes(os.fsencode(common))[:16]}


def _no_links(path):
    current = Path(path)
    for item in (current, *current.parents):
        if item.is_symlink():
            # macOS publishes temporary/application paths through root-owned
            # filesystem aliases. These are canonicalized before registration;
            # user-created links anywhere below them remain forbidden.
            if (str(item) in ("/var", "/tmp", "/etc") and item.lstat().st_uid == 0
                    and item.resolve() == Path("/private") / item.name):
                continue
            raise util.KanpilotError("worktree_invalid", "槽位路径不可包含符号链接")


_STAGES = ("plan", "review", "run")
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _stage_path(root, task_id, stage):
    """<worktrees_root>/<task_id>/<stage>；身份与形状在此唯一判定。"""
    if stage not in _STAGES:
        raise util.KanpilotError("worktree_invalid", "阶段树阶段无效")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise util.KanpilotError("worktree_invalid", "阶段树任务标识无效")
    root = Path(root)
    if not root.is_absolute():
        raise util.KanpilotError("worktree_invalid", "工作树根必须为绝对路径")
    path = root / task_id / stage
    _no_links(path)
    return path


def _pool_path(row):
    """Legacy pool row (schema 6): read-only diagnosis, never allocated."""
    path, root = Path(row["path"]), Path(row["worktrees_root"])
    if (not path.is_absolute() or not root.is_absolute()
            or not re.fullmatch(r"slot-[1-9][0-9]*", path.name)
            or path.parent != root / "pool" / row["repo_key"]):
        raise util.KanpilotError("worktree_invalid", "槽位路径与登记不一致")
    _no_links(path)
    return path


def _validate(row, *, existing=True, conn=None):
    if row["stage"] is None:
        path = _pool_path(row)
    else:
        path = _stage_path(Path(row["worktrees_root"]), row["task_id"], row["stage"])
        if row["path"] != str(path):
            raise util.KanpilotError("worktree_invalid", "阶段树路径与登记不一致")
    identity = repository_identity(row["repository"])
    if any(identity[key] != row[key] for key in identity):
        raise util.KanpilotError("worktree_invalid", "工作树仓库身份与登记不一致")
    if existing:
        if not path.is_dir():
            raise util.KanpilotError("worktree_invalid", "工作树目录不存在")
        if (_common(path) != Path(row["git_common_dir"])
                or Path(_value(path, "rev-parse", "--show-toplevel")).resolve() != path):
            raise util.KanpilotError("worktree_invalid", "工作树不是登记的独立工作树")
        branch = _value(path, "rev-parse", "--abbrev-ref", "HEAD")
        if row["stage"] == "run":
            task = db.get_task(conn, row["task_id"]) if conn is not None else None
            expected_branch = ((db.task_extra(task).get("delivery") or {}).get("branch") if task is not None else None)
            if branch != (expected_branch or constants.DELIVERY_BRANCH_PREFIX + row["task_id"]):
                raise util.KanpilotError("worktree_invalid", "执行工作树不在任务分支上")
        elif branch != "HEAD":
            raise util.KanpilotError("worktree_invalid", "阶段树不是登记的独立detached工作树")
        registered = _git(row["repository"], "worktree", "list", "--porcelain", "-z")
        if os.fsencode("worktree " + str(path)) not in registered.split(b"\0"):
            raise util.KanpilotError("worktree_invalid", "工作树不在Git登记中")
    return path


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_meta(conn, task_id):
    """Best-effort human-readable task summary and artifact mirror (protocol §5.29.6).

    Never part of a lease decision: a failed write must not block or fail the
    stage tree operation that triggered it.
    """
    try:
        rows = conn.execute("SELECT * FROM worktree_slots WHERE task_id=? AND task_id IS NOT NULL "
                            "ORDER BY stage", (task_id,)).fetchall()
        if not rows:
            return
        root = Path(rows[0]["worktrees_root"]) / task_id
        task = db.get_task(conn, task_id)
        extra = db.task_extra(task) if task is not None else {}
        delivery = extra.get("delivery") or {}
        stages = {}
        last_used = None
        for row in rows:
            stages[row["stage"]] = {"state": row["state"], "run_id": row["holder_id"],
                                    "sha": row["sha"], "updated_at": row["updated_at"]}
            if last_used is None or (row["updated_at"] or "") > last_used:
                last_used = row["updated_at"]
        meta = {
            "task_id": task_id,
            "title": task["title"] if task is not None else None,
            "project": task["project_name"] if task is not None else None,
            "repository": task["repository"] if task is not None else rows[0]["repository"],
            "branch": delivery.get("branch"),
            "created_at": task["created_at"] if task is not None else None,
            "last_used_at": last_used,
            "task_status": task["status"] if task is not None else None,
            "stages": stages,
            "retention_days": db.get_setting(conn, "worktree_retention_days",
                                             constants.DEFAULT_WORKTREE_RETENTION_DAYS),
        }
        target = root / "meta.json"
        _no_links(target)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(util.jdump(meta) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        if task is not None:
            _mirror_artifact(root, task, "plan", task["plan_path"], stages.get("plan"))
            _mirror_artifact(root, task, "review", task["review_report_path"], stages.get("review"))
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _mirror_artifact(root, task, stage, source, record):
    """Copy the published plan/report into artifacts/<stage>/<run_id>/ (non-authoritative)."""
    if not record or not isinstance(source, str) or not source:
        return
    origin = Path(source)
    if not origin.is_file() or origin.is_symlink():
        return
    run_id = record.get("run_id") or task["task_id"]
    folder = root / "artifacts" / stage / str(run_id)
    target = folder / ("plan.md" if stage == "plan" else "report.md")
    _no_links(folder)
    if target.is_file() and _sha256_file(target) == _sha256_file(origin):
        return
    folder.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(origin.read_bytes())
    os.replace(temporary, target)


def projection(conn):
    """Rebuild a disposable mapping from SQLite, with atomic replacement."""
    # Serialize publication with row changes, so a slower publisher cannot
    # overwrite a newer mapping after another connection commits.
    with db.write_txn(conn):
        return _projection(conn)


def _projection(conn):
    rows = [row for row in conn.execute(
        "SELECT * FROM worktree_slots ORDER BY task_id,stage").fetchall()
        if row["stage"] is not None]
    for row in rows:
        _stage_path(Path(row["worktrees_root"]), row["task_id"], row["stage"])
    roots = {row["worktrees_root"] for row in rows}
    root = next(iter(roots)) if len(roots) == 1 else str(constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None).resolve())
    payload = {"schema_version": 1, "updated_at": util.now_iso(), "worktrees_root": root,
               "slots": [{key: row[key] for key in ("slot_id", "task_id", "stage", "path", "repository",
                                                    "git_common_dir", "repo_key", "state")}
                         for row in rows]}
    target = constants.app_support() / "worktree-slots.json"
    _no_links(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def _receipt(conn, row, operation, callback):
    scope = "worktree-slot:" + row["slot_id"]
    operation_id = uuid.uuid4().hex
    payload = {"path": row["path"], "lease_token": row["lease_token"], "sha": row["sha"]}
    with db.write_txn(conn):
        runtime_operations.begin_operation(conn, scope, operation_id, "worktree_pool", operation, payload)
        runtime_operations.mark_operation_unknown(conn, scope, operation_id)
    try:
        result = callback()
    except Exception as exc:
        with db.write_txn(conn):
            runtime_operations.fail_operation(conn, scope, operation_id, result={"error": str(exc)})
        raise
    with db.write_txn(conn):
        runtime_operations.complete_operation(conn, scope, operation_id, result={"path": row["path"]})
    return result


def _owned(conn, slot_id, holder_id, lease_token):
    row = conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (slot_id,)).fetchone()
    if row is None or row["holder_id"] != holder_id or row["lease_token"] != lease_token:
        raise util.KanpilotError("conflict", "槽位租约身份或代次不匹配")
    return row


def _result(row):
    return dict(row)


def validate_lease(conn, slot_id, holder_id, lease_token):
    row = _owned(conn, slot_id, holder_id, lease_token)
    if row["state"] != "leased":
        raise util.KanpilotError("conflict", "槽位已不在租用状态")
    if row["reason"] == "release_in_progress":
        raise util.KanpilotError("conflict", "槽位正在释放")
    path = _validate(row, conn=conn)
    if _value(path, "rev-parse", "HEAD") != row["sha"]:
        raise util.KanpilotError("conflict", "槽位冻结提交已变化")
    return _result(row)


def quarantine(conn, slot_id, holder_id, lease_token, reason):
    with db.write_txn(conn):
        _owned(conn, slot_id, holder_id, lease_token)
        conn.execute("UPDATE worktree_slots SET state='quarantined',reason=?,updated_at=? WHERE slot_id=?",
                     (str(reason), util.now_iso(), slot_id))
    projection(conn)
    row = _owned(conn, slot_id, holder_id, lease_token)
    if row["task_id"]:
        write_meta(conn, row["task_id"])
    return _result(row)


def _lease_seconds(conn, requested=None):
    # A stale lease cannot expire inside any configured stage's maximum run.
    limits = [constants.DEFAULT_WORKTREE_POOL_LEASE_SECONDS]
    for key in ("pi_round_timeout_seconds", "codex_timeout_seconds", "executor_timeout_seconds"):
        value = db.get_setting(conn, key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            limits.append(value + 300)
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, (int, float)) or requested <= 0:
            raise util.bad_request("槽位租约时限必须为正数")
        limits.append(requested)
    return max(limits)


def _concurrency_limit(conn):
    value = db.get_setting(conn, "worktree_concurrency_limit",
                           constants.DEFAULT_WORKTREE_CONCURRENCY_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
        raise util.bad_request("worktree_concurrency_limit 必须为1–32整数")
    return value


def _leased_stage_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM worktree_slots WHERE state='leased' AND stage IN ('plan','review')"
    ).fetchone()[0]


def _create_stage_tree(conn, row, *, rebuilding):
    path = _stage_path(Path(row["worktrees_root"]), row["task_id"], row["stage"])

    def create():
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(row["repository"], "worktree", "prune")
        _git(row["repository"], "worktree", "add", "--detach", str(path), row["sha"])

    _receipt(conn, row, "worktree_rebuild" if rebuilding else "worktree_add", create)
    if rebuilding:
        db.add_event(conn, util.now_iso(), "worktree_stage_rebuilt", task_id=row["task_id"],
                     detail={"slot_id": row["slot_id"], "stage": row["stage"],
                             "path": str(path), "sha": row["sha"]})
    return path


def _prepare_stage_tree(conn, row, *, fresh):
    """Fresh create, missing plan/review rebuild, or clean checkout of the frozen sha."""
    stage = row["stage"]
    path = _stage_path(Path(row["worktrees_root"]), row["task_id"], stage)
    if fresh:
        if os.path.lexists(path):
            raise util.KanpilotError("worktree_invalid", "新阶段树路径已有未登记内容")
        _create_stage_tree(conn, row, rebuilding=False)
        _validate(row, conn=conn)
        return path
    if not path.is_dir():
        if stage == "run":
            raise util.KanpilotError(
                "worktree_missing",
                "执行工作树缺失，拒绝重建以保护未推送提交：" + str(path))
        _create_stage_tree(conn, row, rebuilding=True)
        _validate(row, conn=conn)
        return path
    if _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise util.KanpilotError("worktree_invalid", "空闲阶段树有未登记改动")
    _validate(row, conn=conn)
    if _value(path, "rev-parse", "HEAD") != row["sha"]:
        _receipt(conn, row, "worktree_checkout",
                 lambda: _git(path, "checkout", "--no-overwrite-ignore", "--detach", row["sha"]))
    return path


def _acquire(conn, repository, *, stage, holder_id, sha, task_id,
             worktrees_root=None, lease_seconds=None):
    if stage not in ("plan", "review") or not isinstance(holder_id, str) or not holder_id:
        raise util.bad_request("阶段树需要 plan/review 阶段与持有者身份")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise util.bad_request("阶段树需要合法 task_id")
    identity = repository_identity(repository)
    sha = _value(repository, "rev-parse", "--verify", str(sha) + "^{commit}")
    root = Path(worktrees_root or constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None)).expanduser().absolute()
    _no_links(root)
    root = root.resolve()
    path = _stage_path(root, task_id, stage)
    seconds, now = _lease_seconds(conn, lease_seconds), util.now_dt()
    with db.write_txn(conn):
        same = conn.execute(
            "SELECT * FROM worktree_slots WHERE task_id=? AND stage=? AND holder_id=? AND state='leased'",
            (task_id, stage, holder_id)).fetchone()
        if same is not None:
            if same["sha"] != sha:
                raise util.KanpilotError("conflict", "持有者已绑定另一冻结提交")
            # This path must not checkout or delete in-use session artifacts.
            return _result(same)
        row = conn.execute("SELECT * FROM worktree_slots WHERE task_id=? AND stage=?",
                           (task_id, stage)).fetchone()
        fresh = row is None
        if row is not None and row["state"] == "leased":
            # Same task/stage runs serially: the new generation waits for the
            # previous holder to release instead of failing or reusing its tree.
            return {"state": "waiting", "repo_key": identity["repo_key"],
                    "reason": "worktree_pool_full"}
        if row is not None and row["state"] == "quarantined":
            raise util.KanpilotError("conflict", "阶段树处于隔离状态，需先解除隔离")
        if _leased_stage_count(conn) >= _concurrency_limit(conn):
            return {"state": "waiting", "repo_key": identity["repo_key"],
                    "reason": "worktree_pool_full"}
        if fresh:
            slot_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO worktree_slots(slot_id,task_id,stage,repo_key,path,repository,"
                "git_common_dir,worktrees_root,state,updated_at) VALUES (?,?,?,?,?,?,?,?,'free',?)",
                (slot_id, task_id, stage, identity["repo_key"], str(path), identity["repository"],
                 identity["git_common_dir"], str(root), util.to_iso(now)))
        else:
            slot_id = row["slot_id"]
        token = uuid.uuid4().hex
        conn.execute("UPDATE worktree_slots SET state='leased',holder_stage=?,holder_id=?,lease_token=?,sha=?,lease_expires_at=?,heartbeat_at=?,updated_at=?,owner_pid=?,owner_state='not_started',baseline_json=NULL,reason=NULL,session_id=NULL,turn_id=NULL WHERE slot_id=?",
                     (stage, holder_id, token, sha, util.plus_seconds(seconds, now), util.to_iso(now), util.to_iso(now), os.getpid(), slot_id))
        row = _owned(conn, slot_id, holder_id, token)
    try:
        path = _prepare_stage_tree(conn, row, fresh=fresh)
        if _value(path, "rev-parse", "HEAD") != row["sha"]:
            raise util.KanpilotError("worktree_invalid", "阶段树检出提交不匹配")
    except Exception as exc:
        quarantine(conn, slot_id, holder_id, token, str(exc))
        raise
    projection(conn)
    write_meta(conn, task_id)
    return _result(_owned(conn, slot_id, holder_id, token))


def acquire(conn, repository, *, stage, holder_id, sha, task_id,
            worktrees_root=None, lease_seconds=None):
    reap_expired(conn)
    result = _acquire(conn, repository, stage=stage, holder_id=holder_id, sha=sha, task_id=task_id,
                      worktrees_root=worktrees_root, lease_seconds=lease_seconds)
    if result["state"] == "leased":
        try:
            validate_lease(conn, result["slot_id"], holder_id, result["lease_token"])
        except Exception as exc:
            quarantine(conn, result["slot_id"], holder_id, result["lease_token"], str(exc))
            raise
    return result


def heartbeat(conn, slot_id, holder_id, lease_token):
    now = util.now_dt()
    with db.write_txn(conn):
        row = _owned(conn, slot_id, holder_id, lease_token)
        if row["state"] != "leased" or row["reason"] == "release_in_progress":
            raise util.KanpilotError("conflict", "槽位已不在租用状态")
        conn.execute("UPDATE worktree_slots SET heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE slot_id=?",
                     (util.to_iso(now), util.plus_seconds(_lease_seconds(conn), now), util.to_iso(now), slot_id))
    return _result(_owned(conn, slot_id, holder_id, lease_token))


def mark_owner(conn, slot_id, holder_id, lease_token, *, pid=None, state="running", session_id=None, turn_id=None):
    if state not in ("running", "unknown", "stopped", "not_started"):
        raise util.bad_request("槽位持有者状态无效")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise util.bad_request("槽位持有进程无效")
    with db.write_txn(conn):
        row = _owned(conn, slot_id, holder_id, lease_token)
        if row["state"] != "leased" or row["reason"] == "release_in_progress":
            raise util.KanpilotError("conflict", "槽位已不在租用状态")
        conn.execute("UPDATE worktree_slots SET owner_pid=?,owner_state=?,session_id=?,turn_id=?,updated_at=? WHERE slot_id=?",
                     (pid if pid is not None else row["owner_pid"], state, session_id or row["session_id"], turn_id or row["turn_id"], util.now_iso(), slot_id))
    return _result(_owned(conn, slot_id, holder_id, lease_token))


def register_snapshot(conn, slot_id, holder_id, lease_token, snapshot):
    row = _owned(conn, slot_id, holder_id, lease_token)
    if row["state"] != "leased" or snapshot["head"] != row["sha"]:
        raise util.KanpilotError("conflict", "冻结输入不属于当前槽位提交")
    try:
        path = _validate(row, conn=conn)
        if row["baseline_json"]:
            baseline = _load_baseline(row)
            if review_snapshot.fingerprint(baseline) != review_snapshot.fingerprint(snapshot):
                raise util.KanpilotError("conflict", "槽位已登记另一冻结输入")
            review_snapshot.verify_root(path, baseline)
            return _result(row)
        materials = constants.app_support() / "worktree-slot-baselines" / slot_id / lease_token
        _no_links(materials)
        materials.mkdir(parents=True, exist_ok=False)
        files = review_snapshot.write_materials(materials, snapshot)
        baseline = {"head": snapshot["head"], "files": files, "fingerprint": review_snapshot.fingerprint(snapshot)}
        with db.write_txn(conn):
            _owned(conn, slot_id, holder_id, lease_token)
            conn.execute("UPDATE worktree_slots SET baseline_json=?,updated_at=? WHERE slot_id=?",
                         (util.jdump(baseline), util.now_iso(), slot_id))
        def populate():
            if _git(path, "status", "--porcelain"):
                review_snapshot.verify_root(path, snapshot)
            else:
                review_snapshot.populate(path, snapshot)
        _receipt(conn, row, "worktree_populate", populate)
        review_snapshot.verify_root(path, snapshot)
    except Exception as exc:
        quarantine(conn, slot_id, holder_id, lease_token, str(exc))
        raise
    return _result(_owned(conn, slot_id, holder_id, lease_token))


def _load_baseline(row):
    data = util.jload(row["baseline_json"])
    root = (constants.app_support() / "worktree-slot-baselines" / row["slot_id"] / row["lease_token"]).resolve()
    for filename in data["files"].values():
        path = Path(filename)
        _no_links(path)
        if not path.resolve().is_relative_to(root):
            raise util.KanpilotError("worktree_invalid", "冻结材料路径越界")
    snapshot = review_snapshot.from_materials(data["files"], data["head"])
    if review_snapshot.fingerprint(snapshot) != data["fingerprint"]:
        raise util.KanpilotError("worktree_invalid", "冻结基线已变化")
    return snapshot


def _artifacts(path, snapshot):
    """Inventory everything before removing anything; never follow links."""
    tracked = {os.fsdecode(name) for name in _git(path, "ls-files", "-z").split(b"\0") if name}
    tracked.update(os.fsdecode(name) for name in
                   _git(path, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0") if name)
    frozen = {entry["path"] for kind in ("tracked", "untracked")
              for entry in (snapshot or {}).get(kind, [])}
    protected = tracked | frozen
    def overlaps(name):
        return any(name == item or name.startswith(item + "/") or item.startswith(name + "/")
                   for item in protected)
    files, directories = [], []
    material = path / ".kaneopilot-plan-material"
    if os.path.lexists(material):
        pending = [material]
        while pending:
            item = pending.pop()
            mode = item.lstat().st_mode
            name = item.relative_to(path).as_posix()
            if stat.S_ISLNK(mode):
                raise util.KanpilotError("worktree_invalid", "系统材料包含符号链接")
            if stat.S_ISDIR(mode):
                if not overlaps(name):
                    directories.append(item)
                pending.extend(item.iterdir())
            elif stat.S_ISREG(mode):
                if not overlaps(name):
                    files.append(item)
            else:
                raise util.KanpilotError("worktree_invalid", "系统材料包含不安全类型")
    plus = path / ".pi"
    plan = plus / "plan"
    if os.path.lexists(plus) and plus.is_symlink():
        raise util.KanpilotError("worktree_invalid", ".pi不可为符号链接")
    if os.path.lexists(plan):
        if not stat.S_ISDIR(plan.lstat().st_mode):
            raise util.KanpilotError("worktree_invalid", ".pi/plan不可为链接或文件")
        for item in plan.iterdir():
            if item.suffix != ".md":
                continue
            name = item.relative_to(path).as_posix()
            if not stat.S_ISREG(item.lstat().st_mode):
                raise util.KanpilotError("worktree_invalid", "规划产物包含链接或特殊类型")
            if not overlaps(name):
                files.append(item)
    # Frozen files at product paths are inputs, not this round's artifacts.
    # Keep their parent directories until baseline verification/removal.
    return files, sorted(directories, key=lambda p: len(p.parts), reverse=True)


def _unresolved_operation(conn, row):
    return conn.execute(
        "SELECT 1 FROM runtime_operations WHERE status IN ('unknown','prepared') "
        "AND (scope_id IN (?,?) OR (? IS NOT NULL AND session_id=?)) LIMIT 1",
        ("worktree-slot:" + row["slot_id"], row["holder_id"], row["session_id"], row["session_id"]),
    ).fetchone() is not None


def consumer_returned(conn, row):
    """SQLite proof of relinquished directory access, never native exit proof."""
    operation = db.get_runtime_operation(conn, row["holder_id"], "desktop-return:" + row["lease_token"])
    if (operation is None or operation["status"] != "accepted"
            or operation["harness_id"] != "desktop_queue"
            or operation["tool"] != "worktree_consumer_return"):
        return False
    proof = util.jload(operation["result_json"])
    if not isinstance(proof, dict):
        return False
    expected = {"run_id": row["holder_id"], "slot_id": row["slot_id"],
                "holder_id": row["holder_id"], "lease_token": row["lease_token"],
                "backend": "codex_desktop", "contract": "no_more_directory_access"}
    if any(proof.get(key) != value for key, value in expected.items()):
        return False
    request = db.get_request(conn, proof.get("request_id"))
    if (request is None or request["run_id"] != row["holder_id"]
            or request["stage"] != row["holder_stage"]
            or request["status"] not in ("succeeded", "failed", "cancelled")):
        return False
    payload = db.request_payload(request)
    backend_key = "planner_backend" if request["stage"] == "plan" else "review_backend"
    frozen = payload.get("pool_lease") or {}
    return (payload.get(backend_key) == "codex_desktop"
            and all(frozen.get(key) == row[key] for key in
                    ("slot_id", "holder_id", "holder_stage", "lease_token", "path")))


def release(conn, slot_id, holder_id, lease_token, *, owner_stopped=False, only_not_started=False,
            expected_owner=None, expected_request=None):
    with db.write_txn(conn):
        row = _owned(conn, slot_id, holder_id, lease_token)
        if expected_owner is not None:
            util.require(all(row[key] == value for key, value in expected_owner.items()),
                         "槽位持有者状态已变化，未释放", "conflict")
        if expected_request is not None:
            current = db.get_request(conn, expected_request["request_id"])
            util.require(current is not None and all(current[key] == expected_request[key] for key in
                         ("task_id", "run_id", "stage", "status", "payload_json")),
                         "原请求身份或冻结输入已变化，未释放", "conflict")
        if row["state"] != "leased" or row["reason"] == "release_in_progress":
            return _result(row)
        if only_not_started and row["owner_state"] != "not_started":
            return _result(row)
        if _unresolved_operation(conn, row):
            conn.execute("UPDATE worktree_slots SET reason='operation_unconfirmed',updated_at=? WHERE slot_id=?",
                         (util.now_iso(), slot_id))
            return _result(_owned(conn, slot_id, holder_id, lease_token))
        returned = consumer_returned(conn, row)
        if not owner_stopped and not returned:
            conn.execute("UPDATE worktree_slots SET reason='owner_stop_unconfirmed',updated_at=? WHERE slot_id=?", (util.now_iso(), slot_id))
            return _result(_owned(conn, slot_id, holder_id, lease_token))
        conn.execute("UPDATE worktree_slots SET reason='release_in_progress',owner_state=?,updated_at=? WHERE slot_id=?",
                     ("stopped" if owner_stopped else "returned", util.now_iso(), slot_id))
    try:
        path = _validate(row, conn=conn)
        snapshot = _load_baseline(row) if row["baseline_json"] else None
        files, directories = _artifacts(path, snapshot)
        def restore():
            for item in files:
                _no_links(item)
                item.unlink()
            for item in directories:
                item.rmdir()
            if snapshot is not None:
                review_snapshot.verify_root(path, snapshot)
                # Normalize the already verified index from working bytes first:
                # raw CRLF/mode snapshots can intentionally differ from the patch.
                names = [entry["path"] for entry in snapshot["tracked"]]
                if names:
                    _git(path, "add", "--", *names)
                if snapshot["diff"]:
                    _git(path, "apply", "--reverse", "--index", "--binary", "--whitespace=nowarn", "-", content=snapshot["diff"])
                for entry in snapshot["untracked"]:
                    target = path / entry["path"]
                    _no_links(target)
                    target.unlink()
            if _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
                raise util.KanpilotError("worktree_invalid", "槽位有未知改动，保留现场")
        _receipt(conn, row, "worktree_release", restore)
    except Exception as exc:
        return quarantine(conn, slot_id, holder_id, lease_token, str(exc))
    with db.write_txn(conn):
        current = _owned(conn, slot_id, holder_id, lease_token)
        if current["state"] != "leased" or current["reason"] != "release_in_progress":
            raise util.KanpilotError("conflict", "释放期间槽位状态已变化")
        conn.execute("UPDATE worktree_slots SET state='free',reason=NULL,baseline_json=NULL,lease_expires_at=NULL,updated_at=? WHERE slot_id=? AND holder_id=? AND lease_token=? AND state='leased' AND reason='release_in_progress'",
                     (util.now_iso(), slot_id, holder_id, lease_token))
    projection(conn)
    row = _owned(conn, slot_id, holder_id, lease_token)
    if row["task_id"]:
        write_meta(conn, row["task_id"])
    return _result(row)


def _recover_returned(conn, row, *, release_slot):
    """Verify a trusted terminal run and persist artifacts under its run lock."""
    from . import desktop_queue, task_workspace
    util.require(consumer_returned(conn, row), "Desktop 同代交还证明缺失", "conflict")
    proof = util.jload(db.get_runtime_operation(
        conn, row["holder_id"], "desktop-return:" + row["lease_token"])["result_json"])
    request = db.get_request(conn, proof["request_id"])
    task = db.require_task(conn, request["task_id"])
    directory = desktop_queue._directory(conn, task["task_id"], request["run_id"])
    with desktop_queue._serialized(directory):
        job = desktop_queue._load(directory / "job.json")
        desktop_queue._require_terminal_job(request, task, job)
        util.require(job.get("pool_lease") == db.request_payload(request)["pool_lease"],
                     "Desktop 恢复检查点租约与冻结记录不符", "conflict")
        desktop_queue._require_persisted_output(conn, request, job)
        if release_slot:
            db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            task_workspace.release_pool(db_path, job, directory)
        else:
            task_workspace.preserve_pool_artifacts(job, directory)
        desktop_queue._write(directory / "job.json", job)


def _recover_stopped(conn, row, request):
    """Persist evidence from the same stopped, terminal CLI/Plus checkpoint."""
    from . import headless_adapter, task_workspace
    payload = db.request_payload(request)
    backend_key = "planner_backend" if row["holder_stage"] == "plan" else "review_backend"
    review_config = payload.get("review_config")
    review_config = review_config if isinstance(review_config, dict) else {}
    backend = payload.get(backend_key) or ("codex" if row["holder_stage"] == "plan" else
                                         review_config.get("backend") or "pi")
    util.require(backend != "codex_desktop", "Desktop 交还证明缺失", "conflict")
    frozen = payload.get("pool_lease")
    keys = ("slot_id", "holder_id", "holder_stage", "lease_token", "path")
    util.require(isinstance(frozen, dict) and all(frozen.get(key) == row[key] for key in keys),
                 "回收检查点缺少同代冻结租约", "conflict")
    if row["holder_stage"] == "plan":
        try:
            paths = [headless_adapter.resolve_source(conn).run_dir(request["task_id"], row["holder_id"]) / "job.json"]
        except headless_adapter.HeadlessError as exc:
            raise util.KanpilotError("conflict", exc.detail) from exc
    else:
        paths = (constants.app_support() / "executor-sessions" / "review").glob("*/job.json")
    matches = []
    for path in paths:
        try:
            _no_links(path)
            job = json.loads(path.read_bytes())
        except (OSError, ValueError, util.KanpilotError):
            continue
        if (isinstance(job, dict) and job.get("request_id") == request["request_id"]
                and job.get("task_id") == request["task_id"] and job.get("run_id") == row["holder_id"]
                and (row["holder_stage"] != "review" or
                     (job.get("stage") == "review" and job.get("invocation_id") == path.parent.name))):
            matches.append((path, job))
    util.require(len(matches) == 1, "回收检查点缺失或身份不唯一", "conflict")
    path, job = matches[0]
    lease = job.get("pool_lease")
    util.require(job.get("backend") == backend
                 and job.get("status") in ("finished", "failed", "cancelled")
                 and (job.get("owner_stopped") is True or job.get("send_state") == "not_sent")
                 and job.get("pid") == row["owner_pid"]
                 and isinstance(lease, dict) and all(lease.get(key) == row[key] for key in keys)
                 and all(not row[key] or job.get(key) == row[key] for key in ("session_id", "turn_id")),
                 "回收检查点终态、持有者或租约身份不符", "conflict")
    validate_lease(conn, row["slot_id"], row["holder_id"], row["lease_token"])
    task_workspace.preserve_pool_artifacts(job, path.parent)
    # Save relocated material paths before release can remove slot artifacts.
    path.write_text(util.jdump(job), encoding="utf-8")
    current = db.get_request(conn, request["request_id"])
    util.require(current is not None and current["run_id"] == row["holder_id"]
                 and current["stage"] == row["holder_stage"] and current["task_id"] == request["task_id"]
                 and current["status"] in ("succeeded", "failed", "cancelled")
                 and db.request_payload(current) == payload,
                 "产物保全期间请求身份或冻结输入已变化", "conflict")
    validate_lease(conn, row["slot_id"], row["holder_id"], row["lease_token"])
    result = release(conn, row["slot_id"], row["holder_id"], row["lease_token"], owner_stopped=True)
    job.update(pool_release_state=result["state"], pool_released=result["state"] == "free")
    path.write_text(util.jdump(job), encoding="utf-8")
    return result


def reap_expired(conn):
    """Expiry alone is never proof that a remote Plus turn has stopped."""
    results = []
    for row in conn.execute("SELECT * FROM worktree_slots WHERE state='leased'").fetchall():
        expiry = util.parse_ts(row["lease_expires_at"])
        if expiry is None or expiry >= util.now_dt():
            continue
        if consumer_returned(conn, row):
            # The receipt relinquishes access, but does not itself prove that
            # optional slot artifacts have reached persistent run storage.
            try:
                _recover_returned(conn, row, release_slot=True)
            except (OSError, ValueError, KeyError, TypeError, util.KanpilotError):
                with db.write_txn(conn):
                    conn.execute("UPDATE worktree_slots SET reason='return_checkpoint_unconfirmed',updated_at=? "
                                 "WHERE slot_id=? AND holder_id=? AND lease_token=? AND state='leased'",
                                 (util.now_iso(), row["slot_id"], row["holder_id"], row["lease_token"]))
            results.append(_result(_owned(conn, row["slot_id"], row["holder_id"], row["lease_token"])))
            continue
        request = conn.execute("SELECT * FROM requests WHERE run_id=? AND stage=? LIMIT 1",
                               (row["holder_id"], row["holder_stage"])).fetchone()
        stopped = False
        if row["owner_pid"]:
            try:
                os.kill(row["owner_pid"], 0)
            except ProcessLookupError:
                stopped = True
            except PermissionError:
                pass
        proven = (request is not None and request["status"] in ("succeeded", "failed", "cancelled")
                  and stopped and row["owner_state"] in ("stopped", "not_started"))
        if _unresolved_operation(conn, row):
            proven = False
        if proven:
            try:
                results.append(_recover_stopped(conn, row, request))
            except (OSError, ValueError, KeyError, TypeError, util.KanpilotError):
                with db.write_txn(conn):
                    conn.execute("UPDATE worktree_slots SET reason='owner_checkpoint_unconfirmed',updated_at=? "
                                 "WHERE slot_id=? AND holder_id=? AND lease_token=? AND state='leased'",
                                 (util.now_iso(), row["slot_id"], row["holder_id"], row["lease_token"]))
                results.append(_result(_owned(conn, row["slot_id"], row["holder_id"], row["lease_token"])))
            continue
        results.append(release(conn, row["slot_id"], row["holder_id"], row["lease_token"], owner_stopped=proven))
    return results


def unquarantine(conn, slot_id, holder_id, lease_token, *, confirm=False):
    if confirm is not True:
        raise util.bad_request("解除隔离需要confirm=true")
    reservation = "recovery_in_progress:" + str(os.getpid()) + ":" + uuid.uuid4().hex
    with db.write_txn(conn):
        row = _owned(conn, slot_id, holder_id, lease_token)
        if row["state"] != "quarantined":
            raise util.KanpilotError("conflict", "槽位不在隔离状态")
        if _unresolved_operation(conn, row):
            raise util.KanpilotError("conflict", "槽位Git操作尚未确认，保留隔离现场")
        if (row["reason"] or "").startswith("recovery_in_progress:"):
            try:
                os.kill(int(row["reason"].split(":")[1]), 0)
            except ProcessLookupError:
                pass
            except (ValueError, PermissionError):
                raise util.KanpilotError("conflict", "无法证明前次解除隔离操作已停止")
            else:
                raise util.KanpilotError("conflict", "槽位正在解除隔离")
        conn.execute("UPDATE worktree_slots SET reason=?,updated_at=? WHERE slot_id=?",
                     (reservation, util.now_iso(), slot_id))
    try:
        path = _validate(row, conn=conn)
        if _value(path, "rev-parse", "HEAD") != row["sha"]:
            raise util.KanpilotError("conflict", "槽位冻结提交已变化，保留隔离现场")
        returned = consumer_returned(conn, row)
        if row["owner_state"] not in ("stopped", "not_started") and not returned:
            raise util.KanpilotError("conflict", "槽位停止证明或干净状态不满足解除条件")
        if returned:
            _recover_returned(conn, row, release_slot=False)
        if _git(path, "status", "--porcelain"):
            raise util.KanpilotError("conflict", "槽位停止证明或干净状态不满足解除条件")
    except Exception as exc:
        # Only clear our own reservation; another generation is never touched.
        with db.write_txn(conn):
            conn.execute("UPDATE worktree_slots SET reason=?,updated_at=? WHERE slot_id=? AND holder_id=? AND lease_token=? AND state='quarantined' AND reason=?",
                         (str(exc), util.now_iso(), slot_id, holder_id, lease_token, reservation))
        raise
    with db.write_txn(conn):
        current = _owned(conn, slot_id, holder_id, lease_token)
        if current["state"] != "quarantined" or current["reason"] != reservation:
            raise util.KanpilotError("conflict", "解除隔离期间槽位身份或状态已变化")
        if (_unresolved_operation(conn, current)
                or current["owner_state"] not in ("stopped", "not_started") and not consumer_returned(conn, current)):
            raise util.KanpilotError("conflict", "解除隔离期间访问权或操作证明已变化")
        changed = conn.execute("UPDATE worktree_slots SET state='free',reason=NULL,baseline_json=NULL,updated_at=? WHERE slot_id=? AND holder_id=? AND lease_token=? AND state='quarantined' AND reason=?",
                               (util.now_iso(), slot_id, holder_id, lease_token, reservation)).rowcount
        if changed != 1:
            raise util.KanpilotError("conflict", "解除隔离期间槽位身份已变化")
    projection(conn)
    row = dict(conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (slot_id,)).fetchone())
    if row.get("task_id"):
        write_meta(conn, row["task_id"])
    return row


def cmd_worktree_slot_release(ctx, payload):
    return unquarantine(ctx.conn, payload["slot_id"], payload["holder_id"],
                        payload["lease_token"], confirm=payload.get("confirm") is True)
