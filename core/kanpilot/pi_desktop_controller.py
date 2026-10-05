"""PI-Desktop-Plus 控制端 runner（protocol.md §5.24.4，v4 T6.3）。

``executor_runner --stage dispatch|monitor`` 在 ``exec_config.controller_backend
== "pi_desktop"`` 时改走本模块：在 Plus 本机 MCP 控制面建独立 ``mode="agent"``
控制会话运行既有角色 prompt（``core/executor_prompts/<stage>.md``），
**不再**拉起 ``pi -p``。确定性职责全部留在 KaneoPilot，状态写入仍只经既有
kanpilot CLI（模型经本模块生成的固定包装器调用）：

- **宿主环境显式传入**：生成固定本地包装器 ``<invocation>/kanpilot-cli``，显式
  注入本控制轮固定的 ``KANPILOT_DB``/``KANPILOT_APP_SUPPORT``/
  ``KANPILOT_PLAN_ROOT``/``KANPILOT_REPOS_ROOT``/``KANPILOT_LEGACY_ROOT``/
  ``KANEO_CREDENTIALS_PATH`` 与核心脚本路径——Plus 的 shell 工具在宿主进程
  执行、**不继承** runner 环境，故不依赖继承；包装器只启动既有 CLI，不含 token。
- **产物与检查点**：控制轮以 ``invocation_id`` 区分，产物落
  ``<APP_SUPPORT>/executor-sessions/<stage>/<invocation_id>/``（job.json/
  events.jsonl/transcript.json + 包装器）；``settings.executor_runs[stage]``
  增 ``backend/session_id/turn_id/invocation_id/config/send_state``，保留旧
  ``pid/started_at/log/exit_code/finished_at``。控制 ``invocation_id`` 与任务
  执行 ``run_id`` 不可混用。
- **发送与恢复**：发送前持久 ``send_state="unknown"``；响应不明**不重发**；
  已知 session/turn 只重连只读读取；``stale_run`` 由角色 prompt 处理（本模块
  不代角色 claim/report）；单飞同时看既有 worker PID 与持久活跃/unknown 控制轮。
- **不越权判定**：Plus 控制会话 ``completed`` 只表示**控制模型回合结束**；请求
  是否成功以核心 claim/report 与请求结果为准——本模块不写任何任务状态、不把
  排队标成已开始。
"""

from __future__ import annotations

import json
import os
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
_PAGE_MIN_LIMIT = 5
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
    return executable if isinstance(executable, str) and executable else "python3"


