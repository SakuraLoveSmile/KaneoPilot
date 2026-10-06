"""Codex 服务商解析（protocol.md §5.22.3，v2.1/T1；planner-fix-implementation §1）。

规划子进程以 ``--ignore-user-config`` 运行（不继承插件/MCP/用户规则），因此
``config.toml`` 的 ``model_providers`` 段不会自动生效。本模块从
``constants.codex_home()/config.toml`` 解析当前 ``model_provider``，把服务商
必要连接配置转译为只读本次 ``-c key=value`` 覆写，供
``codex_planner.launcher_argv`` 追加。

凭据规则（冻结）：
- 内联 ``experimental_bearer_token`` 只在内存读取，转为子进程专用环境变量
  ``KANPILOT_CODEX_PROVIDER_KEY``，并以 ``-c model_providers.<id>.env_key``
  引用；token **绝不**进入 argv、summary、SQLite 或日志。
- 配置里显式 ``env_key`` 的 provider 原样沿用变量名，``env={}``（父进程
  环境已透传给子进程）。
- 认证方式缺失 / 配置损坏 / provider 引用不存在 →
  ``util.KanpilotError("validation_failed", ...)``，**禁止静默回落官方服务商**。

实测结论（codex-cli 0.157.0，loopback Responses stub）：
- ``requires_openai_auth=true`` + ``env_key`` 并用时 env_key 生效：子进程以
  ``Authorization: Bearer <env var 值>`` 请求自定义 base_url；该标志
  不阻止 env_key（auth_mode=ApiKey），按配置真实值透传即可。
- ``env_key`` 已配置但环境变量缺失 → 子进程报 ``Missing environment
  variable: <NAME>`` 并失败，**不会**回落 auth.json。
- ``--ignore-user-config`` 下 codex 仍会发起插件目录同步的外网请求
  （chatgpt.com/github.com，feature.remote_plugin 默认开启），与服务商
  链路无关；如需完全离线须在调用方另行处置（本模块不负责）。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Vendored tomli（Python 3.9/3.10 无 tomllib）；与 util.py 同法注入
# core/_vendor 再 import（冻结契约 §1 允许之一）。
_VENDOR = Path(__file__).resolve().parent.parent / "_vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
try:
    import tomllib  # noqa: E402  # Python >= 3.11
except ImportError:  # pragma: no cover - 3.9/3.10 路径
    import tomli as tomllib  # noqa: E402

from . import util  # noqa: E402

CONFIG_NAME = "config.toml"
AUTH_NAME = "auth.json"
PROVIDER_ENV_KEY = "KANPILOT_CODEX_PROVIDER_KEY"

# http_headers/env_http_headers/query_params 里按名字判敏感的键
# （小写匹配；下划线与连字符等价）。凭据类字段值绝不进入 config_args。
_SECRET_FIELD_TOKENS = (
    "authorization", "api-key", "apikey", "api_key",
    "token", "secret", "key", "password", "bearer",
)


@dataclass
class ProviderResolution:
    """服务商解析结果（冻结字段，planner-fix-implementation §1）。

    ``env`` 只含须注入子进程的变量（内联 token 的落点）；``config_args``
    每项是一个 ``key=value`` TOML 覆写片段（不含 ``-c`` 前缀）。
    """

    provider_id: Optional[str] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    wire_api: Optional[str] = None
    requires_openai_auth: bool = False
    env_key: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    config_args: List[str] = field(default_factory=list)
    auth_ready: bool = False
    auth_detail: str = ""
    model_catalog_json: Optional[str] = None
    summary: Dict[str, Any] = field(default_factory=dict)


def _toml_literal(value: Any) -> str:
    """单值 → TOML 字面量（字符串经 JSON dump 即合法 TOML 基本串）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    raise ValueError(f"unsupported TOML literal: {type(value).__name__}")


def _toml_inline_table(mapping: dict) -> str:
    return "{ " + ", ".join(
        f"{key} = {_toml_literal(val)}" for key, val in mapping.items()
    ) + " }"


def _is_sensitive_name(name: Any) -> bool:
    lowered = str(name).strip().lower().replace("_", "-")
    return any(token in lowered for token in _SECRET_FIELD_TOKENS)


def _fail(message: str) -> util.KanpilotError:
    return util.KanpilotError("validation_failed", message)


