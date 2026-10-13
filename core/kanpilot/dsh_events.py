"""执行轮次事件日志读取与消息转换（protocol.md §5.18.2，v1.6）。

OWNERSHIP: 主 agent（共享骨架，冻结后只读给子代理——B 只写 events.jsonl，
不得改本模块；缺口报主 agent 修订）。数据源：pi 轮次运行目录下的
``job.json``（身份 + 进程结果）与 ``events.jsonl``（带版本的结构化事件）。

事件行：``{"v":1, "seq":<int 单调递增>, "ts":ISO, "type":..., ...}``。
- **半写容忍**：尾部无法解析的行丢弃（不报错）；有内容但零有效行 → read_failed。
- 映射（Message 形状沿用 protocol §5.15.5）：
  session→system、user→user、assistant_message→assistant、tool_call→tool_call、
  tool_result→tool_result、command_output→command_output、
  subagent_start/subagent_end→system、system→system（codex 错误帧，§5.22.6）、
  terminal→system、turn_end→system。
- Desktop 源消息使用 ``desktop:<source_message_id>:<type>`` 和递增源修订；
  其他事件稳定 id ``round:<seq>``，revision 为内容 sha 派生整数（同 headless 口径）；
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

from . import constants, util, verify_evidence

# 事件 type → Message.type（protocol §5.18.2 + §5.22.6）。
_EVENT_TO_MESSAGE = {
    "session": "system",
    "user": "user",
    "assistant_message": "assistant",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "command_output": "command_output",
    "subagent_start": "system",
    "subagent_end": "system",
    "system": "system",
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
    events, detail, _complete = _read_events(path)
    return events, detail


def _read_events(path: Path) -> tuple:
    """Keep the public tolerant reader while tracking whether a version is complete."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], "事件日志尚未生成", False
    except OSError as exc:
        return [], f"事件日志读取失败：{exc}", False
    if not text.strip():
        return [], "事件日志为空", False
    events = []
    complete = True
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            # 半写行：只允许出现在尾部——由于 append-only，解析失败后的后续行
            # 视为同一次截断，全部丢弃。
            complete = False
            break
        if isinstance(item, dict) and isinstance(item.get("seq"), int):
            events.append(item)
    if not events:
        return [], "事件日志解析失败（可能半写入）", False
    return events, None, complete


def session_event(events: list) -> Optional[dict]:
    """首条 session 事件（dispatch-confirm 核验凭据，§5.18.3）。"""
    for event in events:
        if event.get("type") == "session":
            return event
    return None


def terminal_events(events: list) -> list:
    """全部 terminal 事件（出现顺序）。"""
    return [
        event for event in events
        if isinstance(event, dict) and event.get("type") == "terminal"
    ]


def terminal_event(events: list) -> Optional[dict]:
    """末条 terminal 事件 → {outcome, reason}；无 → None。"""
    terminals = terminal_events(events)
    if not terminals:
        return None
    return _terminal_view(terminals[-1])


def _terminal_view(event: dict) -> dict:
    """terminal 事件 → 判定视图（outcome/reason）。"""
    return {
        "outcome": event.get("outcome"),
        "reason": event.get("reason"),
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
            # system/user/assistant/session/terminal/subagent_*/turn_end
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
        metadata = verify_evidence.window_metadata(event)
        raw.update(metadata)
        if metadata:
            revision_parts += (metadata,)
        source_id = event.get("source_message_id")
        source_revision = event.get("source_revision")
        if (isinstance(source_id, str) and source_id
                and isinstance(source_revision, int)
                and not isinstance(source_revision, bool)
                and source_revision > 0):
            # Desktop transcript identities are strings, independent of the
            # append-only event sequence. Keep the latest sequence so an edit
            # remains visible even outside the reader's revision window.
            raw["id"] = f"desktop:{source_id}:{etype_origin}"
            raw["revision"] = source_revision
        else:
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
    events, detail, complete = _read_events(Path(run_dir) / EVENT_LOG_NAME)
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
        "_events": events, "_fresh": complete,
    }


def round_detail(row, job: Optional[dict], events: list) -> Optional[dict]:
    """get-session 的 round_detail（protocol §5.18.4）：轮次索引行 + job 进程
    结果 + terminal 事件的合并视图。row 为 None → None。"""
    if row is None:
        return None
    from . import round_store  # 局部导入（round_store 不反向依赖本模块）

    view = round_store.round_view(row) or {}
    job = job if isinstance(job, dict) else {}
    # §5.23.7：续接观察已接管（status=running + observation_resumed_at）时，
    # 早于接管时刻的 terminal 属上一次观察者收尾留下的旧终态（心跳被拒的假
    # 失败等），不再作为本轮判定依据；事件行 append-only 不改写，原文转由
    # terminal_superseded 透出供审计。接管后新写的 terminal 才是本轮终态。
    terminal = terminal_event(events)
    terminal_superseded = None
    resumed_at = job.get("observation_resumed_at")
    if job.get("status") == "running" and util.parse_ts(resumed_at) is not None:
        kept = None
        for event in terminal_events(events):
            if util.ts_before(event.get("ts"), resumed_at):
                terminal_superseded = dict(
                    _terminal_view(event), ts=event.get("ts")
                )
            else:
                kept = event
        terminal = _terminal_view(kept) if kept is not None else None
    # 观察进程（§5.23.7）：续接观察只用它判断「本机是否还有人在看」，不作
    # 完成/失败判定依据（完成只认 terminal + job.status + 验证证据）。
    observe_pid = job.get("observe_pid") or job.get("pid") or view.get("pid")
    return {
        "backend": view.get("backend"),
        "run_status": view.get("run_status"),
        "worktree_path": view.get("worktree_path"),
        "pid": job.get("pid") if job.get("pid") is not None
        else view.get("pid"),
        "observe_pid": observe_pid,
        "observer_alive": util.pid_alive(observe_pid),
        "started_at": view.get("started_at"),
        "ended_at": job.get("ended_at"),
        "exit_code": job.get("exit_code"),
        "terminal": terminal,
        "terminal_superseded": terminal_superseded,
        "config": view.get("config"),
        "save_state": view.get("save_state"),
        "saved_at": view.get("saved_at"),
        "detail": job.get("detail"),
        "turn_id": job.get("turn_id"),
        # §5.23.8：已接续的后续轮次（瞬时模型错误后同一会话自动重开），
        # 只作审计展示；完成判定仍只认三证据。
        "turn_adopted": job.get("turn_adopted") or None,
        "send_state": job.get("send_state"),
    }
