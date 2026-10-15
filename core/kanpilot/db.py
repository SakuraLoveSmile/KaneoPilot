"""SQLite state layer. Schema mirrors docs/state-model.md §1 exactly."""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import constants, util

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,
  title TEXT,
  repository TEXT,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  kaneo_status TEXT,
  kaneo_checked_at TEXT,
  requirements_sha256 TEXT,
  source_sha256 TEXT,
  source_path TEXT,
  plan_path TEXT, plan_sha256 TEXT,
  plan_kind TEXT,
  plan_generated_at TEXT,
  planner_model TEXT, reasoning_effort TEXT,
  send_state TEXT,
  sent INTEGER,
  run_id TEXT,
  session_marker TEXT, session_id TEXT, session_title TEXT,
  dispatch_lease_expires_at TEXT,
  original_todo_slug TEXT,
  transition_started_at TEXT, transition_verified_at TEXT, sent_at TEXT,
  ui_evidence TEXT, send_evidence TEXT,
  review_report_path TEXT, review_report_sha256 TEXT,
  review_key TEXT, review_source_task_id TEXT, fix_task_id TEXT,
  failure_code TEXT, failure_reason TEXT, failure_stage TEXT,
  blocked_at TEXT, blocked_summary TEXT,
  lock_retained INTEGER DEFAULT 0,
  manual_recovery_required INTEGER DEFAULT 0,
  last_progress TEXT,
  priority INTEGER DEFAULT 0,
  resolution TEXT,
  migrated_from TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  extra_json TEXT,
  save_state TEXT NOT NULL DEFAULT 'active',
  saved_at TEXT,
  auto_advance_paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests(
  request_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(task_id),
  stage TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  not_before TEXT,
  payload_json TEXT,
  requested_by TEXT,
  run_id TEXT, claimed_by TEXT, claimed_at TEXT,
  heartbeat_at TEXT, lease_expires_at TEXT,
  result_json TEXT, finished_at TEXT,
  attempt_no INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS requests_active_key ON requests(idempotency_key)
  WHERE status IN ('queued','claimed');
CREATE INDEX IF NOT EXISTS requests_poll ON requests(stage, status, priority, created_at);

CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL, task_id TEXT, run_id TEXT,
  stage TEXT, kind TEXT,
  started_at TEXT, finished_at TEXT,
  outcome TEXT,
  send_state TEXT, evidence_json TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS attempts_task ON attempts(task_id, id);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, task_id TEXT, request_id TEXT,
  kind TEXT NOT NULL, actor TEXT, detail_json TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- v1.3: 会话本地读取位点（断线补齐用；消息正文以执行记录为准，
-- 本地不缓存全文）。见 protocol.md §5.15.5。
CREATE TABLE IF NOT EXISTS session_reads(
  session_key TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  cursor TEXT,
  last_seq INTEGER,
  last_revision INTEGER,
  state TEXT NOT NULL DEFAULT 'pending',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_reads_task ON session_reads(task_id, run_id);

-- v1.5 (protocol.md §5.16): 执行轮次索引。task_id+run_id 唯一定位一个执行
-- 轮次；会话保存状态与任务保存状态正交。回填永不改写 save_state/saved_at
-- （重复导入不得复活已删除会话）。
CREATE TABLE IF NOT EXISTS execution_rounds(
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  backend TEXT NOT NULL DEFAULT 'headless',
  session_id TEXT,
  session_title TEXT,
  run_status TEXT NOT NULL DEFAULT 'unknown',
  config_json TEXT,
  worktree_path TEXT,
  started_at TEXT,
  pid INTEGER,
  save_state TEXT NOT NULL DEFAULT 'active',
  saved_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, run_id)
);
CREATE INDEX IF NOT EXISTS execution_rounds_save ON execution_rounds(save_state, updated_at);

-- v2（protocol.md §5.22）：手动规划输入的不可变材料登记。源快照、补充说明、
-- 附件按 input_id 落盘为 planner_inputs/<input_id>/，本表只登记索引与哈希；
-- 请求 payload 存 manifest 副本，不新增附件表。
CREATE TABLE IF NOT EXISTS planner_inputs(
  input_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  requirements_sha256 TEXT,
  source_sha256 TEXT,
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS planner_inputs_task ON planner_inputs(task_id, created_at);

-- v5（protocol.md §5.25 / T7）：MCP 运行时控制/写操作收据表（防止响应丢失后下游重复执行）。
CREATE TABLE IF NOT EXISTS runtime_operations(
  scope_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  harness_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  session_id TEXT,
  turn_id TEXT,
  proposal_id TEXT,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (scope_id, operation_id)
);
CREATE INDEX IF NOT EXISTS runtime_operations_tool ON runtime_operations(harness_id, tool, status);
"""

DEFAULT_SETTINGS = {
    "worktree_pool_size": constants.DEFAULT_WORKTREE_POOL_SIZE,
    "paused_global": False,
    "paused_stages": [],
    "lease_seconds": constants.DEFAULT_LEASE_SECONDS,
    "monitor_interval_seconds": constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
    "plan_scan_interval_hours": constants.DEFAULT_PLAN_SCAN_INTERVAL_HOURS,
    "plan_scan_anchor": constants.DEFAULT_PLAN_SCAN_ANCHOR,
    "workspace_id": constants.WORKSPACE_ID,
    "dispatch_serial": "",
    "lifecycle_owner": "",
    "session_poll_seconds": constants.DEFAULT_SESSION_POLL_SECONDS,
    # v1.6 (protocol.md §5.18): pi 轮次超时秒数（空=未设置时取 constants 默认）。
    "pi_round_timeout_seconds": constants.DEFAULT_PI_ROUND_TIMEOUT_SECONDS,
    # v2（protocol.md §5.22）：规划材料根目录（空=未设置，取默认
    # <app_support>/planner_inputs）与 Codex 规划单轮超时（None=回退
    # executor_timeout_seconds）。
    "planner_inputs_root": "",
    "codex_timeout_seconds": None,
}


class Connection(sqlite3.Connection):
    """Per-call authorization checked after acquiring the SQLite write lock."""
    authorization_guard = None


def connect(db_path: Path, allow_upgrade: bool = False) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _init_schema(conn, allow_upgrade=allow_upgrade)
    except Exception:
        conn.close()
        raise
    return conn


def _init_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, util.jdump(value)),
        )


def _db_file(conn: sqlite3.Connection) -> Optional[Path]:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    try:
        raw = row["file"]
    except (IndexError, KeyError):  # pragma: no cover - defensive
        return None
    if not raw:
        return None
    return Path(raw)


def _backup_database(conn: sqlite3.Connection, tag: str) -> Optional[Path]:
    """Copy the live DB (+wal/shm) before a schema upgrade (state-model §1.1)."""
    path = _db_file(conn)
    if path is None or not path.exists():
        return None
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:  # pragma: no cover - defensive
        pass
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = constants.app_support() / "backups" / f"schema-{stamp}-{tag}"
    target.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(path) + suffix)
        if source.exists():
            shutil.copy2(source, target / source.name)
    return target


# Ordered upgrade steps: (from_version, to_version, apply). Each step runs in
# its own transaction after the pre-upgrade backup (state-model.md §1.1).
def _upgrade_v1_to_v2(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "save_state" not in columns:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN save_state TEXT NOT NULL DEFAULT 'active'"
        )
    if "saved_at" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN saved_at TEXT")
    if "auto_advance_paused" not in columns:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN auto_advance_paused "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _upgrade_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v1.5 (state-model §1.2): 新建执行轮次索引。历史数据不迁移——由读取层
    按 job.json 身份核验回填（protocol §5.16.1），回填永不改写 save_state。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS execution_rounds("
        "  task_id TEXT NOT NULL,"
        "  run_id TEXT NOT NULL,"
        "  backend TEXT NOT NULL DEFAULT 'headless',"
        "  session_id TEXT,"
        "  session_title TEXT,"
        "  run_status TEXT NOT NULL DEFAULT 'unknown',"
        "  config_json TEXT,"
        "  worktree_path TEXT,"
        "  started_at TEXT,"
        "  pid INTEGER,"
        "  save_state TEXT NOT NULL DEFAULT 'active',"
        "  saved_at TEXT,"
        "  created_at TEXT NOT NULL,"
        "  updated_at TEXT NOT NULL,"
        "  PRIMARY KEY (task_id, run_id))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS execution_rounds_save "
        "ON execution_rounds(save_state, updated_at)"
    )


def _upgrade_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v2（state-model §1.3，schema v4）：新建 planner_inputs 登记表；
    save_state 词表扩为含 purged 是读取层变化（无 DDL）；设置键默认值由
    _init_settings 补种（INSERT OR IGNORE）。历史数据不迁移、不重写。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS planner_inputs("
        "  input_id TEXT PRIMARY KEY,"
        "  task_id TEXT NOT NULL,"
        "  input_sha256 TEXT NOT NULL,"
        "  requirements_sha256 TEXT,"
        "  source_sha256 TEXT,"
        "  manifest_json TEXT NOT NULL,"
        "  created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS planner_inputs_task "
        "ON planner_inputs(task_id, created_at)"
    )

def _upgrade_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v5（protocol §5.25，schema v5）：新建 runtime_operations 操作收据表。
    保留现有所有任务、轮次、设置与材料数据，不迁移、不重写旧业务数据。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runtime_operations("
        "  scope_id TEXT NOT NULL,"
        "  operation_id TEXT NOT NULL,"
        "  harness_id TEXT NOT NULL,"
        "  tool TEXT NOT NULL,"
        "  request_sha256 TEXT NOT NULL,"
        "  status TEXT NOT NULL,"
        "  session_id TEXT,"
        "  turn_id TEXT,"
        "  proposal_id TEXT,"
        "  result_json TEXT,"
        "  created_at TEXT NOT NULL,"
        "  updated_at TEXT NOT NULL,"
        "  PRIMARY KEY (scope_id, operation_id))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS runtime_operations_tool "
        "ON runtime_operations(harness_id, tool, status)"
    )


WORKTREE_SLOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS worktree_slots(
  slot_id TEXT PRIMARY KEY,
  repo_key TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  repository TEXT NOT NULL,
  git_common_dir TEXT NOT NULL,
  worktrees_root TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('free','leased','quarantined')),
  holder_stage TEXT, holder_id TEXT, lease_token TEXT, sha TEXT,
  lease_expires_at TEXT, heartbeat_at TEXT, updated_at TEXT NOT NULL,
  owner_pid INTEGER, owner_state TEXT, session_id TEXT, turn_id TEXT,
  baseline_json TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS worktree_slots_repo ON worktree_slots(repo_key,state);
CREATE UNIQUE INDEX IF NOT EXISTS worktree_slots_holder ON worktree_slots(repo_key,holder_stage,holder_id)
  WHERE state='leased';
"""
SCHEMA += WORKTREE_SLOTS_SCHEMA


def _upgrade_v5_to_v6(conn: sqlite3.Connection) -> None:
    # executescript commits implicitly; keep migration inside its existing txn.
    for statement in WORKTREE_SLOTS_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


_UPGRADES = {
    (1, 2): _upgrade_v1_to_v2,
    (2, 3): _upgrade_v2_to_v3,
    (3, 4): _upgrade_v3_to_v4,
    (4, 5): _upgrade_v4_to_v5,
    (5, 6): _upgrade_v5_to_v6,
}

def _upgrade_schema(conn: sqlite3.Connection, version: int) -> None:
    if version > constants.SCHEMA_VERSION:
        raise util.KanpilotError(
            "migration_needed",
            f"schema version {version} is newer than supported "
            f"{constants.SCHEMA_VERSION}",
        )
    current = version
    while current < constants.SCHEMA_VERSION:
        step = _UPGRADES.get((current, current + 1))
        if step is None:
            raise util.KanpilotError(
                "migration_needed",
                f"no upgrade path from schema version {current}",
            )
        _backup_database(conn, f"v{current}-to-v{current + 1}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            step(conn)
            conn.execute(
                "UPDATE schema_meta SET version=?", (current + 1,)
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        current += 1


def _init_schema(conn: sqlite3.Connection, allow_upgrade: bool = False) -> None:
    table_row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if table_row is None:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
            raise util.KanpilotError("migration_needed", "existing database has no schema version; refusing initialization")
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta(version) VALUES (?)", (constants.SCHEMA_VERSION,)
            )
        _init_settings(conn)
        return

    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    if row is None:
        raise util.KanpilotError("migration_needed", "database schema version is missing")

    version = int(row["version"])
    if version > constants.SCHEMA_VERSION:
        raise util.KanpilotError(
            "migration_needed",
            f"schema version {version} is newer than supported {constants.SCHEMA_VERSION}",
        )
    if version < constants.SCHEMA_VERSION:
        if not allow_upgrade:
            raise util.KanpilotError(
                "migration_needed",
                f"database schema version is {version}, but {constants.SCHEMA_VERSION} is required; run 'kanpilot migrate'",
            )
        _upgrade_schema(conn, version)
        conn.executescript(SCHEMA)
    else:
        conn.executescript(SCHEMA)
    _init_settings(conn)


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        guard = getattr(conn, "authorization_guard", None)
        if guard is not None:
            guard(conn)
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- settings

def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return util.jload(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, util.jdump(value)),
    )


def paused(conn: sqlite3.Connection, stage: str) -> bool:
    if get_setting(conn, "paused_global", False):
        return True
    return stage in (get_setting(conn, "paused_stages", []) or [])


# ------------------------------------------------------------------- rows

def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def require_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    task = get_task(conn, task_id)
    if task is None:
        raise util.KanpilotError("not_found", f"task not found: {task_id}")
    return task


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE request_id=?", (request_id,)
    ).fetchone()


def task_fields(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def task_extra(row: sqlite3.Row) -> dict:
    extra = util.jload(row["extra_json"])
    return extra if isinstance(extra, dict) else {}


# ------------------------------------------------------------ save_state
# v1.3 (protocol.md §5.15.1): save_state is orthogonal to the lifecycle
# status; only the console moves tasks between active/archived/trashed.

def task_save_state(row: sqlite3.Row) -> str:
    try:
        value = row["save_state"]
    except (IndexError, KeyError):  # pragma: no cover - defensive
        return "active"
    return value or "active"


def task_is_active(row: sqlite3.Row) -> bool:
    return task_save_state(row) == "active"


def task_auto_paused(row: sqlite3.Row) -> bool:
    try:
        return bool(row["auto_advance_paused"])
    except (IndexError, KeyError):  # pragma: no cover - defensive
        return False


def task_schedulable(row: sqlite3.Row) -> bool:
    """Scheduling entry points (tick/claim/enqueue) skip anything else."""
    return task_is_active(row) and not task_auto_paused(row)


def cancel_queued_requests(
    conn: sqlite3.Connection, task_id: str, now: str
) -> list:
    """Cancel this task's queued requests inside the caller's write txn.

    Returns cancelled request ids. A request that turned `claimed` before the
    transaction started is not returned here (the SELECT only sees queued
    rows), so callers re-check preconditions inside the same transaction to
    avoid racing the executor (protocol.md §5.15.2).
    """
    rows = conn.execute(
        "SELECT * FROM requests WHERE task_id=? AND status='queued'",
        (task_id,),
    ).fetchall()
    cancelled = []
    for row in rows:
        update_request(
            conn, row["request_id"], now,
            status="cancelled", finished_at=now,
            result_json={"outcome": "cancelled", "reason": "task_saved"},
        )
        add_event(
            conn, now, "request_cancelled", task_id=task_id,
            request_id=row["request_id"], actor="user",
            detail={"reason": "task_saved", "kind": row["kind"]},
        )
        cancelled.append(row["request_id"])
    return cancelled


def update_task(conn: sqlite3.Connection, task_id: str, now: str, **fields: Any) -> None:
    if not fields:
        return
    from . import verification_checks
    verification_checks.require_not_running(conn, task_id)
    fields["updated_at"] = now
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = [
        util.jdump(value) if isinstance(value, (dict, list)) else value
        for value in fields.values()
    ]
    conn.execute(
        f"UPDATE tasks SET {assignments} WHERE task_id=?", (*values, task_id)
    )


def set_extra(
    conn: sqlite3.Connection, task_row: sqlite3.Row, now: str, **entries: Any
) -> None:
    extra = task_extra(task_row)
    extra.update(entries)
    update_task(conn, task_row["task_id"], now, extra_json=extra)


def update_request(
    conn: sqlite3.Connection, request_id: str, now: str, **fields: Any
) -> None:
    from . import verification_checks
    request = get_request(conn, request_id)
    if request is not None and request["task_id"]:
        verification_checks.require_not_running(conn, request["task_id"])
    fields["updated_at"] = now
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = [
        util.jdump(value) if isinstance(value, (dict, list)) else value
        for value in fields.values()
    ]
    conn.execute(
        f"UPDATE requests SET {assignments} WHERE request_id=?", (*values, request_id)
    )


def add_event(
    conn: sqlite3.Connection,
    now: str,
    kind: str,
    task_id: Optional[str] = None,
    request_id: Optional[str] = None,
    actor: Optional[str] = None,
    detail: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO events(at, task_id, request_id, kind, actor, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (now, task_id, request_id, kind, actor, util.jdump(detail) if detail is not None else None),
    )


def add_attempt(
    conn: sqlite3.Connection,
    request_id: Optional[str],
    task_id: Optional[str],
    run_id: Optional[str],
    stage: Optional[str],
    kind: Optional[str],
    started_at: Optional[str],
    outcome: str,
    finished_at: Optional[str] = None,
    send_state: Optional[str] = None,
    evidence: Any = None,
    note: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO attempts(request_id, task_id, run_id, stage, kind, "
        "started_at, finished_at, outcome, send_state, evidence_json, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            request_id, task_id, run_id, stage, kind, started_at, finished_at,
            outcome, send_state,
            util.jdump(evidence) if evidence is not None else None, note,
        ),
    )


def close_attempt(
    conn: sqlite3.Connection,
    request_id: str,
    run_id: Optional[str],
    finished_at: str,
    outcome: str,
    send_state: Optional[str] = None,
    evidence: Any = None,
    note: Optional[str] = None,
) -> None:
    """Close the open (outcome='claimed') attempt for request+run, or append."""
    row = conn.execute(
        "SELECT id FROM attempts WHERE request_id=? AND run_id IS ? "
        "AND outcome='claimed' ORDER BY id DESC LIMIT 1",
        (request_id, run_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO attempts(request_id, run_id, finished_at, outcome, "
            "send_state, evidence_json, note) VALUES (?,?,?,?,?,?,?)",
            (
                request_id, run_id, finished_at, outcome, send_state,
                util.jdump(evidence) if evidence is not None else None, note,
            ),
        )
        return
    conn.execute(
        "UPDATE attempts SET finished_at=?, outcome=?, send_state=?, "
        "evidence_json=COALESCE(?, evidence_json), note=COALESCE(?, note) "
        "WHERE id=?",
        (
            finished_at, outcome, send_state,
            util.jdump(evidence) if evidence is not None else None,
            note, row["id"],
        ),
    )


def active_request_for_key(
    conn: sqlite3.Connection, idempotency_key: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM requests WHERE idempotency_key=? "
        "AND status IN ('queued','claimed')",
        (idempotency_key,),
    ).fetchone()


def create_request(
    conn: sqlite3.Connection,
    now: str,
    kind: str,
    idempotency_key: str,
    task_id: Optional[str] = None,
    stage: Optional[str] = None,
    priority: int = 100,
    not_before: Optional[str] = None,
    payload: Any = None,
    requested_by: Optional[str] = None,
    emit_event: bool = True,
) -> tuple[sqlite3.Row, bool]:
    """Insert a queued request. Returns (row, created); dedup on active key."""
    if task_id:
        from . import verification_checks
        verification_checks.require_not_running(conn, task_id)
    existing = active_request_for_key(conn, idempotency_key)
    if existing is not None:
        return existing, False
    if kind == "review":
        from . import review_config
        payload = dict(payload or {})
        snapshot = review_config.queue_snapshot(conn)
        task = get_task(conn, task_id) if task_id else None
        payload.update(review_backend=snapshot["backend"], review_config=snapshot,
                       expected_execution_run_id=task["run_id"] if task else None,
                       plan_sha256=task["plan_sha256"] if task else None)
    request_id = util.new_uuid()
    conn.execute(
        "INSERT INTO requests(request_id, idempotency_key, task_id, stage, "
        "kind, status, priority, not_before, payload_json, requested_by, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            request_id, idempotency_key, task_id,
            stage or constants.KIND_STAGE[kind], kind, "queued", priority,
            not_before,
            util.jdump(payload) if payload is not None else None,
            requested_by, now, now,
        ),
    )
    if emit_event:
        add_event(
            conn, now, "enqueued", task_id=task_id, request_id=request_id,
            actor=requested_by,
            detail={"kind": kind, "idempotency_key": idempotency_key},
        )
    return get_request(conn, request_id), True


def request_payload(row: sqlite3.Row) -> dict:
    payload = util.jload(row["payload_json"])
    return payload if isinstance(payload, dict) else {}


def merge_request_payload(
    conn: sqlite3.Connection, request_id: str, now: str, **entries: Any
) -> dict:
    row = get_request(conn, request_id)
    payload = request_payload(row)
    payload.update(entries)
    update_request(conn, request_id, now, payload_json=payload)
    return payload


# ------------------------------------------------------- v2 planner_inputs

def insert_planner_input(
    conn: sqlite3.Connection,
    input_id: str,
    task_id: str,
    input_sha256: str,
    requirements_sha256: Optional[str],
    source_sha256: Optional[str],
    manifest: dict,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO planner_inputs(input_id, task_id, input_sha256, "
        "requirements_sha256, source_sha256, manifest_json, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            input_id, task_id, input_sha256, requirements_sha256,
            source_sha256, util.jdump(manifest), now,
        ),
    )


def get_planner_input(
    conn: sqlite3.Connection, input_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM planner_inputs WHERE input_id=?", (input_id,)
    ).fetchone()


def planner_input_manifest(row: sqlite3.Row) -> dict:
    manifest = util.jload(row["manifest_json"])
    return manifest if isinstance(manifest, dict) else {}


def task_is_purged(row: Optional[sqlite3.Row]) -> bool:
    return row is not None and task_save_state(row) == "purged"

# ------------------------------------------------------- v5 runtime_operations

def insert_runtime_operation(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
    harness_id: str,
    tool: str,
    request_sha256: str,
    status: str,
    now: str,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
    result_json: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO runtime_operations(scope_id, operation_id, harness_id, tool, "
        "request_sha256, status, session_id, turn_id, proposal_id, result_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            scope_id, operation_id, harness_id, tool, request_sha256,
            status, session_id, turn_id, proposal_id, result_json, now, now,
        ),
    )


def get_runtime_operation(
    conn: sqlite3.Connection, scope_id: str, operation_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runtime_operations WHERE scope_id=? AND operation_id=?",
        (scope_id, operation_id),
    ).fetchone()


def update_runtime_operation(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
    now: str,
    **fields: Any,
) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{col}=?" for col in fields)
    values = list(fields.values()) + [now, scope_id, operation_id]
    conn.execute(
        f"UPDATE runtime_operations SET {assignments}, updated_at=? "
        "WHERE scope_id=? AND operation_id=?",
        values,
    )
