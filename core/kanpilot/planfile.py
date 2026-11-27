"""Plan-file and source-snapshot validation, mirroring the legacy
kaneo_dispatch.py check() semantics (protocol.md §4.5 items 1-3, §5.6)."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from . import bridge, constants, util


def project_records(repository, task_id):
    """Persistent plan evidence owned by this repository, including after worktree removal."""
    if not repository or not task_id or not util.TASK_ID_RE.fullmatch(str(task_id)):
        return None
    repo = Path(repository).expanduser()
    if not (repo / ".git").exists():
        return None
    from . import worktree_pool
    common = worktree_pool._common(repo)
    directory = common / "kanpilot" / "plans" / task_id
    worktree_pool._no_links(directory)
    return directory


def plan_location(path, repository, task_id):
    path = Path(path).expanduser()
    if path.resolve().is_relative_to(constants.plan_root().resolve()):
        return util.inside(path, constants.plan_root(), "plan_path")
    directory = project_records(repository, task_id)
    util.require(directory is not None and path.parent == directory and not path.is_symlink()
                 and path.suffix == ".md", "plan_path outside allowed root")
    return path


def project_document(task):
    if "plan_path" not in task.keys() or not task["plan_path"]:
        return None
    source = Path(task["plan_path"])
    if source.parts[-4:-1] == ("kanpilot", "plans", task["task_id"]):
        return Path("docs/plans") / task["task_id"] / source.name
    return None


def dispatch_document(task, worktree):
    """Only the exact untracked plan left by this dispatch can be reused on a retry."""
    relative = project_document(task)
    if relative and os.path.lexists(Path(worktree) / relative):
        return checkout_plan(task, worktree)
    return None


def checkout_plan(task, worktree, *, install=False):
    """Materialize the immutable plan before execution freezes its source snapshot."""
    from . import worktree_pool
    relative = project_document(task)
    if relative is None:
        return None  # Existing dispatches keep their original evidence and diff.
    source = plan_location(task["plan_path"], task["repository"], task["task_id"])
    raw = source.read_bytes()
    util.require(util.sha256_bytes(raw) == task["plan_sha256"], "冻结计划内容已变化")
    target = Path(worktree) / relative
    worktree_pool._no_links(target)
    if install and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent) as tmp:
            tmp.write(raw)
            tmp.flush()
            try:
                os.link(tmp.name, target)
            except FileExistsError:
                pass
    util.require(target.is_file() and not target.is_symlink() and target.read_bytes() == raw,
                 "任务工作树中的正式计划缺失或内容已变化")
    return str(relative)


def load_plan(plan_path: str, expect_task_id: Optional[str] = None, *, repository=None) -> tuple[dict, bytes, Path]:
    """Read a task-bound repository plan or an existing legacy plan."""
    path = plan_location(plan_path, repository, expect_task_id)
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


_MANUAL_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*人工验收")
_MANUAL_EMPTY = {"无", "无。", "（无）", "(无)", "暂无", "不适用"}


def manual_acceptance(plan_text: str) -> list:
    """「人工验收（不阻塞）」段里的条目。

    这类验收（真机、设备、听感、视觉、跨平台对比）执行环境做不了，只作为
    「待体验」清单交给用户确认，**不**参与 verify、也不阻塞 Review 与完成。
    没有该段或写明“无”时返回空列表。
    """
    items, inside, fenced = [], False, False
    for line in plan_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("`" * 3):
            if inside:
                fenced = not fenced
            continue
        if re.match(r"^#{1,6}\s", stripped):
            inside = bool(_MANUAL_HEADING.match(stripped))
            fenced = False
            continue
        if not inside or fenced or not stripped:
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        text = bullet.group(1).strip() if bullet else stripped
        if text and text.strip("：:。 ") not in _MANUAL_EMPTY:
            items.append(text)
    return items


_DELIVERY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*交付类型")
_DELIVERY_EVIDENCE_ONLY = ("证据/记录收尾", "证据收尾", "记录收尾", "evidence_only")


def delivery_kind(plan_text: str) -> str:
    """计划声明的本轮交付类型：`source`（默认，必须改业务源码）或 `evidence_only`。

    `## 交付类型` 段写明「证据/记录收尾」的轮次不会产生源码 diff（例如只补证据、
    只改任务记录、只准备依赖环境），核心据此允许空 diff 通过 monitor；其余情况仍是
    源码交付，空 diff 照旧判空转。
    """
    inside = False
    for line in plan_text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            inside = bool(_DELIVERY_HEADING.match(stripped))
            continue
        if not inside or not stripped:
            continue
        if any(word in stripped for word in _DELIVERY_EVIDENCE_ONLY):
            return "evidence_only"
    return "source"


def body_line_count(raw: bytes) -> int:
    """计划正文的非空行数（不含 frontmatter）。

    只做可观测记录：篇幅契约（轻档 ≤60、重档 ≤180）由 planner prompt 约束，
    核心不在发布时拒绝计划——重排一次要烧掉一整轮规划。
    """
    text = raw.decode("utf-8", "replace")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return sum(1 for line in text.splitlines() if line.strip())


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
