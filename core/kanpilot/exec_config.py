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
- v4（§5.24.1）：``controller_backend``（``pi``|``pi_desktop``，缺省 ``pi``）只控制
  dispatch/monitor 控制端，与任务执行 ``backend`` 并存、互不覆盖；缺字段/未知值
  一律按 ``pi`` 解释（旧数据零迁移），切换不删除另一分支已存字段。
- controller_snapshot/controller_capabilities/check_controller_available：控制轮
  自身的固化快照与就绪态。``pi`` 分支沿用 PI CLI 检查（可执行 + 版本），
  ``pi_desktop`` 分支复用同一探针但只要求 ``PI_DESKTOP_CONTROLLER_TOOLS``
  （控制端**不**建 Goal 会话，故不需要契约审批工具）；控制器就绪态与任务执行
  就绪态分开，不用其一推断另一，失败**不**自动回落 pi。
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
    """defaults ← stored 合并；v3 起另认 backend/pi_desktop 扩展字段，v4 另认
    ``controller_backend``。

    缺 ``backend`` 按 ``"pi"`` 解释、缺 ``controller_backend`` 同样按 ``"pi"``；
    stored 里 unknown 值同样回落（旧数据零迁移）。``pi_desktop`` 只保留契约
    五字段的非空字符串；整块缺失/为空 → None。未知字段忽略。
    """
    config = dict(constants.EXEC_CONFIG_DEFAULTS)
    if isinstance(stored, dict):
        for key in constants.EXEC_CONFIG_KEYS:
            value = stored.get(key)
            if isinstance(value, str) and value:
                config[key] = value
        config["backend"] = _exec_backend(stored.get("backend"))
        config[constants.CONTROLLER_BACKEND_KEY] = _controller_backend(
            stored.get(constants.CONTROLLER_BACKEND_KEY)
        )
        config["pi_desktop"] = _merge_pi_desktop(stored.get("pi_desktop"))
    else:
        config["backend"] = "pi"
        config[constants.CONTROLLER_BACKEND_KEY] = "pi"
        config["pi_desktop"] = None
    return config


def _exec_backend(value) -> str:
    """任务执行后端规范化（§5.23.3）：缺省/未知值 → "pi"（旧数据零迁移）。"""
    return value if value in constants.EXEC_BACKENDS else "pi"


def _controller_backend(value) -> str:
    """控制器后端规范化（§5.24.1）：缺省/未知值 → "pi"（旧数据零迁移）。"""
    return value if value in constants.CONTROLLER_BACKENDS else "pi"


def _merge_pi_desktop(block: Optional[dict]) -> Optional[dict]:
    """pi_desktop 子块规范化：认契约五字段 + model_source 的非空字符串值；
    非 dict/空块 → None（=未保存）。"""
    if not isinstance(block, dict):
        return None
    cleaned = {}
    for key in constants.EXEC_PI_DESKTOP_ALL_KEYS:
        val = block.get(key)
        if isinstance(val, str) and val:
            cleaned[key] = val
    if not cleaned:
        return None
    model_source = cleaned.get("model_source")
    if model_source not in constants.MODEL_SOURCES:
        if "provider_id" in cleaned or "model_id" in cleaned or "thinking_level" in cleaned:
            model_source = "custom"
        else:
            model_source = "inherit"
    cleaned["model_source"] = model_source
    if not cleaned.get("connection_file"):
        cleaned["connection_file"] = constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE
    return cleaned


def merge_pi_desktop_block(block: Optional[dict]) -> Optional[dict]:
    """五字段块规范化（跨角色共享，§5.24.1 / T7.1）：plan/执行/Review 三处后端都用
    同一形状；认契约字段与 model_source，非 dict/空块 → None（未保存）。"""
    return _merge_pi_desktop(block)


def load(conn: sqlite3.Connection) -> dict:
    """当前生效配置：四字段 + backend + pi_desktop（None=未保存），
    含 updated_at 若已保存。"""
    stored = db.get_setting(conn, "exec_config")
    config = _merge(stored if isinstance(stored, dict) else None)
    if isinstance(stored, dict) and isinstance(stored.get("updated_at"), str):
        config["updated_at"] = stored["updated_at"]
    return config


