"""规划配置（protocol.md §5.22.6 + §5.23.2，v4 后端扩展 §5.24.1/§5.24.2）。

独立的 ``settings.planner_config``——**不复用** pi 的 ``exec_config``，不新增
数据库表。四组键：

- ``standard`` / ``review_fix``：Codex 规划模型组，每组
  ``{"planner_model", "reasoning_effort"}``；
- ``default_method``（v3）：施工规划方式的 UI 默认预选（``builtin|spec_plan``，
  缺省 ``builtin``）——请求级方式仍以 ``start-plans.items[].planner_method``
  为准，item 缺省时按 ``builtin`` 解释，default_method 不改既有请求；
- ``backend``（v4）：``codex``（缺省）| ``pi_desktop``；缺字段按 ``codex``
  解释（旧数据零迁移），切换后端不删除另一分支已存字段；
- ``pi_desktop``（v4）：五字段块（复用 ``exec_config`` 的键与校验），
  ``permission_mode`` 固定 ``ask``（Plus 计划分支为 ``mode=plan``；Plan 模式
  不是 OS 只读沙箱，只有 ask 能拦住自动 shell 写入）。

``snapshot`` 按生效 backend 返回对应分支的**脱敏**快照（无 token）：codex →
``{planner_model, reasoning_effort}``（写入请求 payload 的 ``model_config``）；
pi_desktop → 五字段（写入 ``runtime_config``）。Plus 分支只支持
``builtin``：``spec_plan`` 是 Codex 插件运行方式，在 Plus 下明确禁用并给原因。
"""

from __future__ import annotations

import re
import sqlite3

from . import constants, db, exec_config, util

PLANNER_GROUPS = ("standard", "review_fix")

# v3 §5.23.2：施工规划方式词表与缺省值。
DEFAULT_METHODS = ("builtin", "spec_plan")
DEFAULT_METHOD = "builtin"

# v4 §5.24.1：规划后端词表与缺省值。
BACKENDS = constants.PLANNER_BACKENDS
DEFAULT_BACKEND = constants.PLANNER_BACKEND
# Plus 计划分支只允许该权限模式（§5.24：不开放 auto/accept-edits）。
PI_DESKTOP_PERMISSION_MODE = constants.PI_DESKTOP_READONLY_PERMISSION_MODE
# Plus 下 spec_plan 明确禁用的中文原因（保存校验与能力查询共用，避免两处措辞漂移）。
SPEC_PLAN_DISABLED_REASON = (
    "spec_plan 是 Codex 插件运行方式，Plus 计划分支只支持 builtin"
    "（不把技能文本复制给 Plus 后声称插件实际运行）"
)

DEFAULTS = {
    "standard": dict(constants.STANDARD_PLANNER),
    "review_fix": dict(constants.REVIEW_FIX_PLANNER),
}
CLAUDE_DEFAULT = {"model": "sonnet", "effort": "high"}
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

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


def merged(conn: sqlite3.Connection) -> dict:
    """已存值覆盖默认（逐组逐字段；只认契约字段 + default_method + v4
    后端字段）。缺 ``backend`` 按 ``codex``、缺 ``pi_desktop`` 块按未保存读。"""
    stored = load(conn) or {}
    result = {group: dict(DEFAULTS[group]) for group in PLANNER_GROUPS}
    result["default_method"] = DEFAULT_METHOD
    for group in PLANNER_GROUPS:
        section = stored.get(group)
        if not isinstance(section, dict):
            continue
        for key in ("planner_model", "reasoning_effort"):
            value = _clean(section.get(key))
            if value:
                result[group][key] = value
    stored_method = stored.get("default_method")
    if stored_method in DEFAULT_METHODS:
        result["default_method"] = stored_method
    backend = stored.get("backend")
    result["backend"] = backend if backend in BACKENDS else DEFAULT_BACKEND
    result["pi_desktop"] = exec_config.merge_pi_desktop_block(
        stored.get("pi_desktop")
    )
    result["claude"] = dict(CLAUDE_DEFAULT, **(stored.get("claude") or {}))
    return result


def backend(conn: sqlite3.Connection) -> str:
    """生效的规划后端（§5.24.1）：codex（缺省）或 pi_desktop。"""
    return merged(conn)["backend"]


def pi_desktop_block(conn: sqlite3.Connection) -> Optional[dict]:
    """生效的 Plus 五字段块；未保存 → None。"""
    return merged(conn)["pi_desktop"]


