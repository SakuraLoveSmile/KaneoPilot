"""SQLite state layer. Schema mirrors docs/state-model.md §1 exactly."""

from __future__ import annotations

import sqlite3
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
  extra_json TEXT
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
"""

DEFAULT_SETTINGS = {
    "paused_global": False,
    "paused_stages": [],
    "lease_seconds": constants.DEFAULT_LEASE_SECONDS,
    "monitor_interval_seconds": constants.DEFAULT_MONITOR_INTERVAL_SECONDS,
    "plan_scan_interval_hours": constants.DEFAULT_PLAN_SCAN_INTERVAL_HOURS,
    "plan_scan_anchor": constants.DEFAULT_PLAN_SCAN_ANCHOR,
    "workspace_id": constants.WORKSPACE_ID,
    "dispatch_serial": "",
    "lifecycle_owner": "",
}


def connect(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(version) VALUES (?)", (constants.SCHEMA_VERSION,)
        )
    elif int(row["version"]) != constants.SCHEMA_VERSION:
        raise util.KanpilotError(
            "migration_needed",
            f"schema version {row['version']} != {constants.SCHEMA_VERSION}",
        )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, util.jdump(value)),
        )


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
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


def update_task(conn: sqlite3.Connection, task_id: str, now: str, **fields: Any) -> None:
    if not fields:
        return
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
    existing = active_request_for_key(conn, idempotency_key)
    if existing is not None:
        return existing, False
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
