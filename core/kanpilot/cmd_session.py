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
import sqlite3
from pathlib import Path
from typing import Optional

from . import (
    constants, db, dsh_events, headless_adapter as headless, round_store,
    session_store, util,
)

# 跨进程最近有效快照（存 settings；仅 KaneoPilot 自己的库）。
_SNAPSHOT_PREFIX = "session_snapshot:"
_SNAPSHOT_MAX_BYTES = 2_000_000


def _save_snapshot(conn, marker: str, messages: list, record) -> None:
    payload = json.dumps({"messages": messages, "record": record},
                         ensure_ascii=False)
    if len(payload.encode("utf-8")) > _SNAPSHOT_MAX_BYTES:
        return
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (_SNAPSHOT_PREFIX + marker, payload),
    )


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
    if detail:
        # 读取失败保留内容时 connection 仍 degraded，detail 透出原因。
        connection = dict(connection)
        connection["detail"] = detail
    if status == headless.SESSION_ASSOCIATED:
        return {
            "connection": connection,
            "state": headless.SESSION_ASSOCIATED, "detail": detail,
            "record": result.get("record"),
            "messages": result.get("messages"),
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


def _round_fields(
    conn,
    task_id: str,
    run_id: Optional[str],
    row: Optional[sqlite3.Row],
    now,
    timeout: int,
    jobs: Optional[dict] = None,
) -> dict:
    """SessionInfo 的 §5.16.3 扩展字段：backend / run_status（job 派生 +
    超时判定，不回写）/ save_state / saved_at / allowed_actions /
    blocked_reason。无索引行 → 安全默认（backend null、run_status unknown、
    save_state active、allowed_actions []）。"""
    if row is None:
        return {
            "backend": None, "run_status": "unknown", "save_state": "active",
            "saved_at": None, "allowed_actions": [], "blocked_reason": None,
        }
    job = (jobs or {}).get((task_id, run_id))
    save_state = row["save_state"] \
        if row["save_state"] in constants.SESSION_SAVE_STATES else "active"
    blocked: Optional[str] = None
    read_only = save_state == "purged"
    if save_state == "trashed":
        # restore 无禁止条件（§5.16.2）；v2 §5.20.2 追加永久移除入口。
        allowed = ["restore-session", "purge-session"]
    elif save_state == "purged":
        allowed = []
        blocked = "会话已永久从列表移除（只读审计）"
    else:
        reason = round_store.session_trash_block_reason(
            conn, task_id, run_id, now
        )
        allowed = ["trash-session"] if reason is None else []
        blocked = reason
    return {
        "backend": row["backend"],
        "run_status": round_store.derive_run_status(row, job, now, timeout),
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
) -> dict:
    info = _session_info(task_id, run_id, state, record)
    info.update(_round_fields(conn, task_id, run_id, row, now, timeout, jobs))
    # §5.15.5「最近状态检查」：last_checked_at = session_reads 位点行的刷新
    # 时间（session-messages/pending 每次读取都会刷新）；无位点 → null（从未
    # 检查过）。与会话更新时间（updated_at）分开展示，不把检查时间当进度时间。
    position = (
        session_store.get_position(
            conn, session_store.session_key_for(task_id, run_id)
        )
        if run_id is not None else None
    )
    info["last_checked_at"] = position["updated_at"] if position else None
    return info


def _entry(conn, source, probe, task_id, run_id, row, *, light, effective_states,
           now, timeout, jobs, assoc=None) -> Optional[dict]:
    """索引行 + 会话探测 → SessionInfo（含 §5.16.3 扩展字段），按 save_state
    过滤（不匹配返回 None）。assoc 可外部预计算（兼容路径复用同一次探测）。"""
    if assoc is None:
        assoc = _associate(conn, source, task_id, run_id, probe, light=light)
    info = _session_info_ex(
        conn, task_id, run_id, assoc["state"], assoc["record"], row,
        now, timeout, jobs,
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
    for row in all_rows:
        # §5.15.5 缺省语义：只输出有关联任务的会话。
        if row["task_id"] not in known_tasks:
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


def cmd_get_session(ctx, payload: dict) -> dict:
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
        ctx.now, timeout, jobs,
    )
    # round_detail（§5.18.4）：索引行 + job 进程结果 + terminal 事件；
    # backend==pi 才读 events.jsonl；读不到给安全 None/空（不报错）。
    round_detail = None
    if row is not None:
        events: list = []
        if directory is not None and row["backend"] == "pi":
            events, _event_detail = dsh_events.iter_events(
                Path(directory) / dsh_events.EVENT_LOG_NAME
            )
        round_detail = dsh_events.round_detail(row, job, events)
        # §5.18.4：run_status 与 SessionInfo 同口径（读取层超时派生，不回写
        # 索引/job.json）；超时派生 failed 时按 §5.18.4「明确异常 detail 记原因」
        # 由命令层补充 detail（round_store.derive_run_status 同指示）。
        derived = round_store.derive_run_status(row, job, ctx.now, timeout)
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
    assoc = _associate(conn, source, task_id, run_id)
    connection = assoc["connection"]
    # §5.16.3：session-messages 返回同扩展的 SessionInfo（索引行缺失给安全
    # 默认；本命令非 §5.16.1 回填写入点，只读既有索引行）。session 在位点
    # 刷新之后构建：last_checked_at 反映本次状态检查（§5.15.5）。
    row = round_store.get_round(conn, task_id, run_id)
    timeout = round_store.round_timeout_seconds(conn)

    def build_session() -> dict:
        return _session_info_ex(
            conn, task_id, run_id, assoc["state"], assoc["record"], row,
            ctx.now, timeout,
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
        }

    key = session_store.session_key_for(task_id, run_id)
    stored = session_store.get_position(conn, key)
    # 游标语义（protocol §5.15.5）：payload 无 cursor = 首次加载/重开，一律返回
    # 完整历史；增量须由调用方显式传 next_cursor。session_reads 位点只用于
    # 尾部 revision 对比，不得隐式充当游标（否则重开阅读器丢历史）。
    effective_cursor = payload.get("cursor")
    after_seq = headless.parse_cursor(effective_cursor)
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
        _save_snapshot(conn, key, assoc["messages"], assoc["record"])

    return {
        "session": build_session(),
        "messages": messages,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "connection": connection,
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
                reason = round_store.session_trash_block_reason(
                    conn, task_id, run_id, ctx.now
                )
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
        reason = round_store.session_trash_block_reason(
            conn, task_id, run_id, ctx.now
        )
        if reason is not None:
            raise util.KanpilotError("conflict", reason)
        round_store.set_save_state(conn, task_id, run_id, "purged", now)
        db.add_event(
            conn, now, "session_purged", task_id=task_id, actor="user",
            detail={"run_id": run_id, "from": current, "to": "purged"},
        )
    return {"ok": True, "deduplicated": False}
