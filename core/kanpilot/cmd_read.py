"""Read/diagnostic commands and the tick scheduler (protocol.md §5.1/§5.2/§4.8)."""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

from . import constants, core, db, derive, planfile, remote, util


def tick_inline(conn: sqlite3.Connection, now, preflight_by_project=None,
                project_ids=None) -> dict:
    """Business-cycle request top-up (protocol.md §4.8). Idempotent."""
    now_iso = util.to_iso(now)
    created: list = []
    skipped: list = []

    monitor_interval = util.monitor_interval(conn)
    scope = ""
    scope_params = ()
    if project_ids is not None:
        scope_params = tuple(project_ids)
        scope = " AND project_id IN (%s)" % ",".join("?" for _ in scope_params)

    # v2（protocol §5.22.2）：tick 不再生成 plan-scan——规划改为手动门控，
    # 看板枚举由规划面板 fetch-todos 按需只读拉取。

    # --- monitor: every running task keeps a monitor cycle ------------------
    for task in conn.execute(
        "SELECT * FROM tasks WHERE status='running'" + scope + " ORDER BY task_id",
        scope_params,
    ).fetchall():
        # §5.15.3：归档/回收站/恢复后暂停的任务不建监控周期。
        if not db.task_schedulable(task):
            skipped.append(
                {"kind": "monitor", "task_id": task["task_id"],
                 "reason": "task_saved"}
            )
            continue
        key = f"monitor:{task['task_id']}"
        if db.active_request_for_key(conn, key) is not None:
            skipped.append(
                {"kind": "monitor", "task_id": task["task_id"], "reason": "active"}
            )
            continue
        last = conn.execute(
            "SELECT finished_at FROM requests WHERE task_id=? AND "
            "kind='monitor' AND status IN ('succeeded','failed','cancelled') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        last_at = util.parse_ts(last["finished_at"]) if last else None
        if last_at is not None and last_at + timedelta(seconds=monitor_interval) > now:
            skipped.append(
                {"kind": "monitor", "task_id": task["task_id"], "reason": "interval"}
            )
            continue
        row, made = db.create_request(
            conn, now_iso, "monitor", key, task_id=task["task_id"],
            requested_by="scheduler",
        )
        if made:
            created.append(
                {"request_id": row["request_id"], "kind": "monitor",
                 "task_id": task["task_id"]}
            )

    # --- review: in_review tasks without a pending conclusion --------------
    for task in conn.execute(
        "SELECT * FROM tasks WHERE status='in_review'" + scope + " ORDER BY task_id",
        scope_params,
    ).fetchall():
        # §5.15.3：归档/回收站/恢复后暂停的任务不建评审请求。
        if not db.task_schedulable(task):
            skipped.append(
                {"kind": "review", "task_id": task["task_id"],
                 "reason": "task_saved"}
            )
            continue
        key = f"review:{task['task_id']}"
        if db.active_request_for_key(conn, key) is not None:
            skipped.append(
                {"kind": "review", "task_id": task["task_id"], "reason": "active"}
            )
            continue
        if task["review_report_path"]:
            skipped.append(
                {"kind": "review", "task_id": task["task_id"],
                 "reason": "conclusion_pending"}
            )
            continue
        row, made = db.create_request(
            conn, now_iso, "review", key, task_id=task["task_id"],
            requested_by="scheduler",
        )
        if made:
            created.append(
                {"request_id": row["request_id"], "kind": "review",
                 "task_id": task["task_id"]}
            )

    from . import automation_policy, cmd_queue
    for task in conn.execute(
        "SELECT * FROM tasks WHERE status!='done'" + scope, scope_params
    ).fetchall():
        if not core.is_automation_managed(task) or not db.task_schedulable(task):
            continue
        a = core.automation_of(task)
        # Lost monitor/delivery leases can be released without replaying a send.
        lost = conn.execute("SELECT * FROM requests WHERE task_id=? AND status='claimed' AND kind IN ('monitor','delivery')", (task["task_id"],)).fetchall()
        for req in lost:
            if util.lease_lost(req["lease_expires_at"], now):
                core.finish_request(conn, req, now_iso, "cancelled", "cancelled", result={"via": "release_lost"}, note="released after executor loss")
                db.add_event(conn, now_iso, "lost_request_released", task_id=task["task_id"], request_id=req["request_id"], actor="scheduler")
        extra = db.task_extra(task)
        if task["status"] == "ready" and extra.get("plan_execution_hold") and preflight_by_project is not None:
            policy = automation_policy.policy_for(conn, task["project_id"]) or constants.AUTOMATION_DEFAULTS
            verify = extra.get("plan_verify") or {}
            reason = None
            if not policy.get("enabled"): reason = "disabled"
            elif not policy.get("auto_confirm_plan"): reason = "auto_confirm_off"
            elif preflight_by_project.get(task["project_id"], ["unchecked"]): reason = "preflight"
            elif not verify.get("commands"): reason = "verify_missing"
            elif verify.get("plan_sha256") != task["plan_sha256"]: reason = "verify_mismatch"
            elif not core.automation_budget(task, policy, now)["ok"]: reason = "budget"
            elif db.paused(conn, "dispatch"): reason = "paused"
            if reason:
                if (a.get("last_skip") or {}).get("reason") != reason:
                    core.update_automation(conn, task, now_iso, last_skip={"reason": reason, "at": now_iso})
                    db.add_event(conn, now_iso, "auto_confirm_skipped", task_id=task["task_id"], actor="automation", detail={"reason": reason})
            else:
                row, made = cmd_queue.confirm_plan_inline(conn, task, now_iso, expected_plan_sha256=task["plan_sha256"], actor="automation", note="auto")
                if made: created.append({"request_id": row["request_id"], "kind": "dispatch", "task_id": task["task_id"]})
        history = extra.get("review_history") or []
        d = core.delivery_of(task)
        if (task["status"] == "in_review" and extra.get("review_conclusion") == "pass"
            and core.is_pr_delivery(task) and history
            and history[-1].get("execution_run_id") == task["run_id"]
            and (not d.get("next_sync_at") or util.parse_ts(d["next_sync_at"]) <= now)):
            row, made = db.create_request(conn, now_iso, "delivery", f"delivery:{task['task_id']}", task_id=task["task_id"], requested_by="scheduler")
            if made: created.append({"request_id": row["request_id"], "kind": "delivery", "task_id": task["task_id"]})

    db.set_setting(conn, "last_tick_at", now_iso)
    return {"created": created, "skipped": skipped}


def cmd_recover_execution_goals(ctx, payload: dict) -> dict:
    from . import cmd_dispatch
    # Reconnect question-bearing runs as read-only observers. Only the original
    # managed execution_failed path may request precise Goal continuation.
    # Plus reads and worker attachment stay outside the write transaction.
    goal_recoveries = []
    if not db.paused(ctx.conn, "monitor") and not db.paused(ctx.conn, "dispatch"):
        candidates = ctx.conn.execute(
            "SELECT * FROM tasks WHERE (status='needs_user' "
            "AND failure_code IN ('execution_failed','execution_input_required')) OR status='running' ORDER BY task_id"
        ).fetchall()
        for task in candidates:
            from .execution_questions import open_records, records
            waiting=bool(open_records(task))
            if task["status"]=="running" and not any(q["run_id"]==task["run_id"] for q in records(task)):
                continue
            continuation = task["status"] == "needs_user" and task["failure_code"] == "execution_failed"
            if (not db.task_schedulable(task) or (continuation and not core.is_automation_managed(task))
                    or not task["run_id"]):
                continue
            try:
                result = cmd_dispatch.cmd_round_observe(ctx, {
                    "task_id": task["task_id"], "run_id": task["run_id"],
                    "executor": "scheduler", "continue_goal": continuation and not waiting,
                    "continue_goal_only": continuation and not waiting,
                })
                if result.get("status") != "deferred":
                    goal_recoveries.append({"task_id": task["task_id"], **result})
            except util.KanpilotError as exc:
                # A failed read is not permission to resend or unblock a task.
                goal_recoveries.append({"task_id": task["task_id"],
                                        "status": "deferred", "code": exc.code})
    return {"goal_recoveries": goal_recoveries}


def cmd_tick(ctx, payload: dict) -> dict:
    from . import automation_policy, release_delivery
    project_ids = payload.get("project_ids")
    if "project_ids" in payload:
        if (not isinstance(project_ids, list) or not project_ids
                or any(not isinstance(pid, str) or not pid.strip()
                       for pid in project_ids)):
            raise util.bad_request("project_ids 必须为非空字符串列表")
        project_ids = sorted(set(project_ids))
    # Already-authorized builds keep progressing even when all members were
    # historical done. Network reads precede the business-cycle writer lock.
    release_results = release_delivery.sync_existing(ctx, project_ids=project_ids, limit=3)
    # Recover a committed Review result before entering tick's write transaction.
    # This helper only starts already-authorized managed repairs; manual rows stay held.
    from . import review_repair
    for task in ctx.conn.execute("SELECT * FROM tasks WHERE save_state='active'").fetchall():
        if (project_ids is None or task["project_id"] in project_ids) and core.review_repair_of(task).get("state") == "held":
            review_repair.try_auto_start(ctx, task["task_id"])
    projects = {t["project_id"] for t in ctx.conn.execute(
        "SELECT * FROM tasks WHERE status='ready'").fetchall()
        if core.is_automation_managed(t)
        and (project_ids is None or t["project_id"] in project_ids)}
    checks = {pid: automation_policy.automation_preflight(ctx.conn, pid) for pid in projects}
    with db.write_txn(ctx.conn):
        result = tick_inline(ctx.conn, ctx.now, checks, project_ids=project_ids)
    if release_results.get("results"):
        result["release_batches"] = release_results["results"]
    return result


def _board_attention(ctx) -> dict:
    """A15: the board ``attention`` section — the SAME inbox as ``attention-list``.

    Delegates to ``engine.commands.attention_items`` (single aggregation source);
    no second aggregation logic here. Shape matches the ``attention-list``
    command output exactly.
    """
    from .engine.commands import cmd_attention_list
    return cmd_attention_list(ctx, {})


def cmd_overview(ctx, payload: dict) -> dict:
    conn = ctx.conn
    task_counts = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE save_state='active' GROUP BY status"
    ).fetchall():
        task_counts[row["status"]] = row["n"]
    request_counts = {"queued": 0, "claimed": 0, "lost": 0}
    # v2 §5.20.1：purged 任务的请求退出常规统计。
    for row in conn.execute(
        "SELECT r.status, COUNT(*) AS n FROM requests r "
        "LEFT JOIN tasks t ON t.task_id = r.task_id "
        "WHERE r.task_id IS NULL OR t.save_state != 'purged' "
        "GROUP BY r.status"
    ).fetchall():
        request_counts[row["status"]] = row["n"]
    board_counts = {column: 0 for column in constants.BOARD_COLUMNS}
    # §5.15.3：计数只统计活跃任务（归档/回收站不进看板）。
    for row in conn.execute(
        "SELECT * FROM tasks WHERE save_state='active'"
    ).fetchall():
        board_counts[derive.task_board_column(row, conn, ctx.now)] += 1
    lost = 0
    for row in conn.execute(
        "SELECT lease_expires_at FROM requests WHERE status='claimed'"
    ).fetchall():
        if util.lease_lost(row["lease_expires_at"], ctx.now):
            lost += 1
    request_counts["lost"] = lost
    return {
        "paused": {
            "global": db.get_setting(conn, "paused_global", False),
            "stages": db.get_setting(conn, "paused_stages", []) or [],
        },
        "counts": {
            "tasks": task_counts,
            "board": board_counts,
            "requests": request_counts,
        },
        "last_tick_at": db.get_setting(conn, "last_tick_at"),
        "schema_version": constants.SCHEMA_VERSION,
        "settings": {
            "lease_seconds": db.get_setting(
                conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS
            ),
            "monitor_interval_seconds": util.monitor_interval(conn),
            "plan_scan_interval_hours": db.get_setting(
                conn, "plan_scan_interval_hours",
                constants.DEFAULT_PLAN_SCAN_INTERVAL_HOURS,
            ),
            "plan_scan_anchor": db.get_setting(
                conn, "plan_scan_anchor", constants.DEFAULT_PLAN_SCAN_ANCHOR
            ),
            # v1.3：会话订阅不可用时的增量检查间隔（ui-spec §4.4）。
            "session_poll_seconds": db.get_setting(
                conn, "session_poll_seconds",
                constants.DEFAULT_SESSION_POLL_SECONDS,
            ),
            # v1.4：headless 运行记录根目录（空 = 未设置，取默认；Settings 页
            # 编辑用，protocol.md §5.15.5）。
            "session_runs_root": db.get_setting(
                conn, "session_runs_root", ""
            ) or str(constants.default_session_runs_root()),
            "migration": db.get_setting(conn, "migration"),
            # v2 §5.22：规划材料根与 Codex 超时（None=回退 executor 超时）。
            "planner_inputs_root": db.get_setting(
                conn, "planner_inputs_root", ""
            ) or str(constants.planner_inputs_root()),
            "codex_timeout_seconds": db.get_setting(
                conn, "codex_timeout_seconds"
            ),
        },
        "paths": {
            "db": str(ctx.db_path),
            "plan_root": str(constants.plan_root()),
            "repos_root": str(constants.repos_root()),
            "app_support": str(constants.app_support()),
            "core": str(constants.core_root()),
            # v1.4：headless 运行记录生效根目录（settings 覆盖 > 默认；
            # display-only，不参与 doctor ok 判定；protocol.md §5.15.5）。
            "session_runs_root": str(
                db.get_setting(
                    conn, "session_runs_root", ""
                ) or constants.default_session_runs_root()
            ),
            # v2 §5.22：规划材料根生效路径。
            "planner_inputs_root": str(
                constants.planner_inputs_root(
                    db.get_setting(conn, "planner_inputs_root")
                )
            ),
        },
        # A15：attention 收件箱（与 attention-list 同一来源，逐字段一致）。
        "attention": _board_attention(ctx),
    }


