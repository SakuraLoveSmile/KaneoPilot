"""pi 执行配置与能力查询（protocol.md §5.17，v1.6）。

调度台改配置只写本库 settings.exec_config；**不写 ~/.pi/agent/*、不动
pi 全局配置**。凭据只查就绪态（``pi auth check`` 退出码），本模块任何
输出都不含凭据值。

- load/snapshot：读 settings.exec_config，缺省回落 EXEC_CONFIG_DEFAULTS。
- capabilities：读当前安装 pi（`pi --version` + `~/.pi/agent/models.json`
  provider/model 清单 + `pi auth check` + agents_dir 下 agent.md），
  返回 §5.17.2 形状（含 checks）。
- validate_config：保存时校验（provider/model/effort/preset 存在且有效；
  **不**校验凭据——凭据属执行环境，只在 check_available 拦派发）。
- check_available：dispatch claim/dispatch-start 的可用性门（§5.17.3，
  含凭据就绪）→ (ok, reason|None)。
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

def _merge(stored: Optional[dict]) -> dict:
    """defaults ← stored 合并；只认契约四字段，未知字段忽略。"""
    config = dict(constants.EXEC_CONFIG_DEFAULTS)
    if isinstance(stored, dict):
        for key in constants.EXEC_CONFIG_KEYS:
            value = stored.get(key)
            if isinstance(value, str) and value:
                config[key] = value
    return config


def load(conn: sqlite3.Connection) -> dict:
    """当前生效配置（四字段，含 updated_at 若已保存）。"""
    stored = db.get_setting(conn, "exec_config")
    config = _merge(stored if isinstance(stored, dict) else None)
    if isinstance(stored, dict) and isinstance(stored.get("updated_at"), str):
        config["updated_at"] = stored["updated_at"]
    return config


def snapshot(conn: sqlite3.Connection) -> dict:
    """轮次固化快照（§5.17.3）：只要契约四字段，不含 updated_at。"""
    config = load(conn)
    return {key: config[key] for key in constants.EXEC_CONFIG_KEYS}


def save(conn: sqlite3.Connection, config: dict, now: str) -> dict:
    """合并保存（命令层先 validate_config）。返回含 updated_at 的完整配置。"""
    merged = _merge({**_stored_dict(conn), **config})
    payload = {**merged, "updated_at": now}
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


def capabilities(
    conn: Optional[sqlite3.Connection] = None, timeout: int = 10
) -> dict:
    """exec-capabilities 响应（§5.17.2，v1.6）。凭据检查针对**当前生效
    配置**的 provider/model（conn 提供时）；detail 不含凭据值。"""
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

    # 凭据检查：当前配置 provider/model 的 pi auth check 退出码。
    config = load(conn) if conn is not None else dict(
        constants.EXEC_CONFIG_DEFAULTS
    )
    cred_ok, cred_detail = _probe_auth(
        config.get("provider") or "", config.get("model") or "",
        timeout=max(timeout, 15),
    )
    checks.append({
        "name": "credential",
        "ok": cred_ok,
        "detail": cred_detail,
    })

    return {
        "ok": all(item["ok"] for item in checks),
        "pi": {"path": pi_path, "version": version, "home": str(pi_home)},
        "providers": [
            {"id": p["id"], "display_name": p["display_name"],
             "models": p["models"]}
            for p in providers
        ],
        "presets": presets,
        "checks": checks,
        "_providers_detail": providers,  # 内部：has_api_key 等，命令层不透出
    }


def validate_config(config: dict, caps: dict) -> Optional[str]:
    """保存校验（§5.17.2）：provider/model/effort/preset 必须有效。
    返回中文错误原因；通过返回 None。**不**校验凭据（属执行环境）。"""
    if not isinstance(config, dict):
        return "配置必须是对象"
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
    conn: sqlite3.Connection, timeout: int = 10
) -> Tuple[bool, Optional[str]]:
    """dispatch claim / dispatch-start 的可用性门（§5.17.3）：
    capabilities 全部 checks 通过 + 当前配置经 validate_config。
    失败 → (False, 中文原因)；调用方按 validation_failed 阻止派发，
    **不自动换模型或 agent**。"""
    caps = capabilities(conn, timeout=timeout)
    failed = [c for c in caps["checks"] if not c["ok"]]
    if failed:
        detail = "；".join(f"{c['name']}: {c['detail']}" for c in failed)
        return False, f"pi 执行环境不可用（{detail}）"
    error = validate_config(load(conn), caps)
    if error is not None:
        return False, f"pi 执行配置无效：{error}"
    return True, None
