"""get/set/exec-capabilities（protocol.md §5.17，v1.6）。

OWNERSHIP: T3 subagent（C）。配置对象与能力查询的实现（load/save/
capabilities/validate_config/check_available）在 exec_config.py（主 agent 冻结，
只读）；本模块只做命令层：payload 校验、调用、事件写入与响应组装。
凭据永不输出值（credential check 只查 pi auth check 退出码）。
"""

from __future__ import annotations

import os

from . import constants, db, exec_config, util


def cmd_get_exec_config(ctx, payload: dict) -> dict:
    """get-exec-config {} → {"config": {...}|null, "defaults": {...}}。
    config=null 表示从未保存；defaults 恒为初始默认；config 存在时含
    updated_at。"""
    stored = db.get_setting(ctx.conn, "exec_config")
    config = exec_config.load(ctx.conn) if isinstance(stored, dict) else None
    return {"config": config, "defaults": dict(constants.EXEC_CONFIG_DEFAULTS)}


def cmd_set_exec_config(ctx, payload: dict) -> dict:
    """set-exec-config {provider?, model?, reasoning_effort?, agent_preset?}
    合并保存（已存值 > 默认值，本次 payload 覆盖）；至少一个字段；经
    capabilities + validate_config 校验，失败 bad_request + 中文原因（不静默
    降级、不自动换模型/agent）。成功写 event exec_config_updated（detail
    仅四字段，不含凭据）→ {"config": {...}}。"""
    fields = [key for key in constants.EXEC_CONFIG_KEYS if key in payload]
    if not fields:
        raise util.bad_request(
            "set-exec-config 至少提供一个字段"
            "（provider / model / reasoning_effort / agent_preset）"
        )
    caps = exec_config.capabilities(ctx.conn)
    current = exec_config.load(ctx.conn)
    candidate = {key: current[key] for key in constants.EXEC_CONFIG_KEYS}
    for key in fields:
        candidate[key] = payload[key]
    error = exec_config.validate_config(candidate, caps)
    if error is not None:
        raise util.bad_request(f"执行配置校验失败：{error}")
    config = exec_config.save(ctx.conn, candidate, ctx.now_iso)
    db.add_event(
        ctx.conn, ctx.now_iso, "exec_config_updated", actor="user",
        detail={key: config[key] for key in constants.EXEC_CONFIG_KEYS},
    )
    return {"config": config}


def cmd_exec_capabilities(ctx, payload: dict) -> dict:
    """exec-capabilities {} → protocol §5.17.2 形状（剔除内部
    _providers_detail 字段——其中含 has_api_key 等内部字段；checks 的
    credential detail 只含变量名 + present/missing）。"""
    caps = exec_config.capabilities(ctx.conn)
    caps.pop("_providers_detail", None)
    return caps


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
