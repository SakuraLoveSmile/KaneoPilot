"""Codex Desktop's bounded formal plan/Review pull MCP.

Requests remain the queue authority. The files below are immutable inputs and
recoverable settlement checkpoints, never a second queue or an executor.
"""
from __future__ import annotations

from . import plan_questions

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys

from . import (cmd_exec, codex_planner, constants, core, db, headless_adapter, pi_desktop_controller, planfile,
               pi_desktop_review, planner_inputs, planner_runner, review_settle, review_snapshot,
               util, task_workspace)

SETTING_KEY = "desktop_queue_config"
BACKEND = "codex_desktop"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"
MODEL_SOURCE = {"planner_model": "codex-desktop", "reasoning_effort": "inherit"}
_IDENTITY = ("request_id", "run_id", "claim_token")


def config(conn):
    value = db.get_setting(conn, SETTING_KEY, {})
    return dict({"enabled": False, "poll_minutes": 30, "auto_configured": False,
                 "planner_thread_id": None, "review_thread_id": None,
                 "automation_ids": []}, **(value if isinstance(value, dict) else {}))


def _validate(value, required=(), optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional):
        raise util.bad_request("Desktop 参数字段无效")
    if set(required) - set(value):
        raise util.bad_request("Desktop 参数缺失")


