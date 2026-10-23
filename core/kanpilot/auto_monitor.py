"""Deterministic execution observation and verified completion (§5.27.1)."""

from __future__ import annotations

import os
import time

from . import core, util, verify_evidence
from .auto_dispatch import Controller, StaleRun

_OLD_BACKENDS = ("dsh", "headless", "atif", "ATIF")
_GOAL_FAILURES = ("goal_contract_missing", "goal_approval_failed", "goal_execution_unstarted")


def _get_task(controller):
    return controller.call("get-task", {"task_id": controller.claim["context"]["task_id"]})["task"]


def _recheck(controller, task):
    # A14: the monitor stage runs unattended, so its recheck is an automation
    # action — not a user action (the panel's manual recheck keeps the default).
    return controller.call("recheck", {"task_id": task["task_id"],
                                       "actor": "automation"})


def _advance(controller, task, target, expected):
    # request_id would invoke review/delivery identity checks; monitor uses run_id only.
    return controller.call("advance-task", {"task_id": task["task_id"],
                           "run_id": controller.claim["run_id"],
                           "target": target, "expected_current": expected})


def _get_session(controller, task):
    return controller.call("get-session", {"task_id": task["task_id"], "run_id": task["run_id"]})


def _read_messages(controller, task):
    payload = {"task_id": task["task_id"], "run_id": task["run_id"], "limit": 200}
    messages, seen = [], set()
    while True:
        if controller.clock() >= controller.deadline:
            raise util.KanpilotError("observation_blocked", "消息读取超时")
        page = controller.call("session-messages", payload)
        session = page.get("session") or {}
        if session.get("run_id") not in (None, task["run_id"]):
            raise util.KanpilotError("execution_failed", "会话轮次身份不符")
        if session.get("state") not in (None, "associated"):
            raise util.KanpilotError("observation_blocked", "本轮消息暂不可读")
        for message in page.get("messages", []):
            if message.get("run_id") not in (None, task["run_id"]):
                raise util.KanpilotError("execution_failed", "消息轮次身份不符")
            messages.append(message)
        if not page.get("has_more"):
            return messages
        cursor = page.get("next_cursor")
        if not cursor or cursor in seen:
            raise util.KanpilotError("observation_blocked", "消息分页游标未前进")
        seen.add(cursor)
        payload["cursor"] = cursor


def _observe(controller, task, detail):
    if detail.get("observer_alive") is True or detail.get("backend") not in ("pi_desktop", "codex") or (detail.get("observer_alive") is not False
            and not str(detail.get("detail") or "").startswith("heartbeat_failed:")):
        return None
    automation = (task.get("extra") or {}).get("automation") or {}
    return controller.call("round-observe", {"task_id": task["task_id"],
                           "run_id": task["run_id"], "executor": "kaneo-monitor",
                           "continue_goal": detail.get("backend") == "pi_desktop" and bool(automation) and not automation.get("manual_takeover", False)})


def _verification(task, messages, execution_turn_id=None, evidence_version=None):
    extra = task.get("extra") or {}
    if (extra.get("verification_override") or {}).get("execution_run_id") == task["run_id"]:
        return {"ok": True}
    declaration = extra.get("plan_verify") or {}
    if declaration.get("plan_sha256") != task.get("plan_sha256") or not declaration.get("commands"):
        return {"ok": False, "note": declaration.get("error") or "计划缺少可信 verify 命令声明"}
    if evidence_version == 1:
        result = verify_evidence.match_execution_verification(declaration["commands"], messages, execution_turn_id)
    else:
        messages, window = verify_evidence.execution_window(messages, execution_turn_id)
        if window["window_basis"] == "unscoped_goal_window":
            return dict(window, ok=False, note="Goal 执行证据窗口无法确认：缺少可信执行轮次或起点（whole_round）")
        result = verify_evidence.match_verification(declaration["commands"],
                                                   verify_evidence.extract_command_results(messages))
        result.update(window)
    result["note"] = "；".join(["缺失：" + command for command in result["missing"]] + [
        ("证据无法确认：" + item["command"] + "（缺少独立退出码或命令组合不受支持）"
         if item["exit_code"] is None else
         "失败：" + item["command"] + "（exit_code=" + str(item["exit_code"]) + "）")
        for item in result["failed"]])
    return result


