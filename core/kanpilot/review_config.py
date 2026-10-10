"""Review 环境配置（protocol.md §5.23.4 v3 + §5.24.1/§5.24.2 v4）。

独立 settings 键 ``review_config``——review 控制端（``executor_runner
--stage review``）模型来源；不复用任务执行 ``exec_config`` 的 agent_preset，
也不影响 dispatch/monitor 控制端与规划环境（评审修复计划仍走 planner_config）。

- ``backend="pi"``（缺省/未保存）：沿用 §5.23.4 的 pi 分支——
  ``mode="inherit"`` 继承本机 PI 默认会话配置；``mode="custom"`` 固化
  {provider, model, reasoning_effort}，保存时按 ``exec-capabilities`` 校验
  （同 §5.17.2），生效点为下一次 review 控制端启动。
- ``backend="pi_desktop"``（v4）：使用 ``review_config.pi_desktop`` 五字段，
  新会话为 ``mode=plan`` + ``permission_mode=auto``；审查职责与契约边界不变，
  ``mode`` 不参与生效（仅属 pi 分支，切换后端不删除）。
- ``backend="claude"|"codex"``（v5）：分别保存 model/effort 与
  model/reasoning_effort；只读 CLI 使用本机认证配置，保存时不调用模型。
- 各分支共存：切换后端只改 ``backend``，不删除其他分支已存字段；服务离线
  也能保存（结构/枚举校验，不探测服务）。
- snapshot：写入运行记录/事件的脱敏快照——按**生效分支**输出，不含
  updated_at、不含 token/连接文件全文。
- capabilities：``review-capabilities`` 的只读分支就绪态（§5.24.2）：
  ``{pi:{available,detail}, pi_desktop:{available,detail,tools_ok,version}}``；
  ``available=true`` 只代表本机配置与协议工具可用，不证明凭据有效或模型在线。
"""

from __future__ import annotations

import sqlite3
import shutil
from copy import deepcopy
from typing import Optional

from . import constants, db, exec_config, util, planner_config

# settings.review_config 形状：{backend, mode, provider?, model?,
# reasoning_effort?, pi_desktop?, updated_at}。inherit 时忽略三字段；custom 时
# 三字段必填且有效；pi_desktop 分支忽略 mode、要求五字段完整（permission_mode
# 固定 auto）。
MODES = ("inherit", "custom")
CUSTOM_KEYS = ("provider", "model", "reasoning_effort")
BACKENDS = constants.REVIEW_BACKENDS
DEFAULT_BACKEND = constants.REVIEW_BACKEND
# get-review-config 的 defaults 形状冻结为 {"mode": "inherit"}（§5.23.4）；
# backend 的缺省解释是 pi（§5.24.1），不在此处扩展以免改动冻结响应。
DEFAULTS = {"mode": "inherit"}
CLI_DEFAULTS = {"claude": {"model": "sonnet", "effort": "high"},
                "codex": {"model": constants.REVIEW_FIX_PLANNER["planner_model"],
                          "reasoning_effort": constants.REVIEW_FIX_PLANNER["reasoning_effort"]}}
CLI_KEYS = {"claude": ("model", "effort"), "codex": ("model", "reasoning_effort")}

def cli_block(config, backend):
    stored = (config or {}).get(backend)
    return {key: (stored.get(key, CLI_DEFAULTS[backend][key]) if isinstance(stored, dict)
                  else CLI_DEFAULTS[backend][key]) for key in CLI_KEYS[backend]}


def _validate_cli(block, backend):
    if not isinstance(block, dict):
        return f"{backend} 必须是对象"
    if backend == "claude":
        return planner_config.validate_claude_block(block)
    return planner_config.validate_group(
        {"planner_model": block.get("model"), "reasoning_effort": block.get("reasoning_effort")}, None)

# Plus Review 新轮次固定 auto；既有轮次继续使用其固化权限快照。
PI_DESKTOP_PERMISSION_MODE = constants.PI_DESKTOP_REVIEW_PERMISSION_MODE


def backend(conn: sqlite3.Connection) -> str:
    """生效的 Review 后端（§5.24.1）：pi（缺省）或 pi_desktop。"""
    stored = db.get_setting(conn, "review_config")
    return _backend(stored.get("backend") if isinstance(stored, dict) else None)


def _backend(value) -> str:
    """Review 后端规范化：缺省/未知值 → "pi"（旧数据零迁移）。"""
    return value if value in BACKENDS else DEFAULT_BACKEND