def cmd_list_tasks(ctx, payload: dict) -> dict:
    board_filter = payload.get("board_column")
    if board_filter is not None and board_filter not in constants.BOARD_COLUMNS:
        raise util.KanpilotError(
            "bad_request", f"invalid board_column {board_filter}"
        )
    # §5.15.3：save_state 过滤，缺省只返回 active（归档/回收站不进看板）。
    # v2 §5.20.1：purged 是终态——常规列表（含 save_state=all）一律排除；
    # 仅显式 save_state=purged 或按 id get-task 可读审计详情。
    save_filter = payload.get("save_state") or "active"
    if save_filter not in ("active", "archived", "trashed", "purged", "all"):
        raise util.KanpilotError(
            "bad_request", f"invalid save_state {save_filter}"
        )
    clauses, params = [], []
    if payload.get("stage"):
        clauses.append("stage=?")
        params.append(payload["stage"])
    if payload.get("status"):
        clauses.append("status=?")
        params.append(payload["status"])
    if save_filter == "all":
        clauses.append("save_state != 'purged'")
    else:
        clauses.append("save_state=?")
        params.append(save_filter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = ctx.conn.execute(
        f"SELECT * FROM tasks{where} ORDER BY updated_at DESC", params
    ).fetchall()
    tasks = []
    for row in rows:
        # board_column 是派生值，只能在派生后过滤（protocol.md §5.2/§5.15.4）；
        # 失联归列需要 conn/now（§5.15.4 规则 3）。
        if board_filter is not None and (
            derive.task_board_column(row, ctx.conn, ctx.now) != board_filter
        ):
            continue
        if payload.get("needs_attention"):
            attention = row["status"] == "needs_user" or (
                derive.lost_request_for_task(ctx.conn, row["task_id"], ctx.now)
                is not None
            )
            if not attention:
                continue
        tasks.append(derive.task_summary(ctx.conn, row, ctx.now))
    # A15：attention 收件箱（与 attention-list 同一来源，逐字段一致）。
    return {"tasks": tasks, "attention": _board_attention(ctx)}


def cmd_get_task(ctx, payload: dict) -> dict:
    task = db.require_task(ctx.conn, payload["task_id"])
    return {"task": derive.task_detail(ctx.conn, task, ctx.now)}


def cmd_get_plan(ctx, payload: dict) -> dict:
    """get-plan（protocol.md §5.23.1，v3 T1）：已发布计划正文的核验读取。

    以任务登记的 plan_path 为唯一入口，经 planfile.load_plan 做 Plan 根边界/
    无符号链接/frontmatter task_id 身份校验；重算整个文件 SHA-256 与登记值
    比较。不可信正文不返回；本命令只读（不写状态/事件）。
    """
    task = db.require_task(ctx.conn, payload["task_id"])
    expected = payload.get("expected_plan_sha256")
    registered = task["plan_sha256"]
    if not task["plan_path"] or not registered:
        raise util.KanpilotError(
            "not_found", f"任务 {task['task_id']} 尚未发布计划"
        )
    if expected is not None and expected != registered:
        raise util.KanpilotError(
            "conflict",
            "expected_plan_sha256 与登记计划哈希不符（任务可能已重新规划）",
        )
    meta, raw, _path = planfile.load_plan(
        task["plan_path"], expect_task_id=task["task_id"]
    )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != registered:
        raise util.KanpilotError(
            "conflict",
            "计划文件哈希与登记值不符（计划自发布后被修改）",
        )
    # markdown = 去除 YAML frontmatter 后的正文（raw 已被 read_frontmatter
    # UTF-8 解码校验过）。与 util.read_frontmatter 同口径 split("---", 2)。
    text = raw.decode("utf-8")
    body = text.split("---", 2)[2] if text.startswith("---\n") else text
    if body.startswith("\n"):
        body = body[1:]
    result = {
        "task_id": task["task_id"],
        "title": task["title"],
        "plan_kind": task["plan_kind"] or meta.get("plan_kind") or "standard",
        "generated_at": task["plan_generated_at"] or meta.get("generated_at"),
        "plan_sha256": registered,
        "markdown": body,
    }
    if task["save_state"] == "purged":
        # §5.20.1：purged 终态仍可只读审计，附只读标记。
        result["read_only"] = True
    return result

def cmd_get_request(ctx, payload: dict) -> dict:
    """v2 §5.21.1：请求详情 + attempts + allowed_actions + attention_state。"""
    request = db.get_request(ctx.conn, payload["request_id"])
    if request is None:
        raise util.KanpilotError(
            "not_found", f"request not found: {payload['request_id']}"
        )
    from . import cmd_exec
    result = derive.request_detail(ctx.conn, request, ctx.now)
    result["request"]["backend"] = cmd_exec.request_backend(ctx.conn, request)
    return result


def cmd_list_requests(ctx, payload: dict) -> dict:
    status = payload.get("status")
    if status is not None and status not in (
        "queued", "claimed", "lost", "failed", "succeeded",
        "cancelled", "active",
    ):
        raise util.KanpilotError(
            "bad_request", f"invalid status filter {status}"
        )
    limit = payload.get("limit") or 200
    # v2 §5.20.2：purged 任务的请求退出常规列表；include_removed 显式回看。
    include_removed = bool(payload.get("include_removed"))
    clauses, params = [], []
    if status and status not in ("active", "lost"):
        clauses.append("r.status=?")
        params.append(status)
    elif status == "active":
        clauses.append("r.status IN ('queued','claimed')")
    elif status == "lost":
        # 失联判定在 SQL 层完成（租约过期），避免 LIMIT 截掉真实失联行。
        clauses.append(
            "r.status='claimed' AND r.lease_expires_at < ?")
        params.append(util.to_iso(ctx.now))
    if not include_removed:
        clauses.append("(r.task_id IS NULL OR t.save_state != 'purged')")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = ctx.conn.execute(
        "SELECT r.* FROM requests r "
        "LEFT JOIN tasks t ON t.task_id = r.task_id"
        f"{where} ORDER BY r.created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    views = []
    dismissed = db.get_setting(ctx.conn, "dismissed_failed_requests", {})
    dismissed = dismissed if isinstance(dismissed, dict) else {}
    from . import cmd_exec
    for row in rows:
        view = derive.request_view(row, ctx.now)
        view["backend"] = cmd_exec.request_backend(ctx.conn, row)
        view["failure_dismissed_at"] = dismissed.get(row["request_id"])
        task = db.get_task(ctx.conn, row["task_id"]) if row["task_id"] else None
        view["allowed_actions"] = derive.request_allowed_actions(ctx.conn, row, task, ctx.now)
        views.append(view)
    return {"requests": views}


_EVENT_RESULT_VALUES = ("succeeded", "failed", "cancelled", "needs_user", "lost")


def cmd_list_events(ctx, payload: dict) -> dict:
    """v2 §5.21.2：id DESC 游标分页 + stage/result/query 全史过滤 + 派生字段。

    before_id 为严格小于该 id 的游标；limit 缺省 100、上限 200（>200 按 200，
    非法值 bad_request）。搜索/过滤由核心在全部历史上执行（先过滤再分页）。
    """
    limit = payload.get("limit")
    if limit is None:
        limit = 100
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise util.bad_request("limit must be a positive integer")
    if limit > 200:
        limit = 200
    before_id = payload.get("before_id")
    if before_id is not None:
        if not isinstance(before_id, int) or isinstance(before_id, bool)                 or before_id <= 0:
            raise util.bad_request("before_id must be a positive integer")
    stage = payload.get("stage")
    if stage is not None and stage not in (
        "plan", "dispatch", "monitor", "review", "done",
    ):
        raise util.bad_request(f"invalid stage filter {stage}")
    result_f = payload.get("result")
    if result_f is not None and result_f not in _EVENT_RESULT_VALUES:
        raise util.bad_request(f"invalid result filter {result_f}")
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise util.bad_request("query must be a string")
    query_l = (query or "").strip().lower()

    clauses, params = [], []
    if payload.get("task_id"):
        clauses.append("task_id=?")
        params.append(payload["task_id"])
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = ctx.conn.execute(
        f"SELECT * FROM events{where} ORDER BY id DESC", params
    ).fetchall()

    task_cache: dict = {}
    request_cache: dict = {}
    filtered = []
    for row in rows:
        view = derive.event_view(
            ctx.conn, row, task_cache=task_cache, request_cache=request_cache
        )
        if stage is not None and view["stage"] != stage:
            continue
        if result_f is not None and view["result"] != result_f:
            continue
        if query_l:
            haystack = "\n".join(
                str(part)
                for part in (
                    view["task_title"], view["project_name"],
                    view["task_id"], view["request_id"], view["kind"],
                    view["summary"],
                    util.jdump(view["detail"]) if view["detail"] else "",
                )
                if part
            ).lower()
            if query_l not in haystack:
                continue
        filtered.append(view)

    page = filtered[:limit]
    next_before_id = (
        page[-1]["id"] if len(filtered) > limit and page else None
    )
    return {"events": page, "next_before_id": next_before_id}


# ---------------------------------------------------------------- doctor 探针
# protocol.md §5.27.6：在线诊断。所有探针超时可经 KANPILOT_DOCTOR_TIMEOUT
# （秒）覆盖，缺省 5s。**超时与真实失败必须分开表达**（``timed_out`` 标记）：
# 探测慢不等于服务坏，不得把超时误报成确定性失败。

DOCTOR_PROBE_TIMEOUT_SECONDS = 5.0


def _doctor_probe_timeout() -> float:
    """doctor 探针超时（秒）：KANPILOT_DOCTOR_TIMEOUT 覆盖，非法值回落缺省。"""
    raw = os.environ.get("KANPILOT_DOCTOR_TIMEOUT", "")
    try:
        value = float(raw) if raw else DOCTOR_PROBE_TIMEOUT_SECONDS
    except ValueError:
        value = DOCTOR_PROBE_TIMEOUT_SECONDS
    return value if value > 0 else DOCTOR_PROBE_TIMEOUT_SECONDS


def _probe_timed_out(exc) -> bool:
    """沿 ``__cause__`` / ``reason`` 链判断异常是否为探测超时。

    bridge/pi_desktop 的网络错误会包装底层 socket.timeout；仅凭最外层
    类型名分不清超时与拒绝连接，必须走链判断。
    """
    seen = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (socket.timeout, TimeoutError)):
            return True
        text = str(current).lower()
        if ("timed out" in text or "timeout" in text or "timeouterror" in text
                or "超时" in str(current)):
            return True
        stack.append(getattr(current, "__cause__", None))
        stack.append(getattr(current, "reason", None))
    return False


def _probe_check(name, *, ok, detail, skipped=False, timed_out=False) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail,
            "skipped": bool(skipped), "timed_out": bool(timed_out)}


