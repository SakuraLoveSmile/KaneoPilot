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


def check_plan_meta(meta: dict, plan_kind: str) -> None:
    """Frontmatter completeness + planner/reasoning requirements (§6)."""
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
    util.require(
        meta.get("project_name") in constants.PROJECTS,
        f"unknown project {meta.get('project_name')}",
    )
    util.require(
        meta.get("project_id") == constants.PROJECTS[meta["project_name"]],
        "plan project_id does not match project_name whitelist",
    )
    expected_repo = str(constants.repo_path_for(meta["project_name"]))
    util.require(
        meta.get("repository") == expected_repo,
        f"plan repository {meta.get('repository')} != {expected_repo}",
    )
    util.require(
        Path(meta["repository"]).is_dir(),
        f"repository missing: {meta['repository']}",
    )
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
