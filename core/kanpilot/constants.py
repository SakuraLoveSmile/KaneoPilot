"""Frozen constants from docs/protocol.md §6 and docs/state-model.md.

Filesystem roots are environment-overridable (KANPILOT_*) so the core can run
against an isolated sandbox in tests.  Defaults match the frozen spec.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = 4

# Save states (protocol.md §5.15.1): orthogonal to task.status lifecycle.
SAVE_STATES = ("active", "archived", "trashed", "purged")

# Session (execution round) save states (protocol.md §5.16 + §5.20 v2)：
# 会话保存状态与任务保存状态正交；purged 为终态（永久从列表移除，历史与
# 文件保留，不可恢复）。
SESSION_SAVE_STATES = ("active", "trashed", "purged")

# 执行轮次后端与运行状态（protocol.md §5.16.1 / §5.18）。v1.6 起派发后端为
# pi（pi CLI 子进程）；v2 起规划轮次由 Codex 适配器执行（backend="codex"）。
# 历史行保留旧值，读取层按「已弃用」处理。
ROUND_BACKENDS = ("pi", "codex")
LEGACY_ROUND_BACKENDS = ("dsh", "headless")  # 历史行可能携带；只读展示
ROUND_RUN_STATUSES = ("pending", "running", "finished", "failed", "unknown")

# v2（protocol §5.22）：手动规划链路材料限制与 Codex 规划适配。
PLANNER_EXECUTOR_ID = "kaneo-planner"
PLAN_INPUT_FILE_MAX_BYTES = 20 * 1024 * 1024    # 单文件 ≤20 MiB
PLAN_INPUT_TOTAL_MAX_BYTES = 100 * 1024 * 1024  # 每任务材料合计 ≤100 MiB
PLAN_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PLAN_TEXT_SUFFIXES = {".txt", ".log", ".md", ".text"}
DEFAULT_CODEX_BIN = "codex"
DEFAULT_CODEX_HOME = "~/.codex"
# 规划 runner 心跳间隔上限（≤ min(lease/3, 60)）。
PLANNER_HEARTBEAT_MAX_SECONDS = 60
PLANNER_EVENTS_NAME = "events.jsonl"
PLANNER_CODEX_LOG_NAME = "codex.jsonl"

# pi 执行配置（protocol.md §5.17.1，v1.6）：初始默认。调度台改配置只写
# settings.exec_config，不修改 pi 全局配置；凭据只查就绪态（pi auth check
# 退出码），不读不存密钥值。
EXEC_CONFIG_DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-flash",
    "reasoning_effort": "xhigh",
    "agent_preset": "default",
}
EXEC_CONFIG_KEYS = (
    "provider", "model", "reasoning_effort", "agent_preset",
)

# 能力/环境查询的进程接缝（protocol.md §5.17.2）：测试经 env 指向隔离夹具。
DEFAULT_PI_HOME = "~/.pi"
DEFAULT_PI_BIN = "pi"

# pi 轮次启动与超时（protocol.md §5.18.3/§5.18.4）。
DEFAULT_PI_START_WAIT_SECONDS = 30   # dispatch-start 等待 session 事件的上限
DEFAULT_PI_ROUND_TIMEOUT_SECONDS = 14400  # 4h：超时 → 明确异常，不自动重提

# 会话消息读取缺省页大小（protocol §5.15.5 session-messages limit 省略时）。
DEFAULT_SESSION_MESSAGE_LIMIT = 500

# task.status values that block archive/trash (protocol.md §5.15.2):
# planning / dispatch-in-flight / executing / reviewing.
ARCHIVE_BLOCKED_STATUSES = frozenset(
    {"planning", "dispatching", "prepared", "submitting", "running", "in_review"}
)

# 执行轮次运行记录会话读取（v1.6：pi 轮次 events.jsonl + job.json）。
DEFAULT_SESSION_POLL_SECONDS = 2
# 运行记录根目录缺省值；可被 DB setting ``session_runs_root`` 覆盖（字符串路径，
# 支持 ~ 展开；空字符串视为未设置），或经 list-sessions payload 临时覆盖。
DEFAULT_SESSION_RUNS_ROOT = "~/Library/Application Support/KaneoPilot/runs"
# list-sessions 缺省模式：root 下任务目录数超过该阈值时轻量核验（只 stat 不读
# 文件内容），associated 态由 get-session/session-messages 补齐。
LIST_SESSIONS_LIGHT_THRESHOLD = 50

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

# Board columns (protocol.md §2 看板列, state-model.md §2.1) — fixed order.
BOARD_COLUMNS = (
    "needs_plan", "todo", "in_progress", "in_review", "done", "failed",
)

# needs_user failure_code values that mean a real failure (→ failed column,
# protocol.md §5.14.1); every other code is a block classified by failure_stage.
BOARD_FAILURE_CODES = frozenset(
    {"validation_failed", "dispatch_failed", "execution_failed", "plan_failed"}
)

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

# v1.6（protocol §5.18.3）：新 claim（backend=pi）的无附件指令形态。
DISPATCH_INSTRUCTION_PI = (
    "任务标识：{marker}。读取并完整执行计划文件 {plan_path}，"
    "保留现有改动，按计划使用 subagent 和独立 worktree，完成验证并报告结果。"
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


def sources_dir() -> Path:
    return Path(os.environ.get(
        "KANPILOT_SOURCES_DIR", str(app_support() / "sources"),
    )).expanduser()


def default_session_runs_root() -> Path:
    """headless 运行记录默认根目录（protocol.md §5.15.5）。每次调用按当前
    HOME 解析（~ 展开滞后于环境变化，测试可在 setUp 后安全断言）。"""
    return Path(DEFAULT_SESSION_RUNS_ROOT).expanduser()


def core_root() -> Path:
    return Path(__file__).resolve().parent.parent


def pi_home() -> Path:
    """pi 家目录（protocol.md §5.17.2，v1.6）：KANPILOT_PI_HOME > PI_HOME >
    ~/.pi。测试经 env 指向隔离夹具，不读用户真实配置。"""
    override = os.environ.get("KANPILOT_PI_HOME") or os.environ.get("PI_HOME")
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_PI_HOME).expanduser()


def pi_bin() -> str:
    """pi 可执行（protocol.md §5.17.2）：KANPILOT_PI_BIN > PATH 上 pi >
    ~/.npm-global/bin/pi（Finder 启动的 app PATH 不含用户 bin 目录时的
    回退）。返回值优先为可直接执行的绝对路径。"""
    override = os.environ.get("KANPILOT_PI_BIN")
    if override:
        return override
    import shutil

    found = shutil.which(DEFAULT_PI_BIN)
    if found:
        return found
    fallback = _home() / ".npm-global" / "bin" / "pi"
    if fallback.is_file():
        return str(fallback)
    return DEFAULT_PI_BIN


def agents_dir() -> Path:
    """agent.md 预设目录（protocol.md §5.17.1，v1.6）：
    KANPILOT_AGENTS_DIR > <app_support>/agents。agent_preset 对应
    该目录下 ``<name>.md`` 文件（经 pi --system-prompt 注入）。"""
    override = os.environ.get("KANPILOT_AGENTS_DIR")
    if override:
        return Path(override).expanduser()
    return app_support() / "agents"


def worktrees_root(setting_value=None) -> Path:
    """独立 worktree 根（protocol.md §5.18.1）：settings.worktrees_root >
    KANPILOT_WORKTREES_ROOT > <app_support>/worktrees。"""
    if isinstance(setting_value, str) and setting_value:
        return Path(setting_value).expanduser()
    override = os.environ.get("KANPILOT_WORKTREES_ROOT")
    if override:
        return Path(override).expanduser()
    return app_support() / "worktrees"


def repo_path_for(project_name: str) -> Path:
    return repos_root() / project_name

def codex_home() -> Path:
    """Codex CLI 家目录（protocol.md §5.22.3，v2）：KANPILOT_CODEX_HOME >
    CODEX_HOME > ~/.codex。只查 auth.json 存在性，不读凭据值。"""
    override = os.environ.get("KANPILOT_CODEX_HOME") or os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_CODEX_HOME).expanduser()


def codex_bin() -> str:
    """Codex 可执行：KANPILOT_CODEX_BIN > PATH 上 codex >
    ~/.npm-global/bin/codex 回退（与 pi_bin 同策略）。"""
    override = os.environ.get("KANPILOT_CODEX_BIN")
    if override:
        return override
    import shutil

    found = shutil.which(DEFAULT_CODEX_BIN)
    if found:
        return found
    fallback = _home() / ".npm-global" / "bin" / DEFAULT_CODEX_BIN
    if fallback.is_file():
        return str(fallback)
    return DEFAULT_CODEX_BIN


def planner_inputs_root(setting_value=None) -> Path:
    """规划材料不可变根目录（protocol §5.22.1）：settings.planner_inputs_root
    > KANPILOT_PLANNER_INPUTS > <app_support>/planner_inputs。"""
    if isinstance(setting_value, str) and setting_value:
        return Path(setting_value).expanduser()
    override = os.environ.get("KANPILOT_PLANNER_INPUTS")
    if override:
        return Path(override).expanduser()
    return app_support() / "planner_inputs"