def _doctor_kaneo_ping(timeout: float) -> dict:
    """Kaneo 连通性（§5.27.6）：HTTP 200 = 通；超时与真实失败分开。"""
    try:
        client = remote.get_client()
        if hasattr(client, "timeout"):
            client.timeout = timeout  # 探测超时独立于 KANEO_TIMEOUT
        client.request("/api/project?workspaceId=" + constants.WORKSPACE_ID)
    except Exception as exc:
        timed_out = _probe_timed_out(exc)
        detail = str(exc) or exc.__class__.__name__
        if timed_out:
            detail = f"探测超时（{timeout:g}s）：{detail}"
        return _probe_check("kaneo_ping", ok=False, detail=detail,
                            timed_out=timed_out)
    return _probe_check("kaneo_ping", ok=True, detail="HTTP 200")


def _doctor_gh(timeout: float) -> dict:
    """gh 可执行探测（gh_missing 前置）：存在且可运行；缺失/超时/退出非零分开。"""
    gh = constants.gh_bin()
    try:
        result = subprocess.run([gh, "--version"], capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _probe_check("gh", ok=False,
                            detail=f"探测超时（{timeout:g}s）：gh --version 未在时限内返回",
                            timed_out=True)
    except OSError:
        return _probe_check("gh", ok=False, detail="未找到 gh")
    if result.returncode:
        return _probe_check("gh", ok=False,
                            detail=f"gh --version 退出 {result.returncode}")
    lines = (result.stdout or "").strip().splitlines()
    return _probe_check("gh", ok=True, detail=lines[0] if lines else "gh 可用")


def _doctor_claude(timeout: float) -> dict:
    """Claude CLI local availability only; --version never invokes a model."""
    try:
        result = subprocess.run([constants.claude_bin(), "--version"],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _probe_check("claude", ok=False,
                            detail=f"探测超时（{timeout:g}s）：claude --version 未在时限内返回",
                            timed_out=True)
    except OSError:
        return _probe_check("claude", ok=False, detail="未找到 claude")
    if result.returncode:
        return _probe_check("claude", ok=False,
                            detail=f"claude --version 退出 {result.returncode}")
    lines = (result.stdout or "").strip().splitlines()
    return _probe_check("claude", ok=True, detail=lines[0] if lines else "claude 可用")


def _doctor_plus(ctx, timeout: float) -> dict:
    """Plus（PI-Desktop）控制面探测：连接文件校验 + MCP 握手；绝不输出 token。

    执行后端为 pi 且未配置连接文件 → skipped（Plus 非当前执行路径）；
    backend=pi_desktop 却缺连接文件 → 真实失败（自动化就绪条件不满足）。
    """
    try:
        from . import exec_config, pi_desktop_backend
    except Exception as exc:  # pragma: no cover - defensive
        return _probe_check("plus", ok=False, detail=f"Plus 模块缺失：{exc}")
    try:
        config = exec_config.load(ctx.conn)
    except Exception as exc:
        return _probe_check("plus", ok=False, detail=f"执行配置不可读：{exc}")
    backend = config.get("backend") or "pi"
    block = config.get("pi_desktop") or {}
    conn_file = block.get("connection_file") or constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE
    path = Path(str(conn_file)).expanduser()
    if not path.is_file():
        if backend == "pi_desktop":
            return _probe_check("plus", ok=False,
                                detail=f"Plus 连接文件不存在：{path}")
        return _probe_check("plus", ok=True,
                            detail=f"未配置 Plus 连接文件（执行后端 {backend}）",
                            skipped=True)
    try:
        probe = pi_desktop_backend.probe_connection(path, timeout=timeout)
    except Exception as exc:  # pragma: no cover - defensive
        return _probe_check("plus", ok=False, detail=f"Plus 探测异常：{exc}")
    detail = probe.get("detail") or (
        "Plus 控制面就绪" if probe.get("ok") else "Plus 控制面不可用")
    return _probe_check("plus", ok=probe.get("ok"), detail=detail,
                        timed_out=_probe_timed_out(detail))


def _doctor_plus_fallback_model(ctx, timeout: float) -> dict:
    """plus_fallback_model（spike-fallback-model-id-20261010.md 的失效与刷新）：
    核对各项目配置的 ``fallback_model`` 仍存在于 Plus 本地 cache 目录。

    只读（``providers/list`` + ``providers/listModels {source:"cache"}``，复用
    ``probe_connection``），**绝不输出 token、绝不换模型**——目录可能随 Plus
    升级/账号变更过期，探不到就给出明确修复项（换备选或刷新目录），与引擎侧
    「探针失败即 validation_failed、不静默换模型」是同一条不变量。

    - 未配置 → informational skip（缺省不配置 = 不换模型，无需核对）；
    - 无连接文件 → informational skip（与 ``_doctor_plus`` 同口径）；
    - 配置且存在 → ok；
    - 配置但不存在 → ok=False + 可执行修复项（换 spike 备选或刷新目录）。
    """
    try:
        from . import exec_config, pi_desktop_backend, project_config
        from .engine import fallback as engine_fallback
    except Exception as exc:  # pragma: no cover - defensive
        return _probe_check("plus_fallback_model", ok=True,
                            detail=f"fallback 模块缺失，跳过核对：{exc}",
                            skipped=True)
    try:
        _rev, _ws, items, _saved = project_config.load_raw_mappings(ctx.conn)
    except Exception as exc:
        return _probe_check("plus_fallback_model", ok=True,
                            detail=f"项目映射不可读，跳过核对：{exc}",
                            skipped=True)
    models = engine_fallback.configured_models(items)
    if not models:
        return _probe_check(
            "plus_fallback_model", ok=True,
            detail="未配置 fallback_model（缺省不换模型，无需核对）", skipped=True)

    block = (exec_config.load(ctx.conn).get("pi_desktop") or {})
    conn_file = block.get("connection_file") or constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE
    path = Path(str(conn_file)).expanduser()
    if not path.is_file():
        return _probe_check(
            "plus_fallback_model", ok=True,
            detail=f"无 Plus 连接文件，无法核对目录：{path}", skipped=True)

    verified, missing = [], []
    for model in models:
        try:
            probe = pi_desktop_backend.probe_connection(
                path, provider_id=model["provider_id"],
                model_id=model["model_id"], timeout=timeout)
        except Exception as exc:  # pragma: no cover - defensive
            missing.append((model, f"探测异常：{exc}"))
            continue
        if probe.get("ok"):
            verified.append(f"{model['model_id']}@{model['provider_id'][:8]}")
        else:
            missing.append((model, str(probe.get("detail") or "模型不在 cache 目录")))
    if not missing:
        return _probe_check("plus_fallback_model", ok=True,
                            detail="已核对存在：" + "、".join(verified))
    alternates = "、".join(engine_fallback.RECOMMENDED_ALTERNATES)
    detail = "；".join(
        f"{model['model_id']}@{model['provider_id'][:8]} 不存在（{reason}）"
        for model, reason in missing)
    return _probe_check(
        "plus_fallback_model", ok=False,
        detail=(detail + f"。修复：改用同 provider 备选（{alternates}）"
                "或刷新 Plus 模型目录后重跑 doctor --live；"
                "引擎不会静默换模型，届时按 validation_failed 阻塞"))


def _doctor_engine_lease(ctx) -> dict:
    """engine_lease（§5.27.6）：活跃引擎实例数。引擎未安装 → informational。"""
    try:
        from . import cli as _cli
        commands = getattr(_cli, "COMMANDS", {})
        engine_installed = any(
            key == "engine" or key.startswith("engine-") for key in commands)
    except Exception:  # pragma: no cover - defensive
        engine_installed = False
    from . import process_owner
    instances = db.get_setting(ctx.conn, process_owner.SETTING, {}) or {}
    active = sum(1 for entry in instances.values()
                 if isinstance(entry, dict) and entry.get("state") != "stopped")
    detail = f"active instances: {active}"
    if not engine_installed:
        detail += "（引擎未安装：engine run 未实现，计数仅含 executor-instance 登记）"
    return {"name": "engine_lease", "ok": True, "detail": detail}
def _doctor_attention_backlog(ctx) -> dict:
    """attention_backlog（§5.27.6/§5.27.3）：待处理聚合计数（informational）。

    单一来源 = ``engine.attention``（§5.27.3 的聚合与计数）；引擎模块不可用
    时回落本地 SQL（needs_user 任务 + failed 请求 + 派生 lost）。
    """
    try:
        from .engine import attention as engine_attention
        from .engine import commands as engine_commands
        items = engine_commands.cmd_attention_list(
            ctx, {"include_historical": True})["items"]
        counts = engine_attention.counts(items)
        detail = (f"actionable: {counts['actionable']}, "
                  f"historical: {counts['historical']}")
        if counts.get("informational"):
            detail += f", informational: {counts['informational']}"
        return {"name": "attention_backlog", "ok": True, "detail": detail}
    except Exception:
        pass  # 引擎未就绪 → 下方本地计数兜底
    actionable = ctx.conn.execute(
        "SELECT COUNT(*) FROM tasks "
        "WHERE status='needs_user' AND save_state='active'"
    ).fetchone()[0]
    historical = 0
    rows = ctx.conn.execute(
        "SELECT r.status, r.lease_expires_at, t.save_state FROM requests r "
        "LEFT JOIN tasks t ON t.task_id = r.task_id "
        "WHERE r.status IN ('failed', 'claimed')"
    ).fetchall()
    for row in rows:
        if row["status"] == "failed":
            if (row["save_state"] or "active") != "active":
                historical += 1
            else:
                actionable += 1
        elif row["lease_expires_at"] and row["lease_expires_at"] < ctx.now_iso:
            actionable += 1  # claimed 且租约过期 → 派生 lost
    return {"name": "attention_backlog", "ok": True,
            "detail": f"actionable: {actionable}, historical: {historical}"}



def _doctor_business_tick(ctx) -> dict:
    from .engine import health
    check = {"name": "business_tick_fresh", "ok": True}
    if not health.active_instance_count(ctx.conn, ctx.now):
        return dict(check, detail="没有活动引擎实例")
    if not health.has_live_observation_tasks(ctx.conn, ctx.now):
        return dict(check, detail="活动引擎没有需观察的 live 任务")
    status = health.business_tick_status(ctx.conn, ctx.now)
    age = status["age_seconds"]
    if age is None:
        return dict(check, ok=False,
                    detail="业务 tick 时间缺失或无效；检查 business_tick actions，必要时重启引擎")
    if age > constants.BUSINESS_TICK_FRESH_SECONDS:
        return dict(check, ok=False,
                    detail=f"业务 tick 已过去 {age:.0f} 秒；检查 business_tick actions，必要时重启引擎")
    return dict(check, detail=f"业务 tick 已过去 {age:.0f} 秒")


def cmd_doctor(ctx, payload: dict) -> dict:
    # §5.27.6：`--live` 为 `online=true` 的同义（CLI flag 合并进 payload）。
    online = bool(payload.get("online") or payload.get("live"))
    timeout = _doctor_probe_timeout()
    checks = []

    checks.append({
        "name": "python",
        "ok": sys.version_info >= (3, 9),
        "detail": ".".join(str(part) for part in sys.version_info[:3]),
    })

    try:
        ctx.conn.execute("SELECT 1").fetchone()
        checks.append(
            {"name": "db_writable", "ok": True, "detail": str(ctx.db_path)}
        )
    except sqlite3.Error as exc:
        checks.append({"name": "db_writable", "ok": False, "detail": str(exc)})

    creds_ok, creds_detail = False, "missing"
    try:
        from . import bridge

        base_url, _token = bridge.load_credentials()
        creds_ok, creds_detail = True, f"present, baseUrl {base_url}"
    except Exception as exc:
        creds_detail = str(exc)
    checks.append({"name": "credentials", "ok": creds_ok, "detail": creds_detail})

    for name, path in (
        ("plan_root", constants.plan_root()),
        ("repos_root", constants.repos_root()),
        ("state_root", constants.app_support()),
    ):
        checks.append({"name": name, "ok": path.is_dir(), "detail": str(path)})

    # v2 §5.22.3：Codex 规划环境（可执行 + 登录态就绪；不读凭据值）。
    try:
        from . import codex_planner

        codex_ok, codex_detail = codex_planner.check_available()
    except Exception as exc:  # pragma: no cover - defensive
        codex_ok, codex_detail = False, str(exc)
    checks.append(
        {"name": "codex_planner", "ok": codex_ok, "detail": codex_detail}
    )

    if online:
        checks.append(_doctor_kaneo_ping(timeout))
        checks.append(_doctor_gh(timeout))
        checks.append(_doctor_claude(timeout))
        from . import planner_config, review_config
        if planner_config.backend(ctx.conn) == "qoder" or review_config.backend(ctx.conn) == "qoder":
            from . import qoder_cli
            qoder = qoder_cli.capabilities(timeout=timeout)
            checks.append(_probe_check("qoder", ok=qoder["available"], detail=qoder["detail"]))
        checks.append(_doctor_plus(ctx, timeout))
        # spike-fallback-model-id-20261010.md「失效与刷新」：核对配置的
        # fallback 模型仍在 Plus cache 目录；只在 online 有意义。
        checks.append(_doctor_plus_fallback_model(ctx, timeout))
    else:
        checks.append(_probe_check("kaneo_ping", ok=True, detail="offline",
                                   skipped=True))
        checks.append(_probe_check("gh", ok=True, detail="offline",
                                   skipped=True))
        checks.append(_probe_check("plus", ok=True, detail="offline",
                                   skipped=True))
    # §5.27.6：engine_lease 与 attention_backlog 为 informational 检查项；
    # 引擎未实现时返回「未安装」而非失败。
    checks.append(_doctor_engine_lease(ctx))
    checks.append(_doctor_business_tick(ctx))
    checks.append(_doctor_attention_backlog(ctx))
    from . import worktree_status
    pool = worktree_status.summary(ctx.conn)
    checks.append({"name": "worktree_pool", "ok": not (pool["quarantined"] or pool["unconfirmed"]),
                   "detail": f"free: {pool['free']}, leased: {pool['leased']}, "
                             f"quarantined: {pool['quarantined']}, unconfirmed: {pool['unconfirmed']}, "
                             f"waiting: {len(pool['waiting'])}"})
    # §5.27.6：doctor 输出绝不泄露凭据。所有 check 的 detail 统一过一道脱敏
    # （util.redact，protocol §5.22.3），不依赖每个探针自己构造得正确——
    # 例如 `plus` 检查原样透传 probe["detail"]，此前只靠「detail 由本仓构造」
    # 兜底；这里把它变成可执行保证。
    for check in checks:
        check["detail"] = util.redact(check["detail"])
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


# ===================================== Goal 报告读取与完成判定（T5，§5.23.10）
# 本段实现 T5-A/T5-B/T5-C 的核心侧：
# - ``goal_round_identity``：解析一轮 pi_desktop 轮次的报告身份材料（只读）；
# - ``read_goal_report``：只读取回报告并做身份核对（跨轮/身份不符 → 确定性
#   失败，绝不返回伪造正文）；
# - ``verify_goal_completion``：确定性报告底线（T5-C），供
#   ``record-goal-evaluation`` 与 ``advance-task`` 硬闸复用；
# - ``cmd_get_goal_report``：monitor AI 的只读入口。

# 报告身份材料缺失/不符的失败原因（机器可读，供执行端与测试断言）。
GOAL_REPORT_MISSING_APPROVAL = "goal_approval_missing"
GOAL_REPORT_NOT_PI_DESKTOP = "round_not_pi_desktop"
GOAL_REPORT_JOB_UNREADABLE = "job_unreadable"
GOAL_REPORT_SESSION_MISSING = "job_session_missing"
GOAL_REPORT_TOOL_MISSING = "goal_report_tool_missing"
GOAL_REPORT_IDENTITY_MISMATCH = "goal_report_identity_mismatch"
GOAL_REPORT_UNREADABLE = "goal_report_unreadable"


def _goal_fail(code: str, reason: str, detail: str = "") -> None:
    """确定性失败（中文原因 + 机器可读前缀），绝不返回伪造报告正文。"""
    message = f"{reason}：{detail}" if detail else reason
    raise util.KanpilotError(code, message)


def goal_round_identity(conn, task_id: str, run_id: str) -> dict:
    """解析 pi_desktop 轮次的报告身份材料（只读；§5.23.10 T5-B）。

    返回 ``{"task", "round", "job", "events", "run_dir", "session_id",
    "proposal_id", "execution_id", "approved_turns", "plan_sha256",
    "connection_file", "goal_approval"}``。

    任一前置不成立（轮次非 pi_desktop、job 身份不符、缺 session_id、
    缺已批准 ``goal_approval``）→ ``KanpilotError``（确定性失败）。
    """
    from . import dsh_events, headless_adapter, round_store

    if not isinstance(task_id, str) or not task_id:
        raise util.bad_request("task_id 必须为非空字符串")
    if not isinstance(run_id, str) or not run_id:
        raise util.bad_request("run_id 必须为非空字符串")
    task = db.require_task(conn, task_id)
    row = round_store.get_round(conn, task_id, run_id)
    if row is None:
        raise util.KanpilotError(
            "not_found", f"execution round not found: {task_id}/{run_id}"
        )
    if row["backend"] != "pi_desktop":
        _goal_fail(
            "validation_failed",
            f"该轮次不是 pi_desktop 后端（{row['backend']}），没有 Goal 报告",
            GOAL_REPORT_NOT_PI_DESKTOP,
        )
    config = round_store.round_view(row).get("config") or {}
    source = headless_adapter.resolve_source(conn)
    try:
        run_dir = source.run_dir(task_id, run_id)
    except headless_adapter.HeadlessError as exc:
        _goal_fail("validation_failed", "运行记录目录不可用", exc.detail)
    job, error = dsh_events.read_job(
        run_dir, task_id, run_id, backend="pi_desktop"
    )
    if job is None:
        _goal_fail(
            "validation_failed", "job.json 不可读或身份不符",
            f"{GOAL_REPORT_JOB_UNREADABLE}：{error}",
        )
    events, _detail = dsh_events.iter_events(
        run_dir / dsh_events.EVENT_LOG_NAME
    )
    session_id = job.get("session_id")
    if not session_id:
        _goal_fail(
            "conflict", "该轮次缺少持久会话身份，无法关联 Goal 报告",
            GOAL_REPORT_SESSION_MISSING,
        )
    approval = job.get("goal_approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        # T5-B：无 job.goal_approval（或未批准）→ 不能放行。
        _goal_fail(
            "conflict",
            "本轮没有已批准的 Goal 契约（job.goal_approval 缺失或未批准），"
            "无法关联完成报告",
            GOAL_REPORT_MISSING_APPROVAL,
        )
    proposal_id = approval.get("proposal_id")
    execution_turn = approval.get("execution_turn_id")
    if not proposal_id or not execution_turn:
        _goal_fail(
            "conflict",
            "Goal 契约留痕缺少 proposal_id/execution_turn_id",
            GOAL_REPORT_MISSING_APPROVAL,
        )
    # turnId 允许沿本轮已记录的 turn_adopted 链核对（§5.23.8）——瞬时错误后
    # 同一会话自动重开的后续 turn 仍属本轮。
    approved_turns = {str(execution_turn)}
    for item in job.get("turn_adopted") or []:
        if isinstance(item, dict) and item.get("to"):
            approved_turns.add(str(item["to"]))
    return {
        "task": task,
        "round": row,
        "job": job,
        "events": events,
        "run_dir": run_dir,
        "session_id": str(session_id),
        "proposal_id": str(proposal_id),
        "execution_id": (str(approval["execution_id"])
                         if approval.get("execution_id") else None),
        "approved_turns": approved_turns,
        "plan_sha256": task["plan_sha256"],
        "connection_file": config.get("connection_file"),
        "goal_approval": approval,
    }


def _report_candidates(identity: dict, client) -> list:
    """定位本轮的 Goal 报告候选（只读；优先已记录的 execution_id）。

    没有记录 execution_id 的旧轮次（升级时尚在运行的旧 Plus 轮次）：以
    ``pi_goal_report_list {sessionId}`` 只读列出本会话报告，按
    ``proposalId`` + 已批准 turn 收敛到唯一一份；0 份 → ``not_found``，
    多份 → ``conflict``（歧义不得猜测）。
    """
    if identity["execution_id"]:
        return [{"executionId": identity["execution_id"], "reportId": None}]
    try:
        listing = client.goal_report_list(identity["session_id"])
    except util.KanpilotError as exc:
        return [{"error": exc}]
    rows = listing.get("reports") if isinstance(listing, dict) else None
    matches = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        proposal = item.get("proposalId") or item.get("proposal_id")
        turn = item.get("turnId") or item.get("turn_id")
        execution = item.get("executionId") or item.get("execution_id")
        report_id = item.get("reportId") or item.get("report_id")
        if proposal is not None and str(proposal) != identity["proposal_id"]:
            continue
        if turn is not None and str(turn) not in identity["approved_turns"]:
            continue
        matches.append({"executionId": execution, "reportId": report_id})
    return matches
def read_goal_report(conn, task_id: str, run_id: str, *,
                     client=None, connection_file=None) -> dict:
    """只读取回本轮 Goal 报告并完成身份核对（§5.23.10 T5-A/T5-B）。

    返回 ``{"identity", "state", "detail", "report", "report_sha256",
    "report_id", "session_id", "proposal_id", "execution_id", "turn_id",
    "ready", "structured", "execution_status", "verdict"}``。

    **绝不**返回跨轮/身份不符的报告正文；``state="ready"`` 且身份全部相符
    时 ``ready=True``。语义判断（AI 判定）不属于本函数职责。
    """
    from . import pi_desktop_backend

    identity = goal_round_identity(conn, task_id, run_id)
    connection_file = connection_file or identity["connection_file"]
    if client is None:
        if not connection_file:
            _goal_fail(
                "validation_failed",
                "该轮次未记录 Plus 连接文件，无法读取 Goal 报告",
                GOAL_REPORT_UNREADABLE,
            )
        record = pi_desktop_backend.load_connection(connection_file)
        client = pi_desktop_backend.MCPClient(
            record["url"], record["token"], timeout=30
        )
        client.connect()
        tools = client.tools_list()
        if pi_desktop_backend.GOAL_REPORT_GET_TOOL not in tools:
            _goal_fail(
                "validation_failed",
                "PI-Desktop 控制面缺少只读报告工具 "
                f"{pi_desktop_backend.GOAL_REPORT_GET_TOOL}"
                "（Plus 版本过旧），不能放行",
                GOAL_REPORT_TOOL_MISSING,
            )
    candidates = _report_candidates(identity, client)
    if candidates and isinstance(candidates[0].get("error"), util.KanpilotError):
        exc = candidates[0]["error"]
        _goal_fail(
            "remote_unavailable",
            "Goal 报告列表读取失败（临时读取失败，不得当作没有报告）",
            exc.message,
        )
    if not candidates:
        # 本会话没有本轮的报告：明确「没有报告」，不是伪造的完成报告。
        return _report_result(identity, {
            "state": "not_found", "detail": "本会话没有本轮已批准的 Goal 报告",
            "report": None, "report_sha256": None, "report_id": None,
        })
    if len(candidates) > 1:
        _goal_fail(
            "conflict",
            "本会话同轮次存在多份 Goal 报告，身份歧义不猜测",
            GOAL_REPORT_IDENTITY_MISMATCH,
        )
    target = candidates[0]
    try:
        payload = client.goal_report_get(
            identity["session_id"],
            execution_id=target.get("executionId"),
            report_id=target.get("reportId"),
        )
    except util.KanpilotError as exc:
        # tool 级失败按分态处理：报告自身的 pending/draft/failed/corrupt/
        # truncated 是**明确分态**；其余（连接/传输/未知）是临时读取失败，
        # 必须区别于「没有报告」（§5.23.10 T5-A/T5-D）。
        state = pi_desktop_backend.classify_report_error(
            exc.code, exc.message
        )
        if state in pi_desktop_backend.GOAL_REPORT_STATES:
            return _report_result(identity, {
                "state": state, "detail": exc.message, "report": None,
                "report_sha256": None, "report_id": target.get("reportId"),
            })
        _goal_fail(
            "remote_unavailable",
            "Goal 报告读取失败（临时读取失败，不得当作没有报告）",
            exc.message,
        )
    resolved = pi_desktop_backend.normalize_goal_report(
        payload, report_id=target.get("reportId")
    )
    if resolved["state"] != "ready":
        return _report_result(identity, resolved)
    _verify_report_identity(identity, resolved["report"])
    return _report_result(identity, resolved)
def _verify_report_identity(identity: dict, report: dict) -> None:
    """报告身份核对（§5.23.10 T5-B）；绝不按 session 名称/标题匹配。"""
    from . import pi_desktop_backend

    fields = pi_desktop_backend.report_identity(report)
    if str(fields.get("session_id") or "") != identity["session_id"]:
        _goal_fail(
            "conflict",
            "报告 sessionId 与本轮持久会话身份不符（可能是另一会话的报告）",
            GOAL_REPORT_IDENTITY_MISMATCH,
        )
    if str(fields.get("proposal_id") or "") != identity["proposal_id"]:
        _goal_fail(
            "conflict",
            "报告 proposalId 与本轮已批准的 Goal 契约不符",
            GOAL_REPORT_IDENTITY_MISMATCH,
        )
    execution_id = fields.get("execution_id")
    if not execution_id:
        _goal_fail(
            "conflict", "报告缺少 executionId，身份不完整",
            GOAL_REPORT_IDENTITY_MISMATCH,
        )
    recorded = identity.get("execution_id")
    if recorded and str(execution_id) != str(recorded):
        _goal_fail(
            "conflict", "报告 executionId 与本轮记录不符",
            GOAL_REPORT_IDENTITY_MISMATCH,
        )
    if recorded is None:
        # 旧轮次（升级时仍在运行）没有本地 executionId：以生产报告的对象为准。
        identity["execution_id"] = str(execution_id)
    turn_id = fields.get("turn_id")
    if turn_id is not None and str(turn_id) not in identity["approved_turns"]:
        # turnId 允许沿本轮 turn_adopted 链核对（§5.23.8）。
        _goal_fail(
            "conflict",
            "报告 turnId 不属于本轮已批准的执行轮次（含 turn_adopted 链）",
            GOAL_REPORT_IDENTITY_MISMATCH,
        )
def _report_result(identity: dict, resolved: dict) -> dict:
    from . import pi_desktop_backend

    report = resolved.get("report")
    return {
        "identity": identity,
        "state": resolved.get("state"),
        "detail": resolved.get("detail"),
        "report": report,
        "report_sha256": resolved.get("report_sha256"),
        "report_id": resolved.get("report_id"),
        "session_id": identity["session_id"],
        "proposal_id": identity["proposal_id"],
        "execution_id": identity.get("execution_id"),
        "turn_id": pi_desktop_backend.report_identity(report).get("turn_id"),
        "ready": resolved.get("state") == "ready",
        "structured": (
            pi_desktop_backend.report_integrity_kind(report) == "structured"
            if report else False
        ),
        "execution_status": (
            pi_desktop_backend.report_execution_status(report)
            if report else None
        ),
        "verdict": (
            pi_desktop_backend.report_verdict(report) if report else None
        ),
    }
def _fallback_report_id(report, resolved) -> Optional[str]:
    """report_id 兜底：报告自带 id > Host 返回的 executionId 组合。

    Plus 的 ``GoalReportSummary`` 身份字段以 ``reportId`` 为准；缺失时用
    ``executionId`` 作为稳定引用（同一报告同一值），并**不**冒充 AI 判定。
    """
    if isinstance(report, dict):
        for key in ("reportId", "report_id", "id"):
            value = report.get(key)
            if isinstance(value, str) and value:
                return value
    execution_id = resolved.get("execution_id")
    return f"execution:{execution_id}" if execution_id else None
def cmd_get_goal_report(ctx, payload: dict) -> dict:
    """``get-goal-report {task_id, run_id}``：monitor AI 的只读报告入口。

    返回经身份/完整性校验的报告（T5-A/T5-B）。报告是**不可信数据**：本命令
    不做语义判断，只透出报告、身份、分态与哈希；失败不伪造正文。
    """
    from . import pi_desktop_backend

    task_id = payload["task_id"]
    run_id = payload["run_id"]
    resolved = read_goal_report(ctx.conn, task_id, run_id)
    identity = resolved["identity"]
    report = resolved["report"]
    return {
        "task_id": task_id,
        "run_id": run_id,
        "session_id": resolved["session_id"],
        "proposal_id": resolved["proposal_id"],
        "execution_id": resolved["execution_id"],
        "turn_id": resolved["turn_id"],
        "state": resolved["state"],
        "detail": resolved["detail"],
        "report_sha256": resolved["report_sha256"],
        "report_id": (resolved["report_id"]
                      or _fallback_report_id(report, resolved)),
        "integrity_kind": (
            pi_desktop_backend.report_integrity_kind(report)
            if report else None
        ),
        "execution_status": resolved["execution_status"],
        "verdict": resolved["verdict"],
        "report": report,
        "goal_approval": {
            "proposal_id": identity["proposal_id"],
            "planning_turn_id": identity["goal_approval"].get(
                "planning_turn_id"
            ),
            "execution_turn_id": identity["goal_approval"].get(
                "execution_turn_id"
            ),
            "approved_at": identity["goal_approval"].get("approved_at"),
        },
        "plan_sha256": identity["plan_sha256"],
        "read_only": True,
    }
def collect_criterion_reviews(criterion_reviews) -> tuple:
    """校验 AI 审查项形状 → (reviews, referenced_ids)；失败 → bad_request。"""
    if not isinstance(criterion_reviews, list):
        raise util.bad_request("criterion_reviews 必须是数组")
    reviews = []
    referenced = set()
    for index, item in enumerate(criterion_reviews):
        if not isinstance(item, dict):
            raise util.bad_request(f"criterion_reviews[{index}] 必须是对象")
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise util.bad_request(f"criterion_reviews[{index}] 缺少 id")
        outcome = item.get("result")
        if outcome not in ("met", "missing", "failed", "unknown"):
            raise util.bad_request(
                f"criterion_reviews[{index}].result 非法：{outcome!r}"
            )
        refs = item.get("evidence_refs")
        if refs is None:
            refs = []
        if not isinstance(refs, list):
            raise util.bad_request(
                f"criterion_reviews[{index}].evidence_refs 必须是数组"
            )
        clean_refs = []
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                raise util.bad_request(
                    f"criterion_reviews[{index}].evidence_refs 含非法引用"
                )
            clean_refs.append(ref)
            referenced.add(ref)
        reviews.append({
            "id": cid,
            "result": outcome,
            "reason": item.get("reason"),
            "evidence_refs": clean_refs,
        })
    return reviews, referenced


def goal_report_floor_reason(read: dict):
    """报告底线（不含审查项）：``read_goal_report`` 结果能否作为完成证据（S1）。

    与 ``verify_goal_completion`` 的确定性部分同口径（ready、structured、
    execution.status=completed、verdict=met），但不核对 reviews：live 判定的
    语义由 Plus 报告的 checks 承载，不由 AI 审查项承载。返回不满足的中文原因；
    底线成立返回 ``None``。
    """
    if read.get("state") != "ready":
        return f"报告 state 不是 ready：{read.get('state')!r}"
    if read.get("structured") is not True:
        return "报告 integrity.kind 不是 structured：fallback 等同没有完成报告"
    if read.get("execution_status") != "completed":
        return f"报告 execution.status 不是 completed：{read.get('execution_status')!r}"
    if read.get("verdict") != "met":
        return f"报告 verdict 不是 met：{read.get('verdict')!r}"
    return None


def verify_goal_completion(report: dict, reviews: list,
                          referenced: set, *, report_state=None) -> None:
    """确定性报告底线（§5.23.10 T5-C）；任一不满足 → conflict。

    报告自称 ``verdict=met`` 是**必要但非充分**条件：这里只核对不可绕过的
    身份与报告底线，语义完备性由独立 monitor AI 判断。
    """
    from . import pi_desktop_backend

    if not isinstance(report, dict):
        raise util.KanpilotError("conflict", "缺少可用于完成判定的结构化报告")
    state = report_state if report_state is not None else report.get("status")
    if str(state or "").lower() != "ready":
        raise util.KanpilotError(
            "conflict", f"报告 status 不是 ready：{state!r}"
        )
    kind = pi_desktop_backend.report_integrity_kind(report)
    if kind != "structured":
        raise util.KanpilotError(
            "conflict",
            f"报告 integrity.kind 不是 structured（{kind!r}）："
            "fallback 等同没有完成报告",
        )
    execution_status = pi_desktop_backend.report_execution_status(report)
    if execution_status != "completed":
        raise util.KanpilotError(
            "conflict",
            f"报告 execution.status 不是 completed：{execution_status!r}",
        )
    verdict = pi_desktop_backend.report_verdict(report)
    if verdict != "met":
        raise util.KanpilotError(
            "conflict", f"报告 verdict 不是 met：{verdict!r}"
        )
    if not reviews:
        raise util.KanpilotError("conflict", "完成判定的审查项为空")
    ids = [review["id"] for review in reviews]
    if len(set(ids)) != len(ids):
        raise util.KanpilotError("conflict", "完成判定的审查项 ID 重复")
    not_met = [review["id"] for review in reviews
               if review["result"] != "met"]
    if not_met:
        raise util.KanpilotError(
            "conflict",
            "完成判定要求全部审查项为 met，非 met 项：" + "、".join(not_met),
        )
    reference_ids = pi_desktop_backend.report_reference_ids(report)
    known = set()
    for bucket in reference_ids.values():
        known |= bucket
    criteria = reference_ids.get("criteria") or set()
    if criteria:
        unknown_criteria = [cid for cid in ids if cid not in criteria]
        if unknown_criteria:
            raise util.KanpilotError(
                "conflict",
                "完成判定引用了报告中不存在的验收项 ID："
                + "、".join(unknown_criteria),
            )
    unknown_refs = sorted(ref for ref in referenced if ref not in known)
    if unknown_refs:
        raise util.KanpilotError(
            "conflict",
            "完成判定引用了报告中不存在的 check/evidence ID："
            + "、".join(unknown_refs),
        )
