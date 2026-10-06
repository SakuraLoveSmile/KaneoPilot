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
    cmd_archive, cmd_config, cmd_dispatch, cmd_exec, cmd_migrate, cmd_planner,
    cmd_queue, cmd_read, cmd_remote, cmd_session, constants, db,
    planner_inputs, util,
)

def _automation_command(module, name):
    def call(ctx, payload):
        from importlib import import_module
        return getattr(import_module("." + module, __package__), name)(ctx, payload)
    return call


# field spec: name -> (required, kind); kind: str|int|bool|dict|list|any|str|null
COMMANDS: dict[str, tuple[dict, Callable]] = {
    "set-project-automation": ({"project_id": (True, "str"), "automation": (True, "dict"), "expected_policy_revision": (False, "int")}, _automation_command("automation_policy", "cmd_set_project_automation")),
    "set-automation-global": ({"max_active_tasks": (True, "int")}, _automation_command("automation_policy", "cmd_set_automation_global")),
    "record-cost": ({"task_id": (True, "str"), "request_id": (True, "str"), "run_id": (True, "str"), "stage": (True, "str"), "cost_usd": (True, "any"), "source": (True, "str")}, _automation_command("automation_policy", "cmd_record_cost")),
    "delivery-commit": ({"task_id": (True, "str"), "request_id": (True, "str"), "run_id": (True, "str"), "expected_execution_run_id": (True, "str")}, _automation_command("cmd_delivery", "cmd_delivery_commit")),
    "delivery-publish": ({"task_id": (True, "str"), "request_id": (True, "str"), "run_id": (True, "str"), "expected_execution_run_id": (False, "str")}, _automation_command("cmd_delivery_pr", "cmd_delivery_publish")),
    "delivery-sync": ({"task_id": (True, "str"), "request_id": (True, "str"), "run_id": (True, "str"), "expected_execution_run_id": (False, "str")}, _automation_command("cmd_delivery_pr", "cmd_delivery_sync")),
    "doctor": (
        {"online": (False, "bool")}, cmd_read.cmd_doctor,
    ),
    "tick": ({}, cmd_read.cmd_tick),
    "overview": ({}, cmd_read.cmd_overview),
    "list-tasks": (
        {
            "stage": (False, "str"), "status": (False, "str"),
            "board_column": (False, "str"), "needs_attention": (False, "bool"),
            "save_state": (False, "str"),
        },
        cmd_read.cmd_list_tasks,
    ),
    "get-task": ({"task_id": (True, "str")}, cmd_read.cmd_get_task),
    # v3（§5.23.1）：已发布计划正文的核验读取（只读）。
    "get-plan": (
        {
            "task_id": (True, "str"),
            "expected_plan_sha256": (False, "str"),
        },
        cmd_read.cmd_get_plan,
    ),
    "list-requests": (
        {
            "status": (False, "str"), "limit": (False, "int"),
            # v2（§5.21.2）：含已移除（purged）任务的请求回看。
            "include_removed": (False, "bool"),
        },
        cmd_read.cmd_list_requests,
    ),
    # v2（§5.21.1）：单个请求详情（attempts/allowed_actions/attention_state）。
    "get-request": (
        {"request_id": (True, "str")}, cmd_read.cmd_get_request,
    ),
    "list-events": (
        {
            "task_id": (False, "str"), "limit": (False, "int"),
            # v2（§5.21.3）：游标分页 + stage/result/query 筛选。
            "before_id": (False, "int"), "stage": (False, "str"),
            "result": (False, "str"), "query": (False, "str"),
        },
        cmd_read.cmd_list_events,
    ),
    "enqueue": (
        {
            "task_id": (True, "str"), "kind": (False, "str"),
            "note": (False, "str"),
            # v2（§5.22.4）：确认放行 hold 的 dispatch 排队。
            "confirm_plan": (False, "bool"),
            "expected_plan_sha256": (False, "str"),
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
    # v2（§5.6）：须携带规划请求身份与材料指纹；run_id/request_id 必填。
    "publish-plan": (
        {
            "task_id": (True, "str"), "plan_path": (True, "str"),
            "plan_kind": (True, "str"), "source_sha256": (True, "str"),
            "requirements_sha256": (True, "str"),
            "generated_at": (True, "str"), "planner_model": (True, "str"),
            "reasoning_effort": (True, "str"),
            "request_id": (True, "str"), "run_id": (True, "str"),
            "planner_input_sha256": (True, "str"),
        },
        cmd_exec.cmd_publish_plan,
    ),
    "register-review": (
        {
            "task_id": (True, "str"), "report_path": (True, "str"),
            "conclusion": (True, "str"), "run_id": (False, "str"),
            "review_key": (False, "str"), "fix_task_id": (False, "str"),
            "fix_task_source_sha256": (False, "str"),
            "fix_mode": (False, "str"), "fix": (False, "dict"),
            "repair_plan_path": (False, "str"),
            "kaneo_status": (False, "str"),
            # v4（§5.24.5）：新自动链路必传（无身份的旧调用保持兼容）。
            "request_id": (False, "str"),
            "expected_execution_run_id": (False, "str"),
        },
        cmd_exec.cmd_register_review,
    ),
    "create-review-fix": (
        {
            "source_task_id": (True, "str"), "title": (True, "str"),
            "description": (True, "str"), "priority": (True, "str"),
            "dedupe_key": (True, "str"),
            # v4（§5.24.5）：新自动 Review 的 fix 创建身份与评审周期。
            "request_id": (False, "str"), "run_id": (False, "str"),
            "expected_execution_run_id": (False, "str"),
        },
        cmd_remote.cmd_create_review_fix,
    ),
    "advance-task": (
        {
            "task_id": (True, "str"), "target": (True, "str"),
            "expected_current": (True, "str"), "run_id": (False, "str"),
            # v4（§5.24.5）：新自动 Review 的登记身份（可选；无身份=旧手动兼容）。
            "request_id": (False, "str"),
            "expected_execution_run_id": (False, "str"),
            "evidence_note": (False, "str"),
            # T5-C（§5.23.10）：pi_desktop 轮次进入评审的硬闸材料。
            "monitor_request_id": (False, "str"),
            "monitor_run_id": (False, "str"),
            "evaluation_event_id": (False, "int"),
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
    "archive-task": (
        {"task_id": (True, "str"), "expected_save_state": (False, "str")},
        cmd_archive.cmd_archive_task,
    ),
    "trash-task": (
        {"task_id": (True, "str"), "expected_save_state": (False, "str")},
        cmd_archive.cmd_trash_task,
    ),
    "restore-task": (
        {"task_id": (True, "str"), "resume_auto": (False, "bool")},
        cmd_archive.cmd_restore_task,
    ),
    # v2（§5.20.2）：回收站终态移除（历史/文件保留，不复活）。
    "purge-task": (
        {"task_id": (True, "str"), "expected_save_state": (False, "str")},
        cmd_archive.cmd_purge_task,
    ),
    "list-sessions": (
        {
            "task_id": (False, "str"),
            # 临时覆盖运行记录根目录（不持久化；空串视为未提供）。
            "session_runs_root": (False, "str"),
            # v1.5（protocol §5.16.3）：会话保存状态筛选，缺省 active。
            "save_state": (False, "str"),
            # v2（§5.20.2）：save_state=all 时是否含 purged 回看。
            "include_removed": (False, "bool"),
        },
        cmd_session.cmd_list_sessions,
    ),
    "get-session": (
        {"task_id": (True, "str"), "run_id": (True, "str")},
        cmd_session.cmd_get_session,
    ),
    "session-messages": (
        {
            "task_id": (True, "str"), "run_id": (False, "str"),
            "cursor": (False, "str"), "limit": (False, "int"),
        },
        cmd_session.cmd_session_messages,
    ),
    # v1.5（protocol §5.16.2）：会话回收站。
    "trash-session": (
        {
            "task_id": (True, "str"), "run_id": (True, "str"),
            "expected_save_state": (False, "str"),
        },
        cmd_session.cmd_trash_session,
    ),
    "restore-session": (
        {"task_id": (True, "str"), "run_id": (True, "str")},
        cmd_session.cmd_restore_session,
    ),
    # v2（§5.20.2）：回收站会话终态移除。
    "purge-session": (
        {
            "task_id": (True, "str"), "run_id": (True, "str"),
            "expected_save_state": (False, "str"),
        },
        cmd_session.cmd_purge_session,
    ),
    # v1.6（protocol §5.17）：pi 执行配置与能力查询。
    "get-exec-config": ({}, cmd_config.cmd_get_exec_config),
    "set-exec-config": (
        {
            "provider": (False, "str"), "model": (False, "str"),
            "reasoning_effort": (False, "str"),
            "agent_preset": (False, "str"),
            # v3（§5.23.3）：任务执行后端与 PI-Desktop 分支配置块。
            "backend": (False, "str"),
            "pi_desktop": (False, "dict"),
            # v4（§5.24.1）：dispatch/monitor 控制端后端（缺省 pi）。
            "controller_backend": (False, "str"),
        },
        cmd_config.cmd_set_exec_config,
    ),
    "exec-capabilities": ({}, cmd_config.cmd_exec_capabilities),
    # v3（§5.23.6）：Pi-Desktop-Plus 本地 provider/model 默认目录（只读）。
    "pi-desktop-probe": (
        {"connection_file": (False, "str")},
        lambda ctx, payload: __import__("kanpilot.harness_plus_adapter", fromlist=["plus_capabilities"]).plus_capabilities(
            payload.get("connection_file") or str(Path(constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE).expanduser())
        ),
    ),
    "pi-desktop-catalog": (
        {
            "connection_file": (False, "str"),
            "provider_id": (False, "str"),
        },
        cmd_config.cmd_pi_desktop_catalog,
    ),
    # v4（§5.24.2）：Review 环境的角色分支能力（只读，不调用模型）。
    # 惰性委派 cmd_config.cmd_review_capabilities —— 注册不再要求该属性在导入期
    # 存在（实现落地即生效；缺失时只在真正调用该命令时明确报错）。
    "review-capabilities": (
        {},
        lambda ctx, payload: cmd_config.cmd_review_capabilities(ctx, payload),
    ),
    # v1.6（protocol §5.18.3）：pi 轮次启动。
    "dispatch-start": (
        {"task_id": (True, "str"), "run_id": (True, "str")},
        cmd_dispatch.cmd_dispatch_start,
    ),
    # v3（§5.23.7）：pi_desktop 轮次观察进程续接（只读接管，绝不重发）。
    "round-observe": (
        {
            "task_id": (True, "str"), "run_id": (True, "str"),
            "executor": (False, "str"),
        },
        cmd_dispatch.cmd_round_observe,
    ),
    # v3 T5（§5.23.10）：Goal 报告只读读取与完成判定登记。
    "get-goal-report": (
        {"task_id": (True, "str"), "run_id": (True, "str")},
        cmd_read.cmd_get_goal_report,
    ),
    "record-goal-evaluation": (
        {
            "task_id": (True, "str"),
            "execution_run_id": (True, "str"),
            "monitor_request_id": (True, "str"),
            "monitor_run_id": (True, "str"),
            "plan_sha256": (True, "str"),
            "report_id": (False, "str"),
            "report_sha256": (True, "str"),
            "decision": (True, "str"),
            "criterion_reviews": (True, "list"),
            "reason": (False, "str"),
        },
        cmd_exec.cmd_record_goal_evaluation,
    ),
    # v1.6：执行端状态（App ExecutorScheduler 单飞判断与设置页显示）。
    "executor-status": ({"instance_id": (False, "str")}, cmd_config.cmd_executor_status),
    "executor-instance": ({"instance_id": (True, "str"), "action": (True, "str"),
                           "task_id": (False, "str"), "run_id": (False, "str"),
                           "spec_path": (False, "str")},
                          _automation_command("cmd_config", "cmd_executor_instance")),
    # v2（§5.22）：手动规划链——待办发现 / 材料准备 / 提交规划。
    "fetch-todos": (
        {},
        lambda ctx, _payload: planner_inputs.fetch_todos(ctx.conn),
    ),
    "prepare-plan-input": (
        {
            "task_id": (True, "str"),
            "expected_requirements_sha256": (False, "str"),
            "local_paths": (False, "list"),
            "supplement": (False, "str"),
            "excluded_remote_ids": (False, "list"),
        },
        planner_inputs.prepare_plan_input,
    ),
    "start-plans": (
        {"items": (True, "list")},
        planner_inputs.start_plans,
    ),
    # v2（§5.22.6，规划修复 T2）：Codex 规划配置与能力查询。
    "get-planner-config": ({}, cmd_planner.cmd_get_planner_config),
    "set-planner-config": (
        {
            "standard": (False, "dict"),
            "review_fix": (False, "dict"),
            "default_method": (False, "str"),
            # v4（§5.24.1）：规划后端与 Plus 分支五字段块（缺 backend 按 codex）。
            "backend": (False, "str"),
            "pi_desktop": (False, "dict"), "claude": (False, "dict"),
        },
        cmd_planner.cmd_set_planner_config,
    ),
    "planner-capabilities": ({}, cmd_planner.cmd_planner_capabilities),
    "get-review-config": ({}, cmd_config.cmd_get_review_config),
    "set-review-config": (
        {
            # v4（§5.24.1）：mode 仅在 backend=pi 时必需（Plus 分支用五字段）。
            "mode": (False, "str"),
            "backend": (False, "str"),
            "pi_desktop": (False, "dict"), "claude": (False, "dict"), "codex": (False, "dict"),
            "provider": (False, "str"), "model": (False, "str"),
            "reasoning_effort": (False, "str"),
        },
        cmd_config.cmd_set_review_config,
    ),
    # v5（protocol §5.25 / T7.7）：双向 MCP 领取（事务内原子写入状态与收据）。
    "mcp-claim": (
        {
            "stage": (False, "str"), "executor": (False, "str"),
            "request_id": (False, "str"), "scope_id": (True, "str"),
            "operation_id": (True, "str"), "authorized_task_id": (False, "str"),
        },
        cmd_exec.cmd_mcp_claim,
    ),
    # T1（§5.26）：项目与本地仓库映射配置。
    "get-project-mappings": (
        {"refresh_remote": (False, "bool")},
        lambda ctx, payload: __import__("kanpilot.project_config", fromlist=["cmd_get_project_mappings"]).cmd_get_project_mappings(ctx, payload),
    ),
    "set-project-mapping": (
        {
            "project_id": (True, "str"),
            "repository": (False, "str|null"),
            "enabled": (True, "bool"),
            "expected_revision": (True, "int"),
        },
        lambda ctx, payload: __import__("kanpilot.project_config", fromlist=["cmd_set_project_mapping"]).cmd_set_project_mapping(ctx, payload),
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
    def __init__(self, db_path: Path, allow_upgrade: bool = False):
        self.db_path = db_path
        self.conn = db.connect(db_path, allow_upgrade=allow_upgrade)
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
    allow_upgrade = (command == "migrate")
    ctx = Context(db_path, allow_upgrade=allow_upgrade)
    try:
        return COMMANDS[command][1](ctx, payload)
    finally:
        ctx.close()


def _handle_mcp_serve(flags: dict, db_path_str: Optional[str]) -> int:
    import threading
    import time
    transport = str(flags.get("transport", "http"))
    conn_file = Path(flags["connection_file"]).expanduser() if flags.get("connection_file") else None
    scope_file = Path(flags["scope_file"]).expanduser() if flags.get("scope_file") else None
    db_path = resolve_db_path(db_path_str or flags.get("db"))

    if transport == "stdio":
        from . import mcp_stdio
        return mcp_stdio.run_stdio_bridge(connection_file=conn_file, scope_file=scope_file)

    import os
    import signal
    from . import mcp_server

    host = str(flags.get("host", "127.0.0.1"))
    try:
        port = int(flags.get("port", 0))
    except (ValueError, TypeError):
        port = 0

    srv = mcp_server.MCPServer(
        db_path=db_path,
        connection_file=conn_file,
        host=host,
        port=port,
    )
    srv.start()
    print(
        json.dumps({
            "ok": True,
            "server_name": "kaneopilot",
            "url": srv.server_url,
            "pid": os.getpid(),
            "epoch": srv.epoch,
            "active": True,
        }, ensure_ascii=False)
    )
    sys.stdout.flush()

    stop_event = threading.Event()

    def _sig_handler(signum: Any, frame: Any) -> None:
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except ValueError:
        # Not in main thread (e.g. testing)
        pass

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        srv.stop()
    return 0


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

    if args.command == "mcp-serve":
        try:
            return _handle_mcp_serve(flags, args.db_path)
        except util.KanpilotError as exc:
            print(util.jdump({"code": exc.code, "error": exc.message}))
            return 1
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