def _commit(controller, task):
    return controller.call("delivery-commit", dict(controller.identity(), task_id=task["task_id"],
                           expected_execution_run_id=task["run_id"]))


def _record_cost(controller, task, detail):
    if detail.get("backend") in ("pi_desktop", "codex"):
        # Deduplicate by execution identity, so repeated monitor claims cost once.
        execution = next((attempt for attempt in task.get("attempts", [])
                          if attempt.get("stage") == "dispatch" and attempt.get("run_id") == task["run_id"]), None)
        if execution is None:
            raise util.KanpilotError("observation_blocked", "本轮派发成本身份缺失")
        identity = {"request_id": execution["request_id"], "run_id": task["run_id"]}
        controller.call("record-cost", dict(identity, task_id=task["task_id"], stage="dispatch",
                                            cost_usd=None, source=detail["backend"]))


def _complete(controller, task, detail, expected, messages):
    mode = _engine_mode(controller, task)
    action = core.review_repair_of(task).get("source_kind") in ("merge_conflict", "build_failure")
    codex = detail.get("backend") == "codex"
    strict = mode == "live" or action or codex
    if mode in ("live", "shadow") or action or codex:
        lease = controller.claim["context"].get("lease_seconds", 180)
        budget = int(min(lease * 0.8, controller.deadline - controller.clock()))
        if mode == "shadow" and not strict:
            budget = min(budget, 30, int((controller.deadline - controller.clock()) / 2))
        if budget <= 0 and strict:
            raise util.KanpilotError("observation_blocked", "执行证据核验剩余时限不足")
        if budget <= 0:
            controller.log("shadow-verdict", "budget_exhausted")
            result = {"verification_ok": False}
        else:
            controller.heartbeat(force=True)
            try:
                result = controller.call("engine-verdict", dict(controller.identity(),
                    task_id=task["task_id"], expected_execution_run_id=task["run_id"],
                    expected_plan_sha256=task["plan_sha256"], budget_seconds=budget,
                    operation_id="monitor-verdict:" + controller.claim["request_id"]))
            except util.KanpilotError as exc:
                if strict or exc.code in ("stale_run", "not_found"):
                    raise
                controller.log("shadow-verdict", exc.code)
                result = {"verification_ok": False}
        if strict:
            if result.get("verification_ok") is not True:
                code = (result.get("failure_code") or
                        ("verification_environment_failure" if result.get("environment_failure") else "verification_unconfirmed"))
                controller.report("needs_user", failure_code=code,
                                  note=result.get("detail") or "执行验证证据未通过核验")
                return
            _finish_complete(controller, task, detail, expected)
            return
    verification = _verification(task, messages, detail.get("turn_id"), detail.get("verification_evidence_version"))
    if not verification["ok"]:
        controller.report("needs_user", failure_code="verification_unconfirmed", note=verification["note"])
        return
    _finish_complete(controller, task, detail, expected)


def _engine_mode(controller, task):
    mappings = controller.call("get-project-mappings", {})
    entry = next((item for item in mappings.get("items", [])
                  if item.get("project_id") == task.get("project_id")), {})
    return entry.get("effective_engine_mode") or entry.get("engine_mode", "legacy")


def _finish_complete(controller, task, detail, expected):
    if core.requires_branch_review(task):
        commit = _commit(controller, task)
        if commit.get("outcome") == "blocked":
            controller.report("failed", note="blocked:" + str(commit.get("failure_code")))
            return
        if commit.get("outcome") not in ("committed", "already_committed"):
            raise util.KanpilotError("observation_blocked", "交付提交结果不可确认")
    _advance(controller, task, "in-review", expected)
    _record_cost(controller, task, detail)
    controller.report("succeeded", progress="本轮执行完成且验证通过")


def _running(controller, progress="本轮仍在运行"):
    controller.heartbeat(force=True)
    controller.report("succeeded", progress=progress)