def snapshot(conn: sqlite3.Connection, config_override: Optional[dict] = None) -> dict:
    """轮次固化快照（§5.17.3/§5.23.3 / T7.1 / T7.3）：按当前 backend 固化所选分支，
    不含 updated_at/token。旧轮次缺 backend 按 "pi" 读。
    pi_desktop 分支固化脱敏的 5 字段（inherit 模式解析当前 Plus 默认）。
    """
    config = dict(config_override) if isinstance(config_override, dict) else load(conn)
    backend = config.get("backend") or "pi"
    if backend == "pi_desktop":
        block = config.get("pi_desktop")
        if block is None and all(config.get(k) is not None for k in ("connection_file", "provider_id", "model_id")):
            block = {k: config[k] for k in constants.EXEC_PI_DESKTOP_KEYS if k in config}
        resolved, error = resolve_runtime_config(conn, block)
        if resolved is None:
            raise util.KanpilotError(
                "validation_failed",
                f"pi_desktop 运行配置快照解析失败：{error or '未知原因'}",
            )
        return {"backend": "pi_desktop", **resolved}
    return {
        "backend": "pi",
        **{key: config.get(key) for key in constants.EXEC_CONFIG_KEYS if key in config},
    }

def snapshot_with_model_override(conn: sqlite3.Connection, task) -> dict:
    """``snapshot`` + 引擎一次性模型覆盖（``engine/fallback.py``，scenarios.md #5）。

    有界补救轮次前，引擎把选定的模型快照写进 ``extra.engine_model_override``
    （探针通过才写；探针失败不写、不换模型）。这里只在覆盖与当前计划身份一致时
    应用，且**不**回写 ``settings.exec_config``——设置变更仍只影响新轮次，覆盖
    只影响它瞄准的那一次重跑。无覆盖（旧数据/普通轮次）时与 ``snapshot(conn)``
    完全一致。
    """
    from .engine import fallback
    override = fallback.read_override(db.task_extra(task))
    if not override or not fallback.override_matches(override, task):
        return snapshot(conn)
    merged = dict(load(conn))
    if merged.get("backend") == "pi_desktop":
        block = dict(merged.get("pi_desktop") or {})
        for key in constants.EXEC_PI_DESKTOP_ALL_KEYS:
            if override.get(key):
                block[key] = override[key]
        if block.get("provider_id") and block.get("model_id"):
            block["model_source"] = "custom"
        merged["pi_desktop"] = block
    else:
        for key in constants.EXEC_CONFIG_KEYS:
            if override.get(key):
                merged[key] = override[key]
    return snapshot(conn, config_override=merged)

def save(conn: sqlite3.Connection, fields: dict, now: str) -> dict:
    """校验并合并保存（§5.23.3 + §5.24.1）。

    fields 可为契约四字段子集、``backend``、``controller_backend``、
    ``pi_desktop``（dict）。按**当前生效 backend/controller_backend** 的必填组合
    整体校验：pi → validate_config；pi_desktop → 五字段齐全 + connection_file
    绝对路径 + thinking_level / permission_mode 合法（不允许缺省/inherit）。
    选择 ``pi_desktop``（任一处）都要求同一五字段块完整。保存切换不删除另一组
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
    if constants.CONTROLLER_BACKEND_KEY in fields:
        controller = fields[constants.CONTROLLER_BACKEND_KEY]
        # 显式提交则必须是合法枚举（不静默回落 pi——保存失败比暗改语义安全）。
        if controller not in constants.CONTROLLER_BACKENDS:
            raise util.bad_request(
                f"{constants.CONTROLLER_BACKEND_KEY} 不支持：{controller}"
                f"（可用：{', '.join(constants.CONTROLLER_BACKENDS)}）"
            )
        merged_stored[constants.CONTROLLER_BACKEND_KEY] = controller
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
    # 当前生效 backend/controller_backend 的必填组合整体校验；显式提交的
    # pi_desktop 块即使两个 backend 都用 pi 也先验完整性（避免保存残块）。
    needs_block = (
        "pi_desktop" in fields
        or merged["backend"] == "pi_desktop"
    )
    if needs_block:
        error = validate_pi_desktop_block(merged.get("pi_desktop"))
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


def _probe_pi_desktop(block: dict, timeout: int = 10,
                      required_tools: Optional[tuple] = None) -> dict:
    """PI-Desktop 分支就绪态（§5.23.3 / T7.1）：经 ``pi_desktop_backend``
    做连接文件校验 + 内存 initialize/tools/list 与 provider/model 解析。

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

    conn_file = block.get("connection_file") or constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE
    model_source = block.get("model_source") or "custom"
    provider_id = block.get("provider_id")
    model_id = block.get("model_id")

    if model_source == "inherit" and (not provider_id or not model_id):
        resolved, err = pi_desktop_backend.resolve_plus_default(conn_file, timeout=timeout)
        if err is not None or resolved is None:
            base_probe = pi_desktop_backend.probe_connection(
                conn_file,
                required_tools=(required_tools or constants.PI_DESKTOP_DISPATCH_TOOLS),
                timeout=timeout,
            )
            gate = tuple(required_tools or constants.PI_DESKTOP_DISPATCH_TOOLS)
            tools = base_probe.get("tools")
            tools_ok = bool(tools) and all(name in tools for name in gate)
            return {
                "available": False,
                "detail": f"跟随 Plus 默认配置 · {err}",
                "tools_ok": tools_ok,
                "version": base_probe.get("version"),
                "session_naming": bool(base_probe.get("session_naming")),
            }
        provider_id = resolved["provider_id"]
        model_id = resolved["model_id"]

    try:
        result = pi_desktop_backend.probe_connection(
            conn_file,
            required_tools=(required_tools or constants.PI_DESKTOP_DISPATCH_TOOLS),
            provider_id=provider_id,
            model_id=model_id,
            timeout=timeout,
        )
    except Exception as exc:
        result = {"ok": False, "detail": f"探测失败：{exc}"}
    if not isinstance(result, dict):
        result = {"ok": False, "detail": "探测结果不可用"}
    # 模块返回 {ok, detail, tools:set, version, session_naming} → 归一化形状。

    gate = tuple(required_tools or constants.PI_DESKTOP_DISPATCH_TOOLS)
    tools = result.get("tools")
    tools_ok = bool(tools) and all(name in tools for name in gate)
    return {
        "available": bool(result.get("available", result.get("ok"))),
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
        # v4（§5.24.2）：控制器就绪态与任务执行就绪态分开计算、分开展示。
        "controller": controller_capabilities(conn, timeout=timeout),
        "providers": [
            {"id": p["id"], "display_name": p["display_name"],
             "models": p["models"]}
            for p in providers
        ],
        "presets": presets,
        "checks": checks,
        "_providers_detail": providers,  # 内部：has_api_key 等，命令层不透出
    }


