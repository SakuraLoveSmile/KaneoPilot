"""pi 轮次启动参数与事件映射（protocol.md §5.18，v1.6）。

纯 Python 无第三方依赖。pi_worker 以 ``pi -p --mode json`` 拉起独立会话，
stdout 输出结构化 JSONL 事件（docs/rpc.md 事件词汇）；本模块负责：

- ``launcher_argv(spec, agent_md_path)``：组装 pi 子进程 argv
  （``--model <provider>/<model> --thinking <level> --system-prompt <md>
  --session-dir <run_dir>/pi-sessions -p --mode json``）；
- ``child_env(spec)``：子进程环境（透传父进程环境 + PI_HOME 指向
  spec.pi_home；不注入凭据值）；
- ``map_event(message)``：pi JSONL 行 → ``(etype, ts, fields)``，未知/噪声
  行返回 None（OSC 序列、非 JSON 行、扩展 UI 请求等静默丢弃）。

事件映射（→ events.jsonl 行 ``type``，字段随 ``fields``）：

- ``session``            → ``session``（session_id / session_title）
- user ``message_end``   → ``user``（content=文本块拼接）
- assistant ``message_end`` → ``assistant_message``（content=text+thinking 拼接）
- ``tool_execution_start`` → ``tool_call``（tool 名 + 参数 JSON）
- ``tool_execution_end``   → ``tool_result``；bash 类工具 → ``command_output``
  （exit_code 进 ``tool.exit_code``，stdout/stderr 拼接进 content）
- ``agent_end``           → ``turn_end``（worker 终态判定辅助；不写独立事件）

thinking/usage/message_update 增量事件不落盘（读取层以完整消息为准）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

# 命令类工具：其结果投影为 command_output（退出码入 tool.exit_code）。
_COMMAND_TOOLS = frozenset({"bash", "pwsh", "shell"})

# pi 事件 → 轮次事件 type（None = 不落盘，只参与 worker 内部判定）。
_EVENT_TYPE_MAP = {
    "session": "session",
    "message_end": None,  # 按 message.role 分流（user/assistant/toolResult）
    "tool_execution_start": "tool_call",
    "tool_execution_end": None,  # bash → command_output，其余 → tool_result
    "agent_end": "turn_end",
}


def launcher_argv(spec: dict, agent_md_path: Path) -> list:
    """组装 pi 子进程 argv（protocol §5.18.3，v1.6）。

    ``--system-prompt`` 传 agent.md 文件**内容**（pi 接受文本，不接受路径；
    由调用方读文件后注入 argv）。``--session-dir`` 落在轮次目录下保证会话
    文件随轮次归档。"""
    from . import constants

    config = spec.get("config") or {}
    provider = config.get("provider") or ""
    model = config.get("model") or ""
    model_arg = f"{provider}/{model}" if provider else model
    effort = config.get("reasoning_effort") or ""

    argv = [
        constants.pi_bin(),
        "-p",                       # 非交互：处理 prompt 后退出
        "--mode", "json",           # stdout 结构化 JSONL 事件
        "--session-dir", str(Path(spec["runs_dir"]) / "pi-sessions"),
        "--no-extensions",          # 隔离：不加载 pi-maestro 等扩展
        "--no-skills",
        "--no-context-files",       # 不读 worktree 内 AGENTS.md 等（指令自带）
    ]
    if model_arg:
        argv += ["--model", model_arg]
    if effort:
        argv += ["--thinking", effort]
    system_prompt = spec.get("system_prompt")
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    elif agent_md_path is not None and Path(agent_md_path).is_file():
        argv += [
            "--system-prompt",
            Path(agent_md_path).read_text(encoding="utf-8"),
        ]
    argv.append(spec.get("instruction") or "")
    return argv


def child_env(spec: dict) -> dict:
    """子进程环境：透传父进程 + PI_HOME 指向隔离/真实 pi home。
    不注入凭据值——凭据由 pi 自身 ~/.pi/agent/auth.json 管理。"""
    env = dict(os.environ)
    pi_home = spec.get("pi_home")
    if pi_home:
        env["PI_HOME"] = str(pi_home)
    return env


def map_event(message: dict) -> Optional[Tuple[str, str, dict]]:
    """pi JSONL 行 → (etype, ts, fields)；未知/噪声 → None。

    ``ts`` 取事件自带毫秒时间戳（转 ISO）或调用方填当前时刻。
    """
    if not isinstance(message, dict):
        return None
    etype = message.get("type")

    if etype == "session":
        return ("session", _iso_ms(message.get("timestamp")), {
            "session_id": message.get("id"),
            "session_title": message.get("name") or message.get("cwd"),
        })

    if etype == "message_end":
        msg = message.get("message") or {}
        role = msg.get("role")
        ts = _iso_ms(msg.get("timestamp"))
        if role == "user":
            return ("user", ts, {"content": _text_of(msg.get("content"))})
        if role == "assistant":
            return ("assistant_message", ts, {
                "content": _assistant_text(msg.get("content")),
                "model": msg.get("model"),
                "stop_reason": msg.get("stopReason"),
            })
        if role == "toolResult":
            # 冗余兜底：tool_execution_end 已产出结果事件；这里不重复发。
            return None
        return None

    if etype == "tool_execution_start":
        args = message.get("args")
        return ("tool_call", _iso_ms(message.get("timestamp")), {
            "name": message.get("toolName"),
            "arguments": json.dumps(args, ensure_ascii=False, sort_keys=True)
            if isinstance(args, (dict, list)) else str(args or ""),
        })

    if etype == "tool_execution_end":
        name = message.get("toolName")
        result = message.get("result") or {}
        content = _tool_result_content(result)
        ts = _iso_ms(message.get("timestamp"))
        if name in _COMMAND_TOOLS:
            return ("command_output", ts, {
                "name": name,
                "input": json.dumps(
                    message.get("args") or {}, ensure_ascii=False,
                    sort_keys=True,
                ),
                "output": content,
                "exit_code": _exit_code_of(result),
            })
        return ("tool_result", ts, {
            "name": name, "output": content,
        })

    if etype == "agent_end":
        # worker 据此判定回合结束；不落独立事件（terminal 由 worker 写）。
        return ("turn_end", _iso_ms(message.get("timestamp")), {
            "will_retry": bool(message.get("willRetry")),
        })

    return None


# ------------------------------------------------------------ 辅助

def _iso_ms(value) -> str:
    """毫秒 epoch 或 ISO 字符串 → ISO（无值给空串，由 worker 补当前时刻）。"""
    if isinstance(value, (int, float)):
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).isoformat()
        except (ValueError, OSError):
            return ""
    if isinstance(value, str) and value:
        return value
    return ""


def _text_of(content) -> str:
    """content 块列表 → 拼接文本（text/thinking 均取）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("thinking")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _assistant_text(content) -> str:
    """assistant 消息正文：text + thinking 拼接（thinking 前缀标注）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking" and block.get("thinking"):
            parts.append(f"[thinking] {block['thinking']}")
        elif block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts)


def _tool_result_content(result: dict) -> str:
    """tool_execution_end.result → 展示文本（content 块拼接 + stderr）。"""
    if not isinstance(result, dict):
        return str(result or "")
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr:
        parts.append(stderr)
    return "\n".join(parts)


def _exit_code_of(result: dict) -> Optional[int]:
    """bash 类工具的退出码（result.exitCode）；缺失/非整数 → None。"""
    code = result.get("exitCode")
    if isinstance(code, bool):  # bool 是 int 子类，排除
        return None
    if isinstance(code, int):
        return code
    return None
