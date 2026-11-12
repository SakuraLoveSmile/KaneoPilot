"""Deterministic project automation intake (protocol §5.27.6)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from . import automation_policy, cli, constants, core, db, planner_config, planner_inputs, project_config, remote, util

_TRANSIENT = {"remote_unavailable", "git_transient"}
_PLANNER_RETRY_FAILURES = ('planner_failed', 'planner_timeout', 'planner_output_invalid')


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


def _adopt(conn, task, now, policy, *, wanted, delivery=None, adopted=False):
    a = core.automation_of(task)
    if not a:
        a = {"policy_revision": policy["policy_revision"], "adopted_at": now, "fix_round": 0,
             "segment_started_at": now, "segment_cost_base_usd": 0.0, "plan_attempts": 0,
             "manual_takeover": False, "cost_usd": 0.0, "cost_unknown_steps": 0, "cost_log": []}
        db.add_event(conn, now, "automation_adopted", task_id=task["task_id"], actor="automation",
                     detail={"policy_revision": policy["policy_revision"]})
    core.update_automation(conn, task, now, **dict(a, plan_wanted=wanted))
    if delivery and delivery.get("branch"):
        core.update_delivery(conn, task, now, **delivery)
        if adopted:
            db.add_event(conn, now, "branch_worktree_adopted", task_id=task["task_id"], actor="automation",
                         detail={key: delivery.get(key) for key in ("branch", "worktree_path", "base_branch", "base_sha", "head_sha")})


def _failure(conn, task_id, now, policy, code, message, *, input_id=None, delivery=None, candidate=None, adopted=False,
             guard_active=False, expected_retry=None):
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
        if expected_retry is not None and expected_retry != planner_inputs._retry_snapshot(conn, task_id):
            return  # A completed new owner or explicit replan also invalidates this retry.
        if guard_active and conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                                          (task_id,)).fetchone():
            return  # A new owner appeared while retry evidence was being read.
        task = db.get_task(conn, task_id)
        if task is None and manifest:
            task = planner_inputs._import_task(conn, now, manifest, actor="automation", source_path=source_path)
        if task is None or not db.task_is_active(task) or task["auto_advance_paused"] or core.automation_of(task).get("manual_takeover"):
            return
        _adopt(conn, task, now, policy, wanted=code in _TRANSIENT, delivery=delivery, adopted=adopted)
        core.update_automation(conn, task, now, last_skip={"reason": code, "at": now})
        if code not in _TRANSIENT:
            core.block_task(conn, db.require_task(conn, task_id), now, message, code, "automation", stage="plan")


def _retry_file(root, name):
    content, reason = planner_inputs._read_worktree_material(str(root), name, constants.PLAN_INPUT_FILE_MAX_BYTES)
    util.require(content is not None, '原规划冻结材料缺失或已变化：' + name + '（' + str(reason) + '）',
                 'validation_failed')
    return content


def _check_native_retry_draft(conn, task, request, payload):
    """Only the native Codex adapter owns this job/exit/last-message format."""
    manifest = payload["manifest"]
    input_id, digest = payload["input_id"], payload["planner_input_sha256"]
    from . import headless_adapter, stage_contract
    directory = headless_adapter.resolve_source(conn).run_dir(task['task_id'], request['run_id'])
    job = json.loads(_retry_file(directory, 'job.json'))
    util.require(isinstance(job, dict) and all(job.get(key) == value for key, value in {
        'task_id': task['task_id'], 'request_id': request['request_id'], 'run_id': request['run_id'],
        'backend': payload.get('planner_backend', 'codex'), 'status': 'failed'}.items()),
        '原规划运行检查点身份已变化', 'conflict')
    if type(job.get('exit_code')) is int and job['exit_code'] == 0:
        raw = _retry_file(directory, 'events.jsonl')
        util.require(raw.endswith(b'\n'), '原规划终态日志不完整', 'conflict')
        events = [json.loads(line) for line in raw.splitlines()]
        util.require(events and all(isinstance(event, dict) and event.get('seq') == index
                     for index, event in enumerate(events, 1)), '原规划终态日志不可核验', 'conflict')
        terminals = [event for event in events if event.get('type') == 'terminal']
        util.require(len(terminals) == 1, '原规划缺少唯一终态', 'conflict')
        terminal = terminals[0]
        if terminal.get('reason') == 'publish_failed':
            contract = stage_contract.validate_contract(job.get('stage_contract'), expected={
                'stage': 'plan', 'task_id': task['task_id'], 'request_id': request['request_id'],
                'run_id': request['run_id'], 'harness_id': job['backend']})
            result = stage_contract.validate_result(job.get('stage_result'), contract)
            text = _retry_file(directory, 'last-message.txt').decode('utf-8').strip()
            try:
                output = json.loads(text)
            except ValueError:
                # Match planner_runner.parse_output's accepted fenced/prose JSON,
                # using the same safely opened bytes instead of reopening a path.
                start, end = text.find('{'), text.rfind('}')
                util.require(start >= 0 and end > start, '原规划完成稿需要人工核对', 'conflict')
                output = json.loads(text[start:end + 1])
            output = stage_contract.validate_plan_result(output)
            util.require(result['outcome'] == 'succeeded' and output['status'] == 'ready'
                         and result['payload'] == output
                         and contract['inputs'].get('manifest') == manifest
                         and contract['inputs'].get('input_id') == input_id
                         and contract['inputs'].get('planner_input_sha256') == digest,
                         '原规划发布失败，完成稿证据需要人工核对', 'conflict')
            # Keep the failed request/job and completed draft intact. A new
            # model invocation cannot repair a publication authorization gate.
            raise util.KanpilotError('planner_publish_failed',
                '规划稿已完成（exit 0），但计划发布被拒；已停止自动重跑，请核对原稿后正常重新规划。'
                + str(job.get('detail') or task['failure_reason'] or '')[:800])


def _retry_input(conn, task, expected):
    """Reuse the failed attempt's entire frozen input, never prepare a thinner one."""
    if task['failure_code'] not in _PLANNER_RETRY_FAILURES:
        return None  # Initial intake, an explicit replan, or a new Review fix.
    request = conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='plan' "
                           "ORDER BY created_at DESC,rowid DESC LIMIT 1", (task['task_id'],)).fetchone()
    util.require(request is not None and request['status'] == 'failed' and request['run_id']
                 and request['finished_at'], '原失败规划请求身份不完整，请人工重新准备材料', 'conflict')
    payload = db.request_payload(request)
    input_id, digest = payload.get('input_id'), payload.get('planner_input_sha256')
    row = db.get_planner_input(conn, input_id) if input_id else None
    manifest = payload.get('manifest') or {}
    util.require(isinstance(manifest, dict) and isinstance(manifest.get('task'), dict),
                 '原规划冻结输入结构已变化', 'conflict')
    meta = manifest.get('task') or {}
    util.require(row is not None and row['task_id'] == task['task_id']
                 and payload.get('manual_authorized') is True and digest == row['input_sha256']
                 and manifest == db.planner_input_manifest(row) and planner_inputs._manifest_sha(manifest) == digest
                 and manifest.get('input_id') == input_id and meta.get('task_id') == task['task_id']
                 and meta.get('project_id') == task['project_id'] and meta.get('repository') == task['repository']
                 and manifest.get('requirements_sha256') == expected == task['requirements_sha256']
                 and manifest.get('source_sha256') == task['source_sha256']
                 and row['requirements_sha256'] == expected and row['source_sha256'] == task['source_sha256']
                 and not manifest.get('issues'), '原规划冻结输入或任务身份已变化，请人工重新准备材料', 'conflict')
    root = planner_inputs._inputs_root(conn) / input_id
    try:
        util.require(json.loads(_retry_file(root, 'manifest.json')) == manifest,
                     '原规划材料 manifest 与冻结登记不符', 'conflict')
        for entry in manifest.get('files') or []:
            raw = _retry_file(root, entry['path'])
            util.require(len(raw) == entry['size'] and util.sha256_bytes(raw) == entry['sha256'],
                         '原规划材料内容已变化，请人工重新准备材料', 'conflict')
        if payload.get('planner_backend', 'codex') == 'codex':
            _check_native_retry_draft(conn, task, request, payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise util.KanpilotError('validation_failed', '原规划冻结材料或运行证据不可核验，请人工重新准备材料') from exc
    try:
        live = remote.get_task(remote.get_client(), task['task_id'])
    except Exception as exc:
        raise util.KanpilotError('remote_unavailable', '重试前无法核验远端规划需求') from exc
    live_task = live.get('task') or {}
    util.require(live_task.get('id') == task['task_id'] and live_task.get('projectId') == task['project_id']
                 and live.get('requirements_sha256') == expected
                 and live_task.get('status') in core.expected_remote_slugs(task),
                 '远端任务、项目、需求或规划授权状态已变化，请人工重新准备材料', 'remote_conflict')
    return dict(input_id=input_id, input_sha256=digest, can_start=True,
                planner_backend=payload.get('planner_backend', 'codex'),
                planner_method=payload.get('planner_method', 'builtin'))


def _submit(conn, db_path, task_id, policy, now, expected, *, wanted=False, candidate=None):
    from . import git_delivery, task_workspace
    task = db.get_task(conn, task_id)
    if wanted and not (task is not None and task['status'] == 'needs_plan' and db.task_is_active(task)
                       and core.is_automation_managed(task) and core.automation_of(task).get('plan_wanted')
                       and not task['auto_advance_paused'] and not core.automation_of(task).get('manual_takeover')):
        return {'task_id': task_id, 'reason': 'planning_state_changed'}
    retrying = wanted and task is not None and task['failure_code'] in _PLANNER_RETRY_FAILURES
    expected_retry = planner_inputs._retry_snapshot(conn, task_id) if retrying else None
    if expected_retry is not None and expected_retry['task'] != dict(task):
        return {'task_id': task_id, 'reason': 'planning_state_changed'}
    if wanted and conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                               (task_id,)).fetchone():
        return {'task_id': task_id, 'reason': 'active_request'}
    if task and not core.automation_budget(task, policy, now)["ok"]:
        _failure(conn, task_id, now, policy, "auto_budget_exceeded", "无人值守段成本或时长已达到上限",
                 guard_active=wanted, expected_retry=expected_retry)
        return {"task_id": task_id, "reason": "auto_budget_exceeded"}
    prepared = None
    adopted = False
    prepared_delivery = None
    delivery = core.delivery_of(task) if task else {}
    try:
        if task is not None and task['plan_kind'] == 'review_fix' and not delivery.get('branch'):
            # Independent children are first adopted through the normal To Do
            # intake, before they have automation/plan_wanted metadata. Wait for
            # their exact Review registration before preparing any materials.
            task_workspace.review_fix_head(conn, task, allow_pending=True)
        if wanted:
            prepared = _retry_input(conn, task, expected)
        if wanted and not delivery.get("branch"):
            initial = task_workspace.review_fix_head(conn, task, allow_pending=True) if task["plan_kind"] == "review_fix" else None
            delivery = git_delivery.prepare_branch_worktree(task["repository"], task_id,
                       base_branch=policy.get("delivery_base"),
                       initial_head_sha=initial,
                       worktrees_root=constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None))
            adopted = bool(delivery.pop("adopted", False))
            if delivery.get("branch"):
                delivery["mode"] = "pr"
                prepared_delivery = delivery
        if prepared is None:
            prepared = cli.run("prepare-plan-input", {"task_id": task_id,
                              "expected_requirements_sha256": expected, "local_paths": [],
                              # A14: unattended preparation is not a user action.
                              "actor": "automation"}, db_path)
        if not prepared.get("can_start"):
            raise util.KanpilotError("validation_failed", "规划材料不完整，请人工补齐或排除附件")
        task = db.get_task(conn, task_id)
        if not delivery.get("branch"):
            row = db.get_planner_input(conn, prepared["input_id"])
            meta = db.planner_input_manifest(row)["task"]
            initial = (task_workspace.review_fix_head(conn, task, allow_pending=True)
                       if task is not None and task["plan_kind"] == "review_fix" else None)
            delivery = git_delivery.prepare_branch_worktree(meta["repository"], task_id,
                       base_branch=policy.get("delivery_base"),
                       initial_head_sha=initial,
                       worktrees_root=constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None))
            adopted = bool(delivery.pop("adopted", False))
            if delivery.get("branch"):
                delivery["mode"] = "pr"
                prepared_delivery = delivery
        config = planner_config.merged(conn)
        item = {"input_id": prepared["input_id"], "input_sha256": prepared["input_sha256"],
                "planner_backend": prepared.get('planner_backend', config["backend"]),
                "planner_method": prepared.get('planner_method', config["default_method"]),
                "automation": {"authorized_by": "automation", "policy_revision": policy["policy_revision"], "delivery": delivery, "adopted": adopted}}
        if retrying:
            # Same normal submission gates, with a local identity guard in its
            # transaction. This parameter is deliberately not a CLI field.
            response = planner_inputs.start_plans(SimpleNamespace(conn=conn, now_iso=now),
                {"items": [item]}, expected_retry=expected_retry)["results"][0]
        else:
            response = cli.run("start-plans", {"items": [item]}, db_path)["results"][0]
        if not response.get("request_id"):
            raise util.KanpilotError(response.get("code") or "validation_failed", response.get("error") or "规划提交失败")
        return {"task_id": task_id, "request_id": response["request_id"]}
    except (util.KanpilotError, git_delivery.GitDeliveryError) as exc:
        code, message = exc.code, exc.message
        if code == "review_fix_waiting":
            return {"task_id": task_id, "reason": code}
        if wanted and code == "conflict" and "远端需求" in message:
            code, message = "remote_conflict", "远端需求已变化（新增内容未经筛选）"
        _failure(conn, task_id, now, policy, code, message,
                 input_id=prepared.get("input_id") if prepared else None, delivery=prepared_delivery,
                 candidate=candidate, adopted=adopted, guard_active=wanted, expected_retry=expected_retry)
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