def validate_pi_desktop_block(block: Optional[dict]) -> Optional[str]:
    """pi_desktop 分支保存校验（§5.23.3 / T7.1）：
    - model_source 为 inherit 时：允许 provider_id/model_id/thinking_level 缺省，仅要求 connection_file 与 permission_mode；
    - model_source 为 custom 时：要求 provider_id, model_id, thinking_level, permission_mode, connection_file 齐全；
    - permission_mode 在合法词表内；
    - connection_file 为绝对路径（支持 ~ 展开）。
    """
    if not isinstance(block, dict) or not block:
        return "pi_desktop 配置未保存（backend=pi_desktop 需先保存完整块）"
    model_source = block.get("model_source") or "custom"
    if model_source not in constants.MODEL_SOURCES:
        return f"model_source 不支持：{model_source}（可用：{', '.join(constants.MODEL_SOURCES)}）"

    conn_file = block.get("connection_file") or constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE
    if not isinstance(conn_file, str) or not conn_file:
        return "pi_desktop.connection_file 必须是非空字符串"
    conn_path = Path(conn_file).expanduser()
    if not conn_path.is_absolute():
        return "pi_desktop.connection_file 必须是绝对路径"

    perm = block.get("permission_mode")
    if perm not in constants.EXEC_PERMISSION_MODES:
        return (
            f"pi_desktop.permission_mode 不合法或缺省："
            f"{perm}（必须显式保存："
            f"{', '.join(constants.EXEC_PERMISSION_MODES)}）"
        )

    if model_source == "custom":
        missing = [
            key for key in ("provider_id", "model_id", "thinking_level")
            if not isinstance(block.get(key), str) or not block[key]
        ]
        if missing:
            return f"pi_desktop 缺少必填字段：{', '.join(missing)}"
        if block["thinking_level"] not in PI_DESKTOP_THINKING_LEVELS:
            return (
                f"pi_desktop.thinking_level 不合法：{block['thinking_level']}"
                f"（可用：{', '.join(PI_DESKTOP_THINKING_LEVELS)}）"
            )
    return None


