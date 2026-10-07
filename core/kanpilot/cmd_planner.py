"""规划配置命令层（protocol.md §5.22.6 + §5.23.2，本次修复 T2/T4；v4 §5.24）。

- ``get-planner-config``：已保存配置（或 null）+ 默认值 + 保存时间；v3 起
  config/defaults 均含 ``default_method``（builtin|spec_plan，缺省 builtin）；
  v4 起 config 另含 ``backend``（codex|pi_desktop）与 ``pi_desktop`` 五字段块。
- ``set-planner-config``：逐组校验（planner_config.validate_group）后原子
  保存；``default_method`` 经 validate_method 校验；``backend``/``pi_desktop``
  经 ``planner_config.save`` 分支校验（离线可保存）。**仅影响之后创建的
  计划请求**（且只作 UI 预选；item 的 planner_method 才是请求级方式）。
  Plus 分支不接受 ``spec_plan``（中文原因）。
- ``planner-capabilities``：只读环境能力——CLI 路径/版本、服务商摘要、认证
  就绪、模型元数据及其来源 + ``planner_methods``（builtin=现有 codex
  检查；spec_plan=``codex plugin list`` 结构化确认 + 安装源文件核对）；
  v4 顶层增加 ``backends``：``codex``（沿用旧字段）+ ``pi_desktop``
  （``{available,detail,tools_ok,version,planner_methods}``，工具门 =
  ``PI_DESKTOP_PLAN_TOOLS``）。
  **不发模型请求、不返回凭据值**；Plus 分支不跑 Codex provider 解析或
  PI auth check，失败也不自动降级 codex（§5.24.2）。
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
    v4：config 另含 ``backend`` 与 ``pi_desktop``（None=未保存），
    defaults 的 ``backend`` 为 codex（生效值取缺省）。config=null = 从未保存。
    """
    stored = planner_config.load(ctx.conn)
    updated_at = (
        stored.get("updated_at")
        if isinstance(stored, dict) and stored else None
    )
    config = None
    if isinstance(stored, dict) and stored:
        state = planner_config.merged(ctx.conn)
        config = {
            g: dict(state[g]) for g in planner_config.PLANNER_GROUPS
        }
        config["default_method"] = state["default_method"]
        config["backend"] = state["backend"]
        config["pi_desktop"] = state["pi_desktop"]
        config["claude"] = state["claude"]
    defaults = {g: dict(planner_config.DEFAULTS[g])
                for g in planner_config.PLANNER_GROUPS}
    defaults["default_method"] = planner_config.DEFAULT_METHOD
    defaults["backend"] = planner_config.DEFAULT_BACKEND
    defaults["pi_desktop"] = None
    defaults["claude"] = dict(planner_config.CLAUDE_DEFAULT)
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
    """set-planner-config {standard?, review_fix?, default_method?, backend?,
    pi_desktop?} → {config, updated_at}。

    至少一项；每组必须是含 planner_model/reasoning_effort 的完整对象；
    default_method ∈ DEFAULT_METHODS；backend ∈ PLANNER_BACKENDS；pi_desktop
    经分支校验（五字段 + permission_mode=auto）。校验失败 → bad_request（中文
    原因），不改旧值。Plus 分支不接受 spec_plan——生效方式为 spec_plan 时明确
    失败并提示先改 default_method，不静默改写用户选择。
    """
    groups = {
        g: payload.get(g) for g in planner_config.PLANNER_GROUPS
        if payload.get(g) is not None
    }
    new_method = payload.get("default_method")
    new_backend = payload.get("backend")
    has_backend = new_backend is not None
    new_block = payload.get("pi_desktop")
    has_block = new_block is not None
    new_claude = payload.get("claude")
    if (not groups and new_method is None and not has_backend
            and not has_block and new_claude is None):
        raise util.bad_request(
            "set-planner-config 至少提供一组（standard / review_fix）、"
            "default_method 或 backend"
        )
    if new_method is not None and (
            planner_config.validate_method(new_method) is None):
        raise util.bad_request(
            f"default_method 非法：{new_method!r}"
            f"（仅 {'/'.join(planner_config.DEFAULT_METHODS)}）"
        )
    if has_backend and new_backend not in planner_config.BACKENDS:
        raise util.bad_request(
            f"backend 不支持：{new_backend}"
            f"（可用：{', '.join(planner_config.BACKENDS)}）"
        )
    if has_block and not isinstance(new_block, dict):
        raise util.bad_request("pi_desktop 必须是对象")
    if new_claude is not None:
        error = planner_config.validate_claude_block(new_claude)
        if error:
            raise util.bad_request(error)

    # §5.24.3：Plus 分支只支持 builtin。生效后端/方式都要看（用户可能只提交
    # backend，而已存的 default_method 是 spec_plan）。
    effective_backend = (
        new_backend if has_backend else planner_config.backend(ctx.conn)
    )
    effective_method = (
        new_method if new_method is not None
        else planner_config.default_method(ctx.conn)
    )
    blocked = planner_config.spec_plan_blocked_reason(effective_backend)
    if blocked is not None and effective_method == "spec_plan":
        raise util.bad_request(
            f"{effective_backend} 计划分支不支持 spec_plan：{blocked}；"
            f"请先把 default_method 改为 builtin（或切回 codex）"
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
    if has_backend:
        cleaned["backend"] = new_backend
    if has_block:
        cleaned["pi_desktop"] = new_block
    if new_claude is not None:
        cleaned["claude"] = {"model": new_claude["model"], "effort": new_claude["effort"]}
    saved = planner_config.save(ctx.conn, cleaned, ctx.now_iso)
    detail = {g: dict(saved[g]) for g in planner_config.PLANNER_GROUPS}
    detail["default_method"] = saved.get(
        "default_method") or planner_config.DEFAULT_METHOD
    detail["backend"] = saved.get("backend") or planner_config.DEFAULT_BACKEND
    detail["pi_desktop"] = saved.get("pi_desktop")
    detail["claude"] = saved.get("claude")
    db.add_event(
        ctx.conn, ctx.now_iso, "planner_config_updated", actor="user",
        detail=detail,
    )
    config = {g: dict(saved[g]) for g in planner_config.PLANNER_GROUPS}
    config["default_method"] = detail["default_method"]
    config["backend"] = detail["backend"]
    config["pi_desktop"] = detail["pi_desktop"]
    config["claude"] = detail["claude"]
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
    """planner-capabilities {} → §5.22.6 形状 + v4 ``backends`` 分支。

    顶层沿用旧字段（codex 相关 + planner_methods），``backends.codex`` 与顶层
    同源；``backends.pi_desktop`` 见 ``planner_config.capabilities``（只读、
    不发模型请求、无凭据）。扩展响应保留旧字段（§5.24.2）。
    """
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
    codex_branch = {
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
    result = dict(codex_branch)
    result["backends"] = {
        "codex": codex_branch,
        "pi_desktop": planner_config.capabilities(ctx.conn),
    }
    try:
        probe = subprocess.run([constants.claude_bin(), "--version"],
                               capture_output=True, text=True, timeout=10)
        claude_ok = probe.returncode == 0
        claude_detail = probe.stdout.strip() or probe.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        claude_ok, claude_detail = False, str(exc)
    result["backends"]["claude"] = {
        "available": claude_ok, "path": constants.claude_bin(),
        "version": claude_detail if claude_ok else None,
        "detail": "CLI 可用，认证与模型尚未验证" if claude_ok else claude_detail,
        "planner_methods": {
            "builtin": {"available": claude_ok, "reason": None if claude_ok else claude_detail},
            "spec_plan": {"available": False, "reason": planner_config.spec_plan_blocked_reason("claude")},
        },
    }
    return result