def load(conn: sqlite3.Connection) -> Optional[dict]:
    """已保存配置（含 updated_at）；从未保存/空值 → None。

    v4 形状：``{backend, mode, provider?, model?, reasoning_effort?,
    pi_desktop?}``。``mode`` 非法/缺失按 ``inherit`` 读（不因 pi 分支字段损坏
    丢掉整个 Plus 分支）；``pi_desktop`` 只保留契约五字段的非空字符串。
    """
    stored = db.get_setting(conn, "review_config")
    if not isinstance(stored, dict) or not stored:
        return None
    resolved = _backend(stored.get("backend"))
    mode = stored.get("mode")
    config = {
        "backend": resolved,
        "mode": mode if mode in MODES else "inherit",
    }
    if config["mode"] == "custom":
        for key in CUSTOM_KEYS:
            value = stored.get(key)
            if isinstance(value, str) and value:
                config[key] = value
    config["pi_desktop"] = exec_config.merge_pi_desktop_block(
        stored.get("pi_desktop")
    )
    if config["pi_desktop"] is not None:
        # 投影下一轮的生效权限，不写回旧设置，也不修改历史请求/轮次。
        config["pi_desktop"]["permission_mode"] = PI_DESKTOP_PERMISSION_MODE
    for name in CLI_KEYS:
        config[name] = cli_block(stored, name)
    if isinstance(stored.get("updated_at"), str):
        config["updated_at"] = stored["updated_at"]
    return config


def resolve_runtime_config(
    conn: sqlite3.Connection,
    block: Optional[dict] = None,
    timeout: int = 10,
) -> tuple:
    """解析新轮次的脱敏 5 字段快照（permission_mode 固定 auto）。"""
    if block is None:
        cfg = load(conn) or {}
        block = cfg.get("pi_desktop")
    if not isinstance(block, dict) or not block:
        return None, "Plus Review 分支配置未保存"
    b = dict(block)
    b["permission_mode"] = PI_DESKTOP_PERMISSION_MODE
    return exec_config.resolve_runtime_config(conn, b, timeout=timeout)


def queue_snapshot(conn: sqlite3.Connection) -> dict:
    """新请求的纯本地配置；Plus 默认模型留到 runner 启动时解析。"""
    config = deepcopy(load(conn) or {"backend": DEFAULT_BACKEND, "mode": "inherit"})
    config.pop("updated_at", None)
    return config


def for_request(conn: sqlite3.Connection, payload: dict) -> dict:
    """使用请求快照；旧请求在首次切 Desktop 时保留原配置归属。"""
    frozen = payload.get("review_config")
    if isinstance(frozen, dict):
        return deepcopy(frozen)
    stored = db.get_setting(conn, "review_config") or {}
    legacy = stored.get("legacy_config")
    if isinstance(legacy, dict):
        return deepcopy(legacy)
    current = queue_snapshot(conn)
    if current["backend"] == "codex_desktop":
        return {"backend": DEFAULT_BACKEND, "mode": "inherit"}
    return current


def backend_for_request(conn: sqlite3.Connection, payload: dict) -> str:
    explicit = payload.get("review_backend")
    if explicit in BACKENDS:
        return explicit
    return _backend(for_request(conn, payload).get("backend"))


def snapshot(conn: sqlite3.Connection, config_override: Optional[dict] = None) -> dict:
    """脱敏快照（运行记录/事件 detail 用）：按生效分支输出，不含 updated_at。

    - pi → ``{backend:"pi", mode, provider?, model?, reasoning_effort?}``
      （custom 才带三字段）；
    - pi_desktop → ``{backend:"pi_desktop", 五字段}``（token 绝不进快照）。

    未保存 → ``{backend:"pi", mode:"inherit"}``（=继承 PI 默认会话配置）。
    """
    config = deepcopy(config_override) if config_override is not None else load(conn)
    if config and config.get("backend") == "codex_desktop":
        return {"backend": "codex_desktop", "model_source": "desktop_session",
                "reasoning_effort": "inherit"}
    if config and config.get("backend") in CLI_KEYS:
        active = config["backend"]
        return {"backend": active, **cli_block(config, active)}
    if config is not None and config.get("backend") == "pi_desktop":
        resolved, error = resolve_runtime_config(conn, config.get("pi_desktop") or {})
        if resolved is None:
            raise util.KanpilotError(
                "validation_failed",
                f"Plus Review 配置快照解析失败：{error or '未知原因'}",
            )
        return {"backend": "pi_desktop", **resolved}
    config = config or {}
    mode = config.get("mode") or "inherit"
    snap = {"backend": "pi", "mode": mode}
    if mode == "custom":
        for key in CUSTOM_KEYS:
            if key in config:
                snap[key] = config[key]
    return snap


