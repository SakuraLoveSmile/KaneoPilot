"""Core-owned, explicit verification reruns; never trust model-authored status text."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

from . import bridge, core, db, derive, dsh_events, git_delivery, process_owner, remote, review_snapshot, round_store, util, verification_recovery, verify_evidence

CHECK_TIMEOUT_SECONDS = 600


def _record(task):
    return db.task_extra(task).get("verification_checks") or {}


def public_state(task):
    record = _record(task)
    if not record:
        return None
    receipt = record.get("receipt") or {}
    binding = record.get("binding") or {}
    state = record.get("state")
    if state == "running" and not _alive(record):
        state = "interrupted"
    return {"operation_id": record.get("operation_id"), "state": state,
            "execution_run_id": binding.get("execution_run_id"), "plan_sha256": binding.get("plan_sha256"),
            "checks": receipt.get("checks") or record.get("checks") or [],
            "verification_ok": receipt.get("verification_ok"),
            "recovery_pending": bool(receipt.get("verification_ok") and not record.get("monitor_request_id")),
            "detail": ("逐条验证已保存，恢复推进待重试：" + record["recovery_error"]
                       if record.get("recovery_error") else receipt.get("detail"))}


def _alive(record):
    def alive(pid):
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    def owned_alive(key):
        pid = record.get(key)
        birth = record.get(key.removesuffix("_pid") + "_birth")
        return process_owner.process_birth(pid) == birth if birth else alive(pid)
    # Called inside a write-transaction guard: do not spawn ps or any other
    # process while holding SQLite's writer lock.
    return record.get("state") == "running" and any(owned_alive(key) for key in ("owner_pid", "command_pid"))


def require_not_running(conn, task_id):
    task = db.get_task(conn, task_id)
    record = _record(task) if task is not None else {}
    owned = (getattr(conn, "verification_operation_id", None) == record.get("operation_id")
             and record.get("owner_pid") == os.getpid())
    if task is not None and not owned and _alive(record):
        raise util.KanpilotError("conflict", "本任务正在逐条运行验证，请等待结果后再操作")


def _worktree(task, row):
    path = Path(row["worktree_path"])
    try:
        if (path.is_symlink() or path.resolve() == Path(task["repository"]).resolve()
                or git_delivery._common_dir(path) != git_delivery._common_dir(task["repository"])
                or Path(review_snapshot._git(path, ["rev-parse", "--show-toplevel"]).decode().strip()).resolve() != path.resolve()):
            verification_recovery._conflict("验证目录不是本任务登记的独立工作树")
        return review_snapshot.fingerprint(review_snapshot.freeze(path))
    except (OSError, git_delivery.GitDeliveryError) as exc:
        raise util.KanpilotError("conflict", "任务工作树不可核验") from exc


def _binding(conn, task):
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    if row is None or not row["worktree_path"]:
        verification_recovery._conflict("本任务缺少登记执行工作树")
    return {"task_id": task["task_id"], "execution_run_id": task["run_id"],
            "plan_sha256": task["plan_sha256"], "worktree_path": row["worktree_path"],
            "worktree_sha256": _worktree(task, row),
            "source_content_sha256": _source_content(Path(row["worktree_path"])),
            "commands": (db.task_extra(task).get("plan_verify") or {}).get("commands")}


def _source_content(root):
    """Actual source bytes stay identical when core commits the checked tree."""
    names = sorted(set(name for name in review_snapshot._git(root,
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).decode("utf-8").split("\0") if name))
    entries = []
    for name in names:
        path = root / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            # A tracked deletion becomes absent from the index after commit.
            continue
        if stat.S_ISLNK(before.st_mode):
            entries.append({"path": name, "kind": "symlink", "sha256": util.sha256_bytes(os.fsencode(os.readlink(path)))})
        elif stat.S_ISREG(before.st_mode):
            digest = hashlib.sha256()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                actual = os.fstat(stream.fileno())
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode)
                    != (actual.st_ino, actual.st_size, actual.st_mtime_ns, actual.st_mode)
                    or (actual.st_size, actual.st_mtime_ns, actual.st_mode)
                    != (after.st_size, after.st_mtime_ns, after.st_mode)):
                verification_recovery._conflict("读取验证成果期间源文件已变化")
            entries.append({"path": name, "kind": "file", "sha256": digest.hexdigest(),
                            "executable": bool(before.st_mode & 0o111)})
        else:
            verification_recovery._conflict("验证工作树包含无法绑定的文件类型")
    return util.sha256_text(util.jdump(entries))


def _equivalent_core_commit(task, expected, actual):
    # Only the core's recorded commit for this execution may change Git's HEAD
    # and staging/status representation. Source bytes and every other identity
    # must remain identical; a manual commit is never adopted here.
    if {key: value for key, value in expected.items() if key != "worktree_sha256"} != {
            key: value for key, value in actual.items() if key != "worktree_sha256"}:
        return False
    delivery = core.delivery_of(task)
    sha = delivery.get("head_sha")
    if not sha or not any(item.get("execution_run_id") == task["run_id"] and item.get("sha") == sha
                           for item in delivery.get("commits") or []):
        return False
    try:
        git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]),
                                           require_clean=True, expected_head=sha)
    except git_delivery.GitDeliveryError:
        return False
    return True


def _path_environment(events):
    """Reuse only inert, literal PATH exports from this execution's tool results."""
    environment = os.environ.copy()
    prefixes = []
    for result in verify_evidence.extract_command_results(dsh_events.to_messages(events)):
        parsed = verify_evidence._path_prefix(result["command"])
        if parsed and parsed[0] not in prefixes:
            prefixes.append(parsed[0])
    if prefixes:
        # Different commands may use the same frozen prefix. Conflicting prefixes
        # need an explicit environment decision rather than silently picking one.
        if len(prefixes) != 1:
            verification_recovery._conflict("原执行使用不同 PATH 前缀，无法确定验证环境")
        value = prefixes[0].split("=", 1)[1]
        if value.startswith('"'):
            value = value[1:-1]
        environment["PATH"] = re.sub(r'\$\{PATH\}|\$PATH', lambda _: environment.get("PATH", ""), value)
    return environment


