"""Review 环境配置（protocol.md §5.23.4，v3）。

独立 settings 键 ``review_config``——review 控制端（``executor_runner
--stage review``）的模型来源；不复用任务执行 ``exec_config`` 的
agent_preset，也不影响 dispatch/monitor 控制端与规划环境（评审修复
计划仍走 planner_config）。

- ``mode="inherit"``（缺省/未保存）：继承本机 PI 默认会话配置——保持
  当前实际行为。
- ``mode="custom"``：固化 {provider, model, reasoning_effort}，保存时按
  ``exec-capabilities`` 校验（同 §5.17.2 的 provider/model/effort 规则），
  生效点为下一次 review 控制端启动（executor_runner 加 --model/--thinking）。
- snapshot：写入运行记录/事件的脱敏快照（不含 updated_at，不含凭据）。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import db, exec_config, util

# settings.review_config 形状：{mode, provider?, model?, reasoning_effort?,
# updated_at}。inherit 时忽略三字段；custom 时三字段必填且有效。
MODES = ("inherit", "custom")
CUSTOM_KEYS = ("provider", "model", "reasoning_effort")
DEFAULTS = {"mode": "inherit"}


def load(conn: sqlite3.Connection) -> Optional[dict]:
    """已保存配置（含 updated_at）；未保存/损坏 → None（按 inherit 解释）。"""
    stored = db.get_setting(conn, "review_config")
    if not isinstance(stored, dict):
        return None
    mode = stored.get("mode")
    if mode not in MODES:
        return None
    config = {"mode": mode}
    if mode == "custom":
        for key in CUSTOM_KEYS:
            value = stored.get(key)
            if isinstance(value, str) and value:
                config[key] = value
    if isinstance(stored.get("updated_at"), str):
        config["updated_at"] = stored["updated_at"]
    return config


def snapshot(conn: sqlite3.Connection) -> dict:
    """脱敏快照（运行记录/事件 detail 用）：生效配置，不含 updated_at。
    未保存 → {"mode": "inherit"}。"""
    config = load(conn) or dict(DEFAULTS)
    snap = {"mode": config["mode"]}
    for key in CUSTOM_KEYS:
        if key in config:
            snap[key] = config[key]
    return snap


def validate(config: dict, caps: dict) -> Optional[str]:
    """保存校验（§5.23.4）：mode 必须 inherit|custom；custom 时
    provider/model/reasoning_effort 必填非空字符串，且经
    exec-capabilities 校验（复用 §5.17.2 provider/model/effort 规则，
    agent_preset 以 "default" 占位参与校验）。返回中文错误原因。"""
    if not isinstance(config, dict):
        return "配置必须是对象"
    mode = config.get("mode")
    if mode not in MODES:
        return f"mode 不支持：{mode}（可用：{', '.join(MODES)}）"
    if mode == "inherit":
        return None
    missing = [
        key for key in CUSTOM_KEYS
        if not isinstance(config.get(key), str) or not config[key]
    ]
    if missing:
        return f"custom 模式缺少必填字段：{', '.join(missing)}"
    pseudo = {
        key: config[key] for key in CUSTOM_KEYS
    }
    pseudo["agent_preset"] = "default"
    return exec_config.validate_config(pseudo, caps)


def save(conn: sqlite3.Connection, fields: dict, now: str) -> dict:
    """校验并原子保存（§5.23.4）。

    fields={mode, provider?, model?, reasoning_effort?}；inherit 忽略其余
    字段；custom 经 exec-capabilities 校验。失败 raise bad_request（中文
    原因，旧值不变）；成功写 settings.review_config + updated_at，返回
    完整生效值。"""
    if not isinstance(fields, dict):
        raise util.bad_request("review_config 必须是对象")
    mode = fields.get("mode")
    if mode not in MODES:
        raise util.bad_request(
            f"Review 配置校验失败：mode 不支持：{mode}"
            f"（可用：{', '.join(MODES)}）"
        )
    record = {"mode": mode}
    if mode == "custom":
        candidate = {"mode": "custom"}
        candidate.update(
            {key: fields.get(key) for key in CUSTOM_KEYS}
        )
        error = validate(candidate, exec_config.capabilities(conn))
        if error is not None:
            raise util.bad_request(f"Review 配置校验失败：{error}")
        record.update({key: candidate[key] for key in CUSTOM_KEYS})
    record["updated_at"] = now
    db.set_setting(conn, "review_config", record)
    return record
