"""Read-only compatibility access to historical core verification receipts."""
from __future__ import annotations

import os

from . import db, process_owner, util


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
            "recovery_pending": False,
            "detail": "历史本机验证记录，仅供审计；当前推进只核对 Plus 验证证据"}


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
                "checks": record.get("checks") or [], "detail": "验证仍在运行" if _alive(record) else "历史验证进程已中断；请让 Plus 重新执行并验证"}
    receipt = record.get("receipt") or {}
    audit = ctx.conn.execute("SELECT detail_json FROM events WHERE task_id=? "
        "AND kind='verification_checks_completed' ORDER BY id DESC LIMIT 1", (task["task_id"],)).fetchone()
    detail = util.jload(audit["detail_json"]) if audit else {}
    if (detail.get("operation_id") != record.get("operation_id")
            or detail.get("receipt_sha256") != util.sha256_text(util.jdump(receipt))):
        return {"ok": True, "state": "completed", "verification_ok": False,
                "recovery_pending": False, "checks": receipt.get("checks") or [],
                "detail": "历史验证回执与审计不符；请核对 Plus 证据"}
    return {"ok": True, "state": "completed", "verification_ok": receipt.get("verification_ok"),
            "recovery_pending": False, "checks": receipt.get("checks") or [],
            "detail": "历史本机验证记录，仅供审计；当前推进只核对 Plus 验证证据"}


def valid_receipt(conn, task):
    """Historical core receipts never authorize current execution verification."""
    return None

def cmd_run_verification_checks(ctx, payload):
    """Compatibility entry: Pilot never executes verification commands."""
    raise util.KanpilotError("conflict",
        "Pilot 不再在本机运行验证命令。请让 Plus 重新执行并验证；已有 Plus 验证记录可使用重新核验验证证据。")
