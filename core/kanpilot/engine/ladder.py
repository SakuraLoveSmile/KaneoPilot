"""Failure ladders + exponential backoff (state-model §8, protocol §5.27).

Pure and table-driven: no DB, no I/O, no clock. Both the reconciler
(:mod:`kanpilot.engine.loop`, after a step fails) and the attention inbox
(:mod:`kanpilot.engine.attention`, for ordering) classify a ``failure_code``
through :func:`classify` and read retry/backoff policy from the same table.

The four ladders (state-model §8):

===== =============== ============================================ ==========
阶梯  类型             处置                                         自动重试
===== =============== ============================================ ==========
1     transient       指数退避重试（上限 2 次/阶段）                 同阶段
2     environmental   等待人工修复环境，不升级失败                   不重试
3     quality         有界补救（≤2 轮同分支修复）                    同分支
4     needs-you       收件箱单一来源，用户决定                       不自动
===== =============== ============================================ ==========

Unknown / unlisted codes fall to the needs-you ladder (阶梯 4) — the safe
"收件箱单一来源" default ("其余 needs_user 归入阶梯 4").
"""
from __future__ import annotations

from typing import Optional

# Ladder numbers. The number *is* the attention priority (4 > 3 > 2 > 1).
LADDER_TRANSIENT = 1        # 瞬时：退避重试
LADDER_ENVIRONMENTAL = 2    # 环境：等待人工修复
LADDER_QUALITY = 3          # 质量：有界补救
LADDER_NEEDS_YOU = 4        # 需人工：收件箱

# Exponential backoff for the transient ladder (protocol §5.27.1 退避重启口径,
# reused for per-stage transient retries): 1s → 2s → 4s → … capped at 60s.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

# state-model §8：阶梯 1 每阶段最多自动重试 2 次；阶梯 3 同分支最多修复 2 轮。
MAX_TRANSIENT_RETRIES = 2
MAX_FIX_ROUNDS = 2

_LADDERS = {
    LADDER_TRANSIENT: {
        "type": "transient",
        "auto_retry": True,
        "max_attempts": MAX_TRANSIENT_RETRIES,
        "escalate_to": LADDER_ENVIRONMENTAL,   # 仍失败 → 阶梯 2
    },
    LADDER_ENVIRONMENTAL: {
        "type": "environmental",
        "auto_retry": False,
        "max_attempts": 0,
        "escalate_to": None,                   # 用户修复环境后手动 resume
    },
    LADDER_QUALITY: {
        "type": "quality",
        "auto_retry": True,
        "max_attempts": MAX_FIX_ROUNDS,
        "escalate_to": LADDER_NEEDS_YOU,       # 超出 → 阶梯 4
    },
    LADDER_NEEDS_YOU: {
        "type": "needs-you",
        "auto_retry": False,
        "max_attempts": 0,
        "escalate_to": None,                   # 终态；用户 resolve 后回对应 phase
    },
}

# failure_code → ladder. Sources: state-model §8 rules, automation-contract
# §待处理原因/§交付, and scenarios.md fault switches. Anything not listed is a
# needs-you (阶梯 4) item by default.
_FAILURE_CODE_LADDER = {
    # 阶梯 1 transient：网络/远端瞬时；git 探测超时单独成码，区别于 not_git_repo
    # （automation_policy 明确"避免误升级为阶梯 2"）。
    "remote_unavailable": LADDER_TRANSIENT,
    "git_transient": LADDER_TRANSIENT,
    "network_timeout": LADDER_TRANSIENT,
    "kaneo_timeout": LADDER_TRANSIENT,      # scenarios.md #10 network timeout
    "kaneo_500": LADDER_TRANSIENT,          # scenarios.md #10 HTTP 500
    "git_probe_timeout": LADDER_TRANSIENT,  # slow/hanging git probe, not "not a repo"
    # 阶梯 2 environmental：环境/配置缺失，等待人工修复后手动 resume。
    "repository_missing": LADDER_ENVIRONMENTAL,
    "not_git_repo": LADDER_ENVIRONMENTAL,
    "origin_not_github": LADDER_ENVIRONMENTAL,
    "git_unavailable": LADDER_ENVIRONMENTAL,
    "gh_missing": LADDER_ENVIRONMENTAL,
    "gh_timeout": LADDER_ENVIRONMENTAL,     # scenarios.md #14（阶梯 2 环境失败）
    "gh_not_logged_in": LADDER_ENVIRONMENTAL,
    "exec_permission_not_auto": LADDER_ENVIRONMENTAL,
    "exec_backend_not_plus": LADDER_ENVIRONMENTAL,
    "planner_unconfigured": LADDER_ENVIRONMENTAL,
    "review_backend_unsupported": LADDER_ENVIRONMENTAL,
    "review_desktop_unconfigured": LADDER_ENVIRONMENTAL,
    "validation_failed": LADDER_ENVIRONMENTAL,  # scenarios.md #2 plus probe fail
    # 阶梯 3 quality：有界补救（≤2 轮同分支修复）。
    "verification_unconfirmed": LADDER_QUALITY,
    "review_blocked": LADDER_QUALITY,
    "delivery_commit_failed": LADDER_QUALITY,
    "branch_diverged": LADDER_QUALITY,
    "empty_diff": LADDER_QUALITY,           # scenarios.md #5 空转→同计划重试一次
    # 阶梯 4 needs-you：其余一切（fix_rounds_exhausted / auto_budget_exceeded /
    # automation_stopped / pr_closed / gh_unavailable / no_changes /
    # delivery_blocked_files / remote_conflict / planner_failed / needs_input /
    # execution_failed / dispatch_failed / observation_blocked / …）走缺省。
}


def classify(failure_code: Optional[str]) -> int:
    """Map a ``failure_code`` (or verdict condition) to its ladder number."""
    if not failure_code or not isinstance(failure_code, str):
        return LADDER_NEEDS_YOU
    return _FAILURE_CODE_LADDER.get(failure_code, LADDER_NEEDS_YOU)


def ladder_type(ladder: int) -> str:
    """Human/machine type name for a ladder number."""
    return _LADDERS.get(ladder, _LADDERS[LADDER_NEEDS_YOU])["type"]


def auto_retry_allowed(ladder: int) -> bool:
    """Whether this ladder retries automatically at all."""
    return bool(_LADDERS.get(ladder, _LADDERS[LADDER_NEEDS_YOU])["auto_retry"])


def max_attempts(ladder: int) -> int:
    """Per-stage (阶梯 1) / per-branch (阶梯 3) automatic attempt budget."""
    return int(_LADDERS.get(ladder, _LADDERS[LADDER_NEEDS_YOU])["max_attempts"])


def retries_exhausted(ladder: int, attempt_no: int) -> bool:
    """True when the automatic attempt budget is used up.

    ``attempt_no`` counts retries already spent (0 = first attempt).
    """
    return attempt_no >= max_attempts(ladder)


def escalate(ladder: int) -> Optional[int]:
    """Ladder to move to once retries are exhausted (None = wait for user)."""
    return _LADDERS.get(ladder, _LADDERS[LADDER_NEEDS_YOU])["escalate_to"]


def backoff_seconds(attempt_no: int) -> float:
    """Exponential backoff for a 0-based retry attempt: 1 → 2 → 4 → … capped.

    Used by the transient ladder; clamp negatives to the base delay.
    """
    if attempt_no < 0:
        attempt_no = 0
    return min(BACKOFF_BASE_SECONDS * (2 ** attempt_no), BACKOFF_CAP_SECONDS)
