"""list-sessions / get-session / session-messages / trash-session /
restore-session (protocol.md §5.15.5 v1.4 + §5.16 v1.5).

OWNERSHIP: session subagent. 读取命令经 headless_adapter 读取本机执行记录
（``<runs_root>/<task_id>/<run_id>/`` 下 job.json 身份核验 + events.jsonl；
backend=pi 轮次按 §5.18.2 路由读事件日志，旧后端标记弃用），以
task_id + run_id 绑定确切会话（session_marker = "<task_id>/<run_id>"），
绝不按标题猜测。runs_root 解析优先级：
list-sessions payload ``session_runs_root`` > DB setting ``session_runs_root``
（空字符串视为未设置）> constants 默认；payload 覆盖不持久化。

状态语义：associated = 核验通过且成功解析；pending = 目录 + job.json 有效但
events.jsonl 尚未导出（或无 run_id）；unavailable = 目录/
job.json 缺失、身份不符、路径越界、root 不存在。connection 恒 degraded（无推送
订阅，UI 按 session_poll_seconds 轮询）；机制性失败 → unavailable；半写入读取
失败 → 仍 degraded、detail 说明并保留上一份有效内容（适配层进程内缓存 + 跨进程
持久快照），无缓存按 pending。

SessionInfo.last_checked_at（§5.15.5「最近状态检查」）= session_reads 位点行的
刷新时间（无位点 → null）；session-messages 在位点刷新之后构建 SessionInfo，
使该字段反映本次状态检查，与会话更新时间（updated_at）分开展示。

游标：首次无 cursor 返回完整历史；增量传 next_cursor；断线重连无 cursor 时用
session_reads 已存位点补齐，不跳历史、不重复；尾部进行中步骤的内容更新（revision
变化）在增量读取中透出。消息按 seq 排序、按 id+revision 去重/更新。

v1.5（§5.16）执行轮次索引与会话回收站：
- list-sessions 基于 execution_rounds 索引列举：先对 runs 根目录已发现的执行
  目录做 job.json 身份核验回填（核验通过才 upsert_discovered；backend 按
  job.json backend 原值（pi/dsh/headless）；**回填绝不触碰 save_state/
  saved_at**——round_store 保证重复导入不复活已删除会话），再按 save_state
  （active|trashed|all，缺省 active，非法 → bad_request）过滤索引行输出。
- 兼容：历史任务无索引行时按现有逻辑（tasks.run_id/session_marker + headless
  探测）发现并回填后输出；缺省列表只输出有关联任务的会话（active 任务优先）。
- SessionInfo 增加 backend / run_status（round_store.derive_run_status 派生，
  conn/now/timeout 取 round_store.round_timeout_seconds，超时只派生不回写）/
  save_state / saved_at / allowed_actions / blocked_reason（§5.16.3）。
- list-sessions 轻量核验路径（LIST_SESSIONS_LIGHT_THRESHOLD）语义保持：只 stat
  不读 conversation，associated 态由 get-session / session-messages 补齐。
- trash-session / restore-session 只改 execution_rounds.save_state：不删文件、
  不停进程、不改任务生命周期、不动任务 save_state；禁止条件复用
  round_store.session_trash_block_reason（conflict + 中文原因）；已处于目标状态
  → deduplicated 不重复写事件（event session_trashed / session_restored）。

list-sessions 缺省模式：root 下任务目录数超过 constants.LIST_SESSIONS_LIGHT_
THRESHOLD 时轻量核验（只 stat 不读文件内容），associated 态由 get-session /
session-messages 补齐。

只读边界：不改任务状态、不写远端、不输出凭据；本地写仅为 §5.16.1 列明的读取层
回填（execution_rounds）与 session_reads 读取位点（KaneoPilot 自己的表）。
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import stat
from pathlib import Path
from typing import Optional

from . import (
    constants, db, dsh_events, headless_adapter as headless, round_store,
    session_store, util,
)

# 跨进程最近有效快照（存 settings；仅 KaneoPilot 自己的库）。
_SNAPSHOT_PREFIX = "session_snapshot:"
_SNAPSHOT_META_PREFIX = "session_snapshot_meta:"
_SNAPSHOT_MAX_BYTES = 2_000_000
_STATUS_SNAPSHOT_PREFIX = "session_status_snapshot:"
_UNCHECKED = object()


def _save_snapshot(conn, marker: str, messages: list, record) -> bool:
    payload = json.dumps({"messages": messages, "record": record},
                         ensure_ascii=False)
    if len(payload.encode("utf-8")) > _SNAPSHOT_MAX_BYTES:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (_SNAPSHOT_PREFIX + marker, payload),
    )
    return True


def _load_snapshot_meta(conn, marker: str) -> Optional[dict]:
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_SNAPSHOT_META_PREFIX + marker,)).fetchone()
    if not row:
        return None
    try:
        meta = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if (isinstance(meta, dict) and meta.get("cache_schema") == 1
            and isinstance(meta.get("record"), dict)
            and isinstance(meta.get("max_seq"), int)
            and isinstance(meta.get("source_version"), str)):
        return meta
    return None


def _source_version(signature: dict) -> str:
    canonical = json.dumps(signature, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_snapshot(conn, marker: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (_SNAPSHOT_PREFIX + marker,),
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data
    return None

# SessionInfo 固定字段（protocol.md §5.15.5；last_checked_at 见 §5.15.5
# 「卡片分别显示会话更新时间与最近状态检查」）。
_SESSION_FIELDS = (
    "task_id", "run_id", "session_marker", "session_id", "session_title",
    "state", "started_at", "last_message_at", "message_count", "updated_at",
    "last_checked_at",
)

_EMPTY_VIEW = {
    "session_id": None, "session_title": None, "started_at": None,
    "last_message_at": None, "message_count": 0, "updated_at": None,
}


# --------------------------------------------------------------- helpers

def _connection_view(probe: dict, poll_seconds: int) -> dict:
    return {
        "state": probe.get("state", headless.CONN_UNAVAILABLE),
        "detail": probe.get("detail"),
        "subscription": bool(probe.get("subscription")),
        "poll_seconds": poll_seconds,
    }


def _unavailable_connection(detail: str, poll_seconds: int) -> dict:
    return {
        "state": headless.CONN_UNAVAILABLE,
        "detail": detail,
        "subscription": False,
        "poll_seconds": poll_seconds,
    }


def _session_info(
    task_id: str,
    run_id: Optional[str],
    state: str,
    record: Optional[dict],
) -> dict:
    marker = (
        session_store.session_key_for(task_id, run_id)
        if run_id is not None else None
    )
    view = dict(_EMPTY_VIEW)
    if state == headless.SESSION_ASSOCIATED and record:
        view.update(headless.session_record_view(record))
    return {
        "task_id": task_id,
        "run_id": run_id,
        "session_marker": marker,
        "session_id": view["session_id"],
        "session_title": view["session_title"],
        "state": state,
        "started_at": view["started_at"],
        "last_message_at": view["last_message_at"],
        "message_count": view["message_count"],
        "updated_at": view["updated_at"],
    }


def _resolve_run_id(task: sqlite3.Row, payload_run_id: Optional[str]) -> Optional[str]:
    """run_id 缺省取 task 当前 dispatch run；都无则 None（→ pending）。"""
    if payload_run_id:
        return payload_run_id
    return task["run_id"]


def _associate(
    conn: sqlite3.Connection,
    source: Optional[headless.HeadlessSource],
    task_id: str,
    run_id: Optional[str],
    probe: Optional[dict] = None,
    light: bool = False,
) -> dict:
    """把 task_id + run_id 关联到确切 headless 运行记录。

    返回 {"connection", "state", "detail", "record", "messages"}。probe 可外部
    预计算（list-sessions 复用一次探测）；light=True 时只 stat 核验不读文件。
    root 不存在/越界/身份不符 → state=unavailable；读取失败（半写入）经适配层
    缓存兜底为 associated|pending 并带 detail 原因。
    """
    poll = headless.poll_seconds(conn)
    if source is None:
        return {
            "connection": _unavailable_connection(
                "运行记录根目录不可用", poll
            ),
            "state": headless.SESSION_UNAVAILABLE, "detail": None,
            "record": None, "messages": None,
        }
    if probe is None:
        probe = source.probe()
    connection = _connection_view(probe, poll)
    # 机制性失败（root 不存在等）→ unavailable。
    if probe.get("state") == headless.CONN_UNAVAILABLE:
        return {
            "connection": connection, "state": headless.SESSION_UNAVAILABLE,
            "detail": probe.get("detail"), "record": None, "messages": None,
        }
    # 无 run_id → 无法关联，但连接健康；会话待关联。
    if run_id is None:
        return {
            "connection": connection, "state": headless.SESSION_PENDING,
            "detail": None, "record": None, "messages": None,
        }
    if light:
        result = source.light_inspect(task_id, run_id)
    else:
        result = source.inspect(task_id, run_id)
    status = result.get("status")
    detail = result.get("detail")
    if detail and status != headless.SESSION_PENDING:
        # 读取失败保留内容时 connection 仍 degraded，detail 透出原因。
        # 正常 pending 的 detail 是等待说明；STATUS_READ_FAILED 的无快照
        # 回退在下方才转为 pending，仍须保留其读取错误。
        connection = dict(connection)
        connection["detail"] = detail
    if status == headless.SESSION_ASSOCIATED:
        return {
            "connection": connection,
            "state": headless.SESSION_ASSOCIATED, "detail": detail,
            "record": result.get("record"),
            "messages": result.get("messages"),
            "_events": result.get("_events"),
            "_source_signature": result.get("_source_signature"),
        }
    if status == headless.STATUS_READ_FAILED:
        # 半写入：优先保留上一份有效内容（进程内缓存或跨进程持久快照）。
        snapshot = _load_snapshot(
            conn, session_store.session_key_for(task_id, run_id)
        )
        if snapshot:
            return {
                "connection": connection,
                "state": headless.SESSION_ASSOCIATED, "detail": detail,
                "record": snapshot.get("record"),
                "messages": snapshot["messages"],
            }
        # 无上一份有效内容：按 pending 处理，绝不报「空会话」掩盖。
        return {
            "connection": connection, "state": headless.SESSION_PENDING,
            "detail": detail, "record": None, "messages": None,
        }
    if status == headless.SESSION_PENDING:
        return {
            "connection": connection, "state": headless.SESSION_PENDING,
            "detail": detail, "record": None, "messages": None,
        }
    return {
        "connection": connection, "state": headless.SESSION_UNAVAILABLE,
        "detail": detail, "record": None, "messages": None,
    }


# ------------------------------------------- v1.5 执行轮次索引（§5.16）

def _identity_job(
    source: headless.HeadlessSource, task_id: str, run_id: Optional[str]
) -> tuple:
    """按 §5.15.5 路径安全 + job.json 身份核验读取（语义与 headless inspect
    一致：非法标识/越界/缺文件/解析失败/身份不符/backend 缺失 → 不采信）。
    返回 ``(job, run_dir)``；不可用 → ``(None, None)``。"""
    if not run_id:
        return None, None
    try:
        directory = source.run_dir(task_id, run_id)
    except headless.HeadlessError:
        return None, None
    if not directory.is_dir():
        return None, None
    try:
        job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(job, dict):
        return None, None
    if job.get("task_id") != task_id or job.get("run_id") != run_id:
        return None, None
    if not job.get("backend"):
        return None, None
    return job, directory


def _observed_values(job: dict) -> dict:
    """job.json 的回填观察值：backend 原值透出（pi 或历史后端原样；
    未知值回落 "pi"）。run_status 仅采信 job.status ∈ ROUND_RUN_STATUSES
    （否则保持已存值）。"""
    status = job.get("status")
    backend = job.get("backend")
    if backend not in constants.ROUND_BACKENDS + constants.LEGACY_ROUND_BACKENDS:
        backend = "pi"
    return {
        "backend": backend,
        "session_id": job.get("session_id"),
        "session_title": job.get("session_title"),
        "run_status": status if status in constants.ROUND_RUN_STATUSES else None,
    }


def _refresh_needed(row: Optional[sqlite3.Row], job: dict) -> bool:
    """已存在行是否确有发现类字段变化（无变化不写——重复回填零扰动）。"""
    if row is None:
        return True
    observed = _observed_values(job)
    if row["backend"] != observed["backend"]:
        return True
    if observed["run_status"] is not None and \
            row["run_status"] != observed["run_status"]:
        return True
    for name in ("session_id", "session_title"):
        value = observed[name]
        if name == "session_id" and not value:
            continue
        if value is not None and row[name] != value:
            return True
    return False


def _backfill_from_job(
    conn: sqlite3.Connection, task_id: str, run_id: str, now: str, job: dict
) -> sqlite3.Row:
    """身份核验通过后的读取层回填（§5.16.1）：经 round_store.upsert_discovered
    补入缺失行/刷新发现类字段——**绝不触碰 save_state/saved_at**（回填不复活）。"""
    row = round_store.get_round(conn, task_id, run_id)
    if not _refresh_needed(row, job):
        return row
    observed = _observed_values(job)
    return round_store.upsert_discovered(
        conn, task_id, run_id, now, backend=observed["backend"],
        session_id=observed["session_id"],
        session_title=observed["session_title"],
        run_status=observed["run_status"],
    )


def _backfill_one(conn, source, task_id: str, run_id: Optional[str], now: str) -> tuple:
    """单轮次读取层回填（写入点：§5.16.1 list/get/trash/restore）。
    返回 ``(row|None, job|None)``；job.json 身份核验不通过时只读既有索引行。"""
    if not run_id:
        return None, None
    job, _directory = _identity_job(source, task_id, run_id)
    if job is None:
        return round_store.get_round(conn, task_id, run_id), None
    return _backfill_from_job(conn, task_id, run_id, now, job), job


def _discover_rounds(
    conn, source, probe: dict, now: str, task_id: Optional[str] = None
) -> dict:
    """runs 根目录发现回填（§5.16.1）：对已发现的执行目录做 job.json 身份核验，
    核验通过才 upsert_discovered（回填不复活由 round_store 保证）。
    返回 ``{(task_id, run_id): job}``——本轮核验通过的 job 缓存（run_status 派生用）。"""
    jobs: dict = {}
    if probe.get("state") == headless.CONN_UNAVAILABLE:
        return jobs
    root = source.runs_root
    try:
        if task_id is not None:
            only = root / task_id
            candidates = [only] if only.is_dir() else []
        else:
            candidates = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return jobs
    if not candidates:
        return jobs
    with db.write_txn(conn):
        for task_dir in candidates:
            try:
                run_dirs = [entry for entry in task_dir.iterdir()
                            if entry.is_dir()]
            except OSError:
                continue
            for run_dir in run_dirs:
                _row, job = _backfill_one(
                    conn, source, task_dir.name, run_dir.name, now
                )
                if job is not None:
                    jobs[(task_dir.name, run_dir.name)] = job
    return jobs


def _force_purge_reason(conn, source, row, file_reason=_UNCHECKED) -> Optional[str]:
    """严格确认无实际会话；不可读、损坏或半写入均不能当作不存在。"""
    task_id, run_id = row["task_id"], row["run_id"]
    if row["session_id"]:
        return "该轮次已有会话 ID，不能按无记录残留强制移除"
    reason = round_store.session_force_purge_block_reason(conn, task_id, run_id)
    if reason:
        return reason
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is not None and task["session_id"] and (
        task["run_id"] == run_id or task["session_marker"] == f"{task_id}/{run_id}"
    ):
        return "任务已关联该轮次会话，不能强制移除"
    if file_reason is not _UNCHECKED:
        return file_reason
    return _force_purge_file_reason(conn, source, row)


def _force_purge_file_reason(conn, source, row) -> Optional[str]:
    """File/snapshot proof; activity and task identity are checked on every read."""
    task_id, run_id = row["task_id"], row["run_id"]
    marker = session_store.session_key_for(task_id, run_id)
    snapshot_messages_verified = False
    for prefix in (_SNAPSHOT_PREFIX, _SNAPSHOT_META_PREFIX):
        cached = conn.execute("SELECT value FROM settings WHERE key=?",
                              (prefix + marker,)).fetchone()
        if cached is None:
            continue
        # 通常读取可忽略坏缓存；永久移除需要明确区分不存在与无法核验。
        try:
            snapshot = json.loads(cached[0])
        except (TypeError, ValueError):
            return "会话快照解析失败，无法确认会话记录已缺失"
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("record"), dict):
            return "会话快照格式无效，无法确认会话记录已缺失"
        if snapshot["record"].get("session_id") is not None and not isinstance(
            snapshot["record"]["session_id"], str
        ):
            return "会话快照身份无效，无法确认会话记录已缺失"
        if prefix == _SNAPSHOT_PREFIX:
            messages = snapshot.get("messages")
            if not isinstance(messages, list) or any(
                not isinstance(message, dict) or message.get("type") not in (
                    "system", "user", "assistant", "tool_call", "tool_result", "command_output",
                ) for message in messages
            ):
                return "会话快照消息无效，无法确认会话记录已缺失"
            snapshot_messages_verified = True
        else:
            if (snapshot.get("cache_schema") != 1
                    or not isinstance(snapshot.get("max_seq"), int)
                    or isinstance(snapshot["max_seq"], bool)
                    or not isinstance(snapshot.get("source_version"), str)):
                return "会话快照元数据无效，无法确认会话记录已缺失"
            if snapshot.get("available") is False or (
                snapshot["max_seq"] > 0 and not snapshot_messages_verified
            ):
                return "会话快照正文缺失，无法确认没有实际会话记录"
            messages = []
        if snapshot["record"].get("session_id") or any(
            message["type"] != "system" for message in messages
        ):
            return "该轮次保留有实际会话快照，不能强制移除"
    try:
        if source is None or source.probe().get("state") == headless.CONN_UNAVAILABLE:
            return "运行记录根目录不可用，无法确认会话记录已缺失"
        # is_dir/probe 不足以确认 root 可读；显式列目录防止权限故障误判。
        list(source.runs_root.iterdir())
        directory = source.run_dir(task_id, run_id)
        try:
            mode = directory.stat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(mode):
            return "运行记录路径不是目录，无法确认会话记录已缺失"
        list(directory.iterdir())
        for name in ("job.json", "events.jsonl", "conversation.json", "last-message.txt"):
            if (directory / name).is_symlink():
                return "运行记录文件为符号链接，无法安全确认会话记录已缺失"
        try:
            job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
            job_exists = True
        except FileNotFoundError:
            job, job_exists = None, False
        if job_exists:
            if (not isinstance(job, dict) or job.get("task_id") != task_id
                    or job.get("run_id") != run_id
                    or job.get("backend") not in constants.ROUND_BACKENDS):
                return "运行记录身份或后端无法核验，不能强制移除"
            if job.get("session_id"):
                return "运行记录已有会话 ID，不能强制移除"
        # 旧格式或独立输出仍是实际记录，不能仅因 events 缺失而移除。
        for name in ("conversation.json", "last-message.txt"):
            try:
                if (directory / name).read_text(encoding="utf-8").strip():
                    return "该轮次仍有会话内容文件，不能强制移除"
            except FileNotFoundError:
                pass
        try:
            content = (directory / "events.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        for line in content.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if (not isinstance(event, dict) or event.get("v") != 1
                    or not isinstance(event.get("seq"), int)
                    or isinstance(event.get("seq"), bool) or event["seq"] < 1):
                return "事件日志格式无法核验，不能强制移除"
            if event.get("session_id") or event.get("type") not in (
                "system", "terminal", "turn_end",
            ):
                return "该轮次存在会话事件，不能按无记录残留强制移除"
        return None
    except (OSError, ValueError, headless.HeadlessError):
        return "运行记录不可读或解析失败，无法确认会话记录已缺失"


def _session_trash_reason(conn, source, row, now) -> Optional[str]:
    """结束的旧规划需独立证明进程退出；不采信 stale running 或超时派生。"""
    task_id, run_id = row["task_id"], row["run_id"]
    reason = round_store.session_trash_block_reason(conn, task_id, run_id, now)
    plan_round = conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND run_id=? AND kind='plan' "
        "UNION ALL SELECT 1 FROM attempts WHERE task_id=? AND run_id=? AND kind='plan' "
        "LIMIT 1", (task_id, run_id, task_id, run_id),
    ).fetchone()
    if plan_round is None:
        return reason
    if round_store._plan_round_has_activity(conn, task_id, run_id):
        return "该规划轮次仍有排队或领取中的请求或活动轮次，暂不能移除会话"
    if reason is None:
        # 已完成的历史轮次保留原规则；进程证明只解除 stale-running 保护。
        return None
    if source is None:
        return "运行记录根目录不可用，无法确认该规划轮次进程已退出"
    exited_request_id = None
    job, directory = _identity_job(source, task_id, run_id)
    if (job is not None and job.get("backend") in constants.ROUND_BACKENDS
            and job.get("backend") == row["backend"]
            and isinstance(job.get("request_id"), str)
            and round_store._ended_plan_request(conn, task_id, run_id, job["request_id"])
            and (not row["session_id"] or row["session_id"] == job.get("session_id"))
            and (row["pid"] is None or row["pid"] == job.get("pid"))
            and not (directory / "job.json").is_symlink()):
        # 复用严格退出证明：只有 ESRCH 可确认退出；活跃、权限错误、未知均拒绝。
        from .session_control import _process_exited
        pids = [job.get("pid")]
        if job.get("observe_pid") is not None:
            pids.append(job["observe_pid"])
        try:
            processes_exited = all(_process_exited(pid) for pid in pids)
        except OverflowError:
            processes_exited = False
        if processes_exited:
            current_job, _directory = _identity_job(source, task_id, run_id)
            if current_job == job:
                exited_request_id = job["request_id"]
    if exited_request_id is None:
        return "该规划轮次仍有活动请求，或身份与进程退出状态无法确认，暂不能移除会话"
    return round_store.session_trash_block_reason(
        conn, task_id, run_id, now, exited_plan_request_id=exited_request_id,
    )


def _desktop_plan_finished(conn, row) -> bool:
    """Core publish can commit before the Desktop process exports its terminal job."""
    if row is None or row["backend"] != "codex_desktop":
        return False
    requests = conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND run_id=? "
        "AND stage='plan' AND kind='plan' AND status='succeeded'",
        (row["task_id"], row["run_id"]),
    ).fetchall()
    for request in requests:
        payload = db.request_payload(request)
        binding = payload.get("desktop_claim") or {}
        result = util.jload(request["result_json"]) or {}
        if (payload.get("planner_backend") == "codex_desktop"
                and isinstance(binding, dict) and binding.get("role") == "plan"
                and binding.get("run_id") == row["run_id"]
                and binding.get("operation_id") and binding.get("token_hash")
                and isinstance(result, dict) and result.get("outcome") == "succeeded"
                and isinstance(result.get("plan_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", result["plan_sha256"])):
            return True
    return False


def _round_run_status(conn, row, job, now, timeout) -> str:
    if _desktop_plan_finished(conn, row):
        return "finished"
    task=db.get_task(conn,row["task_id"])
    if task and row["run_id"]==task["run_id"]:
        from .execution_questions import wait_seconds
        timeout += wait_seconds(task,now,run_id=row["run_id"])
    return round_store.derive_run_status(row, job, now, timeout)


def _round_fields(
    conn,
    task_id: str,
    run_id: Optional[str],
    row: Optional[sqlite3.Row],
    now,
    timeout: int,
    jobs: Optional[dict] = None,
    source=None,
    force_purge_file_reason=_UNCHECKED,
) -> dict:
    """SessionInfo 的 §5.16.3 扩展字段：backend / run_status（job 派生 +
    超时判定，不回写）/ save_state / saved_at / allowed_actions /
    blocked_reason。无索引行 → 安全默认（backend null、run_status unknown、
    save_state active、allowed_actions []）。"""
    from .execution_questions import view as question_view
    task_for_question=db.get_task(conn,task_id)
    execution_question=question_view(task_for_question,now,conn) if task_for_question and task_for_question["run_id"]==run_id else None
    if row is None:
        # 无 execution_rounds 索引行（运行记录不可用/未关联）——回收站动作
        # 无法落库，给原因让 UI 可解释而非静默空菜单。
        return {
            "backend": None, "run_status": "unknown", "save_state": "active",
            "saved_at": None, "allowed_actions": [],
            "blocked_reason": "无执行轮次索引（运行记录不可用），"
                              "不能移入回收站",
        }
    job = (jobs or {}).get((task_id, run_id))
    save_state = row["save_state"] \
        if row["save_state"] in constants.SESSION_SAVE_STATES else "active"
    blocked: Optional[str] = None
    read_only = save_state == "purged"
    if save_state == "trashed":
        # restore 无禁止条件（§5.16.2）；v2 §5.20.2 追加永久移除入口。
        allowed = ["restore-session"]
        blocked = _session_trash_reason(conn, source or headless.resolve_source(conn), row, now)
        if blocked is None:
            allowed.append("purge-session")
    elif save_state == "purged":
        allowed = []
        blocked = "会话已永久从列表移除（只读审计）"
    else:
        reason = _session_trash_reason(conn, source or headless.resolve_source(conn), row, now)
        allowed = ["trash-session"] if reason is None else []
        blocked = reason
    if save_state in ("active", "trashed") and not row["session_id"]:
        if _force_purge_reason(conn, source or headless.resolve_source(conn), row,
                               force_purge_file_reason) is None:
            allowed.append("force-purge-session")
            blocked = None
    return {
        "execution_question": execution_question,
        "backend": row["backend"],
        "run_status": _round_run_status(conn, row, job, now, timeout),
        "save_state": save_state,
        "saved_at": row["saved_at"],
        "allowed_actions": allowed,
        "blocked_reason": blocked,
        "read_only": read_only,
    }


def _session_info_ex(
    conn,
    task_id: str,
    run_id: Optional[str],
    state: str,
    record: Optional[dict],
    row: Optional[sqlite3.Row],
    now,
    timeout: int,
    jobs: Optional[dict] = None,
    source=None,
) -> dict:
    info = _session_info(task_id, run_id, state, record)
    info.update(_round_fields(conn, task_id, run_id, row, now, timeout, jobs, source))
    # §5.15.5「最近状态检查」：last_checked_at = session_reads 位点行的刷新
    # 时间（session-messages/pending 每次读取都会刷新）；无位点 → null（从未
    # 检查过）。与会话更新时间（updated_at）分开展示，不把检查时间当进度时间。
    info["last_checked_at"] = (
        session_store.checked_at(
            conn, session_store.session_key_for(task_id, run_id)
        )
        if run_id is not None else None
    )
    return info


def _entry(conn, source, probe, task_id, run_id, row, *, light, effective_states,
           now, timeout, jobs, assoc=None) -> Optional[dict]:
    """索引行 + 会话探测 → SessionInfo（含 §5.16.3 扩展字段），按 save_state
    过滤（不匹配返回 None）。assoc 可外部预计算（兼容路径复用同一次探测）。"""
    if assoc is None:
        assoc = _associate(conn, source, task_id, run_id, probe, light=light)
    info = _session_info_ex(
        conn, task_id, run_id, assoc["state"], assoc["record"], row,
        now, timeout, jobs, source,
    )
    if effective_states is not None and \
            info["save_state"] not in effective_states:
        return None
    return info


def _compat_session(conn, source, probe, task, run_id, *, light, effective_states,
                    ctx, timeout, jobs) -> Optional[dict]:
    """历史任务无索引行时的兼容发现（§5.16.3 兼容条款）：按 v1.4 现有逻辑
    （tasks.run_id/session_marker + headless 探测）发现会话；探测到有效执行
    记录即做身份核验回填，再按 save_state 过滤输出。"""
    assoc = _associate(conn, source, task["task_id"], run_id, probe, light=light)
    row = None
    if run_id and assoc["state"] in (
        headless.SESSION_ASSOCIATED, headless.SESSION_PENDING
    ):
        with db.write_txn(conn):
            row, job = _backfill_one(
                conn, source, task["task_id"], run_id, ctx.now_iso
            )
        if job is not None:
            jobs[(task["task_id"], run_id)] = job
    return _entry(
        conn, source, probe, task["task_id"], run_id, row, light=light,
        effective_states=effective_states, now=ctx.now, timeout=timeout, jobs=jobs,
        assoc=assoc,
    )


# --------------------------------------------------------------- commands

def cmd_list_sessions(ctx, payload: dict) -> dict:
    conn = ctx.conn
    task_id = payload.get("task_id")
    # v1.5（§5.16.3）：会话保存状态筛选，缺省 active；非法值 bad_request
    #（空串按非法值处理：仅「未提供」才是缺省）。
    save_state = payload.get("save_state")
    if save_state is None:
        save_state = "active"
    if save_state not in (*constants.SESSION_SAVE_STATES, "all"):
        raise util.bad_request(
            f"invalid save_state {save_state!r}（允许 active|trashed|purged|all）"
        )
    # v2 §5.20.2：purged 退出常规列表——"all" 也不含；仅 save_state=purged
    # 或 include_removed:true 时可回看。
    include_removed = bool(payload.get("include_removed"))
    if save_state == "all" and not include_removed:
        effective_states = ("active", "trashed")
    elif save_state == "all":
        effective_states = None  # 含 purged
    else:
        effective_states = (save_state,)
    # payload 覆盖仅本次调用生效（不持久化）。
    source = headless.resolve_source(
        conn, runs_root=payload.get("session_runs_root")
    )
    probe = source.probe()
    timeout = round_store.round_timeout_seconds(conn)

    # 运行记录过多时轻量核验：只 stat 不读文件内容；associated 态由
    # get-session/session-messages 补齐（语义保持，§5.15.5）。
    light = (
        probe.get("state") == headless.CONN_DEGRADED
        and source.task_dir_count() > constants.LIST_SESSIONS_LIGHT_THRESHOLD
    )

    if task_id:
        task = db.require_task(conn, task_id)
        # v2 §5.20.2：purged 任务的会话退出常规列表（include_removed 回看）。
        if db.task_save_state(task) == "purged" and not include_removed:
            return {"sessions": []}
        # 1) 发现回填（§5.16.1）：身份核验通过才 upsert_discovered；
        # 回填不触碰 save_state（不复活已删除会话）。
        jobs = _discover_rounds(
            conn, source, probe, ctx.now_iso, task_id=task["task_id"]
        )
        run_id = _resolve_run_id(task, None)
        sessions = []
        # 2) 基于 execution_rounds 索引列举 + save_state 过滤。
        rows = round_store.list_rounds(
            conn, task_id=task["task_id"], save_state=save_state
        )
        for row in rows:
            item = _entry(
                conn, source, probe, task["task_id"], row["run_id"], row,
                light=light, effective_states=effective_states, now=ctx.now,
                timeout=timeout, jobs=jobs,
            )
            if item is not None:
                sessions.append(item)
        # 3) 兼容：当前/最近轮次尚无索引行 → 现有逻辑发现并回填后输出。
        covered = run_id is not None and \
            round_store.get_round(conn, task["task_id"], run_id) is not None
        if not covered and (
            task["run_id"] is not None or task["session_marker"] is not None
        ):
            item = _compat_session(
                conn, source, probe, task, run_id, light=light,
                effective_states=effective_states, ctx=ctx, timeout=timeout, jobs=jobs,
            )
            if item is not None:
                sessions.append(item)
        return {"sessions": sessions}

    # 缺省：发现回填全部已发现目录，再按索引行输出（有关联任务的会话优先）。
    jobs = _discover_rounds(conn, source, probe, ctx.now_iso)
    all_rows = round_store.list_rounds(conn, None, None)
    covered = {(row["task_id"], row["run_id"]) for row in all_rows}
    known_tasks = {
        row["task_id"] for row in conn.execute("SELECT task_id FROM tasks")
    }
    sessions = []
    purged_tasks = set()
    if not include_removed:
        purged_tasks = {
            row_["task_id"] for row_ in conn.execute(
                "SELECT task_id FROM tasks WHERE save_state='purged'"
            )
        }
    for row in all_rows:
        # §5.15.5 缺省语义：只输出有关联任务的会话。
        if row["task_id"] not in known_tasks:
            continue
        # v2 §5.20.2：purged 任务的轮次退出常规列表（无论行自身状态）。
        if row["task_id"] in purged_tasks:
            continue
        if effective_states is not None and \
                row["save_state"] not in effective_states:
            continue
        item = _entry(
            conn, source, probe, row["task_id"], row["run_id"], row,
            light=light, effective_states=effective_states, now=ctx.now,
            timeout=timeout, jobs=jobs,
        )
        if item is not None:
            sessions.append(item)
    # 兼容：历史任务（tasks.run_id/session_marker）无索引行 → 现有逻辑发现回填。
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE run_id IS NOT NULL "
        "OR session_marker IS NOT NULL "
        "ORDER BY CASE save_state WHEN 'active' THEN 0 ELSE 1 END, "
        "updated_at DESC"
    ).fetchall()
    for task in tasks:
        # v2 §5.20.2：purged 任务的兼容会话同样排除（否则无法回收的
        # 「幽灵行」出现在执行会话列表）。
        if task["task_id"] in purged_tasks:
            continue
        run_id = _resolve_run_id(task, None)
        if run_id is not None and (task["task_id"], run_id) in covered:
            continue
        item = _compat_session(
            conn, source, probe, task, run_id, light=light,
            effective_states=effective_states, ctx=ctx, timeout=timeout, jobs=jobs,
        )
        if item is not None:
            sessions.append(item)
    return {"sessions": sessions}


def _load_status_snapshot(conn, marker: str) -> Optional[dict]:
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_STATUS_SNAPSHOT_PREFIX + marker,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if (isinstance(data, dict) and data.get("cache_schema") == 1
            and isinstance(data.get("record"), dict)
            and isinstance(data.get("events"), list)
            and isinstance(data.get("source_signature"), dict)):
        return data
    return None


def _status_summary(job: dict, events: list) -> dict:
    """Keep lifecycle proof and scalar metadata, never message bodies/revisions."""
    session = dsh_events.session_event(events) or {}
    timestamps = [event.get("ts") for event in events if event.get("ts")]
    ids = set()
    for event in events:
        kind = event.get("type")
        if kind not in dsh_events._EVENT_TO_MESSAGE:
            continue
        source_id, revision = event.get("source_message_id"), event.get("source_revision")
        if (isinstance(source_id, str) and source_id and isinstance(revision, int)
                and not isinstance(revision, bool) and revision > 0):
            ids.add(("desktop", source_id, kind))
        else:
            ids.add(("round", event.get("seq")))
    record = {
        "session_id": session.get("session_id") or job.get("session_id"),
        "session_title": session.get("session_title") or job.get("session_title"),
        "started_at": job.get("started_at") or (timestamps[0] if timestamps else None),
        "last_message_at": timestamps[-1] if timestamps else None,
        "message_count": len(ids),
        "updated_at": timestamps[-1] if timestamps else job.get("updated_at"),
    }
    lifecycle = []
    if session:
        lifecycle.append({key: session.get(key) for key in
                          ("type", "ts", "session_id", "session_title", "turn_id")})
    # Only the last superseded and current terminal affect round_detail.
    terminals = dsh_events.terminal_events(events)
    resumed = job.get("observation_resumed_at")
    if job.get("status") == "running" and util.parse_ts(resumed) is not None:
        old = [event for event in terminals if util.ts_before(event.get("ts"), resumed)]
        current = [event for event in terminals if not util.ts_before(event.get("ts"), resumed)]
        terminals = old[-1:] + current[-1:]
    else:
        terminals = terminals[-1:]
    lifecycle.extend({key: event.get(key) for key in
                      ("type", "ts", "outcome", "reason")} for event in terminals)
    return {"record": record, "events": lifecycle,
            "round_job": {key: job.get(key) for key in
                          ("status", "pid", "observe_pid", "ended_at", "exit_code", "detail",
                           "turn_id", "turn_adopted", "send_state", "observation_resumed_at")},
            "has_actual_content": any(event.get("session_id") or event.get("type")
                                      not in ("system", "terminal", "turn_end")
                                      for event in events)}


def _status_force_signature(conn, source, task_id: str, run_id: str) -> Optional[dict]:
    """Invalidate filesystem-only purge proof when any supporting source changes."""
    try:
        directory = source.run_dir(task_id, run_id)
        paths = [source.runs_root, directory]
        paths.extend(directory / name for name in
                     ("job.json", "events.jsonl", "conversation.json", "last-message.txt"))
        stamps = []
        for path in paths:
            try:
                info = path.lstat()
                stamps.append([info.st_dev, info.st_ino, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns, info.st_mode])
            except FileNotFoundError:
                stamps.append(None)
        marker = session_store.session_key_for(task_id, run_id)
        # Snapshot writers use INSERT OR REPLACE: rowid changes even at equal size.
        snapshots = [list(row) for row in conn.execute(
            "SELECT key,rowid,length(value) FROM settings WHERE key IN (?,?) ORDER BY key",
            (_SNAPSHOT_PREFIX + marker, _SNAPSHOT_META_PREFIX + marker),
        )]
        return {"paths": stamps, "snapshots": snapshots}
    except (OSError, headless.HeadlessError):
        return None


def _get_session_status(ctx, payload: dict) -> dict:
    """Small cross-process status cache; no reader positions or transcript requests."""
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    task_id = task["task_id"]
    run_id = _resolve_run_id(task, payload.get("run_id"))
    marker = session_store.session_key_for(task_id, run_id) if run_id else None
    source = headless.resolve_source(conn)
    probe = source.probe()
    connection = _connection_view(probe, headless.poll_seconds(conn))
    cached = _load_status_snapshot(conn, marker) if marker else None
    if cached and cached["source_signature"].get("runs_root") != str(source.runs_root.resolve()):
        cached = None
    summary, job, signature = None, None, None
    state, error = headless.SESSION_PENDING, None
    verified = False
    if probe.get("state") == headless.CONN_UNAVAILABLE:
        state, error = headless.SESSION_UNAVAILABLE, probe.get("detail")
        summary = cached
    elif run_id is not None:
        try:
            directory = source.run_dir(task_id, run_id)
            if any((directory / name).is_symlink() for name in
                   ("job.json", "events.jsonl", "conversation.json", "last-message.txt")):
                raise headless.HeadlessError("unavailable", "运行记录文件为符号链接，无法安全核验状态")
            before = source.source_signature(task_id, run_id)
            job, error = dsh_events.read_job(directory, task_id, run_id, backend=None)
            if error or job.get("backend") not in constants.ROUND_BACKENDS:
                state = headless.SESSION_UNAVAILABLE
                error = error or "运行记录后端不可用"
                if cached and ("解析失败" in error or "缺失或不可读" in error):
                    summary = cached
                if "解析失败" in error:
                    # A half-written job is recoverable: keep polling instead
                    # of marking the session unavailable like an identity error.
                    state = (cached.get("association_state", headless.SESSION_ASSOCIATED)
                             if cached else headless.SESSION_PENDING)
                job = None
            else:
                # Replaced session identity invalidates the old summary as well.
                if cached and (cached["source_signature"].get("backend") != job["backend"]
                               or (job.get("session_id") and cached["record"].get("session_id")
                                   and job["session_id"] != cached["record"]["session_id"])):
                    cached = None
                after = source.source_signature(task_id, run_id)
                if (cached and before and after and before["signature"] == after["signature"]
                        == cached["source_signature"]):
                    summary, signature = cached, after["signature"]
                    verified = cached.get("verified", True)
                    error = cached.get("error")
                    job = after["job"]
                    state = cached.get("association_state", headless.SESSION_ASSOCIATED)
                else:
                    events, detail, complete = dsh_events._read_events(directory / dsh_events.EVENT_LOG_NAME)
                    after = source.source_signature(task_id, run_id)
                    if (events and complete and before and after
                            and before["signature"] == after["signature"]):
                        summary = _status_summary(after["job"], events)
                        signature, job, verified = after["signature"], after["job"], True
                        state = headless.SESSION_ASSOCIATED
                    else:
                        if detail in ("事件日志尚未生成", "事件日志为空"):
                            # Valid job with no exported events is normal waiting,
                            # not a reader failure that masks runtime status.
                            error, state = None, headless.SESSION_PENDING
                            summary = {"record": dict(_EMPTY_VIEW), "events": [],
                                       "has_actual_content": False}
                        else:
                            error = detail or "事件日志仍在写入或读取期间发生变化，无法确认当前状态"
                        if cached and summary is None:
                            summary = cached
                            state = cached.get("association_state", headless.SESSION_ASSOCIATED)
                        if before and after and before["signature"] == after["signature"]:
                            # Remember a stable incomplete version without certifying it.
                            # Polling that same version must not re-read its full body.
                            signature = after["signature"]
                            if summary is None:
                                summary = {"record": dict(_EMPTY_VIEW), "events": [],
                                           "has_actual_content": False}
        except (OSError, headless.HeadlessError) as exc:
            state, error = headless.SESSION_UNAVAILABLE, getattr(exc, "detail", str(exc))
            job = None
    row = round_store.get_round(conn, task_id, run_id)
    # Discovery uses authenticated job identity even before events exist, just
    # like the legacy get-session path. It never changes save_state.
    if job is not None and _refresh_needed(row, job):
        with db.write_txn(conn):
            row = _backfill_from_job(conn, task_id, run_id, ctx.now_iso, job)
    file_reason = _UNCHECKED
    proof_signature = None
    if row is not None and not row["session_id"] and row["save_state"] != "purged":
        if verified:
            if summary.get("has_actual_content"):
                file_reason = "该轮次存在实际会话事件，不能强制移除"
            else:
                proof_signature = _status_force_signature(conn, source, task_id, run_id)
                if (proof_signature is not None and cached
                        and cached.get("force_signature") == proof_signature
                        and "force_file_reason" in cached):
                    file_reason = cached["force_file_reason"]
                else:
                    file_reason = _force_purge_file_reason(conn, source, row)
                    if proof_signature != _status_force_signature(conn, source, task_id, run_id):
                        file_reason = "运行记录正在变化，无法确认会话记录已缺失"
                        proof_signature = None
    if signature is not None:
        snapshot = dict(summary, cache_schema=1, source_signature=signature,
                        verified=verified, error=error, association_state=state)
        # Proof belongs to a confirmed source version only.
        snapshot.pop("force_signature", None)
        snapshot.pop("force_file_reason", None)
        if proof_signature is not None:
            snapshot.update(force_signature=proof_signature, force_file_reason=file_reason)
        if snapshot != cached:
            with db.write_txn(conn):
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                             (_STATUS_SNAPSHOT_PREFIX + marker,
                              json.dumps(snapshot, ensure_ascii=False, sort_keys=True)))
    timeout = round_store.round_timeout_seconds(conn)
    session = _session_info(task_id, run_id, state, summary["record"] if summary else None)
    if summary and state == headless.SESSION_UNAVAILABLE:
        # Offline scalar metadata is retained, but current status remains unknown.
        session.update(headless.session_record_view(summary["record"]))
    session.update(_round_fields(conn, task_id, run_id, row, ctx.now, timeout,
                                {(task_id, run_id): job} if verified else None,
                                source, file_reason))
    session["last_checked_at"] = ctx.now_iso
    detail = dsh_events.round_detail(row, job if verified else
                                   (summary.get("round_job") if summary else None),
                                   summary["events"] if summary else [])
    if detail is not None:
        detail["startup_turn_id"] = (dsh_events.session_event(summary["events"])
                                     or {}).get("turn_id") if summary else None
        detail["run_status"] = session["run_status"]
        if (verified and session["run_status"] == "failed" and job.get("status") == "running"
                and not detail.get("detail")):
            detail["detail"] = f"执行轮次超时：已超过 {timeout} 秒仍未结束"
    if not verified and not _desktop_plan_finished(conn, row):
        session["run_status"] = "unknown"
        if detail is not None:
            detail["run_status"] = "unknown"
            detail["detail"] = error or "尚未取得可核验的运行状态"
    if error:
        connection["detail"] = error
    return {"session": session, "connection": connection, "round_detail": detail}


def cmd_get_session(ctx, payload: dict) -> dict:
    if payload.get("status_only"):
        return _get_session_status(ctx, payload)
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    run_id = _resolve_run_id(task, payload.get("run_id"))
    source = headless.resolve_source(conn)
    timeout = round_store.round_timeout_seconds(conn)
    jobs: dict = {}
    # 读取层回填（§5.16.1 写入点含 get-session）：身份核验通过才写索引；
    # 无发现类字段变化时零写入（重复回填不扰动索引）。
    row = round_store.get_round(conn, task["task_id"], run_id)
    job, directory = _identity_job(source, task["task_id"], run_id)
    if job is not None:
        if run_id:
            jobs[(task["task_id"], run_id)] = job
        if _refresh_needed(row, job):
            with db.write_txn(conn):
                row = _backfill_from_job(
                    conn, task["task_id"], run_id, ctx.now_iso, job
                )
    assoc = _associate(conn, source, task["task_id"], run_id)
    session = _session_info_ex(
        conn, task["task_id"], run_id, assoc["state"], assoc["record"], row,
        ctx.now, timeout, jobs, source,
    )
    # round_detail（§5.18.4）：索引行 + job 进程结果 + terminal 事件；
    # v3：pi/pi_desktop（及 codex 规划轮次）均读 events.jsonl；读不到给安全
    # None/空（不报错）。
    round_detail = None
    if row is not None:
        events = assoc.get("_events") or []
        round_detail = dsh_events.round_detail(row, job, events)
        # Dispatch confirms the immutable startup proof, not the Goal follow-up turn.
        round_detail["startup_turn_id"] = (dsh_events.session_event(events) or {}).get("turn_id")
        # §5.18.4：run_status 与 SessionInfo 同口径（读取层超时派生，不回写
        # 索引/job.json）；超时派生 failed 时按 §5.18.4「明确异常 detail 记原因」
        # 由命令层补充 detail（round_store.derive_run_status 同指示）。
        derived = _round_run_status(conn, row, job, ctx.now, timeout)
        if round_detail.get("run_status") != derived:
            round_detail["run_status"] = derived
            if derived == "failed" and isinstance(job, dict) \
                    and job.get("status") == "running" \
                    and not round_detail.get("detail"):
                round_detail["detail"] = (
                    f"执行轮次超时：已超过 {timeout} 秒仍未结束"
                )
    return {
        "session": session,
        "connection": assoc["connection"],
        "round_detail": round_detail,
    }


def cmd_session_messages(ctx, payload: dict) -> dict:
    conn = ctx.conn
    task = db.require_task(conn, payload["task_id"])
    task_id = task["task_id"]
    run_id = _resolve_run_id(task, payload.get("run_id"))
    limit = payload.get("limit")
    if limit is None:
        limit = constants.DEFAULT_SESSION_MESSAGE_LIMIT

    source = headless.resolve_source(conn)
    key = session_store.session_key_for(task_id, run_id) if run_id else None
    meta = _load_snapshot_meta(conn, key) if key else None
    effective_cursor = payload.get("cursor")
    after_seq = headless.parse_cursor(effective_cursor)
    # Explicit end-of-version acknowledgement only. No snapshot/revision read
    # may precede this branch, including the metadata timestamp query below.
    if (meta and payload.get("source_version") == meta["source_version"]
            and effective_cursor is not None and after_seq is not None
            and after_seq >= meta["max_seq"] and not payload.get("force_reload")):
        before = source.source_signature(task_id, run_id)
        after = source.source_signature(task_id, run_id) if before else None
        if (before and after
                and before["signature"] == after["signature"] == meta.get("source_signature")
                and _source_version(after["signature"]) == meta["source_version"]
                and source.probe().get("state") != headless.CONN_UNAVAILABLE):
            with db.write_txn(conn):
                session_store.touch_position(conn, key, task_id, run_id,
                                             headless.SESSION_ASSOCIATED, ctx.now_iso)
            row = round_store.get_round(conn, task_id, run_id)
            session = _session_info_ex(
                conn, task_id, run_id, headless.SESSION_ASSOCIATED, meta["record"],
                row, ctx.now, round_store.round_timeout_seconds(conn),
                {(task_id, run_id): after["job"]}, source,
            )
            return {"session": session, "messages": [],
                    "next_cursor": effective_cursor, "has_more": False,
                    "connection": _connection_view(source.probe(), headless.poll_seconds(conn)),
                    "source_version": meta["source_version"]}

    assoc = _associate(conn, source, task_id, run_id)
    signature = assoc.get("_source_signature")
    source_version = _source_version(signature) if signature else None
    connection = assoc["connection"]
    # §5.16.3：session-messages 返回同扩展的 SessionInfo（索引行缺失给安全
    # 默认；本命令非 §5.16.1 回填写入点，只读既有索引行）。session 在位点
    # 刷新之后构建：last_checked_at 反映本次状态检查（§5.15.5）。
    row = round_store.get_round(conn, task_id, run_id)
    timeout = round_store.round_timeout_seconds(conn)

    def build_session() -> dict:
        return _session_info_ex(
            conn, task_id, run_id, assoc["state"], assoc["record"], row,
            ctx.now, timeout, source=source,
        )

    # 不可关联 / 不可用：无消息，且不覆盖 session_reads 已存位点（断线补齐用）。
    if assoc["state"] != headless.SESSION_ASSOCIATED:
        if assoc["state"] == headless.SESSION_PENDING and run_id is not None:
            _record_pending(conn, task_id, run_id, ctx.now_iso)
        return {
            "session": build_session(),
            "messages": [],
            "next_cursor": None,
            "has_more": False,
            "connection": connection,
            "source_version": None,
        }

    key = session_store.session_key_for(task_id, run_id)
    stored = session_store.get_position(conn, key)
    # 游标语义（protocol §5.15.5）：payload 无 cursor = 首次加载/重开，一律返回
    # 完整历史；增量须由调用方显式传 next_cursor。session_reads 位点只用于
    # 尾部 revision 对比，不得隐式充当游标（否则重开阅读器丢历史）。
    # get_position 已把 last_revision 解码为 id→revision 映射。
    revisions = stored["last_revision"] if stored else {}
    sliced = headless.slice_messages(
        assoc["messages"], after_seq, limit, revisions
    )
    messages = [
        headless.normalize_message(raw, run_id=run_id)
        for raw in sliced["messages"]
    ]

    next_cursor = sliced["next_cursor"]
    has_more = bool(sliced["has_more"])
    last_seq = _max_seq(messages)
    with db.write_txn(conn):
        session_store.save_position(
            conn, key, task_id, run_id, next_cursor, last_seq,
            # 尾部窗口 id→revision 快照（None = 本轮无位点推进，保持旧快照）。
            session_store.encode_revisions(
                headless.revision_snapshot(assoc["messages"], last_seq)
            ),
            headless.SESSION_ASSOCIATED, ctx.now_iso,
        )
        # 跨进程持久化最近有效快照：CLI 每次调用是新进程，半写入轮次靠它保留
        # 上一份有效内容（进程内缓存只覆盖同进程场景）。
        if source_version and (not meta or meta["source_version"] != source_version):
            available = _save_snapshot(conn, key, assoc["messages"], assoc["record"])
            new_meta = {"cache_schema": 1, "source_signature": signature,
                        "source_version": source_version, "record": assoc["record"],
                        "max_seq": _max_seq(assoc["messages"]),
                        "full_snapshot_available": available}
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                         (_SNAPSHOT_META_PREFIX + key,
                          json.dumps(new_meta, ensure_ascii=False, sort_keys=True)))

    return {
        "session": build_session(),
        "messages": messages,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "connection": connection,
        "source_version": source_version,
    }


def _record_pending(
    conn: sqlite3.Connection, task_id: str, run_id: str, now: str
) -> None:
    """pending 时只刷新 state/updated_at，保留已存 cursor/last_seq 不回退；
    revision 快照显式清空（传 "{}"：None 会被当作「保持不变」，不得让
    pending 读取残留上一轮 messages 的 revisions）。"""
    key = session_store.session_key_for(task_id, run_id)
    with db.write_txn(conn):
        session_store.save_position(
            conn, key, task_id, run_id, None, None, "{}",
            headless.SESSION_PENDING, now,
        )


def _max_seq(messages: list) -> Optional[int]:
    seqs = [m["seq"] for m in messages if m.get("seq") is not None]
    return max(seqs) if seqs else None


# ----------------------------------------------------- v1.5 会话回收站（T1）
# OWNERSHIP: T1 subagent。trash-session / restore-session 只改
# execution_rounds.save_state（§5.16.2）：不删文件、不停进程、不改任务生命周期；
# 禁止条件复用 round_store.session_trash_block_reason；幂等不重复写事件。
# CLI 入口已由主 agent 注册（cli.py）。


def cmd_trash_session(ctx, payload: dict) -> dict:
    """trash-session {task_id, run_id, expected_save_state?} →
    {"ok": true, "deduplicated": bool, "session": SessionInfo}（§5.16.2）。"""
    return _save_session(ctx, payload, "trashed", "session_trashed")


def cmd_restore_session(ctx, payload: dict) -> dict:
    """restore-session {task_id, run_id} → 同上形状（§5.16.2，幂等；
    restore 无禁止条件）。"""
    return _save_session(ctx, payload, "active", "session_restored")


def _save_session(ctx, payload: dict, target_state: str, event_kind: str) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    task_id, run_id = payload["task_id"], payload["run_id"]
    expected = payload.get("expected_save_state")
    source = headless.resolve_source(conn)
    job: Optional[dict] = None
    with db.write_txn(conn):
        db.require_task(conn, task_id)
        # 轮次不存在且无法经身份核验回填 → not_found（§5.16.1 写入点含本命令）。
        _row, job = _backfill_one(conn, source, task_id, run_id, now)
        row = round_store.require_round(conn, task_id, run_id)
        current = row["save_state"]
        # v2 §5.20.1：purged 终态——不可再回收/恢复。
        if current == "purged":
            raise util.KanpilotError(
                "conflict",
                "该会话轮次已永久从列表移除（终态），不能再变更保存状态",
            )
        if current == target_state:
            # 已处于目标状态 → 幂等成功，不重复写事件（§5.16.2）。
            deduplicated = True
        else:
            # expected_save_state 与当前不符（且非目标态）→ conflict。
            if expected is not None and current != expected:
                raise util.KanpilotError(
                    "conflict",
                    f"会话保存状态冲突（期望 {expected}，实际为 {current}）",
                )
            # 禁止删除（§5.16.2）：conflict + 中文原因；restore 无禁止条件。
            if target_state == "trashed":
                reason = _session_trash_reason(conn, source, row, ctx.now)
                if reason is not None:
                    raise util.KanpilotError("conflict", reason)
            round_store.set_save_state(conn, task_id, run_id, target_state, now)
            db.add_event(
                conn, now, event_kind, task_id=task_id, actor="user",
                detail={"run_id": run_id, "from": current, "to": target_state},
            )
            deduplicated = False
        row = round_store.require_round(conn, task_id, run_id)
    # 事务外构建 SessionInfo（会话文件探测不占写锁）。
    assoc = _associate(conn, source, task_id, run_id)
    session = _session_info_ex(
        conn, task_id, run_id, assoc["state"], assoc["record"], row,
        ctx.now, round_store.round_timeout_seconds(conn),
        {(task_id, run_id): job} if job is not None else None,
        source,
    )
    return {"ok": True, "deduplicated": deduplicated, "session": session}


# ----------------------------------------------------- v2 purge-session（§5.20.2）

def cmd_purge_session(ctx, payload: dict) -> dict:
    """purge-session {task_id, run_id, expected_save_state:"trashed"} →
    {"ok", "deduplicated"}：仅从回收站永久从列表移除该轮次；文件与历史保留。"""
    conn, now = ctx.conn, ctx.now_iso
    task_id, run_id = payload["task_id"], payload["run_id"]
    expected = payload.get("expected_save_state")
    if expected is not None and expected != "trashed":
        raise util.bad_request(
            "purge-session 只能从回收站移除（expected_save_state 应为 trashed）"
        )
    source = headless.resolve_source(conn)
    with db.write_txn(conn):
        db.require_task(conn, task_id)
        _row, _job = _backfill_one(conn, source, task_id, run_id, now)
        row = round_store.require_round(conn, task_id, run_id)
        current = row["save_state"]
        if current == "purged":
            return {"ok": True, "deduplicated": True}
        if current != "trashed":
            raise util.KanpilotError(
                "conflict",
                f"会话当前保存状态为 {current}，只能对回收站中的会话执行"
                "永久移除（先移入回收站）",
            )
        # 同事务复核安全条件（复用回收站禁用检查——trashed 行运行中/失联/
        # 发送不明的场景理论不可达，防御性复核）。
        reason = _session_trash_reason(conn, source, row, ctx.now)
        if reason is not None:
            raise util.KanpilotError("conflict", reason)
        round_store.set_save_state(conn, task_id, run_id, "purged", now)
        db.add_event(
            conn, now, "session_purged", task_id=task_id, actor="user",
            detail={"run_id": run_id, "from": current, "to": "purged"},
        )
    return {"ok": True, "deduplicated": False}


def cmd_force_purge_session(ctx, payload: dict) -> dict:
    """仅强制移除确认无实际会话的残留轮次；不删除文件、请求或历史。"""
    conn, now = ctx.conn, ctx.now_iso
    task_id, run_id = payload["task_id"], payload["run_id"]
    expected = payload.get("expected_save_state")
    if expected is not None and expected not in constants.SESSION_SAVE_STATES:
        raise util.bad_request("expected_save_state 应为 active、trashed 或 purged")
    source = headless.resolve_source(conn)
    with db.write_txn(conn):
        db.require_task(conn, task_id)
        row = round_store.require_round(conn, task_id, run_id)
        current = row["save_state"]
        if current == "purged":
            return {"ok": True, "deduplicated": True}
        if expected is not None and current != expected:
            raise util.KanpilotError(
                "conflict", f"会话保存状态冲突（期望 {expected}，实际为 {current}）",
            )
        if current not in ("active", "trashed"):
            raise util.KanpilotError("conflict", "该会话保存状态不能强制移除")
        reason = _force_purge_reason(conn, source, row)
        if reason:
            raise util.KanpilotError("conflict", reason)
        round_store.set_save_state(conn, task_id, run_id, "purged", now)
        db.add_event(
            conn, now, "session_purged", task_id=task_id, actor="user",
            detail={"run_id": run_id, "from": current, "to": "purged",
                    "forced": True, "reason": "no_actual_session"},
        )
    return {"ok": True, "deduplicated": False}
