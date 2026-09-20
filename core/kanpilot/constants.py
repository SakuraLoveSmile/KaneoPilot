"""Frozen constants from docs/protocol.md §6 and docs/state-model.md.

Filesystem roots are environment-overridable (KANPILOT_*) so the core can run
against an isolated sandbox in tests.  Defaults match the frozen spec.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = 1

# Project whitelist: name -> Kaneo project id (protocol.md §6).
PROJECTS = {
    "Music": "e8magole7hbivl8103wevkss",
    "Library": "nosjbbex7hpyaiweh6cdidwr",
    "StudyTool": "jb22vodykf5ct64ng1acje5o",
    "Comic": "ms42fpv4oqdompsw3l0a7j4k",
    "Feedback": "ryryo1reph1xfemqtwkjyiiz",
}

WORKSPACE_ID = "ujhqmhAOywzRnoqy0UUH3KaZajQriUI1"

TODO_SLUGS = {"to-do", "todo"}
REVIEW_SLUGS = {"in-review", "review"}
DONE_SLUGS = {"done"}
IN_PROGRESS_SLUG = "in-progress"

STAGES = ("plan", "dispatch", "monitor", "review")

# task.status lifecycle values (protocol.md §2).
TASK_STATUSES = (
    "needs_plan", "planning", "ready", "dispatching", "prepared",
    "submitting", "running", "in_review", "needs_user", "done",
)
DISPATCH_MID_STATUSES = ("dispatching", "prepared", "submitting")

REQUEST_KINDS = ("plan-scan", "plan", "dispatch", "monitor", "review")
KIND_STAGE = {
    "plan-scan": "plan",
    "plan": "plan",
    "dispatch": "dispatch",
    "monitor": "monitor",
    "review": "review",
}

# Claim guards (protocol.md §4.4): kind -> allowed task.status set (None = no task).
CLAIM_GUARDS = {
    "plan": {"needs_plan"},
    "dispatch": {"ready"},
    "monitor": {"running", "in_review"},
    "review": {"in_review"},
    "plan-scan": None,
}

# enqueue kind inference (protocol.md §5.3).
ENQUEUE_INFER = {
    "needs_plan": "plan",
    "ready": "dispatch",
    "running": "monitor",
    "in_review": "review",
}

# Frontmatter requirements (protocol.md §6).
PLAN_META_REQUIRED = (
    "task_id", "project_id", "project_name", "repository", "source_sha256",
    "requirements_sha256", "planner_model", "reasoning_effort",
    "implementation_status", "generated_at",
)
PLAN_META_REQUIRED_REVIEW_FIX = PLAN_META_REQUIRED + (
    "review_report_path", "review_report_sha256",
    "review_source_task_id", "review_key",
)
STANDARD_PLANNER = {"planner_model": "gpt-6-astra", "reasoning_effort": "medium"}
REVIEW_FIX_PLANNER = {"planner_model": "gpt-5.6-sol", "reasoning_effort": "xhigh"}

REVIEW_MARKER_PREFIX = "<!-- codex-kaneo-review:"

DISPATCH_INSTRUCTION = (
    "任务标识：{marker}。读取并完整执行所附计划，保留现有改动，"
    "按计划使用 subagent 和独立 worktree，完成验证并报告结果。"
)

DEFAULT_LEASE_SECONDS = 1200
DEFAULT_MONITOR_INTERVAL_SECONDS = 900
DEFAULT_PLAN_SCAN_INTERVAL_HOURS = 24
DEFAULT_PLAN_SCAN_ANCHOR = "09:00 Asia/Shanghai"

VALID_RESOLVE_ACTIONS = (
    "retry", "restore", "accept_duplicate", "mark_sent",
    "replan", "release_lost", "mark_done", "dismiss",
)

ERROR_CODES = (
    "bad_request", "not_found", "conflict", "stale_run", "busy", "paused",
    "guard_failed", "validation_failed", "remote_unavailable",
    "remote_conflict", "evidence_invalid", "send_conflict", "already_done",
    "migration_needed", "internal",
)


def _home() -> Path:
    return Path.home()


def app_support() -> Path:
    return Path(os.environ.get(
        "KANPILOT_APP_SUPPORT",
        str(_home() / "Library" / "Application Support" / "KaneoPilot"),
    )).expanduser()


def default_db_path() -> Path:
    override = os.environ.get("KANPILOT_DB")
    if override:
        return Path(override).expanduser()
    return app_support() / "kanpilot.db"


def plan_root() -> Path:
    return Path(os.environ.get(
        "KANPILOT_PLAN_ROOT", str(_home() / "Documents" / "Code" / "Plan"),
    )).expanduser()


def repos_root() -> Path:
    return Path(os.environ.get(
        "KANPILOT_REPOS_ROOT",
        str(_home() / "Documents" / "Code" / "Project" / "Personal Project"),
    )).expanduser()


def legacy_state_root() -> Path:
    return Path(os.environ.get(
        "KANPILOT_LEGACY_ROOT",
        str(_home() / "Library" / "Application Support" / "Codex"),
    )).expanduser()


def review_reports_root() -> Path:
    return Path(os.environ.get(
        "KANPILOT_REPORTS_ROOT",
        str(legacy_state_root() / "KaneoReviewer" / "reports"),
    )).expanduser()


def core_root() -> Path:
    return Path(__file__).resolve().parent.parent


def repo_path_for(project_name: str) -> Path:
    return repos_root() / project_name
