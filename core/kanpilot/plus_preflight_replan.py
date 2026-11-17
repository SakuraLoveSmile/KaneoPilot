"""Proof for explicitly replanning a Plus command-limit failure before send.

This never runs verification commands or changes the failed execution record.
Unknown delivery remains unknown unless the exact registered worker and its
immutable pre-runtime failure can be proved locally.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import (constants, core, db, execution_contract, git_delivery,
               headless_adapter, pi_desktop_backend, planner_inputs,
               process_owner, round_store, runtime_client, util, verify_evidence)


def _read(root, name):
    raw, _reason = planner_inputs._read_worktree_material(
        str(root), name, constants.PLAN_INPUT_FILE_MAX_BYTES)
    util.require(raw is not None, "Plus 未发送证据缺失或不可安全读取", "conflict")
    return raw


def _worker(conn, spec, job, spec_path):
    instance = process_owner.validate_instance_id(spec.get("executor_instance_id"))
    entry = process_owner._instances(conn).get(instance) or {}
    records = [worker for worker in entry.get("workers", [])
               if isinstance(worker, dict) and worker.get("task_id") == spec["task_id"]
               and worker.get("run_id") == spec["run_id"]]
    util.require(len(records) == 1, "Plus 原 worker 出生登记不唯一或缺失", "conflict")
    record = records[0]
    util.require(type(record.get("pid")) is int and record["pid"] > 0
                 and record["pid"] == job.get("pid")
                 and record.get("spec_path") == str(spec_path.resolve())
                 and type(record.get("started_seconds")) is int and record["started_seconds"] > 0
                 and type(record.get("started_microseconds")) is int
                 and 0 <= record["started_microseconds"] < 1_000_000,
                 "Plus 原 worker 出生登记与执行身份不符", "conflict")
    current = process_owner.process_birth(record["pid"])
    if current is None:
        util.require(not util.pid_alive(record["pid"]), "Plus 原 worker 仍存活或出生身份不可读", "conflict")
    else:
        util.require(any(current[key] != record[key] for key in current),
                     "Plus 原 worker 仍在运行", "conflict")
    return record


def prove(conn, task, expected_run):
    """Return a stable receipt only for this precise pre-runtime length failure."""
    try:
        util.require(db.task_is_active(task) and core.is_automation_managed(task)
                     and task["status"] == "needs_user" and task["stage"] == "dispatch"
                     and task["failure_code"] == "dispatch_failed" and task["failure_stage"] == "submitting"
                     and task["sent"] in (None, 0) and task["send_state"] == "unknown"
                     and expected_run and expected_run == task["run_id"] and not task["session_id"]
                     and not db.task_extra(task).get("plan_execution_hold")
                     and not core.review_repair_of(task).get("source_kind")
                     and not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                                          (task["task_id"],)).fetchone(),
                     "当前任务不满足 Plus 未发送重新规划条件", "conflict")
        source = headless_adapter.resolve_source(conn)
        signature = source.source_signature(task["task_id"], expected_run)
        directory = source.run_dir(task["task_id"], expected_run)
        raw = {name: _read(directory, name) for name in ("spec.json", "job.json", "events.jsonl")}
        spec, job = (json.loads(raw[name]) for name in ("spec.json", "job.json"))
        database = next(record[2] for record in conn.execute("PRAGMA database_list") if record[1] == "main")
        util.require(isinstance(spec.get("db_path"), str) and database
                     and Path(spec["db_path"]).resolve() == Path(database).resolve(),
                     "Plus 原执行记录与数据库身份不符", "conflict")
        row = round_store.require_round(conn, task["task_id"], expected_run)
        request = db.get_request(conn, spec.get("request_id"))
        config = util.jload(row["config_json"])
        contract = execution_contract.validate_spec(spec, job=job)
        util.require(row["save_state"] == "active" and row["backend"] == "pi_desktop"
                     and row["run_status"] == "failed" and row["started_at"] and row["pid"]
                     and row["session_id"] is None and spec.get("backend") == job.get("backend") == "pi_desktop"
                     and spec.get("task_id") == job.get("task_id") == task["task_id"]
                     and spec.get("run_id") == job.get("run_id") == expected_run
                     and spec.get("repository") == task["repository"]
                     and spec.get("cwd") == row["worktree_path"] and row["worktree_path"]
                     and spec.get("runs_dir") == str(directory)
                     and spec.get("completion_policy") == job.get("completion_policy") == "command_verification"
                     and spec.get("config") == job.get("config") == config
                     and config.get("backend") == "pi_desktop"
                     and spec.get("plan_path") == task["plan_path"]
                     and spec.get("plan_sha256") == job.get("plan_sha256") == task["plan_sha256"]
                     and spec.get("stage_contract_required") is True and contract
                     and contract["inputs"].get("requirements_sha256") == task["requirements_sha256"]
                     and job.get("status") == "failed" and type(job.get("exit_code")) is int
                     and job["exit_code"] == 1 and job.get("send_state") == "not_sent"
                     and job.get("session_id") is None and job.get("turn_id") is None
                     and job.get("pi_desktop_pid") is None and not job.get("scope_id")
                     and not job.get("turns") and not job.get("pending_input") and job.get("ended_at")
                     and job.get("pid") == row["pid"]
                     and request is not None and request["task_id"] == task["task_id"]
                     and request["run_id"] == expected_run and request["status"] == "failed"
                     and request["stage"] == request["kind"] == "dispatch",
                     "Plus 未发送的执行、请求或计划身份不符", "conflict")
        util.require(raw["events.jsonl"].endswith(b"\n"), "Plus 未发送终态记录不完整", "conflict")
        events = [json.loads(line) for line in raw["events.jsonl"].splitlines()]
        util.require(len(events) == 1 and events[0].get("seq") == 1
                     and events[0].get("type") == "terminal" and events[0].get("outcome") == "failed"
                     and events[0].get("reason") == "verification_requirements_invalid"
                     and str(job.get("detail") or "").startswith("verification_requirements_invalid:"),
                     "Plus 存在其他执行活动或终态原因不符", "conflict")
        plan = Path(task["plan_path"])
        plan_raw = _read(plan.parent, plan.name)
        util.require(util.sha256_bytes(plan_raw) == task["plan_sha256"], "Plus 冻结计划已变化", "conflict")
        parsed = verify_evidence.parse_verify_block(plan_raw.decode("utf-8"))
        commands = parsed.get("commands")
        util.require(parsed.get("ok") and commands == spec.get("required_verification_commands"),
                     "Plus 冻结验证命令不符", "conflict")
        sizes = [len(command.encode("utf-8")) for command in commands]
        util.require(any(size > 4096 for size in sizes) or sum(sizes) > 65536,
                     "Plus 原计划并非已知验证命令长度失败", "conflict")
        try:
            pi_desktop_backend.validate_required_verification_commands(commands, "approve")
        except util.KanpilotError:
            pass
        else:
            raise util.KanpilotError("conflict", "Plus 原验证命令不能复现前置拒绝")
        scope = runtime_client.stable_scope_id("execution", task_id=task["task_id"], run_id=expected_run)
        util.require(not conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=?", (scope,)).fetchone()
                     and not any(os.path.lexists(constants.app_support() / "mcp" / "grants" / (scope + suffix))
                                 for suffix in ("_runtime.json", "_task.json")),
                     "Plus 已存在 runtime 操作或会话授权，不能证明未发送", "conflict")
        worker = _worker(conn, spec, job, directory / "spec.json")
        delivery = core.delivery_of(task)
        util.require(delivery.get("worktree_path") == row["worktree_path"] and delivery.get("head_sha"),
                     "Plus 原成果工作树登记不符", "conflict")
        git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]),
                                           require_clean=False, expected_head=delivery["head_sha"])
        state = git_delivery.source_state_sha256(row["worktree_path"])
        util.require(state == contract["inputs"].get("initial_source_sha256"), "Plus 前置失败后源码已变化", "conflict")
        util.require(signature and source.source_signature(task["task_id"], expected_run) == signature
                     and all(_read(directory, name) == content for name, content in raw.items())
                     and _read(plan.parent, plan.name) == plan_raw
                     and _worker(conn, spec, job, directory / "spec.json") == worker,
                     "Plus 未发送证据在核验期间已变化", "conflict")
        return {"run_id": expected_run, "request_id": request["request_id"],
                "task_sha256": util.sha256_text(util.jdump(dict(task))),
                "files": {name: util.sha256_bytes(content) for name, content in raw.items()},
                "plan_sha256": task["plan_sha256"], "source_sha256": state,
                "round_sha256": util.sha256_text(util.jdump(dict(row))),
                "request_sha256": util.sha256_text(util.jdump(dict(request))),
                "worker": worker, "scope_id": scope}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, headless_adapter.HeadlessError,
            git_delivery.GitDeliveryError, util.KanpilotError) as exc:
        raise util.KanpilotError("conflict", "Plus 验证命令失败的未发送身份、证据或进程终态不可核验，不能重新规划") from exc
