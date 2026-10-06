"""会话读取位点存储（state-model.md §1 / protocol.md §5.15.5）。

OWNERSHIP: session subagent. 封装 ``session_reads`` 表：记录每个 KaneoPilot 会话
（session_key = "<task_id>/<run_id>" = session_marker）最后一次读取的游标与位点，
供断线重连后补齐缺失消息，并支持尾部进行中步骤的修订透出。消息正文以 headless
运行记录数据源为准，本地**只存位点、不缓存全文**。

位点列的用法（v1.4，schema 不变）：``last_revision`` 列为 id→revision 快照的
JSON 文本编码（session_reads 表列类型 SQLite 动态 typing 允许；旧版为整数的行
解码为空映射，等价「无快照」）。空映射 ``{}`` 是合法的（空页），必须与哨兵
``None``（「保持不变」）区分：游标未推进的行用 None 保留旧快照，pending 等只刷
state/updated_at 的路径传 "{}" 显式清空，不遗留上一轮 messages 的 revisions。

写本表是 KaneoPilot 本地状态，不违反只读边界（不改任务状态、不写远端、
不输出凭据）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional

from . import util

# session.state 词汇（与 headless_adapter 一致）。
STATE_ASSOCIATED = "associated"
STATE_PENDING = "pending"
STATE_UNAVAILABLE = "unavailable"


def session_key_for(task_id: str, run_id: str) -> str:
    """session_key = session_marker = "<task_id>/<run_id>"。"""
    return f"{task_id}/{run_id}"


def encode_revisions(revisions: Optional[Dict[str, Any]]) -> Optional[str]:
    """id→revision 快照编码为 JSON 文本存 last_revision 列。

    空映射编码为 ``"{}"``（合法值：读取成功但本页无消息）；None → None，
    表示「保持不变」，两者不可混淆。
    """
    if revisions is None:
        return None
    return json.dumps(revisions, sort_keys=True, separators=(",", ":"))


def decode_revisions(value: Any) -> Dict[str, int]:
    """解码 last_revision。JSON 文本 → {str: int}；空/非法/旧版整数 → {}
    （无快照：增量读取时按 cursor 所在步骤整体重查兜底）。"""
    if isinstance(value, dict):
        source = value
    elif isinstance(value, str) and value:
        source = util.jload(value)
        if not isinstance(source, dict):
            return {}
    else:
        return {}
    decoded: Dict[str, int] = {}
    for key, revision in source.items():
        try:
            decoded[str(key)] = int(revision)
        except (TypeError, ValueError):
            continue
    return decoded


def get_position(
    conn: sqlite3.Connection, session_key: str
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM session_reads WHERE session_key=?", (session_key,)
    ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    return {
        "session_key": row["session_key"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "cursor": row["cursor"],
        "last_seq": row["last_seq"],
        # last_revision 列存 id→revision 快照（JSON 文本）；统一解码为 dict。
        "last_revision": decode_revisions(
            row["revision_map"] if "revision_map" in keys
            else row["last_revision"]
        ),
        "state": row["state"],
        "updated_at": row["updated_at"],
    }


def save_position(
    conn: sqlite3.Connection,
    session_key: str,
    task_id: str,
    run_id: str,
    cursor: Optional[str],
    last_seq: Optional[int],
    last_revision: Optional[str],
    state: str,
    now: str,
) -> None:
    """upsert 读取位点。游标推进不回退：仅当新 last_seq 不小于已存值时才更新
    cursor/last_seq/last_revision，避免乱序/重放把位点拉回。state/updated_at
    始终刷新。last_revision 为 encode_revisions 产物（JSON 文本快照）；
    **None 一律表示该列「保持不变」**，清空快照请显式传 "{}"。

    调用方需已处于写事务内（db.write_txn）。
    """
    existing = conn.execute(
        "SELECT last_seq, last_revision FROM session_reads WHERE session_key=?",
        (session_key,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO session_reads(session_key, task_id, run_id, cursor, "
            "last_seq, last_revision, state, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_key, task_id, run_id, cursor, last_seq, last_revision,
             state, now),
        )
        return
    prev_seq = existing["last_seq"]
    advance = (
        last_seq is not None and (prev_seq is None or last_seq >= prev_seq)
    )
    if advance:
        # None → 沿用已存快照（如缓存兜底读取未产出新页）。
        revision = (
            last_revision if last_revision is not None
            else existing["last_revision"]
        )
        conn.execute(
            "UPDATE session_reads SET task_id=?, run_id=?, cursor=?, "
            "last_seq=?, last_revision=?, state=?, updated_at=? "
            "WHERE session_key=?",
            (task_id, run_id, cursor, last_seq, revision, state, now,
             session_key),
        )
    elif last_revision is not None:
        # 位点不回退，但显式给了快照（如 pending 清空 "{}"）：一并刷新快照。
        conn.execute(
            "UPDATE session_reads SET task_id=?, run_id=?, state=?, "
            "updated_at=?, last_revision=? WHERE session_key=?",
            (task_id, run_id, state, now, last_revision, session_key),
        )
    else:
        # 位点与快照均不回退/不改动，仅刷新 state/updated_at（如关联状态变化）。
        conn.execute(
            "UPDATE session_reads SET task_id=?, run_id=?, state=?, updated_at=? "
            "WHERE session_key=?",
            (task_id, run_id, state, now, session_key),
        )
