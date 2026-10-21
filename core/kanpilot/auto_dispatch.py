"""Deterministic dispatch orchestration; all core access goes through cli.run."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import cli, util


class StaleRun(Exception):
    """Ownership was lost: stop without further core calls."""


class Controller:
    """Shared command/log/lease boundary for deterministic control stages."""

    def __init__(self, stage, db_path, timeout, log_path, sleep, clock):
        self.stage = stage
        self.db_path = db_path
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.sleep, self.clock = sleep, clock
        self.deadline = clock() + timeout
        self.claim = None
        self.last_heartbeat = clock()
        self.heartbeat_interval = 60.0

    def log(self, step, code):
        context = (self.claim or {}).get("context") or {}
        entry = {"time": util.now_iso(), "step": step,
                 "task_id": context.get("task_id"),
                 "request_id": (self.claim or {}).get("request_id"), "code": code}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def call(self, command, payload):
        self.heartbeat()
        return self._call(command, payload)

    def _call(self, command, payload):
        try:
            result = cli.run(command, payload, self.db_path)
        except util.KanpilotError as exc:
            self.log(command, exc.code)
            if exc.code in ("stale_run", "not_found"):
                raise StaleRun from exc
            raise
        self.log(command, result.get("code") or result.get("status") or "ok")
        if result.get("code") in ("stale_run", "not_found"):
            raise StaleRun
        return result

    def identity(self):
        return {key: self.claim[key] for key in ("request_id", "run_id")}

    def task_identity(self):
        return {"task_id": self.claim["context"]["task_id"], "run_id": self.claim["run_id"]}

    def heartbeat(self, force=False):
        if self.claim and (force or self.clock() - self.last_heartbeat >= self.heartbeat_interval):
            self._call("heartbeat", self.identity())
            self.last_heartbeat = self.clock()

    def wait(self, seconds):
        self.heartbeat()
        self.sleep(min(seconds, self.heartbeat_interval))
        self.heartbeat()

    def claim_next(self):
        self.claim = None
        if self.deadline - self.clock() < 120:
            return False
        result = self._call("claim", {"stage": self.stage, "executor": "kaneo-" + self.stage})
        if not result.get("claimed"):
            return False
        self.claim = result
        self.last_heartbeat = self.clock()
        lease = result["context"].get("lease_seconds", 180)
        self.heartbeat_interval = min(60.0, lease / 3.0)
        return True

    def report(self, outcome, **fields):
        return self.call("report", dict(self.identity(), outcome=outcome, **fields))


def _prepare(controller, evidence):
    return controller.call("dispatch-prepare", dict(controller.task_identity(), evidence=evidence))


def _begin_send(controller):
    return controller.call("dispatch-begin-send", controller.task_identity())


def _start(controller):
    return controller.call("dispatch-start", controller.task_identity())


def _wait_session(controller, result):
    if result.get("session_id"):
        return result["session_id"]
    if result.get("status") != "starting" and not result.get("deduplicated"):
        raise util.KanpilotError("validation_failed", "worker 会话未建立")
    deadline = min(controller.deadline, controller.clock() + 300)
    while controller.clock() < deadline:
        controller.wait(min(30, deadline - controller.clock()))
        session = controller.call("get-session", controller.task_identity()).get("session") or {}
        if session.get("session_id"):
            return session["session_id"]
    raise util.KanpilotError("validation_failed", "worker 会话未建立")


def _confirm(controller, evidence, session_id):
    evidence = dict(evidence, session_id=session_id, protocol_seen=True)
    if evidence["backend"] in ("pi_desktop", "codex"):
        observed = controller.call("get-session", controller.task_identity())
        session = observed.get("session") or {}
        detail = observed.get("round_detail") or {}
        # Goal approval can already have replaced the current turn. Confirm
        # against the immutable startup session event, not the execution turn.
        turn_id = detail.get("startup_turn_id") if "startup_turn_id" in detail else detail.get("turn_id")
        if (session.get("task_id") != controller.task_identity()["task_id"]
                or session.get("run_id") != controller.claim["run_id"]
                or session.get("session_id") != session_id
                or not isinstance(turn_id, str) or not turn_id):
            raise util.KanpilotError("evidence_invalid", "worker 会话轮次身份未建立")
        evidence["turn_id"] = turn_id
    return controller.call("dispatch-confirm", dict(controller.task_identity(), evidence=dict(
        evidence)))


def _fail(controller, send_state, reason):
    return controller.call("dispatch-fail", dict(controller.task_identity(),
                          send_state=send_state, reason=reason))


def _dispatch(controller):
    context = controller.claim["context"]
    if context.get("backend") not in ("pi", "pi_desktop", "codex"):
        controller.log("unsupported_backend", "unsupported_backend")
        return False
    evidence = {key: context[key] for key in ("session_marker", "repository", "backend")}
    evidence["config"] = context["exec_config"]
    send_state = "not_sent"
    try:
        _prepare(controller, evidence)
        send_state = "unknown"  # begin-send itself can fail after its remote write.
        _begin_send(controller)
        session_id = _wait_session(controller, _start(controller))
        _confirm(controller, evidence, session_id)
    except util.KanpilotError as exc:
        _fail(controller, send_state, exc.code + ":" + exc.message)
    return True


def run_stage(stage, *, conn, db_path, timeout, log_path, record_run,
              resume=False, sleep=time.sleep, clock=time.monotonic) -> int:
    controller = Controller(stage, db_path, timeout, log_path, sleep, clock)
    record_run(pid=os.getpid(), started_at=util.now_iso(), log=str(log_path))
    exit_code = 0
    try:
        if stage != "dispatch":
            raise ValueError("dispatch stage required")
        while controller.claim_next():
            if not _dispatch(controller):
                break
    except StaleRun:
        pass
    except Exception as exc:
        controller.log("runner", exc.code if isinstance(exc, util.KanpilotError) else "internal")
        exit_code = 1
    finally:
        record_run(exit_code=exit_code, finished_at=util.now_iso())
    return exit_code
