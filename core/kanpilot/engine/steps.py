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
             "expected_plan_sha256": plan_sha},
            kind="dispatch", requires_hold=True,
        )]
    if action == decide.ENQUEUE_DISPATCH:
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "dispatch"},
                            kind="dispatch")]
    if action == decide.RUN_VERDICT:
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "monitor"},
                            kind="monitor")]
    if action in (decide.COMMIT_DELIVERY, decide.SYNC_DELIVERY):
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "delivery"},
                            kind="delivery")]
    if action == decide.ENQUEUE_REVIEW:
        return [CommandCall("enqueue", {"task_id": task_id, "kind": "review"},
                            kind="review")]
    # AWAIT / BLOCK / NOOP / RETRY / ENQUEUE_PLAN / ADVANCE_INTAKE /
    # ADVANCE_DISPATCH / RESUME_OBSERVE / START_FIX_ROUND: no direct scheduling
    # command here (worker-owned, user-owned, or delegated to intake/planner).
    return []
