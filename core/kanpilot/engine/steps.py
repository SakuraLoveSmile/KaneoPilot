"""Map a :class:`~kanpilot.engine.decide.Decision` to core-command calls.

Pure (dict/Decision in → list of :class:`CommandCall` out); the reconciler
(:mod:`kanpilot.engine.loop`) evaluates the gates (existing request, hold) and
executes the calls through ``cli.run`` so every write still goes through the
core command surface (SQLite stays the single source of truth).

Model-backed steps (planning / Plus execution / review) are *not* run here —
the engine enqueues the request and the existing per-stage runner
(``executor_runner`` / ``planner_runner`` / ``pi_desktop_worker``) claims and
executes it. This module only schedules the deterministic forward progress.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import decide


@dataclass(frozen=True)
class CommandCall:
    """A core command the reconciler may run for a task.

    ``kind`` is the request kind to dedup against (skip if one is already
    queued/claimed). ``requires_hold`` gates confirm-style calls so an
    already-confirmed task is not re-confirmed.
    """

    command: str
    payload: dict
    kind: Optional[str] = None
    requires_hold: bool = False


def commands_for(decision: decide.Decision, task: dict) -> List[CommandCall]:
    """Forward intents → core-command calls (empty for await/block/noop/retry)."""
    action = decision.action
    task_id = task.get("task_id")
    plan_sha = task.get("plan_sha256")

    if action == decide.CONFIRM_PLAN:
        return [CommandCall(
            "enqueue",
            {"task_id": task_id, "kind": "dispatch", "confirm_plan": True,
             "expected_plan_sha256": plan_sha, "actor": "automation"},
            kind="dispatch", requires_hold=True,
        )]
    if action == decide.ENQUEUE_DISPATCH:
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "dispatch",
                                        "actor": "automation"},
                            kind="dispatch")]
    if action == decide.RUN_VERDICT:
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "monitor",
                                        "actor": "automation"},
                            kind="monitor")]
    if action in (decide.COMMIT_DELIVERY, decide.SYNC_DELIVERY):
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "delivery",
                                        "actor": "automation"},
                            kind="delivery")]
    if action == decide.ENQUEUE_REVIEW:
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "review",
                                        "actor": "automation"},
                            kind="review")]
    if action == decide.RETRY:
        # Ladder-1 transient retry: re-enqueue the same request kind with an
        # exponential-backoff not_before (≤2/stage; over-limit escalates to
        # ladder 2 before reaching here, so no re-enqueue). Dispatch is rebuilt
        # only when not yet sent — a sent/unknown prompt is never re-sent.
        kind = decision.params.get("kind")
        not_before = decision.params.get("not_before")
        if not kind:
            return []
        if kind == "dispatch" and task.get("send_state") in ("sent", "unknown"):
            return []
        # retry_base 随 payload 传递「本阶段已花费的重试次数」：重入队的是**新**
        # 请求（attempt_no 归零），不传递的话 _transient_retry 每次都只看到 0，
        # 永远达不到 state-model §8 的 ≤2/阶段 上限（无限退避重试）。
        # 它同时是 cmd_enqueue 放行 dispatching 状态退避重入队的凭据。
        return [CommandCall("enqueue",
                            {"task_id": task_id, "kind": kind,
                             "not_before": not_before, "actor": "automation",
                             "retry_base": decision.params.get("attempt_no")},
                            kind=kind)]
    # AWAIT / BLOCK / NOOP / ENQUEUE_PLAN / ADVANCE_INTAKE / ADVANCE_DISPATCH /
    # RESUME_OBSERVE: no direct scheduling command here (worker-owned,
    # user-owned, or delegated to intake/planner). START_FIX_ROUND is decided
    # and recorded by ``loop._quality_retry`` (model choice + one-shot override);
    # the re-execution itself is authorized by the existing resolve/replan path,
    # so there is no enqueue here — an engine-driven re-send would need a
    # ``ready`` task plus an unsent prompt (``CLAIM_GUARDS`` / dispatch guard).
    return []
