"""Explicitly correct a stopped Codex report to an input block, preserving evidence."""
from __future__ import annotations

import json
from pathlib import Path

from . import codex_execution, db, execution_contract, headless_adapter, round_store, runtime_client, util

CODE = "execution_followup_required"


def _need(condition, message):
    util.require(condition, message, "conflict")


def _eligible(conn, task):
    from .execution_questions import open_records
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    _need(db.task_is_active(task) and task["status"] == "needs_user"
          and task["stage"] == "monitor" and task["failure_stage"] in ("monitor", "running")
          and task["failure_code"] in ("execution_failed", CODE)
          and task["sent"] == 1 and task["send_state"] == "sent"
          and row is not None and row["backend"] == "codex" and row["save_state"] == "active",
          "只有当前已发送的 Codex 执行失败可更正为等待材料或决定")
    _need(not open_records(task) and not conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
        (task["task_id"],)).fetchone(), "当前仍有活动请求或待答问题，不能更正分类")
    return row


def _proof(conn, task, row, expected_job):
    source = headless_adapter.resolve_source(conn)
    directory = source.run_dir(task["task_id"], task["run_id"])
    hashes = {}
    job = codex_execution._read_json(directory / "job.json", directory, hashes)
    _need(hashes[str(directory / "job.json")] == expected_job, "执行报告已变化，请重新核对后更正")
    spec = codex_execution._read_json(directory / "spec.json", directory, hashes)
    _need(isinstance(job, dict) and isinstance(spec, dict), "执行记录或冻结配置不是对象")
    contract = execution_contract.validate_spec(spec, job=job)
    _need(contract is not None and spec.get("stage_contract_required") is True
          and contract["inputs"].get("requirements_sha256") == task["requirements_sha256"],
          "当前执行缺少一致的冻结契约")
    for record in (job, spec):
        _need(record.get("task_id") == task["task_id"] and record.get("run_id") == task["run_id"]
              and record.get("backend") == "codex" and record.get("config") == util.jload(row["config_json"])
              and record.get("plan_sha256") == task["plan_sha256"]
              and record.get("runs_dir") == str(directory), "执行记录与当前任务身份不符")
    scope = runtime_client.stable_scope_id("execution", task_id=task["task_id"], run_id=task["run_id"])
    _need(job.get("scope_id") == spec.get("scope_id") == scope, "原 Codex 执行 scope 身份不符")
    dispatch = db.get_request(conn, contract["request_id"])
    _need(dispatch is not None and dispatch["task_id"] == task["task_id"]
          and dispatch["run_id"] == task["run_id"] and dispatch["kind"] == "dispatch"
          and dispatch["stage"] == "dispatch",
          "冻结执行的领取身份不符")
    _need(spec.get("cwd") == row["worktree_path"] and spec.get("repository") == task["repository"]
          and spec.get("plan_path") == task["plan_path"]
          and job.get("spec_sha256") == hashes[str(directory / "spec.json")]
          and job.get("pid") == row["pid"] and job.get("observe_pid") in (None, job.get("pid"))
          and job.get("session_id") == row["session_id"] == task["session_id"]
          and bool(job.get("session_id")) and job.get("send_state") == "sent"
          and job.get("status") == "failed" and type(job.get("exit_code")) is int
          and job["exit_code"] != 0 and job.get("pending_input") is None,
          "原 Codex 执行身份、报告或发送状态不完整")
    codex_execution.require_settled(job)
    turns = job.get("turns") or []
    _need(bool(turns), "原 Codex 执行没有已结束的轮次")
    for index, turn in enumerate(turns, 1):
        _need(turn.get("turn_id") == "codex:" + task["run_id"] + ":" + str(index)
              and turn.get("session_id") == task["session_id"] and turn.get("source") == "codex_adapter",
              "原 Codex 执行代次身份不符")
    turn = turns[-1]
    _need(job.get("turn_id") == turn["turn_id"] and turn.get("status") == "completed"
          and turn.get("exit_code") == 0 and type(turn.get("exit_code")) is int,
          "原 Codex 模型轮次并未正常结束")
    output = turn.get("output_file")
    _need(isinstance(output, str) and bool(output), "原 Codex 最终报告路径缺失")
    report = codex_execution.validate_report(codex_execution._read_json(directory / output, directory, hashes))
    _need(hashes[str(directory / output)] == turn.get("output_sha256")
          and report["status"] == "failed", "原 Codex 最终报告指纹或状态不符")
    commands = contract["verification_commands"]
    _need(bool(commands) and len(report["checks"]) == len(commands)
          and {check["command"] for check in report["checks"]} == set(commands)
          and all(check["exit_code"] == 0 for check in report["checks"]),
          "此更正仅适用于全部冻结检查已通过、仍需材料或决定的报告")
    # Keep the native stream's existing identity/completeness checks. Classification
    # is not a successful verdict: earlier failed attempts and later tools need
    # not invalidate the last actual verify call in the historical final snapshot.
    codex_execution._native_checks(directory, turn, hashes, commands=commands)
    native_path = util.inside(directory / turn["events_file"], directory, "Codex native events")
    _need(hashes[str(native_path)] == turn.get("native_sha256"), "原生事件指纹已变化")
    native_raw = native_path.read_bytes()
    _need(util.sha256_bytes(native_raw) == hashes[str(native_path)], "读取期间原生事件已变化")
    rows = [json.loads(line) for line in native_raw.decode("utf-8").splitlines()]
    final_source = (job.get("failure_report") or {}).get("source_sha256")
    _need(isinstance(final_source, str) and len(final_source) == 64,
          "原报告缺少最终源码快照")
    started, latest = {}, {}
    for native_row in rows:
        event = native_row["event"]
        item = event.get("item") or {}
        command = codex_execution.exact_command(item.get("command")) if item.get("type") == "command_execution" else None
        if command not in commands:
            continue
        if event.get("type") == "item.started":
            started[item.get("id")] = native_row
        elif event.get("type") == "item.completed":
            latest[command] = (started.get(item.get("id")), native_row)
    for command in commands:
        opening, ending = latest.get(command, (None, None))
        item = ((ending or {}).get("event") or {}).get("item") or {}
        opening_item = ((opening or {}).get("event") or {}).get("item") or {}
        _need(opening is not None and ending is not None
              and codex_execution.exact_command(opening_item.get("command")) == command
              and item.get("status") == "completed" and type(item.get("exit_code")) is int
              and item["exit_code"] == 0 and opening.get("source_sha256") == final_source
              and ending.get("source_sha256") == final_source,
              "原生检查没有逐条证明冻结命令的最后调用在最终源码快照上通过")
    events = [row["event"] for row in rows]
    texts = [(event.get("item") or {}).get("text") for event in events
             if event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "agent_message"]
    try:
        declared = json.loads(texts[-1]) if texts else None
    except (ValueError, TypeError):
        declared = None
    _need(declared == report, "原生最终消息与落盘报告不一致")
    hashes[str(Path(spec["plan_path"]))] = task["plan_sha256"]
    _unchanged(hashes)
    return job, hashes


