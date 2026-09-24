"""执行轮次事件日志读取与消息转换（protocol.md §5.18.2，v1.6）。

OWNERSHIP: 主 agent（共享骨架，冻结后只读给子代理——B 只写 events.jsonl，
不得改本模块；缺口报主 agent 修订）。数据源：pi 轮次运行目录下的
``job.json``（身份 + 进程结果）与 ``events.jsonl``（带版本的结构化事件）。

事件行：``{"v":1, "seq":<int 单调递增>, "ts":ISO, "type":..., ...}``。
- **半写容忍**：尾部无法解析的行丢弃（不报错）；有内容但零有效行 → read_failed。
- 映射（Message 形状沿用 protocol §5.15.5）：
  session→system、user→user、assistant_message→assistant、tool_call→tool_call、
  tool_result→tool_result、command_output→command_output、
  subagent_start/subagent_end→system、terminal→system。
- 稳定 id ``round:<seq>``；revision 为内容 sha 派生整数（同 headless 口径）；
  同 seq 重发行按 id+revision 去重（保留高 revision）。
- 本模块只服务 backend=pi 的轮次；旧后端（dsh/headless/ATIF）已由
  headless_adapter 路由为 unavailable「旧后端会话已弃用」（v1.6 起 ATIF
  读取路径删除）。headless_adapter 在函数内局部 import 本模块（避免循环导入）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple

from . import constants, util

# 事件 type → Message.type（protocol §5.18.2）。
_EVENT_TO_MESSAGE = {
    "session": "system",
    "user": "user",
    "assistant_message": "assistant",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "command_output": "command_output",
    "subagent_start": "system",
    "subagent_end": "system",
    "terminal": "system",
    "turn_end": "system",
}
# 携带 tool 结构的 Message 类型。
_TOOL_TYPES = frozenset({"tool_call", "tool_result", "command_output"})

EVENT_LOG_NAME = "events.jsonl"
JOB_NAME = "job.json"


# ------------------------------------------------------------ job.json

def read_job(
    run_dir: Path, task_id: str, run_id: str, backend: str = "pi"
) -> Tuple[Optional[dict], Optional[str]]:
    """读取并核验 job.json（身份语义同 §5.15.5）。
    返回 (job, None) 或 (None, 错误原因)。backend 必须等于期望值
    （v1.6 仅 pi 轮次）；不符 → 身份不符，不读他目录内容。"""
    job_path = Path(run_dir) / JOB_NAME
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"job.json 缺失或不可读：{exc}"
    except ValueError:
        return None, "job.json 解析失败（半写入或损坏）"
    if not isinstance(job, dict):
        return None, "job.json 不是 JSON 对象"
    if job.get("task_id") != task_id or job.get("run_id") != run_id:
        return None, (
            f"job.json 身份与目录不符（task_id={job.get('task_id')!r} "
            f"run_id={job.get('run_id')!r}）"
        )
    if backend is not None and job.get("backend") != backend:
        return None, f"job.json backend 不符（{job.get('backend')!r}）"
    return job, None


# ------------------------------------------------------------ events.jsonl

def iter_events(path: Path) -> Tuple[list, Optional[str]]:
    """容错解析事件日志（§5.18.2 半写容忍）。
    返回 (events, detail)：无法解析的**尾行**静默丢弃（detail=None 正常路径，
    文件不存在 → ([], '事件日志尚未生成') 由调用方按 pending 处理）；
    文件存在且有内容但零有效行 → ([], '事件日志解析失败') → read_failed。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], "事件日志尚未生成"
    except OSError as exc:
        return [], f"事件日志读取失败：{exc}"
    if not text.strip():
        return [], "事件日志为空"
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            # 半写行：只允许出现在尾部——由于 append-only，解析失败后的后续行
            # 视为同一次截断，全部丢弃。
            break
        if isinstance(item, dict) and isinstance(item.get("seq"), int):
            events.append(item)
    if not events:
        return [], "事件日志解析失败（可能半写入）"
    return events, None


def session_event(events: list) -> Optional[dict]:
    """首条 session 事件（dispatch-confirm 核验凭据，§5.18.3）。"""
    for event in events:
        if event.get("type") == "session":
            return event
    return None


def terminal_event(events: list) -> Optional[dict]:
    """末条 terminal 事件 → {outcome, reason}；无 → None。"""
    terminal = None
    for event in events:
        if event.get("type") == "terminal":
            terminal = event
    if terminal is None:
        return None
    return {
        "outcome": terminal.get("outcome"),
        "reason": terminal.get("reason"),
    }


# ------------------------------------------------------------ 消息转换

def _revision(*parts) -> int:
    """内容 sha 派生的稳定 revision（与 headless_adapter.content_revision 同
    口径；本地实现以避免与 headless_adapter 的循环导入）。"""
    payload = json.dumps(
        list(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12], 16)


