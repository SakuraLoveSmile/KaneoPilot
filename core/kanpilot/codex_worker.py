"""Managed, persistent Codex exec worker; SQLite writes only through core commands."""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import (cli, codex_execution as adapter, codex_planner, codex_provider, constants,
               execution_contract, git_delivery, process_owner, repair_prompt,
               runtime_client, runtime_operations, util)
from .pi_worker import _EventLog


class Stopped(BaseException):
    pass


def _signal(signum, frame):
    raise Stopped("signal:" + str(signum))


def _heartbeat(spec):
    return cli.run("heartbeat", {"request_id": spec["request_id"], "run_id": spec["run_id"], "progress": "executing"}, Path(spec["db_path"]))


def _stop_child(proc):
    """Group was exclusively created by this worker, including shell descendants."""
    if proc is None:
        return
    current = process_owner.process_birth(proc.pid)
    recorded = getattr(proc, "codex_birth", None)
    if current is not None and recorded is not None and current != recorded:
        raise util.KanpilotError("conflict", "Codex CLI PID 已被复用，拒绝发信号")
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        if proc.poll() is None:
            raise
    deadline = time.monotonic() + 2
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(.02)
    # A shell can exit before its descendants; always clean the owned group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if proc.poll() is None:
            raise
    proc.wait(timeout=3)
    if proc.stdout:
        proc.stdout.close()


def _start_authorized(spec):
    """Read only the core's launch receipt. Never creates or modifies a database."""
    expected = runtime_client.stable_scope_id("execution", task_id=spec["task_id"], run_id=spec["run_id"])
    if spec.get("scope_id") != expected:
        adapter._fail("Codex 启动 scope 不符")
    uri = Path(spec["db_path"]).resolve().as_uri() + "?mode=ro"
    deadline = time.monotonic() + 5
    while True:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM runtime_operations WHERE scope_id=? AND operation_id=?", (expected, "codex-start:" + spec["run_id"])).fetchone()
        if row is not None and row["harness_id"] == "codex" and row["tool"] == "codex_execution_start" and row["status"] == "accepted":
            result = util.jload(row["result_json"]) or {}
            if result.get("pid") == os.getpid():
                return
            adapter._fail("Codex 启动收据 worker PID 不符")
        if time.monotonic() >= deadline:
            adapter._fail("Codex 启动没有核心 accepted 操作收据")
        time.sleep(.05)



def _mailbox_authorized(spec, payload, tool):
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        return False
    uri = Path(spec["db_path"]).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM runtime_operations WHERE scope_id=? AND operation_id=?", (spec["scope_id"], operation_id)).fetchone()
    return bool(row and row["harness_id"] == "codex" and row["tool"] == tool and row["status"] == "accepted"
                and row["session_id"] == payload.get("session_id")
                and row["request_sha256"] == runtime_operations.compute_request_sha256(payload))


def _reconcile(spec):
    try:
        cli.run("reconcile-execution-question", {"task_id": spec["task_id"], "run_id": spec["run_id"]}, Path(spec["db_path"]))
    except util.KanpilotError:
        # Observation later reconciles durable consumption/terminal evidence.
        pass


