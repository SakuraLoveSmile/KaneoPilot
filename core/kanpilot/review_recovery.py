"""Explicit recovery of a Review block accidentally dismissed into dispatch.

Listing uses only SQLite audit facts. The command checks local files and reads
Kaneo before acquiring the write transaction, then rechecks the same evidence.
"""

from pathlib import Path
import json

from . import constants, core, db, util


def _conflict(message):
    raise util.KanpilotError("conflict", message)


def _latest_request(conn, task_id, kinds):
    marks = ",".join("?" for _ in kinds)
    return conn.execute(
        f"SELECT * FROM requests WHERE task_id=? AND kind IN ({marks}) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id, *kinds),
    ).fetchone()


def candidate(conn, task):
    """Cheap, read-only eligibility; file and remote evidence remain mandatory."""
    if (not db.task_is_active(task) or task["status"] != "ready"
            or task["sent"] != 1 or task["send_state"] != "sent"
            or not task["run_id"] or not task["plan_sha256"]):
        return None
    extra = db.task_extra(task)
    if (any(task[k] for k in ("review_report_path", "review_report_sha256", "review_key"))
            or extra.get("review_conclusion") or extra.get("review_registered_at")
            or any(h.get("execution_run_id") == task["run_id"]
                   for h in extra.get("review_history", []))):
        return None
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                    (task["task_id"],)).fetchone():
        return None
    request = _latest_request(conn, task["task_id"], ("review",))
    dispatch = _latest_request(conn, task["task_id"], ("dispatch", "plan"))
    if (request is None or request["status"] != "failed" or not request["run_id"]
            or not request["finished_at"] or dispatch is None
            or dispatch["kind"] != "dispatch" or dispatch["status"] != "succeeded"
            or dispatch["run_id"] != task["run_id"]
            or not dispatch["finished_at"] or dispatch["finished_at"] > request["created_at"]):
        return None
    events = conn.execute(
        "SELECT * FROM events WHERE task_id=? AND kind IN "
        "('resolved','status_change','reported','review_registered') ORDER BY id DESC",
        (task["task_id"],),
    ).fetchall()
    resolved = next((e for e in events if e["kind"] == "resolved"), None)
    changes = [e for e in events if e["kind"] == "status_change"]
    if resolved is None or len(changes) < 2:
        return None
    resolution = util.jload(task["resolution"]) or {}
    detail = util.jload(resolved["detail_json"]) or {}
    dismissed, blocked = changes[:2]
    dd = util.jload(dismissed["detail_json"]) or {}
    bd = util.jload(blocked["detail_json"]) or {}
    if (resolution.get("action") != "dismiss" or detail.get("action") != "dismiss"
            or resolution.get("at") != resolved["at"]
            or dismissed["at"] != resolved["at"]
            or dd.get("via") != "resolve dismiss" or dd.get("from") != "needs_user"
            or dd.get("to") != "ready" or bd.get("from") != "in_review"
            or bd.get("to") != "needs_user" or bd.get("code") != "review_blocked"
            or blocked["at"] != request["finished_at"]):
        return None
    reported = next((e for e in events if e["kind"] == "reported"
                     and e["request_id"] == request["request_id"]
                     and e["at"] == blocked["at"]
                     and blocked["id"] < e["id"] < dismissed["id"]), None)
    if reported is None or (util.jload(reported["detail_json"]) or {}).get("outcome") not in ("needs_user", "failed"):
        return None
    if any(e["kind"] == "review_registered" and e["id"] > blocked["id"] for e in events):
        return None
    return {"execution_run_id": task["run_id"], "failed_review_request_id": request["request_id"]}


def _snapshot(conn, task_id, run_id):
    from . import round_store
    row = round_store.get_round(conn, task_id, run_id)
    return {
        "task": dict(db.require_task(conn, task_id)),
        "round": dict(row) if row is not None else None,
        "requests": [dict(r) for r in conn.execute("SELECT * FROM requests WHERE task_id=? ORDER BY rowid", (task_id,))],
        "events": [dict(r) for r in conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))],
    }


