"""PI-Desktop-Plus 的共享工具层（原「控制端 runner」，kp-e02 后只剩工具）。

历史：本模块曾是 executor_runner --stage dispatch|monitor 的 Plus 控制端 runner——
在 Plus 本机 MCP 面建 mode='agent' 控制会话，用模型跑 core/executor_prompts/<stage>.md。
kp-e02 把 dispatch / monitor 收成确定性流程（auto_dispatch / auto_monitor，不经模型），
这些入口与对应 prompt 已删除。

现在留下的是别的模块仍在用的工具：

- 调用目录与事件日志：invocation_dir / EventLog / write_json；
- MCP 会话读写：_open_client / read_transcript / message_text / last_assistant_text /
  turn_status / agent_status / prove_started / stop_turn；
- 角色配置解析：review_round_config / active_round / validate_block / stored_setting；
- 运行记录：base_job / record_run_entry。

使用方：pi_desktop_review（Review 控制轮）、desktop_queue（Desktop 领取）、
release_verification、cmd_exec、review_settle。
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import constants, db, exec_config, runtime_client, util

# 本模块负责的控制端 stage（review 走 pi_desktop_review）。
STAGES = ("dispatch", "monitor")
EXECUTOR_IDS = {
    "dispatch": "kaneo-dispatch",
    "monitor": "kaneo-monitor",
    "review": "kaneo-review",
}

# 控制轮时序（测试经模块属性加速）。
_POLL_SECONDS = 2.0
_PROOF_WAIT_SECONDS = 30.0
_STOP_GRACE_SECONDS = 10.0
_STOP_VERIFY_SECONDS = 30.0
_PAGE_LIMIT = 40                  # pi_session_get 单页消息数（初始值）
_PAGE_MIN_LIMIT = 1               # 单条仍超限时 runtime 可完整分片读取
_MAX_PAGES_PER_READ = 60          # 单次只读翻页上限（防御无界）
_MAX_READ_ERRORS = 3              # 连续读不到 turn 的可容忍次数
_MAX_EVENT_TEXT = 4000            # 事件里正文截断长度（全文进 transcript.json）

# 包装器与显式环境（protocol §5.24.4）。
WRAPPER_NAME = "kanpilot-cli"
CREDENTIALS_ENV = "KANEO_CREDENTIALS_PATH"
DEFAULT_CREDENTIALS_PATH = "~/.config/kaneo-mcp/credentials.json"
REQUIRED_HOST_ENV = (
    "KANPILOT_DB", "KANPILOT_APP_SUPPORT", "KANPILOT_PLAN_ROOT",
    "KANPILOT_REPOS_ROOT", "KANPILOT_LEGACY_ROOT", CREDENTIALS_ENV,
)
PROMPT_BLOCK_MARKER = "## 本轮宿主环境（Pi-Desktop-Plus 控制端）"
_UNSAFE_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "APIKEY", "API_KEY", "KEY")

_TERMINAL_TURN_STATUSES = ("completed", "error", "aborted")


def _sleep(seconds: float) -> None:  # 测试接缝
    time.sleep(seconds)


# --------------------------------------------------------------- 宿主环境/包装器

def _env_key_allowed(key: str) -> bool:
    """传递的宿主环境键白名单过滤：绝不把疑似凭据透传/写进包装器。"""
    upper = key.upper()
    return not any(marker in upper for marker in _UNSAFE_ENV_MARKERS)


def host_environment() -> dict:
    """本控制轮固定的宿主环境（显式传入；绝不含 token/凭据值）。

    显式解析的六个契约键**覆盖**继承值；其余 ``KANPILOT_*`` 覆盖原样透传
    （隔离夹具下核心读同一环境：worktrees 根、pi home、报告根等）。
    """
    env = {
        key: value for key, value in os.environ.items()
        if (key.startswith("KANPILOT_") or key == CREDENTIALS_ENV)
        and _env_key_allowed(key)
    }
    env["KANPILOT_DB"] = str(constants.default_db_path())
    env["KANPILOT_APP_SUPPORT"] = str(constants.app_support())
    env["KANPILOT_PLAN_ROOT"] = str(constants.plan_root())
    env["KANPILOT_REPOS_ROOT"] = str(constants.repos_root())
    env["KANPILOT_LEGACY_ROOT"] = str(constants.legacy_state_root())
    env[CREDENTIALS_ENV] = str(
        Path(env.get(CREDENTIALS_ENV) or DEFAULT_CREDENTIALS_PATH).expanduser()
    )
    return env


def _shell_quote(value: str) -> str:
    """POSIX 单引号引用（包装器固化路径用；内含单引号按 '\\'' 转义）。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _python_bin() -> str:
    executable = getattr(sys, "executable", None)
    if not isinstance(executable, str) or not executable:
        raise util.KanpilotError(
            "validation_failed", "无法确定核心的 Python 解释器路径（sys.executable 为空）")
    return executable