def _identity(task, payload):
    if (not db.task_is_active(task) or task["run_id"] != payload["expected_execution_run_id"]
            or task["plan_sha256"] != payload["expected_plan_sha256"]):
        verification_recovery._conflict("当前任务、执行轮次或计划已变化，请刷新后重试")


def _require_current_remote(task):
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    current = live.get("task") or {}
    if (current.get("id") != task["task_id"] or current.get("projectId") != task["project_id"]
            or live.get("requirements_sha256") != task["requirements_sha256"]
            or current.get("status") not in ("in-progress", "in-review")):
        raise util.KanpilotError("remote_conflict", "远端任务、项目、需求或执行阶段已变化，请刷新并检查后再验证")


def _receipt(conn, task):
    record = _record(task)
    receipt = record.get("receipt")
    if record.get("state") != "completed" or not isinstance(receipt, dict):
        return None
    digest = util.sha256_text(util.jdump(receipt))
    audited = conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='verification_checks_completed' ORDER BY id DESC LIMIT 1", (task["task_id"],)).fetchone()
    audit = util.jload(audited["detail_json"]) if audited else {}
    if audit.get("receipt_sha256") != digest or audit.get("operation_id") != record.get("operation_id"):
        verification_recovery._conflict("验证回执与核心审计记录不符")
    actual_binding = _binding(conn, task)
    if receipt.get("binding") != actual_binding and not _equivalent_core_commit(task, receipt.get("binding") or {}, actual_binding):
        verification_recovery._conflict("验证后计划、执行轮次或工作树内容已变化，需要重新运行验证")
    if verification_recovery._read_evidence(conn, task, require_pass=False)[0] != receipt.get("evidence_sha256"):
        verification_recovery._conflict("验证后原执行记录或计划文件已变化")
    for check in receipt.get("checks") or []:
        path = util.inside(Path(check["output_path"]), Path(receipt["output_root"]), "验证输出")
        if util.sha256_file(path) != check["output_sha256"]:
            verification_recovery._conflict("核心验证输出已变化或不可读")
    return receipt


