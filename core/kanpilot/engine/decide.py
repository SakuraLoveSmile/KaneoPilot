"""Pure, table-driven reconciler decision (state-model §7–§8, protocol §5.27).

No DB, no I/O, no clock — :func:`decide` maps a plain *snapshot* dict to a
:class:`Decision` (an intent the reconciler in :mod:`kanpilot.engine.loop`
turns into core-command writes + worker launches). Every
``(phase, condition)`` row and every failure ladder row is covered by a unit
test in ``core/tests/test_engine_decide.py`` ("每行一条").

Two tables drive everything:

1. ``_PHASE_ACTION`` — steady state: ``(phase, condition)`` → next intent.
2. ``failure_decision`` — after a step fails: failure class (ladder) →
   retry-with-backoff / bounded fix-round / block-to-attention.

The golden path (state-model §9) is intake → plan → confirm → dispatch →
observe → verdict → commit → review ⇄ fix(≤2) → PR → merge-sync → done; each
phase only advances forward or escalates to needs-you.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from . import ladder

# ---------------------------------------------------------------------------
# Decision intents. The reconciler interprets these; they carry no side effect.
# ---------------------------------------------------------------------------
NOOP = "noop"                       # nothing to do (terminal / not ours)
AWAIT = "await"                     # a worker owns the step; just wait
BLOCK = "block"                     # needs-you: surface to the attention inbox
RETRY = "retry"                     # transient: requeue same stage with backoff
ADVANCE_INTAKE = "advance_intake"   # adopt a ready project's To Do
ENQUEUE_PLAN = "enqueue_plan"       # (re)plan — plan_wanted
CONFIRM_PLAN = "confirm_plan"       # release plan_execution_hold → dispatch
ENQUEUE_DISPATCH = "enqueue_dispatch"
ADVANCE_DISPATCH = "advance_dispatch"   # prepare → begin-send → start → confirm
RESUME_OBSERVE = "resume_observe"       # read-only reattach to a running round
RUN_VERDICT = "run_verdict"             # core-owned verify (diff + verify cmds)
COMMIT_DELIVERY = "commit_delivery"     # delivery-commit
ENQUEUE_REVIEW = "enqueue_review"
START_FIX_ROUND = "start_fix_round"     # same-branch bounded remediation
SYNC_DELIVERY = "sync_delivery"         # delivery-sync (poll merge)
SETTLE_ROUND = "settle_round"           # round exited but terminal state unrecorded

# Forward actions that must yield to the per-task budget gate
# (automation-contract §计划自动确认 / §同任务修复轮).
_BUDGET_GATED = frozenset({CONFIRM_PLAN, START_FIX_ROUND})
# Forward actions that consume an active-task slot (max_active_tasks).
# plan_wanted replans are deliberately *not* capacity-gated (scenarios #18).
_CAPACITY_GATED = frozenset({ENQUEUE_DISPATCH, ADVANCE_INTAKE})

# (phase, condition) → next intent (state-model §7 phase×condition).
_PHASE_ACTION = {
    ("intake", "idle"): NOOP,
    ("intake", "pending"): ADVANCE_INTAKE,
    ("plan", "wanted"): ENQUEUE_PLAN,
    ("plan", "drafting"): AWAIT,
    ("plan", "ready"): CONFIRM_PLAN,
    ("dispatch", "confirmed"): ENQUEUE_DISPATCH,
    ("dispatch", "submitting"): ADVANCE_DISPATCH,
    ("observe", "running"): AWAIT,
    ("observe", "observing"): RESUME_OBSERVE,
    ("verdict", "checking"): RUN_VERDICT,
    ("verdict", "unconfirmed"): RUN_VERDICT,
    ("commit", "committing"): COMMIT_DELIVERY,
    ("review", "reviewing"): ENQUEUE_REVIEW,
    ("review", "repairing"): START_FIX_ROUND,
    ("delivery", "syncing"): SYNC_DELIVERY,
    ("done", "merged"): NOOP,
    ("needs-you", "blocked"): BLOCK,
}

# task.status → (phase, condition) fallback (state-model §7 mapping). The
# engine may store a finer (phase, condition) in extra.engine and pass it in.
_STATUS_PHASE_CONDITION = {
    "needs_plan": ("plan", "wanted"),
    "planning": ("plan", "drafting"),
    "ready": ("plan", "ready"),
    "dispatching": ("dispatch", "confirmed"),
    "prepared": ("dispatch", "submitting"),
    "submitting": ("dispatch", "submitting"),
    "running": ("observe", "running"),
    "in_review": ("review", "reviewing"),
    "needs_user": ("needs-you", "blocked"),
    "done": ("done", "merged"),
}


@dataclass(frozen=True)
class Decision:
    """A pure intent. ``params`` carries per-action data for the reconciler."""

    action: str
    reason: str = ""
    ladder: int = 0
    attention: bool = False
    params: dict = field(default_factory=dict)

    @property
    def is_block(self) -> bool:
        return self.action == BLOCK

    @property
    def is_noop(self) -> bool:
        return self.action == NOOP


def phase_condition(
    status: Optional[str],
    *,
    phase: Optional[str] = None,
    condition: Optional[str] = None,
    fix_round: int = 0,
    fix_open: bool = False,
) -> Optional[Tuple[str, str]]:
    """Resolve ``(phase, condition)`` (state-model §7).

    An explicit ``phase``/``condition`` (engine-tracked in extra.engine) wins;
    otherwise derive from ``task.status``. ``in_review`` maps to
    ``review/repairing`` while a same-task fix round is open. Returns ``None``
    for an unrecognised status.
    """
    if phase is not None:
        return (phase, condition)
    if status == "in_review" and (fix_open or fix_round > 0):
        return ("review", "repairing")
    return _STATUS_PHASE_CONDITION.get(status)


def base_action(phase: str, condition: str) -> Optional[str]:
    """Table lookup for the steady-state intent of a (phase, condition)."""
    return _PHASE_ACTION.get((phase, condition))


def decide(snapshot: dict) -> Decision:
    """Map a task snapshot to the next reconciler intent (pure).

    Expected snapshot keys (all optional unless noted):
      status, send_state, phase, condition, fix_round, fix_open,
      automation_managed, manual_takeover, paused,
      budget_ok, capacity_ok, failure_code, failure_stage, failure_reason.
    """
    # The engine only drives automation-managed tasks; manual take-over and
    # unmanaged rows are left to the legacy/manual path.
    if not snapshot.get("automation_managed") or snapshot.get("manual_takeover"):
        return Decision(NOOP, reason="not_automation_managed")
    # Pause stops new progress but never cancels a running round (contract).
    if snapshot.get("paused"):
        return Decision(AWAIT, reason="paused")

    resolved = phase_condition(
        snapshot.get("status"),
        phase=snapshot.get("phase"),
        condition=snapshot.get("condition"),
        fix_round=int(snapshot.get("fix_round") or 0),
        fix_open=bool(snapshot.get("fix_open")),
    )
    if resolved is None:
        return Decision(NOOP, reason="unmapped_status",
                        params={"status": snapshot.get("status")})
    phase, condition = resolved

    if snapshot.get("status") == "needs_user" or (phase, condition) == ("needs-you", "blocked"):
        code = snapshot.get("failure_code")
        return Decision(
            BLOCK, reason=code or "needs_user", ladder=ladder.classify(code),
            attention=True,
            params={"failure_code": code, "failure_stage": snapshot.get("failure_stage")},
        )

    action = base_action(phase, condition)
    if action is None:
        return Decision(NOOP, reason="unmapped_phase_condition",
                        params={"phase": phase, "condition": condition})

    if action in _BUDGET_GATED and not snapshot.get("budget_ok", True):
        return Decision(BLOCK, reason="auto_budget_exceeded",
                        ladder=ladder.LADDER_NEEDS_YOU, attention=True,
                        params={"phase": phase, "condition": condition})
    if action in _CAPACITY_GATED and not snapshot.get("capacity_ok", True):
        return Decision(AWAIT, reason="capacity_full",
                        params={"phase": phase, "condition": condition})

    return Decision(action, params={"phase": phase, "condition": condition})


def failure_decision(
    failure_code: Optional[str],
    *,
    attempt_no: int = 0,
    fix_round: int = 0,
    budget_ok: bool = True,
    fallback_model: Optional[dict] = None,
) -> Decision:
    """Decide what to do after a step fails (state-model §8 ladder rules).

    - transient (阶梯 1): retry the same stage with exponential backoff until
      ``MAX_TRANSIENT_RETRIES``; then escalate to environmental and block.
    - environmental (阶梯 2): block immediately, wait for the user to fix.
    - quality (阶梯 3): bounded same-branch fix round until ``MAX_FIX_ROUNDS``
      (and within budget); then escalate to needs-you and block. A configured
      ``fallback_model`` (``{provider_id, model_id}``) is recorded in the
      decision params as ``model="fallback"`` so the reconciler can probe and
      stage that snapshot (``engine.fallback``); otherwise ``model="primary"``.
    - needs-you (阶梯 4): block to the attention inbox.
    """
    level = ladder.classify(failure_code)

    if level == ladder.LADDER_TRANSIENT:
        if not ladder.retries_exhausted(level, attempt_no):
            return Decision(
                RETRY, reason=failure_code or "transient", ladder=level,
                params={"attempt_no": attempt_no + 1,
                        "backoff_seconds": ladder.backoff_seconds(attempt_no)},
            )
        return Decision(
            BLOCK, reason="transient_exhausted", ladder=ladder.escalate(level),
            attention=True, params={"failure_code": failure_code},
        )

    if level == ladder.LADDER_QUALITY:
        if fix_round < ladder.MAX_FIX_ROUNDS and budget_ok:
            return Decision(
                START_FIX_ROUND, reason=failure_code or "quality", ladder=level,
                params={"fix_round": fix_round + 1,
                        "model": "fallback" if fallback_model else "primary",
                        "fallback_model": fallback_model},
            )
        reason = ("fix_rounds_exhausted" if fix_round >= ladder.MAX_FIX_ROUNDS
                  else "auto_budget_exceeded")
        return Decision(
            BLOCK, reason=reason, ladder=ladder.escalate(level),
            attention=True, params={"failure_code": failure_code},
        )

    if level == ladder.LADDER_ENVIRONMENTAL:
        return Decision(
            BLOCK, reason=failure_code or "environmental", ladder=level,
            attention=True, params={"failure_code": failure_code},
        )

    return Decision(
        BLOCK, reason=failure_code or "needs_you", ladder=ladder.LADDER_NEEDS_YOU,
        attention=True, params={"failure_code": failure_code},
    )


def round_settled_decision(exit_code: int) -> Decision:
    """Map a probed round exit code to the settle intent (sandbox §13.2).

    The engine never writes round state itself: it enqueues a monitor request
    and the deterministic monitor stage finishes the job — ``_complete``
    (core-owned verification → delivery commit → in-review) for a clean exit,
    or ``needs_user``/``execution_failed`` (阶梯 4 → attention inbox) for a
    non-zero one. Both go through :data:`SETTLE_ROUND` so the reconciler and
    the action log see one mechanism; the exit code travels in ``params``.
    """
    if exit_code == 0:
        return Decision(SETTLE_ROUND, reason="round_exited_zero",
                        params={"exit_code": 0, "terminal": "completed"})
    return Decision(
        SETTLE_ROUND, reason="execution_failed",
        ladder=ladder.classify("execution_failed"),
        params={"failure_code": "execution_failed", "exit_code": exit_code,
                "terminal": "failed"})


# Per-action explanation templates for shadow-mode logging (§5.27: 决策差异落日志且有解释).
_EXPLAIN = {
    NOOP: "无需动作：{reason}",
    AWAIT: "等待中：{reason}",
    BLOCK: "需人工处理：{reason}（失败阶梯 {ladder}）",
    RETRY: "瞬时失败，退避重试：{reason}",
    ADVANCE_INTAKE: "接收新 To Do 进入规划",
    ENQUEUE_PLAN: "需求待规划，入队规划",
    CONFIRM_PLAN: "计划就绪且预算充足，确认并安排执行",
    ENQUEUE_DISPATCH: "已确认执行，入队派发",
    ADVANCE_DISPATCH: "派发推进中，等待发送确认",
    RESUME_OBSERVE: "观察者缺席，只读续接执行轮次",
    RUN_VERDICT: "执行中，入队 monitor 读取终态并核验",
    COMMIT_DELIVERY: "验证通过，提交任务工作树",
    ENQUEUE_REVIEW: "进入评审，入队 review",
    START_FIX_ROUND: "评审要求修改，启动同任务修复轮",
    SYNC_DELIVERY: "PR 已开，入队 delivery 轮询合并",
    SETTLE_ROUND: "轮次已退出但任务仍在运行，入队 monitor 收尾：{reason}",
}


def explain(snapshot: dict, decision: "Decision") -> str:
    """One-line Chinese explanation of a decision (for shadow/audit logs)."""
    template = _EXPLAIN.get(decision.action, "动作 {action}：{reason}")
    phase = decision.params.get("phase")
    condition = decision.params.get("condition")
    prefix = f"[{phase}/{condition}] " if phase else ""
    return prefix + template.format(
        reason=decision.reason or "-", ladder=decision.ladder, action=decision.action)
