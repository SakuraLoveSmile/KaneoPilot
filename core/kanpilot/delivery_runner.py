"""Deterministic delivery stage: publish, observe, advance, then report."""

import os
import time

from . import util
from .auto_dispatch import Controller, StaleRun


def _delivery(controller):
    task_id = controller.claim["context"]["task_id"]
    task = controller.call("get-task", {"task_id": task_id})["task"]
    delivery = task.get("delivery") or (task.get("extra") or {}).get("delivery") or {}
    identity = dict(controller.identity(), task_id=task_id,
                    expected_execution_run_id=task["run_id"])
    try:
        if not delivery.get("pr_number") or delivery.get("pushed_sha") != delivery.get("head_sha"):
            result = controller.call("delivery-publish", identity)
            if result.get("outcome") in ("blocked", "retry"):
                _report(controller, result)
                return
            if result.get("outcome") != "published":
                raise util.KanpilotError("internal", "交付发布结果不可确认")
        result = controller.call("delivery-sync", identity)
        outcome = result.get("outcome")
        if outcome in ("merged", "kaneo_done"):
            controller.call("advance-task", dict(identity,
                expected_execution_run_id=task["run_id"], target="done",
                expected_current="done" if outcome == "kaneo_done" else "in-review"))
            controller.report("succeeded", progress="已合并并完成任务" if outcome == "merged" else "已同步 Kaneo 完成状态")
        else:
            _report(controller, result)
    except util.KanpilotError as exc:
        controller.report("failed", note=exc.code)


def _report(controller, result):
    if result.get("outcome") == "blocked":
        controller.report("failed", note="blocked:" + str(result.get("failure_code")))
    elif result.get("outcome") in ("retry", "waiting"):
        controller.report("succeeded", progress="等待下次交付复核" if result["outcome"] == "waiting" else "交付暂不可用，稍后重试")
    else:
        raise util.KanpilotError("internal", "交付复核结果不可确认")


def run_stage(stage, *, conn, db_path, timeout, log_path, record_run,
              resume=False, sleep=time.sleep, clock=time.monotonic) -> int:
    controller = Controller(stage, db_path, timeout, log_path, sleep, clock)
    record_run(pid=os.getpid(), started_at=util.now_iso(), log=str(log_path))
    exit_code = 0
    try:
        if stage != "delivery":
            raise ValueError("delivery stage required")
        while controller.claim_next():
            _delivery(controller)
    except StaleRun:
        pass
    except Exception as exc:
        controller.log("runner", exc.code if isinstance(exc, util.KanpilotError) else "internal")
        exit_code = 1
    finally:
        record_run(exit_code=exit_code, finished_at=util.now_iso())
    return exit_code