def valid_receipt(conn, task):
    receipt = _receipt(conn, task)
    if not receipt or not receipt.get("verification_ok"):
        return None
    commands = receipt["binding"]["commands"]
    checks = receipt.get("checks") or []
    if ([item.get("command") for item in checks] != commands
            or any(type(item.get("exit_code")) is not int or item["exit_code"] != 0
                   or item.get("timed_out") for item in checks)):
        verification_recovery._conflict("验证回执没有逐条成功的独立退出码")
    return {"ok": True, "matched": commands, "missing": [], "failed": []}


def _response(conn, task, *, deduplicated):
    record = _record(task)
    receipt = record["receipt"]
    request = db.get_request(conn, record.get("monitor_request_id")) if record.get("monitor_request_id") else None
    detail = receipt["detail"]
    if record.get("recovery_error"):
        detail = "逐条验证已完成并保存；恢复推进暂未成功：" + record["recovery_error"] + "。请重试原操作，不会重复运行验证。"
    return {"ok": True, "deduplicated": deduplicated, "verification_ok": receipt["verification_ok"],
            "checks": receipt["checks"], "detail": detail,
            "recovery_pending": bool(receipt["verification_ok"] and not request),
            "request": derive.request_view(request, util.now_dt()) if request is not None else None}


def _recover_completion(ctx, task):
    try:
        _require_current_remote(task)
        result = verification_recovery.recover(ctx, {"task_id": task["task_id"],
            "expected_execution_run_id": task["run_id"]})
        changes = {"monitor_request_id": result["request"]["request_id"], "recovery_error": None}
    except util.KanpilotError as exc:
        changes = {"recovery_error": exc.message}
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task["task_id"])
        if _record(current).get("operation_id") == _record(task).get("operation_id"):
            db.set_extra(ctx.conn, current, util.now_iso(), verification_checks=dict(_record(current), **changes))


def cmd_get_verification_check_status(ctx, payload):
    task = db.require_task(ctx.conn, payload["task_id"])
    record = _record(task)
    if record.get("operation_id") != payload["operation_id"]:
        started = False
        for event in ctx.conn.execute("SELECT kind,detail_json FROM events WHERE task_id=? AND kind IN ('verification_checks_started','verification_checks_completed') ORDER BY id DESC", (task["task_id"],)):
            detail = util.jload(event["detail_json"]) or {}
            if detail.get("operation_id") != payload["operation_id"]:
                continue
            if event["kind"] == "verification_checks_completed":
                return {"ok": True, "state": "completed", "verification_ok": False, "recovery_pending": False,
                        "checks": (detail.get("receipt") or {}).get("checks") or [],
                        "detail": "此操作已由新的验证操作取代，请载入当前任务状态；旧操作不会再次执行"}
            started = True
        if started:
            return {"ok": True, "state": "interrupted", "recovery_pending": False,
                    "detail": "此操作未取得完成回执，已由新的验证取代；旧操作不会再次执行"}
        return {"ok": True, "state": "not_found", "detail": "没有此操作的当前验证回执"}
    state = record.get("state")
    if state == "running":
        return {"ok": True, "state": "running" if _alive(record) else "interrupted",
                "checks": record.get("checks") or [], "detail": "验证仍在运行" if _alive(record) else "验证进程已中断，未认定通过；请重新运行"}
    try:
        _receipt(ctx.conn, task)
    except (util.KanpilotError, OSError) as exc:
        return {"ok": True, "state": "completed", "verification_ok": False,
                "checks": (record.get("receipt") or {}).get("checks") or [], "detail": str(exc)}
    return dict(_response(ctx.conn, task, deduplicated=True), state="completed")