def model_snapshot(conn: sqlite3.Connection, group: str, *, backend="codex") -> dict:
    """Codex 分支快照：某配置组的生效 {planner_model, reasoning_effort}。
    default_method 属请求级/UI 预选字段，不进快照（§5.23.2）。"""
    state = merged(conn)
    if backend == "claude":
        return {"planner_model": state["claude"]["model"],
                "reasoning_effort": state["claude"]["effort"]}
    base = state.get(group) or state["standard"]
    return {
        "planner_model": base["planner_model"],
        "reasoning_effort": base["reasoning_effort"],
    }


def resolve_runtime_config(
    conn: sqlite3.Connection,
    block: Optional[dict] = None,
    timeout: int = 10,
) -> tuple:
    """解析脱敏的 5 字段 runtime_config 快照（permission_mode 固定 ask）。"""
    if block is None:
        block = pi_desktop_block(conn)
    if not isinstance(block, dict) or not block:
        return None, "Plus 计划分支配置未保存"
    b = dict(block)
    b["permission_mode"] = PI_DESKTOP_PERMISSION_MODE
    return exec_config.resolve_runtime_config(conn, b, timeout=timeout)


def runtime_snapshot(conn: sqlite3.Connection) -> dict:
    """Plus 分支快照：五字段**脱敏**值（无 token/连接文件全文；只有路径）。
    inherit 模式解析当前 Plus 默认配置，固定 permission_mode=ask。
    """
    resolved, error = resolve_runtime_config(conn)
    if resolved is None:
        raise util.KanpilotError(
            "validation_failed",
            f"Plus 规划运行配置快照解析失败：{error or '未知原因'}",
        )
    return resolved

def snapshot(conn: sqlite3.Connection, group: str) -> dict:
    """按生效 backend 返回该请求要固化的运行快照（§5.24.3）。

    codex → ``{planner_model, reasoning_effort}``（payload ``model_config``）；
    pi_desktop → 五字段（payload ``runtime_config``，无 token）。
    """
    if backend(conn) == "pi_desktop":
        return runtime_snapshot(conn)
    return model_snapshot(conn, group, backend=backend(conn))


def default_method(conn: sqlite3.Connection) -> str:
    """生效的默认规划方式（builtin|spec_plan；未保存/非法值按 builtin）。"""
    return merged(conn)["default_method"]


def spec_plan_blocked_reason(backend_value: Optional[str]) -> Optional[str]:
    """Plus 分支禁 spec_plan 的中文原因（None=允许）。"""
    if backend_value == "pi_desktop":
        return SPEC_PLAN_DISABLED_REASON
    if backend_value == "claude":
        return "spec_plan 是 Codex 插件运行方式，Claude 计划分支只支持 builtin"
    return None


def save(conn: sqlite3.Connection, groups: dict, now: str) -> dict:
    """原子保存（validated 后调用；groups 可含 default_method/backend/
    pi_desktop）。失败 raise bad_request（中文原因，旧值不变）。

    保存校验不接受 Plus 下的 ``spec_plan``：生效 backend 为 ``pi_desktop`` 时
    生效 default_method 必须是 ``builtin``——否则用户会在 UI 预设一个永远无法
    提交的方式；此处明确失败并提示先改 default_method，不静默改写用户选择。
    """
    current = merged(conn)
    for group in PLANNER_GROUPS:
        section = groups.get(group)
        if not isinstance(section, dict):
            continue
        current[group] = {
            "planner_model": section["planner_model"],
            "reasoning_effort": section["reasoning_effort"],
        }
    method = groups.get("default_method")
    if method in DEFAULT_METHODS:
        current["default_method"] = method
    if "backend" in groups:
        submitted = groups["backend"]
        if submitted not in BACKENDS:
            raise util.bad_request(
                f"backend 不支持：{submitted}"
                f"（可用：{', '.join(BACKENDS)}）"
            )
        current["backend"] = submitted
    if "pi_desktop" in groups:
        block = groups["pi_desktop"]
        if not isinstance(block, dict):
            raise util.bad_request("pi_desktop 必须是对象")
        current_block = dict(current.get("pi_desktop") or {})
        current_block.update(block)
        current["pi_desktop"] = exec_config.merge_pi_desktop_block(current_block)
    if "claude" in groups:
        error = validate_claude_block(groups["claude"])
        if error:
            raise util.bad_request(error)
        current["claude"] = dict(groups["claude"])
    if current["backend"] in ("pi_desktop", "claude") and (
            current["default_method"] == "spec_plan"):
        raise util.bad_request(
            f"{current['backend']} 计划分支不支持 default_method=spec_plan："
            f"{spec_plan_blocked_reason(current['backend'])}；请先把 default_method 改为 builtin"
            f"（或切回 codex）"
        )
    if current["backend"] == "pi_desktop":
        # 结构校验（离线可保存：不探测服务/连接，只查形状与枚举）。
        error = validate_pi_desktop_block(current.get("pi_desktop"))
        if error is not None:
            raise util.bad_request(f"plus 规划配置校验失败：{error}")
    stored = {group: dict(current[group]) for group in PLANNER_GROUPS}
    stored["default_method"] = current["default_method"]
    stored["backend"] = current["backend"]
    stored["claude"] = dict(current["claude"])
    if current.get("pi_desktop") is not None:
        stored["pi_desktop"] = dict(current["pi_desktop"])
    stored["updated_at"] = now
    db.set_setting(conn, "planner_config", stored)
    return stored


