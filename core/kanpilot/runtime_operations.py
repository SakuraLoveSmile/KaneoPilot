"""Runtime operation receipts tracking for idempotent MCP control and task writes.

Protocol §5.25 / T7.4 / T7.7.
Ensures that lost responses or client retries do not trigger duplicate sessions,
prompts, claims, or reports.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional, Tuple

from . import db, util


def compute_request_sha256(payload: Any) -> str:
    """Canonical SHA-256 hash of the request arguments (excluding volatile tokens)."""
    blob = util.jdump(payload)
    return util.sha256_bytes(blob.encode("utf-8"))


def begin_operation(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
    harness_id: str,
    tool: str,
    payload: Any,
    now: Optional[str] = None,
) -> dict:
    """Register or look up an operation receipt.

    Returns dict with keys: status, result, session_id, turn_id, proposal_id.
    """
    if not operation_id or not isinstance(operation_id, str):
        raise util.bad_request("operation_id 必须为非空字符串")
    if not scope_id or not isinstance(scope_id, str):
        raise util.bad_request("scope_id 必须为非空字符串")

    now_iso = now or util.to_iso(util.now_dt())
    req_hash = compute_request_sha256(payload)
    existing = db.get_runtime_operation(conn, scope_id, operation_id)
    if existing is not None:
        if (existing["request_sha256"] != req_hash or existing["tool"] != tool
                or existing["harness_id"] != harness_id):
            raise util.KanpilotError(
                "conflict",
                f"operation_id '{operation_id}' 已被用于不同参数的调用，禁止复用",
            )
        res_data = None
        if existing["result_json"]:
            try:
                res_data = json.loads(existing["result_json"])
            except Exception:
                pass
        return {
            "status": existing["status"],
            "result": res_data,
            "session_id": existing["session_id"],
            "turn_id": existing["turn_id"],
            "proposal_id": existing["proposal_id"],
        }

    db.insert_runtime_operation(
        conn,
        scope_id=scope_id,
        operation_id=operation_id,
        harness_id=harness_id,
        tool=tool,
        request_sha256=req_hash,
        status="prepared",
        now=now_iso,
    )
    return {
        "status": "prepared",
        "result": None,
        "session_id": None,
        "turn_id": None,
        "proposal_id": None,
    }


def mark_operation_unknown(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
    now: Optional[str] = None,
) -> None:
    """Mark operation as unknown right before dispatching downstream."""
    now_iso = now or util.to_iso(util.now_dt())
    db.update_runtime_operation(
        conn,
        scope_id=scope_id,
        operation_id=operation_id,
        now=now_iso,
        status="unknown",
    )


def complete_operation(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
    status: str = "accepted",
    result: Any = None,
    result_data: Any = None,
    now: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
) -> None:
    """Record terminal result for an operation."""
    if status not in ("accepted", "rejected", "unknown", "prepared", "completed"):
        raise ValueError(f"Invalid operation status: {status}")
    now_iso = now or util.to_iso(util.now_dt())
    actual_res = result if result is not None else result_data
    db.update_runtime_operation(
        conn,
        scope_id=scope_id,
        operation_id=operation_id,
        now=now_iso,
        status="accepted" if status == "completed" else status,
        result_json=util.jdump(actual_res) if actual_res is not None else None,
        session_id=session_id,
        turn_id=turn_id,
        proposal_id=proposal_id,
    )


def fail_operation(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
    status: str = "rejected",
    result: Any = None,
    now: Optional[str] = None,
) -> None:
    """Record failed terminal result for an operation."""
    complete_operation(
        conn,
        scope_id=scope_id,
        operation_id=operation_id,
        status=status,
        result=result,
        now=now,
    )


def get_operation(
    conn: sqlite3.Connection,
    scope_id: str,
    operation_id: str,
) -> Optional[sqlite3.Row]:
    return db.get_runtime_operation(conn, scope_id, operation_id)


def reserve_downstream(conn, scope_id, operation_id, harness_id, tool, payload):
    """Exactly one caller acquires the send slot, with no lock across network IO."""
    with db.write_txn(conn):
        op = begin_operation(conn, scope_id, operation_id, harness_id, tool, payload)
        if op["status"] == "prepared":
            mark_operation_unknown(conn, scope_id, operation_id)
            return {**op, "send": True}
        return {**op, "send": False}


def session_owned(conn, scope_id, session_id):
    return conn.execute(
        "SELECT 1 FROM runtime_operations WHERE scope_id=? AND session_id=? "
        "AND tool='kp_runtime_session_create' AND status='accepted'",
        (scope_id, session_id),
    ).fetchone() is not None


def session_attached(conn, scope_id, session_id):
    return conn.execute(
        "SELECT 1 FROM runtime_operations WHERE scope_id=? AND session_id=? "
        "AND tool='kp_runtime_attach_mcp' AND status='accepted'",
        (scope_id, session_id),
    ).fetchone() is not None


def claim_gate(conn, scope_id, operation_id, payload):
    """Called inside each claim commit transaction, before any state mutation."""
    op = begin_operation(conn, scope_id, operation_id, "core", "kp_task_claim", payload)
    if op["status"] == "accepted":
        return op["result"]
    if op["status"] != "prepared":
        raise util.KanpilotError("conflict", "claim operation cannot be replayed")
    for row in conn.execute(
        "SELECT result_json FROM runtime_operations WHERE scope_id=? "
        "AND tool='kp_task_claim' AND status='accepted'", (scope_id,),
    ):
        result = util.jload(row["result_json"]) or {}
        if not result.get("claimed"):
            continue
        request = db.get_request(conn, result.get("request_id"))
        if request is not None and request["status"] == "claimed":
            raise util.KanpilotError("conflict", "scope already owns an active claim")
    return None
