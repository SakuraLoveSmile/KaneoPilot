"""Project automation policies (protocol §5.27)."""
import math
import re
import shutil
import subprocess
from pathlib import Path

from . import constants, core, db, project_config, util
from .git_delivery import github_repository


def policy_for(conn, project_id):
    _, _, items, _ = project_config.load_raw_mappings(conn)
    raw = (items.get(project_id) or {}).get("automation")
    if not isinstance(raw, dict):
        return None
    return dict(constants.AUTOMATION_DEFAULTS, **raw)


def global_settings(conn):
    return dict(constants.AUTOMATION_GLOBAL_DEFAULTS, **(db.get_setting(conn, "automation_global") or {}))



def validate_automation(raw):
    if not isinstance(raw, dict):
        raise util.bad_request("automation 必须为对象")
    result = dict(constants.AUTOMATION_DEFAULTS)
    for key in ("enabled", "auto_confirm_plan"):
        value = raw.get(key, result[key])
        if not isinstance(value, bool):
            raise util.bad_request(f"{key} 必须为布尔值")
        result[key] = value
    labels = raw.get("label_filter", [])
    if not isinstance(labels, list) or len(labels) > 20:
        raise util.bad_request("label_filter 最多 20 个字符串")
    normalized = []
    for label in labels:
        if not isinstance(label, str) or len(label.strip()) > 50 or not label.strip():
            raise util.bad_request("标签须为 1..50 字符的字符串")
        label = label.strip().lower()
        if label not in normalized:
            normalized.append(label)
    result["label_filter"] = normalized
    for key, low, high in (("max_fix_rounds", 0, 2), ("max_task_minutes", 30, 1440)):
        value = raw.get(key, result[key])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise util.bad_request(f"{key} 须为 {low}..{high} 的整数")
        result[key] = value
    cost = raw.get("max_task_cost_usd", result["max_task_cost_usd"])
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                             or not math.isfinite(cost) or not 0.5 <= cost <= 100):
        raise util.bad_request("max_task_cost_usd 须为 null 或 0.5..100")
    result["max_task_cost_usd"] = cost
    base = raw.get("delivery_base")
    if base is not None and (not isinstance(base, str) or base.startswith("-")
                             or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", base)):
        raise util.bad_request("delivery_base 非法")
    result["delivery_base"] = base
    return result


def _executable(binary):
    return bool(binary and shutil.which(str(binary)))


def _planner_ready(conn):
    from . import codex_planner, exec_config, planner_config
    backend = planner_config.backend(conn)
    if backend == "codex":
        return codex_planner.check_available()[0]
    if backend == "claude":
        return _executable(constants.claude_bin())
    block = planner_config.pi_desktop_block(conn)
    # Structural/file checks only: no local MCP handshake or network probe.
    return (exec_config.validate_pi_desktop_block(block) is None
            and Path((block or {}).get("connection_file", "")).expanduser().is_file())


def automation_preflight(conn, project_id):
    from . import exec_config, review_config
    _, _, items, _ = project_config.load_raw_mappings(conn)
    repo = (items.get(project_id) or {}).get("repository")
    errors = []
    def add(code, message):
        errors.append({"code": code, "message": message})
    if not repo or not Path(repo).expanduser().is_dir():
        add("repository_missing", "未配置仓库路径或目录不存在")
    else:
        try:
            probe = subprocess.run(["git", "-C", str(Path(repo).expanduser()), "rev-parse", "--git-dir"],
                                   capture_output=True, timeout=0.07)
            if probe.returncode:
                add("not_git_repo", "仓库不是 git 仓库")
            else:
                origin = subprocess.run(["git", "-C", str(Path(repo).expanduser()), "remote", "get-url", "origin"],
                                        capture_output=True, text=True, timeout=0.07)
                if origin.returncode or not github_repository(origin.stdout, cwd=str(Path(repo).expanduser())):
                    add("origin_not_github", "origin 不是 GitHub 仓库")
        except (OSError, subprocess.TimeoutExpired):
            add("not_git_repo", "本机 git 检查不可用")
    if not _executable(constants.gh_bin()):
        add("gh_missing", "未找到 gh")
    if not _planner_ready(conn):
        add("planner_unconfigured", "规划后端未配置或本机不可用")
    review = review_config.load(conn) or {}
    if review.get("backend", "pi") not in constants.AUTOMATION_REVIEW_BACKENDS:
        add("review_backend_unsupported", "Review 后端需为 Plus/Claude/Codex")
    execution = exec_config.load(conn)
    if execution.get("backend") != "pi_desktop":
        add("exec_backend_not_plus", "执行端需为 Plus")
    if (execution.get("pi_desktop") or {}).get("permission_mode") != "auto":
        add("exec_permission_not_auto", "Plus 执行权限需为 auto")
    return errors


def cmd_set_project_automation(ctx, payload):
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise util.bad_request("project_id 必须为非空字符串")
    project_id = project_id.strip()
    value = validate_automation(payload.get("automation"))
    expected = payload.get("expected_policy_revision")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
        raise util.bad_request("expected_policy_revision 须为非负整数")
    with db.write_txn(ctx.conn):
        revision, workspace, items, _ = project_config.load_raw_mappings(ctx.conn)
        if project_id not in items:
            raise util.KanpilotError("not_found", "项目映射不存在")
        current = policy_for(ctx.conn, project_id) or constants.AUTOMATION_DEFAULTS
        if expected is not None and expected != current["policy_revision"]:
            raise util.KanpilotError("conflict", "automation policy_revision 不符")
        value.update(policy_revision=current["policy_revision"] + 1, updated_at=ctx.now_iso)
        items[project_id] = dict(items[project_id], automation=value)
        db.set_setting(ctx.conn, project_config.SETTINGS_KEY,
                       {"version": project_config.CURRENT_VERSION, "revision": revision,
                        "workspace_id": workspace, "items": items})
    return {"project_id": project_id, "automation": value, "policy_revision": value["policy_revision"]}


def cmd_set_automation_global(ctx, payload):
    value = payload.get("max_active_tasks")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise util.bad_request("max_active_tasks 须为 1..5 的整数")
    with db.write_txn(ctx.conn):
        db.set_setting(ctx.conn, "automation_global", {"max_active_tasks": value, "updated_at": ctx.now_iso})
    return {"max_active_tasks": value}


def cmd_record_cost(ctx, payload):
    cost = payload.get("cost_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                             or not math.isfinite(cost) or cost < 0):
        raise util.bad_request("cost_usd 须为 null 或非负有限数值")
    for key in ("task_id", "request_id", "run_id", "stage", "source"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise util.bad_request(f"{key} 必须为非空字符串")
    with db.write_txn(ctx.conn):
        task = db.require_task(ctx.conn, payload["task_id"])
        request = db.get_request(ctx.conn, payload["request_id"])
        if request is None:
            raise util.KanpilotError("not_found", "成本请求不存在")
        if request["task_id"] != task["task_id"] or request["stage"] != payload["stage"]:
            raise util.KanpilotError("conflict", "成本请求归属或阶段不符")
        if not request["run_id"] or request["run_id"] != payload["run_id"]:
            raise util.KanpilotError("stale_run", "成本请求运行身份不符")
        if request["status"] == "claimed" and util.lease_lost(request["lease_expires_at"], ctx.now):
            raise util.KanpilotError("stale_run", "成本请求租约已过期")
        if request["status"] not in ("claimed", "succeeded", "failed"):
            raise util.KanpilotError("stale_run", "成本请求尚未运行或已取消")
        if not core.is_automation_managed(task):
            return {"recorded": False, "cost_usd": core.automation_of(task).get("cost_usd", 0),
                    "cost_unknown_steps": core.automation_of(task).get("cost_unknown_steps", 0)}
        # Events retain dedup evidence even after the bounded cost_log rolls over.
        previous = ctx.conn.execute("SELECT detail_json FROM events WHERE kind='cost_recorded' AND task_id=? AND request_id=?",
                                    (task["task_id"], request["request_id"])).fetchall()
        a = core.automation_of(task)
        recorded = not any((util.jload(row["detail_json"]) or {}).get("run_id") == payload["run_id"] for row in previous)
        if recorded:
            entry = {key: payload[key] for key in ("request_id", "run_id", "stage", "cost_usd", "source")}
            entry["at"] = ctx.now_iso
            a = core.update_automation(ctx.conn, task, ctx.now_iso,
                 cost_usd=float(a.get("cost_usd", 0)) + (cost or 0),
                 cost_unknown_steps=int(a.get("cost_unknown_steps", 0)) + int(cost is None),
                 cost_log=(a.get("cost_log", []) + [entry])[-50:])
            db.add_event(ctx.conn, ctx.now_iso, "cost_recorded", task_id=task["task_id"],
                         request_id=request["request_id"], actor=payload["source"], detail=entry)
        return {"recorded": recorded, "cost_usd": a.get("cost_usd", 0),
                "cost_unknown_steps": a.get("cost_unknown_steps", 0)}