def validate_method(value) -> Optional[str]:
    """default_method 校验：∈ DEFAULT_METHODS → 原值；否则 None。"""
    if isinstance(value, str) and value in DEFAULT_METHODS:
        return value
    return None


def validate_claude_block(block) -> Optional[str]:
    if not isinstance(block, dict) or _clean(block.get("model")) is None:
        return "claude.model 必须是非空模型标识"
    if block.get("effort") not in CLAUDE_EFFORTS:
        return "claude.effort 仅支持 low/medium/high/xhigh/max"
    return None


def validate_pi_desktop_block(block: Optional[dict]) -> Optional[str]:
    """Plus 计划分支五字段校验（§5.24.1 / T7.1）：复用执行环境的键与校验，并额外
    要求 ``permission_mode=ask``（计划分支固定 ``mode=plan``，不开放
    auto/accept-edits）。返回中文错误或 None。"""
    if not isinstance(block, dict) or not block:
        return "pi_desktop 配置未保存（请在规划环境保存五字段）"
    b = dict(block)
    error = exec_config.validate_pi_desktop_block(b)
    if error is not None:
        return error
    if b.get("permission_mode") != PI_DESKTOP_PERMISSION_MODE:
        return (
            f"Plus 计划分支仅允许 permission_mode={PI_DESKTOP_PERMISSION_MODE}"
            f"（当前 {b.get('permission_mode')}）：Plan 模式不是 OS 只读沙箱，"
            f"仅 ask 能拦住自动 shell 写入"
        )
    return None


def capabilities(conn: sqlite3.Connection, timeout: int = 10) -> dict:
    """``planner-capabilities.backends.pi_desktop`` 分支（§5.24.2，只读）。

    只检测**已保存**的 ``planner_config.pi_desktop`` 五字段与 Plus 连接：
    ``{available, detail, tools_ok, version, planner_methods}``。工具门 =
    ``PI_DESKTOP_PLAN_TOOLS``（基础工具 + 契约审批工具）。``available=false``
    给中文原因；**不**自动降级 codex，也不为 Plus 分支跑 Codex provider 解析
    或 PI auth check。``available=true`` 只代表本机配置与协议工具可用，不证明
    凭据有效或模型在线。
    """
    block = pi_desktop_block(conn)
    methods = {
        "builtin": {"available": False, "reason": "pi_desktop 配置未保存"},
        "spec_plan": {"available": False, "reason": SPEC_PLAN_DISABLED_REASON},
    }
    if block is None:
        return {
            "available": False,
            "detail": "pi_desktop 配置未保存（请在规划环境保存五字段）",
            "tools_ok": False,
            "version": None,
            "planner_methods": methods,
        }
    error = validate_pi_desktop_block(block)
    if error is not None:
        return {
            "available": False,
            "detail": error,
            "tools_ok": False,
            "version": None,
            "planner_methods": methods,
        }
    probe = exec_config._probe_pi_desktop(
        block, timeout=timeout,
        required_tools=constants.PI_DESKTOP_PLAN_TOOLS,
    )
    available = bool(probe["available"] and probe["tools_ok"])
    detail = probe["detail"] or (
        f"PI-Desktop 控制面就绪（协议版本 {probe['version']}）"
        if probe["available"] else "PI-Desktop 控制面不可用"
    )
    return {
        "available": available,
        "detail": detail,
        "tools_ok": bool(probe["tools_ok"]),
        "version": probe["version"],
        "planner_methods": {
            "builtin": {
                "available": available,
                "reason": None if available else detail,
            },
            "spec_plan": {
                "available": False,
                "reason": SPEC_PLAN_DISABLED_REASON,
            },
        },
    }


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
