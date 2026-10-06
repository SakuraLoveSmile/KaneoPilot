"""规划配置命令层（protocol.md §5.22.6 + §5.23.2，本次修复 T2/T4）。

- ``get-planner-config``：已保存配置（或 null）+ 默认值 + 保存时间；
  v3 起 config/defaults 均含 ``default_method``（builtin|spec_plan，
  缺省 builtin）。
- ``set-planner-config``：逐组校验（planner_config.validate_group）后原子
  保存；``default_method`` 经 validate_method 校验。**仅影响之后创建的
  计划请求**（且只作 UI 预选；item 的 planner_method 才是请求级方式）。
- ``planner-capabilities``：只读环境能力——CLI 路径/版本、服务商摘要、认证
  就绪、模型元数据及其来源 + ``planner_methods``（builtin=现有 codex
  检查；spec_plan=``codex plugin list`` 结构化确认 + 安装源文件核对）。
  **不发模型请求、不返回凭据值**。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import codex_planner, constants, db, planner_config, util


def _provider_module():
    """惰性导入 codex_provider（T1 模块；未就绪时调用方按失败处理）。"""
    from . import codex_provider

    return codex_provider

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def cmd_get_planner_config(ctx, payload: dict) -> dict:
    """get-planner-config {} → {config|null, defaults, updated_at}。

    v3：config（已保存时）与 defaults 均含 ``default_method``；
    config=null = 从未保存（生效值=defaults，default_method=builtin）。
    """
    stored = planner_config.load(ctx.conn)
    updated_at = stored.get("updated_at") if isinstance(stored, dict) else None
    config = (
        {g: stored[g] for g in planner_config.PLANNER_GROUPS
         if isinstance(stored.get(g), dict)}
        if stored else None
    )
    if config is not None:
        method = planner_config.validate_method(
            stored.get("default_method")) or planner_config.DEFAULT_METHOD
        config["default_method"] = method
    defaults = {g: dict(planner_config.DEFAULTS[g])
                for g in planner_config.PLANNER_GROUPS}
    defaults["default_method"] = planner_config.DEFAULT_METHOD
    return {
        "config": config,
        "defaults": defaults,
        "updated_at": updated_at,
    }


def _catalog(codex_home: Path) -> tuple:
    """(catalog, models_source)。目录不可读 → ([], path-or-None)。"""
    try:
        provider = _provider_module()
        resolution = provider.resolve_provider(codex_home)
    except util.KanpilotError:
        return [], None
    except ImportError:
        return [], None
    catalog_path = resolution.model_catalog_json
    if not catalog_path:
        return [], None
    return provider.model_catalog(catalog_path), catalog_path


def cmd_set_planner_config(ctx, payload: dict) -> dict:
    """set-planner-config {standard?, review_fix?, default_method?} →
    {config, updated_at}。

    至少一项；每组必须是含 planner_model/reasoning_effort 的完整对象；
    default_method ∈ DEFAULT_METHODS。校验失败 → bad_request（中文原因），
    不改旧值。
    """
    groups = {
        g: payload.get(g) for g in planner_config.PLANNER_GROUPS
        if payload.get(g) is not None
    }
    new_method = payload.get("default_method")
    if not groups and new_method is None:
        raise util.bad_request(
            "set-planner-config 至少提供一组（standard / review_fix）"
            "或 default_method"
        )
    if new_method is not None and (
            planner_config.validate_method(new_method) is None):
        raise util.bad_request(
            f"default_method 非法：{new_method!r}"
            f"（仅 {'/'.join(planner_config.DEFAULT_METHODS)}）"
        )
    catalog, _src = _catalog(constants.codex_home())
    cleaned = {}
    for group, section in groups.items():
        error = planner_config.validate_group(section, catalog)
        if error is not None:
            raise util.bad_request(f"{group} 配置校验失败：{error}")
        cleaned[group] = {
            "planner_model": planner_config.validate_model_id(
                section["planner_model"]
            ),
            "reasoning_effort": section["reasoning_effort"].strip(),
        }
    if new_method is not None:
        cleaned["default_method"] = new_method
    saved = planner_config.save(ctx.conn, cleaned, ctx.now_iso)
    db.add_event(
        ctx.conn, ctx.now_iso, "planner_config_updated", actor="user",
        detail={g: cleaned[g] for g in cleaned},
    )
    config = {g: saved[g] for g in planner_config.PLANNER_GROUPS}
    config["default_method"] = saved.get(
        "default_method") or planner_config.DEFAULT_METHOD
    return {
        "config": config,
        "updated_at": saved["updated_at"],
    }


def _probe_codex_version(timeout: int = 10) -> tuple:
    """(version|None, detail)。codex --version 探测（同 exec_config 口径）。"""
    binary = constants.codex_bin()
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"codex 可执行不存在：{binary}"
    except subprocess.TimeoutExpired:
        return None, f"codex --version 超时（{timeout}s）"
    except OSError as exc:
        return None, f"codex --version 执行失败：{exc}"
    for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
        match = _VERSION_RE.search(line.strip())
        if match:
            return match.group(0), None
    return None, f"codex --version 退出码 {proc.returncode}"


def cmd_planner_capabilities(ctx, payload: dict) -> dict:
    """planner-capabilities {} → §5.22.6 形状；不发模型请求、不输出凭据。"""
    codex_home = constants.codex_home()
    version, _verr = _probe_codex_version()
    provider_summary = None
    auth = {"ready": False, "detail": "codex 配置不可用"}
    try:
        provider = _provider_module()
        resolution = provider.resolve_provider(codex_home)
        provider_summary = resolution.summary
        auth = {
            "ready": bool(resolution.auth_ready),
            "detail": resolution.auth_detail,
        }
        if resolution.provider_id is None:
            # 默认路径：沿用既有 auth.json 存在性语义。
            auth_json = codex_home / "auth.json"
            auth = {
                "ready": auth_json.is_file(),
                "detail": (
                    f"默认认证路径（{auth_json.name} "
                    f"{'存在' if auth_json.is_file() else '缺失'}）"
                ),
            }
            provider_summary = None
    except util.KanpilotError as exc:
        auth = {"ready": False, "detail": f"codex 配置错误：{exc.message}"}
    except ImportError:
        auth = {"ready": False, "detail": "codex_provider 未安装"}
    catalog, catalog_source = _catalog(codex_home)
    # §5.23.2 planner_methods：builtin=现有 codex 检查；spec_plan=
    # codex plugin list 结构化确认 + 安装源 manifest/SKILL.md 核对。
    builtin_ok, builtin_detail = codex_planner.check_available()
    spec_probe = codex_planner.probe_spec_plugin()
    spec_entry = {"available": bool(spec_probe.get("available")),
                  "reason": spec_probe.get("reason")}
    if spec_probe.get("plugin_id"):
        spec_entry["plugin_id"] = spec_probe["plugin_id"]
    if spec_probe.get("version"):
        spec_entry["version"] = spec_probe["version"]
    return {
        "codex": {
            "path": constants.codex_bin(),
            "version": version,
        },
        "provider": provider_summary,
        "auth": auth,
        "models": catalog,
        "models_source": catalog_source,
        "models_verified": bool(catalog),
        "planner_methods": {
            "builtin": {
                "available": bool(builtin_ok),
                "reason": None if builtin_ok else builtin_detail,
            },
            "spec_plan": spec_entry,
        },
    }