def _validate_pi_desktop(block: Optional[dict]) -> Optional[str]:
    """Plus Review 分支校验：五字段完整 + 新轮次权限固定 auto。"""
    if not isinstance(block, dict) or not block:
        return "Plus Review 分支配置未保存"
    b = dict(block)
    error = exec_config.validate_pi_desktop_block(b)
    if error is not None:
        return error
    if b.get("permission_mode") != PI_DESKTOP_PERMISSION_MODE:
        return (
            f"Plus Review 分支仅允许 permission_mode="
            f"{PI_DESKTOP_PERMISSION_MODE}（当前 {b.get('permission_mode')}）"
        )
    return None

def validate(config: dict, caps: dict) -> Optional[str]:
    """保存校验（§5.23.4 + §5.24.1）——按 backend 分支化。返回中文错误原因。

    - ``pi_desktop``：五字段（connection_file 绝对路径、thinking_level/
      permission_mode 合法、permission_mode 必须 auto）；``mode`` 不参与。
    - ``pi``（缺省）：mode 必须 inherit|custom；custom 时
      provider/model/reasoning_effort 必填非空且经 exec-capabilities 校验
      （agent_preset 以 "default" 占位参与校验）。
    """
    if not isinstance(config, dict):
        return "配置必须是对象"
    resolved = config.get("backend")
    if resolved is not None and resolved not in BACKENDS:
        return (
            f"backend 不支持：{resolved}"
            f"（可用：{', '.join(BACKENDS)}）"
        )
    if _backend(resolved) in CLI_KEYS:
        active = _backend(resolved)
        return _validate_cli(cli_block(config, active), active)
    if _backend(resolved) == "codex_desktop":
        return None
    if _backend(resolved) == "pi_desktop":
        return _validate_pi_desktop(
            exec_config.merge_pi_desktop_block(config.get("pi_desktop"))
        )
    mode = config.get("mode", "inherit")
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
    """校验并原子保存（§5.23.4 + §5.24.1）。

    fields={backend?, mode?, provider?, model?, reasoning_effort?,
    pi_desktop?}：按**当前生效 backend** 分支校验（pi → mode/三字段；
    pi_desktop → 五字段 + auto），**服务离线仍可保存**；切换后端不删除另一
    分支已存字段。失败 raise bad_request（中文原因，旧值不变）；成功写
    settings.review_config + updated_at，返回完整生效值。
    """
    if not isinstance(fields, dict):
        raise util.bad_request("review_config 必须是对象")
    stored = db.get_setting(conn, "review_config")
    stored = dict(stored) if isinstance(stored, dict) else {}
    merged = dict(stored)
    if isinstance(stored.get("pi_desktop"), dict) and stored["pi_desktop"]:
        merged["pi_desktop"] = dict(stored["pi_desktop"],
                                    permission_mode=PI_DESKTOP_PERMISSION_MODE)

    if "backend" in fields:
        submitted = fields["backend"]
        if submitted not in BACKENDS:
            raise util.bad_request(
                f"Review 配置校验失败：backend 不支持：{submitted}"
                f"（可用：{', '.join(BACKENDS)}）"
            )
        merged["backend"] = submitted
    resolved = _backend(merged.get("backend"))

    if "mode" in fields and fields["mode"] is not None:
        if fields["mode"] not in MODES:
            raise util.bad_request(
                f"Review 配置校验失败：mode 不支持：{fields['mode']}"
                f"（可用：{', '.join(MODES)}）"
            )
        merged["mode"] = fields["mode"]
    for key in CUSTOM_KEYS:
        if key in fields:
            merged[key] = fields[key]
    if "pi_desktop" in fields:
        block = fields["pi_desktop"]
        if not isinstance(block, dict):
            raise util.bad_request("pi_desktop 必须是对象")
        merged_block = dict(merged.get("pi_desktop") or {})
        merged_block.update(block)
        merged["pi_desktop"] = merged_block

    for name in CLI_KEYS:
        if name in fields:
            if not isinstance(fields[name], dict):
                raise util.bad_request(f"{name} 必须是对象")
            if set(fields[name]) - set(CLI_KEYS[name]):
                raise util.bad_request(f"{name} 含不支持的字段")
            block = cli_block(stored, name)
            block.update(fields[name])
            error = _validate_cli(block, name)
            if error:
                raise util.bad_request(error)
            merged[name] = block
    candidate = {"backend": resolved}
    if resolved in CLI_KEYS:
        candidate[resolved] = cli_block(merged, resolved)

    if resolved == "pi_desktop":
        candidate["pi_desktop"] = merged.get("pi_desktop")
    else:
        candidate["mode"] = merged.get("mode", "inherit")
        for key in CUSTOM_KEYS:
            if key in merged:
                candidate[key] = merged[key]
    error = validate(candidate, exec_config.capabilities(conn) if resolved == "pi" else {})
    if error is not None:
        raise util.bad_request(f"Review 配置校验失败：{error}")

    record = {
        "backend": resolved,
        "mode": merged.get("mode") if merged.get("mode") in MODES else "inherit",
    }
    if record["mode"] == "custom":
        # 保留 pi 分支已存的显式模型字段（切回 pi 时可恢复；inherit 时才清）。
        record.update({key: merged[key] for key in CUSTOM_KEYS if key in merged})
    record["pi_desktop"] = exec_config.merge_pi_desktop_block(
        merged.get("pi_desktop")
    )
    for name in CLI_KEYS:
        record[name] = cli_block(merged, name)
    record["updated_at"] = now
    if isinstance(stored.get("legacy_config"), dict):
        record["legacy_config"] = deepcopy(stored["legacy_config"])
    elif resolved == "codex_desktop" and _backend(stored.get("backend")) != "codex_desktop":
        # 首次切换时只读本地设置，不能在保存事务内解析 Plus/provider。
        record["legacy_config"] = queue_snapshot(conn)
    db.set_setting(conn, "review_config", record)
    return record