def resolve_runtime_config(
    conn: sqlite3.Connection,
    block: Optional[dict] = None,
    timeout: int = 10,
) -> Tuple[Optional[dict], Optional[str]]:
    """解析脱敏的 5 字段 runtime_config 快照（无 token）。

    - inherit 模式：经目录探针解析 Plus 当前默认配置；
    - custom 模式：使用已存五字段；
    - 失败返回 (None, error_msg)。
    """
    if block is None:
        cfg = load(conn)
        block = cfg.get("pi_desktop")
    if not isinstance(block, dict) or not block:
        return None, "pi_desktop 配置未保存"
    error = validate_pi_desktop_block(block)
    if error is not None:
        return None, error

    model_source = block.get("model_source") or "custom"
    conn_file = block.get("connection_file") or constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE
    conn_path_str = str(Path(conn_file).expanduser())
    perm = block.get("permission_mode")

    if model_source == "inherit":
        try:
            from . import pi_desktop_backend
        except ImportError:
            return None, "pi_desktop 后端模块缺失"
        resolved, err = pi_desktop_backend.resolve_plus_default(conn_path_str, timeout=timeout)
        if err is not None or resolved is None:
            return None, f"Plus 默认配置解析失败：{err or '未知原因'}"
        return {
            "connection_file": conn_path_str,
            "provider_id": resolved["provider_id"],
            "model_id": resolved["model_id"],
            "thinking_level": resolved["thinking_level"],
            "permission_mode": perm,
        }, None
    else:
        return {
            "connection_file": conn_path_str,
            "provider_id": block["provider_id"],
            "model_id": block["model_id"],
            "thinking_level": block["thinking_level"],
            "permission_mode": perm,
        }, None


def validate_config(config: dict, caps: Optional[dict]) -> Optional[str]:
    """保存校验（§5.17.2/§5.23.3）：backend=pi_desktop → 分支五字段校验；
    pi（缺省）→ provider/model/effort/preset 必须有效。
    返回中文错误原因；通过返回 None。**不**校验凭据（属执行环境）。"""
    if not isinstance(config, dict):
        return "配置必须是对象"
    if config.get("backend") == "pi_desktop":
        return validate_pi_desktop_block(config.get("pi_desktop"))
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
    """dispatch claim / dispatch-start 的可用性门（§5.17.3/§5.23.3 / T7.3）。"""
    if config_snapshot is None:
        config = load(conn)
    else:
        if not isinstance(config_snapshot, dict):
            return False, "exec_config 轮次快照无效：必须是对象"
        config = dict(config_snapshot)
        backend = config.get("backend")
        if backend not in constants.EXEC_BACKENDS:
            return False, f"exec_config 轮次快照 backend 不支持：{backend}"

    if config.get("backend") == "pi_desktop":
        if all(config.get(k) is not None for k in ("provider_id", "model_id", "connection_file")):
            block = {k: config[k] for k in constants.EXEC_PI_DESKTOP_KEYS if k in config}
            probe = _probe_pi_desktop(block, timeout=timeout)
            if not probe["available"]:
                return False, (
                    probe["detail"]
                    or ("PI-Desktop 控制面就绪但缺少必需工具"
                        if not probe["tools_ok"] else "PI-Desktop 控制面不可用")
                )
            return True, None

        block = config.get("pi_desktop")
        if block is None:
            block = {
                key: config.get(key)
                for key in constants.EXEC_PI_DESKTOP_ALL_KEYS
                if config.get(key) is not None
            }
        error = validate_pi_desktop_block(block)
        if error is not None:
            return False, f"pi_desktop 执行配置无效：{error}"

        resolved, err = resolve_runtime_config(conn, block, timeout=timeout)
        if err is not None or resolved is None:
            return False, f"pi_desktop 默认配置解析失败：{err}"

        probe = _probe_pi_desktop(resolved, timeout=timeout)
        if not probe["available"]:
            return False, (
                probe["detail"]
                or ("PI-Desktop 控制面就绪但缺少必需工具"
                    if not probe["tools_ok"] else "PI-Desktop 控制面不可用")
            )
        return True, None

    caps = capabilities(conn, timeout=timeout, config_snapshot=config_snapshot)
    failed = [c for c in caps["checks"] if not c["ok"]]
    if failed:
        detail = "；".join(f"{c['name']}: {c['detail']}" for c in failed)
        return False, f"pi 执行环境不可用（{detail}）"
    error = validate_config(config, caps)
    if error is not None:
        return False, f"pi 执行配置无效：{error}"
    return True, None


# ------------------------------------------------------------- 控制器（v4）

def controller_backend(conn: sqlite3.Connection) -> str:
    """当前生效的控制器后端（§5.24.1）：pi（缺省）或 pi_desktop。"""
    return load(conn)[constants.CONTROLLER_BACKEND_KEY]