def run_real(spec):
    run_dir = Path(spec["runs_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    # Never adopt an interrupted job or implicitly repeat a paid invocation.
    try:
        with (run_dir / "codex-started.json").open("x", encoding="utf-8") as stream:
            stream.write(util.jdump({"pid": os.getpid(), "at": util.now_iso()}))
    except FileExistsError:
        return 3
    job = execution_contract.attach_to_job(spec, {
        "task_id": spec["task_id"], "run_id": spec["run_id"], "backend": "codex",
        "request_id": spec.get("request_id"), "scope_id": spec.get("scope_id"),
        "runs_dir": str(run_dir), "config": spec.get("config"), "plan_sha256": spec.get("plan_sha256"),
        "completion_policy": spec.get("completion_policy"), "pid": os.getpid(),
        "worker_birth": process_owner.process_birth(os.getpid()), "status": "running",
        "session_id": None, "turn_id": None, "turns": [], "send_state": "not_sent",
        "started_at": util.now_iso(), "exit_code": None, "cli_terminal": False,
    })
    log = _EventLog(run_dir / "events.jsonl")
    proc = None
    previous = {}

    def save():
        adapter.atomic_json(run_dir / "job.json", job)

    def stop_requested():
        for path in run_dir.glob("stop-*.json"):
            payload = adapter._read_json(path, run_dir)
            if payload.get("scope_id") == spec["scope_id"] and payload.get("session_id") == job["session_id"]:
                if any(_mailbox_authorized(spec, payload, tool) for tool in ("kp_runtime_stop", "kp_runtime_abort")):
                    raise Stopped("user_stop")

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, _signal)
        save()
        if spec.get("backend") != "codex" or not spec.get("stage_contract_required"):
            adapter._fail("Codex 执行必须有冻结阶段契约")
        error = adapter.validate_config(spec.get("config"))
        if error:
            adapter._fail(error)
        execution_contract.validate_spec(spec, job=job, starting=True)
        spec_path = Path(spec.get("_spec_path") or run_dir / "spec.json")
        persisted_spec = adapter._read_json(spec_path, run_dir)
        expected_spec = {key: value for key, value in spec.items() if key != "_spec_path"}
        if persisted_spec != expected_spec or util.sha256_file(Path(spec["plan_path"])) != spec["plan_sha256"]:
            adapter._fail("Codex 启动 spec 或计划指纹不符")
        job["spec_sha256"] = util.sha256_file(spec_path)
        if job["worker_birth"] is None:
            adapter._fail("Codex worker 进程出生身份不可读")
        _start_authorized(spec)
        provider = codex_provider.resolve_provider(constants.codex_home())
        ok, detail = codex_planner.check_available()
        if not ok:
            adapter._fail(detail)
        schema = run_dir / "codex-schema.json"
        adapter.atomic_json(schema, adapter.output_schema())
        prompt = ((spec.get("instruction") or "").rstrip() + "\n\n" + repair_prompt.execution_body(spec)
                  + "\n\nYou are executing this frozen task. Do not push, merge, release, or change the plan. "
                  "Run every frozen verification command exactly as written after the final source edit. "
                  "Return only the required JSON. If you need a user decision, return needs_input with questions; do not guess. "
                  "checks must list exact commands and actual integer exit codes.\nFrozen verification commands:\n"
                  + json.dumps(spec["required_verification_commands"], ensure_ascii=False))
        with process_owner.WorkerOwner(spec) as owner:
            last_heartbeat = time.monotonic()
            heartbeat_active = True
            while True:
                owner.check(); stop_requested()
                index = len(job["turns"]) + 1
                turn_id = "codex:" + spec["run_id"] + ":" + str(index)
                native_path = run_dir / ("codex-turn-" + str(index) + ".jsonl")
                output_path = run_dir / ("codex-final-" + str(index) + ".json")
                turn = {"turn_id": turn_id, "source": "codex_adapter", "status": "running", "events_file": native_path.name,
                        "output_file": output_path.name, "started_at": util.now_iso(), "cli_terminal": False}
                job.update(turn_id=turn_id, adapter_turn_index=index, status="running", pending_input=None)
                job["turns"].append(turn)
                # Durable checkpoint and generation precede every invocation/resume.
                save()
                stderr_path = run_dir / ("codex-stderr-" + str(index) + ".log")
                with stderr_path.open("xb") as stderr:
                    proc = subprocess.Popen(adapter.launcher_argv(spec, schema, output_path, provider, job["session_id"]),
                        cwd=spec["cwd"], env=codex_planner.child_env(provider), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
                    turn.update(pid=proc.pid, birth=process_owner.process_birth(proc.pid))
                    if turn["birth"] is None:
                        adapter._fail("Codex CLI 进程出生身份不可读")
                    proc.codex_birth = turn["birth"]
                    job.update(codex_cli_pid=proc.pid, codex_cli_birth=turn["birth"])
                    save()
                    proc.stdin.write(prompt.encode("utf-8")); proc.stdin.close()
                    deadline = time.monotonic() + spec.get("timeout_seconds", 7200)
                    buffer = b""; native_seq = 0; thread_seen = False; completed = False; final_text = None; errors = False
                    with native_path.open("xb") as native:
                        def consume(raw):
                            nonlocal native_seq, thread_seen, completed, final_text, errors, last_heartbeat
                            try:
                                event = json.loads(raw)
                            except (ValueError, UnicodeError) as exc:
                                raise util.KanpilotError("verification_unconfirmed", "Codex stdout 含非完整 JSONL") from exc
                            if type(event) is not dict or completed:
                                adapter._fail("Codex 终态后仍有事件或事件无效")
                            native_seq += 1
                            row = {"seq": native_seq, "turn_id": turn_id, "event": event}
                            item = event.get("item") or {}
                            if event.get("type") in ("item.started", "item.completed") and item.get("type") == "command_execution":
                                row["source_sha256"] = git_delivery.source_state_sha256(spec["cwd"])
                            native.write((util.jdump(row) + "\n").encode("utf-8")); native.flush(); os.fsync(native.fileno())
                            if event.get("type") == "thread.started":
                                session = event.get("thread_id")
                                if not isinstance(session, str) or not session or thread_seen or (job["session_id"] and session != job["session_id"]):
                                    adapter._fail("Codex thread.started 身份冲突")
                                try:
                                    _heartbeat(spec)
                                except util.KanpilotError as exc:
                                    if index == 1 or exc.code not in ("stale_run", "not_found"):
                                        raise
                                last_heartbeat = time.monotonic()
                                thread_seen = True; job.update(session_id=session, send_state="sent")
                                turn["session_id"] = session
                                save()
                                if index == 1:
                                    log.emit("session", util.now_iso(), session_id=session,
                                        session_title=spec.get("session_display_name") or "Codex CLI", task_id=spec["task_id"], run_id=spec["run_id"],
                                        session_marker=spec.get("session_marker"), backend="codex", turn_id=turn_id)
                                    log.emit("user", util.now_iso(), content=prompt, session_id=session)
                            elif not thread_seen:
                                adapter._fail("Codex 首个原生事件不是 thread.started")
                            elif event.get("type") == "turn.completed":
                                completed = True
                            elif event.get("type") in ("error", "turn.failed") or item.get("type") in ("error", "turn_failed") or item.get("error") is not None:
                                errors = True
                            if event.get("type") == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
                                final_text = item.get("text")
                            mapped = codex_planner.map_event(event)
                            if mapped and mapped[0] != "session":
                                kind, ts, fields = mapped
                                log.emit(kind, ts or util.now_iso(), **fields)

                        while True:
                            owner.check(); stop_requested()
                            if time.monotonic() >= deadline:
                                raise Stopped("timeout")
                            if heartbeat_active and time.monotonic() - last_heartbeat >= 10:
                                try:
                                    _heartbeat(spec)
                                except util.KanpilotError as exc:
                                    if thread_seen and exc.code in ("stale_run", "not_found"):
                                        heartbeat_active = False
                                    else:
                                        raise
                                last_heartbeat = time.monotonic()
                            readable, _, _ = select.select([proc.stdout], [], [], .1)
                            if readable:
                                chunk = os.read(proc.stdout.fileno(), 65536)
                                if not chunk:
                                    break
                                buffer += chunk
                                if len(buffer) > 16 * 1024 * 1024:
                                    adapter._fail("Codex 原生事件过大")
                                while b"\n" in buffer:
                                    line, buffer = buffer.split(b"\n", 1)
                                    consume(line)
                        if buffer:
                            adapter._fail("Codex stdout 尾部被截断", "verification_unconfirmed")
                    code = proc.wait(timeout=5)
                    _stop_child(proc); proc = None
                turn.update(exit_code=code, cli_terminal=True, ended_at=util.now_iso())
                if code != 0 or not thread_seen or not completed or errors:
                    adapter._fail("Codex CLI 缺少成功线程/轮次终态或非零退出", "verification_unconfirmed")
                report = adapter.validate_report(adapter._read_json(output_path, run_dir))
                try:
                    declared = json.loads(final_text) if isinstance(final_text, str) else None
                except ValueError:
                    declared = None
                if declared != report:
                    adapter._fail("Codex 最后消息与落盘 JSON 不一致", "verification_unconfirmed")
                turn.update(status="completed", native_sha256=util.sha256_file(native_path), output_sha256=util.sha256_file(output_path))
                source = git_delivery.source_state_sha256(spec["cwd"])
                save()
                if report["status"] != "needs_input":
                    if report["status"] != "completed":
                        adapter._fail("Codex 报告执行失败", "verification_unconfirmed")
                    report_id = "codex-report:" + spec["run_id"] + ":" + str(index)
                    job.update(report_id=report_id, verification_source_sha256=source, verification_evidence_version=1)
                    envelope = {key: job[key] for key in ("task_id", "run_id", "session_id", "turn_id", "plan_sha256", "verification_source_sha256", "report_id")}
                    envelope.update(report=report, native_sha256=turn["native_sha256"])
                    adapter.atomic_json(run_dir / adapter.REPORT_NAME, envelope)
                    job.update(status="finished", exit_code=0, ended_at=util.now_iso(), cli_terminal=True)
                    save(); log.emit("terminal", util.now_iso(), outcome="completed", reason="codex_report_ready")
                    return 0
                request_id = "codex-input:" + spec["run_id"] + ":" + str(index)
                pending = {"requestId": request_id, "toolCallId": "codex-question:" + str(index), "turnId": turn_id, "questions": report["questions"]}
                job.update(status="waiting_input", pending_input=pending, pending_source_sha256=source)
                save()
                question_payload = {"task_id": spec["task_id"], "run_id": spec["run_id"], "session_id": job["session_id"], "turn_id": turn_id,
                    "request_id": request_id, "tool_call_id": pending["toolCallId"], "state": "open", "questions": report["questions"]}
                question_reported = False
                confirmed_conflict_retries = 0
                answer_path = run_dir / ("answer-" + util.sha256_text(request_id) + ".json")
                wait_started = time.monotonic()
                while True:
                    owner.check(); stop_requested()
                    max_wait = spec["config"].get("execution_question_max_wait_seconds", 0)
                    if max_wait and time.monotonic() - wait_started >= max_wait:
                        raise Stopped("execution_question_expired")
                    if not question_reported:
                        try:
                            cli.run("report-execution-question", question_payload, Path(spec["db_path"]))
                        except util.KanpilotError as exc:
                            # A fast needs_input can precede dispatch-confirm.
                            # Retain the exact pending question; only repeat its
                            # idempotent core report, never a model invocation.
                            if exc.code != "conflict":
                                raise
                            current = cli.run("get-task", {"task_id": spec["task_id"]}, Path(spec["db_path"])).get("task") or {}
                            persisted = adapter._read_json(run_dir / "job.json", run_dir)
                            same_question = (persisted.get("task_id") == spec["task_id"]
                                and persisted.get("run_id") == spec["run_id"]
                                and persisted.get("session_id") == job["session_id"]
                                and persisted.get("turn_id") == turn_id
                                and persisted.get("status") == "waiting_input"
                                and persisted.get("pending_input") == pending)
                            if current.get("run_id") != spec["run_id"] or not same_question:
                                raise
                            if current.get("status") == "running" and confirmed_conflict_retries < 1:
                                # Confirm may commit between the rejected report
                                # and this read. Permit one report after that
                                # transition; a persistent conflict is an error.
                                confirmed_conflict_retries += 1
                            elif current.get("status") != "submitting":
                                raise
                        else:
                            question_reported = True
                            job["pending_input_reported_at"] = util.now_iso()
                            save()
                    if question_reported and answer_path.exists():
                        answer = adapter._read_json(answer_path, run_dir)
                        if not _mailbox_authorized(spec, answer, "codex_execution_input"):
                            time.sleep(.1)
                            continue
                        from .execution_questions import validate_wire_answers
                        validate_wire_answers(answer.get("answers"), job["session_id"], request_id, pending["toolCallId"])
                        if (any(answer.get(key) != value for key, value in (("scope_id", spec["scope_id"]), ("session_id", job["session_id"]), ("request_id", request_id), ("tool_call_id", pending["toolCallId"])))
                                or len(answer["answers"]) != len(report["questions"]) or source != git_delivery.source_state_sha256(spec["cwd"])):
                            adapter._fail("Codex 回答身份或等待期间源码变化")
                        # Persist consumption before resume. A crash never reuses this job.
                        turn.update(input_resolution="answered", answer_operation_id=answer["operation_id"], answer_sha256=util.sha256_file(answer_path), answer_consumed_at=util.now_iso())
                        job.update(pending_input=None)
                        save()
                        _reconcile(spec)
                        prompt = "User answers to the pending questions (same order):\n" + json.dumps(answer["answers"], ensure_ascii=False) + "\nContinue only this frozen task. Re-run every frozen verification command after final edits and return the required JSON."
                        break
                    if heartbeat_active and time.monotonic() - last_heartbeat >= 10:
                        try:
                            _heartbeat(spec)
                        except util.KanpilotError as exc:
                            if exc.code in ("stale_run", "not_found"):
                                heartbeat_active = False
                            else:
                                raise
                        last_heartbeat = time.monotonic()
                    time.sleep(.1)
    except (Stopped, process_owner.OwnerClosed, Exception) as exc:
        _stop_child(proc)
        if job["turns"] and job["turns"][-1]["status"] == "running":
            last = job["turns"][-1]
            actual_code = last.get("exit_code") if type(last.get("exit_code")) is int else (proc.returncode if proc and proc.returncode is not None else 3)
            last.update(status="aborted" if isinstance(exc, (Stopped, process_owner.OwnerClosed)) else "error",
                        cli_terminal=True, exit_code=actual_code, ended_at=util.now_iso())
        if job["turns"] and job["turns"][-1].get("pid") is not None:
            job["turns"][-1].setdefault("ended_at", util.now_iso())
            job["turns"][-1]["cli_terminal"] = True
            if type(job["turns"][-1].get("exit_code")) is not int:
                job["turns"][-1]["exit_code"] = proc.returncode if proc and proc.returncode is not None else 3
        job.update(status="aborted" if isinstance(exc, (Stopped, process_owner.OwnerClosed)) else "failed", exit_code=3, ended_at=util.now_iso(), cli_terminal=True, detail=util.redact(str(exc)), pending_input=None)
        save(); log.emit("terminal", util.now_iso(), outcome="failed", reason=job["detail"])
        if job.get("session_id"):
            _reconcile(spec)
        return 3
    finally:
        _stop_child(proc)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    path = Path(args.spec).resolve()
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["_spec_path"] = str(path)
    return run_real(spec)


if __name__ == "__main__":
    sys.exit(main())