def _default_resolution(codex_home: Path,
                        catalog: Optional[str]) -> ProviderResolution:
    """未配置自定义服务商：Codex 默认认证路径（auth.json 存在性探测）。"""
    has_auth = (codex_home / AUTH_NAME).is_file()
    detail = (
        f"默认认证路径：{'已' if has_auth else '未'}发现 "
        f"{codex_home / AUTH_NAME}"
        + ("" if has_auth else "（codex 未登录）")
    )
    summary = {
        "provider_id": None,
        "name": None,
        "base_url": None,
        "wire_api": None,
        "requires_openai_auth": False,
        "env_key": None,
        "auth_ready": has_auth,
        "auth_detail": detail,
        "model_catalog_json": catalog,
    }
    return ProviderResolution(
        provider_id=None,
        auth_ready=has_auth,
        auth_detail=detail,
        model_catalog_json=catalog,
        config_args=(
            [f"model_catalog_json={_toml_literal(catalog)}"]
            if catalog else []
        ),
        summary=summary,
    )


def _resolve_auth(provider: dict, codex_home: Path):
    """凭据三态裁决 → (env_key, env, auth_ready, auth_detail)。

    a) 显式 env_key：原样采用；父进程环境已透传故 env={}。变量无值时
       auth_ready=False——实测 codex-cli 0.157.0：env_key 已配置而环境
       变量缺失时子进程直接报 `Missing environment variable: <NAME>`，
       **不回落** auth.json，因此不做「备选就绪」误判。
    b) 内联 experimental_bearer_token：固定 env_key=
       KANPILOT_CODEX_PROVIDER_KEY，token 只进 env（内存），不进 argv。
    c) 仅 requires_openai_auth：走 auth.json 登录态探测。
    d) 全无认证方式 → KanpilotError。
    """
    raw_env_key = provider.get("env_key")
    configured_env_key = (
        raw_env_key.strip()
        if isinstance(raw_env_key, str) and raw_env_key.strip()
        else None
    )
    raw_token = provider.get("experimental_bearer_token")
    inline_token = (
        raw_token if isinstance(raw_token, str) and raw_token else None
    )
    requires_auth = bool(provider.get("requires_openai_auth"))
    auth_json = codex_home / AUTH_NAME
    has_auth_json = auth_json.is_file()

    if configured_env_key:
        if os.environ.get(configured_env_key):
            detail = f"凭据经环境变量 {configured_env_key} 提供（已就位）"
            return configured_env_key, {}, True, detail
        detail = (
            f"环境变量 {configured_env_key} 未设值（codex 不会回落 "
            "auth.json，需先在环境中提供该变量）"
        )
        return configured_env_key, {}, False, detail

    if inline_token:
        detail = (
            f"内联 bearer token 已读取（注入 {PROVIDER_ENV_KEY}，不落盘）"
        )
        return PROVIDER_ENV_KEY, {PROVIDER_ENV_KEY: inline_token}, True, detail

    if requires_auth:
        if has_auth_json:
            detail = f"requires_openai_auth=true，使用 {auth_json} 登录态"
            return None, {}, True, detail
        detail = (
            f"requires_openai_auth=true，但缺少 {auth_json}（codex 未登录）"
        )
        return None, {}, False, detail

    raise _fail(
        "服务商认证方式缺失：provider 无 env_key、无 "
        "experimental_bearer_token，且 requires_openai_auth 未启用"
    )


