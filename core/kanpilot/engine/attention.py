"""Attention inbox — the single source for "needs-you" items (§5.27.3 + A15).

Pure derivation (like :mod:`kanpilot.derive`): the source of truth is task
state, so an item exists **iff** its task currently needs attention. That gives
the two invariants for free:

- **at most one open item per task** — one ``needs_user`` state per task;
  :func:`collect` additionally collapses any duplicate signals by ``task_id``.
- **auto-close when the condition disappears** — once a task leaves
  ``needs_user`` (resolve / resume / advance), :func:`build_item` returns
  ``None`` and the item vanishes; :func:`reconcile` reports the close reason.

The item shape is the §5.27.3 union (protocol v6): ``{attention_id, task_id,
project_name, title, reason_code, failure_code, failure_stage, failure_reason,
evidence, blocked_summary, recommended, alternatives, allowed_actions,
attention_state, since}``.

``reason_code`` is a stable enum derived from ``failure_code`` (+ failure
ladder) via :func:`reason_code_for`; unclassifiable blocks fall to
``needs_user_other``. ``attention_id`` is deterministic
(``attn_<task_id>_<reason_code>``) so it is stable across restarts and reused
for the same task+reason_code.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from . import ladder

# attention_state vocabulary (protocol §5.21.1 / §5.27.3).
ACTIONABLE = "actionable"
HISTORICAL = "historical"
INFORMATIONAL = "informational"

# Stable reason_code enum (protocol §5.27.3 table). Derived from failure_code
# (+ failure ladder); anything not mapped falls to NEEDS_USER_OTHER.
NEEDS_PLAN_INPUT = "needs_plan_input"
VERIFY_UNCONFIRMED = "verify_unconfirmed"
REVIEW_CHANGES_EXHAUSTED = "review_changes_exhausted"
DELIVERY_BLOCKED = "delivery_blocked"
REMOTE_UNAVAILABLE = "remote_unavailable"
PLUS_UNREACHABLE = "plus_unreachable"
PLUS_SEND_UNKNOWN = "plus_send_unknown"
GH_ENVIRONMENT = "gh_environment"
PUSH_REJECTED = "push_rejected"
PR_CLOSED_UNMERGED = "pr_closed_unmerged"
BUDGET_EXCEEDED = "budget_exceeded"
CONCURRENCY_STALLED = "concurrency_stalled"
NEEDS_USER_OTHER = "needs_user_other"

# failure_code → reason_code (protocol §5.27.3 table + the codebase's actual
# codes grouped into the same categories). Unknown codes → NEEDS_USER_OTHER.
_REASON_TABLE = {
    # needs_plan_input (阶梯 3): planning material/failure.
    "planner_failed": NEEDS_PLAN_INPUT, "plan_missing": NEEDS_PLAN_INPUT,
    "plan_failed": NEEDS_PLAN_INPUT, "planner_timeout": NEEDS_PLAN_INPUT,
    "planner_output_invalid": NEEDS_PLAN_INPUT, "planner_input_invalid": NEEDS_PLAN_INPUT,
    "needs_input": NEEDS_PLAN_INPUT, "branch_exists": NEEDS_PLAN_INPUT,
    "delivery_base_unknown": NEEDS_PLAN_INPUT,
    # verify_unconfirmed (阶梯 3): core-run verify failed / evidence missing.
    "verification_unconfirmed": VERIFY_UNCONFIRMED, "empty_diff": VERIFY_UNCONFIRMED,
    # review_changes_exhausted (3→4): same-branch fix rounds used up.
    "review_blocked": REVIEW_CHANGES_EXHAUSTED, "review_fix_exhausted": REVIEW_CHANGES_EXHAUSTED,
    "fix_rounds_exhausted": REVIEW_CHANGES_EXHAUSTED, "review_backend_unsupported": REVIEW_CHANGES_EXHAUSTED,
    # delivery_blocked (阶梯 3): commit/push/PR受阻.
    "delivery_commit_failed": DELIVERY_BLOCKED, "delivery_blocked_files": DELIVERY_BLOCKED,
    "branch_diverged": DELIVERY_BLOCKED, "no_changes": DELIVERY_BLOCKED,
    "gh_unavailable": DELIVERY_BLOCKED,
    # remote_unavailable (1→2): Kaneo unreachable / task gone remotely.
    "remote_unavailable": REMOTE_UNAVAILABLE, "remote_task_missing": REMOTE_UNAVAILABLE,
    "remote_conflict": REMOTE_UNAVAILABLE,
    # plus_unreachable (阶梯 2): Plus control plane unreachable/timeout.
    "plus_unreachable": PLUS_UNREACHABLE, "plus_timeout": PLUS_UNREACHABLE,
    # plus_send_unknown (阶梯 2): prompt send result unknown, no auto-resend.
    "send_state_unknown": PLUS_SEND_UNKNOWN,
    # gh_environment (阶梯 2): gh not installed / not logged in / timeout.
    "gh_missing": GH_ENVIRONMENT, "gh_not_logged_in": GH_ENVIRONMENT, "gh_timeout": GH_ENVIRONMENT,
    # push_rejected (阶梯 3): non-fast-forward push rejected.
    "push_rejected": PUSH_REJECTED,
    # pr_closed_unmerged (阶梯 3): PR closed unmerged, never mark done.
    "pr_closed_unmerged": PR_CLOSED_UNMERGED, "pr_closed": PR_CLOSED_UNMERGED,
    # budget_exceeded (阶梯 4): cost/time cap hit.
    "auto_budget_exceeded": BUDGET_EXCEEDED,
    # concurrency_stalled (1→2): lease expired / request lost, re-enqueue.
    "lease_expired": CONCURRENCY_STALLED, "request_lost": CONCURRENCY_STALLED,
}

# failure_stage → reason_code, used only when failure_code is empty (§5.27.3:
# "failure_code 为空时按 status/failure_stage 归类"); values from the frozen 13.
_STAGE_REASON = {
    "plan": NEEDS_PLAN_INPUT,
    "monitor": VERIFY_UNCONFIRMED,
    "review": REVIEW_CHANGES_EXHAUSTED,
    "delivery": DELIVERY_BLOCKED,
    "dispatch": DELIVERY_BLOCKED,
}

# Frozen item field set (single source for the snapshot contract).
ITEM_FIELDS = (
    "attention_id", "task_id", "project_name", "title", "reason_code",
    "failure_code", "failure_stage", "failure_reason", "evidence",
    "blocked_summary", "recommended", "alternatives", "allowed_actions",
    "attention_state", "since",
)


def reason_code_for(failure_code: Optional[str], *, status: Optional[str] = None,
                    failure_stage: Optional[str] = None) -> str:
    """Stable reason enum (§5.27.3): table by failure_code; an empty failure_code
    falls back by failure_stage; otherwise needs_user_other.

    Mapping values are drawn only from the 13 frozen reason_codes; the ladder
    category is *not* a reason_code (it lives in ``evidence``).
    """
    if failure_code in _REASON_TABLE:
        return _REASON_TABLE[failure_code]
    if not failure_code and failure_stage in _STAGE_REASON:
        return _STAGE_REASON[failure_stage]
    return NEEDS_USER_OTHER


# Backwards-compatible alias.
reason_code = reason_code_for


def attention_id(task_id: Optional[str], code: str) -> str:
    """Deterministic, restart-stable id (reused for the same task+reason_code)."""
    return f"attn_{task_id}_{code}"


def needs_attention(task: dict, *, include_historical: bool = False) -> bool:
    """Whether a task view is an attention source right now."""
    if task.get("status") != "needs_user":
        return False
    if _save_state(task) == "purged":
        return include_historical
    if _save_state(task) in ("archived", "trashed"):
        return include_historical
    return True


def attention_state(task: dict) -> str:
    """actionable | historical | informational for a needs_user task view."""
    if _save_state(task) in ("archived", "trashed", "purged"):
        return HISTORICAL
    return ACTIONABLE


def _evidence(task: dict, since) -> List[dict]:
    """Structured evidence array (§5.27.3): [{kind, id, at, detail}, ...].

    The command handler supplies real event/request ids; otherwise a single
    block item is derived from the failure fields. Never contains tokens,
    connection-file contents, or full Plus transcripts.
    """
    provided = task.get("evidence")
    if isinstance(provided, list):
        return provided
    detail = task.get("blocked_summary") or task.get("failure_reason")
    if not detail:
        return []
    return [{"kind": "event", "id": task.get("block_event_id"),
             "at": since, "detail": detail}]


def build_item(task: dict, now=None) -> Optional[dict]:
    """Project a needs_user task view to the §5.27.3 union item (or None).

    Pure projection: builds for *any* ``needs_user`` task (``attention_state``
    reflects save_state). Filtering by ``include_historical`` happens in
    :func:`collect`, not here.
    """
    if task.get("status") != "needs_user":
        return None
    code = reason_code_for(task.get("failure_code"), status=task.get("status"),
                           failure_stage=task.get("failure_stage"))
    actions = list(task.get("allowed_actions") or [])
    failure_code = task.get("failure_code")
    since = task.get("blocked_at") or task.get("updated_at")
    return {
        "attention_id": attention_id(task.get("task_id"), code),
        "task_id": task.get("task_id"),
        "project_name": task.get("project_name"),
        "title": task.get("title"),
        "reason_code": code,
        "failure_code": failure_code,
        "failure_stage": task.get("failure_stage"),
        "failure_reason": task.get("failure_reason"),
        "evidence": _evidence(task, since),
        "blocked_summary": task.get("blocked_summary"),
        # recommended/alternatives derive from allowed_actions (no second table);
        # derive.allowed_actions already orders by relevance.
        "recommended": actions[0] if actions else None,
        "alternatives": actions[1:],
        "allowed_actions": actions,
        "attention_state": attention_state(task),
        "since": since,
    }


def collect(tasks: List[dict], now=None, *, include_historical: bool = False) -> List[dict]:
    """Aggregate attention items: dedup per task, order by ladder priority.

    Ordering (protocol §5.27.3): ladder 4 > 3 > 2 > 1, then oldest block first.
    """
    by_task = {}
    for task in tasks:
        if not needs_attention(task, include_historical=include_historical):
            continue
        item = build_item(task, now)
        if item is None:
            continue
        existing = by_task.get(item["task_id"])
        if existing is None or _rank(task) > _rank(existing["_task"]):
            item["_task"] = task
            by_task[item["task_id"]] = item
    items = list(by_task.values())
    items.sort(key=lambda it: (-_rank(it["_task"]), _since(it["_task"])))
    for it in items:
        it.pop("_task", None)
    return items


def reconcile(item: dict, task: dict, now=None) -> Tuple[bool, Optional[str]]:
    """Is a previously-open item still open? If closed, why (for logging)."""
    if needs_attention(task):
        return True, None
    status = task.get("status")
    if status == "needs_user":
        return False, "save_state"
    if status == "done":
        return False, "resolved"
    return False, "moved"


def recommendation(item: dict) -> Tuple[Optional[str], List[str]]:
    """(recommended, alternatives) — already carried on the item."""
    return item.get("recommended"), list(item.get("alternatives") or [])


def counts(items: List[dict]) -> dict:
    """Backlog counts by attention_state (doctor --live attention_backlog)."""
    out = {ACTIONABLE: 0, HISTORICAL: 0, INFORMATIONAL: 0}
    for item in items:
        state = item.get("attention_state")
        if state in out:
            out[state] += 1
    return out


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _save_state(task: dict) -> str:
    return task.get("save_state") or "active"


def _rank(task: dict) -> int:
    """Attention sort rank = failure ladder (4 highest)."""
    return ladder.classify(task.get("failure_code"))


def _since(task: dict) -> str:
    return task.get("blocked_at") or task.get("updated_at") or ""