def _update_operation(ctx, operation_id, **fields):
    with db.write_txn(ctx.conn):
        task = db.require_task(ctx.conn, ctx.verification_task_id)
        record = _record(task)
        if record.get("operation_id") != operation_id or record.get("state") != "running":
            verification_recovery._conflict("验证操作登记已变化")
        db.set_extra(ctx.conn, task, util.now_iso(), verification_checks=dict(record, **fields))


def _run_command(ctx, operation_id, command, cwd, environment, output):
    started = util.now_iso()
    timed_out, code, detail = False, None, None
    process = None
    handshake = output.with_suffix(".start.json")
    with output.open("xb") as stream:
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("verification_process.py")),
                str(os.getpid()), str(handshake), command, str(CHECK_TIMEOUT_SECONDS),
                util.jdump(process_owner.process_birth(os.getpid()))],
                cwd=cwd, env=environment, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            _update_operation(ctx, operation_id, command_pid=process.pid,
                              command_birth=process_owner.process_birth(process.pid))
            # The watchdog does not start Bash until this core-owned registration
            # has committed. A parent crash before it leaves no source writer.
            pending_handshake = handshake.with_suffix(".pending")
            with pending_handshake.open("x") as registration:
                json.dump({"owner_pid": os.getpid(), "command_pid": process.pid,
                           "owner_birth": process_owner.process_birth(os.getpid()),
                           "command_birth": process_owner.process_birth(process.pid)}, registration)
            os.replace(pending_handshake, handshake)
            try:
                code = process.wait(timeout=CHECK_TIMEOUT_SECONDS + 10)
                if code == 124:
                    timed_out, detail = True, "验证命令超时，未认定通过"
            except subprocess.TimeoutExpired:
                timed_out, detail = True, "验证命令超时，未认定通过"
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                code = process.returncode
        except OSError as exc:
            detail = "验证命令无法启动：" + str(exc)
            stream.write(detail.encode("utf-8"))
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            _update_operation(ctx, operation_id, command_pid=None, command_birth=None)
    with output.open("rb") as stream:
        stream.seek(max(0, output.stat().st_size - 4000))
        tail = stream.read().decode("utf-8", errors="replace")
    return {"command": command, "exit_code": code, "timed_out": timed_out, "detail": detail,
            "started_at": started, "finished_at": util.now_iso(), "output_tail": tail,
            "output_path": str(output), "output_sha256": util.sha256_file(output)}


