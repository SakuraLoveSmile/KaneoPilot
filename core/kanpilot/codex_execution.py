"""Codex CLI runtime boundary. Reads evidence; never runs verification commands."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from . import (codex_planner, codex_provider, constants, db, execution_contract,
               git_delivery, planner_config, process_owner, round_store,
               runtime_client, runtime_operations, stage_evidence, util)

DEFAULT_CONFIG = {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
REPORT_NAME = "codex-report.json"


def _fail(message, code="verification_identity_unconfirmed"):
    raise util.KanpilotError(code, message)


def atomic_json(path, value):
    path = Path(path)
    util.inside(path, path.parent, "Codex evidence")
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(util.jdump(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _catalog():
    resolution = codex_provider.resolve_provider(constants.codex_home())
    return codex_provider.model_catalog(resolution.model_catalog_json) if resolution.model_catalog_json else []


def validate_config(block):
    if not isinstance(block, dict):
        return "Codex 执行配置未保存"
    if set(block) - {"backend", "model", "reasoning_effort", "sandbox", "execution_question_max_wait_seconds"}:
        return "Codex 执行配置包含未知字段"
    if block.get("backend", "codex") != "codex" or block.get("sandbox", "workspace-write") != "workspace-write":
        return "Codex 执行仅支持 workspace-write 沙箱"
    if "execution_question_max_wait_seconds" in block and (type(block["execution_question_max_wait_seconds"]) is not int or block["execution_question_max_wait_seconds"] < 0):
        return "执行问题等待时间必须是非负整数"
    try:
        catalog = _catalog()
    except util.KanpilotError as exc:
        return exc.message
    error = planner_config.validate_group({"planner_model": block.get("model"), "reasoning_effort": block.get("reasoning_effort")}, catalog)
    if error:
        return error
    if catalog and not any(row["id"] == block["model"] for row in catalog):
        return "Codex 模型不在当前模型目录中：" + block["model"]
    if block["reasoning_effort"] not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        return "Codex 思考等级无效"
    return None


def capabilities(block=None, timeout=10):
    result = {"available": False, "detail": "Codex 执行配置未保存", "path": constants.codex_bin(), "version": None, "models": []}
    if block is None:
        return result
    error = validate_config(block)
    if error:
        result["detail"] = error
        return result
    ok, detail = codex_planner.check_available()
    result.update(detail=detail, models=_catalog())
    if not ok:
        return result
    try:
        probe = subprocess.run([result["path"], "--version"], capture_output=True, text=True, timeout=timeout, env=codex_planner.child_env())
        result["version"] = probe.stdout.strip()[:256]
        result["available"] = probe.returncode == 0 and bool(result["version"])
        if not result["available"]:
            result["detail"] = "Codex CLI 版本探测失败"
    except (OSError, subprocess.TimeoutExpired):
        result["detail"] = "Codex CLI 版本探测失败或超时"
    return result


def output_schema():
    return {"type": "object", "additionalProperties": False,
            "required": ["status", "summary", "checks", "questions"], "properties": {
                "status": {"type": "string", "enum": ["completed", "needs_input", "failed"]},
                "summary": {"type": "string"},
                "checks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["command", "exit_code"], "properties": {"command": {"type": "string"}, "exit_code": {"type": "integer"}}}},
                "questions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["title", "options"], "properties": {"title": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}}}}}


def validate_report(report):
    if (type(report) is not dict or set(report) != {"status", "summary", "checks", "questions"}
            or report["status"] not in ("completed", "needs_input", "failed") or type(report["summary"]) is not str
            or type(report["checks"]) is not list or type(report["questions"]) is not list):
        _fail("Codex 最终结构化报告格式无效", "verification_unconfirmed")
    for check in report["checks"]:
        if (type(check) is not dict or set(check) != {"command", "exit_code"}
                or type(check["command"]) is not str or not check["command"].strip() or type(check["exit_code"]) is not int):
            _fail("Codex 报告检查缺少独立整数退出码", "verification_unconfirmed")
    for question in report["questions"]:
        if (type(question) is not dict or set(question) != {"title", "options"}
                or type(question["title"]) is not str or not question["title"].strip()
                or type(question["options"]) is not list
                or any(type(option) is not str or not option.strip() for option in question["options"])):
            _fail("Codex 报告问题格式无效", "verification_unconfirmed")
    if (report["status"] == "needs_input") != bool(report["questions"]):
        _fail("Codex 报告状态与问题不一致", "verification_unconfirmed")
    if report["questions"]:
        from .execution_questions import normalize_questions
        normalize_questions(report["questions"])
    return report


def launcher_argv(spec, schema_path, out_path, provider, session_id=None):
    config = spec["config"]
    argv = [constants.codex_bin(), "exec", "-s", "workspace-write", "-C", spec["cwd"]]
    if session_id:
        argv += ["resume"]
    argv += ["--ignore-user-config", "--json", "--skip-git-repo-check", "--output-schema", str(schema_path), "-o", str(out_path)]
    for value in provider.config_args:
        argv += ["-c", value]
    for key, value in (("model", config["model"]), ("model_reasoning_effort", config["reasoning_effort"]),
                       ("sandbox_mode", "workspace-write"), ("approval_policy", "never")):
        argv += ["-c", key + "=" + json.dumps(value)]
    if session_id:
        argv.append(session_id)
    return argv + ["-"]


def exact_command(command):
    """Unwrap only a single literal sh/bash/zsh -c/-lc invocation; never evaluate."""
    if type(command) is not str:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) == 3 and words[0] in ("sh", "bash", "zsh", "/bin/sh", "/bin/bash", "/bin/zsh") and words[1] in ("-c", "-lc"):
        return words[2]
    return command


def _alive(pid, birth):
    if type(pid) is not int or not isinstance(birth, dict) or set(birth) != {"started_seconds", "started_microseconds"} or any(type(v) is not int for v in birth.values()):
        _fail("Codex 进程出生身份缺失")
    current = process_owner.process_birth(pid)
    if current is None:
        if util.pid_alive(pid):
            _fail("Codex 进程出生身份不可读")
        return False
    return current == birth


def job_alive(job):
    """Read-only exact process identity; an unreadable live PID is an error."""
    identities = [(job.get("pid"), job.get("worker_birth"))]
    identities += [(turn.get("pid"), turn.get("birth")) for turn in job.get("turns") or [] if turn.get("pid") is not None]
    return any(_alive(pid, birth) for pid, birth in identities)


def require_settled(job):
    if (job.get("status") not in ("finished", "failed", "aborted") or type(job.get("exit_code")) is not int
            or not job.get("ended_at") or job.get("cli_terminal") is not True or job_alive(job)):
        _fail("Codex worker/CLI 尚无可确认的持久终态", "conflict")
    for turn in job.get("turns") or []:
        if (turn.get("status") not in ("completed", "error", "aborted") or not turn.get("ended_at")
                or turn.get("cli_terminal") is not True or type(turn.get("exit_code")) is not int):
            _fail("Codex CLI 代次终态不完整", "conflict")


def require_terminal(job):
    require_settled(job)
    if (job.get("status") != "finished" or job["exit_code"] != 0 or not job.get("turns")
            or any(turn["status"] != "completed" or turn["exit_code"] != 0 for turn in job["turns"])):
        _fail("Codex CLI 尚无持久 finished/0 终态", "verification_unconfirmed")


def _read_json(path, directory, hashes=None):
    path = util.inside(path, directory, "Codex evidence")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise util.KanpilotError("verification_unconfirmed", "Codex 证据不可读或不完整") from exc
    if hashes is not None:
        hashes[str(path)] = util.sha256_bytes(raw)
    return value


def _native_checks(directory, turn, hashes, commands=None):
    if type(turn.get("events_file")) is not str or type(turn.get("session_id")) is not str:
        _fail("Codex 原生事件文件身份缺失")
    path = util.inside(directory / turn["events_file"], directory, "Codex native events")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise util.KanpilotError("verification_unconfirmed", "Codex 原生事件文件不可读") from exc
    hashes[str(path)] = util.sha256_bytes(raw)
    if not raw.endswith(b"\n"):
        _fail("Codex 原生事件日志尾部不完整", "verification_unconfirmed")
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise util.KanpilotError("verification_unconfirmed", "Codex 原生事件日志损坏") from exc
    if not rows or any(type(row) is not dict or row.get("seq") != i or row.get("turn_id") != turn["turn_id"] or type(row.get("event")) is not dict for i, row in enumerate(rows, 1)):
        _fail("Codex 原生事件序列或代次不符")
    events = [row["event"] for row in rows]
    if (sum(event.get("type") == "thread.started" for event in events) != 1
            or next(event for event in events if event.get("type") == "thread.started").get("thread_id") != turn["session_id"]
            or sum(event.get("type") == "turn.completed" for event in events) != 1
            or events[-1].get("type") != "turn.completed"
            or any(event.get("type") in ("error", "turn.failed") for event in events)):
        _fail("Codex 原生线程或完成事件缺失", "verification_unconfirmed")
    # Reconstruct the final verification window from native event order, not
    # pipe timing. A later tool which may write invalidates earlier checks even
    # when all stdout lines were delivered together after the write happened.
    declared = set(commands) if commands is not None else {
        exact_command((event.get("item") or {}).get("command"))
        for event in events if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict) and event["item"].get("type") == "command_execution"}
    result, started = [], {}

    def invalidate():
        for check in result:
            check.update(exit_code=None, source_sha256=None, detail="验证后出现可能写入工作树的工具事件")

    readonly_items = {"reasoning", "agent_message", "assistant_message", "web_search", "todo_list"}
    for row in rows:
        event = row["event"]
        item = event.get("item") or {}
        if not isinstance(item, dict):
            _fail("Codex 原生 item 格式无效")
        kind, event_type = item.get("type"), event.get("type")
        if event_type in ("item.started", "item.updated", "item.completed"):
            command = exact_command(item.get("command")) if kind == "command_execution" else None
            if kind == "command_execution" and command in declared:
                identity = item.get("id")
                if event_type == "item.started":
                    if type(identity) is not str or identity in started:
                        _fail("Codex 验证命令开始事件身份重复或缺失")
                    started[identity] = {"command": command, "source_sha256": row.get("source_sha256")}
                elif event_type == "item.completed":
                    opening = started.get(identity) or {}
                    code = item.get("exit_code")
                    source = row.get("source_sha256")
                    intact = (type(identity) is str and opening.get("command") == command
                              and isinstance(source, str) and opening.get("source_sha256") == source)
                    result.append({"id": identity, "command": command,
                                   "exit_code": code if intact and type(code) is int and item.get("status") == "completed" else None,
                                   "result": "passed" if type(code) is int and code == 0 else "failed",
                                   "provenance": "harness_observed", "source_sha256": source if intact else None,
                                   "detail": "" if intact else "验证命令开始/完成源码快照不一致或缺失"})
            elif kind not in readonly_items:
                invalidate()
        elif event_type not in ("thread.started", "turn.started", "turn.completed"):
            invalidate()
    return result


def read_evidence(conn, task):
    from . import verification_recovery
    hashes, job, _events, declaration = verification_recovery._read_run_evidence(conn, task)
    require_terminal(job)
    from . import headless_adapter
    directory = headless_adapter.resolve_source(conn).run_dir(task["task_id"], task["run_id"])
    if job.get("runs_dir") != str(directory):
        _fail("Codex 报告运行目录不符")
    spec = _read_json(directory / "spec.json", directory, hashes)
    contract = execution_contract.validate_spec(spec, job=job)
    if contract is None or job.get("backend") != "codex":
        _fail("Codex 执行契约缺失")
    if job.get("spec_sha256") != hashes[str(directory / "spec.json")]:
        _fail("Codex 固化 spec 指纹变化")
    if contract["verification_commands"] != declaration["commands"]:
        _fail("Codex 契约验证命令与冻结计划不符")
    source = git_delivery.source_state_sha256(spec["cwd"])
    if source != job.get("verification_source_sha256"):
        _fail("Codex 验证后源码已变化")
    envelope = _read_json(directory / REPORT_NAME, directory, hashes)
    for key in ("task_id", "run_id", "session_id", "turn_id", "plan_sha256", "verification_source_sha256"):
        if envelope.get(key) != job.get(key):
            _fail("Codex 报告身份不符：" + key)
    turn = job["turns"][-1]
    if envelope.get("native_sha256") != turn.get("native_sha256") or envelope.get("report_id") != job.get("report_id"):
        _fail("Codex 报告与原生证据绑定不符")
    report = validate_report(envelope.get("report"))
    if report["status"] != "completed":
        _fail("Codex 报告未完成", "verification_unconfirmed")
    native = []
    for index, generation in enumerate(job["turns"], 1):
        if (generation.get("turn_id") != "codex:" + job["run_id"] + ":" + str(index)
                or generation.get("source") != "codex_adapter" or generation.get("session_id") != job["session_id"]):
            _fail("Codex 续跑代次链身份不符")
        observed = _native_checks(directory, generation, hashes, commands=contract["verification_commands"])
        if hashes[str(directory / generation["events_file"])] != generation.get("native_sha256"):
            _fail("Codex 原生事件指纹变化")
        if type(generation.get("output_file")) is not str:
            _fail("Codex 最终输出文件身份缺失")
        final = validate_report(_read_json(directory / generation["output_file"], directory, hashes))
        if hashes[str(directory / generation["output_file"])] != generation.get("output_sha256"):
            _fail("Codex 最终输出指纹变化")
        if index == len(job["turns"]):
            if final != report:
                _fail("Codex 最终输出与报告不一致")
            native = observed
            continue
        request_id = "codex-input:" + job["run_id"] + ":" + str(index)
        answer_path = directory / ("answer-" + util.sha256_text(request_id) + ".json")
        answer = _read_json(answer_path, directory, hashes)
        if (final["status"] != "needs_input" or generation.get("input_resolution") != "answered"
                or not generation.get("answer_consumed_at") or generation.get("answer_operation_id") != answer.get("operation_id")
                or generation.get("answer_sha256") != hashes[str(answer_path)]
                or any(answer.get(key) != value for key, value in (("scope_id", spec["scope_id"]), ("session_id", job["session_id"]), ("request_id", request_id), ("tool_call_id", "codex-question:" + str(index))))):
            _fail("Codex 续跑缺少绑定的显式回答消费证据")
        receipt = runtime_operations.get_operation(conn, spec["scope_id"], answer.get("operation_id"))
        if (receipt is None or receipt["harness_id"] != "codex" or receipt["tool"] != "codex_execution_input"
                or receipt["status"] != "accepted" or receipt["session_id"] != job["session_id"]
                or receipt["request_sha256"] != runtime_operations.compute_request_sha256(answer)):
            _fail("Codex 续跑回答操作收据不符")
    commands = declaration["commands"]
    declared = [{**check, "result": "passed" if check["exit_code"] == 0 else "failed", "provenance": "model_declared", "source_sha256": source} for check in report["checks"]]
    model_coverage = stage_evidence.match_checks(commands, declared, expected_source_sha256=source)
    native_coverage = stage_evidence.match_checks(commands, native, expected_source_sha256=source)
    # Preserve exact causes in coverage; both independent channels must pass.
    coverage = dict(native_coverage if not native_coverage["ok"] or model_coverage["ok"] else model_coverage)
    coverage["native"] = native_coverage
    coverage["model"] = model_coverage
    verification_recovery._require_unchanged(hashes)
    return {"hashes": hashes, "job": job, "report": {"state": "ready", "report_id": envelope["report_id"], "turn_id": job["turn_id"], "report_sha256": hashes[str(directory / REPORT_NAME)], "report": report}, "commands": commands, "coverage": coverage}


class InputClient:
    """Local answer/stop bridge. Reading never starts or resumes a process."""
    def __init__(self, conn, task, run_id, read_only=False):
        from . import headless_adapter
        self.conn, self.task, self.run_id, self.read_only = conn, task, run_id, read_only
        row = round_store.require_round(conn, task["task_id"], run_id)
        if task["run_id"] != run_id or row["backend"] != "codex":
            _fail("Codex 输入不是当前执行轮次", "conflict")
        self.directory = headless_adapter.resolve_source(conn).run_dir(task["task_id"], run_id)
        self.spec = _read_json(self.directory / "spec.json", self.directory)
        self.job = self._job()
        execution_contract.validate_spec(self.spec, job=self.job)
        if (not self.spec.get("stage_contract_required") or any(record.get("task_id") != task["task_id"] or record.get("run_id") != run_id or record.get("backend") != "codex" or record.get("config") != util.jload(row["config_json"]) for record in (self.spec, self.job))
                or self.spec.get("cwd") != row["worktree_path"] or self.spec.get("plan_sha256") != task["plan_sha256"]
                or self.job.get("spec_sha256") != util.sha256_file(self.directory / "spec.json")):
            _fail("Codex 输入的固化配置或运行身份不符", "conflict")
        self.scope_id = runtime_client.stable_scope_id("execution", task_id=task["task_id"], run_id=run_id)
        if self.spec.get("scope_id") != self.scope_id or self.job.get("scope_id") != self.scope_id:
            _fail("Codex 输入 scope 身份不符", "conflict")
        self.harness_pid = self.job.get("pid")

    def _job(self):
        job = _read_json(self.directory / "job.json", self.directory)
        if self.spec.get("task_id") != job.get("task_id") or self.spec.get("run_id") != job.get("run_id"):
            _fail("Codex 输入运行身份变化", "conflict")
        return job

    def _session(self, session_id):
        job = self._job()
        if not session_id or job.get("session_id") != session_id:
            _fail("Codex 输入线程身份不符", "conflict")
        return job

    def agent_status(self, session_id):
        job = self._session(session_id)
        live = _alive(job.get("pid"), job.get("worker_birth"))
        return {"sessionId": session_id, "isRunning": live and job.get("status") in ("running", "waiting_input"), "currentTurnId": job.get("turn_id"), "source": "codex_adapter"}

    def input_pending(self, session_id):
        job = self._session(session_id)
        pending = job.get("pending_input")
        return {"sessionId": session_id, "asks": [pending] if pending and job.get("status") == "waiting_input" else []}

    def turn_get(self, session_id, turn_id, require_identity=True):
        job = self._session(session_id)
        turn = next((row for row in job.get("turns", []) if row["turn_id"] == turn_id), None)
        if turn is None:
            _fail("Codex 适配器代次不存在", "conflict")
        state = turn["status"]
        if job.get("pending_input") and job["pending_input"]["turnId"] == turn_id:
            state = "running" if self.agent_status(session_id)["isRunning"] else "unknown"
        return {"sessionId": session_id, "turnId": turn_id, "status": state, "source": "codex_adapter", "inputResolution": turn.get("input_resolution"), "answerOperationId": turn.get("answer_operation_id"), "answerSha256": turn.get("answer_sha256"), "answerConsumedAt": turn.get("answer_consumed_at")}

    def input_respond(self, session_id, request_id, tool_call_id, answers, *, operation_id):
        from .execution_questions import validate_wire_answers
        if self.read_only:
            _fail("只读 Codex 输入客户端禁止写入", "forbidden")
        job = self._session(session_id)
        validate_wire_answers(answers, session_id, request_id, tool_call_id)
        pending = job.get("pending_input") or {}
        payload = {"scope_id": self.scope_id, "session_id": session_id, "request_id": request_id, "tool_call_id": tool_call_id, "answers": answers, "operation_id": operation_id}
        existing = runtime_operations.get_operation(self.conn, self.scope_id, operation_id)
        if existing is None and job.get("pending_input") and git_delivery.source_state_sha256(self.spec["cwd"]) != job.get("pending_source_sha256"):
            _fail("Codex 等待回答期间源码变化", "conflict")
        if existing is None and (not self.agent_status(session_id)["isRunning"] or job.get("status") != "waiting_input" or pending.get("requestId") != request_id or pending.get("toolCallId") != tool_call_id or len(answers) != len(pending.get("questions") or [])):
            _fail("Codex 问题不再等待回答或 worker 已失联", "conflict")
        return self._mailbox("codex_execution_input", operation_id, payload, "answer-" + util.sha256_text(request_id) + ".json", {"status": "answered", "session_id": session_id, "request_id": request_id})

    def _mailbox(self, tool, operation_id, payload, name, result):
        slot = runtime_operations.reserve_downstream(self.conn, self.scope_id, operation_id, "codex", tool, payload)
        if not slot["send"]:
            if slot["status"] == "accepted":
                return slot["result"]
            _fail("Codex 操作结果未知，禁止自动重发", "conflict")
        path = util.inside(self.directory / name, self.directory, "Codex mailbox")
        # Exclusive creation makes competing operations for one question fail closed.
        temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(util.jdump(payload)); stream.flush(); os.fsync(stream.fileno())
            # Atomic visibility without replacing an existing answer/stop request.
            os.link(temporary, path)
        except FileExistsError:
            _fail("Codex 操作信箱已存在，禁止覆盖", "conflict")
        finally:
            if temporary.exists():
                temporary.unlink()
        with db.write_txn(self.conn):
            runtime_operations.complete_operation(self.conn, self.scope_id, operation_id, result=result, session_id=payload["session_id"])
        return result

    def _write(self, tool, operation_id, args):
        if self.read_only:
            _fail("只读 Codex 输入客户端禁止停止", "forbidden")
        if tool not in ("kp_runtime_stop", "kp_runtime_abort"):
            _fail("Codex 不支持该控制操作", "forbidden")
        job = self._session(args.get("session_id"))
        payload = {"scope_id": self.scope_id, "session_id": job["session_id"], "operation_id": operation_id}
        return self._mailbox(tool, operation_id, payload, "stop-" + util.sha256_text(operation_id) + ".json", {"status": "accepted", "session_id": job["session_id"]})


def run_real(spec):
    from .codex_worker import run_real as worker
    return worker(spec)