def controller_snapshot(conn: sqlite3.Connection) -> dict:
    """控制轮固化快照（§5.24.4 / T7.1 / T7.3）：pi → {backend:"pi"}；
    pi_desktop → {backend:"pi_desktop", 五字段}（脱敏、无 token）。
    """
    config = load(conn)
    if config.get(constants.CONTROLLER_BACKEND_KEY) == "pi_desktop":
        block = config.get("pi_desktop") or {}
        resolved, error = resolve_runtime_config(conn, block)
        if resolved is None:
            raise util.KanpilotError(
                "validation_failed",
                f"pi_desktop 控制端配置快照解析失败：{error or '未知原因'}",
            )
        return {"backend": "pi_desktop", **resolved}
    return {"backend": "pi"}


def _controller_block(config: dict) -> Optional[dict]:
    """控制器分支的五字段块：已保存配置是嵌套块，轮次快照是扁平字段。"""
    block = config.get("pi_desktop")
    if block is None:
        block = {
            key: config.get(key) for key in constants.EXEC_PI_DESKTOP_KEYS
        }
    return block if isinstance(block, dict) else None


def controller_capabilities(
    conn: Optional[sqlite3.Connection] = None, timeout: int = 10, *,
    config_snapshot: Optional[dict] = None,
) -> dict:
    """``exec-capabilities.controller``（§5.24.2）→ {backend, available, detail}。

    - ``pi``（缺省）：沿用 PI CLI 检查（可执行 + 版本）。凭据/模型就绪属任务
      执行检查（``checks``），不在控制器就绪里重复声称。
    - ``pi_desktop``：五字段块已保存 + 结构有效 + 连接探测通过，工具门为
      ``PI_DESKTOP_CONTROLLER_TOOLS``（控制端不建 Goal 会话，故不需要契约
      审批工具）。失败给中文原因，**不**自动回落 pi。
    """
    if config_snapshot is None:
        config = load(conn) if conn is not None else {
            **constants.EXEC_CONFIG_DEFAULTS,
            "backend": "pi",
            constants.CONTROLLER_BACKEND_KEY: "pi",
            "pi_desktop": None,
        }
    elif isinstance(config_snapshot, dict):
        config = dict(config_snapshot)
    else:
        config = {}
    backend = _controller_backend(
        config.get(constants.CONTROLLER_BACKEND_KEY)
    )
    if backend == "pi":
        version, error = _probe_version(timeout)
        return {
            "backend": "pi",
            "available": version is not None,
            "detail": (
                f"Pi CLI {version}（继承本机 PI 默认会话配置）"
                if version is not None
                else (error or "Pi CLI 不可用")
            ),
        }
    block = _controller_block(config)
    error = validate_pi_desktop_block(block)
    if error is not None:
        return {
            "backend": "pi_desktop",
            "available": False,
            "detail": f"pi_desktop 控制端配置无效：{error}",
        }
    probe = _probe_pi_desktop(
        block, timeout=timeout,
        required_tools=constants.PI_DESKTOP_CONTROLLER_TOOLS,
    )
    detail = probe["detail"]
    if probe["available"] and not probe["tools_ok"]:
        # 探测自身已按同一工具门判定；此处只兜底防御，避免「可用」却缺工具。
        detail = detail or "PI-Desktop 控制面缺少控制器必需工具"
    return {
        "backend": "pi_desktop",
        "available": bool(probe["available"] and probe["tools_ok"]),
        "detail": detail or (
            f"PI-Desktop 控制面就绪（协议版本 {probe['version']}）"
            if probe["available"] else "PI-Desktop 控制面不可用"
        ),
    }


def check_controller_available(
    conn: sqlite3.Connection, timeout: int = 10, *,
    config_snapshot: Optional[dict] = None,
) -> Tuple[bool, Optional[str]]:
    """dispatch/monitor 控制端启动前的可用性门（§5.24.4）。

    显式传入的控制轮快照（``controller_snapshot`` 形状，扁平或嵌套块）优先，
    不回读后来被改动的设置；失败 → (False, 中文原因)，调用方按
    ``pi_desktop_controller_unavailable`` 明确失败，**不**自动回退 pi。
    """
    caps = controller_capabilities(
        conn, timeout=timeout, config_snapshot=config_snapshot,
    )
    if caps["available"]:
        return True, None
    return False, caps["detail"]


# 兼容 v3 私有名（外部/旧测试引用）。
_validate_pi_desktop_block = validate_pi_desktop_block
