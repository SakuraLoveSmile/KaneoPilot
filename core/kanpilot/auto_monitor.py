"""Deterministic execution observation and verified completion (§5.27.1)."""

from __future__ import annotations

import os
import time

from . import util, verify_evidence
from .auto_dispatch import Controller, StaleRun

_OLD_BACKENDS = ("dsh", "headless", "atif", "ATIF")
_GOAL_FAILURES = ("goal_contract_missing", "goal_approval_failed", "goal_execution_unstarted")
_READ_ONLY_TOOLS = frozenset(("UpdateGoalProgress", "Glob", "Read"))
_SHORT_REPLY_CODEPOINTS = 16


def _get_task(controller):
    return controller.call("get-task", {"task_id": controller.claim["context"]["task_id"]})["task"]


def _recheck(controller, task):
    return controller.call("recheck", {"task_id": task["task_id"]})


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
    if detail.get("observer_alive") is True or detail.get("backend") != "pi_desktop" or (detail.get("observer_alive") is not False
            and not str(detail.get("detail") or "").startswith("heartbeat_failed:")):
        return None
    return controller.call("round-observe", {"task_id": task["task_id"],
                           "run_id": task["run_id"], "executor": "kaneo-monitor"})


def _verification(task, messages, window_metadata=None):
    if window_metadata is None:
        messages, window_metadata = verify_evidence.execution_window(messages)
    extra = task.get("extra") or {}
    if (extra.get("verification_override") or {}).get("execution_run_id") == task["run_id"]:
        return dict(window_metadata, ok=True)
    declaration = extra.get("plan_verify") or {}
    if declaration.get("plan_sha256") != task.get("plan_sha256") or not declaration.get("commands"):
        return dict(window_metadata, ok=False, note=declaration.get("error") or "计划缺少可信 verify 命令声明")
    result = verify_evidence.match_verification(declaration["commands"],
                                               verify_evidence.extract_command_results(messages))
    result["note"] = "；".join(["缺失：" + command for command in result["missing"]] + [
        "失败：" + item["command"] + "（exit_code=" + str(item["exit_code"]) + "）"
        for item in result["failed"]])
    result.update(window_metadata)
    return result


def _noop_evidence(controller, task, detail, messages, window_metadata):
    automation = (task.get("extra") or {}).get("automation") or {}
    backend = detail.get("backend") or (task.get("execution_round") or {}).get("backend")
    if not automation or automation.get("manual_takeover") or backend != "pi_desktop":
        return None
    delivery = ((task.get("extra") or {}).get("delivery") or {})
    if any(item.get("execution_run_id") == task["run_id"] for item in delivery.get("commits") or []):
        # A monitor can resume after delivery-commit but before advance-task.
        # The already registered delivery proves this execution did act.
        return None
    tool_names, final_reply = [], ""
    for message in messages:
        raw = message.get("raw") or {}
        kind = message.get("type", message.get("kind", raw.get("type")))
        if kind in ("tool_call", "tool_result", "command_output"):
            tool = message.get("tool") or raw.get("tool") or {}
            tool_names.append(tool.get("name") or message.get("name"))
        elif kind in ("assistant", "assistant_message"):
            final_reply = message.get("content") if isinstance(message.get("content"), str) else ""
    signals = []
    evidence = dict(window_metadata, tool_event_count=len(tool_names),
                    action_tool_event_count=sum(name not in _READ_ONLY_TOOLS for name in tool_names),
                    reply_length=len(final_reply.strip()))
    if ((task.get("extra") or {}).get("delivery") or {}).get("mode") == "pr":
        activity = controller.call("execution-activity", {"task_id": task["task_id"], "run_id": task["run_id"]})
        count = activity.get("effective_change_count")
        if type(count) is not int or count < 0:
            raise util.KanpilotError("observation_blocked", "执行改动数不可确认")
        if activity.get("delivery_recovered") is True:
            return None
        evidence["effective_change_count"] = count
        if count == 0:
            signals.append("no_effective_changes")
    if (window_metadata.get("window_basis") != "unscoped_goal_window"
            and not window_metadata.get("reply_unconfirmed")
            and evidence["reply_length"] < _SHORT_REPLY_CODEPOINTS
            and evidence["action_tool_event_count"] == 0):
        signals.append("short_reply_read_only")
    return dict(evidence, signals=signals) if signals else None


def _commit(controller, task):
    return controller.call("delivery-commit", dict(controller.identity(), task_id=task["task_id"],
                           expected_execution_run_id=task["run_id"]))


def _record_cost(controller, task, detail):
    if detail.get("backend") == "pi_desktop":
        # Deduplicate by execution identity, so repeated monitor claims cost once.
        execution = next((attempt for attempt in task.get("attempts", [])
                          if attempt.get("stage") == "dispatch" and attempt.get("run_id") == task["run_id"]), None)
        if execution is None:
            raise util.KanpilotError("observation_blocked", "本轮派发成本身份缺失")
        identity = {"request_id": execution["request_id"], "run_id": task["run_id"]}
        controller.call("record-cost", dict(identity, task_id=task["task_id"], stage="dispatch",
                                            cost_usd=None, source="pi_desktop"))


def _complete(controller, task, detail, expected, messages):
    messages, window_metadata = verify_evidence.execution_window(messages, detail.get("turn_id"))
    noop = _noop_evidence(controller, task, detail, messages, window_metadata)
    if noop:
        controller.report("needs_user", failure_code="execution_noop",
                          note="执行轮次结束但未产生实际执行活动", evidence=noop)
        return
    verification = _verification(task, messages, window_metadata)
    if not verification["ok"]:
        controller.report("needs_user", failure_code="verification_unconfirmed", note=verification["note"],
                          evidence=window_metadata)
        return
    if ((task.get("extra") or {}).get("delivery") or {}).get("mode") == "pr":
        commit = _commit(controller, task)
        if commit.get("outcome") == "blocked":
            controller.report("failed", note="blocked:" + str(commit.get("failure_code")))
            return
        if commit.get("outcome") not in ("committed", "already_committed"):
            raise util.KanpilotError("observation_blocked", "交付提交结果不可确认")
    _advance(controller, task, "in-review", expected)
    _record_cost(controller, task, detail)
    controller.report("succeeded", progress="本轮执行完成且验证通过", evidence=window_metadata)


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
        if remote_status == "done" or (remote_status == "in-review" and not managed):
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
            if observed.get("reopened"):
                _running(controller, "观察者已续接，本轮仍在运行")
                return
            if observed.get("status") in ("deferred", "exited", "starting"):
                controller.report("failed", note="observation_blocked")
                return
            if observed.get("status") in ("observing", "finished", "settled"):
                detail = _get_session(controller, task).get("round_detail")
                if not isinstance(detail, dict):
                    controller.report("failed", note="observation_blocked")
                    return
            else:
                controller.report("failed", note="observation_blocked")
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
        messages = _read_messages(controller, task)
        if outcome == "completed" and detail.get("run_status") == "finished":
            if type(detail.get("exit_code")) is not int or detail["exit_code"] != 0:
                controller.report("needs_user", failure_code="execution_failed", note="执行退出码不可确认")
            else:
                _complete(controller, task, detail, expected, messages)
        else:
            _running(controller)
    except util.KanpilotError as exc:
        if exc.code == "execution_failed":
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