def resolve_provider(codex_home: Path) -> ProviderResolution:
    """解析 ``<codex_home>/config.toml`` 的当前服务商 → ProviderResolution。

    只读操作；不写任何文件。失败一律 ``KanpilotError("validation_failed")``
    明确报错，绝不静默回落官方服务商（protocol §5.22.3）。
    """
    codex_home = Path(codex_home)
    config_path = codex_home / CONFIG_NAME
    if not config_path.is_file():
        return _default_resolution(codex_home, None)

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(f"无法读取 {config_path}：{exc}")
    except tomllib.TOMLDecodeError as exc:
        raise _fail(f"{config_path} TOML 解析失败：{exc}")
    if not isinstance(config, dict):
        raise _fail(f"{config_path} 结构非法：顶层不是表")

    catalog_raw = config.get("model_catalog_json")
    catalog = (
        catalog_raw if isinstance(catalog_raw, str) and catalog_raw else None
    )

    raw_provider = config.get("model_provider")
    if raw_provider is None or (
        isinstance(raw_provider, str) and not raw_provider.strip()
    ):
        return _default_resolution(codex_home, catalog)

    # model_provider 支持字符串引用 model_providers.<id>，也支持内联表直写
    #（codex 内置语法 `model_provider = { name = ..., base_url = ... }`）。
    if isinstance(raw_provider, dict):
        provider = raw_provider
        provider_name = provider.get("name")
        provider_id = (
            provider_name.strip()
            if isinstance(provider_name, str) and provider_name.strip()
            else "inline"
        )
    elif isinstance(raw_provider, str):
        provider_id = raw_provider.strip()
        providers = config.get("model_providers")
        if providers is None:
            raise _fail(
                f"model_provider={provider_id}，但 config.toml 缺少 "
                "model_providers 段"
            )
        if not isinstance(providers, dict):
            raise _fail("config.toml 结构非法：model_providers 不是表")
        provider = providers.get(provider_id)
        if not isinstance(provider, dict):
            raise _fail(
                f"model_provider 指向不存在的服务商 "
                f"model_providers.{provider_id}"
            )
    else:
        raise _fail(
            f"config.toml 结构非法：model_provider 类型 "
            f"{type(raw_provider).__name__} 不受支持"
        )

    name = provider.get("name")
    base_url = provider.get("base_url")
    wire_api = provider.get("wire_api")
    requires_auth = bool(provider.get("requires_openai_auth"))

    if not isinstance(name, str) or not name.strip():
        raise _fail(f"model_providers.{provider_id} 缺少 name")
    if not isinstance(base_url, str) or not base_url.strip():
        raise _fail(f"model_providers.{provider_id} 缺少 base_url")
    if not isinstance(wire_api, str) or not wire_api.strip():
        raise _fail(f"model_providers.{provider_id} 缺少 wire_api")

    env_key, env, auth_ready, auth_detail = _resolve_auth(provider, codex_home)

    prefix = f"model_providers.{provider_id}"
    config_args = [
        f"model_provider={_toml_literal(provider_id)}",
        f"{prefix}.name={_toml_literal(name.strip())}",
        f"{prefix}.base_url={_toml_literal(base_url.strip())}",
        f"{prefix}.wire_api={_toml_literal(wire_api.strip())}",
        f"{prefix}.requires_openai_auth="
        f"{'true' if requires_auth else 'false'}",
    ]
    if env_key:
        config_args.append(f"{prefix}.env_key={_toml_literal(env_key)}")

    # 可选透传非敏感字段：query_params（表）与 http_headers/env_http_headers
    #（表，按头部名过滤认证类条目；env_http_headers 的值是环境变量名，
    # 名字敏感的整项跳过，不猜其指向）。
    query_params = provider.get("query_params")
    if isinstance(query_params, dict) and query_params:
        safe_params = {
            k: v for k, v in query_params.items()
            if isinstance(k, str) and not _is_sensitive_name(k)
            and isinstance(v, (str, int, float, bool))
        }
        if safe_params:
            config_args.append(
                f"{prefix}.query_params={_toml_inline_table(safe_params)}"
            )
    for header_key in ("http_headers", "env_http_headers"):
        headers = provider.get(header_key)
        if not isinstance(headers, dict) or not headers:
            continue
        safe_headers = {
            k: v for k, v in headers.items()
            if isinstance(k, str) and not _is_sensitive_name(k)
            and isinstance(v, (str, int, float, bool))
        }
        if safe_headers:
            config_args.append(
                f"{prefix}.{header_key}={_toml_inline_table(safe_headers)}"
            )

    if catalog:
        config_args.append(f"model_catalog_json={_toml_literal(catalog)}")

    summary = {
        "provider_id": provider_id,
        "name": name.strip(),
        "base_url": base_url.strip(),
        "wire_api": wire_api.strip(),
        "requires_openai_auth": requires_auth,
        "env_key": env_key,
        "auth_ready": auth_ready,
        "auth_detail": auth_detail,
        "model_catalog_json": catalog,
    }
    return ProviderResolution(
        provider_id=provider_id,
        name=name.strip(),
        base_url=base_url.strip(),
        wire_api=wire_api.strip(),
        requires_openai_auth=requires_auth,
        env_key=env_key,
        env=env,
        config_args=config_args,
        auth_ready=auth_ready,
        auth_detail=auth_detail,
        model_catalog_json=catalog,
        summary=summary,
    )


def model_catalog(path) -> List[dict]:
    """只读解析 ``model_catalog_json`` 指向的模型目录 → 摘要条目。

    每条 ``{id, name, reasoning_efforts}``：id=slug、
    name=display_name（缺省回落 slug）、reasoning_efforts=
    supported_reasoning_levels[].effort。文件缺失/解析失败/结构非法
    一律返回 ``[]``（不抛错，由 capabilities 标「未验证」）。
    """
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return []
    catalog: List[dict] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        display = entry.get("display_name")
        levels = entry.get("supported_reasoning_levels")
        efforts = [
            level["effort"]
            for level in levels
            if isinstance(level, dict)
            and isinstance(level.get("effort"), str)
            and level["effort"]
        ] if isinstance(levels, list) else []
        catalog.append({
            "id": slug,
            "name": display if isinstance(display, str) and display else slug,
            "reasoning_efforts": efforts,
        })
    return catalog
