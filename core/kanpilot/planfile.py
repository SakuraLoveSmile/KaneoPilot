"""Plan-file and source-snapshot validation, mirroring the legacy
kaneo_dispatch.py check() semantics (protocol.md §4.5 items 1-3, §5.6)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import bridge, constants, util


def load_plan(plan_path: str, expect_task_id: Optional[str] = None) -> tuple[dict, bytes, Path]:
    """Locate a plan inside PLAN_ROOT, parse frontmatter, basic field checks."""
    path = util.inside(
        Path(plan_path), constants.plan_root(), "plan_path"
    )
    util.require(path.is_file(), f"plan file missing: {plan_path}")
    meta, raw = util.read_frontmatter(path)
    if expect_task_id is not None:
        util.require(
            meta.get("task_id") == expect_task_id,
            f"plan task_id {meta.get('task_id')} != {expect_task_id}",
        )
    task_id = meta.get("task_id")
    util.require(
        isinstance(task_id, str) and bool(util.TASK_ID_RE.fullmatch(task_id)),
        "plan task_id invalid",
    )
    return meta, raw, path


def expected_model_snapshot(req_payload: dict) -> Optional[dict]:
    """本请求固化的模型快照（§5.22.6 codex / §5.24.3 Plus）——**唯一**解析点。

    `publish-plan` 的 frontmatter 校验与规划 runner 选择写入值必须用同一口径，
    否则一边写 §6 常量、一边按 Plus 快照校验就会互相拒收（本函数的由来）。

    - codex / claude：``model_config``；
    - Plus（``planner_backend="pi_desktop"``）：``runtime_config`` 的
      ``model_id``/``thinking_level``（Plus 没有 codex 的 ``reasoning_effort``）；
    - 快照缺失/不完整：None（旧记录回落 §6 历史常量，由调用方留痕）。
    """
    if req_payload.get("planner_backend") == "codex_desktop":
        # 来源标记只表明 Desktop 继承配置，不是对实际型号的核验结果。
        from .planner_config import DESKTOP_MODEL
        return dict(DESKTOP_MODEL)
    expected = req_payload.get("model_config")
    if isinstance(expected, dict) and expected.get("planner_model"):
        return expected
    if req_payload.get("planner_backend") != "pi_desktop":
        return None
    runtime = req_payload.get("runtime_config")
    if not isinstance(runtime, dict):
        return None
    model_id = runtime.get("model_id")
    thinking = runtime.get("thinking_level")
    if not isinstance(model_id, str) or not model_id:
        return None
    if not isinstance(thinking, str) or not thinking:
        return None
    return {"planner_model": model_id, "reasoning_effort": thinking}


def check_plan_meta(
    meta: dict,
    plan_kind: str,
    expected_model: Optional[dict] = None,
    expected_project: Optional[dict] = None,
    configured_mappings: Optional[dict] = None,
) -> None:
    """Frontmatter completeness + planner/reasoning requirements (§6).

    ``expected_model``（``{"planner_model":…,"reasoning_effort":…}``）提供时
    按请求/任务登记的快照比对（v2.1 §5.22.6）；缺省时沿用历史固定常量
    （旧记录缺登记的兼容路径）。
    ``expected_project``（``{"project_id":…, "repository":…, "project_name":…}``）
    提供时按固化的项目快照比对（T1 §5.26）；缺省时按调用方提供的本地已保存映射校验。
    """
    required = (
        constants.PLAN_META_REQUIRED_REVIEW_FIX
        if plan_kind == "review_fix"
        else constants.PLAN_META_REQUIRED
    )
    missing = [key for key in required if meta.get(key) in (None, "")]
    util.require(not missing, f"plan frontmatter missing: {', '.join(missing)}")
    util.require(
        meta.get("implementation_status") == "未开始",
        "plan implementation_status is not 未开始",
    )
    if expected_project is not None:
        expected_pid = expected_project.get("project_id")
        if expected_pid:
            util.require(
                meta.get("project_id") == expected_pid,
                f"plan project_id {meta.get('project_id')} != {expected_pid}",
            )
        expected_repo = expected_project.get("repository")
        if expected_repo:
            util.require(
                meta.get("repository") == expected_repo,
                f"plan repository {meta.get('repository')} != {expected_repo}",
            )
        expected_pname = expected_project.get("project_name")
        if expected_pname:
            util.require(
                meta.get("project_name") == expected_pname,
                f"plan project_name {meta.get('project_name')} != {expected_pname}",
            )
    else:
        entry = (configured_mappings or {}).get(meta.get("project_id"))
        util.require(bool(entry), f"unknown configured project {meta.get('project_name')}")
        util.require(
            meta.get("project_name") == entry.get("project_name"),
            "plan project_id does not match configured project_name",
        )
        expected_repo = entry.get("repository")
        util.require(bool(expected_repo), "configured project has no repository")
        util.require(
            meta.get("repository") == expected_repo,
            f"plan repository {meta.get('repository')} != {expected_repo}",
        )
    util.require(
        Path(meta["repository"]).is_dir(),
        f"repository missing: {meta['repository']}",
    )
    if expected_model is not None:
        util.require(
            meta.get("planner_model") == expected_model.get("planner_model")
            and meta.get("reasoning_effort")
            == expected_model.get("reasoning_effort"),
            "plan planner_model/reasoning_effort 与本请求登记不符",
        )
    else:
        expected = (
            constants.REVIEW_FIX_PLANNER
            if plan_kind == "review_fix"
            else constants.STANDARD_PLANNER
        )
        util.require(
            meta.get("planner_model") == expected["planner_model"]
            and meta.get("reasoning_effort") == expected["reasoning_effort"],
            f"{plan_kind} plan format mismatch "
            f"(planner_model/reasoning_effort)",
        )
    if plan_kind == "standard" and meta.get("plan_kind") not in (None, "standard"):
        util.require(False, "plan_kind frontmatter conflicts with standard plan")


def check_source_snapshot(
    source_path: str,
    expect_task_id: str,
    expect_project_id: str,
) -> tuple[dict, str, str]:
    """Recompute source_sha256 / requirements_sha256 from the snapshot.

    Returns (source, source_sha256, requirements_sha256)."""
    path = Path(source_path).expanduser()
    util.require(path.is_file(), f"source snapshot missing: {source_path}")
    util.require(not path.is_symlink(), f"snapshot is a symlink: {path}")
    try:
        source = util.jload(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise util.KanpilotError("validation_failed", f"cannot read snapshot: {exc}")
    util.require(isinstance(source, dict), "source snapshot must be a JSON object")
    util.require(
        all(key in source for key in ("task", "comments", "relations", "activity")),
        "source snapshot missing task/comments/relations/activity",
    )
    canonical = bridge.stable(
        {key: source[key] for key in ("task", "comments", "relations", "activity")}
    )
    source_sha256 = bridge.add_hash(canonical)["source_sha256"]
    task = source.get("task") or {}
    util.require(
        task.get("id") == expect_task_id,
        f"snapshot task.id {task.get('id')} != {expect_task_id}",
    )
    util.require(
        task.get("projectId") == expect_project_id,
        "snapshot task.projectId mismatch",
    )
    try:
        requirements = bridge.requirements_hash(
            {"task": task, "comments": source["comments"],
             "relations": source["relations"]}
        )
    except bridge.BridgeError as exc:
        raise util.KanpilotError("validation_failed", str(exc)) from exc
    return source, source_sha256, requirements