def _monitor(controller):
    task = _get_task(controller)
    try:
        recheck = _recheck(controller, task)
        remote_status = recheck.get("kaneo_status")
        automation = (task.get("extra") or {}).get("automation") or {}
        managed = bool(automation) and not automation.get("manual_takeover", False)
        if remote_status == "done" or (remote_status == "in-review" and not managed
                                       and not core.requires_branch_review(task)):
            _advance(controller, task, remote_status, remote_status)
            controller.report("succeeded", progress="已同步远端状态")
            return
        expected = "in-review" if remote_status == "in-review" else "in-progress"
        session = _get_session(controller, task)
        detail = session.get("round_detail")
        backend = (detail or {}).get("backend") or (task.get("execution_round") or {}).get("backend")
        if backend in _OLD_BACKENDS:
            controller.report("needs_user", failure_code="observation_blocked", note="旧后端轮次无法观察，请人工判断")
            return
        if not task.get("run_id") or not isinstance(detail, dict):
            controller.report("failed", note="observation_blocked")
            return
        if (session.get("session") or {}).get("run_id") not in (None, task["run_id"]):
            raise util.KanpilotError("execution_failed", "执行会话身份不符")
        observed = _observe(controller, task, detail)
        if observed:
            if observed.get("reopened") and observed.get("status") == "observing":
                _running(controller, "观察者已续接，本轮仍在运行")
                return
            if observed.get("status") in ("deferred", "exited", "starting"):
                controller.report("failed", note="observation_blocked")
                return
            if observed.get("status") in ("observing", "finished", "settled", "finalizing"):
                detail = _get_session(controller, task).get("round_detail")
                if not isinstance(detail, dict):
                    controller.report("failed", note="observation_blocked")
                    return
            else:
                controller.report("failed", note="observation_blocked")
                return
        if backend == "codex":
            # Reconcile durable answer consumption and stop intents, including
            # ordinary stops without an open question. This never resumes CLI.
            reconciled = controller.call("reconcile-execution-question", {
                "task_id": task["task_id"], "run_id": task["run_id"]})
            if reconciled.get("status") == "stopped":
                controller.report("needs_user", failure_code="execution_failed", note="Codex 执行已停止")
                return
        if detail.get("observation_finalizing") is True and detail.get("observer_alive") is True:
            _running(controller, "执行已完成，正在收尾")
            return
        terminal = detail.get("terminal") or {}
        terminal_detail = "；".join(str(value) for value in
                                   (terminal.get("detail"), terminal.get("reason"), detail.get("detail")) if value)
        outcome = terminal.get("outcome")
        failure = (any(code in terminal_detail for code in _GOAL_FAILURES)
                   or outcome in ("failed", "error", "aborted", "timeout", "truncated")
                   or detail.get("run_status") in ("failed", "timeout", "truncated")
                   or detail.get("send_state") == "unknown"
                   or any(marker in terminal_detail for marker in
                          ("transcript_incomplete", "turn_identity_mismatch", "identity_mismatch", "截断", "超时"))
                   or (type(detail.get("exit_code")) is int and detail["exit_code"] != 0)
                   or terminal.get("truncated") is True or detail.get("truncated") is True)
        if failure:
            controller.report("needs_user", failure_code="execution_failed", note=terminal_detail or "执行轮次明确失败")
            return
        messages = [] if backend == "codex" else _read_messages(controller, task)
        if outcome == "completed" and detail.get("run_status") == "finished":
            if type(detail.get("exit_code")) is not int or detail["exit_code"] != 0:
                controller.report("needs_user", failure_code="execution_failed", note="执行退出码不可确认")
            else:
                _complete(controller, task, detail, expected, messages)
        else:
            _running(controller)
    except util.KanpilotError as exc:
        if exc.code in ("execution_failed", "verification_identity_unconfirmed"):
            controller.report("needs_user", failure_code=exc.code, note=exc.message)
        else:
            controller.report("failed", note=exc.code if exc.code in ("remote_conflict", "remote_unavailable") else "observation_blocked")


def run_stage(stage, *, conn, db_path, timeout, log_path, record_run,
              resume=False, sleep=time.sleep, clock=time.monotonic) -> int:
    controller = Controller(stage, db_path, timeout, log_path, sleep, clock)
    record_run(pid=os.getpid(), started_at=util.now_iso(), log=str(log_path))
    exit_code = 0
    try:
        if stage != "monitor":
            raise ValueError("monitor stage required")
        while controller.claim_next():
            _monitor(controller)
    except StaleRun:
        pass
    except Exception as exc:
        controller.log("runner", exc.code if isinstance(exc, util.KanpilotError) else "internal")
        exit_code = 1
    finally:
        record_run(exit_code=exit_code, finished_at=util.now_iso())
    return exit_code
