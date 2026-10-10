"""Headless 运行记录只读会话适配层（protocol.md §5.15.5，v1.4）。

OWNERSHIP: session subagent. 数据源为本机执行轮次记录：
``<runs_root>/<task_id>/<run_id>/`` 下的 ``job.json``（身份核验）与
``events.jsonl``（pi 轮次事件流，经 dsh_events 转换）。runs_root 取 DB
setting ``session_runs_root``，缺省 ``~/Library/Application Support/
KaneoPilot/runs``（constants）。**不依赖任何远端凭据，不按标题猜测关联；
不写任何远端。**

历史 ATIF 结构（v1.6 起不再读取，仅注释存档）::

    {"schema_version": "ATIF-v1.7", "session_id": "...",
     "agent": {"name", "version", "model_name", "tool_definitions", "extra"},
     "steps": [{"step_id": int, "timestamp": ISO8601,
                "source": "system|user|agent", "message": str, "extra": dict,
                # agent 步另有：
                "model_name", "reasoning_content", "metrics",
                "tool_calls": [{"tool_call_id", "function_name",
                                 "arguments": dict}],
                "observation": {"results": [{"source_call_id", "content"}]}}],
     "final_metrics": {...}}

映射决定（Message 形状不变，见 normalize_message）：
  * **system 步骤不产消息**（保持纯 user/assistant 对话流）；user 步 →
    type=user；agent 步 message 非空 → assistant。
  * 每个 tool_calls 条目 → tool_call：``tool.name=function_name``、
    ``tool.input=arguments 的 JSON 字符串序列化``；arguments 含 ``command``
    视为命令调用（真实 backend 的 exec 工具即如此）。
  * observation.results 条目 → tool_result；反查对应 tool_call 若为命令调用
    （arguments 含 ``command``，真实 backend 的 exec 工具即如此）则映射为
    command_output（真实输出为 "Output from command in shell …/Exit code: N"，
    尾部退出码解析进 ``tool.exit_code``）；``tool.name``/``tool.input`` 取自
    反查到的 tool_call（反查不到则为 None）。
  * 稳定 id：``round:<seq>``（pi 事件）；revision 为消息源内容
    sha256 派生的稳定整数（内容更新 → revision 变化）。

状态语义（命令层据此组装响应，见 cmd_session）：
  * session.state：associated（身份核验通过且 events.jsonl 解析成功）/
    pending（目录 + job.json 有效但 events.jsonl 尚未生成；或无 run_id）/
    unavailable（目录或 job.json 缺失、身份不符、路径越界、root 不存在）。
  * v1.6（protocol §5.18.2）：job.json ``backend=="pi"`` 的轮次经
    ``dsh_events.inspect`` 读 events.jsonl（状态词汇相同）；backend≠pi
    的历史轮次返回 unavailable「旧后端会话已弃用」。
  * connection.state：headless 无推送订阅 → 恒 degraded（poll_seconds）；
    root 不存在等机制性失败 → unavailable。
  * 读取失败（半写入 JSON）→ 仍 degraded，connection.detail 写原因，且保留
    上一份有效内容（进程内缓存 _GOOD_SNAPSHOTS 按 session_key 存最近一次成功
    解析的 steps）；无缓存时按 pending，绝不返回「空会话」掩盖解析失败。

路径安全：task_id/run_id 仅允许 [A-Za-z0-9._-]；realpath 解析后必须仍在
runs_root 内（拒绝 `..` 与符号链接逃逸）。

数据源接缝：``set_source(src)`` / ``get_source()``（进程内注入，测试用）；
``resolve_source(conn)`` 按 DB setting 解析。

只读边界：不写任务状态、不写远端、不输出凭据；唯一本地写是 session_reads。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from . import constants, util

# ------------------------------------------------------------- 状态词汇

# connection.state 词汇（protocol.md §5.15.5）。
CONN_OK = "ok"              # 保留：headless 恒 degraded，不使用
CONN_DEGRADED = "degraded"  # 读取可用、无推送订阅（UI 按 poll_seconds 轮询）
CONN_UNAVAILABLE = "unavailable"  # root 不存在等机制性失败

# session.state 词汇。
SESSION_ASSOCIATED = "associated"
SESSION_PENDING = "pending"
SESSION_UNAVAILABLE = "unavailable"

# inspect() 的 status 词汇（含读取失败）。
STATUS_READ_FAILED = "read_failed"

# 受支持的会话消息类型（protocol.md §5.15.5）。
MESSAGE_TYPES = (
    "user", "assistant", "tool_call", "tool_result", "command_output", "system",
)
_TOOL_TYPES = frozenset({"tool_call", "tool_result", "command_output"})


# 增量读取修订透出的尾部窗口（seq 跨度，约 3 个步骤）。
REVISION_WINDOW_SEQ = 300

# 标识符白名单（路径安全）。
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")



class HeadlessError(Exception):
    """数据源机制性错误。kind 目前只有 unavailable（root 不存在等）。"""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------- message normalization

def normalize_message(raw: dict, run_id: Optional[str] = None) -> dict:
    """把一条（pi 事件转换或测试注入的）消息规整为 §5.15.5 稳定形状。

    - ``id``/``seq``/``type``/``content``/``revision``/``source_at`` 原样透出，
      缺省给安全默认；``type`` 不在词表内回落 ``system``。
    - ``tool`` 仅 tool_call/tool_result/command_output 出现，其余类型不含该键。
    - ``run_id`` 由命令层按关联结果注入（数据源不感知 KaneoPilot run）。
    """
    msg_type = raw.get("type")
    if msg_type not in MESSAGE_TYPES:
        msg_type = "system"
    message = {
        "id": raw.get("id"),
        "seq": raw.get("seq"),
        "run_id": run_id,
        "type": msg_type,
        "content": raw.get("content") if raw.get("content") is not None else "",
        "revision": raw.get("revision") if raw.get("revision") is not None else 0,
        "source_at": raw.get("source_at"),
    }
    if msg_type in _TOOL_TYPES:
        tool = raw.get("tool") or {}
        message["tool"] = {
            "name": tool.get("name"),
            "input": tool.get("input"),
            "output": tool.get("output"),
            "exit_code": tool.get("exit_code"),
        }
    return message


def content_revision(*parts: Any) -> int:
    """从消息源内容派生的稳定 revision（同内容恒等、内容更新则变）。"""
    payload = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def dedupe_and_slice(
    messages: list, after_seq: Optional[int], limit: Optional[int]
) -> dict:
    """按 seq 排序、按 id+revision 去重/更新（保留最大 revision），再按游标切片。

    ``after_seq`` 为游标解析出的“已读到的最大 seq”：仅返回 seq 更大的消息；
    ``None`` 表示首次加载，返回完整历史。``revisions``（本模块 slice_messages 填）
    使尾部进行中步骤的内容更新即使 seq<=cursor 也能透出。返回
    ``{"messages": [...], "next_cursor": str|None, "has_more": bool, "last_seq": int|None}``。
    """
    best: dict = {}
    for raw in messages:
        key = raw.get("id")
        current = best.get(key)
        revision = raw.get("revision") if raw.get("revision") is not None else 0
        if current is None or revision >= current[0]:
            best[key] = (revision, raw)
    ordered = sorted(
        (item[1] for item in best.values()),
        key=lambda m: (m.get("seq") if m.get("seq") is not None else 0),
    )
    return {
        "ordered": ordered,
        "best": best,
        **_page_from_ordered(ordered, limit),
    }


def _page_from_ordered(ordered: list, limit: Optional[int]) -> dict:
    has_more = False
    page = ordered
    if limit is not None and limit >= 0 and len(ordered) > limit:
        page = ordered[:limit]
        has_more = True
    last_seq = None
    if page:
        seqs = [m.get("seq") for m in page if m.get("seq") is not None]
        last_seq = max(seqs) if seqs else None
    next_cursor = None if last_seq is None else f"seq:{last_seq}"
    return {
        "messages": page,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "last_seq": last_seq,
    }


def slice_messages(
    messages: list,
    after_seq: Optional[int],
    limit: Optional[int],
    revisions: Optional[dict] = None,
) -> dict:
    """游标切片 + **修订透出**：seq>cursor 的正常返回；已读尾部窗口中当前
    revision 与已存快照（revisions，来自 session_reads）不同的消息也返回，
    保证「进行中步骤的内容更新」能在增量读取中透出（空页时游标保持不回退）。

    revisions 只覆盖上次保存的尾部窗口；窗口外的旧消息视为已定格，不再复查
    （已导出步骤不再变化）。"""
    sliced = dedupe_and_slice(messages, None, None)
    ordered = sliced["ordered"]
    best = sliced["best"]
    known = revisions or {}
    kept = []
    for message in ordered:
        seq = message.get("seq") if message.get("seq") is not None else 0
        if after_seq is None or seq > after_seq:
            kept.append(message)
            continue
        mid = message.get("id")
        if mid in known and known[mid] != best[mid][0]:
            kept.append(message)
    return _page_from_ordered(kept, limit)


def revision_snapshot(messages: list, last_seq: Optional[int]) -> Optional[dict]:
    """构造尾部窗口的 id→revision 快照（存入 session_reads，见
    session_store.encode_revisions）：覆盖 last_seq 前 REVISION_WINDOW_SEQ 个
    seq 至当前最大 seq 的消息，用于增量读取时检测进行中步骤的内容更新。
    last_seq 为 None（本轮未产出可推进位点）→ None：调用方保持旧快照。"""
    if last_seq is None:
        return None
    low = last_seq - REVISION_WINDOW_SEQ
    return {
        message["id"]: message.get("revision")
        for message in messages
        if message.get("id") is not None
        and low < (message.get("seq") or 0)
    }


def _parse_cursor(cursor: Optional[str]) -> Optional[int]:
    """把不透明游标解析为 after_seq；无法解析按首次加载（None）处理。"""
    if not cursor:
        return None
    text = str(cursor)
    if text.startswith("seq:"):
        try:
            return int(text[4:])
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_cursor(cursor: Optional[str]) -> Optional[int]:
    """公开别名：命令层需要游标语义时使用。"""
    return _parse_cursor(cursor)


def session_record_view(record: dict) -> dict:
    """从数据源的会话记录提取 SessionInfo 的源侧字段。"""
    return {
        "session_id": record.get("session_id"),
        "session_title": record.get("session_title"),
        "started_at": record.get("started_at"),
        "last_message_at": record.get("last_message_at"),
        "message_count": record.get("message_count")
        if record.get("message_count") is not None else 0,
        "updated_at": record.get("updated_at"),
    }


# --------------------------------------------------------- source interface

class HeadlessSource:
    """读取本机 headless 运行记录的只读数据源。

    runs_root 为字符串路径（支持 ~ 展开）。进程内缓存 ``_snapshots`` 按
    session_key ("<task_id>/<run_id>") 存最近一次成功解析的消息与视图，
    供半写入读取失败时保留上一份有效内容。
    """

    def __init__(self, runs_root: str):
        self._root_raw = str(runs_root or "")
        self._snapshots: dict = {}

    @property
    def runs_root(self) -> Path:
        return Path(self._root_raw).expanduser()

    def probe(self) -> dict:
        root = self.runs_root
        if not self._root_raw or not root.is_dir():
            return {
                "state": CONN_UNAVAILABLE,
                "detail": (
                    f"运行记录根目录不存在：{self._root_raw or '(未设置)'}"
                    "；或在 Settings 中写入 session_runs_root 配置"
                ),
                "subscription": False,
            }
        return {
            "state": CONN_DEGRADED,
            "detail": None,
            "subscription": False,
        }

    # -- 路径安全 --

    def run_dir(self, task_id: str, run_id: str) -> Path:
        """解析 <root>/<task_id>/<run_id>，越界/符号链接逃逸抛 HeadlessError。"""
        for label, value in (("task_id", task_id), ("run_id", run_id)):
            if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
                raise HeadlessError(
                    "unavailable",
                    f"{label} 含非法字符（仅允许 [A-Za-z0-9._-]）：{value!r}",
                )
        root = self.runs_root
        candidate = root / task_id / run_id
        try:
            root_real = root.resolve()
            candidate_real = candidate.resolve()
        except OSError as exc:
            raise HeadlessError("unavailable", f"路径解析失败：{exc}")
        if candidate_real == root_real or root_real not in candidate_real.parents:
            raise HeadlessError(
                "unavailable",
                f"执行目录越出运行记录根目录：{task_id}/{run_id}",
            )
        return candidate

    # -- 关联 + 读取（含缓存兜底）--

    def source_signature(self, task_id: str, run_id: str) -> Optional[dict]:
        """Validate the current small job and stat both sources without reading events.

        The job stat brackets its identity read. Missing/unstable/invalid sources
        cannot acknowledge a cached version. File metadata includes ctime so a
        same-size rewrite with restored mtime still invalidates it.
        """
        from . import dsh_events

        def stamp(path):
            info = path.stat()
            return [info.st_dev, info.st_ino, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns]

        try:
            directory = self.run_dir(task_id, run_id)
            job_path = directory / dsh_events.JOB_NAME
            before = stamp(job_path)
            job, error = dsh_events.read_job(directory, task_id, run_id, backend=None)
            if error or job.get("backend") not in constants.ROUND_BACKENDS:
                return None
            after = stamp(job_path)
            if before != after:
                return None
            signature = {
                "runs_root": str(self.runs_root.resolve()),
                "task_id": task_id, "run_id": run_id, "backend": job["backend"],
                "job": after, "events": stamp(directory / dsh_events.EVENT_LOG_NAME),
            }
            return {"signature": signature, "job": job}
        except (OSError, HeadlessError):
            return None

    def inspect(self, task_id: str, run_id: str) -> dict:
        """核验身份并读取轮次记录。返回 {"status", "detail", "record", "messages"}，
        status ∈ associated|pending|unavailable|read_failed（read_failed 仅在
        有缓存时返回缓存内容；命令层将其映射为 associated + degraded detail）。
        """
        signature_before = self.source_signature(task_id, run_id)
        try:
            directory = self.run_dir(task_id, run_id)
        except HeadlessError as exc:
            return self._fallback(exc.detail, "unavailable", task_id, run_id)
        if not directory.is_dir():
            return self._fallback(
                f"执行目录不存在：{directory}", "unavailable", task_id, run_id
            )
        job_path = directory / "job.json"
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except OSError:
            return self._fallback(
                f"job.json 缺失或不可读：{job_path}",
                "unavailable", task_id, run_id,
            )
        except ValueError:
            return self._fallback(
                f"job.json 解析失败（半写入或损坏）：{job_path}",
                STATUS_READ_FAILED, task_id, run_id,
            )
        if not isinstance(job, dict):
            return self._fallback(
                "job.json 不是 JSON 对象", "unavailable", task_id, run_id
            )
        if job.get("task_id") != task_id or job.get("run_id") != run_id:
            return self._fallback(
                "job.json 身份与目录不符"
                f"（task_id={job.get('task_id')!r} run_id={job.get('run_id')!r}）",
                "unavailable", task_id, run_id,
            )
        if not job.get("backend"):
            return self._fallback(
                "job.json 缺少 backend 字段", "unavailable", task_id, run_id
            )
        # v1.5（protocol §5.18.2）：backend=dsh 轮次改读 events.jsonl；
        # 历史后端（backend≠pi）读取已弃用。
        # 身份核验（task_id/run_id/backend 存在）在两分支之前统一完成——
        # session-messages / get-session / list-sessions 均经本入口路由。
        # v1.6（protocol §5.18.2）：backend="pi" 轮次读 events.jsonl；
        # 历史后端（dsh/headless/ATIF conversation.json）已弃用 → unavailable。
        backend = job.get("backend")
        # v2（§5.22.3）：codex 规划轮次与 pi 轮次共用 events.jsonl 词汇，
        # 一并走 dsh_events；其余历史后端（dsh/headless/ATIF）已弃用。
        if backend not in constants.ROUND_BACKENDS:
            return self._fallback(
                f"旧后端会话已弃用（backend={backend!r}，仅 "
                f"{'/'.join(constants.ROUND_BACKENDS)} 可读）",
                SESSION_UNAVAILABLE, task_id, run_id,
            )
        from . import dsh_events  # 局部导入：避免与本模块循环依赖

        result = dsh_events.inspect(
            directory, task_id, run_id, job=job, backend=backend
        )
        status = result.get("status")
        if status == SESSION_ASSOCIATED:
            signature_after = self.source_signature(task_id, run_id)
            if (result.get("_fresh") and signature_before and signature_after
                    and signature_before["signature"] == signature_after["signature"]):
                result["_source_signature"] = signature_after["signature"]
            # 半写入兜底：成功解析即缓存最近有效内容。
            self._snapshots[_key(task_id, run_id)] = {
                "messages": result.get("messages") or [],
                "record": result.get("record"),
            }
            return result
        if status == SESSION_PENDING:
            # events.jsonl 尚未生成：清旧缓存，不让历史内容掩盖。
            self._snapshots.pop(_key(task_id, run_id), None)
            return result
        if status == STATUS_READ_FAILED:
            # 有内容但零有效行（半写入）：进程内缓存兜底。
            return self._fallback(
                result.get("detail") or "事件日志读取失败",
                STATUS_READ_FAILED, task_id, run_id,
            )
        return result  # unavailable

    def _fallback(
        self, detail: str, fallback_status: str, task_id: str, run_id: str
    ) -> dict:
        """读取失败时优先保留上一份有效内容（进程内缓存）。"""
        cached = self._snapshots.get(_key(task_id, run_id))
        if cached is not None:
            return {
                "status": SESSION_ASSOCIATED, "detail": detail,
                "record": cached["record"], "messages": cached["messages"],
            }
        return {
            "status": fallback_status, "detail": detail,
            "record": None, "messages": None,
        }

    # -- 轻量核验（list-sessions 记录过多时用，只 stat 不读文件内容）--

    def task_dir_count(self) -> int:
        """root 下一层任务目录条目数（>阈值时 list-sessions 缺省模式轻量处理）。"""
        try:
            return sum(1 for p in self.runs_root.iterdir() if p.is_dir())
        except OSError:
            return 0

    def light_inspect(self, task_id: str, run_id: str) -> dict:
        """不读文件内容的目录核验：缺失/越界 → unavailable；其余 → pending
        （associated 态与消息由 get-session / session-messages 补齐）。"""
        try:
            directory = self.run_dir(task_id, run_id)
        except HeadlessError as exc:
            return {"status": SESSION_UNAVAILABLE, "detail": exc.detail}
        if not directory.is_dir():
            return {
                "status": SESSION_UNAVAILABLE,
                "detail": f"执行目录不存在：{directory}",
            }
        if not (directory / "job.json").exists():
            return {"status": SESSION_UNAVAILABLE, "detail": "job.json 缺失"}
        return {
            "status": SESSION_PENDING, "deferred": True,
            "detail": "运行记录较多，已延迟核验：请经 get-session / "
                      "session-messages 查看会话详情",
        }


def _key(task_id: str, run_id: str) -> str:
    return f"{task_id}/{run_id}"


# ------------------------------------------------------------- source 工厂

_source_override: Optional[HeadlessSource] = None


def set_source(source: Optional[HeadlessSource]) -> None:
    """进程内注入数据源（测试接缝；生产经 DB setting 解析）。传 None 清除。"""
    global _source_override
    _source_override = source


def get_source() -> Optional[HeadlessSource]:
    return _source_override


def resolve_source(conn, runs_root: Optional[str] = None) -> HeadlessSource:
    """解析数据源。根目录优先级：显式 runs_root（list-sessions payload）>
    DB setting ``session_runs_root``（字符串路径，支持 ~ 展开；空字符串视为
    未设置）> constants 默认根目录。注入接缝 set_source 始终优先。永不返回
    None；root 不存在经 probe()/inspect() 映射为 unavailable。"""
    if _source_override is not None:
        return _source_override
    root = runs_root
    if not (isinstance(root, str) and root != ""):
        from . import db

        root = db.get_setting(conn, "session_runs_root", "")
    if not isinstance(root, str) or root == "":
        root = str(constants.default_session_runs_root())
    return HeadlessSource(root)


def poll_seconds(conn) -> int:
    """degraded 时 UI 的增量检查间隔（settings.session_poll_seconds，默认 2s）。"""
    from . import db

    value = db.get_setting(
        conn, "session_poll_seconds", constants.DEFAULT_SESSION_POLL_SECONDS
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return constants.DEFAULT_SESSION_POLL_SECONDS
