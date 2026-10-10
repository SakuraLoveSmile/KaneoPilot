"""Deterministic project automation intake (protocol §5.27.6)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from . import automation_policy, cli, constants, core, db, planner_config, planner_inputs, project_config, remote, util

_TRANSIENT = {"remote_unavailable", "git_transient"}


def _readonly(db_path):
    # Dry preview must not create a database, schema, settings, directories or WAL.
    conn = sqlite3.connect(Path(db_path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _candidate(conn, task_id):
    task = db.get_task(conn, task_id)
    if task is None:
        return True
    return (db.task_is_active(task) and not task["auto_advance_paused"]
            and task["status"] == "needs_plan" and not core.automation_of(task)
            and conn.execute("SELECT 1 FROM requests WHERE task_id=? AND kind='plan' LIMIT 1", (task_id,)).fetchone() is None)


def _occupied(conn):
    count = 0
    for task in conn.execute("SELECT * FROM tasks WHERE save_state='active'"):
        if not core.is_automation_managed(task) or task["status"] in ("done", "needs_user"):
            continue
        if core.is_pr_delivery(task) and db.task_extra(task).get("review_conclusion") == "pass":
            continue
        count += 1
    return count


def _sort_key(item):
    created = util.parse_ts(item.get("created_at"))
    number = item.get("number")
    return (0, created.timestamp(), str(item["task_id"])) if created else (1, str(number or item["task_id"]).zfill(20), str(item["task_id"]))


def _adopt(conn, task, now, policy, *, wanted, delivery=None):
    a = core.automation_of(task)
    if not a:
        a = {"policy_revision": policy["policy_revision"], "adopted_at": now, "fix_round": 0,
             "segment_started_at": now, "segment_cost_base_usd": 0.0, "plan_attempts": 0,
             "manual_takeover": False, "cost_usd": 0.0, "cost_unknown_steps": 0, "cost_log": []}
        db.add_event(conn, now, "automation_adopted", task_id=task["task_id"], actor="automation",
                     detail={"policy_revision": policy["policy_revision"]})
    core.update_automation(conn, task, now, **dict(a, plan_wanted=wanted))
    if delivery:
        core.update_delivery(conn, task, now, **dict(delivery, mode="pr"))
    elif not core.delivery_of(task):
        core.update_delivery(conn, task, now, mode="pr")


def _failure(conn, task_id, now, policy, code, message, *, input_id=None, delivery=None, candidate=None):
    manifest, source_path = None, None
    if input_id:
        row = db.get_planner_input(conn, input_id)
        if row:
            manifest = db.planner_input_manifest(row)
            source = planner_inputs._inputs_root(conn) / input_id / "source.json"
            source_path = str(source) if source.is_file() else None
    elif candidate and code not in _TRANSIENT:
        mapping = project_config.get_effective_mapping(conn, candidate["project_id"]) or {}
        manifest = {"task": {"task_id": task_id, "project_id": candidate["project_id"],
                             "project_name": candidate.get("project_name") or mapping.get("project_name") or "",
                             "repository": candidate.get("repository") or mapping.get("repository"),
                             "title": candidate.get("title"), "kaneo_status": candidate.get("kaneo_status"),
                             "plan_kind": "standard"},
                    "requirements_sha256": candidate.get("requirements_sha256"),
                    "source_sha256": candidate.get("source_sha256")}
    with db.write_txn(conn):
        task = db.get_task(conn, task_id)
        if task is None and manifest:
            task = planner_inputs._import_task(conn, now, manifest, actor="automation", source_path=source_path)
        if task is None or not db.task_is_active(task) or task["auto_advance_paused"] or core.automation_of(task).get("manual_takeover"):
            return
        _adopt(conn, task, now, policy, wanted=code in _TRANSIENT, delivery=delivery)
        core.update_automation(conn, task, now, last_skip={"reason": code, "at": now})
        if code not in _TRANSIENT:
            core.block_task(conn, db.require_task(conn, task_id), now, message, code, "automation", stage="plan")


def _submit(conn, db_path, task_id, policy, now, expected, *, wanted=False, candidate=None):
    from . import git_delivery
    task = db.get_task(conn, task_id)
    if task and not core.automation_budget(task, policy, now)["ok"]:
        _failure(conn, task_id, now, policy, "auto_budget_exceeded", "无人值守段成本或时长已达到上限")
        return {"task_id": task_id, "reason": "auto_budget_exceeded"}
    prepared = None
    delivery = core.delivery_of(task) if task else {}
    try:
        if wanted and not delivery.get("branch"):
            delivery = git_delivery.prepare_branch_worktree(task["repository"], task_id,
                       base_branch=policy.get("delivery_base"),
                       worktrees_root=constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None))
        prepared = cli.run("prepare-plan-input", {"task_id": task_id,
                          "expected_requirements_sha256": expected, "local_paths": [],
                          # A14: unattended preparation must not be recorded as a
                          # user action.
                          "actor": "automation"}, db_path)
        if not prepared.get("can_start"):
            raise util.KanpilotError("validation_failed", "规划材料不完整，请人工补齐或排除附件")
        task = db.get_task(conn, task_id)
        if not delivery.get("branch"):
            row = db.get_planner_input(conn, prepared["input_id"])
            meta = db.planner_input_manifest(row)["task"]
            delivery = git_delivery.prepare_branch_worktree(meta["repository"], task_id,
                       base_branch=policy.get("delivery_base"),
                       worktrees_root=constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None))
        delivery = dict(delivery, mode="pr")
        config = planner_config.merged(conn)
        item = {"input_id": prepared["input_id"], "input_sha256": prepared["input_sha256"],
                "planner_backend": config["backend"], "planner_method": config["default_method"],
                "automation": {"authorized_by": "automation", "policy_revision": policy["policy_revision"], "delivery": delivery}}
        response = cli.run("start-plans", {"items": [item]}, db_path)["results"][0]
        if not response.get("request_id"):
            raise util.KanpilotError(response.get("code") or "validation_failed", response.get("error") or "规划提交失败")
        return {"task_id": task_id, "request_id": response["request_id"]}
    except (util.KanpilotError, git_delivery.GitDeliveryError) as exc:
        code, message = exc.code, exc.message
        if wanted and code == "conflict" and "远端需求" in message:
            code, message = "remote_conflict", "远端需求已变化（新增内容未经筛选）"
        _failure(conn, task_id, now, policy, code, message,
                 input_id=prepared.get("input_id") if prepared else None, delivery=delivery, candidate=candidate)
        return {"task_id": task_id, "reason": code}


def run_once(db_path, *, dry_run=False, now=None, project_ids=None):
    db_path = Path(db_path)
    stamp = util.to_iso(now) if now is not None and not isinstance(now, str) else now or util.now_iso()
    current = util.parse_ts(stamp)
    summary = {"started_at": stamp, "finished_at": None, "projects": [], "errors": []}
    conn = _readonly(db_path) if dry_run else db.connect(db_path)
    acquired = False
    try:
        if dry_run:
            runs = db.get_setting(conn, "executor_runs") or {}
            if util.pid_alive((runs.get("intake") or {}).get("pid")):
                summary["errors"].append({"code": "already_running"})
                return summary
        else:
            with db.write_txn(conn):
                runs = db.get_setting(conn, "executor_runs") or {}
                if util.pid_alive((runs.get("intake") or {}).get("pid")):
                    summary["errors"].append({"code": "already_running"})
                    return summary
                runs["intake"] = {"pid": os.getpid(), "started_at": stamp, "exit_code": None}
                db.set_setting(conn, "executor_runs", runs)
                acquired = True
        if db.paused(conn, "plan"):
            summary["errors"].append({"code": "paused"})
            return summary
        _, _, mappings, _ = project_config.load_raw_mappings(conn)
        ready = {}
        for project_id, mapping in mappings.items():
            # A12: the engine may restrict intake to a subset of (live) projects.
            if project_ids is not None and project_id not in project_ids:
                continue
            policy = automation_policy.policy_for(conn, project_id)
            if not policy or not policy.get("enabled"):
                continue
            issues = automation_policy.automation_preflight(conn, project_id)
            if not mapping.get("enabled", True):
                issues.append({"code": "repository_missing", "message": "项目已停用新规划"})
            project = {"project_id": project_id, "ready": not issues, "preflight": issues, "adopted": [], "skipped": []}
            if dry_run:
                project["candidates"] = []
            summary["projects"].append(project)
            if not issues:
                ready[project_id] = (policy, project)
        if not ready:
            return summary
        if not dry_run:
            wanted_tasks = conn.execute("SELECT * FROM tasks WHERE status='needs_plan' AND save_state='active' ORDER BY created_at, task_id").fetchall()
            for task in wanted_tasks:
                if (task["project_id"] not in ready or not core.is_automation_managed(task)
                        or not core.automation_of(task).get("plan_wanted") or task["auto_advance_paused"]
                        or db.active_request_for_key(conn, "plan:" + task["task_id"])):
                    continue
                policy, project = ready[task["project_id"]]
                result = _submit(conn, db_path, task["task_id"], policy, stamp, task["requirements_sha256"], wanted=True)
                project["skipped" if result.get("reason") else "adopted"].append(result if result.get("reason") else task["task_id"])
        capacity = max(0, automation_policy.global_settings(conn)["max_active_tasks"] - _occupied(conn))
        if not capacity:
            return summary
        fetched = planner_inputs.fetch_todos(conn) if dry_run else cli.run("fetch-todos", {}, db_path)
        state = db.get_setting(conn, "automation_intake_state") or {}
        cache = dict(state.get("label_checked") or {})
        cache = {k: v for k, v in cache.items() if isinstance(v, dict) and util.parse_ts(v.get("at"))
                 and current - util.parse_ts(v["at"]) < timedelta(minutes=30)}
        queries, attempts = 0, 0
        client = None
        for item in sorted(fetched.get("items", []), key=_sort_key):
            if attempts >= min(3, capacity):
                break
            task_id = item["task_id"]
            if item.get("project_id") not in ready or not _candidate(conn, task_id):
                continue
            policy, project = ready[item["project_id"]]
            if not item.get("selectable", True) or not item.get("requirements_sha256"):
                project["skipped"].append({"task_id": task_id, "reason": "validation_failed"})
                continue
            filters = policy["label_filter"]
            if filters:
                labels = item.get("labels")
                if not isinstance(labels, list):
                    if task_id in cache and cache[task_id].get("filter") == filters:
                        project["skipped"].append({"task_id": task_id, "reason": "label_filter"})
                        continue
                    if queries >= 20:
                        project["skipped"].append({"task_id": task_id, "reason": "label_query_limit"})
                        continue
                    queries += 1
                    try:
                        client = client or remote.get_client()
                        labels = remote.get_task_labels(client, task_id)
                    except Exception:
                        project["skipped"].append({"task_id": task_id, "reason": "remote_unavailable"})
                        continue
                names = {str(label.get("name", "") if isinstance(label, dict) else label).strip().lower() for label in labels}
                if not names.intersection(filters):
                    cache[task_id] = {"at": stamp, "filter": filters}
                    project["skipped"].append({"task_id": task_id, "reason": "label_filter"})
                    continue
                cache.pop(task_id, None)
            attempts += 1
            if dry_run:
                project["candidates"].append(task_id)
            else:
                result = _submit(conn, db_path, task_id, policy, stamp, item["requirements_sha256"], candidate=item)
                project["skipped" if result.get("reason") else "adopted"].append(result if result.get("reason") else task_id)
        if not dry_run:
            with db.write_txn(conn):
                db.set_setting(conn, "automation_intake_state", dict(state, label_checked=cache))
        return summary
    except Exception as exc:
        summary["errors"].append({"code": getattr(exc, "code", "internal"), "message": str(exc)})
        return summary
    finally:
        summary["finished_at"] = stamp if now is not None else util.now_iso()
        if acquired:
            with db.write_txn(conn):
                db.set_setting(conn, "automation_last_intake", summary)
                runs = db.get_setting(conn, "executor_runs") or {}
                entry = dict(runs.get("intake") or {})
                if entry.get("pid") == os.getpid():
                    entry.update(pid=None, finished_at=summary["finished_at"], exit_code=0 if not summary["errors"] else 1)
                    runs["intake"] = entry
                    db.set_setting(conn, "executor_runs", runs)
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kanpilot.auto_intake")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    try:
        result = run_once(cli.resolve_db_path(args.db), dry_run=args.dry_run)
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"errors": [{"code": "internal", "message": str(exc)}]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(bool(result.get("errors")))


if __name__ == "__main__":
    raise SystemExit(main())