def cmd_run_verification_checks(ctx, payload):
    task_id, operation_id = payload["task_id"], payload["operation_id"]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', operation_id):
        raise util.bad_request("operation_id 格式无效")
    task = db.require_task(ctx.conn, task_id)
    _identity(task, payload)
    record = _record(task)
    if record.get("operation_id") == operation_id:
        if record.get("state") == "completed":
            _receipt(ctx.conn, task)
            if record["receipt"].get("verification_ok") and verification_recovery.candidate(ctx.conn, task):
                _recover_completion(ctx, task)
                task = db.require_task(ctx.conn, task_id)
            return _response(ctx.conn, task, deduplicated=True)
        verification_recovery._conflict("此验证操作仍在运行或已中断；请读取结果，中断后使用新操作重新运行")
    for event in ctx.conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='verification_checks_started'", (task_id,)):
        if (util.jload(event["detail_json"]) or {}).get("operation_id") == operation_id:
            verification_recovery._conflict("此操作已经执行过；请使用新操作重新运行")
    require_not_running(ctx.conn, task_id)
    if not verification_recovery.candidate(ctx.conn, task):
        verification_recovery._conflict("仅当前待确认验证且无活动请求的任务可逐条运行验证")
    _require_current_remote(task)
    hashes, job, _ = verification_recovery._read_evidence(ctx.conn, task, require_pass=False)
    verification_recovery._require_live_terminal(job)
    binding = _binding(ctx.conn, task)
    directory = Path(next(path for path in hashes if Path(path).name == "job.json")).parent
    events = [json.loads(line) for line in (directory / dsh_events.EVENT_LOG_NAME).read_text().splitlines() if line.strip()]
    environment = _path_environment(events)
    snapshot = verification_recovery._snapshot(ctx.conn, task_id, task["run_id"])
    output_root = directory.parent / "verification-checks" / operation_id
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task_id)
        _identity(current, payload)
        require_not_running(ctx.conn, task_id)
        if snapshot != verification_recovery._snapshot(ctx.conn, task_id, task["run_id"]):
            verification_recovery._conflict("准备验证期间任务记录已变化")
        if output_root.exists():
            verification_recovery._conflict("此验证操作已有未登记输出，请使用新操作重试")
        record = {"operation_id": operation_id, "state": "running", "owner_pid": os.getpid(),
                  "owner_birth": process_owner.process_birth(os.getpid()), "command_pid": None,
                  "command_birth": None, "binding": binding, "checks": [], "started_at": util.now_iso()}
        db.set_extra(ctx.conn, current, util.now_iso(), verification_checks=record)
        db.add_event(ctx.conn, util.now_iso(), "verification_checks_started", task_id=task_id,
                     actor="user", detail={"operation_id": operation_id, "execution_run_id": task["run_id"]})
    ctx.verification_task_id = task_id
    ctx.conn.verification_operation_id = operation_id
    checks, error = [], None
    try:
        output_root.mkdir(parents=True, exist_ok=False)
        for index, command in enumerate(binding["commands"]):
            current = db.require_task(ctx.conn, task_id)
            _identity(current, payload)
            if _binding(ctx.conn, current) != binding:
                verification_recovery._conflict("验证期间工作树或计划已变化，未认定通过")
            checks.append(_run_command(ctx, operation_id, command, binding["worktree_path"], environment, output_root / (str(index) + ".log")))
            _update_operation(ctx, operation_id, checks=checks)
        current = db.require_task(ctx.conn, task_id)
        _identity(current, payload)
        if _binding(ctx.conn, current) != binding:
            verification_recovery._conflict("验证命令改变了工作树内容，需要检查后重新运行验证")
        if verification_recovery._read_evidence(ctx.conn, current, require_pass=False)[0] != hashes:
            verification_recovery._conflict("验证期间原执行记录或计划已变化")
        verification_recovery._require_live_terminal(job)
        _require_current_remote(current)
    except (util.KanpilotError, OSError, subprocess.SubprocessError) as exc:
        error = str(exc)
    passed = bool(not error and len(checks) == len(binding["commands"]) and all(type(item["exit_code"]) is int and item["exit_code"] == 0 and not item["timed_out"] for item in checks))
    detail = "全部验证命令逐条通过，已重新核验并安排监控推进" if passed else error or "验证未全部通过，请查看各条独立退出码与输出后修复并重跑"
    receipt = {"binding": binding, "checks": checks, "verification_ok": passed,
               "detail": detail, "output_root": str(output_root), "completed_at": util.now_iso(),
               "evidence_sha256": hashes}
    with db.write_txn(ctx.conn):
        current = db.require_task(ctx.conn, task_id)
        _identity(current, payload)
        record = _record(current)
        if record.get("operation_id") != operation_id or record.get("state") != "running":
            verification_recovery._conflict("验证完成时操作身份已变化")
        db.set_extra(ctx.conn, current, util.now_iso(), verification_checks=dict(record, state="completed", owner_pid=None, command_pid=None, receipt=receipt))
        db.add_event(ctx.conn, util.now_iso(), "verification_checks_completed", task_id=task_id,
                     actor="core", detail={"operation_id": operation_id, "receipt_sha256": util.sha256_text(util.jdump(receipt)), "verification_ok": passed, "receipt": receipt})
    if passed:
        _recover_completion(ctx, db.require_task(ctx.conn, task_id))
    return _response(ctx.conn, db.require_task(ctx.conn, task_id), deduplicated=False)