def _unchanged(hashes):
    try:
        _need(all(util.sha256_file(Path(path)) == digest for path, digest in hashes.items()),
              "读取期间原执行证据已变化，请重新核对")
    except OSError as exc:
        raise util.KanpilotError("conflict", "原执行证据已不可读") from exc


def resolve(ctx, payload):
    """No remote calls, model launches, question waiter or successful verification receipt."""
    _need(payload.get("actor", "user") == "user", "分类更正必须由用户明确发起")
    note = payload.get("note")
    _need(isinstance(note, str) and bool(note.strip()) and len(note) <= 4096,
          "请填写具体缺少的材料或决定及下一步（不超过 4096 字）")
    expected = payload.get("expected_execution_run_id")
    expected_job = payload.get("expected_job_sha256")
    _need(isinstance(expected_job, str) and len(expected_job) == 64
          and all(char in "0123456789abcdef" for char in expected_job), "更正必须绑定原 job.json 指纹")
    task = db.require_task(ctx.conn, payload["task_id"])
    _need(expected and expected == task["run_id"], "所选执行轮次已变化，请刷新后处理")
    row = _eligible(ctx.conn, task)
    before, before_round = dict(task), dict(row)
    try:
        job, hashes = _proof(ctx.conn, task, row, expected_job)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise util.KanpilotError("conflict", "原 Codex 执行记录不完整，不能更正分类") from exc
    binding = {"run_id": expected, "session_id": task["session_id"], "job_sha256": expected_job,
               "note": note, "evidence_sha256": hashes}
    with db.write_txn(ctx.conn):
        task = db.require_task(ctx.conn, payload["task_id"])
        _need(dict(task) == before, "当前任务已变化，请刷新后更正")
        row = _eligible(ctx.conn, task)
        _need(dict(row) == before_round, "当前执行轮次已变化，请刷新后更正")
        _unchanged(hashes)
        codex_execution.require_settled(job)
        extra = db.task_extra(task)
        previous = extra.get("execution_followup") or {}
        if task["failure_code"] == CODE:
            _need(all(previous.get(key) == value for key, value in binding.items())
                  and task["failure_reason"] == note and task["blocked_summary"] == note,
                  "当前更正内容已变化，请重新核对")
            return {"ok": True, "deduplicated": True, "task_status": "needs_user", "request": None}
        audit = dict(binding, actor="user", at=ctx.now_iso, previous={key: task[key] for key in
                     ("failure_code", "failure_reason", "blocked_summary", "blocked_at", "failure_stage")})
        extra["execution_followup"] = audit
        db.update_task(ctx.conn, task["task_id"], ctx.now_iso, failure_code=CODE,
                       failure_reason=note, blocked_summary=note, extra_json=extra,
                       resolution={"action": "await_input", "note": note, "actor": "user", "at": ctx.now_iso})
        db.add_event(ctx.conn, ctx.now_iso, "execution_classification_corrected", task_id=task["task_id"],
                     actor="user", detail=audit)
    return {"ok": True, "task_status": "needs_user", "request": None}
