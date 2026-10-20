"""pi_desktop 轮次 worker（protocol.md §5.23.3，v3 T3）。

CLI：``python -m kanpilot.pi_desktop_worker --spec <spec.json>``（dispatch-start
以独立会话拉起，stdout/stderr 重定向到运行目录 worker.log）。

spec.json（v1；connection_file 是路径不是 token）::

    {"v": 1, "task_id", "run_id", "session_marker", "backend": "pi_desktop",
     "config": {"connection_file", "provider_id", "model_id",
                "thinking_level", "permission_mode"},
     "cwd": "<worktree 绝对路径>", "repository", "plan_path", "plan_sha256",
     "instruction", "runs_dir", "timeout_seconds",
     "request_id", "db_path"}

worker 职责（写文件 + 经 kanpilot 命令心跳；绝不直写 SQLite）：
1. 写 ``job.json``（身份三字段 + backend=pi_desktop + config 快照 + pid +
   status=running + send_state=not_sent）；
2. 读 plan_path → 重算 sha256 与登记不符 → job failed(detail=plan_mismatch)
   + terminal(failed)，**不发 prompt**；正文 UTF-8 字节数超
   ``PI_DESKTOP_MCP_MAX_REQUEST_BYTES`` → job failed(detail=plan_too_large)
   + terminal(failed)，发送前明确拒绝；
3. 经连接文件完成 MCP initialize → ``pi_session_create(projectPath=cwd,
   mode="goal", providerId/modelId/thinkingLevel, title=session_marker)``
   → 已保存 permission_mode 非 inherit 时 ``pi_session_configure(id,
   mode="goal", confirm=true, permissionMode=已保存值)``；工具门要求同时
   具备基础工具与 ``pi_plans_pending``/``pi_plans_resolve``（§5.23.9）；
4. ``pi_agent_prompt(sessionId, content=instruction + "\n\n" + 计划正文)``
   → 返回 ``{turnId}`` 记 ``job.send_state="sent"`` + session_id/turn_id/
   pi_desktop_pid 写回 job.json；
5. **启动证明**（§5.23.3）：``pi_turn_get`` 确认同 session/turn=running
   （或已 completed 的持久 turn 记录）且 ``heartbeat`` 回报核心成功，才写
   events.jsonl 首个 ``session`` 事件（含 session_id/turn_id）；
5b. **Goal 契约审批门**（§5.23.9）：goal 会话的规划轮以「提交目标契约」收
   官（``planningState ∈ {planning, awaiting_approval}``）——本机代批
   （``pi_plans_resolve(approve, targetPermissionMode=该轮 permission_mode,
   confirm=true)``）→ 等宿主在同一会话开启执行轮次 → 换绑该 turn 继续观察
   （不新建会话、绝不重发 prompt）；契约缺失/批准失败/执行轮次未开启 →
   明确失败并留痕（``job.goal_approval`` + ``system`` 事件）；
6. 采集：``pi_agent_status`` + 分页 ``pi_session_get``（messageBefore/
   messageLimit 游标翻页到底），按消息 id + 内容指纹去重映射 events.jsonl
   （session/user/assistant_message/tool_call/tool_result/command_output/
   system/terminal）。分页不完整/截断 → 保留证据、job failed
   （detail=transcript_incomplete）、不写成功 terminal；
7. 终态：``pi_turn_get`` completed + 记录读全 → job finished(exit_code=0)
   + terminal(completed)；error/aborted/interrupted → job failed +
   terminal(failed, reason=errorCode/turn 状态)；超时 → ``pi_agent_stop``
   → 宽限 → ``pi_agent_abort`` → ``pi_turn_get`` 核实终态；
8. **绝不自动重复 session_create/agent_prompt**：发送响应丢失/身份不符 →
   ``job.send_state="unknown"`` + 尽力取证 + job failed；收尾写序恒
   「job 先写、session 补齐、terminal 最后」。

``--resume``（§5.23.7 观察者续接）：观察进程退出后**只读续接观察**同一个
session/turn——不建会话、不下发权限、绝不重发 prompt；接管时写
``job.observe_pid``/``resume_started_at``，接管成功写
``job.observation_resumed_at``。观察环境不成立（job 身份不符/未确认发送/
已超时/连接不可用）→ 退出码 3 + ``job.resume_detail``，既有轮次终态与证据
原样保留，交监控端下一轮重试。

KANPILOT_PI_DESKTOP_FAKE（隔离测试接缝；未知值按 finished 语义）：
- ``running``：job(running)+session/user 事件后挂起（测试负责清理）；
- ``finished``：job(finished,0)+session/user/assistant/terminal(completed)；
- ``failed``：job(failed,1)+terminal(failed)；
- ``crash``：job(running)+session 事件后退出码 3，不写 terminal；
- ``send_unknown``：job(running, send_state="unknown", session_id/turn_id 已
  知)+session 事件后退出码 1——发送响应丢失但保留身份与证据的现场。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from . import constants, dsh_events, execution_contract, git_delivery, pi_desktop_backend, process_owner, runtime_client, util, verify_evidence
from . import cli  # heartbeat 经命令层（worker 不直写 SQLite）

# MCP 轮询节奏（worker 进程内部参数，不进 settings）。
_POLL_SECONDS = 2.0
_HEARTBEAT_SECONDS = 30.0
_PROOF_WAIT_SECONDS = 30.0       # 启动证明（pi_turn_get+heartbeat）等待上限
_STOP_GRACE_SECONDS = 10.0       # pi_agent_stop 后的宽限
_PAGE_LIMIT = 40                 # pi_session_get 单页消息数（初始值）
_PAGE_MIN_LIMIT = 1              # 单条仍超限时 runtime 可完整分片读取
_READ_RECOVERY_SECONDS = 10.0
_READ_RECOVERY_ATTEMPTS = 3
_STOP_VERIFY_SECONDS = 30.0
_MAX_PAGES_PER_POLL = 200        # 单次轮询最多翻页数（防御无界）
                                 # 首轮/续接需从 0 起核验整段链；此后每轮
                                 # 只读到「已核验前缀」锚点即收敛（§5.23.3）。
# 后续轮次接续（§5.23.8）：瞬时模型错误后 Plus 会在同一会话自动重开新 turn，
# 该轮次的执行没有中断——只接续这些错误码，且最多接续 _MAX_TURN_ADOPTIONS 次。
_TRANSIENT_TURN_ERRORS = frozenset({"EMPTY_MODEL_RESPONSE"})
_MAX_TURN_ADOPTIONS = 5
# Goal 契约审批门（§5.23.9）：turn completed 后等契约落库的宽限，以及批准后等
# 宿主开启执行轮次的宽限。超时一律明确失败，绝不重发 prompt。
_GOAL_CONTRACT_WAIT_SECONDS = 20.0
_GOAL_EXECUTION_WAIT_SECONDS = 30.0
_GOAL_POLL_SECONDS = 1.0

# UiMessage.role → events.jsonl 事件类型（§5.15.5/§5.18.2 词汇）。
# shell 类工具名（大小写归一；真实名称是 `Bash`，见 §5.23.10 采集修补）。
_SHELL_TOOL_NAMES = frozenset(
    {"bash", "shell", "exec", "run_command", "terminal", "command"}
)
# T5-D（§5.23.10）：执行终态落地后给 Host 完成 outbox/报告发布的宽限，期间
# 只读查询，绝不重发 prompt。
_GOAL_REPORT_GRACE_SECONDS = constants.GOAL_REPORT_GRACE_SECONDS
_GOAL_REPORT_POLL_SECONDS = 2.0


def _is_shell_tool(name) -> bool:
    """shell 工具判定（大小写归一）：`Bash`/`bash` 等同（§5.23.10）。"""
    return str(name or "").strip().lower() in _SHELL_TOOL_NAMES


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


class _EventLog:
    """events.jsonl 追加器（seq 单调递增；每行 flush，供读取层随时消费）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seq = 0
        if self.path.is_file():
            events, _ = dsh_events.iter_events(self.path)
            if events:
                self.seq = max(e.get("seq", 0) for e in events)

    def emit(self, etype: str, ts: str, **fields) -> dict:
        self.seq += 1
        event = {"v": 1, "seq": self.seq, "ts": ts, "type": etype, **fields}
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
        return event


def _base_job(spec: dict) -> dict:
    return execution_contract.attach_to_job(spec, {
        "task_id": spec["task_id"],
        "run_id": spec["run_id"],
        "backend": "pi_desktop",
        "session_id": None,
        "session_title": None,
        "turn_id": None,
        "send_state": "not_sent",
        "pi_desktop_pid": None,
        "config": spec.get("config"),
        "completion_policy": spec.get("completion_policy", "goal_report"),
        "plan_sha256": spec.get("plan_sha256"),
        "verification_evidence_version": 1,
        "pid": os.getpid(),
        "status": "running",
        "started_at": util.now_iso(),
        "ended_at": None,
        "exit_code": None,
        "detail": None,
    })


def _record_verification_source(job: dict, spec: dict) -> None:
    # Old jobs/resumes have no contemporaneous snapshot contract. Never fill in
    # historical evidence using the source bytes observed by a later worker.
    if type(job.get("verification_evidence_version")) is not int or job["verification_evidence_version"] != 1:
        return
    job.pop("verification_source_sha256", None)
    job.pop("verification_source_error", None)
    try:
        job["verification_source_sha256"] = git_delivery.source_state_sha256(spec["cwd"])
    except (git_delivery.GitDeliveryError, OSError, KeyError):
        job["verification_source_error"] = "Plus 结束时无法核验源码快照，验证证据尚未确认"


def run_fake(spec: dict, mode: str) -> int:
    return process_owner.run_owned(spec, lambda owner: _run_fake(spec, mode, owner))


