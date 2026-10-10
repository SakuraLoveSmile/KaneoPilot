"""Fallback execution model for bounded quality remediation (scenarios.md #5).

state-model §8 阶梯 3（quality）给失败执行 ≤2 轮同分支有界补救。项目配了
``fallback_model``（``{provider_id, model_id}``）时，引擎用该模型快照重跑补救
轮——但**先探针**：模型必须存在于 Plus 本地 cache 目录
（``providers/list`` + ``providers/listModels {source:"cache"}``）。探针失败即
``validation_failed``（阶梯 2 环境类），**不静默换模型**。未配 fallback 则沿用
主模型重跑（记 ``model: primary``）并产出「建议配置备用模型」提示事件。

缺省推荐值取自 ``docs/evidence/spike-fallback-model-id-20261010.md``（与主模型
同一 provider、跨厂商 flash 档，复用主模型 connection_file 与鉴权）。它只是
**建议值**：``fallback_model`` 始终是项目配置，缺省 = 不配置 = 不换模型，缺省 id
绝不隐式套用。

探针复用 ``pi_desktop_backend.probe_connection``（只读；不返回 token、不返回
连接文件全文）。选定的快照由引擎写入任务 ``extra.engine_model_override``，dispatch
claim 冻结轮次快照时经 ``exec_config.snapshot_with_model_override`` 应用——
覆盖只影响它瞄准的那一次重跑，**不**回写 ``settings.exec_config``。
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

# 项目配置键。放**映射条目级**（与 engine_mode / verify_env 同层级）：
# ``validate_automation`` 用固定白名单重建 automation 子字典，放进去会被静默丢弃。
FALLBACK_MODEL_KEY = "fallback_model"
# 一次性模型覆盖：引擎写任务 extra，dispatch claim 应用后由引擎清除。
MODEL_OVERRIDE_KEY = "engine_model_override"
# 模型选择标记（事件 detail 的 ``model`` 值 + 决策 params）。
MODEL_PRIMARY = "primary"
MODEL_FALLBACK = "fallback"
# spike-fallback-model-id-20261010.md 结论：同 provider、跨厂商 flash 档。
DEFAULT_FALLBACK_PROVIDER_ID = "707a4c67-4ee0-484d-b635-730372501eaf"
DEFAULT_FALLBACK_MODEL_ID = "google/gemini-3.7-flash"
# 事件 kind（审计「是否静默换模型」，scenarios.md #5）。
EVENT_MODEL = "model"
EVENT_MODEL_HINT = "model_hint"
# pi_desktop 运行快照字段（resolve_runtime_config 形状，脱敏、无 token）。
_SNAPSHOT_KEYS = ("backend", "connection_file", "provider_id", "model_id",
                  "thinking_level", "permission_mode")


def recommended_default() -> dict:
    """spike 结论的推荐 fallback（仅建议值，绝不隐式套用）。"""
    return {"provider_id": DEFAULT_FALLBACK_PROVIDER_ID,
            "model_id": DEFAULT_FALLBACK_MODEL_ID}


def normalize(raw) -> Optional[dict]:
    """``{provider_id, model_id}`` 规范化；缺字段/非字符串 → None（= 未配置）。"""
    if not isinstance(raw, dict):
        return None
    provider_id = raw.get("provider_id")
    model_id = raw.get("model_id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        return None
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    return {"provider_id": provider_id.strip(), "model_id": model_id.strip()}


def configured_model(entry) -> Optional[dict]:
    """项目配置的 fallback 模型：条目级 ``fallback_model`` 优先，否则
    ``automation.fallback_model``（为将来白名单纳入预留）。两者都缺 → None。"""
    if not isinstance(entry, dict):
        return None
    found = normalize(entry.get(FALLBACK_MODEL_KEY))
    if found is not None:
        return found
    automation = entry.get("automation")
    if isinstance(automation, dict):
        return normalize(automation.get(FALLBACK_MODEL_KEY))
    return None


def probe_model(provider_id: str, model_id: str, *,
                connection_file=None, timeout: int = 10) -> Tuple[bool, str]:
    """只读探针：provider 存在且未禁用 + 模型在 cache 目录中（source 允许
    cache/fallback）。复用 ``pi_desktop_backend.probe_connection``；任何异常都
    归一成 ``(False, 中文原因)``，绝不向上抛、绝不返回凭据。"""
    try:
        from .. import pi_desktop_backend
        result = pi_desktop_backend.probe_connection(
            connection_file, provider_id=provider_id, model_id=model_id,
            timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — 探针失败不是引擎崩溃理由
        return False, f"fallback 模型探针失败：{exc}"
    if not isinstance(result, dict):
        return False, "fallback 模型探针结果不可用"
    ok = bool(result.get("available", result.get("ok")))
    detail = str(result.get("detail") or result.get("reason") or "")
    return ok, detail or ("模型在 Plus cache 目录中可用" if ok else "模型不可用")


def primary_snapshot(conn) -> Tuple[Optional[dict], Optional[str]]:
    """当前生效的执行运行快照（脱敏）。失败 → ``(None, 中文原因)``。"""
    from .. import exec_config
    try:
        return exec_config.snapshot(conn), None
    except Exception as exc:  # noqa: BLE001 — 快照解析失败即显式失败
        return None, f"主模型运行快照解析失败：{exc}"


def build_snapshot(primary: Optional[dict], fallback: dict) -> Optional[dict]:
    """主快照 + fallback 模型 → 重跑轮次要冻结的快照（spike §形态与接线：
    ``model_source="custom"`` + provider_id/model_id，沿用主模型 connection_file /
    thinking_level / permission_mode，不新增凭据）。

    仅 pi_desktop 分支有意义；其他后端返回 None（调用方按主模型重试并说明原因）。
    """
    if not isinstance(primary, dict) or primary.get("backend") != "pi_desktop":
        return None
    snapshot = {key: primary.get(key) for key in _SNAPSHOT_KEYS}
    snapshot["model_source"] = "custom"
    snapshot["provider_id"] = fallback["provider_id"]
    snapshot["model_id"] = fallback["model_id"]
    return snapshot


def plan_retry(configured: Optional[dict], *, primary: Optional[dict] = None,
               probe: Optional[Callable[[str, str], Tuple[bool, str]]] = None,
               connection_file=None, timeout: int = 10) -> dict:
    """有界补救轮次的模型选择（scenarios.md #5）。

    返回 ``{model, snapshot, validation_failed, probe_ok, probe_detail, events}``：

    - 配了 fallback 且探针通过 → ``model="fallback"`` + 该模型快照 + 事件
      ``model: fallback``；
    - 配了 fallback 但探针失败 → ``validation_failed=True``、快照 None、事件记
      ``model: primary``（原因 fallback_probe_failed）——**不换模型**，调用方按
      阶梯 2 阻塞；
    - 未配 fallback → ``model="primary"``、快照 None、事件 ``model: primary`` +
      提示事件「建议配置备用模型」（带 spike 推荐值）；
    - 主后端不是 pi_desktop → 同「未配」分支，另记 ``unsupported_backend`` 原因。
    """
    prober = probe or (lambda p, m: probe_model(
        p, m, connection_file=connection_file, timeout=timeout))
    if configured is None:
        return {
            "model": MODEL_PRIMARY, "snapshot": None, "validation_failed": False,
            "probe_ok": None, "probe_detail": "",
            "events": [
                {"kind": EVENT_MODEL,
                 "detail": {"model": MODEL_PRIMARY,
                            "reason": "fallback_model_not_configured"}},
                {"kind": EVENT_MODEL_HINT,
                 "detail": {"hint": "configure_fallback_model",
                            "recommended": recommended_default(),
                            "reason": "fallback_model_not_configured"}},
            ],
        }

    ok, detail = prober(configured["provider_id"], configured["model_id"])
    if not ok:
        return {
            "model": MODEL_PRIMARY, "snapshot": None, "validation_failed": True,
            "probe_ok": False, "probe_detail": detail,
            "events": [
                {"kind": EVENT_MODEL,
                 "detail": {"model": MODEL_PRIMARY,
                            "reason": "fallback_probe_failed",
                            "provider_id": configured["provider_id"],
                            "model_id": configured["model_id"],
                            "detail": detail}},
            ],
        }

    snapshot = build_snapshot(primary, configured)
    if snapshot is None:
        # 主后端不支持 Plus 模型目录：显式失败而非静默换模型。
        return {
            "model": MODEL_PRIMARY, "snapshot": None, "validation_failed": False,
            "probe_ok": True, "probe_detail": detail,
            "events": [
                {"kind": EVENT_MODEL,
                 "detail": {"model": MODEL_PRIMARY,
                            "reason": "unsupported_backend",
                            "provider_id": configured["provider_id"],
                            "model_id": configured["model_id"],
                            "detail": "当前执行后端不是 pi_desktop，"
                                      "fallback 模型不适用"}},
                {"kind": EVENT_MODEL_HINT,
                 "detail": {"hint": "configure_fallback_model",
                            "recommended": recommended_default(),
                            "reason": "unsupported_backend"}},
            ],
        }
    return {
        "model": MODEL_FALLBACK, "snapshot": snapshot, "validation_failed": False,
        "probe_ok": True, "probe_detail": detail,
        "events": [
            {"kind": EVENT_MODEL,
             "detail": {"model": MODEL_FALLBACK, "model_source": "custom",
                        "provider_id": configured["provider_id"],
                        "model_id": configured["model_id"],
                        "detail": detail}},
        ],
    }


def override_payload(snapshot: dict, task) -> dict:
    """把选定快照打成一次性覆盖（附计划身份，供 dispatch claim 校验）。"""
    payload = {key: snapshot.get(key) for key in _SNAPSHOT_KEYS}
    payload["model"] = MODEL_FALLBACK
    try:
        payload["plan_sha256"] = task["plan_sha256"]
        payload["plan_generated_at"] = task["plan_generated_at"]
    except (IndexError, KeyError):  # pragma: no cover - 非行对象
        pass
    return payload


def read_override(extra) -> Optional[dict]:
    """读任务 extra 里待应用的一次性模型覆盖（无/为空 → None）。"""
    if not isinstance(extra, dict):
        return None
    value = extra.get(MODEL_OVERRIDE_KEY)
    return value if isinstance(value, dict) and value else None


def override_matches(override: dict, task) -> bool:
    """覆盖是否仍瞄准当前计划（计划身份变了就不应用——旧覆盖绝不静默套用）。"""
    try:
        if override.get("plan_sha256") != task["plan_sha256"]:
            return False
        generated = override.get("plan_generated_at")
        if generated is not None and generated != task["plan_generated_at"]:
            return False
    except (IndexError, KeyError):  # pragma: no cover - 非行对象
        return False
    return True
