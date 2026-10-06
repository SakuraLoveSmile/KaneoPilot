"""get/set/exec-capabilities 命令层（protocol.md §5.17 v1.6 + §5.23.3/§5.23.4 v3）。

配置对象与能力查询的实现（load/save/snapshot/capabilities/validate_config/
check_available）在 exec_config.py；Review 环境读写与校验在
review_config.py；本模块只做命令层：payload 校验、调用、事件写入与响应组装。
凭据永不输出值（credential check 只查 pi auth check 退出码 /
PI-Desktop 连接文件 token 的内存就绪性）。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import (
    constants, db, exec_config, pi_desktop_backend, review_config, util,
)

# set-exec-config 允许的字段：契约四字段 + v3 后端扩展。
_SET_FIELDS = constants.EXEC_CONFIG_KEYS + ("backend", "pi_desktop")
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
    backend?, pi_desktop?}（至少一个字段）。校验/保存经
    ``exec_config.save``——当前生效 backend 的必填组合整体校验，失败
    bad_request + 中文原因（不静默降级、不自动换模型/agent/后端）。
    成功写 event exec_config_updated（detail 脱敏：pi 分支四字段；
    pi_desktop 分支五字段含 connection_file 路径——token 绝不记）
    → {"config": {...}}。"""
    fields = {key: payload[key] for key in _SET_FIELDS if key in payload}
    if not fields:
        raise util.bad_request(
            "set-exec-config 至少提供一个字段"
            "（provider / model / reasoning_effort / agent_preset / "
            "backend / pi_desktop）"
        )
    config = exec_config.save(ctx.conn, fields, ctx.now_iso)
    db.add_event(
        ctx.conn, ctx.now_iso, "exec_config_updated", actor="user",
        detail=_exec_config_event_detail(config),
    )
    return {"config": config}


def _exec_config_event_detail(config: dict) -> dict:
    """事件 detail 脱敏快照：pi 分支沿用四字段（历史事件形状不变）；
    pi_desktop 分支记 backend + 五字段（connection_file 为普通路径可记，
    token 绝不记）。"""
    if config.get("backend") == "pi_desktop":
        block = config.get("pi_desktop") or {}
        return {
            "backend": "pi_desktop",
            **{key: block.get(key)
               for key in constants.EXEC_PI_DESKTOP_KEYS},
        }
    return {key: config[key] for key in constants.EXEC_CONFIG_KEYS}


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

        return {
            "connection_file": str(connection_path),
            "providers": [item for _row, item in response_providers],
            "default_provider_id": default_provider_id,
            "default_model_id": default_model_id,
            "selected_provider_id": selected[1]["id"] if selected else None,
            "catalog_source": catalog_source,
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
    """set-review-config {mode, provider?, model?, reasoning_effort?}
    （§5.23.4）。mode=inherit 忽略其余字段；mode=custom 时三字段必填
    并经 exec-capabilities 校验（同 §5.17.2）。失败 bad_request 旧值
    不变；成功原子保存 + event review_config_updated（detail 脱敏快照）
    → {"config": {...}}。仅影响下一次 review 控制端启动。"""
    config = review_config.save(ctx.conn, payload, ctx.now_iso)
    db.add_event(
        ctx.conn, ctx.now_iso, "review_config_updated", actor="user",
        detail=review_config.snapshot(ctx.conn),
    )
    return {"config": config}


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
    for stage in ("plan", "dispatch", "monitor", "review"):
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
    return result