def write_wrapper(directory: Path, stage: str) -> Path:
    """生成固定本地包装器：只注入本轮固定环境并调用既有 CLI（无 token）。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / WRAPPER_NAME
    lines = [
        "#!/bin/sh",
        f"# KaneoPilot 固定包装器（Pi-Desktop-Plus 控制端 stage={stage}）",
        "# 只注入本控制轮固定的宿主环境并调用既有 kanpilot CLI；不含任何凭据值。",
    ]
    for key in sorted(host_environment()):
        lines.append(f"export {key}={_shell_quote(host_environment()[key])}")
    lines.append(
        "exec "
        + " ".join(
            (
                _shell_quote(_python_bin()),
                _shell_quote(str(constants.core_root() / "kanpilot.py")),
                '"$@"',
            )
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def host_environment_block(wrapper: Path, invocation_dir: Path,
                           stage: str) -> str:
    """追加到控制轮 prompt 的显式宿主环境说明（含包装器与固定键值）。"""
    lines = [PROMPT_BLOCK_MARKER, ""]
    lines.append(
        "- 本控制轮的固定 CLI 入口（已注入下列宿主环境；与既有 CLI 调用形态"
        "完全一致，工作目录为本轮独立控制目录）："
    )
    lines.append(f"  `{wrapper}`")
    lines.append(
        f"- 调用形态：`{wrapper} <command> --json '<payload>'`；"
        "不要另行 export 环境、不要改用宿主默认值、不要猜测路径。"
    )
    lines.append("- 本轮固定宿主环境：")
    for key in sorted(host_environment()):
        lines.append(f"  - `{key}={host_environment()[key]}`")
    lines.append(
        f"- 独立控制目录（本轮产物；不改 Plus 全局当前项目）：`{invocation_dir}`"
    )
    lines.append(f"- 本端 executor id：`{EXECUTOR_IDS.get(stage, 'kaneo-dispatch')}`")
    lines.append(
        "- KaneoPilot 不代批 Plus 工具权限：工具确认请在 Plus 内处理；"
        "权限等待期间不要重发同一指令、不要另建会话。"
    )
    lines.append(
        "- 心跳（§5.24.4）：`claim` 后每 ≤ `min(lease_seconds/3, 60)` 秒调一次 "
        "`heartbeat`（至少每 60 秒）；被拒 `stale_run`/`not_found` 即停止续租并"
        "正常收尾该项——**不**写失败、**不**重发、**不**新建会话。"
    )
    return "\n".join(lines) + "\n"



def point_cli_at_wrapper(prompt: str, wrapper: Path) -> str:
    """把 prompt 里记录的调度核心脚本路径换成固定包装器路径。

    角色 prompt 由 ``executor_runner.render_prompt`` 渲染时已把 ``{KANPILOT}``
    展开为 ``<core>/kanpilot.py``；控制轮改为指向包装器，模型便**总是**经包装器
    取得本轮固定宿主环境，而不是依赖 Plus 宿主进程继承的环境。
    """
    core_script = str(constants.core_root() / "kanpilot.py")
    return prompt.replace(core_script, str(wrapper))


def render_mcp_prompt(stage: str, invocation_id: str) -> str:
    """Load the Plus-only task-MCP prompt and bind its local invocation identity."""
    if stage not in STAGES:
        raise util.KanpilotError("validation_failed", f"Plus 控制端 stage 无效：{stage}")
    path = constants.core_root() / "executor_prompts" / f"{stage}_mcp.md"
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise util.KanpilotError("not_found", f"Plus MCP 专用提示词不可用：{path}: {exc}")
    return (
        template.rstrip() + "\n\n## 本轮 invocation\n"
        f"`{invocation_id}`\n"
        "此标识由 KaneoPilot 固定，不是任务、请求或 run 身份；具体身份只以"
        "已挂接 task MCP 工具返回值为准。\n"
    )


# --------------------------------------------------------------- 配置/单飞


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


def controller_round_config(conn) -> Tuple[str, Optional[dict]]:
    """dispatch/monitor 控制端来源：``settings.exec_config``（§5.24.1/§5.24.4 / T7.1 / T7.3）。"""
    loaded = exec_config.load(conn)
    raw = stored_setting(conn, "exec_config")
    value = loaded.get(constants.CONTROLLER_BACKEND_KEY)
    if value is None:
        value = raw.get(constants.CONTROLLER_BACKEND_KEY)
    backend = (
        value if value in constants.CONTROLLER_BACKENDS
        else constants.CONTROLLER_BACKEND
    )
    block = _pi_desktop_block(loaded.get("pi_desktop"))
    if block is None:
        block = _pi_desktop_block(raw.get("pi_desktop"))
    if backend == "pi_desktop" and block is not None:
        resolved, _err = exec_config.resolve_runtime_config(conn, block)
        if resolved is not None:
            block = resolved
    return backend, block


def review_round_config(conn) -> Tuple[str, Optional[dict]]:
    """review 控制端来源：``settings.review_config``（§5.24.1/§5.24.5 / T7.1 / T7.3）。"""
    from . import review_config  # 局部导入：避免与 review_config 的导入顺序耦合

    loaded = review_config.load(conn) or {}
    raw = stored_setting(conn, "review_config")
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
                or (boundary is not None and end != boundary)
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
    status = view.get("status")
    return status if isinstance(status, dict) else None


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


def run_stage(stage: str, *, conn, db_path, prompt: str, timeout: float,
              log_path: Path, record_run: Callable, resume: bool = False) -> int:
    """跑一个 Plus 控制轮；返回进程退出码（0=控制回合正常结束）。"""
    if stage not in STAGES:
        print(f"unknown stage: {stage}", file=sys.stderr)
        return 2

    if resume:
        active = active_round(conn, stage)
        if active is None:
            print(f"executor {stage} 没有可恢复的 Plus 控制轮", file=sys.stderr)
            return 1
        if active.get("backend") != "pi_desktop":
            print(f"executor {stage} 历史控制轮后端不可恢复：{active.get('backend')}", file=sys.stderr)
            return 1
        block = active.get("config")
        error = validate_block(block)
        if error is not None:
            print(f"恢复失败：持久配置快照缺失或无效：{error}", file=sys.stderr)
            return 1
        return _resume_round(
            stage, conn=conn, block=block, prompt=prompt, timeout=timeout,
            log_path=log_path, record_run=record_run,
        )

    backend, block = controller_round_config(conn)
    if backend != "pi_desktop":
        print(
            f"executor {stage} 控制端后端不是 pi_desktop（{backend}）："
            "不在此分支运行",
            file=sys.stderr,
        )
        return 2
    error = validate_block(block)
    if error is not None:
        print(f"pi_desktop_controller_unavailable：{error}", file=sys.stderr)
        return 1

    active = active_round(conn, stage)
    if active is not None:
        print(
            f"executor {stage} 已有未收尾的 Plus 控制轮"
            f"（invocation={active.get('invocation_id')} "
            f"send_state={active.get('send_state')} "
            f"turn={active.get('turn_id')} pid={active.get('pid')}）："
            "不重复启动；无已知 turn 的 unknown 保留人工阻塞（只读恢复入口）",
            file=sys.stderr,
        )
        return 1

    return _control_round(
        stage, conn=conn, block=block, prompt=prompt, timeout=timeout,
        log_path=log_path, record_run=record_run,
    )


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


def _control_round(stage: str, *, conn, block: dict, prompt: str,
                   timeout: float, log_path: Path, record_run: Callable) -> int:
    invocation_id = util.new_uuid()
    directory = invocation_dir(stage, invocation_id)
    job_path = directory / "job.json"
    log = EventLog(directory / "events.jsonl")
    job = base_job(stage, invocation_id, block)
    job["log"] = str(log_path)
    job["request_id"] = None
    write_json(job_path, job)
    record_run_entry(record_run, stage, job)

    try:
        control_prompt = render_mcp_prompt(stage, invocation_id)
    except util.KanpilotError as exc:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": f"mcp_prompt_unavailable: {exc.message}",
        })
        write_json(job_path, job)
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        return 1
    try:
        client = _open_client(
            block, timeout=min(timeout, 30), role=stage,
            invocation_id=invocation_id, project_path=directory,
            lease_seconds=timeout,
        )
    except util.KanpilotError as exc:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": f"pi_desktop_controller_unavailable: {exc.message}",
        })
        write_json(job_path, job)
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        print(f"executor {stage} 控制面连接失败：{exc.message}", file=sys.stderr)
        return 1

    job["scope_id"] = client.scope_id
    write_json(job_path, job)
    record_run_entry(record_run, stage, job)

    created = _create_session(client, block, directory)
    if created is None:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": "pi_desktop_controller_unavailable: 建会话失败",
        })
        write_json(job_path, job)
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        return 1
    session_id, detail = created
    job["session_id"] = session_id
    job["send_state"] = "not_sent"
    write_json(job_path, job)
    record_run_entry(record_run, stage, job)

    if detail is not None:
        # 会话已建立但权限配置未获确认：不发送任何指令（不与未配置会话对话）。
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": f"pi_desktop_controller_unavailable: {detail}",
        })
        write_json(job_path, job)
        log.emit("system", util.now_iso(), content=job["detail"])
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        print(f"executor {stage} 会话权限配置失败：{detail}", file=sys.stderr)
        return 1

    # 发送前落 send_state=unknown（响应不明不重发）。
    job["send_state"] = "unknown"
    write_json(job_path, job)
    record_run_entry(record_run, stage, job)

    try:
        result = client.agent_prompt(session_id, control_prompt)
    except util.KanpilotError as exc:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": f"prompt_response_lost: {exc.message}",
            "send_state": (
                "not_sent" if exc.code in ("validation_failed",) else "unknown"
            ),
        })
        write_json(job_path, job)
        log.emit("system", util.now_iso(),
                 content=f"发送结果不明（不重发）：{exc.message}")
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        print(f"executor {stage} 发送结果不明（不重发）：{exc.message}",
              file=sys.stderr)
        return 1

    turn_id = result.get("turnId") if isinstance(result, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": "prompt_response_lost: 响应未含 turnId（不重发）",
            "send_state": "unknown",
        })
        write_json(job_path, job)
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        return 1
    job["turn_id"] = turn_id
    write_json(job_path, job)
    record_run_entry(record_run, stage, job)

    started, proof_detail = prove_started(client, session_id, turn_id)
    if not started:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": proof_detail or "startup_unproven",
            "send_state": "unknown",
        })
        write_json(job_path, job)
        log.emit("system", util.now_iso(), content=job["detail"])
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        print(f"executor {stage} 启动证明失败：{job['detail']}", file=sys.stderr)
        return 1

    job["send_state"] = "sent"
    write_json(job_path, job)
    log.emit("session", util.now_iso(), session_id=session_id,
             turn_id=turn_id, backend="pi_desktop")
    log.emit("user", util.now_iso(), content=control_prompt[:_MAX_EVENT_TEXT])
    record_run_entry(record_run, stage, job)

    return _observe_round(
        stage, client=client, job=job, job_path=job_path, log=log,
        directory=directory, timeout=timeout, record_run=record_run,
    )


def _create_session(client, block: dict, directory: Path):
    """``mode="agent"`` 控制会话（模型/权限取 exec_config.pi_desktop）。

    返回 ``(session_id, detail)``；建会话失败 → None；权限配置未确认 → 非空
    detail（调用方不发送任何指令）。
    """
    try:
        created = client.session_create(
            str(directory), mode=constants.PI_DESKTOP_CONTROLLER_MODE,
            provider_id=block.get("provider_id"),
            model_id=block.get("model_id"),
            thinking_level=block.get("thinking_level"),
            title=f"KaneoPilot 控制端 · {directory.name}",
        )
    except util.KanpilotError as exc:
        print(f"pi_session_create 失败：{exc.message}", file=sys.stderr)
        return None
    session_id = None
    if isinstance(created, dict):
        session = created.get("session")
        if isinstance(session, dict):
            session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            session_id = created.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        print("pi_session_create 未返回 session id", file=sys.stderr)
        return None
    detail = None
    try:
        client.session_configure(
            session_id, mode=constants.PI_DESKTOP_CONTROLLER_MODE,
            permission_mode=block.get("permission_mode"),
        )
    except util.KanpilotError as exc:
        detail = f"pi_session_configure 失败：{exc.message}"
    if detail is None:
        try:
            client.attach_mcp(session_id)
        except util.KanpilotError as exc:
            detail = f"kp_runtime_attach_mcp 失败：{exc.message}"
    return session_id, detail


def _observe_round(stage: str, *, client, job: dict, job_path: Path,
                   log: EventLog, directory: Path, timeout: float,
                   record_run: Callable) -> int:
    """观察控制回合到终态；权限等待/超时绝不判成功。"""
    session_id, turn_id = job["session_id"], job["turn_id"]
    deadline = time.monotonic() + timeout
    read_errors = 0
    permission_seen = False
    outcome, detail = None, None
    while True:
        status, error = turn_status(client, session_id, turn_id)
        if status == "completed":
            outcome = "completed"
            break
        if status in ("error", "aborted"):
            outcome = "failed"
            detail = f"turn {status}"
            break
        if status is None and error:
            read_errors += 1
            if read_errors == 1:
                log.emit("system", util.now_iso(),
                         content=f"turn 读取失败（重试只读）：{error}")
            if read_errors >= _MAX_READ_ERRORS:
                outcome = "failed"
                detail = f"turn_unreadable: {error}"
                break
        else:
            read_errors = 0
        view = agent_status(client, session_id)
        if view and view.get("pendingToolConfirmations") and not permission_seen:
            permission_seen = True
            log.emit(
                "system", util.now_iso(),
                content=(
                    "permission_waiting: Plus 内有待处理的工具权限确认"
                    "（不代批、不重发；等待不算成功）"
                ),
            )
            job["permission_waiting"] = True
        if time.monotonic() >= deadline:
            outcome = "timeout"
            break
        _sleep(_POLL_SECONDS)

    messages, complete, transcript_detail = read_transcript(
        client, session_id
    )
    if messages:
        _emit_transcript(log, messages)
    _dump_transcript(directory, messages, complete, transcript_detail)

    if outcome == "completed":
        job.update({"status": "finished", "exit_code": 0, "ended_at": util.now_iso(),
                    "detail": None,
                    "transcript_complete": bool(complete)})
        log.emit(
            "system", util.now_iso(),
            content=(
                "控制回合结束（Plus 侧 turn completed）：只表示控制模型回合"
                "结束，请求是否成功以核心 claim/report 与请求结果为准"
            ),
        )
        log.emit("terminal", util.now_iso(), outcome="completed",
                 reason="control_turn_completed")
    elif outcome == "timeout":
        stopped = stop_turn(client, session_id, turn_id)
        reason = "permission_waiting" if job.get("permission_waiting") else "timeout"
        job.update({
            "status": "failed", "exit_code": -2, "ended_at": util.now_iso(),
            "detail": (
                f"{reason}（{timeout}s）；停止结果："
                f"terminal={stopped.get('terminal')} "
                f"aborted={stopped.get('aborted')} "
                f"unknown={stopped.get('unknown')}"
            ),
            "transcript_complete": bool(complete),
            "stop_result": stopped,
        })
        # 发送已获证明（send_state 保持 sent）：超时是明确结束的失败，不保留
        # 人工阻塞；权限等待只作为证据记在 job/事件里（等待不算成功）。
        log.emit("terminal", util.now_iso(), outcome="failed", reason=reason)
        outcome = "failed"
    else:
        job.update({
            "status": "failed", "exit_code": 1, "ended_at": util.now_iso(),
            "detail": detail or "control_turn_failed",
            "transcript_complete": bool(complete),
        })
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=detail or "control_turn_failed")

    write_json(job_path, job)
    record_run_entry(record_run, stage, job)
    if job["status"] == "finished":
        print(f"executor {stage} Plus 控制回合正常结束：{job['session_id']}",
              file=sys.stderr)
        return 0
    print(f"executor {stage} Plus 控制回合未成功：{job['detail']}",
          file=sys.stderr)
    return 1


def _resume_round(stage: str, *, conn, block: dict, prompt: str, timeout: float,
                  log_path: Path, record_run: Callable) -> int:
    """只读恢复：不建会话、不重发 prompt，按持久 turn 收尾。"""
    active = active_round(conn, stage)
    if active is None:
        print(f"executor {stage} 没有可恢复的 Plus 控制轮", file=sys.stderr)
        return 1
    invocation_id = active.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        print(f"executor {stage} 运行记录缺 invocation_id，无法恢复", file=sys.stderr)
        return 1
    directory = invocation_dir(stage, invocation_id)
    job_path = directory / "job.json"
    if not job_path.is_file():
        print(f"executor {stage} 控制轮现场缺失：{job_path}", file=sys.stderr)
        return 1
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except ValueError:
        print(f"executor {stage} 控制轮 job.json 不可解析", file=sys.stderr)
        return 1
    if not isinstance(job, dict) or job.get("stage") != stage:
        print(f"executor {stage} 控制轮现场身份不符", file=sys.stderr)
        return 1
    if job.get("send_state") != "unknown" and job.get("status") in (
        "finished", "failed",
    ):
        # 明确结束（发送已获证明，或从未发送成功）：无待恢复现场，不重连不重发。
        print(
            f"executor {stage} 控制轮已收尾（{job.get('status')}）：无需恢复",
            file=sys.stderr,
        )
        return 0 if job.get("status") == "finished" else 1
    session_id, turn_id = job.get("session_id"), job.get("turn_id")
    if not isinstance(session_id, str) or not session_id or not isinstance(
        turn_id, str
    ) or not turn_id:
        print(
            f"executor {stage} send_state={job.get('send_state')} 且无已知 "
            "session/turn：不自动重发，保留人工阻塞",
            file=sys.stderr,
        )
        return 1

    log = EventLog(directory / "events.jsonl")
    job["log"] = str(log_path)
    job["resumed_at"] = util.now_iso()
    job["resume"] = True
    round_block = job.get("config")
    if (not isinstance(round_block, dict) or not round_block.get("connection_file")
            or validate_block(round_block) is not None):
        print("恢复失败：job 缺少有效五字段配置快照，不读取当前配置替代", file=sys.stderr)
        return 1
    try:
        if job.get("scope_id"):
            expected_scope = runtime_client.stable_scope_id(
                stage, invocation_id=job.get("invocation_id"),
                task_id=job.get("task_id"), run_id=job.get("run_id"),
            )
            if expected_scope != job["scope_id"]:
                raise util.KanpilotError("conflict", "job scope_id 与 invocation 身份不匹配")
            client = _open_client(
                round_block, timeout=min(timeout, 30), role=stage,
                invocation_id=str(job.get("invocation_id") or ""),
                project_path=directory,
                request_id=job.get("request_id"), run_id=job.get("run_id"),
                task_id=job.get("task_id"), lease_seconds=timeout,
                read_only=True,
            )
        else:
            client = runtime_client.HistoricalReadClient(
                round_block["connection_file"], timeout=min(timeout, 30))
            client.connect(required_tools=constants.PI_DESKTOP_CONTROLLER_TOOLS)
    except util.KanpilotError as exc:
        job["detail"] = f"pi_desktop_controller_unavailable: {exc.message}"
        write_json(job_path, job)
        print(f"executor {stage} 恢复失败（只读重连）：{exc.message}",
              file=sys.stderr)
        return 1
    log.emit("system", util.now_iso(),
             content="只读恢复：重连既有控制会话，不重发 prompt")
    started, proof_detail = prove_started(client, session_id, turn_id)
    if not started:
        job.update({
            "status": "failed", "ended_at": util.now_iso(), "exit_code": 1,
            "detail": proof_detail or "startup_unproven",
        })
        write_json(job_path, job)
        log.emit("terminal", util.now_iso(), outcome="failed",
                 reason=job["detail"])
        record_run_entry(record_run, stage, job)
        return 1
    if job.get("send_state") != "sent":
        job["send_state"] = "sent"
        job["send_state_recovered_from"] = active.get("send_state")
    write_json(job_path, job)
    record_run_entry(record_run, stage, job)
    return _observe_round(
        stage, client=client, job=job, job_path=job_path, log=log,
        directory=directory, timeout=timeout, record_run=record_run,
    )