def stored_setting(conn, name: str) -> dict:
    """原始 settings 值（非 dict → 空 dict）。"""
    value = db.get_setting(conn, name)
    return value if isinstance(value, dict) else {}


def _pi_desktop_block(value) -> Optional[dict]:
    """五字段块规范化（复用 ``exec_config`` 的跨角色共享实现）。

    共享实现缺失时回落等价本地规则：只认契约五字段的非空字符串，
    非 dict/空块 → None（=未保存）。
    """
    merge = getattr(exec_config, "merge_pi_desktop_block", None)
    if callable(merge):
        return merge(value)
    if not isinstance(value, dict):
        return None
    block = {
        key: value[key]
        for key in constants.EXEC_PI_DESKTOP_KEYS
        if isinstance(value.get(key), str) and value[key]
    }
    return block or None
def review_round_config(conn, config_override=None) -> Tuple[str, Optional[dict]]:
    """review 控制端来源：``settings.review_config``（§5.24.1/§5.24.5 / T7.1 / T7.3）。"""
    from . import review_config  # 局部导入：避免与 review_config 的导入顺序耦合

    loaded = config_override if config_override is not None else review_config.load(conn) or {}
    raw = config_override if config_override is not None else stored_setting(conn, "review_config")
    value = loaded.get("backend")
    if value is None:
        value = raw.get("backend")
    backend = (
        value if value in constants.REVIEW_BACKENDS
        else constants.REVIEW_BACKEND
    )
    block = _pi_desktop_block(loaded.get("pi_desktop"))
    if block is None:
        block = _pi_desktop_block(raw.get("pi_desktop"))
    if backend == "pi_desktop" and block is not None:
        resolved, _err = review_config.resolve_runtime_config(conn, block)
        if resolved is not None:
            block = resolved
    return backend, block


def validate_block(block: Optional[dict]) -> Optional[str]:
    """五字段 + 连接文件 + 权限模式校验（复用 exec_config 冻结规则）。"""
    return exec_config.validate_config(
        {"backend": "pi_desktop", "pi_desktop": block}, None
    )


def active_round(conn, stage: str) -> Optional[dict]:
    """需要收尾/人工处置的 Plus 控制轮（阻止同阶段重复启动）。

    判据（§5.24.4 单飞扩展）：
    - 未写 ``finished_at``（控制轮进程在途）→ 活跃；
    - ``send_state == "unknown"``（**发送结果不明**：指令可能已落地）→ 即使进程
      已退出也保留人工阻塞，同一阶段禁止新控制轮重复发送，只走只读恢复；
    - 已结束且 ``sent``/``not_sent`` 的轮次不阻塞新轮（明确结束的失败可再跑）。

    只认 ``backend=="pi_desktop"``；pi 轮次仍由既有 worker PID 单飞（本函数不
    参与）。
    """
    entry = stored_setting(conn, "executor_runs").get(stage)
    if not isinstance(entry, dict):
        return None
    if entry.get("backend") != "pi_desktop":
        return None
    if entry.get("send_state") == "unknown":
        return entry
    if entry.get("finished_at"):
        return None
    return entry


# --------------------------------------------------------------- 产物写入