def _run_fake(spec: dict, mode: str, owner) -> int:
    """KANPILOT_PI_DESKTOP_FAKE 隔离接缝（语义同 pi_worker fake）。"""
    execution_contract.validate_spec(spec, starting=True)
    run_dir = Path(spec["runs_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    job_path = run_dir / dsh_events.JOB_NAME
    job = _base_job(spec)
    log = _EventLog(run_dir / dsh_events.EVENT_LOG_NAME)
    now = util.now_iso()
    session_id = f"fake-pd-session-{spec['run_id'][:8]}"
    turn_id = f"fake-turn-{spec['run_id'][:8]}"
    if mode == "running":
        _write_json(job_path, job)
        log.emit("session", now, session_id=session_id,
                 session_title="Fake pi_desktop · 运行中", turn_id=turn_id)
        log.emit("user", now, content=spec.get("instruction") or "")
        while True:  # 测试负责终止该进程
            owner.check()
            time.sleep(0.2 if owner.instance_id else 60)
    if mode == "crash":
        _write_json(job_path, job)
        log.emit("session", now, session_id=session_id,
                 session_title="Fake pi_desktop · 崩溃", turn_id=turn_id)
        return 3  # 不写 terminal、不更新 job → 模拟崩溃/截断现场
    if mode == "send_unknown":
        # 发送响应丢失但持久身份已知的现场：send_state=unknown + 证据保留，
        # 不重复 prompt，不写成功 terminal。
        job.update({
            "session_id": session_id,
            "turn_id": turn_id,
            "send_state": "unknown",
            "detail": "prompt_response_lost",
        })
        _write_json(job_path, job)
        log.emit("session", now, session_id=session_id,
                 session_title="Fake pi_desktop · 发送不明", turn_id=turn_id)
        return 1
    failed = mode == "failed"
    job.update({
        "status": "failed" if failed else "finished",
        "ended_at": util.now_iso(),
        "exit_code": 1 if failed else 0,
        "session_id": session_id,
        "session_title": "Fake pi_desktop · 已完成" if not failed
        else "Fake pi_desktop · 失败",
        "turn_id": turn_id,
        "send_state": "sent",
        "detail": "fake failure" if failed else None,
    })
    if not failed:
        _record_verification_source(job, spec)
    _write_json(job_path, job)
    log.emit("session", now, session_id=session_id,
             session_title=job["session_title"], turn_id=turn_id)
    log.emit("user", now, content=spec.get("instruction") or "")
    if not failed:
        log.emit("assistant_message", util.now_iso(), content="执行完成。", turn_id=turn_id)
        parsed = {"ok": False}
        try:
            parsed = verify_evidence.parse_verify_block(Path(spec["plan_path"]).read_text(encoding="utf-8"))
        except (OSError, KeyError, UnicodeError):
            pass
        for command in parsed.get("commands", []) if parsed.get("ok") else [None]:
            log.emit("command_output", util.now_iso(), name="exec", turn_id=turn_id,
                     input=json.dumps({"command": command}) if command is not None else "{}",
                     output="ok\nExit code: 0", exit_code=0)
    log.emit("terminal", util.now_iso(),
             outcome="failed" if failed else "completed",
             reason="fake failure" if failed else "ok")
    return 1 if failed else 0


# ------------------------------------------------------------ 真实分支

class _Collector:
    """pi_session_get 分页采集 + UiMessage → events.jsonl 映射。

    每轮拉取最新页，随后沿 ``messageBefore`` 游标向历史翻页直到与已见最大
    消息序衔接（或 hasMoreBefore=false）；按消息 id + 内容指纹去重，新消息
    按时间序补发。页链断裂/页数超限 → ``truncated=True``（阻止成功判定，
    已采证据全部保留）。
    """

    def __init__(self, client: "runtime_client.RuntimeClient",
                 session_id: str, log: _EventLog, marker: str, previous_events=(),
                 retain_messages: bool = False):
        self.client = client
        self.session_id = session_id
        self.log = log
        self.marker = marker
        self.seen: dict = {}          # str(message_id) -> (fingerprint, revision)
        # Prior events establish revision floors only. Their flattened content
        # cannot reconstruct a Host message fingerprint, so re-read it normally.
        self.revision_floors = {}
        for event in previous_events:
            mid, revision = event.get("source_message_id"), event.get("source_revision")
            if isinstance(mid, str) and mid and type(revision) is int and revision > 0:
                self.revision_floors[mid] = max(self.revision_floors.get(mid, 0), revision)
        self.verified_end = 0         # 已核验的物理前缀长度（[0, verified_end)）
        self._prefix_hashes: list = []  # 该前缀逐条指纹（索引对齐，None=未采到）
        # 会话归属锚点（§5.23.8）：物理下标最小的 user 消息 = 本轮派发的 prompt。
        # 用户随后在同一会话追加的指令不改变锚点；锚点不符 → 会话不是本轮的。
        self.first_user_index = None
        self.first_user_message = None
        self.input_resolutions = {}   # (turn_id, tool_call_id) -> durable resolution kind
        self.user_messages = {}       # physical index -> user content; recovery forbids appended instructions
        self.messages = {}            # opt-in physical transcript for strict turn recovery
        self.retain_messages = retain_messages
        self._last_page_start = object()
        self.unrecognized = False
        self.truncated = False
        self.read_error = None        # 首个读取错误（保留现场原因）
        self.gate_open = False        # session 事件落盘后才放出采集事件
        self._queued: list = []

    def poll(self) -> None:
        """Read a complete physical page chain; keep partial evidence on error.

        截断/读失败是**本次轮询**的观察结果，不是轮次终态：每轮重新派生，一次
        瞬时截断（或控制面偶发超限）不得把整轮钉死成永远无法完成。
        """
        self.truncated = False
        try:
            self._poll_inner()
            self.read_error = None
        except util.KanpilotError as exc:
            self.read_error = exc.message

    def _page(self, boundary, limit: int):
        """取一页；控制面因结果超出大小上限而截断时，减半缩小消息数重试。

        返回 ``(result, truncated, limit)``；``limit`` 为本次实际生效的页大小，
        每个有进展的物理页之后恢复初始页大小，避免局部超大消息耗尽
        后续普通消息的物理页预算。最小页仍被截断
        → ``(None, True, limit)``（调用方按分页链断裂处理，已采证据全部保留）。
        """
        while True:
            result = self.client.session_get(
                self.session_id, message_before=boundary,
                message_limit=limit,
            )
            if not result.get("truncated"):
                return result, False, limit
            if limit <= _PAGE_MIN_LIMIT:
                return None, True, limit
            limit = max(_PAGE_MIN_LIMIT, limit // 2)

    def _connects(self, start: int, end: int, messages: list) -> bool:
        """本页是否已与「已核验前缀」衔接（不必再把整段历史重读一遍）。

        仅当此前完成过至少一次自 0 起的完整链，且本页覆盖该前缀末端、**页内
        与前缀重叠的整段指纹逐一相符**（历史未被改写）时才成立；否则继续向前
        翻页（安全回退到整段重读）。
        """
        if self.verified_end <= 0:
            return False
        if not start < self.verified_end <= end:
            return False
        overlap = self.verified_end - start
        if overlap <= 0 or overlap > len(messages):
            return False
        if len(self._prefix_hashes) < self.verified_end:
            return False
        for offset in range(overlap):
            message = messages[offset]
            recorded = self._prefix_hashes[start + offset]
            if not isinstance(message, dict) or recorded is None:
                return False
            if self._fingerprint(message) != recorded:
                return False
        return True

    def _mark_verified(self, pages: list, connected: bool) -> None:
        """链已接上 → 补齐「已核验前缀」指纹表（下次轮询据此提前收敛）。"""
        if not connected or not pages:
            return
        newest_end = pages[0][1]
        if newest_end <= self.verified_end:
            return
        if len(self._prefix_hashes) != self.verified_end:
            return                        # 指纹表与前缀长度不符 → 不推进
        fresh = {}
        for start, _end, messages in pages:
            for offset, message in enumerate(messages):
                index = start + offset
                if index >= self.verified_end and isinstance(message, dict):
                    fresh[index] = self._fingerprint(message)
        for index in range(self.verified_end, newest_end):
            self._prefix_hashes.append(fresh.get(index))
        self.verified_end = newest_end

    def _poll_inner(self) -> None:
        boundary = None
        pages: list = []               # [(start, end, messages)]，新 → 旧
        connected = False
        limit = _PAGE_LIMIT
        try:
            for _ in range(_MAX_PAGES_PER_POLL):
                result, truncated, limit = self._page(boundary, limit)
                if truncated:
                    self.truncated = True
                    break
                page = result.get("session")
                if not isinstance(page, dict):
                    raise util.KanpilotError("not_found", "pi_session_get 未返回 session")
                if page.get("id") not in (None, self.session_id):
                    raise util.KanpilotError("conflict", "transcript session 身份不符")
                messages = page.get("messages")
                if not isinstance(messages, list):
                    raise util.KanpilotError("internal", "pi_session_get messages 不是数组")
                start, end = page.get("messageStart"), page.get("messageEnd")
                more = page.get("hasMoreBefore")
                if (type(start) is not int or type(end) is not int
                        or not isinstance(more, bool) or start < 0 or end < start
                        or (boundary is not None and (end != boundary or start >= boundary))
                        or (boundary is None and page.get("hasMoreAfter") is True)
                        or (messages and end - start != len(messages))):
                    self.truncated = True
                    break
                pages.append((start, end, messages))
                if self._connects(start, end, messages):
                    connected = True
                    break
                if not more:
                    if start != 0:
                        self.truncated = True
                    else:
                        connected = True
                    break
                if start == 0:
                    self.truncated = True
                    break
                boundary = start
                limit = _PAGE_LIMIT
            else:
                self.truncated = True
        finally:
            for page_start, _end, messages in reversed(pages):
                for offset, message in enumerate(messages):
                    if isinstance(message, dict):
                        self._emit(message, page_start + offset)
                    else:
                        self.unrecognized = True
            self._mark_verified(pages, connected)

    def _fingerprint(self, message: dict) -> str:
        payload = json.dumps(
            [
                message.get("role"),
                message.get("content"),
                message.get("toolName") or message.get("name"),
                message.get("toolCallId"),
                message.get("parentToolCallId"),
                message.get("agentName"),
                message.get("toolStatus") or message.get("status"),
                message.get("toolArgs", message.get("arguments")),
                message.get("toolResult", message.get("output")),
                message.get("exitCode"),
                message.get("toolCompletedAt"),
                message.get("isError"),
                message.get("turnId") or message.get("turn_id"),
                message.get("timestamp") or message.get("createdAt"),
            ],
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def drain(self) -> None:
        """开闸：首个 session 事件已写 → 依序放出缓冲的采集事件。"""
        self.gate_open = True
        queue, self._queued = self._queued, []
        for etype, ts, fields in queue:
            self.log.emit(etype, ts, **fields)

    def preserve_unverified(self, run_dir: Path, token: Optional[str] = None) -> None:
        """Keep pre-proof observations without violating the first-session event rule."""
        if not self._queued:
            return
        with open(Path(run_dir) / "unverified-events.jsonl", "a", encoding="utf-8") as stream:
            for etype, ts, fields in self._queued:
                line = json.dumps(
                    {"type": etype, "ts": ts, **fields},
                    ensure_ascii=False, sort_keys=True,
                )
                stream.write(util.redact(line, secrets=[token] if token else []) + "\n")
        self._queued.clear()

    def _queue(self, etype: str, ts, **fields) -> None:
        if self.gate_open:
            self.log.emit(etype, ts, **fields)
        else:
            self._queued.append((etype, ts, fields))

    def _emit(self, message: dict, index: Optional[int] = None) -> None:
        if self.retain_messages and index is not None:
            self.messages[index] = message
        mid = message.get("id")
        if not isinstance(mid, str) or not mid:
            self.unrecognized = True
            return
        fingerprint = self._fingerprint(message)
        previous = self.seen.get(mid)
        if previous and previous[0] == fingerprint:
            return
        revision = (previous[1] + 1) if previous else self.revision_floors.get(mid, 0) + 1
        self.seen[mid] = (fingerprint, revision)
        role = str(message.get("role") or "").lower()
        source = {"source_message_id": mid, "source_revision": revision}
        source_turn = message.get("turnId") or message.get("turn_id")
        if isinstance(source_turn, str) and source_turn:
            source["turn_id"] = source_turn
        if not role:
            self.unrecognized = True
            return
        content = message.get("content")
        if isinstance(content, list):
            # content 分块（text/thinking 等）拍平为文本。
            parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)
        if not isinstance(content, str):
            content = "" if content is None else json.dumps(
                content, ensure_ascii=False, sort_keys=True
            )
        ts = message.get("timestamp") or message.get("createdAt") \
            or util.now_iso()
        if util.parse_ts(message.get("timestamp") or message.get("createdAt")) is None:
            # The event still needs a collection time, but it is not proof of
            # when Plus executed this message.
            source["source_timestamp_missing"] = True
        extra = {}
        if message.get("parentToolCallId"):
            extra["parent_tool_call_id"] = message["parentToolCallId"]
        if message.get("agentName"):
            extra["agent_name"] = message["agentName"]
        if role == "user":
            if index is not None:
                self.user_messages[index] = content
            if index is not None and (self.first_user_index is None
                                      or index < self.first_user_index):
                self.first_user_index = index
                self.first_user_message = content
            self._queue("user", ts, content=content, **source, **extra)
        elif role in ("system", "thinking"):
            self._queue("system", ts, content=content, **source, **extra)
        elif role in ("assistant", "assistant_message"):
            self._queue("assistant_message", ts, content=content, **source, **extra)
        elif role in ("tool", "toolcall", "tool_call", "toolresult", "tool_result"):
            name = message.get("toolName") or message.get("name") or "tool"
            status = str(message.get("toolStatus") or message.get("status") or "").lower()
            arguments = message.get("toolArgs", message.get("arguments"))
            input_json = arguments if isinstance(arguments, str) else \
                json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
            output = message.get("toolResult", message.get("output"))
            raw_output = output
            if not isinstance(output, str):
                output = None if output is None else json.dumps(
                    output, ensure_ascii=False, sort_keys=True
                )
            completed_at = message.get("toolCompletedAt")
            is_error = message.get("isError") is True or status in ("error", "failed", "aborted")
            finished = role in ("toolresult", "tool_result") or status in (
                "success", "completed", "done", "finished", "error", "failed", "aborted",
            ) or completed_at is not None or output is not None
            if (finished and not is_error and str(name).lower() in ("asktool","askuserquestion")
                    and isinstance(raw_output,dict) and isinstance(raw_output.get("details"),dict)
                    and isinstance(source_turn,str) and isinstance(message.get("toolCallId"),str)):
                kind=raw_output["details"].get("resolutionKind")
                if kind in ("answered","stopped","aborted"):
                    self.input_resolutions[(source_turn,message["toolCallId"])]=kind
            tool_extra = dict(source)
            tool_extra.update(extra)
            if message.get("toolCallId") is not None:
                tool_extra["tool_call_id"] = str(message["toolCallId"])
            if not finished:
                self._queue("tool_call", ts, name=name,
                            arguments=input_json, **tool_extra)
            elif _is_shell_tool(name):
                exit_code = message.get("exitCode")
                if isinstance(raw_output, dict):
                    exit_code = raw_output.get("exitCode", raw_output.get("exit_code", exit_code))
                    details = raw_output.get("details")
                    if exit_code is None and isinstance(details, dict):
                        exit_code = details.get("exitCode", details.get("exit_code"))
                self._queue(
                    "command_output", ts, name=name, input=input_json,
                    output=output,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    is_error=is_error, **tool_extra,
                )
            else:
                self._queue("tool_result", ts, name=name,
                            input=input_json, output=output,
                            is_error=is_error, **tool_extra)
        else:
            # Unknown message shapes are evidence, but must block a success.
            self.unrecognized = True
            self._queue("system", ts, content=content,
                        source_role=role, **source, **extra)


def _heartbeat(spec: dict) -> None:
    """回报核心（request_id/db_path 由 dispatch-start 写入 spec）。"""
    request_id = spec.get("request_id")
    db_path = spec.get("db_path")
    if not request_id or not db_path:
        raise util.KanpilotError(
            "internal", "spec 缺少 request_id/db_path，无法回报心跳"
        )
    cli.run("heartbeat", {
        "request_id": request_id,
        "run_id": spec["run_id"],
        "progress": "executing",
    }, Path(db_path))


def _turn_status(record: dict) -> str:
    """pi_turn_get 结果 → 归一状态（running/completed/error/aborted/…）。"""
    turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
    return str(turn.get("status") or "").lower()


def _turn_identity_matches(record: dict, session_id: str, turn_id: str) -> bool:
    """Every persistent turn response must identify this exact turn."""
    turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
    return (
        isinstance(turn, dict)
        and str(turn.get("sessionId") or turn.get("session_id") or "") == str(session_id)
        and str(turn.get("turnId") or turn.get("turn_id") or "") == str(turn_id)
    )


def _core_input_state(spec):
    if not spec or not spec.get("db_path"): return None
    try:
        task=cli.run("get-task",{"task_id":spec["task_id"]},Path(spec["db_path"]))["task"]
        if task.get("run_id") != spec["run_id"]: return None
        return {"task_id":task["task_id"],"run_id":task["run_id"],"extra_json":util.jdump(task.get("extra") or {})}
    except util.KanpilotError: return None


def _input_wait_seconds(spec):
    from .execution_questions import wait_seconds
    task=_core_input_state(spec)
    return wait_seconds(task,util.now_dt(),run_id=spec["run_id"]) if task else 0


class _ExecutionInputObserver:
    """Poll summary every two seconds, fetch details only for new identities."""
    def __init__(self,spec,job,log):
        self.spec,self.job,self.log=spec,job,log
        self.known={}
        self.stop_info=None
        self.terminal_failure=None
        task=_core_input_state(spec)
        if task:
            from .execution_questions import open_records
            self.known={q["request_id"]:q for q in open_records(task)}
            self.stop_info=next((q for q in self.known.values() if q.get("stop_requested_at")),None)

    def _report(self,q,state,detail=None):
        payload={"task_id":self.spec["task_id"],"run_id":self.spec["run_id"],
                 "session_id":self.job["session_id"],"turn_id":q["turn_id"],
                 "request_id":q["request_id"],"tool_call_id":q["tool_call_id"],"state":state}
        if state=="open": payload["questions"]=q["questions"]
        if detail: payload["detail"]=detail
        return cli.run("report-execution-question",payload,Path(self.spec["db_path"]))

    def observe(self,reader,status,record):
        from . import execution_questions as eq
        task=_core_input_state(self.spec)
        if task:
            self.stop_info=next((q for q in eq.records(task) if q["run_id"]==self.spec["run_id"] and q.get("stop_requested_at")),self.stop_info)
        terminal=_turn_status(record) in ("completed","error","aborted")
        if terminal:
            if self.known:
                # A durable ask result can precede completed between two polls.
                # Core reads the complete transcript and applies the same identity gate.
                cli.run("reconcile-execution-question", {"task_id":self.spec["task_id"],"run_id":self.spec["run_id"]},Path(self.spec["db_path"]))
                task=_core_input_state(self.spec)
                self.known={q["request_id"]:q for q in eq.open_records(task)} if task else self.known
            failed=[q for q in eq.records(task) if q["run_id"]==self.spec["run_id"]
                and q["turn_id"]==self.job["turn_id"] and q["state"] in ("expired","stopped")] if task else []
            if failed: self.terminal_failure=failed[-1]
            return bool(self.known)
        inner=status.get("status") or status
        summary=inner.get("pendingInputs")
        if summary is None: return bool(self.known) # historical host: no new capability gate
        if (not isinstance(summary,dict) or type(summary.get("count")) is not int
                or not isinstance(summary.get("requestIds"),list)
                or summary["count"] != len(summary["requestIds"])
                or any(not isinstance(v,str) for v in summary["requestIds"])):
            raise util.KanpilotError("protocol_error","执行问题摘要无效")
        ids=set(summary["requestIds"])
        new=ids-set(self.known)
        if new:
            pending=reader.input_pending(self.job["session_id"])
            if pending.get("sessionId")!=self.job["session_id"] or not isinstance(pending.get("asks"),list):
                raise util.KanpilotError("conflict","执行问题 pending 身份不符")
            for ask in pending["asks"]:
                if not isinstance(ask,dict) or ask.get("requestId") not in new: continue
                turn=ask.get("turnId",inner.get("currentTurnId"))
                if turn != self.job["turn_id"] or inner.get("currentTurnId")!=self.job["turn_id"]:
                    raise util.KanpilotError("conflict","执行问题 turn 身份不符")
                q={"request_id":ask.get("requestId"),"tool_call_id":ask.get("toolCallId"),"turn_id":turn,"questions":ask.get("questions")}
                try: result=self._report(q,"open")
                except util.KanpilotError as exc:
                    # dispatch-confirm can race the first ask in the Goal turn.
                    # Keep pending and try next poll; no answer or send is replayed.
                    if exc.code=="conflict": continue
                    raise
                self.known[q["request_id"]]=q
                self.log.emit("system",util.now_iso(),content="input_requested",request_id=q["request_id"])
        for rid,q in list(self.known.items()):
            if rid not in ids and inner.get("currentTurnId")==self.job["turn_id"] and inner.get("isRunning") is True:
                self._report(q,"closed")
                self.known.pop(rid,None)
        task=_core_input_state(self.spec)
        if task:
            current=eq.open_records(task)
            self.known={q["request_id"]:q for q in current}
        self.job["pending_input"]=list(self.known)
        self.job["input_wait_seconds"]=_input_wait_seconds(self.spec)
        return bool(self.known) or bool(ids)

    def expired(self):
        maximum=(self.spec.get("config") or {}).get("execution_question_max_wait_seconds",constants.EXECUTION_QUESTION_MAX_WAIT_SECONDS)
        if not maximum: return False
        opened=[util.parse_ts(q.get("opened_at")) for q in self.known.values()]
        return any(value and (util.now_dt()-value).total_seconds()>=maximum for value in opened)


class _ReadRecovery:
    """Reconnect only observation calls; never replay a desktop mutation."""

    def __init__(self, client, connection_file, session_id, turn_id, *, require_identity=False):
        self.client = client
        self.connection_file = connection_file
        self.session_id = session_id
        self.turn_id = turn_id
        self.pid = None
        self.allow_input_recovery = False
        self.require_identity = require_identity

    def _checked_turn(self, record):
        if not _turn_identity_matches(record, self.session_id, self.turn_id):
            raise util.KanpilotError("conflict", "turn_identity_mismatch: 持久轮次身份不符")
        if _turn_status(record) not in ("running", "completed", "error", "aborted"):
            raise util.KanpilotError("conflict", "turn 状态未知，无法归因")
        return record

    def _read(self, method, *args, **kwargs):
        deadline = time.monotonic() + _READ_RECOVERY_SECONDS
        self.client._timeout = min(2.0, _READ_RECOVERY_SECONDS / 5)
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except util.KanpilotError as exc:
            if exc.code != "remote_unavailable":
                raise
            last = exc
        for _ in range(_READ_RECOVERY_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                reconnect = getattr(self.client, "reconnect", None)
                if not callable(reconnect):
                    raise util.KanpilotError(
                        "remote_unavailable", "runtime MCP client 不支持安全重连")
                candidate = reconnect()
                candidate._timeout = min(1.0, remaining / 5)
                turn = self._checked_turn(candidate.turn_get(
                    self.session_id, self.turn_id, **({"require_identity": True} if self.require_identity else {})))
                if _turn_status(turn) == "running":
                    status=candidate.agent_status(self.session_id)
                    inner=status.get("status") or status
                    pending=inner.get("pendingInputs") or {}
                    if inner.get("currentTurnId")!=self.turn_id or not pending.get("count"):
                        raise util.KanpilotError("conflict", "服务恢复后 turn 仍 running，需人工核实")
                self.client = candidate
                self.pid = getattr(candidate, "harness_pid", None)
                if method == "turn_get":
                    return turn
                return getattr(candidate, method)(*args, **kwargs)
            except util.KanpilotError as exc:
                if exc.code in ("conflict", "not_found"):
                    raise
                last = exc
        raise util.KanpilotError("remote_unavailable", "只读重连失败，可能仍在运行：" + last.message)

    def turn_get(self, session_id, turn_id):
        return self._checked_turn(self._read("turn_get", session_id, turn_id,
                                             **({"require_identity": True} if self.require_identity else {})))

    def session_get(self, session_id, **kwargs):
        return self._read("session_get", session_id, **kwargs)

    def agent_status(self, session_id):
        return self._read("agent_status", session_id)

    def input_pending(self,session_id):
        return self._read("input_pending",session_id)


def _pending_goal_contract(view: dict, session_id: str, turn_id: str) -> Optional[dict]:
    """Never infer identity from the queried session or choose between proposals."""
    plans = view.get("plans")
    if not isinstance(plans, list):
        return None
    pending = [item for item in plans if isinstance(item, dict)
               and item.get("kind") == "goal" and item.get("status") == "pending"]
    if len(pending) != 1:
        return None
    proposal = pending[0]
    if (proposal.get("sessionId") != session_id or proposal.get("turnId") != turn_id
            or any(not isinstance(proposal.get(key), str) or not proposal[key].strip()
                   for key in ("id", "toolCallId"))
            or not isinstance(proposal.get("version"), int)
            or isinstance(proposal.get("version"), bool) or proposal["version"] < 1):
        return None
    if proposal.get("expiresAt") is not None:
        expiry = _strict_turn_time(proposal["expiresAt"])
        if expiry is None or expiry <= util.now_dt():
            return None
    return proposal


def _strict_turn_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def prepare_goal_continuation(spec: dict, job: dict, client) -> dict:
    """Read-only preflight, repeated by the observer before its one bound write."""
    expected_scope = runtime_client.stable_scope_id(
        "execution", task_id=spec["task_id"], run_id=spec["run_id"])
    if (job.get("scope_id") != expected_scope or client.scope_id != expected_scope
            or (spec.get("config") or {}).get("permission_mode") != "auto"
            or job.get("config") != spec.get("config")
            or job.get("task_id") != spec["task_id"] or job.get("run_id") != spec["run_id"]
            or job.get("backend") != "pi_desktop"):
        raise util.KanpilotError("forbidden", "goal_continuation_identity: 非当前自动执行 scope")
    from . import repair_prompt
    if not spec.get("plan_sha256"):
        raise util.KanpilotError("validation_failed", "goal_continuation_plan: 原计划哈希缺失")
    body = repair_prompt.execution_body(spec)
    session_id, original = job["session_id"], job["turn_id"]
    status = client.agent_status(session_id)
    inner = status.get("status") or status
    current = inner.get("currentTurnId")
    if (inner.get("planningState") not in ("planning", "awaiting_approval")
            or not isinstance(current, str) or not current
            or inner.get("pendingPermission")):
        raise util.KanpilotError("conflict", "goal_continuation_pending: 无可自动批准的当前目标")
    proposal = _pending_goal_contract(client.plans_pending(session_id), session_id, current)
    if proposal is None:
        raise util.KanpilotError("conflict", "goal_continuation_pending: 当前唯一 pending Goal 身份不完整或已过期")
    record = client.turn_get(session_id, current, require_identity=True)
    if not _turn_identity_matches(record, session_id, current) or _turn_status(record) != "completed":
        raise util.KanpilotError("conflict", "goal_continuation_turn: 当前目标轮次尚未持久完成")
    if current != original:
        history = job.get("turn_adopted") or []
        if (len(history) >= _MAX_TURN_ADOPTIONS
                or any(item.get("to") == current for item in history)):
            raise util.KanpilotError("conflict", "goal_continuation_turn: 后续轮次已接续或超出接续上限")
        previous = client.turn_get(session_id, original, require_identity=True)
        old = previous.get("turn") if isinstance(previous.get("turn"), dict) else previous
        new = record.get("turn") if isinstance(record.get("turn"), dict) else record
        old_end, new_end = _strict_turn_time(old.get("endedAt")), _strict_turn_time(new.get("endedAt"))
        if (not _turn_identity_matches(previous, session_id, original)
                or _turn_status(previous) != "error"
                or old.get("errorCode") not in ("EMPTY_MODEL_RESPONSE", "NETWORK_ERROR")
                or old_end is None or new_end is None or new_end <= old_end):
            raise util.KanpilotError("conflict", "goal_continuation_turn: 原瞬时失败与后续轮次证据不符")
    collector = _Collector(client, session_id, None, spec.get("session_marker") or "")
    collector.poll()
    expected = (spec.get("instruction") or "").rstrip() + "\n\n" + body
    normalize = lambda text: re.sub(r"\s+", " ", str(text or "")).strip()
    if (collector.truncated or collector.read_error or collector.unrecognized
            or len(collector.user_messages) != 1
            or normalize(collector.first_user_message) != normalize(expected)):
        raise util.KanpilotError("conflict", "goal_continuation_prompt: 转录不完整、原始授权不符或有追加用户指令")
    after = client.agent_status(session_id)
    after = after.get("status") or after
    if any(inner.get(key) != after.get(key) for key in
           ("currentTurnId", "planningState", "isRunning", "pendingPermission")):
        raise util.KanpilotError("conflict", "goal_continuation_changed: 当前会话状态已变化")
    return {"scope_id": expected_scope, "session_id": session_id,
            "original_turn_id": original, "turn_id": current,
            "proposal_id": proposal["id"], "tool_call_id": proposal["toolCallId"],
            "version": proposal["version"]}


def prepare_turn_recovery(spec: dict, job: dict, client, *, turn_id=None) -> Optional[dict]:
    """Prove a read-only continuation; an explicit turn binds user-authorized recovery.

    Automatic recovery rejects appended user instructions: an owned session and
    later timestamps alone do not prove that its next task is this execution.
    Explicit selection relaxes only that ambiguity, never identity, transcript,
    ordering or pending-Goal checks. This function performs no desktop writes.
    """
    expected_scope = runtime_client.stable_scope_id(
        "execution", task_id=spec["task_id"], run_id=spec["run_id"])
    if (job.get("scope_id") != expected_scope or client.scope_id != expected_scope
            or job.get("task_id") != spec["task_id"] or job.get("run_id") != spec["run_id"]
            or job.get("backend") != "pi_desktop" or job.get("config") != spec.get("config")):
        raise util.KanpilotError("forbidden", "turn_recovery_identity: 非本轮 execution scope")
    session_id, original = job.get("session_id"), job.get("turn_id")
    if not session_id or not original:
        raise util.KanpilotError("conflict", "turn_recovery_identity: 缺少原始轮次身份")
    status = client.agent_status(session_id)
    inner = status.get("status") or status
    current = inner.get("currentTurnId")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id or turn_id != current):
        raise util.KanpilotError("conflict", "turn_recovery_candidate: 显式轮次不是该会话当前轮次")
    if not isinstance(current, str) or not current or current == original:
        return None
    if (inner.get("sessionId", session_id) != session_id
            or inner.get("planningState") in ("planning", "awaiting_approval")
            or inner.get("pendingPermission") or (inner.get("pendingInputs") or {}).get("count")):
        raise util.KanpilotError("conflict", "turn_recovery_pending: 会话身份不符或仍有待处理审批/输入")
    history = job.get("turn_adopted") or []
    if len(history) >= _MAX_TURN_ADOPTIONS or any(item.get("to") == current for item in history):
        raise util.KanpilotError("conflict", "turn_recovery_candidate: 重复轮次或超出接续上限")
    old_record = client.turn_get(session_id, original, require_identity=True)
    new_record = client.turn_get(session_id, current, require_identity=True)
    old = old_record.get("turn") if isinstance(old_record.get("turn"), dict) else old_record
    new = new_record.get("turn") if isinstance(new_record.get("turn"), dict) else new_record
    old_state, new_state = _turn_status(old_record), _turn_status(new_record)
    if (not _turn_identity_matches(old_record, session_id, original)
            or not _turn_identity_matches(new_record, session_id, current)):
        raise util.KanpilotError("conflict", "turn_recovery_identity: 持久轮次身份不符")
    if old_state not in ("error", "aborted"):
        return None
    if (old.get("errorCode") not in ("TURN_ABORTED", "EMPTY_MODEL_RESPONSE", "NETWORK_ERROR")
            or new_state not in ("running", "completed")
            or inner.get("isRunning") is not (new_state == "running")):
        raise util.KanpilotError("conflict", "turn_recovery_state: 原失败或后续轮次状态不支持接续")
    old_start, old_end = _strict_turn_time(old.get("startedAt")), _strict_turn_time(old.get("endedAt"))
    new_start, new_end = _strict_turn_time(new.get("startedAt")), _strict_turn_time(new.get("endedAt"))
    if (old_end is None or (old.get("startedAt") is not None and old_start is None)
            or (old_start is not None and old_end < old_start)
            or (new.get("startedAt") is not None and new_start is None)
            or (new_state == "completed" and new_end is None)):
        raise util.KanpilotError("conflict", "turn_recovery_time: 缺少完整且有序的持久轮次时序")
    from . import repair_prompt
    if not spec.get("plan_sha256"):
        raise util.KanpilotError("validation_failed", "turn_recovery_plan: 原计划哈希缺失")
    body = repair_prompt.execution_body(spec)
    expected = (spec.get("instruction") or "").rstrip() + "\n\n" + body
    collector = _Collector(client, session_id, None, spec.get("session_marker") or "", retain_messages=True)
    collector.poll()
    normalize = lambda text: re.sub(r"\s+", " ", str(text or "")).strip()
    events, _detail = dsh_events.iter_events(Path(spec["runs_dir"]) / dsh_events.EVENT_LOG_NAME)
    session = dsh_events.session_event(events) or {}
    first = collector.messages.get(collector.first_user_index) or {}
    anchor_turn = first.get("turnId") or first.get("turn_id")
    if (collector.truncated or collector.read_error or collector.unrecognized
            or not collector.messages or collector.verified_end != len(collector.messages)
            or normalize(collector.first_user_message) != normalize(expected)
            or session.get("session_id") != session_id or not session.get("turn_id")
            or anchor_turn != session["turn_id"]
            or (turn_id is None and len(collector.user_messages) != 1)):
        raise util.KanpilotError("conflict", "turn_recovery_prompt: 转录不完整、派发锚点不符或存在未明确归属的追加指令")
    candidate_transcript = [message for message in collector.messages.values()
        if (message.get("turnId") or message.get("turn_id")) == current]
    time_basis = "persistent_turn"
    started_at = new.get("startedAt")
    if new_start is None:
        # Current Plus versions expose endedAt but omit startedAt. Identity was
        # established above; only timestamps on this exact turn may supply its
        # observed lower bound, never the latest unrelated session message.
        times = [(message.get("timestamp") or message.get("createdAt"),
                  _strict_turn_time(message.get("timestamp") or message.get("createdAt")))
                 for message in candidate_transcript]
        if not times or any(stamp is None for _raw, stamp in times):
            raise util.KanpilotError("conflict", "turn_recovery_time: 后续轮次缺少可核实的开始时序")
        started_at, new_start = min(times, key=lambda item: item[1])
        time_basis = "transcript_turn_messages"
    if new_start <= old_end or (new_end is not None and new_end < new_start):
        raise util.KanpilotError("conflict", "turn_recovery_time: 原失败与后续轮次时序不符")
    candidate_messages = [message for message in candidate_transcript
        if str(message.get("role") or "").lower() in ("assistant", "assistant_message")]
    if not candidate_messages or any(
            (stamp := _strict_turn_time(message.get("timestamp") or message.get("createdAt"))) is None
            or stamp < new_start or (new_end is not None and stamp > new_end)
            for message in candidate_messages):
        raise util.KanpilotError("conflict", "turn_recovery_transcript: 后续轮次缺少可核实的 assistant 转录与时序")
    after = client.agent_status(session_id)
    after = after.get("status") or after
    if any(inner.get(key) != after.get(key) for key in
           ("currentTurnId", "planningState", "isRunning", "pendingPermission", "pendingInputs")):
        raise util.KanpilotError("conflict", "turn_recovery_changed: 会话状态在核验期间发生变化")
    if client.turn_get(session_id, current, require_identity=True) != new_record:
        raise util.KanpilotError("conflict", "turn_recovery_changed: 持久后续轮次在核验期间发生变化")
    return {"scope_id": expected_scope, "session_id": session_id,
            "original_turn_id": original, "turn_id": current,
            "original_ended_at": old["endedAt"], "started_at": started_at, "time_basis": time_basis,
            "prompt_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
            "explicit": turn_id is not None}


def _resume_job(job_path: Path, spec: dict):
    """``--resume`` 前置核验（§5.23.7）：只接受本轮自己的、已确认发送的现场。

    返回 ``(job, error)``；任一不符（不可读/身份不符/未确认发送/缺 session·
    turn）→ ``(None, 原因)``，调用方不续接且不改既有证据。
    """
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"job.json 不可读：{exc}"
    except ValueError:
        return None, "job.json 解析失败（半写入或损坏）"
    if not isinstance(job, dict):
        return None, "job.json 不是 JSON 对象"
    try:
        execution_contract.validate_spec(spec, job=job)
    except (util.KanpilotError, KeyError) as exc:
        return None, "执行阶段契约不可确认：" + str(exc)
    if job.get("task_id") != spec["task_id"] \
            or job.get("run_id") != spec["run_id"]:
        return None, "job.json 身份与目录不符"
    if job.get("backend") != "pi_desktop":
        return None, f"job.json backend 不符（{job.get('backend')!r}）"
    if not job.get("session_id") or not job.get("turn_id"):
        return None, "轮次缺少持久会话身份（session_id/turn_id）"
    # send_state=sent 是常规值；旧版把心跳被拒误判为失败时写下的 unknown 也
    # 可续接——「已确认发送」的硬证据是 events.jsonl 的 session 事件（由发送
    # 成功 + 启动证明共同产生），见 run_real 的再次核验。
    if job.get("send_state") not in ("sent", "unknown"):
        return None, f"轮次发送状态不可续接（send_state={job.get('send_state')!r}）"
    return job, None


def _round_remaining_seconds(job: dict, timeout: float, spec=None) -> float:
    """续接观察的剩余预算（自 job.started_at 起算；不可解析 → 全额时限）。"""
    started = util.parse_ts(job.get("started_at"))
    now = util.parse_ts(util.now_iso())
    if started is None or now is None:
        return timeout
    return timeout - max(0.0, now.timestamp() - started.timestamp() - (_input_wait_seconds(spec) if spec else 0))


def required_verification_commands(spec: dict) -> list:
    """Read only frozen plan bytes; never infer requirements from model output."""
    if spec.get("completion_policy") != "command_verification":
        return []
    if not isinstance(spec.get("plan_path"), str) or not spec["plan_path"]:
        raise util.KanpilotError("validation_failed", "缺少冻结验证计划路径")
    raw = Path(spec["plan_path"]).read_bytes()
    if not spec.get("plan_sha256") or hashlib.sha256(raw).hexdigest() != spec["plan_sha256"]:
        raise util.KanpilotError("conflict", "冻结验证计划哈希不符")
    parsed = verify_evidence.parse_verify_block(raw.decode("utf-8"))
    if not parsed.get("ok"):
        raise util.KanpilotError("validation_failed", parsed.get("error") or "冻结验证命令无效")
    commands = parsed["commands"]
    pi_desktop_backend.validate_required_verification_commands(commands, "approve")
    if "required_verification_commands" in spec and spec["required_verification_commands"] != commands:
        raise util.KanpilotError("conflict", "冻结验证命令与计划不符")
    return commands


def run_real(spec: dict, resume: bool = False) -> int:
    return process_owner.run_owned(spec, lambda owner: _run_real(spec, resume, owner))


def _run_real(spec: dict, resume: bool, owner) -> int:
    """真实 pi_desktop 轮次（§5.23.3；写序恒 job→session→terminal）。

    ``resume=True``（§5.23.7）：现场已存在时只读续接观察——不建会话、不下发
    权限、**绝不重发 prompt**。观察环境不成立 → 退出码 3 且既有轮次终态与
    证据原样保留（监控端下一轮可再试）。
    """
    execution_contract.validate_spec(spec, starting=not resume)
    run_dir = Path(spec["runs_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    job_path = run_dir / dsh_events.JOB_NAME
    existing = None
    if job_path.exists():
        if not resume:
            # A persisted intent/result is not permission to send a second prompt.
            return 1
        existing, resume_error = _resume_job(job_path, spec)
        if existing is None:
            print(f"resume rejected: {resume_error}", file=sys.stderr)
            return 1
        if existing.get("status") == "finished":
            return 0        # 已按 completed 收尾的轮次无需再观察
    log = _EventLog(run_dir / dsh_events.EVENT_LOG_NAME)
    if resume:
        # 只有已确认启动（events.jsonl 已有 session 事件）的轮次可续接：否则
        # 该轮从未进入 running，续接会写出与生命周期不符的证据。
        resume_events, _detail = dsh_events.iter_events(
            run_dir / dsh_events.EVENT_LOG_NAME
        )
        session_event = dsh_events.session_event(resume_events)
        if session_event is None or session_event.get("session_id") != existing.get("session_id"):
            print("resume rejected: session 事件缺失（该轮未确认发送/启动）",
                  file=sys.stderr)
            return 1
    job = existing if existing is not None else _base_job(spec)
    collector = None              # session 建立后才存在；emit_session 负责开闸
    proof_ok = {"ok": False}      # 启动证明（turn_get + heartbeat）后才允许写 session
    session_written = {"done": False}

    def emit_session() -> None:
        """首事件 session（§5.23.3：启动证明前绝不写；只写一次）。"""
        if session_written["done"] or not job.get("session_id"):
            return
        if not proof_ok["ok"]:
            return
        session_written["done"] = True
        log.emit("session", util.now_iso(),
                 session_id=job["session_id"],
                 session_title=job.get("session_title") or "pi_desktop 会话",
                 turn_id=job.get("turn_id"))
        if collector is not None:
            collector.drain()       # 放出证明前缓冲的采集事件（session 恒为 seq=1）
    def finalize(status: str, exit_code, detail, terminal: bool,
                 outcome: str = "failed", reason: str = "") -> int:
        """收尾写序：job 先写 → session 补齐 → terminal 最后。"""
        owner.check()
        job.pop("observation_finalizing", None)
        if resume:
            # A terminal takeover is visible only together with its new result,
            # so readers cannot mistake the previous failed snapshot for it.
            job["observation_resumed_at"] = util.now_iso()
        job.update({
            "status": status,
            "exit_code": exit_code,
            "ended_at": util.now_iso(),
            "detail": detail,
        })
        if status == "finished":
            _record_verification_source(job, spec)
        _write_json(job_path, job)
        emit_session()
        if terminal:
            log.emit("terminal", util.now_iso(), outcome=outcome,
                     reason=reason or (detail or status))
        return 0 if status == "finished" else 1

    def defer(detail: str) -> int:
        """续接观察未成立（§5.23.7）：既有轮次终态/证据原样保留，只记本次尝试
        与原因；退出码 3 与「轮次失败」（1）区分，监控端据此下一轮再试。"""
        job.pop("observation_finalizing", None)
        job.update({
            "resume_detail": detail,
            "resume_deferred_at": util.now_iso(),
            "resume_attempts": int(job.get("resume_attempts") or 0) + 1,
        })
        _write_json(job_path, job)
        print(f"resume deferred: {detail}", file=sys.stderr)
        return 3

    def fail(detail: str, reason: str) -> int:
        """正常路径 = 轮次失败；--resume = 只记录（不改轮次终态）。"""
        if resume:
            return defer(detail)
        return finalize("failed", 1, detail, True, "failed", reason)

    if resume:
        # 续接标记先落盘（谁在接管、何时开始）；status/detail 保持原值。
        session_written["done"] = True      # session 事件已存在于现场
        job.pop("observation_finalizing", None)
        job["observe_pid"] = os.getpid()
        job["resume_started_at"] = util.now_iso()
        _write_json(job_path, job)

    # ---- 0. cwd 校验 + job(running) 启动即写（先于任何事件/网络调用）。
    cwd_value = spec.get("cwd")
    cwd = Path(cwd_value) if cwd_value else None
    if cwd is None or not cwd.is_dir():
        if resume:
            return defer(f"cwd_missing: {cwd_value}")
        _write_json(job_path, job)
        return finalize("failed", 1, f"cwd_missing: {cwd_value}",
                        True, "failed", "cwd_missing")
    if not resume:
        _write_json(job_path, job)

    timeout = float(spec.get("timeout_seconds")
                    or constants.DEFAULT_PI_ROUND_TIMEOUT_SECONDS)
    if timeout <= 0:
        timeout = float(constants.DEFAULT_PI_ROUND_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout

    # ---- 1. 计划核验（先于任何远端副作用：不重发、不留孤儿会话）。
    plan_body = ""
    try:
        raw = Path(spec["plan_path"]).read_bytes()
        text = raw.decode("utf-8")
        # 与 util.read_frontmatter/cmd_get_plan 同口径 split("---", 2)。
        if text.startswith("---\n"):
            plan_body = text.split("---", 2)[2]
            if plan_body.startswith("\n"):
                plan_body = plan_body[1:]
        else:
            plan_body = text
    except (OSError, UnicodeDecodeError) as exc:
        return fail(f"plan_unreadable: {exc}", "plan_unreadable")
    expected_sha = spec.get("plan_sha256")
    if expected_sha:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha:
            return fail("plan_mismatch", "plan_mismatch")
    if spec.get("review_repair") is not None or spec.get("repair_prompt"):
        from . import repair_prompt
        try:
            plan_body = repair_prompt.execution_body(spec)
        except util.KanpilotError as exc:
            return fail("repair_materials: " + exc.message, "repair_materials")
    body_bytes = plan_body.encode("utf-8")
    if len(body_bytes) > constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES:
        # 计划正文超限 → 发送前明确失败（不入队、不截断）。
        return fail(f"plan_too_large: {len(body_bytes)} 字节 > 2 MiB",
                    "plan_too_large")

    commands = []
    if not resume or spec.get("continue_goal_authorization"):
        try:
            commands = required_verification_commands(spec)
        except (util.KanpilotError, OSError, UnicodeDecodeError) as exc:
            return fail("verification_requirements_invalid: " + str(exc), "verification_requirements_invalid")

    # ---- 2. 通过 KaneoPilot runtime scope 连接并复核 Plus 工具集。
    owner.check()
    config = spec.get("config") or {}
    try:
        if resume and not job.get("scope_id"):
            client = runtime_client.HistoricalReadClient(
                config.get("connection_file"), timeout=10)
            client.connect(required_tools=(constants.PI_DESKTOP_REQUIRED_TOOLS + constants.PI_DESKTOP_GOAL_TOOLS) if resume else constants.PI_DESKTOP_DISPATCH_TOOLS)
        else:
            client = runtime_client.RuntimeClient(
                role="execution", connection_file=config.get("connection_file"),
                project_path=str(cwd), runtime_config=config,
                invocation_id=None, request_id=None,
                run_id=spec["run_id"], task_id=spec["task_id"], timeout=10,
                lease_seconds=timeout, read_only=resume,
                goal_continuation=spec.get("continue_goal_authorization") if resume else None,
            )
            client.connect(required_tools=(constants.PI_DESKTOP_REQUIRED_TOOLS + constants.PI_DESKTOP_GOAL_TOOLS) if resume else constants.PI_DESKTOP_DISPATCH_TOOLS)
            job["scope_id"] = client.scope_id
        if commands and not client.supports_required_verification_commands():
            raise util.KanpilotError("unsupported_tools", "请升级 Plus：pi_plans_resolve 缺少 requiredVerificationCommands 能力，尚未启动模型")
        if "stage_contract" in spec and not resume:
            from . import harnesses
            description = client.capabilities()
            required = ["durable_status", "verification_report", "input_requests"]
            if commands:
                required.append("verification_command_binding")
            harnesses.require_capabilities(description, required)
            job["harness_capabilities"] = description
        job["pi_desktop_pid"] = getattr(client, "harness_pid", None)
        if not resume:
            _write_json(job_path, job)
    except util.KanpilotError as exc:
        return fail(f"connect_failed: {exc.message}", "connect_failed")

    permission_mode = config.get("permission_mode")
    if resume:
        # ---- 3'-4'. 续接观察（§5.23.7）：只读接管既有 session/turn。
        remaining = _round_remaining_seconds(job, timeout,spec)
        early_status=client.agent_status(job["session_id"])
        early_inner=early_status.get("status") or early_status
        active_input=bool((early_inner.get("pendingInputs") or {}).get("count"))
        if remaining <= 0 and not active_input:
            # 已超时的轮次只能人工判定终态：续接不得先动手停止仍在跑的会话。
            return defer("round_timeout_elapsed: 轮次已超出时限，需人工判定终态")
        deadline = time.monotonic() + remaining
        turn_id = job["turn_id"]
        collector = _Collector(client, job["session_id"], log,
                               spec.get("session_marker") or "", previous_events=resume_events)
        observer = _ReadRecovery(client, config["connection_file"],
                                 job["session_id"], turn_id)
        collector.client = observer
    else:
        # ---- 3. 建会话 + 权限配置（permission_mode 非 inherit 才下发）。
        if permission_mode not in constants.EXEC_PERMISSION_MODES:
            return fail(f"permission_mode 非法：{permission_mode}",
                        "validation_failed")
        # T6（§5.23.10）：展示快照来自 spec（新轮次）；旧 spec 缺字段 → 按旧
        # 语义（title=session_marker），不重新生成、不推断。
        session_display_name = spec.get("session_display_name")
        workspace_display_name = spec.get("workspace_display_name")
        create_title = session_display_name or (
            spec.get("session_marker") or spec["task_id"]
        )
        try:
            owner.check()
            created = client.session_create(
                str(cwd), mode=constants.PI_DESKTOP_EXEC_MODE,
                provider_id=config.get("provider_id"),
                model_id=config.get("model_id"),
                thinking_level=config.get("thinking_level"),
                title=create_title,
                project_name=workspace_display_name,
            )
            session = created.get("session") or {}
            session_id = (session.get("id") or created.get("sessionId")
                          or created.get("id"))
            if not session_id:
                raise util.KanpilotError(
                    "internal", "pi_session_create 未返回 session.id"
                )
            job["session_id"] = session_id
            job["session_title"] = session.get("title") or session.get("name")
            _write_json(job_path, job)
        except util.KanpilotError as exc:
            return fail(f"session_setup_failed: {exc.message}",
                        "session_setup_failed")

        # T6（§5.23.10）：Plus 返回的两种显示名与请求不符 → **不下发
        # agent_prompt**，保留已创建会话 ID 供人工检查，绝不自动再建一次。
        if session_display_name or workspace_display_name:
            returned_title = session.get("title") or session.get("name")
            returned_project = (
                session.get("projectName") or session.get("project_name")
            )
            mismatch = []
            if session_display_name and returned_title != session_display_name:
                mismatch.append(
                    f"title 请求 {session_display_name!r} ≠ 返回 "
                    f"{returned_title!r}"
                )
            if (workspace_display_name
                    and returned_project != workspace_display_name):
                mismatch.append(
                    f"projectName 请求 {workspace_display_name!r} ≠ 返回 "
                    f"{returned_project!r}"
                )
            if mismatch:
                detail = (
                    "session_naming_mismatch: PI-Desktop 返回的显示名与请求"
                    "不符（" + "；".join(mismatch) + "）；已保留会话 "
                    f"{session_id} 供人工检查，未下发 agent_prompt"
                )
                job["session_naming_mismatch"] = mismatch
                return finalize("failed", 1, detail, True, "failed",
                                "session_naming_mismatch")

        try:
            if permission_mode and permission_mode != "inherit":
                owner.check()
                client.session_configure(
                    session_id, mode=constants.PI_DESKTOP_EXEC_MODE,
                    permission_mode=permission_mode,
                )
        except util.KanpilotError as exc:
            return fail(f"session_setup_failed: {exc.message}",
                        "session_setup_failed")

        collector = _Collector(client, job["session_id"], log,
                               spec.get("session_marker") or "")

        # ---- 4. 发送 prompt（instruction + 计划正文；绝不自动重发）。
        prompt = (spec.get("instruction") or "").rstrip() + "\n\n" + plan_body
        turn_id = None
        # A crash during the network call cannot distinguish accepted from unsent.
        owner.check()
        job["send_state"] = "unknown"
        _write_json(job_path, job)
        try:
            owner.check()
            result = client.agent_prompt(job["session_id"], prompt)
            turn_id = result.get("turnId") or result.get("turn_id")
            if not turn_id:
                raise util.KanpilotError(
                    "internal", "pi_agent_prompt 未返回 turnId"
                )
            job["turn_id"] = turn_id
            job["send_state"] = "sent"
            _write_json(job_path, job)
        except util.KanpilotError as exc:
            if exc.code == "validation_failed" and "超过 2 MiB" in exc.message:
                job["send_state"] = "not_sent"
                return finalize("failed", 1, "plan_too_large: " + exc.message,
                                True, "failed", "plan_too_large")
            # 发送响应丢失/未达：可能已受理——读 agent_status 取证，绝不重发。
            recovered = None
            try:
                status_view = client.agent_status(job["session_id"])
                inner = status_view.get("status") or status_view
                recovered = inner.get("currentTurnId")
            except util.KanpilotError:
                recovered = None
            collector.poll()  # 尽力保全已产生的证据
            collector.preserve_unverified(run_dir)
            # currentTurnId is only candidate evidence.  It cannot prove that
            # the prompt response landed, so preserve unknown and require
            # intervention.
            job["send_state"] = "unknown"
            if recovered:
                job["turn_id"] = recovered
                job["detail"] = (
                    f"prompt_response_lost: {exc.message}；"
                    f"currentTurnId 仅作候选证据：{recovered}"
                )
            else:
                job["detail"] = f"prompt_response_lost: {exc.message}"
            _write_json(job_path, job)
            return finalize("failed", 1, job["detail"],
                            True, "failed", "send_unknown")

        observer = _ReadRecovery(client, config["connection_file"],
                                 job["session_id"], turn_id)
        collector.client = observer

    def observe_turn():
        owner.check()
        record = observer.turn_get(job["session_id"], turn_id)
        if observer.pid is not None:
            job["pi_desktop_pid"] = observer.pid
        return record

    # ---- 5.23.8 后续轮次接续：瞬时模型错误后 Plus 会在同一会话自动重开新
    # turn，本轮执行并未中断——接续观察新 turn，绝不重发 prompt。
    expected_prompt = (spec.get("instruction") or "").rstrip() + "\n\n" + plan_body

    def _normalize(text) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def adopt_follow_up(record: dict) -> Optional[str]:
        """可接续的后续 turn_id；任一条安全条件不成立 → None（按失败收尾）。

        条件：①错误码属瞬时（Plus 自动重开）；②会话归属锚点成立——转录里
        物理下标最小的 user 消息仍是本轮派发的 prompt（证明该会话由本轮创建；
        用户随后在同一会话追加的指令不阻止接续，会话被改用则不接续）；
        ③同一会话仍在跑另一个 turn；④未超接续上限、不重复接续同一 turn。
        """
        history = list(job.get("turn_adopted") or [])
        if len(history) >= _MAX_TURN_ADOPTIONS:
            return None
        turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
        code = str(turn.get("errorCode") or turn.get("error_code") or "")
        if code not in _TRANSIENT_TURN_ERRORS:
            return None
        collector.poll()          # 取转录：接续的安全前提是「会话仍是本轮的」
        if _normalize(collector.first_user_message) != _normalize(expected_prompt):
            return None
        try:
            status_view = observer.agent_status(job["session_id"])
        except util.KanpilotError:
            return None
        inner = status_view.get("status") or status_view
        if inner.get("isRunning") is not True:
            return None
        current = inner.get("currentTurnId")
        if not current or current == turn_id:
            return None
        if any(item.get("to") == current for item in history):
            return None
        return str(current)

    def rebind_turn(follow: str) -> None:
        """换绑到同一会话的新 turn 观察（job 先写；不新建会话、不重发 prompt）。

        调用方负责审计记账与事件留痕（§5.23.8 接续 / §5.23.9 契约批准）。
        """
        nonlocal turn_id, observer
        turn_id = follow
        job["turn_id"] = follow
        _write_json(job_path, job)
        observer = _ReadRecovery(client, config["connection_file"],
                                 job["session_id"], follow)
        collector.client = observer

    def adopt(follow: str, record: dict, recovery_authorization=None) -> None:
        """记账并切换到新 turn 的观察（job 先写、事件留痕；不新建会话）。"""
        turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
        code = str(turn.get("errorCode") or turn.get("error_code") or "")
        history = list(job.get("turn_adopted") or [])
        previous = turn_id
        entry = {"from": previous, "to": follow, "code": code, "at": util.now_iso()}
        if recovery_authorization is not None:
            entry["recovery_authorization"] = recovery_authorization
        history.append(entry)
        job["turn_adopted"] = history
        log.emit("system", util.now_iso(),
                 content=(f"turn_adopted: {previous} → {follow}"
                          f"（{code} 后同一会话后续轮次，只读接续观察；"
                          f"未重发 prompt）"))
        rebind_turn(follow)

    def pending_goal_contract() -> Optional[dict]:
        """本会话当前待批准的 Goal 契约（只读；非 goal/无契约 → None）。"""
        view = client.plans_pending(job["session_id"])
        return _pending_goal_contract(view, job["session_id"], turn_id)

    def goal_contract_gate() -> tuple:
        """Goal 契约审批门（§5.23.9）。返回 ``(outcome, detail, reason)``。

        - ``none``：本会话无待处理契约（非 goal 轮次，或执行轮次自身收尾）；
        - ``adopted``：契约已由本机代批，宿主已开启执行轮次并换绑观察；
        - ``failed``：契约未出现/批准失败/执行轮次未开启——明确失败，
          绝不重发 prompt（reason 供终态细分）。
        """
        authorization = spec.get("continue_goal_authorization") if resume else None
        read_only_resume = resume and not authorization
        approval = job.get("goal_approval")
        if isinstance(approval, dict) and approval.get("status") == "approved":
            approved_turn = approval.get("execution_turn_id")
            # Strictly verified read-only recovery continues the already approved
            # execution; it is not authority to approve another Goal contract.
            for adoption in job.get("turn_adopted") or []:
                if (adoption.get("from") == approved_turn
                        and adoption.get("recovery_authorization", {}).get("original_turn_id") == approved_turn
                        and adoption.get("recovery_authorization", {}).get("turn_id") == adoption.get("to")):
                    approved_turn = adoption["to"]
            if not resume or approved_turn == turn_id:
                return ("none", "", "")       # 执行轮次自身收尾，不再有审批门
            return ("deferred", "goal_resume_unconfirmed: 执行轮次与既有审批检查点不符", "")
        if authorization:
            try:
                owner.check()
                origin = dict(job, turn_id=authorization["original_turn_id"])
                history = list(job.get("turn_adopted") or [])
                if history and history[-1].get("from") == authorization["original_turn_id"] and history[-1].get("to") == authorization["turn_id"]:
                    origin["turn_adopted"] = history[:-1]
                if prepare_goal_continuation(spec, origin, client) != authorization:
                    return ("deferred", "goal_continuation_changed: 目标授权身份已变化", "")
            except (util.KanpilotError, OSError, UnicodeDecodeError, IndexError) as exc:
                return ("deferred", "goal_continuation_blocked: " + str(exc), "")
        owned_origin = bool(spec.get("executor_instance_id") or spec.get("original_executor_instance_id")
                            or spec.get("requires_goal_identity"))
        # 只有会话仍处 Goal 契约模式（planning / awaiting_approval）才过审批门：
        # 批准后宿主把会话切回 Agent 模式，执行轮次 completed 即普通收尾；
        # 模型未提交契约就结束（状态仍 planning）才是异常。
        try:
            status_view = client.agent_status(job["session_id"])
        except util.KanpilotError:
            if resume:
                return ("deferred", "goal_resume_unconfirmed: 无法核验当前会话轮次，保留观察现场", "")
            return ("none", "", "")
        inner = status_view.get("status") or status_view
        planning_state = inner.get("planningState")
        if read_only_resume and planning_state in ("planning", "awaiting_approval"):
            return ("deferred", "goal_approval_observation_pending: 只读续接不代批目标契约，请在 Plus 完成审批", "")
        current = inner.get("currentTurnId")
        if read_only_resume and current == turn_id:
            if owned_origin:
                return ("deferred", "goal_resume_unconfirmed: App 归属轮次缺少执行检查点且当前仍是原轮次", "")
            try:
                history = client.plans_pending(job["session_id"]).get("history") or []
            except util.KanpilotError:
                return ("deferred", "goal_resume_unconfirmed: 审批历史不可读，不能确认本轮是否仍是规划轮次", "")
            if any(isinstance(item, dict) and item.get("kind") == "goal"
                   and item.get("status") == "approved" and item.get("sessionId") == job["session_id"]
                   and item.get("turnId") == turn_id for item in history):
                return ("deferred", "goal_resume_unconfirmed: 契约已批准但尚无不同的执行轮次身份", "")
        if read_only_resume and not current:
            try:
                history = client.plans_pending(job["session_id"]).get("history") or []
            except util.KanpilotError:
                history = []
            known_approved_goal = any(isinstance(item, dict) and item.get("kind") == "goal"
                and item.get("status") == "approved" and item.get("sessionId") == job["session_id"]
                and item.get("turnId") == turn_id for item in history)
            if owned_origin or known_approved_goal:
                return ("deferred", "goal_resume_unconfirmed: 缺少当前执行轮次身份，保留观察现场", "")
            # Historical manual agent rounds predate Goal ownership and can
            # have a terminal turn with no current-turn projection.
            return ("none", "", "")
        if read_only_resume and current != turn_id:
            # Approval may have reached Plus before the observer persisted it.
            # A current turn alone is not proof: require the exact approved Goal
            # history and a complete transcript containing only our initial user.
            try:
                owner.check()
                view = client.plans_pending(job["session_id"])
                approved = [item for item in (view.get("history") or [])
                            if isinstance(item, dict) and item.get("kind") == "goal"
                            and item.get("status") == "approved"
                            and item.get("sessionId") == job["session_id"]
                            and item.get("turnId") == turn_id
                            and isinstance(item.get("id"), str) and item["id"]]
                if len(approved) != 1 or not isinstance(current, str) or not current:
                    return ("deferred", "goal_resume_unconfirmed: 缺少旧规划轮次对应的唯一已批准 Goal 或当前执行轮次身份", "")
                record = client.turn_get(job["session_id"], current)
                if (not _turn_identity_matches(record, job["session_id"], current)
                        or _turn_status(record) not in ("running", "completed", "error", "aborted")):
                    return ("deferred", "goal_resume_unconfirmed: 当前执行轮次身份或状态无法确认", "")
                collector.poll()
                if (collector.truncated or collector.read_error or collector.unrecognized
                        or len(collector.user_messages) != 1
                        or _normalize(collector.first_user_message) != _normalize(expected_prompt)):
                    return ("deferred", "goal_resume_unconfirmed: 转录不完整、任务锚点不符或存在追加用户指令", "")
                proposal = approved[0]
                planning_turn = turn_id
                job["goal_approval"] = {"proposal_id": proposal["id"], "status": "approved",
                    "planning_turn_id": planning_turn, "execution_turn_id": current,
                    "approved_by": "plus_history", "recovered_at": util.now_iso(),
                    "execution_started_at": record.get("startedAt")
                        if util.parse_ts(record.get("startedAt")) is not None else None}
                log.emit("system", util.now_iso(), execution_turn_id=current,
                    execution_started_at=job["goal_approval"]["execution_started_at"], content=(
                    f"goal_execution_recovered: {planning_turn} → {current}（已批准 Goal 历史与完整转录核验；只读续接）"))
                rebind_turn(current)
                return ("adopted", "", "")
            except util.KanpilotError:
                return ("deferred", "goal_resume_unconfirmed: 只读审批历史或执行轮次证据不可读，保留观察现场", "")
        if planning_state not in ("planning", "awaiting_approval"):
            return ("none", "", "")
        planning_turn = turn_id
        contract_deadline = time.monotonic() + _GOAL_CONTRACT_WAIT_SECONDS
        while True:
            owner.check()
            try:
                proposal = pending_goal_contract()
            except util.KanpilotError as exc:
                return ("failed", f"goal_contract_unreadable: {exc.message}",
                        "goal_contract_unreadable")
            if proposal is not None:
                break
            if time.monotonic() >= contract_deadline:
                return ("failed",
                        "goal_contract_missing: goal 模式轮次结束但未观察到"
                        f"待批准的目标契约（planningState={planning_state}；"
                        "未重发 prompt）",
                        "goal_contract_missing")
            time.sleep(_GOAL_POLL_SECONDS)
        proposal_id = str(proposal.get("id") or "")
        title = str(proposal.get("title") or "").strip()
        if permission_mode not in constants.EXEC_PERMISSION_MODES:
            return ("failed",
                    f"goal_approval_permission_unknown: 已保存 permission_mode "
                    f"不可用于契约批准：{permission_mode}",
                    "goal_approval_permission_unknown")
        log.emit("system", util.now_iso(),
                 content=(f"goal_contract: 目标契约待批准（{title or proposal_id}）；"
                          f"本机代批后同一会话开启执行轮次"))
        version = proposal.get("version")
        approval_requested_at = None
        try:
            owner.check()
            requested_at = util.now_iso()
            client.plans_resolve(
                proposal_id, job["session_id"],
                proposal["turnId"],
                proposal["toolCallId"],
                "approve",
                target_permission_mode=permission_mode,
                version=version,
                **({"required_verification_commands": commands} if commands else {}),
            )
            # Only our successful resolve call establishes this boundary;
            # a concurrent approval must rely on turn identity/start proof.
            approval_requested_at = requested_at
        except util.KanpilotError as exc:
            # 并发代批（另一个观察进程已批）不算失败：以「契约已不再 pending」
            # 为准继续等执行轮次；仍 pending 才是真失败。
            try:
                still_pending = pending_goal_contract()
            except util.KanpilotError:
                still_pending = None
            if still_pending is not None:
                return ("failed", f"goal_approval_failed: {exc.message}",
                        "goal_approval_failed")
        execution_turn = None
        execution_deadline = time.monotonic() + _GOAL_EXECUTION_WAIT_SECONDS
        while True:
            owner.check()
            try:
                status_view = client.agent_status(job["session_id"])
            except util.KanpilotError:
                status_view = None
            if isinstance(status_view, dict):
                inner = status_view.get("status") or status_view
                current = inner.get("currentTurnId")
                if current and str(current) != planning_turn:
                    candidate = str(current)
                    try:
                        record = client.turn_get(job["session_id"], candidate,
                                                 **({"require_identity": True} if authorization else {}))
                        if (_turn_identity_matches(record, job["session_id"],
                                                   candidate)
                                and _turn_status(record) in
                                ("running", "completed", "error", "aborted")):
                            if authorization:
                                collector.poll()
                                approved = [item for item in (client.plans_pending(job["session_id"]).get("history") or [])
                                            if isinstance(item, dict) and item.get("kind") == "goal"
                                            and item.get("status") == "approved"
                                            and item.get("sessionId") == job["session_id"]
                                            and item.get("turnId") == planning_turn
                                            and item.get("id") == proposal_id
                                            and item.get("toolCallId") == proposal["toolCallId"]
                                            and item.get("version") == version + 1]
                                if (len(approved) != 1 or collector.truncated or collector.read_error
                                        or collector.unrecognized or len(collector.user_messages) != 1
                                        or _normalize(collector.first_user_message) != _normalize(expected_prompt)):
                                    return ("deferred", "goal_execution_unconfirmed: 已批准，但执行归属或原始授权转录不完整", "")
                            execution_turn = candidate
                            break
                    except util.KanpilotError:
                        pass          # 候选尚未落库：继续等
            if time.monotonic() >= execution_deadline:
                break
            time.sleep(_GOAL_POLL_SECONDS)
        if not execution_turn:
            if authorization:
                return ("deferred", "goal_execution_unconfirmed: 已批准，但无法严格核验执行轮次身份", "")
            return ("failed",
                    "goal_execution_unstarted: 目标契约已批准但 "
                    f"{_GOAL_EXECUTION_WAIT_SECONDS:g}s 内未观察到同会话执行轮次"
                    "（宿主 dispatcher 未开启；未重发 prompt）",
                    "goal_execution_unstarted")
        job["goal_approval"] = {
            "proposal_id": proposal_id,
            "title": title,
            "status": "approved",
            "planning_turn_id": planning_turn,
            "execution_turn_id": execution_turn,
            "target_permission_mode": permission_mode,
            "approved_at": util.now_iso(),
            "approved_by": "kaneopilot",
            "approval_requested_at": approval_requested_at,
            "execution_started_at": record.get("startedAt")
                if util.parse_ts(record.get("startedAt")) is not None else None,
        }
        log.emit("system", util.now_iso(),
                 execution_turn_id=execution_turn,
                 approval_requested_at=approval_requested_at,
                 execution_started_at=job["goal_approval"]["execution_started_at"],
                 content=(f"goal_approved: {proposal_id} → 执行轮次 "
                          f"{execution_turn}（权限 {permission_mode}；本机代批，"
                          f"未重发 prompt）"))
        rebind_turn(execution_turn)
        return ("adopted", "", "")

    def unknown(detail, reason="unverifiable"):
        if resume:
            # 续接观察只负责观察：读不到持久轮次时不得改写轮次终态（更不得
            # 写成发送不明），留给监控端下一轮再续接。
            return defer(detail)
        if not proof_ok["ok"]:
            collector.poll()
            collector.preserve_unverified(run_dir)
        job["send_state"] = "unknown"
        return finalize("failed", 1, detail, True, "failed", reason)

    # ---- 5. Persistent failure is not proof of a successful startup.
    authorization = spec.get("continue_goal_authorization") if resume else None
    if authorization:
        try:
            owner.check()
            if prepare_goal_continuation(spec, job, client) != authorization:
                return defer("goal_continuation_changed: 目标授权身份已变化")
            if authorization["turn_id"] != turn_id:
                original_record = client.turn_get(job["session_id"], turn_id, require_identity=True)
                job["goal_continuation_previous_turn"] = original_record
                adopt(authorization["turn_id"], original_record)
        except (util.KanpilotError, OSError, UnicodeDecodeError, IndexError) as exc:
            return defer("goal_continuation_blocked: " + str(exc))
    turn_authorization = spec.get("continue_turn_authorization") if resume else None
    if turn_authorization:
        try:
            owner.check()
            origin = job
            already_adopted = job.get("turn_id") == turn_authorization.get("turn_id")
            if already_adopted:
                history = list(job.get("turn_adopted") or [])
                if not history or history[-1].get("recovery_authorization") != turn_authorization:
                    return defer("turn_recovery_changed: 缺少后续轮次已绑定的审计记录")
                origin = dict(job, turn_id=turn_authorization["original_turn_id"], turn_adopted=history[:-1])
            candidate = prepare_turn_recovery(spec, origin, client,
                turn_id=turn_authorization.get("turn_id") if turn_authorization.get("explicit") else None)
            if candidate is None or candidate != turn_authorization:
                return defer("turn_recovery_changed: 后续轮次授权身份已变化")
            if not already_adopted:
                previous = client.turn_get(job["session_id"], turn_id, require_identity=True)
                adopt(candidate["turn_id"], previous, candidate)
        except (util.KanpilotError, OSError, UnicodeDecodeError, IndexError) as exc:
            return defer("turn_recovery_blocked: " + str(exc))
    proof_deadline = min(deadline, time.monotonic() + _PROOF_WAIT_SECONDS)
    while True:
        try:
            record = observe_turn()
            break
        except util.KanpilotError as exc:
            # Accepted prompts can briefly precede their durable turn row.
            if exc.code != "not_found" or time.monotonic() >= proof_deadline:
                return unknown("startup_unproven: " + exc.message, "startup_unproven")
            time.sleep(min(0.5, max(0, proof_deadline - time.monotonic())))
    state = _turn_status(record)
    turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
    if state in ("error", "aborted"):
        recovery = None
        if resume and job.get("scope_id"):
            try:
                recovery = prepare_turn_recovery(spec, job, client)
            except (util.KanpilotError, OSError, UnicodeDecodeError, IndexError) as exc:
                # A failed continuation proof must never resurrect the old turn.
                job["resume_detail"] = "turn_recovery_blocked: " + str(exc)
            follow = recovery["turn_id"] if recovery else None
        else:
            follow = adopt_follow_up(record) if not resume else None
        if follow is None:
            collector.poll()
            collector.preserve_unverified(run_dir)
            code = turn.get("errorCode") or state
            detail = f"turn {state}: {code}"
            if resume and job.get("status") == "failed" and job.get("detail") == detail:
                # Idempotent terminal observation: preserve the existing evidence.
                _write_json(job_path, job)
                return 1
            return finalize("failed", 1, detail,
                            True, "failed", str(code))
        adopt(follow, record, recovery)  # 身份、换绑与审计授权在同一次 job 写入
        if resume:
            record = observe_turn()
            state = _turn_status(record)
    if resume:
        # 续接接管证明：持久 turn 可读 + 身份相符即视为接管成功（启动证明已
        # 由原进程完成，不重复 heartbeat、不重复写 session 事件）。
        proof_ok["ok"] = True
        if "resume_previous" not in job:
            # 首次续接：上一次收尾（观察者退出造成的假失败/崩溃现场）留作
            # 审计字段后，轮次回到观察态——已重新接管，run_status 不再是 failed。
            job["resume_previous"] = {
                "status": job.get("status"),
                "detail": job.get("detail"),
                "ended_at": job.get("ended_at"),
                "exit_code": job.get("exit_code"),
                "send_state": job.get("send_state"),
            }
        if state == "running":
            job["observation_resumed_at"] = util.now_iso()
        job["resume_detail"] = None
        # session 事件即「已确认发送」证据：把旧版假失败留下的 unknown 修回
        # sent（原值已在 resume_previous 保留），避免轮次继续显示发送不明。
        job["send_state"] = "sent"
        if state == "running":
            job.update({"status": "running", "detail": None,
                        "ended_at": None, "exit_code": None})
        elif state == "completed":
            # Persist the proved completed identity before potentially slow
            # transcript/report settlement, without publishing a running turn.
            job["observation_finalizing"] = {"turn_id": turn_id, "observed_status": "completed"}
        _write_json(job_path, job)
        # The original session event and this turn's takeover proof are both
        # verified; resume appends evidence without emitting a second session.
        collector.drain()
    else:
        try:
            _heartbeat(spec)
        except Exception as exc:
            detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
            return unknown(f"heartbeat_failed: {detail}", "heartbeat_failed")
        proof_ok["ok"] = True
        emit_session()

    # ---- 6. Observe until settlement; stop/abort are each attempted once.
    last_heartbeat = time.monotonic()
    # 派发请求在 dispatch-confirm 后即收尾（succeeded），此后核心对它的
    # heartbeat 一律回 stale_run/not_found。这是正常路径，不是轮次失败：观察
    # 只停止续租，绝不中断——轮次由本轮观察 + 监控口径收尾。
    heartbeating = bool(spec.get("request_id") and spec.get("db_path"))
    stop_requested = False
    stop_at = None
    abort_attempted = False
    abort_accepted = False
    stop_detail = ""
    stop_reason = "timeout"
    inputs=_ExecutionInputObserver(spec,job,log)
    while True:
        owner.check()
        now_m = time.monotonic()
        try:
            record = observe_turn()
            state = _turn_status(record)
            turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
        except util.KanpilotError as exc:
            return unknown("turn 终态无法核实，可能仍在运行：" + exc.message)
        try:
            status_view=observer.agent_status(job["session_id"])
            inner=status_view.get("status") or status_view
            current=inner.get("currentTurnId")
            if current and current != turn_id and state == "running":
                return unknown("turn_mismatch: 会话当前 turn 与本轮不符", "turn_mismatch")
            waiting=inputs.observe(observer,status_view,record) if spec.get("db_path") else False
            observer.allow_input_recovery=waiting
            if inputs.stop_info and not stop_requested:
                stamp=util.parse_ts(inputs.stop_info["stop_requested_at"])
                stop_requested=True
                stop_at=now_m-max(0,(util.now_dt()-stamp).total_seconds())
                stop_reason=inputs.stop_info.get("termination_reason") or "user_stop"
                stop_detail="核心已登记停止请求"
            waited=_input_wait_seconds(spec)
            job["input_wait_seconds"]=waited
            _write_json(job_path,job)
            remaining=_round_remaining_seconds(job,timeout,spec)
            # Preserve monotonic fallback for historical tests/jobs without UTC starts.
            elapsed=remaining<=0 if util.parse_ts(job.get("started_at")) else now_m>=deadline
            if not stop_requested and (inputs.expired() or elapsed and not waiting):
                stop_requested,stop_at=True,now_m
                stop_reason="execution_question_expired" if inputs.expired() else "timeout"
                observer.client._timeout=min(2.0,_STOP_VERIFY_SECONDS/5)
                try:
                    if stop_reason=="execution_question_expired":
                        cli.run("stop-execution",{"task_id":spec["task_id"],"run_id":spec["run_id"],
                            "operation_id":"input_wait_stop", "reason":stop_reason},Path(spec["db_path"]))
                    else: observer.client.agent_stop(job["session_id"])
                    stop_detail="stop 请求已接受"
                except util.KanpilotError as exc: stop_detail="stop 响应未证实："+exc.message
        except util.KanpilotError as exc:
            return unknown("执行问题观察无法核实："+exc.message)
        if state in ("completed", "error", "aborted"):
            if inputs.terminal_failure and not stop_requested:
                collector.poll()
                return finalize("failed",1,"execution_failed: 执行问题缺少正常回答终态证据",
                    True,"failed","execution_failed")
            if state in ("error", "aborted"):
                recovery = None
                if resume and job.get("scope_id"):
                    try:
                        recovery = prepare_turn_recovery(spec, job, client)
                    except (util.KanpilotError, OSError, UnicodeDecodeError, IndexError) as exc:
                        job["resume_detail"] = "turn_recovery_blocked: " + str(exc)
                    follow = recovery["turn_id"] if recovery else None
                else:
                    follow = adopt_follow_up(record) if not resume else None
                if follow is not None:
                    adopt(follow, record, recovery)
                    continue
            if state == "completed" and not stop_requested:
                # §5.23.9：goal 模式的规划轮次以「提交目标契约」结束——审批门
                # 通过后继续观察同会话执行轮次；否则按普通 completed 收尾。
                outcome, detail, reason = goal_contract_gate()
                if outcome == "adopted":
                    continue
                if outcome == "deferred":
                    return defer(detail)
                if outcome == "failed":
                    collector.poll()
                    return finalize("failed", 1, detail, True, "failed", reason)
            break
        if stop_requested:
            if not abort_attempted and now_m >= stop_at + _STOP_GRACE_SECONDS:
                abort_attempted = True
                try:
                    owner.check()
                    if stop_reason in ("execution_question_expired","user_stop"):
                        cli.run("stop-execution",{"task_id":spec["task_id"],"run_id":spec["run_id"],
                            "operation_id":"input_wait_abort","abort":True},Path(spec["db_path"]))
                    else: observer.client.agent_abort(job["session_id"])
                    abort_accepted = True
                except util.KanpilotError as exc:
                    stop_detail += "；abort 响应未证实：" + exc.message
            if now_m >= stop_at + _STOP_VERIFY_SECONDS:
                return unknown("停止未证实，可能仍在运行；" + stop_detail, "timeout_unverified")
        try:
            status_view = observer.agent_status(job["session_id"])
            inner = status_view.get("status") or status_view
            current = inner.get("currentTurnId")
            if current and current != turn_id:
                return unknown("turn_mismatch: 会话当前 turn 与本轮不符", "turn_mismatch")
        except util.KanpilotError as exc:
            return unknown("会话运行状态无法核实：" + exc.message)
        collector.poll()
        if collector.read_error is not None:
            return unknown("transcript_read_failed: " + collector.read_error)
        if heartbeating and now_m - last_heartbeat >= _HEARTBEAT_SECONDS:
            try:
                _heartbeat(spec)
            except Exception as exc:  # noqa: BLE001 - 心跳失败不中断观察
                detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
                code = exc.code if isinstance(exc, util.KanpilotError) else "internal"
                if code in ("stale_run", "not_found"):
                    heartbeating = False
                    job["heartbeat_stopped"] = f"{code}: {detail}"
                else:
                    job["heartbeat_retry"] = f"{code}: {detail}"
                _write_json(job_path, job)
            last_heartbeat = now_m
        time.sleep(_POLL_SECONDS)

    # ---- 7. 末轮采集（尽力补全 transcript）+ 终态判定。
    collector.poll()
    if stop_requested:
        reason = stop_reason
        detail = stop_reason if stop_reason in ("execution_question_expired","user_stop") else f"timeout after {timeout:g}s"
        if abort_accepted:
            detail += "；abort 请求已接受"
        detail += "；" + stop_detail
        if state in ("completed", "error", "aborted"):
            # stop 期间 turn 自行落终态：以持久终态为准（仍按超时标记）。
            detail += f"；turn 终态 {state}"
        return finalize("failed", 1, detail, True, "failed", reason)
    if collector.truncated or collector.read_error is not None:
        # 分页不完整/截断：保留全部证据，轮次不得判成功，不写成功 terminal。
        parts = []
        if collector.truncated:
            parts.append("transcript_incomplete: 分页链断裂/超限")
        if collector.read_error is not None:
            parts.append(f"transcript_read_failed: {collector.read_error}")
        return finalize("failed", 1, "；".join(parts), False)
    if state == "completed" and (collector.unrecognized or not collector.seen):
        return finalize("failed", 1,
                        "transcript_incomplete: 空转录或存在无法解析的消息结构",
                        False)
    if state == "completed":
        # T5-D（§5.23.10）：执行终态落地后给 Host 最多 60s 完成 outbox/报告
        # 发布，期间**只读**查询、绝不重发 prompt；随后按报告分态写终态
        # detail（具体缺口写进 reason，供监控端 report needs_user 引用）。
        report_note, report_reason = _goal_report_settlement(client, job)
        return finalize("finished", 0, report_note, True, "completed",
                        report_reason or "completed")
    if state in ("error", "aborted"):
        code = turn.get("errorCode") or turn.get("error_code") or state
        return finalize("failed", 1, f"turn {state}: {code}",
                        True, "failed", str(code))
    # 服务重启/无法核实：持久 turn 非终态且证据已尽 → 明确异常。
    return finalize("failed", 1,
                    "turn 终态无法核实（服务重启或控制面不可达）",
                    True, "failed", "unverifiable")


def _goal_report_settlement(client, job: dict) -> tuple:
    """终态落地后的只读报告宽限（§5.23.10 T5-D）。

    - 轮次没有已批准的 Goal 契约（``job.goal_approval`` 缺失）→ 不是需要
      Canvas 报告的 Goal 执行场景：不追加缺口。
    - 有已批准契约：最多 ``_GOAL_REPORT_GRACE_SECONDS`` 秒内**只读**轮询
      ``pi_goal_report_get``；``ready + structured`` → 记录报告身份与
      ``report_sha256`` 供监控端/核心硬闸复核；其余分态 → 在 job detail
      写明**具体缺口**，不自动重新派发、不重发 prompt。

    返回 ``(detail, reason)``：``reason`` 可为 ``None``（结构化报告就绪）、
    ``execution_failed``（缺报告/兜底报告——可归因的执行缺口）或
    ``report_unreadable``（报告不可读/损坏/工具缺失——区别于「已验证失败」）。
    """
    # 新 Plus 轮次保留真实执行/工具证据，由确定性 monitor 核对 verify 声明。
    # 缺省保持旧手动 Goal 收口；策略来自派发时固化的轮次快照。
    if job.get("completion_policy") == "command_verification":
        return None, None
    approval = job.get("goal_approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        return None, None
    session_id = job.get("session_id")
    if not session_id:
        return None, None
    execution_id = approval.get("execution_id")
    proposal_id = approval.get("proposal_id")
    approved_turns = {str(approval.get("execution_turn_id") or "")}
    for item in job.get("turn_adopted") or []:
        if isinstance(item, dict) and item.get("to"):
            approved_turns.add(str(item["to"]))
    deadline = time.monotonic() + _GOAL_REPORT_GRACE_SECONDS
    last_state, last_detail = "not_found", None
    while True:
        try:
            payload = _fetch_report_payload(
                client, session_id, execution_id, proposal_id, approved_turns,
            )
            resolved = payload
        except util.KanpilotError as exc:
            resolved = {
                "state": pi_desktop_backend.classify_report_error(
                    exc.code, exc.message
                ),
                "detail": exc.message,
            }
        except Exception as exc:  # noqa: BLE001 - 只读探测不得中断收尾
            resolved = {"state": "unreadable", "detail": str(exc)}
        last_state = resolved.get("state")
        last_detail = resolved.get("detail")
        if last_state == "ready":
            job["goal_report"] = {
                "report_id": resolved.get("report_id"),
                "report_sha256": resolved.get("report_sha256"),
                "execution_id": resolved.get("execution_id"),
                "proposal_id": proposal_id,
                "turn_id": resolved.get("turn_id"),
                "integrity_kind": resolved.get("integrity_kind"),
                "execution_status": resolved.get("execution_status"),
                "verdict": resolved.get("verdict"),
            }
            if resolved.get("integrity_kind") == "structured":
                return None, None
            # ready + fallback 等同「没有完成报告」（§5.23.10 T5-D）。
            return (
                "goal_report_fallback: Plus 生成的是兜底报告"
                "（integrity=fallback），没有完成报告",
                "execution_failed",
            )
        if last_state in ("corrupt", "truncated"):
            # 报告不可读/损坏：一次有界重读后交人工，原因区别于「已验证失败」。
            return (
                f"goal_report_{last_state}: 报告不可读/损坏"
                f"（{last_detail or '无正文'}）；交人工处理，"
                "不作为已验证失败",
                "report_unreadable",
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(_GOAL_REPORT_POLL_SECONDS)
    gap = {
        "not_found": "没有结构化完成报告（Host 未在宽限内发布 Goal 报告）",
        "pending": "报告仍在 pending（Host 未在宽限内完成 outbox 发布）",
        "draft": "报告仍是 draft（结构化草稿未提交）",
        "failed": "报告自身为 failed",
        "unreadable": "报告读取失败（临时读取失败，不等于没有报告）",
        "tool_missing": "Plus 缺少只读报告工具（版本过旧）",
    }.get(last_state, f"报告状态未知（{last_state}）")
    reason = (
        "report_unreadable"
        if last_state in ("unreadable", "tool_missing")
        else "execution_failed"
    )
    return (
        f"goal_report_missing: {gap}"
        f"（state={last_state}；{last_detail or ''}）；"
        "未自动重新派发、未重发 prompt",
        reason,
    )


def _fetch_report_payload(client, session_id, execution_id, proposal_id,
                          approved_turns) -> dict:
    """只读取回本轮报告并归一化 + 身份核对（worker 自持身份，不查 DB）。

    与 ``cmd_read.read_goal_report`` 同口径：优先按已记录的 executionId
    取；缺记录时经 ``pi_goal_report_list`` 按 proposalId + 已批准 turn 收敛。
    跨轮/身份不符 → ``KanpilotError``（绝不返回伪造正文）。
    """
    candidates = []
    if execution_id:
        candidates = [{"executionId": execution_id, "reportId": None}]
    else:
        listing = client.goal_report_list(session_id)
        rows = listing.get("reports") if isinstance(listing, dict) else None
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            proposal = item.get("proposalId") or item.get("proposal_id")
            turn = item.get("turnId") or item.get("turn_id")
            if proposal is not None and str(proposal) != str(proposal_id):
                continue
            if turn is not None and str(turn) not in approved_turns:
                continue
            candidates.append({
                "executionId": item.get("executionId")
                or item.get("execution_id"),
                "reportId": item.get("reportId") or item.get("report_id"),
            })
    if not candidates:
        return {"state": "not_found", "detail": "本会话没有本轮 Goal 报告"}
    if len(candidates) > 1:
        raise util.KanpilotError(
            "conflict", "同轮次存在多份 Goal 报告，身份歧义不猜测"
        )
    target = candidates[0]
    payload = client.goal_report_get(
        session_id, execution_id=target.get("executionId"),
        report_id=target.get("reportId"),
    )
    resolved = pi_desktop_backend.normalize_goal_report(
        payload, report_id=target.get("reportId")
    )
    if resolved.get("state") != "ready":
        return resolved
    report = resolved["report"]
    fields = pi_desktop_backend.report_identity(report)
    if str(fields.get("session_id") or "") != str(session_id):
        raise util.KanpilotError(
            "conflict", "报告 sessionId 与本轮会话身份不符"
        )
    if str(fields.get("proposal_id") or "") != str(proposal_id):
        raise util.KanpilotError(
            "conflict", "报告 proposalId 与本轮已批准契约不符"
        )
    if execution_id and str(fields.get("execution_id") or "")             != str(execution_id):
        raise util.KanpilotError(
            "conflict", "报告 executionId 与本轮记录不符"
        )
    turn_id = fields.get("turn_id")
    if turn_id is not None and str(turn_id) not in approved_turns:
        raise util.KanpilotError(
            "conflict", "报告 turnId 不属于本轮已批准的执行轮次"
        )
    resolved["turn_id"] = turn_id
    resolved["execution_id"] = fields["execution_id"]
    resolved["integrity_kind"] = pi_desktop_backend.report_integrity_kind(
        report
    )
    resolved["execution_status"] = (
        pi_desktop_backend.report_execution_status(report)
    )
    resolved["verdict"] = pi_desktop_backend.report_verdict(report)
    return resolved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kanpilot.pi_desktop_worker")
    parser.add_argument("--spec", required=True)
    parser.add_argument(
        "--resume", action="store_true",
        help="§5.23.7 观察者续接：只读接管既有 session/turn，绝不重发 prompt",
    )
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    spec["_spec_path"] = str(Path(args.spec).resolve())
    if spec.get("v") != 1:
        print(f"unsupported spec version: {spec.get('v')}", file=sys.stderr)
        return 2
    for key in ("task_id", "run_id", "runs_dir"):
        if not spec.get(key):
            print(f"spec missing {key}", file=sys.stderr)
            return 2
    fake = os.environ.get("KANPILOT_PI_DESKTOP_FAKE")
    if fake and args.resume:
        # fake 接缝没有真实 session/turn 现场，续接语义只属于真实分支。
        print("--resume 与 KANPILOT_PI_DESKTOP_FAKE 互斥", file=sys.stderr)
        return 2
    if fake:
        try:
            return run_fake(spec, fake)
        except KeyboardInterrupt:
            return 130
    try:
        return run_real(spec, resume=args.resume)
    except KeyboardInterrupt:
        return 130
    except util.KanpilotError as exc:
        # 兜底：任何未归类错误也要落 job，绝不留 running 假象。
        run_dir = Path(spec["runs_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        job_path = run_dir / dsh_events.JOB_NAME
        try:
            job = json.loads(job_path.read_text(encoding="utf-8")) \
                if job_path.is_file() else _base_job(spec)
        except (OSError, ValueError):
            job = _base_job(spec)
        if args.resume:
            # 续接失败不得把既有轮次改写成 failed（观察环境问题 ≠ 轮次失败）。
            job.update({
                "resume_detail": f"{exc.code}: {exc.message}",
                "resume_deferred_at": util.now_iso(),
                "resume_attempts": int(job.get("resume_attempts") or 0) + 1,
            })
            _write_json(job_path, job)
            return 3
        job.update({
            "status": "failed", "ended_at": util.now_iso(),
            "exit_code": 1, "detail": f"{exc.code}: {exc.message}",
        })
        _write_json(job_path, job)
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底落现场
        run_dir = Path(spec["runs_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        job_path = run_dir / dsh_events.JOB_NAME
        try:
            job = json.loads(job_path.read_text(encoding="utf-8")) \
                if job_path.is_file() else _base_job(spec)
        except (OSError, ValueError):
            job = _base_job(spec)
        if args.resume:
            job.update({
                "resume_detail": f"internal: {exc}",
                "resume_deferred_at": util.now_iso(),
                "resume_attempts": int(job.get("resume_attempts") or 0) + 1,
            })
            _write_json(job_path, job)
            return 3
        job.update({
            "status": "failed", "ended_at": util.now_iso(),
            "exit_code": 1, "detail": f"internal: {exc}",
        })
        _write_json(job_path, job)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
