"""get/set/exec-capabilities 命令层（protocol.md §5.17 v1.6 +
§5.23.3/§5.23.4 v3 + §5.24.1/§5.24.2 v4）。

配置对象与能力查询的实现（load/save/snapshot/capabilities/validate_config/
check_available/controller_*）在 exec_config.py；Review 环境读写、校验与
``review-capabilities`` 在 review_config.py；本模块只做命令层：payload 校验、
调用、事件写入与响应组装。凭据永不输出值（credential check 只查 pi auth check
退出码 / PI-Desktop 连接文件 token 的内存就绪性）。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import (
    constants, db, exec_config, pi_desktop_backend, review_config, util,
)

# set-exec-config 允许的字段：契约四字段 + v3 后端扩展 + v4 控制器后端。
_SET_FIELDS = (
    constants.EXEC_CONFIG_KEYS
    + ("backend", "pi_desktop", constants.CONTROLLER_BACKEND_KEY)
)
_PI_DESKTOP_THINKING_LEVELS = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)


def cmd_get_exec_config(ctx, payload: dict) -> dict:
    """get-exec-config {} → {"config": {...}|null, "defaults": {...}}。
    config=null 表示从未保存；defaults 恒为初始默认；config 存在时含
    backend/pi_desktop/updated_at。"""
    stored = db.get_setting(ctx.conn, "exec_config")
    config = exec_config.load(ctx.conn) if isinstance(stored, dict) else None
    return {"config": config, "defaults": dict(constants.EXEC_CONFIG_DEFAULTS)}


def cmd_set_exec_config(ctx, payload: dict) -> dict:
    """set-exec-config {provider?, model?, reasoning_effort?, agent_preset?,
    backend?, controller_backend?, pi_desktop?}（至少一个字段）。校验/保存经
    ``exec_config.save``——当前生效 backend/controller_backend 的必填组合整体
    校验，失败 bad_request + 中文原因（不静默降级；仅空转重试可使用
    预先配置的 pi_desktop.fallback，不自动更换其他模型/agent/后端）。
    成功写 event exec_config_updated（detail 脱敏：pi 分支四字段 +
    controller_backend；pi_desktop 分支五字段含 connection_file 路径——token
    绝不记）→ {"config": {...}}。"""
    fields = {key: payload[key] for key in _SET_FIELDS if key in payload}
    if not fields:
        raise util.bad_request(
            "set-exec-config 至少提供一个字段"
            "（provider / model / reasoning_effort / agent_preset / "
            "backend / controller_backend / pi_desktop）"
        )
    config = exec_config.save(ctx.conn, fields, ctx.now_iso)
    db.add_event(
        ctx.conn, ctx.now_iso, "exec_config_updated", actor="user",
        detail=_exec_config_event_detail(config),
    )
    return {"config": config}


def _exec_config_event_detail(config: dict) -> dict:
    """事件 detail 脱敏快照：pi 分支沿用契约四字段 + ``controller_backend``
    （v4 扩展，保留旧字段）；pi_desktop 分支记 backend + controller_backend +
    五字段（connection_file 为普通路径可记，token 绝不记）。"""
    controller = (
        config.get(constants.CONTROLLER_BACKEND_KEY) or "pi"
    )
    if config.get("backend") == "pi_desktop":
        block = config.get("pi_desktop") or {}
        return {
            "backend": "pi_desktop",
            constants.CONTROLLER_BACKEND_KEY: controller,
            **{key: block.get(key)
               for key in constants.EXEC_PI_DESKTOP_KEYS},
            "fallback": block.get("fallback"),
        }
    return {
        **{key: config[key] for key in constants.EXEC_CONFIG_KEYS},
        constants.CONTROLLER_BACKEND_KEY: controller,
    }


def cmd_exec_capabilities(ctx, payload: dict) -> dict:
    """exec-capabilities {} → protocol §5.17.2 形状 + §5.23.3 pi_desktop
    分支就绪态（剔除内部 _providers_detail 字段——其中含 has_api_key
    等内部字段；checks 的 credential detail 只含变量名 + present/missing；
    pi_desktop 分支不含 token/连接文件全文）。"""
    caps = exec_config.capabilities(ctx.conn)
    caps.pop("_providers_detail", None)
    return caps


def cmd_pi_desktop_catalog(ctx, payload: dict) -> dict:
    """Read the local Pi-Desktop-Plus provider/model catalog (protocol §5.23.6).

    Every operation in this command is read-only.  In particular, model
    discovery is restricted to the Plus cache and never creates a session or
    asks the provider for a remote catalog.
    """
    payload = payload or {}
    connection_file = payload.get("connection_file")
    if connection_file is None or connection_file == "":
        connection_file = str(
            Path(os.path.expanduser("~/.pi-desktop-plus/mcp-control.json"))
        )
    if not isinstance(connection_file, str) or not connection_file:
        raise util.bad_request("connection_file 必须为绝对路径")
    connection_path = Path(connection_file).expanduser()
    if not connection_path.is_absolute():
        raise util.bad_request("connection_file 必须为绝对路径")
    provider_id = payload.get("provider_id")
    if provider_id is not None and (
        not isinstance(provider_id, str) or not provider_id
    ):
        raise util.bad_request("provider_id 必须为非空字符串")

    try:
        record = pi_desktop_backend.load_connection(str(connection_path))
        client = pi_desktop_backend.MCPClient(record["url"], record["token"])
        client.connect()
        if client.protocol_version not in pi_desktop_backend.MCP_COMPATIBLE_VERSIONS:
            raise util.KanpilotError(
                "validation_failed",
                f"PI-Desktop MCP 协议版本不受支持：{client.protocol_version or '未知'}",
            )
        tools = client.tools_list()
        if "pi_desktop_invoke" not in tools:
            raise util.KanpilotError(
                "not_found", "PI-Desktop 控制面缺少 pi_desktop_invoke 工具"
            )
        providers_result = client.call_tool(
            "pi_desktop_invoke",
            {"operation": "providers/list", "args": []},
        )
        providers_result = pi_desktop_backend._unwrap_invoke_result(
            providers_result
        )
        settings_result = client.call_tool(
            "pi_desktop_invoke",
            {"operation": "settings/get", "args": []},
        )
        settings_result = pi_desktop_backend._unwrap_invoke_result(
            settings_result
        )
        rows = providers_result.get("providers")
        if not isinstance(rows, list):
            raise util.KanpilotError("internal", "PI-Desktop provider 目录格式无效")
        settings = settings_result.get("settings", settings_result)
        if not isinstance(settings, dict):
            raise util.KanpilotError("internal", "PI-Desktop 设置目录格式无效")

        def value(obj, *keys):
            for key in keys:
                if key in obj:
                    return obj[key]
            return None

        def string_or_none(value):
            return value if isinstance(value, str) else None

        enabled = []
        response_providers = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            is_enabled = row.get("enabled") is not False and row.get("disabled") is not True
            item = {
                "id": row["id"],
                "name": string_or_none(value(row, "name", "displayName")) or row["id"],
                "enabled": is_enabled,
                "default_model_id": string_or_none(
                    value(row, "defaultModelId", "default_model_id")
                ),
                "models": [],
            }
            response_providers.append((row, item))
            if is_enabled:
                enabled.append((row, item))

        settings_default_provider = value(
            settings, "defaultProviderId", "default_provider_id"
        )
        settings_default_model = value(
            settings, "defaultModelId", "default_model_id"
        )
        settings_has_provider = any(
            key in settings for key in ("defaultProviderId", "default_provider_id")
        )
        default_provider_id = string_or_none(settings_default_provider)
        default_model_id = string_or_none(settings_default_model)
        selected = None
        if provider_id is not None:
            selected = next((entry for entry in enabled if entry[1]["id"] == provider_id), None)
            if selected is None:
                raise util.KanpilotError(
                    "validation_failed", f"PI-Desktop provider 不存在或已禁用：{provider_id}"
                )
        elif default_provider_id:
            selected = next(
                (entry for entry in enabled if entry[1]["id"] == default_provider_id),
                None,
            )
        elif not settings_has_provider and len(enabled) == 1:
            selected = enabled[0]

        catalog_source = None
        if selected is not None:
            selected_row, selected_item = selected
            models_result = client.call_tool(
                "pi_desktop_invoke",
                {
                    "operation": "providers/listModels",
                    "args": [{"providerId": selected_item["id"], "source": "cache"}],
                },
            )
            models_result = pi_desktop_backend._unwrap_invoke_result(models_result)
            catalog_source = models_result.get("source")
            if catalog_source not in ("cache", "fallback"):
                raise util.KanpilotError(
                    "validation_failed",
                    "PI-Desktop model 目录不是允许的本地 cache/fallback 来源",
                )
            model_rows = models_result.get("models")
            if not isinstance(model_rows, list):
                raise util.KanpilotError("internal", "PI-Desktop model 目录格式无效")
            provider_models = selected_row.get("models")
            if not isinstance(provider_models, list):
                provider_models = []
            provider_levels = value(
                selected_row, "supportedThinkingLevels", "supported_thinking_levels"
            )
            for model_row in model_rows:
                if not isinstance(model_row, dict):
                    continue
                model_id = value(model_row, "modelId", "id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                configured = next(
                    (m for m in provider_models if isinstance(m, dict)
                     and value(m, "modelId", "id") == model_id),
                    None,
                )
                if configured is None:
                    configured = (
                        selected_row
                        if model_id == value(
                            selected_row, "defaultModelId", "default_model_id"
                        )
                        else {}
                    )
                levels = value(configured, "thinkingLevels", "thinking_levels")
                if not isinstance(levels, list) or not levels:
                    levels = value(model_row, "supportedThinkingLevels", "supported_thinking_levels")
                if not isinstance(levels, list) or not levels:
                    levels = provider_levels
                if not isinstance(levels, list):
                    levels = []
                normalized_levels = [
                    x for x in levels
                    if isinstance(x, str) and x in _PI_DESKTOP_THINKING_LEVELS
                ]
                default_thinking = string_or_none(value(
                    configured, "defaultThinkingLevel", "default_thinking_level"
                ))
                if default_thinking not in _PI_DESKTOP_THINKING_LEVELS:
                    default_thinking = None
                selected_item["models"].append({
                    "id": model_id,
                    "name": string_or_none(
                        value(model_row, "name", "displayName")
                    ) or model_id,
                    "thinking_levels": normalized_levels,
                    "default_thinking_level": default_thinking,
                })

        # Resolve Plus default for preview & UI inherit mode
        resolved_default = None
        default_detail = None
        def fetch_models_for_default(pid: str):
            if selected is not None and selected[1]["id"] == pid:
                return models_result, None
            try:
                res = client.call_tool(
                    "pi_desktop_invoke",
                    {"operation": "providers/listModels", "args": [{"providerId": pid, "source": "cache"}]},
                )
                return res, None
            except Exception as e:
                return None, str(e)

        resolved, resolve_err = pi_desktop_backend.resolve_default_from_catalog_data(
            str(connection_path),
            rows,
            settings,
            models_fetcher=fetch_models_for_default,
        )
        if resolved is not None:
            resolved_default = {
                "provider_id": resolved["provider_id"],
                "model_id": resolved["model_id"],
                "thinking_level": resolved["thinking_level"],
            }
        else:
            default_detail = resolve_err
        return {
            "connection_file": str(connection_path),
            "providers": [item for _row, item in response_providers],
            "default_provider_id": default_provider_id,
            "default_model_id": default_model_id,
            "selected_provider_id": selected[1]["id"] if selected else None,
            "catalog_source": catalog_source,
            "resolved_default": resolved_default,
            "default_detail": default_detail,
        }
    except util.KanpilotError as exc:
        raise util.KanpilotError(
            exc.code,
            pi_desktop_backend._strip_secret(exc.message, record.get("token") if "record" in locals() else None),
        ) from exc


def cmd_get_review_config(ctx, payload: dict) -> dict:
    """get-review-config {} → {"config": {...}|null, "defaults": {"mode":
    "inherit"}}（§5.23.4）。config=null 表示从未保存——review 控制端
    继承本机 PI 默认会话配置。"""
    stored = db.get_setting(ctx.conn, "review_config")
    config = review_config.load(ctx.conn) if isinstance(stored, dict) else None
    return {"config": config, "defaults": dict(review_config.DEFAULTS)}


def cmd_set_review_config(ctx, payload: dict) -> dict:
    """set-review-config {backend?, mode?, provider?, model?, reasoning_effort?,
    pi_desktop?}（§5.23.4 + §5.24.1）。``backend=pi``（缺省）时 mode=inherit
    忽略其余字段、mode=custom 三字段必填并经 exec-capabilities 校验（同
    §5.17.2）；``backend=pi_desktop`` 时 ``mode`` 不参与，要求五字段完整且
    permission_mode=ask。失败 bad_request 旧值不变；成功原子保存 + event
    review_config_updated（detail 脱敏快照，无 token）→ {"config": {...}}。
    仅影响下一次 review 控制端启动。"""
    fields = {
        key: payload[key]
        for key in ("mode", "backend", "pi_desktop", "claude", "codex") + review_config.CUSTOM_KEYS
        if key in payload
    }
    if not fields:
        raise util.bad_request(
            "set-review-config 至少提供一个字段"
            "（mode / backend / pi_desktop / provider / model / "
            "reasoning_effort）"
        )
    config = review_config.save(ctx.conn, fields, ctx.now_iso)
    if config.get("backend") == "pi_desktop":
        # Configuration audit stores the saved redacted values. Runtime
        # snapshots still resolve inherit defaults before a review starts.
        block = config.get("pi_desktop") or {}
        detail = {
            "backend": "pi_desktop",
            **{key: block.get(key)
               for key in constants.EXEC_PI_DESKTOP_KEYS},
        }
    else:
        detail = review_config.snapshot(ctx.conn)
    db.add_event(
        ctx.conn, ctx.now_iso, "review_config_updated", actor="user",
        detail=detail,
    )
    return {"config": config}


def cmd_review_capabilities(ctx, payload: dict) -> dict:
    """review-capabilities {} → {pi:{available,detail},
    pi_desktop:{available,detail,tools_ok,version}}（§5.24.2，新增只读命令）。

    只读本机配置与 Plus 连接状态：不写 settings、不发模型请求、不返回凭据；
    pi 分支=PI 环境检查（custom 时另验目标 provider/model/effort 在目录内），
    pi_desktop 分支=五字段 + 连接 + 基础工具 + 契约审批工具。``available``
    只代表本机配置与协议工具可用，不证明凭据有效或模型在线。"""
    return review_config.capabilities(ctx.conn)


def cmd_executor_status(ctx, payload: dict) -> dict:
    """executor-status {} → 四个 stage 的在途执行端状态。

    ExecutorScheduler 拉起前先调本命令判断单飞：``running`` 非空表示该
    stage 有在途 executor 进程（按 pid 存活探测）。返回各 stage 最近
    一次运行的 stage/pid/started_at/log（来自 settings ``executor_runs``，
    由 App 侧拉起时写入；进程内不另起跟踪）。
    """
    runs = db.get_setting(ctx.conn, "executor_runs")
    result = {"stages": {}}
    if not isinstance(runs, dict):
        runs = {}
    for stage in constants.STAGES:
        entry = runs.get(stage)
        if not isinstance(entry, dict):
            result["stages"][stage] = {"running": None, "last": None}
            continue
        pid = entry.get("pid")
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError):
                alive = False
            except Exception:
                alive = False
        result["stages"][stage] = {
            "running": entry if alive else None,
            "last": entry,
        }
    from . import core, cmd_exec, automation_policy, project_config
    for stage in constants.STAGES:
        rows = ctx.conn.execute(
            "SELECT r.* FROM requests r LEFT JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.stage=? AND r.status='queued' AND (r.not_before IS NULL OR r.not_before<=?) "
            "AND (r.task_id IS NULL OR (t.save_state='active' AND t.auto_advance_paused=0))",
            (stage, ctx.now_iso)).fetchall()
        eligible = []
        for row in rows:
            if row["kind"] == "plan" and not db.request_payload(row).get("manual_authorized"):
                continue
            task = db.get_task(ctx.conn, row["task_id"]) if row["task_id"] else None
            if row["kind"] == "dispatch" and task is not None and db.task_extra(task).get("plan_execution_hold"):
                continue
            eligible.append(row)
        result["stages"][stage]["claimable"] = len(eligible)
    intake = runs.get("intake") or {}
    pid = intake.get("pid")
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    result["intake"] = {"running": intake if alive else None, "last": intake or None}
    result["paused"] = {"global": db.get_setting(ctx.conn, "paused_global", False),
                        "stages": db.get_setting(ctx.conn, "paused_stages", []) or []}
    _, _, items, _ = project_config.load_raw_mappings(ctx.conn)
    managed = [t for t in ctx.conn.execute("SELECT * FROM tasks").fetchall()
               if core.is_automation_managed(t) and db.task_schedulable(t)]
    waiting = [t for t in managed if t["status"] == "in_review" and db.task_extra(t).get("review_conclusion") == "pass"]
    result["automation"] = {
        "enabled_projects": sum(bool((v.get("automation") or {}).get("enabled")) for v in items.values()),
        "active_tasks": sum(t["status"] not in ("done", "needs_user") and t not in waiting for t in managed),
        "max_active_tasks": automation_policy.global_settings(ctx.conn)["max_active_tasks"],
        "waiting_merge": len(waiting),
        "pending_tasks": ctx.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='needs_user' AND save_state='active'").fetchone()[0], "last_intake": db.get_setting(ctx.conn, "automation_last_intake"),
    }
    if "instance_id" in payload:
        from . import process_owner
        result["owned_workers"] = process_owner.owned_workers(ctx.conn, payload["instance_id"])
    return result


def cmd_executor_instance(ctx, payload: dict) -> dict:
    """App-owned detached worker registration, without touching Plus sessions."""
    from . import process_owner
    action = payload.get("action")
    if action == "status":
        return process_owner.instance_status(ctx.conn, payload.get("instance_id"))
    if action == "register-worker":
        return process_owner.register_worker(ctx, payload)
    return process_owner.set_instance(ctx, payload.get("instance_id"), action)
