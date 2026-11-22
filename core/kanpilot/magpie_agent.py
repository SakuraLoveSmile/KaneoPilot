"""Magpie 接管 KaneoPilot 的模型入口（protocol §5.33，kp-mag01）。

KaneoPilot 自己不再保存 base URL / API Key：Magpie 的 Agents 页给
「KaneoPilot」选中模型后，插件把网关地址、网关 Key 与模型 id 写进
``~/Library/Application Support/KaneoPilot/agent.json``（与 TaskBoard 的
agent.json 同形）。本模块只做三件事：读这份文件、判断是否接管、给出脱敏
状态。

文件形状（Magpie 写）::

    {"baseURL": "http://127.0.0.1:3425/v1",
     "apiKey": "<magpie 为 KaneoPilot 单发的网关 key>",
     "model": "opencode-go/deepseek-v4.1-flash"}

裁决规则（冻结）：

- 文件不存在 → 未接管，行为与 §5.22/§5.23 完全一致；
- 文件存在但 baseURL/model 缺失、或 baseURL 不是本机回环地址 → **未接管**，
  ``status()["reason"]`` 给出原因，绝不用半份配置发请求；
- apiKey 可缺省（Magpie 回环网关默认不校验），缺省时 ``has_key=False``，
  子进程不带 Authorization；
- ``KANPILOT_MAGPIE_AGENT`` 覆盖文件路径（隔离测试与自建目录）。

本模块任何返回值都不含 key 值本身：``load()`` 的 ``api_key`` 只供子进程
环境使用，快照/命令层一律用 ``status()`` 的脱敏视图。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

# 覆盖 agent.json 路径（测试与自建目录）。
ENV_PATH = "KANPILOT_MAGPIE_AGENT"
# 默认位置：与 Magpie 插件写入位置一致。
DEFAULT_PARTS = ("Library", "Application Support", "KaneoPilot", "agent.json")
# Magpie 网关只应经本机回环访问；远端地址一律不接管。
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
# Codex 以 Responses API 对接 Magpie 的 /v1/responses。
WIRE_API = "responses"
# 说明用：管理这份文件的 Magpie 页面。
SOURCE_LABEL = "Magpie Agents 页"


def agent_path() -> Path:
    """生效的 agent.json 路径（环境变量优先）。"""
    override = os.environ.get(ENV_PATH)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return Path.home().joinpath(*DEFAULT_PARTS)


def _clean(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_base_url(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """校验并规范化网关地址 → (base_url, 错误原因)。"""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return None, f"baseURL 不是 http(s) 地址：{raw}"
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        return None, f"baseURL 只接受本机回环地址，收到 {host or raw}"
    return raw.rstrip("/"), None


def _read(target: Path):
    """读文件 → (原始 dict, 错误原因)；文件不存在时返回 (None, None)。"""
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"读取 {target} 失败：{exc}"
    try:
        data = json.loads(text)
    except ValueError:
        return None, f"{target} 不是合法 JSON"
    if not isinstance(data, dict):
        return None, f"{target} 顶层必须是对象"
    return data, None


def _normalize(data: dict, target: Path):
    base_raw = (_clean(data.get("baseURL")) or _clean(data.get("baseUrl"))
                or _clean(data.get("base_url")))
    if not base_raw:
        return None, "agent.json 缺少 baseURL"
    base_url, error = _normalize_base_url(base_raw)
    if error:
        return None, error
    model = _clean(data.get("model"))
    if not model:
        return None, "agent.json 缺少 model"
    return {
        "base_url": base_url,
        "api_key": _clean(data.get("apiKey")) or _clean(data.get("api_key")),
        "model": model,
        "path": str(target),
        "wire_api": WIRE_API,
    }, None


def load(path=None) -> Optional[dict]:
    """接管生效时返回规范化配置，否则 None。

    只读；不写任何文件。返回的 dict 含 api_key，只许交给子进程环境。
    """
    target = Path(path).expanduser() if path else agent_path()
    data, _error = _read(target)
    if data is None:
        return None
    normalized, _reason = _normalize(data, target)
    return normalized


def status(path=None) -> dict:
    """脱敏状态（命令层/UI 用）：active / model / base_url / has_key / reason。"""
    target = Path(path).expanduser() if path else agent_path()
    data, error = _read(target)
    result = {
        "active": False,
        "path": str(target),
        "source": SOURCE_LABEL,
        "base_url": None,
        "model": None,
        "has_key": False,
        "wire_api": WIRE_API,
        "present": target.is_file(),
        "reason": None,
    }
    if data is None:
        result["reason"] = error or "未接管：默认位置没有 agent.json"
        return result
    normalized, normalize_error = _normalize(data, target)
    if normalized is None:
        result["reason"] = normalize_error
        return result
    result.update(
        active=True,
        base_url=normalized["base_url"],
        model=normalized["model"],
        has_key=bool(normalized["api_key"]),
    )
    return result


def model_override() -> Optional[str]:
    """接管生效时的规划/评审模型 id；否则 None。"""
    agent = load()
    return agent["model"] if agent else None


def anthropic_base() -> Optional[str]:
    """Claude CLI 的 ANTHROPIC_BASE_URL：Magpie 网关根（去掉 /v1）。"""
    agent = load()
    if not agent:
        return None
    base = agent["base_url"]
    return base[:-3].rstrip("/") if base.endswith("/v1") else base