def capabilities(conn: sqlite3.Connection, timeout: int = 10) -> dict:
    """``review-capabilities`` 形状（§5.24.2，只读、不发模型请求）。

    - ``pi``：``{available, detail}``——``mode="inherit"`` 要求本机 PI 环境
      检查通过；``mode="custom"`` 另要求目标 provider/model/reasoning_effort
      在 PI 目录内（沿用 §5.17.2 规则）。
    - ``pi_desktop``：``{available, detail, tools_ok, version}``——已保存五字段
      + 连接探测（工具门 ``PI_DESKTOP_PLAN_TOOLS``：基础工具 + 契约审批工具）。
      失败给中文原因，**不**自动降级到 pi，也不为 Plus 分支跑 PI auth check。
    """
    config = load(conn)
    mode = (config or {}).get("mode") or "inherit"
    pi_caps = exec_config.capabilities(conn, timeout=timeout)
    failed = [c for c in pi_caps["checks"] if not c["ok"]]
    pi_available = not failed
    pi_detail = (
        f"继承本机 PI 默认会话配置"
        f"（Pi CLI {pi_caps['pi'].get('version') or '未知版本'}）"
    )
    if failed:
        pi_detail = "PI 环境不可用（" + "；".join(
            f"{c['name']}: {c['detail']}" for c in failed
        ) + "）"
    elif mode == "custom":
        error = validate(
            {
                "backend": "pi", "mode": "custom",
                **{key: (config or {}).get(key) for key in CUSTOM_KEYS},
            },
            pi_caps,
        )
        if error is not None:
            pi_available = False
            pi_detail = f"custom 配置无效：{error}"
        else:
            pi_detail = (
                f"custom：{(config or {}).get('provider')}/"
                f"{(config or {}).get('model')}"
                f"（thinking {(config or {}).get('reasoning_effort')}）"
            )

    block = exec_config.merge_pi_desktop_block(
        (config or {}).get("pi_desktop")
    )
    if block is None:
        pi_desktop = {
            "available": False,
            "detail": "pi_desktop 配置未保存（请在 Review 环境保存五字段）",
            "tools_ok": False,
            "version": None,
        }
    else:
        error = _validate_pi_desktop(block)
        if error is not None:
            pi_desktop = {
                "available": False, "detail": error,
                "tools_ok": False, "version": None,
            }
        else:
            probe = exec_config._probe_pi_desktop(
                block, timeout=timeout,
                required_tools=constants.PI_DESKTOP_PLAN_TOOLS,
            )
            pi_desktop = {
                "available": bool(probe["available"] and probe["tools_ok"]),
                "detail": probe["detail"] or (
                    f"PI-Desktop 控制面就绪（协议版本 {probe['version']}）"
                    if probe["available"] else "PI-Desktop 控制面不可用"
                ),
                "tools_ok": bool(probe["tools_ok"]),
                "version": probe["version"],
            }

    cli_caps = {}
    for name, executable in (("claude", constants.claude_bin()), ("codex", constants.codex_bin())):
        error = _validate_cli(cli_block(config, name), name)
        present = shutil.which(executable) is not None
        cli_caps[name] = {"available": present and error is None,
                          "detail": error or (f"{name} CLI 可用（模型/认证尚未验证）" if present else f"{name} CLI 不存在")}
    return {
        "pi": {"available": pi_available, "detail": pi_detail},
        "pi_desktop": pi_desktop, **cli_caps,
        "codex_desktop": planner_config.desktop_capabilities(conn),
    }