def to_messages(events: list) -> list:
    """事件 → protocol §5.15.5 原始消息列表（不含 run_id，由命令层注入）。
    按 id（round:<seq>）+ revision 去重（同 seq 重发行保留高 revision）。"""
    best: dict = {}
    for event in events:
        seq = event.get("seq")
        etype = _EVENT_TO_MESSAGE.get(event.get("type"))
        if seq is None or etype is None:
            continue
        ts = event.get("ts")
        raw = {
            "id": f"round:{seq}",
            "seq": seq,
            "type": etype,
            "source_at": ts,
        }
        etype_origin = event.get("type")
        if etype == "tool_call":
            arguments = event.get("arguments")
            input_json = arguments if isinstance(arguments, str) else json.dumps(
                arguments if arguments is not None else {},
                ensure_ascii=False, sort_keys=True,
            )
            content = input_json
            raw["tool"] = {
                "name": event.get("name"), "input": input_json,
                "output": None, "exit_code": None,
            }
            revision_parts = ("tc", event.get("name"), input_json)
        elif etype == "tool_result":
            content = event.get("output")
            if not isinstance(content, str):
                content = "" if content is None else json.dumps(
                    content, ensure_ascii=False, sort_keys=True
                )
            raw["tool"] = {
                "name": event.get("name"), "input": event.get("input"),
                "output": content, "exit_code": None,
            }
            revision_parts = ("tr", event.get("name"), content)
        elif etype == "command_output":
            content = event.get("output")
            if not isinstance(content, str):
                content = "" if content is None else json.dumps(
                    content, ensure_ascii=False, sort_keys=True
                )
            exit_code = event.get("exit_code")
            raw["tool"] = {
                "name": event.get("name"), "input": event.get("input"),
                "output": None,
                "exit_code": exit_code if isinstance(exit_code, int) else None,
            }
            revision_parts = ("co", event.get("name"), content, exit_code)
        else:
            # system/user/assistant/session/terminal/subagent_*
            content = event.get("content")
            if not isinstance(content, str):
                payload = {
                    key: event.get(key)
                    for key in ("session_id", "session_title", "outcome",
                                "reason", "label", "name")
                    if event.get(key) is not None
                }
                content = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True
                )
            revision_parts = (etype_origin, content)
        raw["content"] = content
        raw["revision"] = _revision(*revision_parts)
        key = raw["id"]
        current = best.get(key)
        if current is None or raw["revision"] >= current["revision"]:
            best[key] = raw
    return sorted(best.values(), key=lambda m: m["seq"])


def _job_record_view(job: Optional[dict], events: list, messages: list) -> dict:
    """SessionInfo 源侧字段（dsh 轮次）：优先 job.json，事件时间兜底。"""
    timestamps = [e.get("ts") for e in events if e.get("ts")]
    session = session_event(events) or {}
    session_id = (session.get("session_id")
                  or (job or {}).get("session_id"))
    session_title = (session.get("session_title")
                     or (job or {}).get("session_title"))
    started_at = (job or {}).get("started_at") or (
        timestamps[0] if timestamps else None
    )
    last_message_at = timestamps[-1] if timestamps else None
    return {
        "session_id": session_id,
        "session_title": session_title,
        "started_at": started_at,
        "last_message_at": last_message_at,
        "message_count": len(messages),
        "updated_at": last_message_at or (job or {}).get("updated_at"),
    }


def inspect(
    run_dir: Path, task_id: str, run_id: str, job: Optional[dict] = None,
    backend: str = "pi",
) -> dict:
    """pi/codex 轮次读取（形状同 headless_inspect）：
    返回 {"status": associated|pending|read_failed|unavailable,
          "detail", "record", "messages"}。
    - job 未预读时自行 read_job；身份不符 → unavailable（不读事件）。
    - events.jsonl 不存在（job 有效）→ pending（等待事件日志）。
    - 有内容零有效行 → read_failed（调用方保留上一份快照/按 pending）。
    - 正常 → associated + 消息（半写尾行丢弃不报错）。"""
    if job is None:
        job, error = read_job(run_dir, task_id, run_id, backend=backend)
        if job is None:
            return {
                "status": "unavailable", "detail": error,
                "record": None, "messages": None,
            }
    events, detail = iter_events(Path(run_dir) / EVENT_LOG_NAME)
    if events is None:  # pragma: no cover - defensive
        return {
            "status": "unavailable", "detail": detail,
            "record": None, "messages": None,
        }
    if detail == "事件日志尚未生成" or detail == "事件日志为空":
        return {
            "status": "pending",
            "detail": "等待事件日志：events.jsonl 尚未生成",
            "record": None, "messages": None,
        }
    if not events:
        return {
            "status": "read_failed", "detail": detail,
            "record": None, "messages": None,
        }
    messages = to_messages(events)
    return {
        "status": "associated", "detail": None,
        "record": _job_record_view(job, events, messages),
        "messages": messages,
    }


def round_detail(row, job: Optional[dict], events: list) -> Optional[dict]:
    """get-session 的 round_detail（protocol §5.18.4）：轮次索引行 + job 进程
    结果 + terminal 事件的合并视图。row 为 None → None。"""
    if row is None:
        return None
    from . import round_store  # 局部导入（round_store 不反向依赖本模块）

    view = round_store.round_view(row) or {}
    job = job if isinstance(job, dict) else {}
    return {
        "backend": view.get("backend"),
        "run_status": view.get("run_status"),
        "worktree_path": view.get("worktree_path"),
        "pid": job.get("pid") if job.get("pid") is not None
        else view.get("pid"),
        "started_at": view.get("started_at"),
        "ended_at": job.get("ended_at"),
        "exit_code": job.get("exit_code"),
        "terminal": terminal_event(events),
        "config": view.get("config"),
        "save_state": view.get("save_state"),
        "saved_at": view.get("saved_at"),
        "detail": job.get("detail"),
    }
