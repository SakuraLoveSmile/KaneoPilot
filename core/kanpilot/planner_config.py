"""Codex 规划配置（protocol.md §5.22.6 + §5.23.2，本次修复 T2）。

独立的 ``settings.planner_config``——**不复用** pi 的 ``exec_config``，不新增
数据库表。两组键：``standard`` / ``review_fix``，每组
``{"planner_model", "reasoning_effort"}``；外加 ``default_method``
（v3，施工规划方式的 UI 默认预选，``builtin|spec_plan``，缺省
``builtin``——请求级方式仍以 ``start-plans.items[].planner_method``
为准，item 缺省时按 ``builtin`` 解释，default_method 不改既有请求）。

- ``load``：返回原始保存值（无 → None）；``snapshot``：合并默认后的生效值
  （只含两组模型字段，default_method 不进请求快照）。
- ``save``：原子写回（整组替换 + default_method + updated_at）；调用前
  须过 ``validate_group``/``validate_method``。
- ``validate_group``/``validate_model_id``：模型 ID 去空白、拒空/控制字符；
  已知模型按 catalog 元数据校验思考等级；catalog 为空（未验证）或模型不在
  清单时放行（仅「能力未验证」提示由展示层负责），不擅自换模型/思考等级。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from . import constants, db

PLANNER_GROUPS = ("standard", "review_fix")

# v3 §5.23.2：施工规划方式词表与缺省值。
DEFAULT_METHODS = ("builtin", "spec_plan")
DEFAULT_METHOD = "builtin"

DEFAULTS = {
    "standard": dict(constants.STANDARD_PLANNER),
    "review_fix": dict(constants.REVIEW_FIX_PLANNER),
}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def group_for(plan_kind: Optional[str]) -> str:
    """plan_kind → 配置组名（None/standard → standard）。"""
    return "review_fix" if plan_kind == "review_fix" else "standard"


def _clean(value) -> Optional[str]:
    """模型/思考等级字符串规范化：去首尾空白；非 str/空/含控制字符 → None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _CONTROL_RE.search(text):
        return None
    return text


def validate_model_id(value) -> Optional[str]:
    """返回规范化的模型 ID；不合法 → None（调用方给中文错误）。"""
    return _clean(value)


def load(conn: sqlite3.Connection) -> Optional[dict]:
    """settings.planner_config 原始值（dict）或 None（从未保存）。"""
    stored = db.get_setting(conn, "planner_config")
    return stored if isinstance(stored, dict) else None


def _merged(conn: sqlite3.Connection) -> dict:
    """已存值覆盖默认（逐组逐字段；只认契约两字段 + default_method）。"""
    stored = load(conn) or {}
    merged = {group: dict(DEFAULTS[group]) for group in PLANNER_GROUPS}
    merged["default_method"] = DEFAULT_METHOD
    for group in PLANNER_GROUPS:
        section = stored.get(group)
        if not isinstance(section, dict):
            continue
        for key in ("planner_model", "reasoning_effort"):
            value = _clean(section.get(key))
            if value:
                merged[group][key] = value
    stored_method = stored.get("default_method")
    if stored_method in DEFAULT_METHODS:
        merged["default_method"] = stored_method
    return merged


def snapshot(conn: sqlite3.Connection, group: str) -> dict:
    """某配置组的生效 {planner_model, reasoning_effort}（写入请求快照用）。

    default_method 属请求级/UI 预选字段，不进快照（§5.23.2）。
    """
    merged = _merged(conn)
    base = merged.get(group) or merged["standard"]
    return {
        "planner_model": base["planner_model"],
        "reasoning_effort": base["reasoning_effort"],
    }


def default_method(conn: sqlite3.Connection) -> str:
    """生效的默认规划方式（builtin|spec_plan；未保存/非法值按 builtin）。"""
    return _merged(conn)["default_method"]


def save(conn: sqlite3.Connection, groups: dict, now: str) -> dict:
    """原子保存整组配置（validated 后调用；groups 可含 default_method）。
    返回含 updated_at 的完整值。"""
    stored = _merged(conn)
    for group in PLANNER_GROUPS:
        section = groups.get(group)
        if not isinstance(section, dict):
            continue
        stored[group] = {
            "planner_model": section["planner_model"],
            "reasoning_effort": section["reasoning_effort"],
        }
    method = groups.get("default_method")
    if method in DEFAULT_METHODS:
        stored["default_method"] = method
    stored["updated_at"] = now
    db.set_setting(conn, "planner_config", stored)
    return stored


def validate_method(value) -> Optional[str]:
    """default_method 校验：∈ DEFAULT_METHODS → 原值；否则 None。"""
    if isinstance(value, str) and value in DEFAULT_METHODS:
        return value
    return None


def validate_group(config: dict, catalog: Optional[list]) -> Optional[str]:
    """校验单组配置；返回中文错误或 None。

    ``catalog`` 为 codex_provider.model_catalog 结果（元素含
    ``id``/``reasoning_efforts``）。catalog 为空 = 元数据不可用 → 仅格式校验、
    放行（「能力未验证」由 UI 显示）；模型在 catalog 中 → 思考等级必须命中。
    """
    if not isinstance(config, dict):
        return "配置组必须是对象"
    model = _clean(config.get("planner_model"))
    if model is None:
        return "planner_model 为空或含非法字符（首尾空白/控制字符不允许）"
    effort = _clean(config.get("reasoning_effort"))
    if effort is None:
        return "reasoning_effort 为空或含非法字符"
    if catalog:
        entry = next(
            (m for m in catalog if m.get("id") == model), None
        )
        if entry is not None:
            efforts = entry.get("reasoning_efforts") or []
            if efforts and effort not in efforts:
                return (
                    f"模型 {model} 不支持思考等级 {effort}"
                    f"（支持：{', '.join(efforts)}）"
                )
    return None