def invocation_dir(stage: str, invocation_id: str) -> Path:
    """``<APP_SUPPORT>/executor-sessions/<stage>/<invocation_id>/``。"""
    path = (
        constants.app_support() / "executor-sessions" / stage / invocation_id
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


class EventLog:
    """events.jsonl 追加器（seq 单调；每行 flush）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seq = 0
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.seq = max(self.seq, int(event.get("seq") or 0))

    def emit(self, etype: str, ts: str, **fields) -> dict:
        self.seq += 1
        event = {"v": 1, "seq": self.seq, "ts": ts, "type": etype, **fields}
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
        return event


def base_job(stage: str, invocation_id: str, block: dict) -> dict:
    return {
        "v": 1,
        "stage": stage,
        "invocation_id": invocation_id,
        "backend": "pi_desktop",
        "executor": EXECUTOR_IDS.get(stage),
        "session_id": None,
        "turn_id": None,
        "send_state": "not_sent",
        "config": dict(block),
        "pid": os.getpid(),
        "status": "running",
        "started_at": util.now_iso(),
        "ended_at": None,
        "exit_code": None,
        "detail": None,
    }


# --------------------------------------------------------------- 只读转录

def read_transcript(client, session_id: str,
                    page_limit: int = _PAGE_LIMIT
                    ) -> Tuple[list, bool, Optional[str]]:
    """只读整段转录：沿 ``messageBefore`` 物理游标翻页到 0。

    返回 ``(messages, complete, detail)``；页链断裂/页数超限/session 身份不符
    时 ``complete=False``（detail 记原因），调用方不得据此判成功。
    """
    pages: list = []           # [(start, messages)]，新 → 旧
    boundary = None
    limit = page_limit
    complete = False
    detail = None
    result = None
    try:
        for _ in range(_MAX_PAGES_PER_READ):
            while True:
                result = client.session_get(
                    session_id, message_before=boundary, message_limit=limit,
                )
                if not result.get("truncated"):
                    break
                if limit <= _PAGE_MIN_LIMIT:
                    return [], False, "transcript_incomplete: 单页超过控制面结果上限"
                limit = max(_PAGE_MIN_LIMIT, limit // 2)
            page = result.get("session")
            if not isinstance(page, dict):
                return [], False, "transcript_incomplete: pi_session_get 未返回 session"
            if page.get("id") not in (None, session_id):
                return [], False, "transcript_incomplete: 转录会话身份不符"
            rows = page.get("messages")
            if not isinstance(rows, list):
                return [], False, "transcript_incomplete: messages 不是数组"
            if not rows and boundary is None:
                return [], True, None         # 空会话
            start, end = page.get("messageStart"), page.get("messageEnd")
            more = page.get("hasMoreBefore")
            broken = (
                type(start) is not int or type(end) is not int
                or not isinstance(more, bool)
                or start < 0 or end < start
                or (rows and end - start != len(rows))
                or (boundary is not None and (end != boundary or start >= boundary))
                or (boundary is None and page.get("hasMoreAfter") is True)
            )
            if broken:
                return [], False, "transcript_incomplete: 分页边界不衔接"
            pages.append((start, rows))
            boundary = start
            if start == 0:
                complete = True
                break
            if not more:
                return [], False, "transcript_incomplete: 历史前缀不完整"
            # Every physical page has a fresh size budget. Large messages on
            # this page must not shrink all older ordinary pages permanently.
            limit = page_limit
        else:
            return [], False, "transcript_incomplete: 页数超过上限"
    except util.KanpilotError as exc:
        detail = f"transcript_read_failed: {exc.message}"
        # 已采页仍按证据返回（不判成功由调用方决定）。
        messages: list = []
        for _start, rows in reversed(pages):
            messages.extend(rows)
        return messages, False, detail
    messages = []
    for _start, rows in reversed(pages):
        messages.extend(rows)
    return messages, complete, detail


def message_text(message: dict) -> str:
    """UiMessage → 纯文本（字符串正文 / 文本块数组）。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                blocks.append(block["text"])
        return "\n".join(blocks)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def last_assistant_text(messages: list) -> Optional[str]:
    """最终 assistant 答复文本（无 assistant 消息 → None）。"""
    text = None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if (message.get("role") or "") == "assistant":
            text = message_text(message)
    return text


def strict_json_object(text: Optional[str]) -> Tuple[Optional[dict], Optional[str]]:
    """严格解析模型最终答复为 JSON 对象（允许整体包一层代码栅栏）。

    非纯 JSON、非对象、或混入额外文字 → 失败（绝不从自由文本里猜结构）。
    """
    if not isinstance(text, str) or not text.strip():
        return None, "最终答复为空"
    trimmed = text.strip()
    if trimmed.startswith("```"):
        lines = trimmed.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return None, "代码栅栏不完整"
        trimmed = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(trimmed)
    except ValueError as exc:
        return None, f"最终答复不是合法 JSON：{exc}"
    if not isinstance(value, dict):
        return None, "最终答复 JSON 不是对象"
    return value, None


# --------------------------------------------------------------- 会话交互

def turn_status(client, session_id: str, turn_id: str
                 ) -> Tuple[Optional[str], Optional[str]]:
    """pi_turn_get 持久状态；身份不符/读不到 → (None, 原因)。"""
    try:
        record = client.turn_get(session_id, turn_id)
    except util.KanpilotError as exc:
        return None, exc.message
    if not isinstance(record, dict):
        return None, "pi_turn_get 返回不是对象"
    if record.get("sessionId") not in (None, session_id) or record.get(
        "turnId"
    ) not in (None, turn_id):
        return None, "turn 身份不符"
    status = record.get("status")
    return (status if isinstance(status, str) else None), None


def agent_status(client, session_id: str) -> Optional[dict]:
    try:
        view = client.agent_status(session_id)
    except util.KanpilotError:
        return None
    if not isinstance(view, dict):
        return None
    status = view.get("status", view)
    if not isinstance(status, dict) or not isinstance(status.get("isRunning"), bool):
        return None
    return status


def prove_started(client, session_id: str, turn_id: str) -> Tuple[bool, Optional[str]]:
    """启动证明：``pi_turn_get`` 或 ``agent_status.currentTurnId`` 观察到位。

    ``error``/``aborted`` 是**终止证据**，不是启动证明（与任务执行 worker 同
    口径）：证明失败 → ``send_state`` 保持 unknown，绝不重发。
    """
    deadline = time.monotonic() + _PROOF_WAIT_SECONDS
    last = None
    while True:
        status, error = turn_status(client, session_id, turn_id)
        if status == "running" or status == "completed":
            return True, None
        if status in ("error", "aborted"):
            return False, f"turn 终态 {status}（启动未获证明）"
        if status is None and error:
            last = error
        view = agent_status(client, session_id)
        if view and view.get("currentTurnId") == turn_id and view.get(
            "isRunning"
        ) is True:
            return True, None
        if view and view.get("currentTurnId") not in (None, turn_id):
            return False, "turn_mismatch: 会话当前轮次不是本控制轮"
        if time.monotonic() >= deadline:
            return False, f"startup_unproven: {last or '宽限内未见 turn 启动证据'}"
        _sleep(_POLL_SECONDS)


def stop_turn(client, session_id: str, turn_id: str) -> dict:
    """超时/中断的有界停止：stop → 核验 → abort；响应丢失报 unknown。"""
    detail = {"stop_acked": False, "aborted": False, "terminal": None,
              "unknown": None}
    try:
        client.agent_stop(session_id)
        detail["stop_acked"] = True
    except util.KanpilotError as exc:
        detail["unknown"] = f"pi_agent_stop 响应不明：{exc.message}"
    deadline = time.monotonic() + _STOP_VERIFY_SECONDS
    status, _error = turn_status(client, session_id, turn_id)
    while status not in _TERMINAL_TURN_STATUSES and time.monotonic() < deadline:
        _sleep(_POLL_SECONDS)
        status, _error = turn_status(client, session_id, turn_id)
    if status in _TERMINAL_TURN_STATUSES:
        detail["terminal"] = status
        return detail
    try:
        client.agent_abort(session_id)
        detail["aborted"] = True
    except util.KanpilotError as exc:
        detail["unknown"] = f"pi_agent_abort 响应不明：{exc.message}"
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    status, _error = turn_status(client, session_id, turn_id)
    while status not in _TERMINAL_TURN_STATUSES and time.monotonic() < deadline:
        _sleep(_POLL_SECONDS)
        status, _error = turn_status(client, session_id, turn_id)
    if status in _TERMINAL_TURN_STATUSES:
        detail["terminal"] = status
    return detail


# --------------------------------------------------------------- 控制轮

def _emit_transcript(log: EventLog, messages: list) -> None:
    """把转录映射为事件（正文截断；全文另落 transcript.json）。"""
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = message_text(message)
        if role == "user":
            log.emit("user", util.now_iso(), content=text[:_MAX_EVENT_TEXT])
        elif role == "assistant":
            log.emit("assistant_message", util.now_iso(),
                     content=text[:_MAX_EVENT_TEXT])
        elif role == "toolResult":
            log.emit("tool_result", util.now_iso(),
                     name=message.get("toolName") or message.get("name"),
                     is_error=bool(message.get("isError")),
                     exit_code=message.get("exitCode"))


def _dump_transcript(directory: Path, messages: list, complete: bool,
                     detail: Optional[str]) -> None:
    write_json(
        Path(directory) / "transcript.json",
        {
            "complete": bool(complete),
            "detail": detail,
            "messages": messages or [],
        },
    )


def record_run_entry(record_run: Callable, stage: str, job: dict, **extra) -> None:
    """把控制轮状态合并进 settings.executor_runs[stage]（保留旧字段）。"""
    fields = {
        "pid": job.get("pid"),
        "started_at": job.get("started_at"),
        "log": job.get("log"),
        "exit_code": job.get("exit_code"),
        "finished_at": job.get("ended_at"),
        "backend": "pi_desktop",
        "session_id": job.get("session_id"),
        "turn_id": job.get("turn_id"),
        "invocation_id": job.get("invocation_id"),
        "config": job.get("config"),
        "send_state": job.get("send_state"),
    }
    fields.update(extra)
    record_run(**fields)
def _open_client(block: dict, timeout: float, *, role: str,
                 invocation_id: str, project_path: Path,
                 request_id: Optional[str] = None,
                 run_id: Optional[str] = None,
                 task_id: Optional[str] = None,
                 required_tools=constants.PI_DESKTOP_CONTROLLER_TOOLS,
                 lease_seconds: Optional[float] = None,
                 read_only: bool = False):
    """Connect through the registered KaneoPilot runtime scope."""
    client = runtime_client.RuntimeClient(
        role=role, connection_file=block["connection_file"],
        project_path=str(project_path), runtime_config=block,
        invocation_id=invocation_id, request_id=request_id,
        run_id=run_id, task_id=task_id, timeout=timeout,
        lease_seconds=lease_seconds, read_only=read_only,
    )
    client.connect(required_tools=required_tools)
    return client
