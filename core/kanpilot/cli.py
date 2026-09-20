"""CLI front-end: `kanpilot.py <command> [--json '<payload>'] [--db <path>]`.

Success: single JSON object on stdout, exit 0.
Failure: {"error": ..., "code": ...} on stdout, exit 1 (diagnostics on stderr).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from . import (
    cmd_dispatch, cmd_exec, cmd_migrate, cmd_queue, cmd_read, cmd_remote,
    constants, db, util,
)

# field spec: name -> (required, kind); kind: str|int|bool|dict|list|any|str|null
COMMANDS: dict[str, tuple[dict, Callable]] = {
    "doctor": (
        {"online": (False, "bool")}, cmd_read.cmd_doctor,
    ),
    "tick": ({}, cmd_read.cmd_tick),
    "overview": ({}, cmd_read.cmd_overview),
    "list-tasks": (
        {
            "stage": (False, "str"), "status": (False, "str"),
            "needs_attention": (False, "bool"),
        },
        cmd_read.cmd_list_tasks,
    ),
    "get-task": ({"task_id": (True, "str")}, cmd_read.cmd_get_task),
    "list-requests": (
        {"status": (False, "str"), "limit": (False, "int")},
        cmd_read.cmd_list_requests,
    ),
    "list-events": (
        {"task_id": (False, "str"), "limit": (False, "int")},
        cmd_read.cmd_list_events,
    ),
    "enqueue": (
        {
            "task_id": (True, "str"), "kind": (False, "str"),
            "note": (False, "str"),
        },
        cmd_queue.cmd_enqueue,
    ),
    "set-priority": (
        {
            "request_id": (False, "str"), "task_id": (False, "str"),
            "priority": (True, "int"),
        },
        cmd_queue.cmd_set_priority,
    ),
    "set-paused": (
        {"stage": (True, "str|null"), "paused": (True, "bool")},
        cmd_queue.cmd_set_paused,
    ),
    "resolve": (
        {
            "task_id": (True, "str"), "action": (True, "str"),
            "note": (False, "str"),
        },
        cmd_queue.cmd_resolve,
    ),
    "recheck": (
        {"task_id": (True, "str"), "offline": (False, "bool")},
        cmd_remote.cmd_recheck,
    ),
    "sync-tasks": (
        {"executor": (True, "str"), "projects": (True, "list")},
        cmd_queue.cmd_sync_tasks,
    ),
    "claim": (
        {
            "stage": (True, "str"), "executor": (True, "str"),
            "request_id": (False, "str"),
        },
        cmd_exec.cmd_claim,
    ),
    "heartbeat": (
        {
            "request_id": (True, "str"), "run_id": (True, "str"),
            "progress": (False, "str"),
        },
        cmd_exec.cmd_heartbeat,
    ),
    "report": (
        {
            "request_id": (True, "str"), "run_id": (True, "str"),
            "outcome": (True, "str"), "note": (False, "str"),
            "send_state": (False, "str"), "evidence": (False, "any"),
            "progress": (False, "str"), "kaneo_status": (False, "str"),
            "failure_code": (False, "str"), "failure_reason": (False, "str"),
        },
        cmd_exec.cmd_report,
    ),
    "publish-plan": (
        {
            "task_id": (True, "str"), "plan_path": (True, "str"),
            "plan_kind": (True, "str"), "source_sha256": (True, "str"),
            "requirements_sha256": (True, "str"),
            "generated_at": (True, "str"), "planner_model": (True, "str"),
            "reasoning_effort": (True, "str"), "run_id": (False, "str"),
        },
        cmd_exec.cmd_publish_plan,
    ),
    "register-review": (
        {
            "task_id": (True, "str"), "report_path": (True, "str"),
            "conclusion": (True, "str"), "run_id": (False, "str"),
            "review_key": (False, "str"), "fix_task_id": (False, "str"),
            "fix_task_source_sha256": (False, "str"),
            "repair_plan_path": (False, "str"),
            "kaneo_status": (False, "str"),
        },
        cmd_exec.cmd_register_review,
    ),
    "create-review-fix": (
        {
            "source_task_id": (True, "str"), "title": (True, "str"),
            "description": (True, "str"), "priority": (True, "str"),
            "dedupe_key": (True, "str"),
        },
        cmd_remote.cmd_create_review_fix,
    ),
    "advance-task": (
        {
            "task_id": (True, "str"), "target": (True, "str"),
            "expected_current": (True, "str"), "run_id": (False, "str"),
            "evidence_note": (False, "str"),
        },
        cmd_remote.cmd_advance_task,
    ),
    "dispatch-prepare": (
        {
            "task_id": (True, "str"), "run_id": (True, "str"),
            "evidence": (True, "dict"),
        },
        cmd_dispatch.cmd_dispatch_prepare,
    ),
    "dispatch-begin-send": (
        {"task_id": (True, "str"), "run_id": (True, "str")},
        cmd_dispatch.cmd_dispatch_begin_send,
    ),
    "dispatch-confirm": (
        {
            "task_id": (True, "str"), "run_id": (True, "str"),
            "evidence": (True, "dict"),
        },
        cmd_dispatch.cmd_dispatch_confirm,
    ),
    "dispatch-fail": (
        {
            "task_id": (True, "str"), "run_id": (True, "str"),
            "send_state": (True, "str"), "reason": (True, "str"),
        },
        cmd_dispatch.cmd_dispatch_fail,
    ),
    "migrate": (
        {
            "legacy_root": (False, "str"), "plan_root": (False, "str"),
            "backup_root": (False, "str"), "dry_run": (False, "bool"),
        },
        cmd_migrate.cmd_migrate,
    ),
}


def _check_type(value: Any, kind: str, field: str) -> None:
    ok = True
    if kind == "str":
        ok = isinstance(value, str)
    elif kind == "str|null":
        ok = value is None or isinstance(value, str)
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "dict":
        ok = isinstance(value, dict)
    elif kind == "list":
        ok = isinstance(value, list)
    elif kind == "any":
        ok = True
    if not ok:
        raise util.bad_request(f"field {field} must be {kind}")


def validate_payload(command: str, payload: Any) -> dict:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise util.bad_request("payload must be a JSON object")
    spec, _fn = COMMANDS[command]
    unknown = set(payload) - set(spec)
    if unknown:
        raise util.bad_request(f"unknown fields: {', '.join(sorted(unknown))}")
    for field, (required, kind) in spec.items():
        if required and field not in payload:
            raise util.bad_request(f"missing required field: {field}")
        if field in payload:
            _check_type(payload[field], kind, field)
    return payload


class Context:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = db.connect(db_path)
        self.now = util.now_dt()
        self.now_iso = util.to_iso(self.now)

    def close(self) -> None:
        self.conn.close()


def _parse_flag_value(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def resolve_db_path(flag: Optional[str]) -> Path:
    if flag:
        return Path(flag).expanduser()
    return constants.default_db_path()


def run(command: str, payload: Optional[dict], db_path: Path) -> dict:
    """Programmatic entry used by tests; returns the result dict."""
    if command not in COMMANDS:
        raise util.KanpilotError("bad_request", f"unknown command {command}")
    payload = validate_payload(command, payload)
    ctx = Context(db_path)
    try:
        return COMMANDS[command][1](ctx, payload)
    finally:
        ctx.close()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="kanpilot")
    parser.add_argument("command")
    parser.add_argument("--json", dest="json_payload")
    parser.add_argument("--db", dest="db_path")
    args, extras = parser.parse_known_args(argv)

    # Flag-style payloads: `--task-id x` merges into {"task_id": "x"}.
    flags: dict[str, Any] = {}
    index = 0
    while index < len(extras):
        item = extras[index]
        if item.startswith("--"):
            key = item[2:].replace("-", "_")
            if index + 1 < len(extras) and not extras[index + 1].startswith("--"):
                flags[key] = _parse_flag_value(extras[index + 1])
                index += 2
            else:
                flags[key] = True
                index += 1
        else:
            index += 1

    try:
        if args.json_payload is not None:
            try:
                payload = json.loads(args.json_payload)
            except ValueError as exc:
                raise util.bad_request(f"--json is not valid JSON: {exc}")
        elif not sys.stdin.isatty():
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        else:
            payload = {}
        if not isinstance(payload, dict):
            raise util.bad_request("payload must be a JSON object")
        payload.update(flags)
        result = run(args.command, payload, resolve_db_path(args.db_path))
    except util.KanpilotError as exc:
        print(
            json.dumps({"error": exc.message, "code": exc.code},
                       ensure_ascii=False)
        )
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        print(
            json.dumps({"error": str(exc), "code": "internal"},
                       ensure_ascii=False)
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