def _read_files(conn, task, request):
    from . import cmd_session, dsh_events, headless_adapter, planfile, round_store
    files = {}
    def read(path):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise util.KanpilotError("conflict", "本机评审恢复证据不可读") from exc
        files[str(path)] = util.sha256_bytes(raw)
        return raw

    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    job, directory = cmd_session._identity_job(headless_adapter.resolve_source(conn), task["task_id"], task["run_id"])
    if row is None or job is None:
        _conflict("当前执行轮次缺少可信本机记录")
    try:
        job = json.loads(read(directory / "job.json"))
    except ValueError as exc:
        raise util.KanpilotError("conflict", "执行记录解析失败") from exc
    if (not isinstance(job, dict) or job.get("task_id") != task["task_id"]
            or job.get("run_id") != task["run_id"] or job.get("backend") != row["backend"]):
        _conflict("本机执行记录身份已变化")
    read(directory / "events.jsonl")
    events, error = dsh_events.iter_events(directory / "events.jsonl")
    if util.sha256_file(directory / "events.jsonl") != files[str(directory / "events.jsonl")]:
        _conflict("执行终态记录在读取期间已变化")
    detail = dsh_events.round_detail(row, job, events) or {}
    if (error or job.get("status") != "finished"
            or (detail.get("terminal") or {}).get("outcome") != "completed"
            or job.get("plan_sha256") != task["plan_sha256"]):
        _conflict("当前执行尚无 completed/finished 终态或计划身份不符")
    try:
        meta, raw, path = planfile.load_plan(task["plan_path"], task["task_id"])
    except (util.KanpilotError, OSError, ValueError) as exc:
        raise util.KanpilotError("conflict", "当前计划不可核验") from exc
    if (util.sha256_bytes(raw) != task["plan_sha256"]
            or any(meta.get(k) != task[k] for k in ("project_id", "repository", "requirements_sha256"))):
        _conflict("当前计划文件或需求身份已变化")
    files[str(path)] = util.sha256_bytes(raw)
    # Controllers use invocation UUIDs, not request run IDs as directory names.
    root = constants.app_support() / "executor-sessions" / "review"
    matched = []
    for path in root.glob("*/job.json"):
        try:
            safe = util.inside(path, root, "review job")
            raw = safe.read_bytes()
            review = json.loads(raw)
        except (OSError, ValueError, util.KanpilotError):
            continue
        if not isinstance(review, dict) or review.get("request_id") != request["request_id"]:
            continue
        if (review.get("task_id") != task["task_id"] or review.get("run_id") != request["run_id"]
                or review.get("execution_run_id") != task["run_id"]
                or review.get("status") not in ("failed", "finished")
                or review.get("conclusion") or review.get("report_path")
                or review.get("report_sha256") or review.get("review_key")):
            _conflict("失败评审的本机记录与当前执行不符或已有结论")
        matched.append((safe, raw))
    if len(matched) != 1:
        _conflict("失败评审缺少唯一的执行轮次绑定记录")
    path, raw = matched[0]
    files[str(path)] = util.sha256_bytes(raw)
    return files


def _deduplicated(conn, task, identity):
    for row in conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='review_stage_recovered' ORDER BY id DESC", (task["task_id"],)):
        detail = util.jload(row["detail_json"]) or {}
        if all(detail.get(k) == v for k, v in identity.items()):
            if task["run_id"] != identity["execution_run_id"] or task["plan_sha256"] != detail.get("plan_sha256"):
                _conflict("当前执行或计划已变化，不能恢复旧评审周期")
            return {"ok": True, "deduplicated": True, "task_status": task["status"], "request": None}
    return None


def recover(ctx, payload):
    identity = {"execution_run_id": payload.get("expected_execution_run_id"),
                "failed_review_request_id": payload.get("expected_failed_review_request_id")}
    if not all(isinstance(v, str) and v.strip() for v in identity.values()):
        raise util.bad_request("recover_review 必须指定期望的执行轮次和失败评审请求")
    conn, task_id = ctx.conn, payload["task_id"]
    task = db.require_task(conn, task_id)
    with db.write_txn(conn):
        task = db.require_task(conn, task_id)
        if not db.task_is_active(task):
            _conflict("仅活跃任务可以恢复评审阶段")
        repeated = _deduplicated(conn, task, identity)
        if repeated:
            return repeated
        if candidate(conn, task) != identity:
            _conflict("任务不符合评审误退恢复条件，或期望身份已变化")
        snapshot = _snapshot(conn, task_id, task["run_id"])
        request = db.get_request(conn, identity["failed_review_request_id"])
    files = _read_files(conn, task, request)
    from . import remote
    try:
        live = remote.get_task(remote.get_client(), task_id)
    except Exception as exc:
        raise util.KanpilotError("remote_unavailable", "远端任务读取失败，未恢复评审阶段") from exc
    remote_task = live.get("task") or {}
    if (remote_task.get("id") != task_id or remote_task.get("projectId") != task["project_id"]
            or remote_task.get("status") != "in-review"
            or not task["requirements_sha256"]
            or live.get("requirements_sha256") != task["requirements_sha256"]):
        _conflict("远端任务不在评审阶段，或项目/需求身份已变化")
    with db.write_txn(conn):
        current = db.require_task(conn, task_id)
        repeated = _deduplicated(conn, current, identity)
        if repeated:
            return repeated
        if _snapshot(conn, task_id, identity["execution_run_id"]) != snapshot or candidate(conn, current) != identity:
            _conflict("核验期间任务、请求或执行证据已变化，请刷新后重试")
        if _read_files(conn, current, request) != files:
            _conflict("核验期间本机计划或评审恢复证据已变化")
        resolution = {"action": "recover_review", "actor": "user", "at": ctx.now_iso,
                      "note": payload.get("note"), **identity}
        core.transition_task(conn, current, "in_review", ctx.now_iso, "user",
                             detail={"via": "resolve recover_review", **identity},
                             **core.clear_block_fields(), auto_advance_paused=1, resolution=resolution)
        db.add_event(conn, ctx.now_iso, "review_stage_recovered", task_id=task_id,
                     request_id=identity["failed_review_request_id"], actor="user",
                     detail={**identity, "plan_sha256": current["plan_sha256"]})
        db.add_event(conn, ctx.now_iso, "resolved", task_id=task_id, actor="user", detail=resolution)
        return {"ok": True, "deduplicated": False, "task_status": "in_review", "request": None}