def _text(value, *, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise util.bad_request("Desktop 字符串参数无效")
    return value


def cmd_set_desktop_queue_config(ctx, payload):
    allowed = ("enabled", "poll_minutes", "auto_configured", "planner_thread_id",
               "review_thread_id", "automation_ids")
    _validate(payload, optional=allowed)
    for key in ("enabled", "auto_configured"):
        if key in payload and type(payload[key]) is not bool:
            raise util.bad_request("Desktop 开关必须为布尔值")
    if "poll_minutes" in payload and (type(payload["poll_minutes"]) is not int
                                      or not 1 <= payload["poll_minutes"] <= 1440):
        raise util.bad_request("Desktop 领取周期须为 1 到 1440 分钟")
    for key in ("planner_thread_id", "review_thread_id"):
        if key in payload and payload[key] is not None:
            _text(payload[key], limit=200)
    if "automation_ids" in payload:
        values = payload["automation_ids"]
        if not isinstance(values, list) or len(values) > 2:
            raise util.bad_request("Desktop 自动任务绑定无效")
        for value in values:
            _text(value, limit=200)
    with db.write_txn(ctx.conn):
        value = config(ctx.conn)
        value.update(payload)
        db.set_setting(ctx.conn, SETTING_KEY, value)
    return cmd_desktop_queue_status(ctx, {})


def cmd_desktop_queue_status(ctx, payload):
    _validate(payload)
    queue = {role: {"queued": 0, "claimed": 0, "lost": 0} for role in ("plan", "review")}
    recent = []
    rows = ctx.conn.execute("SELECT * FROM requests WHERE stage IN ('plan','review') "
                            "ORDER BY created_at DESC, request_id DESC").fetchall()
    for row in rows:
        if cmd_exec.request_backend(ctx.conn, row) != BACKEND:
            continue
        role, status = row["stage"], row["status"]
        if status in queue[role]:
            queue[role][status] += 1
        if len(recent) < 10:
            task = db.get_task(ctx.conn, row["task_id"])
            metadata = db.request_payload(row).get("desktop_claim") or {}
            recent.append({"request_id": row["request_id"], "task_id": row["task_id"],
                           "title": task["title"] if task else None, "role": role,
                           "status": status, "run_id": row["run_id"],
                           "thread_id": metadata.get("thread_id"),
                           "lease_expires_at": row["lease_expires_at"]})
    counts = {role: queue[role]["queued"] for role in queue}
    return {**config(ctx.conn), "queue": counts, "queue_details": queue,
            "recent_requests": recent,
            "detail": "Desktop 主动领取已启用" if config(ctx.conn)["enabled"] else "Desktop 主动领取已暂停"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _key(db_path):
    # Per-database key, atomically created even with two simultaneous consumers.
    path = Path(db_path).expanduser().resolve()
    root = path.parent / ".desktop-queue" / hashlib.sha256(str(path).encode()).hexdigest()[:24]
    for directory in (root.parent, root):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise util.KanpilotError("config_invalid", "Desktop 凭据目录不可用")
        os.chmod(directory, 0o700)
    key_path = root / "claim.key"
    if not key_path.exists():
        temporary = root / ("key-" + secrets.token_hex(16))
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(str(temporary), str(key_path))
            except FileExistsError:
                pass
        finally:
            temporary.unlink()
    descriptor = os.open(str(key_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise util.KanpilotError("config_invalid", "Desktop 凭据文件权限无效")
        value = stream.read(33)
    if len(value) != 32:
        raise util.KanpilotError("config_invalid", "Desktop 凭据文件无效")
    return value


def _token(db_path, role, operation_id):
    return hmac.new(_key(db_path), _canonical([role, operation_id]).encode(), hashlib.sha256).hexdigest()


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _directory(conn, task_id, run_id):
    return headless_adapter.resolve_source(conn).run_dir(task_id, run_id)


def _write(path, value):
    pi_desktop_controller.write_json(path, value)


def _load(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise util.KanpilotError("conflict", "Desktop 轮次检查点缺失或不可读")
    if not isinstance(value, dict):
        raise util.KanpilotError("conflict", "Desktop 轮次检查点无效")
    return value


@contextmanager
def _serialized(directory):
    # This core-owned advisory lock only serializes checkpoint side effects;
    # the request/lease in SQLite remains the authority after process death.
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".settle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise util.KanpilotError("busy", "该 Desktop 轮次正在准备或收尾")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _secured_command(ctx, command, arguments, identity, role=None, job=None):
    from . import cli
    expected_material_identity = None
    if job is not None:
        row, task = _authenticate(ctx, identity, role)
        _guard_materials(ctx, row, task, job)
        expected_material_identity = _material_db_identity(ctx.conn, row, task)
    previous = getattr(ctx.conn, "authorization_guard", None)
    def guard(conn):
        if previous:
            previous(conn)
        ctx.now = util.now_dt()
        ctx.now_iso = util.to_iso(ctx.now)
        row, task = _authenticate(ctx, identity, role, check_files=False)
        if job is not None:
            if _material_db_identity(conn, row, task) != expected_material_identity:
                raise util.KanpilotError("conflict", "Desktop 材料或工作区登记在校验期间已变化")
    ctx.conn.authorization_guard = guard
    try:
        ctx.now = util.now_dt()
        ctx.now_iso = util.to_iso(ctx.now)
        return cli.COMMANDS[command][1](ctx, cli.validate_payload(command, arguments))
    finally:
        ctx.conn.authorization_guard = previous


def _authenticate(ctx, arguments, role=None, *, terminal=False, check_files=True):
    for key in _IDENTITY:
        _text(arguments.get(key), limit=200)
    row = db.get_request(ctx.conn, arguments["request_id"])
    if row is None:
        raise util.KanpilotError("not_found", "Desktop 请求不存在")
    metadata = db.request_payload(row).get("desktop_claim") or {}
    if (row["stage"] not in ("plan", "review") or (role and row["stage"] != role)
            or cmd_exec.request_backend(ctx.conn, row) != BACKEND
            or metadata.get("role") != row["stage"]
            or metadata.get("run_id") != row["run_id"]
            or not isinstance(metadata.get("token_hash"), str)
            or not hmac.compare_digest(metadata["token_hash"], _token_hash(arguments["claim_token"]))):
        raise util.KanpilotError("forbidden", "Desktop 领取身份不符")
    if row["run_id"] != arguments["run_id"]:
        raise util.KanpilotError("stale_run", "Desktop 请求轮次已变化")
    task = db.require_task(ctx.conn, row["task_id"])
    if terminal and row["status"] in ("succeeded", "failed", "cancelled"):
        return row, task
    if row["status"] != "claimed" or util.lease_lost(row["lease_expires_at"], ctx.now):
        raise util.KanpilotError("stale_run", "Desktop 请求非在途或租约已过期；交人工恢复")
    if not db.task_is_active(task) or db.task_is_purged(task):
        raise util.KanpilotError("conflict", "Desktop 任务已归档或移除")
    payload = db.request_payload(row)
    if row["stage"] == "review":
        if (not payload.get("expected_execution_run_id")
                or task["run_id"] != payload["expected_execution_run_id"]
                or not payload.get("plan_sha256") or task["plan_sha256"] != payload["plan_sha256"]):
            raise util.KanpilotError("stale_run", "Desktop Review 执行或计划身份已变化")
        plan = Path(task["plan_path"] or "")
        if check_files and (not plan.is_file() or util.sha256_file(plan) != payload["plan_sha256"]):
            raise util.KanpilotError("conflict", "Desktop Review 计划文件已变化")
    return row, task


def _material_db_identity(conn, row, task):
    """Repeat immutable registration checks under SQL lock, without external IO."""
    payload = db.request_payload(row)
    extra = db.task_extra(task)
    record = extra.get("review_remediation") or {}
    old_round = None
    if record.get("execution_run_id"):
        old_round = conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
                                 (task["task_id"], record["execution_run_id"])).fetchone()
    return {"task": {key: task[key] for key in ("task_id", "repository", "project_id", "source_sha256", "requirements_sha256", "plan_sha256", "run_id")},
            "payload": payload, "delivery": core.delivery_of(task), "remediation": record,
            "round": dict(old_round) if old_round is not None else None}


def _guard_materials(ctx, row, task, job):
    if job.get("pool_lease"):
        task_workspace.validate_pool(ctx.conn, job["pool_lease"])
    payload = db.request_payload(row)
    frozen = job.get("frozen") or {}
    for key in ("source_sha256", "requirements_sha256", "repository"):
        if frozen.get(key) != task[key]:
            raise util.KanpilotError("conflict", "Desktop 本轮任务来源或仓库已变化")
    if row["stage"] == "plan":
        task_workspace.verify_plan_workspace(task, payload)
        from . import review_resolution
        review_resolution.verify_plan_cycle(ctx.conn, task, payload)
        context = job["context"]
        manifest = context.get("payload", {}).get("manifest") or {}
        if payload.get("planner_input_sha256") != job.get("planner_input_sha256"):
            raise util.KanpilotError("conflict", "Desktop 规划材料身份已变化")
        if planner_inputs._manifest_sha(manifest) != payload.get("planner_input_sha256"):
            raise util.KanpilotError("conflict", "Desktop 规划材料清单指纹不符")
        root = Path(context.get("input_dir") or "")
        saved_manifest = _load(root / "manifest.json")
        if planner_inputs._manifest_sha(saved_manifest) != payload.get("planner_input_sha256"):
            raise util.KanpilotError("conflict", "Desktop 规划清单文件已变化")
        for entry in manifest.get("files") or []:
            target = util.inside(root / entry["path"], root, "Desktop 规划材料")
            if not target.is_file() or util.sha256_file(target) != entry.get("sha256"):
                raise util.KanpilotError("conflict", "Desktop 冻结规划材料已变化")
        planner_runner._verify_partial_materials(context)
    else:
        core.require_committed_review(task)
        core.require_review_identity(ctx.conn, {"task_id": task["task_id"], "request_id": row["request_id"],
                                               "run_id": row["run_id"],
                                               "expected_execution_run_id": job.get("execution_run_id")}, task, ctx.now)
        if (task["run_id"] != job.get("execution_run_id")
                or task["plan_sha256"] != frozen.get("plan_sha256")):
            raise util.KanpilotError("stale_run", "Desktop Review 周期已变化")
        for path, digest in (job.get("material_hashes") or {}).items():
            source = Path(path)
            if not source.is_file() or util.sha256_file(source) != digest:
                raise util.KanpilotError("conflict", "Desktop Review 冻结材料已变化")
        if not Path(job.get("review_root") or "").is_dir():
            raise util.KanpilotError("conflict", "Desktop Review 独立快照不可用")
        execution = ctx.conn.execute("SELECT worktree_path FROM execution_rounds WHERE task_id=? AND run_id=?",
                                     (task["task_id"], job["execution_run_id"])).fetchone()
        if execution is None or execution["worktree_path"] != job.get("execution_worktree"):
            raise util.KanpilotError("stale_run", "Desktop Review 执行工作树身份已变化")
        if _execution_fingerprint(Path(job["execution_worktree"])) != job.get("execution_fingerprint"):
            raise util.KanpilotError("conflict", "Desktop Review 执行工作树内容已变化")
        if _execution_fingerprint(Path(job["review_root"])) != job.get("review_fingerprint"):
            raise util.KanpilotError("conflict", "Desktop Review 独立快照内容已变化")
        if job.get("manual_snapshot_head"):
            snapshot = review_snapshot.from_materials(job["materials"], job["manual_snapshot_head"])
            review_snapshot.verify_root(Path(job["review_root"]), snapshot)


def _execution_fingerprint(path):
    return review_snapshot.fingerprint(review_snapshot.freeze(path))



def _record(ctx, job, directory):
    status = job.get("status") or "running"
    with db.write_txn(ctx.conn):
        ctx.conn.execute("UPDATE execution_rounds SET backend=?, run_status=?, "
                         "session_id=COALESCE(?, session_id), pid=?, "
                         "started_at=COALESCE(started_at, ?), updated_at=? "
                         "WHERE task_id=? AND run_id=?",
                         (BACKEND, status, job.get("session_id"), job.get("pid"),
                          job["started_at"], util.to_iso(ctx.now), job["task_id"], job["run_id"]))
    _write(directory / "job.json", job)


def _prepare(ctx, claim, metadata):
    context = claim["context"]
    role = metadata["role"]
    task = db.require_task(ctx.conn, context["task_id"])
    directory = _directory(ctx.conn, task["task_id"], claim["run_id"])
    with _serialized(directory):
        saved = directory / "job.json"
        if saved.is_file():
            job = _load(saved)
            if job.get("request_id") != claim["request_id"] or job.get("run_id") != claim["run_id"]:
                raise util.KanpilotError("conflict", "Desktop 轮次检查点身份不符")
            return job
        job = {"backend": BACKEND, "role": role, "task_id": task["task_id"],
               "request_id": claim["request_id"], "run_id": claim["run_id"],
               "status": "running", "started_at": util.to_iso(ctx.now),
               "session_id": metadata.get("thread_id"), "session_title": "Desktop " + role,
               "pid": metadata["pid"], "ppid": metadata["ppid"],
               "config": MODEL_SOURCE, "context": context, "checkpoints": {},
               "invocation_dir": str(directory),
               "frozen": {key: task[key] for key in ("source_sha256", "requirements_sha256",
                                                      "repository", "plan_sha256")}}
        if role == "plan":
            from . import review_resolution
            review_resolution.verify_plan_cycle(ctx.conn, task, context["payload"])
            workspace = task_workspace.prepare_plan_pool(ctx.conn, ctx.db_path, task, context["payload"],
                                                         job, directory / "job.json")
            if workspace is not None:
                job["plan_cwd"] = str(workspace)
            job["planner_input_sha256"] = db.request_payload(db.get_request(ctx.conn, claim["request_id"])).get("planner_input_sha256")
            manifest = context.get("payload", {}).get("manifest") or {}
            job["instruction"] = codex_planner.build_prompt(
                dict(context["task"], fix_mode=context["payload"].get("fix_mode"),
                     plan_cwd=job.get("plan_cwd")), manifest,
                Path(context["input_dir"]) if context.get("input_dir") else None,
                method=context["payload"].get("planner_method") or "builtin")
            _guard_materials(ctx, db.get_request(ctx.conn, claim["request_id"]), task, job)
        else:
            facts = pi_desktop_review._facts(ctx.db_path, task["task_id"], claim["request_id"])
            problem = pi_desktop_review._identity_problem(facts, job)
            if problem:
                raise util.KanpilotError("review_blocked", problem)
            summary = facts["task"]
            job["execution_run_id"] = task["run_id"]
            execution = Path(summary["execution_round"]["worktree_path"])
            delivery = summary.get("delivery") or (summary.get("extra") or {}).get("delivery") or {}
            branch_review = core.requires_branch_review(summary)
            snapshot = review_snapshot.freeze(execution) if not branch_review else None
            frozen_execution = review_snapshot.fingerprint(snapshot) if snapshot is not None else _execution_fingerprint(execution)
            review_root = pi_desktop_review.prepare_pooled_worktree(
                ctx.conn, ctx.db_path, job, summary, execution, snapshot)
            materials = pi_desktop_review.collect_materials(
                db_path=ctx.db_path, conn=ctx.conn, materials=directory / "materials",
                task=summary, execution_worktree=execution, execution_run_id=task["run_id"], snapshot=snapshot)
            if "execution-evidence.note" in materials["files"]:
                raise util.KanpilotError("review_blocked", "本周期执行证据缺失，不能开始 Desktop Review")
            raw_hashes = {}
            for name in ("tracked-manifest.json", "untracked-manifest.json"):
                manifest_path = materials["files"].get(name)
                if manifest_path:
                    raw_hashes.update({entry["material_path"]: entry["sha256"]
                                       for entry in (_load(Path(manifest_path)).get("files") or [])})
            if _execution_fingerprint(execution) != frozen_execution:
                raise util.KanpilotError("review_blocked", "Desktop Review 材料准备期间执行工作树已变化")
            if snapshot is not None:
                review_snapshot.verify_root(review_root, snapshot)
            job.update(review_root=str(review_root), execution_worktree=str(execution),
                       execution_fingerprint=frozen_execution,
                       review_fingerprint=_execution_fingerprint(review_root),
                       materials=materials["files"],
                       material_hashes={**raw_hashes, **{path: util.sha256_file(Path(path)) for path in materials["files"].values()}})
            if branch_review:
                job["review_branch_snapshot"] = core.branch_review_snapshot(task)
            if snapshot is not None:
                job["manual_snapshot_head"] = snapshot["head"]
            job["instruction"] = pi_desktop_review.review_instruction(
                "只读审查；不运行修复、构建、测试或远端写操作。", job=job,
                review_root=review_root, materials=materials, facts=facts)
        _record(ctx, job, directory)
        pi_desktop_controller.EventLog(directory / "events.jsonl").emit(
            "session", util.to_iso(ctx.now), session_id=metadata.get("thread_id"),
            session_title=job["session_title"], backend=BACKEND)
        return job


def _claim(ctx, payload):
    _validate(payload, ("role", "operation_id"), ("thread_id",))
    role = payload["role"]
    if role not in ("plan", "review"):
        raise util.bad_request("Desktop 仅领取规划或 Review")
    operation_id = _text(payload["operation_id"], limit=200)
    if "thread_id" in payload:
        _text(payload["thread_id"], limit=200)
    token = _token(ctx.db_path, role, operation_id)
    metadata = {"operation_id": operation_id, "token_hash": _token_hash(token),
                "pid": os.getpid(), "ppid": os.getppid(), **({"thread_id": payload["thread_id"]} if "thread_id" in payload else {})}
    # Receipt replay precedes enable, so disabling stops new claims but does
    # not strand an already claimed request after a lost response.
    cached = cmd_exec.desktop_claim_receipt(ctx.conn, role, metadata)
    if cached is None and not config(ctx.conn)["enabled"]:
        return {"claimed": False, "reason": "disabled"}
    claim = cmd_exec.cmd_claim(ctx, {"stage": role, "executor": "codex-desktop-" + role}, desktop_claim=metadata)
    if not claim.get("claimed"):
        return claim
    identity = {"request_id": claim["request_id"], "run_id": claim["run_id"], "claim_token": token}
    row, task = _authenticate(ctx, identity, role, terminal=True)
    if row["status"] != "claimed":
        return {"claimed": True, **identity, "status": row["status"], "completed": True}
    _authenticate(ctx, identity, role)
    bound = db.request_payload(row)["desktop_claim"]
    try:
        job = _prepare(ctx, claim, bound)
        ctx.now = util.now_dt()
        ctx.now_iso = util.to_iso(ctx.now)
        row, task = _authenticate(ctx, identity, role)
        _guard_materials(ctx, row, task, job)
    except util.KanpilotError as exc:
        if exc.code == "worktree_pool_waiting":
            return {"claimed": False, "reason": exc.code, "request_id": claim["request_id"]}
        if exc.code == "busy":
            raise
        # This valid claim is now blocked, never returned as ready to review.
        try:
            _secured_command(ctx, "report", {"request_id": row["request_id"], "run_id": row["run_id"],
                                      "outcome": "needs_user", "note": "Desktop 材料准备受阻：" + exc.message,
                                      "failure_code": "review_blocked" if role == "review" else "planner_input_invalid"}, identity, role)
        finally:
            # Request ownership may already be lost. Only this never-delivered,
            # not-started slot can be returned without reporting the new owner.
            directory = _directory(ctx.conn, task["task_id"], identity["run_id"])
            saved = directory / "job.json"
            if saved.is_file():
                prepared = _load(saved)
                if (prepared.get("request_id") == identity["request_id"]
                        and prepared.get("run_id") == identity["run_id"]
                        and prepared.get("send_state") == "not_sent"):
                    task_workspace.release_pool(ctx.db_path, prepared, directory, unexposed=True)
                    _record(ctx, prepared, directory)
        raise
    directory = _directory(ctx.conn, task["task_id"], row["run_id"])
    if job.get("pool_lease"):
        # The stdio caller is short lived; external consumer termination is unknown.
        job["send_state"] = "unknown"
        task_workspace.pool_owner(ctx.conn, job, state="unknown")
        _record(ctx, job, directory)
    # Context is a bounded material summary. Full frozen input stays in the
    # trusted core directory; a model cannot provide a replacement path.
    return {"claimed": True, **identity, "context": {
        "task_id": task["task_id"], "title": task["title"], "role": role,
        "repository": task["repository"], "planner_method": claim["context"].get("payload", {}).get("planner_method"),
        "input_dir": claim["context"].get("input_dir"), "review_root": job.get("review_root"),
        "plan_cwd": job.get("plan_cwd"),
        "materials": job.get("materials", {}), "job_path": str(directory / "job.json")},
        "instructions": _consumer_instruction(role, job, directory),
        "heartbeat_seconds": min(claim["context"]["lease_seconds"] / 3.0, 60),
        "lease_expires_at": row["lease_expires_at"]}


def _consumer_instruction(role, job, directory):
    method = job.get("context", {}).get("payload", {}).get("planner_method") or "builtin"
    skill = ("必须真实调用 $spec-planner:spec-plan 施工规划技能。" if role == "plan" and method == "spec_plan" else "")
    return ("这是正式 KaneoPilot " + role + " 请求。" + skill +
            "读取 " + str(directory / "job.json") + " 中的 instruction 与冻结材料。"
            "只读仓库；不执行业务修改、构建、测试、外部发布或直接写 Kaneo。"
            "每 heartbeat_seconds 秒显式调用 kp_desktop_heartbeat；不得由 MCP 存活代替续租。"
            "完成后仅调用 kp_desktop_submit_" + role + " 提交结果正文，由核心生成文件并收尾。"
            "不要在结果、进度、文件或日志中写领取凭据。"
            "thread_id 仅在确知当前会话身份时填写，不得猜测；模型和权限继承当前 Desktop 原生会话。")


def _heartbeat(ctx, payload):
    _validate(payload, _IDENTITY, ("progress",))
    if "progress" in payload:
        _text(payload["progress"], limit=2000)
    _reject_token_text(payload, payload.get("progress", ""))
    row, task = _authenticate(ctx, payload)
    directory = _directory(ctx.conn, task["task_id"], row["run_id"])
    job = _load(directory / "job.json")
    if job.get("pool_lease"):
        from . import worktree_pool
        worktree_pool.heartbeat(ctx.conn, **task_workspace.pool_identity(job["pool_lease"]))
    return _secured_command(ctx, "heartbeat", {key: value for key, value in payload.items() if key != "claim_token"}, payload)


def _reject_token_text(identity, text):
    if identity["claim_token"] in text:
        raise util.bad_request("Desktop 正文不得包含领取凭据")


def _plan_result(value):
    _validate(value, ("status",), ("plan_markdown", "questions", "evidence"))
    if value["status"] not in ("ready", "needs_input"):
        raise util.bad_request("Desktop 规划 status 无效")
    result = {"status": value["status"], "plan_markdown": value.get("plan_markdown", ""),
              "questions": value.get("questions", []), "evidence": value.get("evidence", [])}
    if not isinstance(result["plan_markdown"], str) or any(not isinstance(result[key], list)
             or any(not isinstance(item, str) for item in result[key]) for key in ("evidence",)):
        raise util.bad_request("Desktop 规划结果类型不符 Codex 输出协议")
    result["questions"] = plan_questions.normalize(result["questions"])
    if result["status"] == "ready" and (not result["plan_markdown"].strip() or result["questions"]):
        raise util.KanpilotError("validation_failed", "ready 规划须有正文且没有未决问题")
    return result


def _submit(ctx, payload, role):
    _validate(payload, (*_IDENTITY, "result"), ("thread_id",))
    if "thread_id" in payload:
        _text(payload["thread_id"], limit=200)
    result = _plan_result(payload["result"]) if role == "plan" else review_settle.validate_result(payload["result"])
    encoded = _canonical(result)
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise util.bad_request("Desktop 结果正文过大")
    _reject_token_text(payload, encoded)
    row, task = _authenticate(ctx, payload, role, terminal=True)
    directory = _directory(ctx.conn, task["task_id"], row["run_id"])
    with _serialized(directory):
        job = _load(directory / "job.json")
        if job.get("request_id") != row["request_id"] or job.get("run_id") != row["run_id"]:
            raise util.KanpilotError("conflict", "Desktop 结果检查点身份不符")
        digest = _digest(result)
        if job.get("result_digest") and job["result_digest"] != digest:
            raise util.KanpilotError("conflict", "Desktop 本轮已提交不同结果")
        if row["status"] in ("succeeded", "failed", "cancelled"):
            if job.get("result_digest") != digest or not job.get("submit_intent"):
                raise util.KanpilotError("conflict", "Desktop 请求已结束，不能写新结果")
            return _public_result(row, task, job, deduplicated=True)
        _authenticate(ctx, payload, role)
        _guard_materials(ctx, row, task, job)
        if not job.get("result_digest"):
            job.update(result_digest=digest, submit_intent=role,
                       **({"output_status": result["status"], **{key: value for key, value in result.items() if key != "status"}}
                          if role == "plan" else result))
            if payload.get("thread_id"):
                job["session_id"] = payload["thread_id"]
            _record(ctx, job, directory)
        if role == "plan":
            _settle_plan(ctx, payload, row, task, job, directory)
        else:
            driver = _ReviewDriver(ctx, payload, job)
            review_settle.settle_job(driver=driver, db_path=ctx.db_path, job=job,
                                    job_path=directory / "job.json",
                                    log=pi_desktop_controller.EventLog(directory / "events.jsonl"),
                                    record_run=lambda **_kwargs: None)
            _record(ctx, job, directory)
        fresh = db.get_request(ctx.conn, row["request_id"])
        return _public_result(fresh, db.require_task(ctx.conn, task["task_id"]), job)


def _public_result(row, task, job, deduplicated=False):
    return {"ok": row["status"] in ("succeeded", "failed"), "deduplicated": deduplicated,
            "request_id": row["request_id"], "run_id": row["run_id"],
            "request_status": row["status"], "task_status": task["status"],
            "plan_path": job.get("plan_path"), "report_path": job.get("report_path"),
            "detail": job.get("detail"), "conclusion": job.get("conclusion")}


def _settle_plan(ctx, identity, row, task, job, directory):
    def guard():
        request, current = _authenticate(ctx, identity, "plan")
        _guard_materials(ctx, request, current, job)
    guard()
    if job["output_status"] == "needs_input":
        draft = directory / "plan-draft.md"
        if job["plan_markdown"].strip() and not draft.exists():
            draft.write_text(job["plan_markdown"], encoding="utf-8")
        guard()
        _secured_command(ctx, "report", {"request_id": row["request_id"], "run_id": row["run_id"],
                                  "outcome": "needs_user", "note": "规划需要补充材料：" + plan_questions.summary(job["questions"]),
                                  "failure_code": "reported", "evidence": {"questions": job["questions"], "plan_markdown": job["plan_markdown"], "draft_path": str(draft), "evidence": job["evidence"]}}, identity, "plan", job)
    else:
        manifest = job["context"].get("payload", {}).get("manifest") or {}
        if not job.get("plan_path"):
            job["generated_at"] = util.to_iso(ctx.now)
            metadata = planner_runner.build_plan_meta(job["context"], manifest, job["generated_at"],
                                                       task_row=task, model_config=MODEL_SOURCE)
            title = (job["context"].get("task") or {}).get("title") or task["task_id"]
            job["plan_path"] = str(planner_runner._plan_file_path(metadata.get("project_id"), title))
            _write(directory / "job.json", job)
        target = Path(job["plan_path"])
        if not job.get("plan_sha256"):
            if target.exists():
                metadata = planner_runner.build_plan_meta(job["context"], manifest, job["generated_at"],
                                                           task_row=task, model_config=MODEL_SOURCE)
                saved, raw, _ = planfile.load_plan(str(target), task["task_id"])
                if (any(str(saved.get(key)) != str(value) for key, value in metadata.items())
                        or planner_runner._strip_model_frontmatter(raw.decode("utf-8")).lstrip("\n")
                        != planner_runner._strip_model_frontmatter(job["plan_markdown"]).lstrip("\n")):
                    raise util.KanpilotError("conflict", "Desktop 固定计划输出与已提交结果不符")
            else:
                planner_runner.write_plan_file(job["context"], manifest, job["plan_markdown"],
                                                job["generated_at"], task_row=task,
                                                model_config=MODEL_SOURCE, target_path=target)
            job["plan_sha256"] = util.sha256_file(target)
            _write(directory / "job.json", job)
        if not target.is_file() or util.sha256_file(target) != job["plan_sha256"]:
            raise util.KanpilotError("conflict", "Desktop 固定计划输出已变化")
        guard()
        _secured_command(ctx, "publish-plan", {
            "task_id": task["task_id"], "request_id": row["request_id"], "run_id": row["run_id"],
            "plan_path": str(target), "plan_kind": task["plan_kind"] or "standard",
            "source_sha256": manifest.get("source_sha256") or task["source_sha256"] or "",
            "requirements_sha256": manifest.get("requirements_sha256") or task["requirements_sha256"] or "",
            "planner_input_sha256": job["planner_input_sha256"], "generated_at": job["generated_at"],
            **MODEL_SOURCE}, identity, "plan", job)
    job.update(status="finished", exit_code=0, ended_at=util.to_iso(ctx.now))
    _record(ctx, job, directory)
    pi_desktop_controller.EventLog(directory / "events.jsonl").emit("terminal", util.to_iso(ctx.now), outcome="completed", reason="desktop_plan_settled")


class _ReviewDriver:
    """Existing Review recovery semantics with credential and material guards."""
    _identity_payload = staticmethod(pi_desktop_review._identity_payload)
    _facts = staticmethod(pi_desktop_review._facts)
    _lease_valid = staticmethod(pi_desktop_review._lease_valid)
    _review_summary = staticmethod(pi_desktop_review._review_summary)

    def __init__(self, ctx, identity, job):
        self.ctx, self.identity, self.job = ctx, identity, job

    def guard(self):
        # Refresh time on each independent stage rather than reusing arrival
        # time during a slow remote round trip.
        self.ctx.now = util.now_dt()
        self.ctx.now_iso = util.to_iso(self.ctx.now)
        row, task = _authenticate(self.ctx, self.identity, "review")
        _guard_materials(self.ctx, row, task, self.job)

    def core_call(self, db_path, command, payload):
        self.guard()
        directory = _directory(self.ctx.conn, self.job["task_id"], self.job["run_id"])
        remote_operation = command in ("create-review-fix", "advance-task")
        operations = self.job.setdefault("remote_operations", {})
        signature = _digest(payload)
        if remote_operation and command in operations:
            previous = operations[command]
            if previous.get("signature") != signature:
                raise util.KanpilotError("conflict", "Desktop Review 远端阶段输入已变化")
            if previous.get("phase") == "done":
                return previous["result"]
            recovered = self._recover_operation(command, payload)
            if recovered is None:
                raise util.KanpilotError("remote_outcome_unknown", "Desktop Review 上次远端阶段结果未知；不重放写入，交人工核对")
            previous.update(phase="done", result=recovered)
            _write(directory / "job.json", self.job)
            return recovered
        if remote_operation:
            operations[command] = {"phase": "in_flight", "signature": signature}
        # Persist report path/hash, registration and remote intent before a
        # command can commit and lose its response.
        _write(directory / "job.json", self.job)
        try:
            value = _secured_command(self.ctx, command, payload, self.identity, "review", self.job)
        except util.KanpilotError:
            value = self._recover_operation(command, payload)
            if value is None:
                raise
        if remote_operation:
            operations[command].update(phase="done", result=value)
        _write(directory / "job.json", self.job)
        return value

    def _recover_operation(self, command, payload):
        if command == "create-review-fix":
            row = self.ctx.conn.execute("SELECT * FROM tasks WHERE plan_kind='review_fix' "
                                         "AND review_key=? AND review_source_task_id=?",
                                         (payload["dedupe_key"], self.job["task_id"])).fetchone()
            if row is not None and db.task_is_active(row):
                return {"ok": True, "fix_task_id": row["task_id"], "source_sha256": row["source_sha256"], "created": False}
        elif command == "advance-task":
            task = self._facts(self.ctx.db_path, self.job["task_id"], self.job["request_id"]).get("task") or {}
            if task.get("status") == "done" and self._review_summary(task).get("conclusion") == "pass":
                return {"ok": True, "deduplicated": True, "task_status": "done"}
        elif command == "register-review":
            task = self._facts(self.ctx.db_path, self.job["task_id"], self.job["request_id"]).get("task") or {}
            if review_settle._registered_matches(task, self.job):
                return {"ok": True, "already_registered": True}
        return None

    def _report(self, db_path, job, *, outcome, note, failure_code=None):
        self.guard()
        payload = {"request_id": job["request_id"], "run_id": job["run_id"], "outcome": outcome, "note": note}
        if failure_code:
            payload["failure_code"] = failure_code
        try:
            _secured_command(self.ctx, "report", payload, self.identity, "review", self.job)
        except util.KanpilotError as exc:
            row = db.get_request(self.ctx.conn, job["request_id"])
            if row is None or row["status"] not in ("succeeded", "failed") or (util.jload(row["result_json"]) or {}).get("outcome") != outcome:
                return False, exc.code + ": " + exc.message
        job.setdefault("checkpoints", {})["reported"] = True
        return True, None

    def _close(self, **kwargs):
        return pi_desktop_review._close(**kwargs)

    def _stop_no_write(self, **kwargs):
        self._close(**kwargs, status="failed", exit_code=1)
        return 1

    def _blocked_no_pass(self, **kwargs):
        self._report(kwargs["db_path"], kwargs["job"], outcome="needs_user", note=kwargs["detail"], failure_code="review_blocked")
        clean = {key: value for key, value in kwargs.items() if key != "db_path"}
        return self._stop_no_write(**clean)

    def _registration_failed(self, **kwargs):
        exc = kwargs.pop("exc")
        label = kwargs.pop("label")
        kwargs["detail"] = label + " 失败：" + exc.code + ": " + exc.message
        if exc.code in pi_desktop_review._IDENTITY_BLOCK_CODES:
            kwargs.pop("db_path", None)
            return self._stop_no_write(**kwargs)
        return self._blocked_no_pass(**kwargs)


def _fail(ctx, payload):
    _validate(payload, (*_IDENTITY, "reason"))
    reason = _text(payload["reason"], limit=4000)
    _reject_token_text(payload, reason)
    row, task = _authenticate(ctx, payload)
    directory = _directory(ctx.conn, task["task_id"], row["run_id"])
    with _serialized(directory):
        _authenticate(ctx, payload)
        response = _secured_command(ctx, "report", {"request_id": row["request_id"], "run_id": row["run_id"],
                                             "outcome": "failed", "note": reason,
                                             "failure_code": "planner_failed" if row["stage"] == "plan" else "review_blocked"}, payload)
        if (directory / "job.json").is_file():
            job = _load(directory / "job.json")
            job.update(status="failed", exit_code=1, ended_at=util.to_iso(ctx.now), detail=reason)
            _record(ctx, job, directory)
        return response


def _schema(properties, required=()):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required)}


_STRING = {"type": "string", "minLength": 1}
_IDENTITY_PROPERTIES = {key: _STRING for key in _IDENTITY}
_PLAN_SCHEMA = _schema({"status": {"type": "string", "enum": ["ready", "needs_input"]},
                        "plan_markdown": {"type": "string"}, "questions": {"type": "array", "items": plan_questions.QUESTION_SCHEMA},
                        "evidence": {"type": "array", "items": {"type": "string"}}}, ("status",))
_TOOLS = [
    {"name": "kp_desktop_status", "description": "只读查询正式 Desktop 规划及 Review 队列；不返回领取凭据。", "inputSchema": _schema({})},
    {"name": "kp_desktop_claim", "description": "领取一条正式规划或 Review；同 operation_id 重试恢复同一请求。", "inputSchema": _schema({"role": {"type": "string", "enum": ["plan", "review"]}, "operation_id": _STRING, "thread_id": _STRING}, ("role", "operation_id"))},
    {"name": "kp_desktop_heartbeat", "description": "显式续租自身有效领取；过期轮次不能复活。", "inputSchema": _schema({**_IDENTITY_PROPERTIES, "progress": _STRING}, _IDENTITY)},
    {"name": "kp_desktop_submit_plan", "description": "提交规划正文；核心生成元数据并发布，或保存草稿转人工。", "inputSchema": _schema({**_IDENTITY_PROPERTIES, "result": _PLAN_SCHEMA, "thread_id": _STRING}, (*_IDENTITY, "result"))},
    {"name": "kp_desktop_submit_review", "description": "提交严格 Review 结果；核心复用登记、修复任务及结算流程。", "inputSchema": _schema({**_IDENTITY_PROPERTIES, "result": review_settle.RESULT_SCHEMA, "thread_id": _STRING}, (*_IDENTITY, "result"))},
    {"name": "kp_desktop_fail", "description": "失败结束自身有效领取；不修改其它阶段。", "inputSchema": _schema({**_IDENTITY_PROPERTIES, "reason": _STRING}, (*_IDENTITY, "reason"))},
]


def _tool_result(name, arguments, db_path):
    from .cli import Context
    methods = {"kp_desktop_status": cmd_desktop_queue_status, "kp_desktop_claim": _claim,
               "kp_desktop_heartbeat": _heartbeat, "kp_desktop_submit_plan": lambda c, p: _submit(c, p, "plan"),
               "kp_desktop_submit_review": lambda c, p: _submit(c, p, "review"), "kp_desktop_fail": _fail}
    if name not in methods:
        return {"content": [{"type": "text", "text": "Unknown tool"}], "isError": True}
    ctx = None
    try:
        ctx = Context(Path(db_path))
        response = methods[name](ctx, arguments)
        return {"content": [{"type": "text", "text": _canonical(response)}], "structuredContent": response, "isError": False}
    except util.KanpilotError as exc:
        # Never echo tool arguments or tracebacks, even invalid tokens.
        token = arguments.get("claim_token")
        message = exc.message.replace(token, "<redacted>") if isinstance(token, str) and token else exc.message
        return {"content": [{"type": "text", "text": _canonical({"code": exc.code, "error": message})}], "isError": True}
    except sqlite3.Error:
        return {"content": [{"type": "text", "text": _canonical({
            "code": "database_unavailable",
            "error": "Desktop 数据库暂时不可用；检查文件访问权限或数据库占用后再查询。"
        })}], "isError": True}
    except (OSError, ValueError, KeyError, TypeError):
        return {"content": [{"type": "text", "text": "Desktop 核心材料或检查点不可用；交人工核对。"}], "isError": True}
    finally:
        if ctx is not None:
            ctx.close()


def _rpc_error(identifier, code, message):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _dispatch(message, db_path):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(None, -32600, "Invalid request")
    identifier = message.get("id")
    if "id" not in message:
        return None
    if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
        return _rpc_error(None, -32600, "Invalid request id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(identifier, -32602, "Invalid params")
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "kaneopilot-desktop-queue", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _TOOLS}
    elif method == "tools/call":
        if (set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str)
                or not isinstance(params.get("arguments", {}), dict)):
            return _rpc_error(identifier, -32602, "Invalid tool params")
        result = _tool_result(params["name"], params.get("arguments", {}), db_path)
    else:
        return _rpc_error(identifier, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def run_stdio(db_path):
    """Bounded JSONL MCP. The CLI must require an explicit database path."""
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    while True:
        line = stream.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        size = len(line) if isinstance(line, bytes) else len(line.encode("utf-8"))
        if size > MAX_MESSAGE_BYTES:
            while not line.endswith(b"\n" if isinstance(line, bytes) else "\n"):
                line = stream.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
            response = _rpc_error(None, -32600, "Message too large")
        elif not line.strip():
            continue
        else:
            try:
                response = _dispatch(json.loads(line), db_path)
            except (ValueError, UnicodeError):
                response = _rpc_error(None, -32700, "Parse error")
        if response is not None:
            output = _canonical(response)
            if len(output.encode("utf-8")) > MAX_MESSAGE_BYTES:
                output = _canonical(_rpc_error(response.get("id"), -32603, "Result too large; inspect frozen material files"))
            sys.stdout.write(output + "\n")
            sys.stdout.flush()
