"""Frozen constants from docs/protocol.md §6 and docs/state-model.md.

Filesystem roots are environment-overridable (KANPILOT_*) so the core can run
against an isolated sandbox in tests.  Defaults match the frozen spec.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA_VERSION = 5

# Save states (protocol.md §5.15.1): orthogonal to task.status lifecycle.
SAVE_STATES = ("active", "archived", "trashed", "purged")

# Session (execution round) save states (protocol.md §5.16 + §5.20 v2)：
# 会话保存状态与任务保存状态正交；purged 为终态（永久从列表移除，历史与
# 文件保留，不可恢复）。
SESSION_SAVE_STATES = ("active", "trashed", "purged")

# 执行轮次后端与运行状态（protocol.md §5.16.1 / §5.18 / §5.23.3）。v1.6 起派发
# 后端为 pi（pi CLI 子进程）；v2 起规划轮次由 Codex 适配器执行（backend="codex"）；
# v3 起任务执行轮次可为 PI-Desktop 本机 MCP 控制面（backend="pi_desktop"）。
# 历史行保留旧值，读取层按「已弃用」处理。
ROUND_BACKENDS = ("pi", "codex", "pi_desktop", "codex_desktop")
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

# v3（protocol §5.23.3）：任务执行后端词表与 PI-Desktop 配置块字段名。
# backend 缺省按 "pi" 解释；pi_desktop 子块五字段须用户显式保存
# （permission_mode 不自动补 auto）。
EXEC_BACKENDS = ("pi", "pi_desktop")
EXEC_PI_DESKTOP_KEYS = (
    "connection_file", "provider_id", "model_id", "thinking_level",
    "permission_mode",
)
EXEC_PI_DESKTOP_ALL_KEYS = (
    "model_source", "connection_file", "provider_id", "model_id",
    "thinking_level", "permission_mode",
)
EXEC_PERMISSION_MODES = ("ask", "accept-edits", "auto")
MODEL_SOURCES = ("inherit", "custom")
DEFAULT_MODEL_SOURCE = "inherit"
DEFAULT_PI_DESKTOP_CONNECTION_FILE = "~/.pi-desktop-plus/mcp-control.json"

# v5（protocol §5.25 / T7）：KaneoPilot 项目自有标准 MCP 服务契约
MCP_SERVER_NAME = "kaneopilot"
MCP_SERVER_VERSION = "1.0.0"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_COMPATIBLE_VERSIONS = ("2025-06-18", "2025-03-26")
MCP_TOOL_CONTRACT_VERSION = "kp-mcp-v1"
# PI-Desktop 本机 MCP 控制面所需工具清单（protocol §5.23.3 能力门）。
PI_DESKTOP_REQUIRED_TOOLS = (
    "pi_turn_get", "pi_session_create", "pi_session_configure",
    "pi_agent_prompt", "pi_agent_status", "pi_session_get",
    "pi_agent_stop", "pi_agent_abort", "pi_desktop_invoke",
)
# 任务执行轮次固定以 Goal 模式建会话（protocol §5.23.9）：Goal 是契约模式——
# 首轮协商出「目标契约」，审批后宿主在同会话开启执行轮次。审批经这两个工具
# 完成；缺其一则派发会在审批门停滞，故启动前即失败（unsupported_tools）。
PI_DESKTOP_GOAL_TOOLS = ("pi_plans_pending", "pi_plans_resolve")
PI_DESKTOP_INPUT_TOOLS = ("pi_asks_pending", "pi_asks_resolve")
EXECUTION_QUESTION_MAX_WAIT_SECONDS = 86400
PI_DESKTOP_EXEC_MODE = "goal"
# 派发就绪（exec-capabilities / probe / worker 工具门）要求的完整工具集：
# 基础工具 + Goal 契约审批工具，缺任一即判不可用（不做事后半残派发）。
PI_DESKTOP_DISPATCH_TOOLS = (PI_DESKTOP_REQUIRED_TOOLS
                             + PI_DESKTOP_GOAL_TOOLS + PI_DESKTOP_INPUT_TOOLS)
# T5-A（protocol §5.23.10）：Goal 报告只读工具。核心只在读取报告时要求它们；
# 派发就绪门不要求（旧 Plus 仍可派发，但报告读取会明确失败）。
PI_DESKTOP_GOAL_REPORT_TOOLS = ("pi_goal_report_list", "pi_goal_report_get")
# T6（protocol §5.23.10）：`pi_session_create.inputSchema.properties.projectName`
# 是旧 Plus 与新 Plus 的兼容门；缺字段 → 明确阻断，不静默派发 UUID 名称。
PI_SESSION_CREATE_PROJECT_NAME_CAPABILITY = "projectName"
PI_DESKTOP_NAMING_CAPABILITY_MISSING = "版本缺少工作区命名能力"
# MCP 单请求上限 2 MiB——计划正文超限在发送前明确失败，不截断。
PI_DESKTOP_MCP_MAX_REQUEST_BYTES = 2 * 1024 * 1024

# v4（protocol §5.24.1）：四个模型调用点各自的后端词表与缺省值。缺字段一律
# 按旧语义解释（codex/pi），旧数据零迁移；切换后端不删除另一分支字段。
PLANNER_BACKENDS = ("codex", "pi_desktop", "claude", "codex_desktop")
PLANNER_BACKEND = "codex"
CONTROLLER_BACKENDS = ("pi", "pi_desktop")
CONTROLLER_BACKEND = "pi"
REVIEW_BACKENDS = ("pi", "pi_desktop", "claude", "codex", "codex_desktop")
REVIEW_BACKEND = "pi"
# exec_config 里控制端后端字段名（与任务执行 backend 并存，互不覆盖）。
CONTROLLER_BACKEND_KEY = "controller_backend"

# v4 工具门（protocol §5.24.2）：控制端不建 Goal 会话，只需基础工具；
# 计划/Review 需读并安全关闭产物检点，故要求基础 + 契约审批工具。
PI_DESKTOP_CONTROLLER_TOOLS = PI_DESKTOP_REQUIRED_TOOLS
PI_DESKTOP_PLAN_TOOLS = PI_DESKTOP_REQUIRED_TOOLS + PI_DESKTOP_GOAL_TOOLS
# 各角色的 Plus 会话模式：规划与 Review plan + auto；
# 控制端 agent + 用户显式 permission_mode；任务执行 goal。
PI_DESKTOP_PLAN_MODE = "plan"
PI_DESKTOP_CONTROLLER_MODE = "agent"
# 规划独立于 Review 权限；新请求使用 auto，既有请求保留其固化快照。
PI_DESKTOP_PLANNER_PERMISSION_MODE = "auto"
# Review 新轮次使用 auto；与规划权限独立，历史快照不改写。
PI_DESKTOP_REVIEW_PERMISSION_MODE = "auto"
# 兼容既有 ask 轮次（Plan 模式本身不是 OS 只读沙箱）。
PI_DESKTOP_READONLY_PERMISSION_MODE = "ask"
# v4：Review 最终答复的结论与 fix 优先级枚举（后者与 bridge.VALID_PRIORITIES
# 同源，新增值须两处同步）。
REVIEW_CONCLUSIONS = ("pass", "changes_requested", "blocked")
REVIEW_FIX_PRIORITIES = ("no-priority", "low", "medium", "high", "urgent")
# T5-D（protocol §5.23.10）：执行终态落地后给 Host 完成 outbox/报告发布的宽限
# （期间只读查询，绝不重发 prompt）。
GOAL_REPORT_GRACE_SECONDS = 60.0
# 报告不可读/损坏时最多允许的有界重读次数（此后交人工，区分于「已验证失败」）。
GOAL_REPORT_MAX_REREAD = 1


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
    "Music": "example-project-01",
    "Library": "example-project-02",
    "StudyTool": "example-project-03",
    "Comic": "example-project-04",
    "Feedback": "example-project-05",
}

WORKSPACE_ID = "example-record-id"

TODO_SLUGS = {"to-do", "todo"}
REVIEW_SLUGS = {"in-review", "review"}
DONE_SLUGS = {"done"}
IN_PROGRESS_SLUG = "in-progress"

STAGES = ("plan", "dispatch", "monitor", "review", "delivery")

# task.status lifecycle values (protocol.md §2).
TASK_STATUSES = (
    "needs_plan", "planning", "ready", "dispatching", "prepared",
    "submitting", "running", "in_review", "needs_user", "done",
)
DISPATCH_MID_STATUSES = ("dispatching", "prepared", "submitting")

REQUEST_KINDS = ("plan-scan", "plan", "dispatch", "monitor", "review", "delivery")
KIND_STAGE = {
    "plan-scan": "plan",
    "plan": "plan",
    "dispatch": "dispatch",
    "monitor": "monitor",
    "review": "review",
    "delivery": "delivery",
}

# Claim guards (protocol.md §4.4): kind -> allowed task.status set (None = no task).
CLAIM_GUARDS = {
    "plan": {"needs_plan"},
    "dispatch": {"ready"},
    "monitor": {"running", "in_review"},
    "review": {"in_review"},
    "delivery": {"in_review"},
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
DEFAULT_MONITOR_INTERVAL_SECONDS = 120
DEFAULT_PLAN_SCAN_INTERVAL_HOURS = 24
DEFAULT_PLAN_SCAN_ANCHOR = "09:00 Asia/Shanghai"

VALID_RESOLVE_ACTIONS = (
    "retry", "restore", "accept_duplicate", "mark_sent",
    "replan", "release_lost", "mark_done", "dismiss", "resume", "accept_verification", "recover_review", "reverify",
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
    """评审报告根（v4 §5.24.5）：默认 ``<APP_SUPPORT>/reports``。

    `register-review`/`dispatch_validate` 用它做 ``util.inside`` 边界校验，因此
    写入方与校验方必须同一根。v3 的 ``<LEGACY_ROOT>/KaneoReviewer/reports`` 仅作
    历史路径，不再是默认；``KANPILOT_REPORTS_ROOT`` 覆盖仍然生效（隔离夹具与
    需要保留旧目录的部署可用）。
    """
    return Path(os.environ.get(
        "KANPILOT_REPORTS_ROOT",
        str(app_support() / "reports"),
    )).expanduser()


def sources_dir() -> Path:
    return Path(os.environ.get(
        "KANPILOT_SOURCES_DIR", str(app_support() / "sources"),
    )).expanduser()


def default_session_runs_root() -> Path:
    """headless 运行记录默认根目录（protocol.md §5.15.5）。动态解析 constants.app_support() / "runs" """
    if DEFAULT_SESSION_RUNS_ROOT != "~/Library/Application Support/KaneoPilot/runs":
        return Path(DEFAULT_SESSION_RUNS_ROOT).expanduser()
    return app_support() / "runs"


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

# v5 deterministic automation and branch delivery (protocol §5.27).
DELIVERY_EXECUTOR_ID = "kaneo-delivery"
DELIVERY_SYNC_FAST_SECONDS = 300
DELIVERY_SYNC_FAST_WINDOW_SECONDS = 3600
DELIVERY_SYNC_SLOW_SECONDS = 1800
INTAKE_INTERVAL_SECONDS = 300
INTAKE_MAX_ADOPT_PER_PASS = 3
INTAKE_LABEL_LOOKUPS_PER_PASS = 20
INTAKE_LABEL_RECHECK_SECONDS = 1800
AUTOMATION_DEFAULTS = {
    "enabled": False, "label_filter": [], "auto_confirm_plan": True,
    "max_fix_rounds": 2, "max_task_minutes": 240, "max_task_cost_usd": 10.0,
    "delivery_base": None, "policy_revision": 0, "updated_at": None,
}
AUTOMATION_GLOBAL_DEFAULTS = {"max_active_tasks": 2}
KANEO_MARKER_PREFIX = "[kaneopilot:"
DELIVERY_BRANCH_PREFIX = "kaneo/"
DELIVERY_BLOCKED_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*")
DELIVERY_ALLOWED_NAMES = (".env.example",)
DELIVERY_MAX_FILE_BYTES = 5 * 1024 * 1024
# 构建产物：执行器在工作树跑 verify 时必然产生（Python 的 __pycache__/.pyc、
# pytest/mypy/ruff 缓存、node_modules、dist/build、.DS_Store 等）。它们不是任务
# 交付物——提交进去会把本机二进制差异带进分支，多个任务分支间因内容不同而
# add/add 冲突（沙盒 PR #2/#3 CONFLICTING 的根因）。目录模式以 "/" 结尾，
# 匹配路径中任意一段；其余按文件名 fnmatch。
DELIVERY_ARTIFACT_PATTERNS = (
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", ".tox/", ".coverage", "htmlcov/", "*.egg-info/",
    "node_modules/", "dist/", "build/", ".DS_Store", "Thumbs.db",
    ".venv/", "venv/", "*.so", "*.dylib", "*.class", "*.o", "*.a",
)
CLI_PLAN_DEFAULT_BUDGET_USD = 5.0
CLI_REVIEW_DEFAULT_BUDGET_USD = 5.0
CLI_MIN_BUDGET_USD = 0.5
AUTOMATION_REVIEW_BACKENDS = ("pi_desktop", "claude", "codex", "codex_desktop")
DISPATCH_INSTRUCTION_BRANCH = (
    "任务标识：{marker}。当前目录 {worktree} 是本任务专用分支 {branch} 的工作树，直接在这里完整执行{plan_ref}。"
    "可以按计划使用 subagent，但所有改动必须落在本工作树；不要新建 worktree 或分支，不要 commit、push、reset、stash、rebase 或切换分支；"
    "提交与推送由调度台完成。每条 verify 命令分别使用一次终端工具调用，直接记录独立退出码；"
    "需要 PATH 时只设置 PATH 后运行这一条检查，不将多项检查、echo 或打印退出码合并。最终答复逐条列出命令与退出码。"
)
DISPATCH_INSTRUCTION_BRANCH_LIVE = (
    "任务标识：{marker}。当前目录 {worktree} 是本任务专用分支 {branch} 的工作树，直接在这里完整执行{plan_ref}。"
    "可以按计划使用 subagent，但所有改动必须落在本工作树；不要新建 worktree 或分支，不要 commit、push、reset、stash、rebase 或切换分支；"
    "提交与推送由调度台完成。按计划自检并报告结果；调度台核心会在本工作树独立运行冻结计划中的 verify 命令，"
    "以核心退出码和有效改动判定通过。最终答复列出完成项与剩余问题。"
)
AUTOMATION_FAILURE_STAGES = {
    "verification_environment_failure": "monitor",
    "verification_unconfirmed": "monitor", "branch_exists": "plan", "delivery_base_unknown": "plan",
    "fix_rounds_exhausted": "review", "auto_budget_exceeded": "review", "automation_stopped": "review",
    "review_backend_unsupported": "review", "delivery_blocked_files": "delivery", "delivery_commit_failed": "delivery",
    "no_changes": "delivery", "branch_diverged": "delivery", "gh_unavailable": "delivery", "pr_closed": "delivery",
    "gh_missing": "delivery", "gh_timeout": "delivery", "gh_not_logged_in": "delivery",
}
AUTOMATION_FAILURE_CODES = frozenset(AUTOMATION_FAILURE_STAGES)


def gh_bin() -> str:
    return os.environ.get("KANPILOT_GH_BIN", "gh")


def claude_bin() -> str:
    return os.environ.get("KANPILOT_CLAUDE_BIN", "claude")
