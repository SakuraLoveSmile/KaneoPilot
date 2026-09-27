"""pi 执行配置与能力查询（protocol.md §5.17 v1.6 + §5.23.3 v3 后端扩展）。

调度台改配置只写本库 settings.exec_config；**不写 ~/.pi/agent/*、不动
pi/PI-Desktop 全局配置**。凭据只查就绪态（``pi auth check`` 退出码 /
PI-Desktop 连接文件 token 的内存探测），本模块任何输出都不含凭据值。

- load/snapshot：读 settings.exec_config，缺省回落 EXEC_CONFIG_DEFAULTS；
  缺 ``backend`` 一律按 ``"pi"`` 解释（旧数据零迁移），unknown backend
  值同样回落 ``"pi"``。snapshot 按当前 backend 固化所选分支：
  pi → {backend:"pi", provider, model, reasoning_effort, agent_preset}；
  pi_desktop → {backend:"pi_desktop", connection_file, provider_id,
  model_id, thinking_level, permission_mode}（§5.23.3，不含 token）。
- capabilities：pi 分支读当前安装 pi（`pi --version` +
  `~/.pi/agent/models.json` provider/model 清单 + `pi auth check` +
  agents_dir 下 agent.md），返回 §5.17.2 形状（含 checks）；顶层另含
  ``pi_desktop`` 分支就绪态——仅当 pi_desktop 配置块已保存时，经
  ``pi_desktop_backend.probe_connection``（惰性导入）读连接文件并在内存
  完成 initialize/tools/list 探测，不返回 token/连接文件全文。
- validate_config：保存校验。pi 分支沿用 provider/model/effort/preset
  存在且有效（**不**校验凭据）；pi_desktop 分支要求五字段齐全、
  connection_file 为绝对路径、thinking_level/permission_mode 合法
  （permission_mode 不允许缺省/inherit——首次必须用户显式保存）。
- save：合并 fields（四字段子集 / backend / pi_desktop 块）后按**当前
  生效 backend** 的必填组合整体校验，失败 raise bad_request 中文原因；
  切换 backend 不删除另一组已存字段（两分支共存）。成功原子写
  settings.exec_config + updated_at，返回完整生效值。
- check_available：dispatch claim/dispatch-start 的可用性门（§5.17.3
  /§5.23.3）。backend=pi 为既有检查链（含凭据就绪）；backend=pi_desktop
  要求配置块已保存且 probe_connection 可用，失败 → (False, 中文原因)，
  **不自动切回 pi**。
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from . import constants, db, util

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")
# 思考等级的规范展示顺序（模型支持子集按此过滤/排序）。
_EFFORT_ORDER = ("low", "medium", "high", "max")


# ------------------------------------------------------------- 配置读写

# PI-Desktop 分支 thinking_level 词表（protocol §5.23.3）。
PI_DESKTOP_THINKING_LEVELS = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)


def _merge(stored: Optional[dict]) -> dict:
    """defaults ← stored 合并；v3 起另认 backend/pi_desktop 扩展字段。

    缺 ``backend`` 按 ``"pi"`` 解释；stored 里 unknown backend 值同样回落
    ``"pi"``（旧数据零迁移）。``pi_desktop`` 只保留契约五字段的非空
    字符串；整块缺失/为空 → None。未知字段忽略。
    """
    config = dict(constants.EXEC_CONFIG_DEFAULTS)
    if isinstance(stored, dict):
        for key in constants.EXEC_CONFIG_KEYS:
            value = stored.get(key)
            if isinstance(value, str) and value:
                config[key] = value
        backend = stored.get("backend")
        config["backend"] = (
            backend if backend in constants.EXEC_BACKENDS else "pi"
        )
        config["pi_desktop"] = _merge_pi_desktop(stored.get("pi_desktop"))
    else:
        config["backend"] = "pi"
        config["pi_desktop"] = None
    return config


def _merge_pi_desktop(block: Optional[dict]) -> Optional[dict]:
    """pi_desktop 子块规范化：只认契约五字段的非空字符串值；
    非 dict/空块 → None（=未保存）。"""
    if not isinstance(block, dict):
        return None
    cleaned = {
        key: block[key]
        for key in constants.EXEC_PI_DESKTOP_KEYS
        if isinstance(block.get(key), str) and block[key]
    }
    return cleaned or None


def load(conn: sqlite3.Connection) -> dict:
    """当前生效配置：四字段 + backend + pi_desktop（None=未保存），
    含 updated_at 若已保存。"""
    stored = db.get_setting(conn, "exec_config")
    config = _merge(stored if isinstance(stored, dict) else None)
    if isinstance(stored, dict) and isinstance(stored.get("updated_at"), str):
        config["updated_at"] = stored["updated_at"]
    return config


def snapshot(conn: sqlite3.Connection) -> dict:
    """轮次固化快照（§5.17.3/§5.23.3）：按当前 backend 固化所选分支，
    不含 updated_at/token。旧轮次缺 backend 按 "pi" 读。"""
    config = load(conn)
    if config["backend"] == "pi_desktop":
        block = config.get("pi_desktop") or {}
        return {
            "backend": "pi_desktop",
            **{key: block.get(key)
               for key in constants.EXEC_PI_DESKTOP_KEYS},
        }
    return {
        "backend": "pi",
        **{key: config[key] for key in constants.EXEC_CONFIG_KEYS},
    }


def save(conn: sqlite3.Connection, fields: dict, now: str) -> dict:
    """校验并合并保存（§5.23.3）。

    fields 可为契约四字段子集、``backend``、``pi_desktop``（dict）。按
    **当前生效 backend** 的必填组合整体校验：pi → validate_config；
    pi_desktop → 五字段齐全 + connection_file 绝对路径 + thinking_level /
    permission_mode 合法（不允许缺省/inherit）。切换 backend 不删除另一组
    已存字段。失败 raise bad_request（中文原因，旧值不变）；成功原子写
    settings.exec_config + updated_at，返回 load() 形状的完整生效值。
    """
    if not isinstance(fields, dict):
        raise util.bad_request("exec_config 必须是对象")
    stored = _stored_dict(conn)
    merged_stored = dict(stored)
    for key in constants.EXEC_CONFIG_KEYS:
        if key in fields:
            merged_stored[key] = fields[key]
    backend = fields.get("backend", stored.get("backend"))
    if backend is None:
        backend = "pi"  # 缺 backend 按 pi 解释（旧数据零迁移）
    if backend not in constants.EXEC_BACKENDS:
        raise util.bad_request(
            f"backend 不支持：{backend}"
            f"（可用：{', '.join(constants.EXEC_BACKENDS)}）"
        )
    merged_stored["backend"] = backend
    if "pi_desktop" in fields:
        block = fields["pi_desktop"]
        if not isinstance(block, dict):
            raise util.bad_request("pi_desktop 必须是对象")
        merged_block = dict(stored.get("pi_desktop") or {})
        merged_block.update(block)
        merged_stored["pi_desktop"] = merged_block
    merged = _merge(merged_stored)

    # 当前生效 backend 的必填组合整体校验；显式提交的 pi_desktop 块即使
    # backend=pi 也先验完整性（避免保存残块）。
    if "pi_desktop" in fields or merged["backend"] == "pi_desktop":
        error = _validate_pi_desktop_block(merged.get("pi_desktop"))
        if error is not None:
            raise util.bad_request(f"执行配置校验失败：{error}")
    if merged["backend"] == "pi":
        error = validate_config(merged, capabilities(conn))
        if error is not None:
            raise util.bad_request(f"执行配置校验失败：{error}")

    payload = dict(merged)
    payload["updated_at"] = now
    db.set_setting(conn, "exec_config", payload)
    return payload


def _stored_dict(conn: sqlite3.Connection) -> dict:
    stored = db.get_setting(conn, "exec_config")
    return stored if isinstance(stored, dict) else {}

# ------------------------------------------------------------- 能力查询

def _read_models_json(pi_home: Path) -> Tuple[Optional[dict], Optional[str]]:
    """pi 模型清单：~/.pi/agent/models.json（v1.6）。"""
    path = pi_home / "agent" / "models.json"
    if not path.is_file():
        return None, f"pi models.json 不存在：{path}"
    try:
        data = util.jload(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"pi models.json 解析失败：{exc}"
    if not isinstance(data, dict):
        return None, "pi models.json 不是对象结构"
    return data, None


def _providers_from(data: dict) -> list:
    """models.json providers → 能力视图（§5.17.2 形状）。

    pi 结构：{"providers": {"<id>": {"baseUrl","api","apiKey",
    "models":[{"id","name","reasoning","thinkingLevelMap"…}]}}}。
    **apiKey 不透出**——只记 present/missing。"""
    providers = (data or {}).get("providers")
    result = []
    if not isinstance(providers, dict):
        return result
    for pid, spec in providers.items():
        if not isinstance(spec, dict):
            continue
        models = []
        for model in spec.get("models") or []:
            if not isinstance(model, dict) or not model.get("id"):
                continue
            # thinkingLevelMap 声明支持的等级键；reasoning=False → 无思考。
            efforts = []
            if model.get("reasoning"):
                level_map = model.get("thinkingLevelMap")
                if isinstance(level_map, dict):
                    efforts = [e for e in _EFFORT_ORDER if e in level_map]
                    efforts += [e for e in level_map if e not in efforts]
                if not efforts:
                    efforts = list(_EFFORT_ORDER)
            models.append({
                "id": str(model["id"]),
                "name": str(model.get("name") or model["id"]),
                "reasoning_efforts": efforts,
            })
        result.append({
            "id": str(pid),
            "display_name": str(spec.get("displayName") or pid),
            "has_api_key": bool(spec.get("apiKey")),
            "models": models,
        })
    return result


def _seed_agents_dir(agents_dir: Path) -> None:
    """内置预设播种（v1.6）：仅在缺省目录（无 KANPILOT_AGENTS_DIR 覆盖）
    且目录缺失/为空时，从 ``core/agents/`` 复制内置 *.md；用户文件不覆盖。"""
    if os.environ.get("KANPILOT_AGENTS_DIR"):
        return
    bundled = constants.core_root() / "agents"
    if not bundled.is_dir():
        return
    try:
        existing = (
            [p for p in agents_dir.iterdir()
             if p.is_file() and p.suffix == ".md"]
            if agents_dir.is_dir() else []
        )
        if existing:
            return
        agents_dir.mkdir(parents=True, exist_ok=True)
        for src in bundled.iterdir():
            if src.is_file() and src.suffix == ".md":
                dst = agents_dir / src.name
                if not dst.exists():
                    dst.write_text(src.read_text(encoding="utf-8"),
                                   encoding="utf-8")
    except OSError:
        pass


def _agents_from(agents_dir: Path) -> Tuple[list, dict]:
    """扫描 agent.md 预设目录：``<name>.md`` → preset id=<name>。
    返回 (presets, checks_extra)。目录不存在 → 空 roster。"""
    _seed_agents_dir(agents_dir)
    presets, checks = [], {"agents_dir": str(agents_dir)}
    if not agents_dir.is_dir():
        checks["missing"] = True
        return presets, checks
    try:
        entries = sorted(agents_dir.iterdir())
    except OSError:
        checks["unreadable"] = True
        return presets, checks
    for entry in entries:
        if entry.is_file() and entry.suffix == ".md" and not entry.name.startswith("."):
            presets.append({
                "id": entry.stem, "name": entry.stem,
                "description": None, "broken": None,
                "path": str(entry),
            })
    return presets, checks


def _probe_version(timeout: int = 10) -> Tuple[Optional[str], Optional[str]]:
    binary = constants.pi_bin()
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"pi 可执行不存在：{binary}"
    except subprocess.TimeoutExpired:
        return None, f"pi --version 超时（{timeout}s）：{binary}"
    except OSError as exc:
        return None, f"pi --version 执行失败：{exc}"
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if _VERSION_RE.match(line):
            return line, None
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if _VERSION_RE.match(line):
            return line, None
    if proc.returncode != 0:
        return None, f"pi --version 退出码 {proc.returncode}"
    return (proc.stdout or "").strip() or None, None


def _probe_auth(provider: str, model: str, timeout: int = 15
               ) -> Tuple[bool, str]:
    """``pi auth check --provider <p> --model <m>`` 就绪检查。

    只看退出码；**不读不输出凭据值**。pi 缺失/超时/非 0 → (False, detail)。
    """
    binary = constants.pi_bin()
    argv = [binary, "auth", "check"]
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["--model", model]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, f"pi 可执行不存在：{binary}"
    except subprocess.TimeoutExpired:
        return False, f"pi auth check 超时（{timeout}s）"
    except OSError as exc:
        return False, f"pi auth check 执行失败：{exc}"
    if proc.returncode == 0:
        return True, "凭据就绪"
    # stderr/stdout 摘要（不含密钥——pi 自身不输出值，只报 provider/原因）
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = detail[-1][:120] if detail else f"退出码 {proc.returncode}"
    return False, f"pi auth check 未就绪：{tail}"


def _probe_pi_desktop(block: dict, timeout: int = 10) -> dict:
    """PI-Desktop 分支就绪态（§5.23.3）：经 ``pi_desktop_backend``（惰性
    导入——该模块由子任务 B 实现，缺失不阻塞 pi 分支）按用户保存的
    connection_file 做连接文件校验 + 内存 initialize/tools/list。

    返回 {available, detail, tools_ok, version, session_naming}；token、
    连接文件全文、Authorization header 绝不进返回值。``session_naming``
    为 T6 兼容门（§5.23.10）：``pi_session_create`` schema 暴露
    ``projectName`` 时为 True（旧 Plus → False）。
    """
    try:
        from . import pi_desktop_backend
    except ImportError:
        return {
            "available": False,
            "detail": "pi_desktop 后端模块缺失",
            "tools_ok": False,
            "version": None,
            "session_naming": False,
        }
    try:
        result = pi_desktop_backend.probe_connection(
            block.get("connection_file") or "",
            required_tools=constants.PI_DESKTOP_DISPATCH_TOOLS,
            provider_id=block.get("provider_id") or None,
            model_id=block.get("model_id") or None,
            timeout=timeout,
        )
    except Exception as exc:  # 防御：探测失败按不可用报原因，不崩溃命令
        result = {"ok": False, "detail": f"探测失败：{exc}"}
    if not isinstance(result, dict):
        result = {"ok": False, "detail": "探测结果不可用"}
    # 模块返回 {ok, detail, tools:set, version, session_naming} → 归一化形状。
    tools = result.get("tools")
    tools_ok = bool(tools) and all(
        name in tools for name in constants.PI_DESKTOP_DISPATCH_TOOLS
    )
    return {
        "available": bool(
            result.get("available", result.get("ok"))
        ),
        "detail": str(result.get("detail") or result.get("reason") or ""),
        "tools_ok": tools_ok,
        "version": (
            result.get("version")
            if isinstance(result.get("version"), str) else None
        ),
        "session_naming": bool(result.get("session_naming")),
    }


def capabilities(
    conn: Optional[sqlite3.Connection] = None, timeout: int = 10, *,
    config_snapshot: Optional[dict] = None,
) -> dict:
    """exec-capabilities 响应（§5.17.2，v1.6）。

    默认凭据检查针对当前生效配置；dispatch 传入轮次快照时只使用该快照
    的 provider/model，避免配置在 claim 后被修改导致错检。"""
    pi_home = constants.pi_home()
    checks = []
    pi_path = None
    version, version_error = _probe_version(timeout)
    if version_error is None:
        import shutil as _shutil

        pi_path = _shutil.which(constants.pi_bin()) or constants.pi_bin()
    checks.append({
        "name": "pi_binary",
        "ok": version is not None,
        "detail": version or version_error or "pi 不可用",
    })

    data, models_error = _read_models_json(pi_home)
    providers = _providers_from(data) if data is not None else []
    checks.append({
        "name": "models",
        "ok": bool(providers),
        "detail": (
            f"{pi_home / 'agent' / 'models.json'}，{len(providers)} 个 provider"
            if providers else (models_error or "models.json 中没有可用 provider")
        ),
    })

    presets, preset_checks = _agents_from(constants.agents_dir())
    checks.append({
        "name": "agent_preset",
        "ok": bool(presets),
        "detail": (
            f"{len(presets)} 个 agent.md（{preset_checks['agents_dir']}）"
            if presets else
            f"agent.md 目录为空或不存在：{preset_checks['agents_dir']}"
        ),
    })

    # 凭据检查：默认读当前设置；显式快照时绝不回读当前设置。
    if config_snapshot is None:
        config = load(conn) if conn is not None else dict(
            constants.EXEC_CONFIG_DEFAULTS
        )
    elif isinstance(config_snapshot, dict):
        config = dict(config_snapshot)
    else:
        config = {}
    cred_ok, cred_detail = _probe_auth(
        config.get("provider") or "", config.get("model") or "",
        timeout=max(timeout, 15),
    )
    checks.append({
        "name": "credential",
        "ok": cred_ok,
        "detail": cred_detail,
    })

    # v3（§5.23.3）：pi_desktop 分支就绪态——仅在配置块已保存时探测
    # 连接文件；provider/model 有效性以字段非空为准（控制面无枚举）。
    block = config.get("pi_desktop")
    if isinstance(block, dict):
        pi_desktop = _probe_pi_desktop(block, timeout=timeout)
    else:
        pi_desktop = {
            "available": False,
            "detail": "pi_desktop 配置未保存",
            "tools_ok": False,
            "version": None,
            "session_naming": False,
        }

    return {
        "ok": all(item["ok"] for item in checks),
        "pi": {"path": pi_path, "version": version, "home": str(pi_home)},
        "pi_desktop": pi_desktop,
        "providers": [
            {"id": p["id"], "display_name": p["display_name"],
             "models": p["models"]}
            for p in providers
        ],
        "presets": presets,
        "checks": checks,
        "_providers_detail": providers,  # 内部：has_api_key 等，命令层不透出
    }


def _validate_pi_desktop_block(block: Optional[dict]) -> Optional[str]:
    """pi_desktop 分支保存校验（§5.23.3）：五字段齐全（permission_mode
    不允许缺省/inherit——首次必须用户显式保存）、connection_file 为
    绝对路径、thinking_level/permission_mode 在合法词表内。
    provider_id/model_id 有效性以字段非空为准（控制面无 provider 枚举）。
    """
    if not isinstance(block, dict):
        return "pi_desktop 配置未保存（backend=pi_desktop 需先保存完整块）"
    missing = [
        key for key in constants.EXEC_PI_DESKTOP_KEYS
        if not isinstance(block.get(key), str) or not block[key]
    ]
    if missing:
        return f"pi_desktop 缺少必填字段：{', '.join(missing)}"
    if not os.path.isabs(block["connection_file"]):
        return "pi_desktop.connection_file 必须是绝对路径"
    if block["thinking_level"] not in PI_DESKTOP_THINKING_LEVELS:
        return (
            f"pi_desktop.thinking_level 不合法：{block['thinking_level']}"
            f"（可用：{', '.join(PI_DESKTOP_THINKING_LEVELS)}）"
        )
    if block["permission_mode"] not in constants.EXEC_PERMISSION_MODES:
        return (
            f"pi_desktop.permission_mode 不合法或缺省："
            f"{block['permission_mode']}（必须显式保存："
            f"{', '.join(constants.EXEC_PERMISSION_MODES)}）"
        )
    return None


def validate_config(config: dict, caps: Optional[dict]) -> Optional[str]:
    """保存校验（§5.17.2/§5.23.3）：backend=pi_desktop → 分支五字段校验；
    pi（缺省）→ provider/model/effort/preset 必须有效。
    返回中文错误原因；通过返回 None。**不**校验凭据（属执行环境）。"""
    if not isinstance(config, dict):
        return "配置必须是对象"
    if config.get("backend") == "pi_desktop":
        return _validate_pi_desktop_block(config.get("pi_desktop"))
    caps = caps if isinstance(caps, dict) else {}
    provider_id = config.get("provider")
    provider = next(
        (p for p in caps.get("_providers_detail", [])
         if p["id"] == provider_id), None
    )
    if provider is None:
        available = ", ".join(
            p["id"] for p in caps.get("_providers_detail", [])
        ) or "无"
        return f"provider 不存在：{provider_id}（可用：{available}）"
    model = next(
        (m for m in provider["models"] if m["id"] == config.get("model")), None
    )
    if model is None:
        available = ", ".join(m["id"] for m in provider["models"]) or "无"
        return (
            f"provider {provider_id} 下模型不存在："
            f"{config.get('model')}（可用：{available}）"
        )
    effort = config.get("reasoning_effort")
    if effort not in model["reasoning_efforts"]:
        supported = ", ".join(model["reasoning_efforts"]) or "无"
        return (
            f"模型 {model['id']} 不支持思考等级 {effort}"
            f"（支持：{supported}）"
        )
    preset_id = config.get("agent_preset")
    preset = next(
        (p for p in caps.get("presets", []) if p["id"] == preset_id), None
    )
    if preset is None:
        available = ", ".join(p["id"] for p in caps.get("presets", [])) or "无"
        return f"预设不存在：{preset_id}（可用：{available}）"
    if preset.get("broken"):
        return f"预设不可用：{preset_id}（{preset['broken']}）"
    return None


def check_available(
    conn: sqlite3.Connection, timeout: int = 10, *,
    config_snapshot: Optional[dict] = None,
) -> Tuple[bool, Optional[str]]:
    """dispatch claim / dispatch-start 的可用性门（§5.17.3/§5.23.3）。

    backend=pi：capabilities 全部 checks 通过 + 当前配置经
    validate_config。backend=pi_desktop：配置块已保存且
    probe_connection 可用（连接文件/token/tools/provider/model）。
    失败 → (False, 中文原因)；调用方按 validation_failed 阻止派发，
    **不自动换模型、agent 或后端**。"""
    if config_snapshot is None:
        config = load(conn)
    else:
        # dispatch-start must recheck the claim's flat snapshot.  Do not
        # silently replace it with the current settings after a user edit.
        if not isinstance(config_snapshot, dict):
            return False, "exec_config 轮次快照无效：必须是对象"
        config = dict(config_snapshot)
        backend = config.get("backend")
        if backend not in constants.EXEC_BACKENDS:
            return False, f"exec_config 轮次快照 backend 不支持：{backend}"
    if config["backend"] == "pi_desktop":
        # Saved settings keep this branch nested; round snapshots are flat.
        block = config.get("pi_desktop")
        if block is None:
            block = {
                key: config.get(key)
                for key in constants.EXEC_PI_DESKTOP_KEYS
            }
        if not isinstance(block, dict):
            return False, (
                "pi_desktop 执行配置未保存"
                "（set-exec-config 需完整保存五字段，"
                "permission_mode 不允许缺省）"
            )
        error = _validate_pi_desktop_block(block)
        if error is not None:
            return False, f"pi_desktop 执行配置无效：{error}"
        probe = _probe_pi_desktop(block, timeout=timeout)
        if not probe["available"]:
            return False, (
                f"pi_desktop 执行环境不可用：{probe['detail']}"
            )
        return True, None
    caps = capabilities(
        conn, timeout=timeout, config_snapshot=config_snapshot,
    )
    failed = [c for c in caps["checks"] if not c["ok"]]
    if failed:
        detail = "；".join(f"{c['name']}: {c['detail']}" for c in failed)
        return False, f"pi 执行环境不可用（{detail}）"
    error = validate_config(config, caps)
    if error is not None:
        return False, f"pi 执行配置无效：{error}"
    return True, None
