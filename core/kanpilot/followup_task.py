"""User-confirmed follow-up task for findings outside the original task (KP-MG04).

The review already separates blocking findings from out-of-scope residual risks
(KP-MG02). This command turns such a finding into a linked secondary task
instead of spending the original task's repair rounds on it. v1 is CLI-only and
explicitly confirmed; the new task runs its own plan, branch and PR.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import bridge, constants, core, db, remote, runtime_operations, util

TOOL = "kaneo_create_followup"
HARNESS = "kaneo"
MARKER = "kaneopilot-followup"
SCOPE = "followup-task:"


def _scope(source_task_id):
    return SCOPE + source_task_id


def _operation_id(source_task_id, dedupe_key):
    return runtime_operations.compute_request_sha256(
        {"source_task_id": source_task_id, "dedupe_key": dedupe_key})


def _existing(conn, source_task_id, dedupe_key):
    for row in conn.execute("SELECT * FROM tasks WHERE review_source_task_id=?",
                            (source_task_id,)):
        if db.task_extra(row).get("followup_key") == dedupe_key:
            return row
    return None


def _snapshot(conn, result, task_id):
    source = bridge.stable({
        "task": result.get("task"),
        "comments": result.get("comments"),
        "relations": result.get("relations"),
        "activity": result.get("activity"),
    })
    directory = constants.sources_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(source, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False), encoding="utf-8")
    return path


def cmd_create_followup_task(ctx, payload):
    util.require(payload.get("confirm") is True, "另立任务需要显式确认", "bad_request")
    source = db.require_task(ctx.conn, payload["source_task_id"])
    util.require(not db.task_is_purged(source), "来源任务已永久移除", "conflict")
    dedupe_key = payload.get("dedupe_key")
    util.require(isinstance(dedupe_key, str) and dedupe_key.strip()
                 and "<!--" not in dedupe_key and "-->" not in dedupe_key,
                 "dedupe_key 需为非空且不含注释定界符的字符串", "bad_request")
    dedupe_key = dedupe_key.strip()
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    util.require(0 < len(title) <= 200, "标题需为 1-200 字", "bad_request")
    util.require(len(description) <= 8000, "描述过长（上限 8000 字）", "bad_request")
    priority = payload.get("priority") or "high"
    util.require(priority in bridge.VALID_PRIORITIES, "无效的优先级", "bad_request")

    existing = _existing(ctx.conn, source["task_id"], dedupe_key)
    if existing is not None:
        return {"ok": True, "followup_task_id": existing["task_id"], "created": False}
    scope = _scope(source["task_id"])
    owner = f"create-followup-task:{util.new_uuid()}"
    core.acquire_lifecycle(ctx.conn, owner)
    try:
        operation_id = _operation_id(source["task_id"], dedupe_key)
        previous = db.get_runtime_operation(ctx.conn, scope, operation_id)
        if previous is not None and previous["status"] == "accepted":
            result = util.jload(previous["result_json"]) or {}
            if result.get("task_id"):
                return {"ok": True, "followup_task_id": result["task_id"],
                        "created": False, "reused": True}
        with db.write_txn(ctx.conn):
            runtime_operations.begin_operation(
                ctx.conn, scope, operation_id, HARNESS, TOOL,
                {"source_task_id": source["task_id"], "dedupe_key": dedupe_key},
                now=ctx.now_iso)
        try:
            client = remote.get_client()
            result = remote.create_todo(
                client, source["project_id"], dedupe_key,
                {"title": title, "description": description, "priority": priority},
                marker_prefix=MARKER)
        except bridge.BridgeError as exc:
            # The remote marker makes a same-parameter retry safe; keep it unknown.
            with db.write_txn(ctx.conn):
                runtime_operations.complete_operation(
                    ctx.conn, scope, operation_id, status="unknown",
                    result={"reason": str(exc)}, now=ctx.now_iso)
            raise util.KanpilotError("remote_unavailable", str(exc)) from exc
        task_id = str(result["task"]["id"])
        snapshot_path = _snapshot(ctx.conn, result, task_id)
        with db.write_txn(ctx.conn):
            task = db.get_task(ctx.conn, task_id)
            if task is None:
                ctx.conn.execute(
                    "INSERT INTO tasks(task_id, project_id, project_name, title, "
                    "repository, stage, status, kaneo_status, kaneo_checked_at, "
                    "requirements_sha256, source_sha256, source_path, plan_kind, "
                    "review_source_task_id, review_key, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, source["project_id"], source["project_name"], title,
                     source["repository"], "plan", "needs_plan",
                     result.get("todo_slug"), ctx.now_iso,
                     result.get("requirements_sha256"), result.get("source_sha256"),
                     str(snapshot_path), "standard", source["task_id"], None,
                     ctx.now_iso, ctx.now_iso))
                task = db.get_task(ctx.conn, task_id)
                db.set_extra(ctx.conn, task, ctx.now_iso, followup_key=dedupe_key)
                db.add_event(ctx.conn, ctx.now_iso, "task_created", task_id=task_id,
                             actor="user",
                             detail={"via": "create-followup-task",
                                     "source_task_id": source["task_id"]})
            runtime_operations.complete_operation(
                ctx.conn, scope, operation_id, status="accepted",
                result={"task_id": task_id}, now=ctx.now_iso)
        return {"ok": True, "followup_task_id": task_id,
                "created": bool(result.get("created"))}
    finally:
        with db.write_txn(ctx.conn):
            core.release_lifecycle(ctx.conn, owner)
