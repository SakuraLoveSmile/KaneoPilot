"""Dispatch claim validation chain (protocol.md §4.5), equivalent to the
legacy kaneo_dispatch.py check(). Runs outside the write transaction —
remote checks never hold BEGIN IMMEDIATE."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import bridge, constants, core, db, exec_config, planfile, remote, util, mcp_client, git_delivery


class DispatchCheckFailed(util.KanpilotError):
    """Deterministic validation failure -> request fails, task needs_user."""

    def __init__(self, message: str, code: str = "validation_failed"):
        super().__init__(code, message)


class DispatchRemoteFailed(util.KanpilotError):
    """Transient remote failure -> request fails, task unchanged."""

    def __init__(self, message: str):
        super().__init__("remote_unavailable", message)


def _require(condition, message, code="validation_failed"):
    if not condition:
        raise DispatchCheckFailed(message, code)


def validate_dispatch(conn: sqlite3.Connection, task, now) -> dict:
    """Run the full §4.5 chain; returns the dispatch claim context fields."""
    task_id = task["task_id"]
    from . import review_resolution
    repair = core.review_repair_of(task)
    if repair:
        from . import review_repair
        try:
            repair = review_repair.verify_frozen(task)
            review_repair.verify_source_terminal(conn, task, now)
        except util.KanpilotError as exc:
            raise DispatchCheckFailed(exc.message, exc.code) from exc
    else:
        review_resolution.verify_remediation(conn, task)
    plan_kind = task["plan_kind"] or "standard"

    # 1. plan file + frontmatter + registered hash -------------------------
    util.require(task["plan_path"], "task has no plan_path")
    try:
        meta, raw, path = planfile.load_plan(task["plan_path"], task_id)
    except util.KanpilotError as exc:
        raise DispatchCheckFailed(exc.message, exc.code)
    try:
        # §5.22.6：按任务登记的 planner_model/reasoning_effort 比对；旧任务
        # 缺登记（planner_model NULL）→ 回落历史常量规则。
        expected_model = (
            {"planner_model": task["planner_model"],
             "reasoning_effort": task["reasoning_effort"]}
            if task["planner_model"] else None
        )
        expected_project = {
            "project_id": task["project_id"],
            "project_name": task["project_name"],
            "repository": task["repository"],
        } if (task["project_id"] or task["repository"]) else None
        planfile.check_plan_meta(
            meta, plan_kind,
            expected_model=expected_model,
            expected_project=expected_project,
        )
    except util.KanpilotError as exc:
        raise DispatchCheckFailed(exc.message)
    if meta.get("plan_kind") not in (None, plan_kind):
        raise DispatchCheckFailed("plan_kind frontmatter conflicts with task")
    plan_sha = util.sha256_bytes(raw)
    _require(
        task["plan_sha256"] == plan_sha,
        "published plan changed (plan_sha256 mismatch)",
    )
    _require(meta.get("source_sha256"), "plan missing source_sha256")

    # 2. source snapshot recompute -----------------------------------------
    util.require(task["source_path"], "task has no source snapshot")
    try:
        _source, source_sha, requirements = planfile.check_source_snapshot(
            task["source_path"], task_id, meta["project_id"]
        )
    except util.KanpilotError as exc:
        raise DispatchCheckFailed(exc.message)
    _require(
        source_sha == meta["source_sha256"],
        "original source mismatch (source_sha256)",
    )

    # 3. requirements fingerprint ------------------------------------------
    if meta.get("requirements_sha256"):
        _require(
            requirements == meta["requirements_sha256"],
            "plan requirements mismatch",
        )
    if task["requirements_sha256"]:
        _require(
            requirements == task["requirements_sha256"],
            "registered requirements baseline mismatch",
        )

    # 4. remote task + columns ----------------------------------------------
    try:
        live = remote.get_task(remote.get_client(), task_id)
    except bridge.BridgeError as exc:
        raise DispatchRemoteFailed(str(exc))
    except Exception as exc:  # defensive: stub clients
        raise DispatchRemoteFailed(str(exc))
    live_task = live.get("task") or {}
    _require(
        live_task.get("projectId") == meta["project_id"],
        "remote project mismatch",
    )
    _require(
        live_task.get("status") in (core.expected_remote_slugs(task) | (set(constants.REVIEW_SLUGS) if repair else set())),
        "remote task not in expected status",
    )
    _require(
        live.get("requirements_sha256") == requirements,
        "remote requirements changed",
    )
    slugs = {
        str(item.get("slug"))
        for item in live.get("columns") or []
        if isinstance(item, dict)
    }
    _require(
        live_task.get("status") in slugs
        and constants.IN_PROGRESS_SLUG in slugs,
        "required columns absent",
    )

    # 5. plan_kind-specific --------------------------------------------------
    if repair:
        pass  # Frozen direct identity replaces historical parent/fix-plan pointers.
    elif meta.get("fix_mode") == "same_task":
        fix = db.task_extra(task).get("fix_request") or {}
        _require(core.is_automation_managed(task), "same_task plan requires automation")
        try:
            report = util.inside(Path(meta.get("review_report_path") or ""),
                                 constants.review_reports_root(), "review_report_path")
        except util.KanpilotError as exc:
            raise DispatchCheckFailed(exc.message) from exc
        _require(report.is_file(), "review report missing")
        _require(str(report) == fix.get("report_path"), "review report path mismatch")
        _require(util.sha256_file(report) == meta.get("review_report_sha256") == fix.get("report_sha256"),
                 "review report changed")
        _require(meta.get("review_source_task_id") == task_id, "same_task source mismatch")
        _require(meta.get("fix_round") == core.automation_of(task).get("fix_round"), "fix round mismatch")
        _require(meta.get("review_key") == fix.get("review_key") and bool(fix.get("review_key")),
                 "review key mismatch")
    elif plan_kind == "review_fix":
        report = util.inside(
            Path(meta["review_report_path"]),
            constants.review_reports_root(),
            "review_report_path",
        )
        util.require(report.is_file(), "review report missing")
        _require(
            util.sha256_file(report) == meta["review_report_sha256"],
            "review report changed",
        )
        source_task = db.get_task(conn, meta["review_source_task_id"])
        _require(source_task is not None, "reviewed task not registered")
        extra = db.task_extra(source_task)
        _require(
            source_task["fix_task_id"] == task_id
            and source_task["review_key"] == meta["review_key"],
            "review delivery mismatch (fix_task_id/review_key)",
        )
        _require(
            extra.get("repair_plan_path") == str(path)
            and extra.get("repair_plan_sha256") == plan_sha,
            "review delivery mismatch (repair_plan)",
        )
        _require(
            constants.REVIEW_MARKER_PREFIX + meta["review_key"] + " -->"
            in str(live_task.get("description", "")),
            "review marker missing in remote description",
        )
    else:
        _require(
            constants.REVIEW_MARKER_PREFIX
            not in str(live_task.get("description", "")),
            "review task requires review_fix plan",
        )

    # 6. conflicting send evidence -------------------------------------------
    _require(
        task["send_state"] not in ("unknown", "sent") and task["sent"] != 1,
        "conflicting send evidence; resolve required",
        "send_conflict",
    )

    # 7. 执行后端可用性门（protocol §5.17.3 / §5.23.3）：当前 exec_config.backend
    #    分支决定检查内容——pi 为 pi 可执行/模型清单/当前配置/凭据/agent.md；
    #    pi_desktop 为连接文件/token/tools/provider/model。任一不可用 → 确定性
    #    validation_failed（task 进 needs_user），**不自动换模型、agent 或后端**。
    #    失败信息取自 exec_config.check_available 的中文原因。
    #    快照经 snapshot_with_model_override：引擎在有界补救轮次前写入的一次性
    #    模型覆盖（engine/fallback.py，scenarios.md #5）在此冻结进轮次；无覆盖时
    #    与 snapshot(conn) 完全一致（旧数据零迁移），设置变更仍只影响新轮次。
    config_snapshot = (repair["action_config"]
                       if repair and repair.get("source_kind") == "merge_conflict"
                       else exec_config.snapshot_with_model_override(conn, task))
    backend = config_snapshot.get("backend") or "pi"
    if backend == "pi_desktop":
        try:
            service = mcp_client.MCPClient.from_server_file(
                constants.app_support() / "mcp" / "server.json", timeout=3)
            initialized = service.initialize()
            _require(initialized.get("serverInfo", {}).get("name") == "kaneopilot", "本机 MCP 服务身份不符")
            service.ping()
        except util.KanpilotError as exc:
            raise DispatchCheckFailed("本机 MCP 服务未就绪：" + exc.message) from exc
    available, reason = exec_config.check_available(conn, config_snapshot=config_snapshot)
    _require(available, reason or "执行环境不可用")
    delivery = dict(core.delivery_of(task), repository=task["repository"])
    manual_branch = (core.automation_of(task).get("manual_takeover") is True
                     and bool(delivery.get("branch")) and bool(delivery.get("worktree_path")))
    user_replan = core.is_user_replan(task)
    state_sha = core.automation_of(task).get("replan_worktree_sha256")
    retry = core.execution_retry_of(task)
    if repair:
        try:
            recovery = repair.get("verification_recovery")
            if repair.get("source_kind") == "merge_conflict" and recovery:
                from . import cmd_queue, task_ai_action
                _require(retry.get("source_run_id") == recovery.get("source_run_id"),
                         "验证恢复与原处理轮次不符")
                git_delivery.verify_conflict_remote(delivery, expected_repo=repair["github_repo"],
                                                    target_base_sha=repair["target_base_sha"])
                cmd_queue.verify_execution_retry_terminal(conn, task, now)
                task_ai_action.verify_verification_recovery(task, recovery)
            else:
                git_delivery.verify_branch_worktree(delivery, require_clean=True,
                                                    expected_head=repair["head_sha"])
        except git_delivery.GitDeliveryError as exc:
            raise DispatchCheckFailed(exc.message) from exc
    elif retry:
        from . import cmd_queue
        cmd_queue.verify_execution_retry_terminal(conn, task, now)
        worktree = retry.get("worktree") or {}
        _require(worktree.get("repository") == task["repository"], "重新执行工作树仓库身份不符")
        try:
            _require(git_delivery.worktree_state_sha256(worktree, detached=retry.get("detached") is True)
                     == retry.get("worktree_sha256"), "重新执行确认后工作树内容已变化，请重新确认")
        except git_delivery.GitDeliveryError as exc:
            raise DispatchCheckFailed(exc.message) from exc
    elif core.is_pr_delivery(task) or manual_branch:
        if not manual_branch:
            _require(bool(delivery.get("head_sha")), "delivery head missing")
        if user_replan:
            _require(bool(state_sha), "重新规划缺少工作树状态指纹")
        try:
            git_delivery.verify_branch_worktree(
                delivery, require_clean=not (manual_branch or user_replan),
                expected_head=None if manual_branch else delivery["head_sha"])
            if user_replan:
                _require(git_delivery.worktree_state_sha256(delivery) == state_sha,
                         "重新规划后工作树内容已变化，请重新准备规划材料")
        except git_delivery.GitDeliveryError as exc:
            raise DispatchCheckFailed(exc.message) from exc
    return {
        **({"review_repair": repair} if repair else {}),
        "task_id": task_id,
        "plan_path": str(path),
        "plan_sha256": plan_sha,
        "attachment_name": path.name,
        "repository": meta["repository"],
        "project_id": meta["project_id"],
        "plan_kind": plan_kind,
        "original_todo_slug": live_task["status"],
        "requirements_sha256": requirements,
        "source_sha256": meta["source_sha256"],
        "kaneo_status": live_task["status"],
        # v3（§5.23.3）：后端按 exec_config.backend 固化（缺省 pi）。
        "backend": backend,
        "exec_config": config_snapshot,
    }


def handle_dispatch_guard_failure(
    conn: sqlite3.Connection,
    request,
    task,
    exc: util.KanpilotError,
    now: str,
    actor: str,
) -> dict:
    """§4.2-5 + §4.5 failure bookkeeping; returns the claim response."""
    now_iso = now
    with db.write_txn(conn):
        request = db.get_request(conn, request["request_id"])
        task = db.get_task(conn, task["task_id"])
        if request["status"] == "queued":
            # Fail only when still ours: a competing claim may have taken it
            # while validation ran outside the transaction.
            core.fail_request(conn, request, now_iso, exc.message, actor=actor)
        # Deterministic failures block the task; remote_unavailable leaves it
        # untouched (protocol.md §4.5-4: not a blocking condition).
        if exc.code != "remote_unavailable" and task["status"] != "needs_user":
            core.block_task(
                conn, task, now_iso, exc.message, exc.code, actor,
                stage=task["status"], send_state=task["send_state"],
            )
    return {
        "claimed": False,
        "reason": "guard_failed",
        "request_id": request["request_id"],
        "detail": exc.message,
        "code": exc.code,
    }
