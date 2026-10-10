"""执行轮次索引 execution_rounds（protocol.md §5.16 / state-model §1.2）。

OWNERSHIP: 主 agent（共享骨架，冻结后只读给子代理）。以 task_id + run_id 唯一
定位一个执行轮次；会话保存状态（save_state）与任务保存状态正交。

不变量：
- 回填（upsert_discovered）**永不改写** save_state/saved_at/config_json/
  started_at/pid——重复导入不得复活已删除会话（protocol §5.16.1）。
- 启动意图（started_at）由 dispatch-start 事务写入；非空即禁止再创建进程
  （§5.18.3——同一轮次只允许一次真实启动，pid 只作诊断不作存活依据）。
- run_status 是最近观察值：pi 轮次以 job.json.status 为准，读取时刷新；
  status=running 但 started_at + timeout 已过 → 派生 failed（detail=timeout，
  只在读取层派生，不回写 job.json）。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import constants, db, util

# execution_rounds 可写列（发现类字段：回填与观察允许刷新的列）。
_DISCOVERED_FIELDS = (
    "backend", "session_id", "session_title", "run_status",
)


def get_round(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[str]
) -> Optional[sqlite3.Row]:
    if not run_id:
        return None
    return conn.execute(
        "SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
        (task_id, run_id),
    ).fetchone()


def require_round(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[str]
) -> sqlite3.Row:
    row = get_round(conn, task_id, run_id)
    if row is None:
        raise util.KanpilotError(
            "not_found", f"execution round not found: {task_id}/{run_id}"
        )
    return row


def round_view(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """execution_rounds → JSON 视图（SessionInfo/round_detail 复用）。"""
    if row is None:
        return None
    config = util.jload(row["config_json"])
    return {
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "backend": row["backend"],
        "session_id": row["session_id"],
        "session_title": row["session_title"],
        "run_status": row["run_status"],
        "config": config if isinstance(config, dict) else None,
        "worktree_path": row["worktree_path"],
        "started_at": row["started_at"],
        "pid": row["pid"],
        "save_state": row["save_state"],
        "saved_at": row["saved_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def upsert_on_claim(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    config: dict,
    now: str,
    backend: str = "pi",
) -> None:
    """claim 时创建轮次（dispatch=pi + 固化配置快照，§5.17.3，v1.6；v2 起
    plan claim 登记 backend='codex' 轮次）。幂等：已存在则只刷新
    config_json/updated_at/backend（同 run 重放场景），不触碰 save_state。"""
    if backend not in constants.ROUND_BACKENDS:
        backend = "pi"
    existing = get_round(conn, task_id, run_id)
    if existing is None:
        conn.execute(
            "INSERT INTO execution_rounds("
            "task_id, run_id, backend, run_status, config_json, "
            "save_state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'active',?,?)",
            (task_id, run_id, backend, "pending", util.jdump(config), now, now),
        )
        return
    conn.execute(
        "UPDATE execution_rounds SET backend=?, config_json=?, updated_at=? "
        "WHERE task_id=? AND run_id=?",
        (backend, util.jdump(config), now, task_id, run_id),
    )


def upsert_discovered(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    now: str,
    backend: str = "pi",
    session_id: Optional[str] = None,
    session_title: Optional[str] = None,
    run_status: Optional[str] = None,
) -> sqlite3.Row:
    """读取层回填（§5.16.1）：job.json 身份核验通过后补入缺失行。
    **绝不触碰 save_state/saved_at/config_json/started_at/pid/worktree_path**
    ——重复导入不得复活已删除会话、不得覆盖启动意图与配置快照。"""
    if backend not in constants.ROUND_BACKENDS:
        # 历史/未知后端原样保留（读取层按「已弃用」处理），不改写观测值。
        if backend not in constants.LEGACY_ROUND_BACKENDS:
            backend = "pi"
    existing = get_round(conn, task_id, run_id)
    if existing is None:
        conn.execute(
            "INSERT INTO execution_rounds("
            "task_id, run_id, backend, session_id, session_title, run_status, "
            "save_state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?, 'active', ?,?)",
            (
                task_id, run_id, backend, session_id, session_title,
                run_status if run_status in constants.ROUND_RUN_STATUSES
                else "unknown",
                now, now,
            ),
        )
    else:
        fields, params = [], []
        for name, value in (
            ("backend", backend),
            ("session_id", session_id),
            ("session_title", session_title),
            ("run_status", run_status),
        ):
            # 只在显式提供时刷新发现类字段；backend 恒按最新核验值刷新。
            if name == "session_id" and not value:
                # 空观察值不能抹掉已知会话身份，否则缺文件后可能被误判为残留。
                continue
            if name == "backend" or value is not None:
                if name == "run_status" and value not in constants.ROUND_RUN_STATUSES:
                    continue
                fields.append(f"{name}=?")
                params.append(value)
        fields.append("updated_at=?")
        params.extend([now, task_id, run_id])
        conn.execute(
            f"UPDATE execution_rounds SET {', '.join(fields)} "
            "WHERE task_id=? AND run_id=?",
            params,
        )
    row = get_round(conn, task_id, run_id)
    assert row is not None
    return row


def mark_intent(
    conn: sqlite3.Connection, task_id: str, run_id: str, now: str
) -> bool:
    """dispatch-start 意图位（protocol §5.18.3-2）：started_at 为空则写入并
    返回 True（本轮拿到启动权）；非空返回 False（已启动 → 调用方幂等返回）。
    只在意图位，不写 pid/worktree/run_status（进程拉起成功后再 mark_started）。"""
    cur = conn.execute(
        "UPDATE execution_rounds SET started_at=?, updated_at=? "
        "WHERE task_id=? AND run_id=? AND started_at IS NULL",
        (now, now, task_id, run_id),
    )
    return cur.rowcount == 1


def mark_started(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    now: str,
    pid: Optional[int] = None,
    worktree_path: Optional[str] = None,
) -> None:
    """dispatch-start：启动意图落盘（started_at 必写）+ 进程 pid/worktree 记录。"""
    conn.execute(
        "UPDATE execution_rounds SET started_at=COALESCE(started_at, ?), "
        "pid=COALESCE(?, pid), worktree_path=COALESCE(?, worktree_path), "
        "run_status=CASE WHEN run_status IN ('pending','unknown') "
        "THEN 'running' ELSE run_status END, "
        "updated_at=? WHERE task_id=? AND run_id=?",
        (now, pid, worktree_path, now, task_id, run_id),
    )


def set_save_state(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    save_state: str,
    now: str,
) -> sqlite3.Row:
    """trash-session / restore-session 的存储动作（命令层先做前置与幂等判定）。"""
    if save_state not in constants.SESSION_SAVE_STATES:
        raise util.bad_request(f"invalid session save_state {save_state}")
    row = require_round(conn, task_id, run_id)
    if row["save_state"] == save_state:
        return row
    conn.execute(
        "UPDATE execution_rounds SET save_state=?, saved_at=?, updated_at=? "
        "WHERE task_id=? AND run_id=?",
        (
            save_state,
            now if save_state in ("trashed", "purged") else None,
            now, task_id, run_id,
        ),
    )
    return require_round(conn, task_id, run_id)


def list_rounds(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    save_state: Optional[str] = None,
) -> list:
    clauses, params = [], []
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if save_state and save_state != "all":
        clauses.append("save_state=?")
        params.append(save_state)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM execution_rounds{where} "
        "ORDER BY updated_at DESC, created_at DESC",
        params,
    ).fetchall()


def round_timeout_seconds(conn: sqlite3.Connection) -> int:
    # v1.6 改名 pi_round_timeout_seconds；旧 setting 键做一次性回退兼容。
    value = db.get_setting(conn, "pi_round_timeout_seconds")
    if value is None:
        value = db.get_setting(conn, "dsh_round_timeout_seconds")
    try:
        return int(value)
    except (TypeError, ValueError):
        return constants.DEFAULT_PI_ROUND_TIMEOUT_SECONDS


def derive_run_status(
    row: Optional[sqlite3.Row],
    job: Optional[dict],
    now,
    timeout_seconds: int,
) -> str:
    """读取层派生 run_status（§5.16.1/§5.18.4）：job.status 为准；
    status=running 但 started_at+timeout 已过 → failed（超时派生，不回写文件）。
    无行 → unknown；无 job → 沿用已存值。"""
    if row is None:
        return "unknown"
    stored = row["run_status"] if row["run_status"] in constants.ROUND_RUN_STATUSES \
        else "unknown"
    if not isinstance(job, dict):
        return stored
    status = job.get("status")
    if status == "running":
        started = util.parse_ts(job.get("started_at") or row["started_at"])
        now_ts = util.parse_ts(util.to_iso(now))
        if started is not None and now_ts is not None:
            if now_ts.timestamp() > started.timestamp() + timeout_seconds:
                return "failed"  # 超时派生（detail 由命令层按 job 补充）
        return "running"
    if status in ("finished", "failed"):
        return status
    return stored


def _plan_round_has_activity(
    conn: sqlite3.Connection, task_id: str, run_id: str
) -> bool:
    """规划删除只检查本轮次；同一任务的新规划不保护旧轮次。"""
    if conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND run_id=? "
        "AND status IN ('queued','claimed') LIMIT 1", (task_id, run_id),
    ).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM attempts WHERE run_id=? AND (task_id=? OR request_id IN "
        "(SELECT request_id FROM requests WHERE task_id=?)) "
        "AND (outcome IN ('claimed','running','started') OR "
        "(COALESCE(outcome,'')='' AND finished_at IS NULL)) LIMIT 1",
        (run_id, task_id, task_id),
    ).fetchone():
        return True
    return False


def _ended_plan_request(
    conn: sqlite3.Connection, task_id: str, run_id: str, request_id: str
) -> bool:
    """仅明确结束且没有同轮次活动的规划请求可解除历史运行观察值。"""
    request = db.get_request(conn, request_id)
    if (request is None or request["task_id"] != task_id
            or request["run_id"] != run_id or request["kind"] != "plan"
            or request["stage"] != "plan"
            or request["status"] not in ("cancelled", "failed", "succeeded")):
        return False
    return not _plan_round_has_activity(conn, task_id, run_id)


def session_trash_block_reason(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    now,
    *,
    exited_plan_request_id: Optional[str] = None,
) -> Optional[str]:
    """会话移入回收站的禁止条件（protocol §5.16.2）；None=允许。
    ① 仍在执行/评审：run_status=running，或该轮为任务当前轮次且 task.status
      ∈ {planning, dispatching, prepared, submitting, running, in_review}；
    ② 失联待处理：run_id 匹配的 claimed dispatch 请求租约过期未解除；
    ③ 发送状态不明：该轮为任务当前轮次且 send_state='unknown'。
    返回中文原因（UI 直接展示），允许时 None。"""
    row = get_round(conn, task_id, run_id)
    if row is None:
        raise util.KanpilotError(
            "not_found", f"execution round not found: {task_id}/{run_id}"
        )
    ended_plan = bool(exited_plan_request_id) and _ended_plan_request(
        conn, task_id, run_id, exited_plan_request_id
    )
    if row["run_status"] == "running" and not ended_plan:
        return "该执行轮次仍在运行，暂不能移入会话回收站"
    task = conn.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    current = False
    if task is not None:
        marker = f"{task_id}/{run_id}"
        current = (task["run_id"] == run_id) or (
            task["session_marker"] == marker
        )
        if current and not (ended_plan and task["status"] == "planning") and task["status"] in (
            "planning", "dispatching", "prepared", "submitting",
            "running", "in_review",
        ):
            label = {
                "planning": "规划", "dispatching": "派发", "prepared": "派发",
                "submitting": "派发", "running": "执行", "in_review": "评审",
            }.get(task["status"], task["status"])
            return f"该轮次任务正在{label}中，暂不能移入会话回收站"
        if current and task["send_state"] == "unknown":
            return "发送结果不明，暂不能移入会话回收站"
    # ② 该轮次（kind=dispatch）的 claimed 请求失联未解除。
    rows = conn.execute(
        "SELECT lease_expires_at FROM requests WHERE run_id=? "
        "AND status='claimed' AND kind='dispatch'",
        (run_id,),
    ).fetchall()
    for req in rows:
        if util.lease_lost(req["lease_expires_at"], now):
            return "存在未解除的执行端失联请求，暂不能移入会话回收站"
    return None


def session_force_purge_block_reason(
    conn: sqlite3.Connection, task_id: str, run_id: str
) -> Optional[str]:
    """真实活动保护；索引 run_status 不足以判定残留轮次仍在运行。"""
    if conn.execute(
        "SELECT 1 FROM requests WHERE task_id=? AND run_id=? "
        "AND status IN ('queued','claimed') LIMIT 1", (task_id, run_id)
    ).fetchone():
        return "该轮次仍有排队或领取中的请求，不能强制移除"
    task = conn.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        return None
    current = task["run_id"] == run_id or task["session_marker"] == f"{task_id}/{run_id}"
    if not current:
        return None
    if task["send_state"] == "unknown":
        return "该轮次发送结果不明，不能强制移除"
    if task["status"] == "planning":
        # 规划请求有独立 run_id，tasks.run_id 可能还保留旧执行轮次。
        latest = conn.execute(
            "SELECT run_id FROM attempts WHERE task_id=? AND kind='plan' "
            "ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
        if latest is not None and latest["run_id"] != run_id:
            return None
        other = conn.execute(
            "SELECT 1 FROM requests WHERE task_id=? AND kind='plan' "
            "AND status IN ('queued','claimed') AND (run_id IS NULL OR run_id!=?) "
            "LIMIT 1", (task_id, run_id)
        ).fetchone()
        if other is not None:
            return None
    if task["status"] in (
        "planning", "dispatching", "prepared", "submitting", "running", "in_review",
    ):
        return "任务正在该轮次执行或评审，不能强制移除"
    return None
