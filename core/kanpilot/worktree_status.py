"""Read-only pool health projection; SQLite remains authoritative."""
from . import db, util


def summary(conn):
    result = {"free": 0, "leased": 0, "quarantined": 0, "unconfirmed": 0, "waiting": [], "slots": []}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worktree_slots'").fetchone():
        for row in conn.execute("SELECT * FROM worktree_slots ORDER BY repo_key,path"):
            slot = dict(row)
            state = slot["state"]
            if state in result:
                result[state] += 1
            uncertain = state == "leased" and (
                slot.get("owner_state") == "unknown"
                or slot.get("reason") in ("operation_unconfirmed", "owner_stop_unconfirmed"))
            if uncertain:
                result["unconfirmed"] += 1
            if state == "quarantined" or uncertain or (state == "leased" and slot.get("reason")):
                result["slots"].append({key: slot.get(key) for key in (
                    "slot_id", "path", "state", "holder_stage", "holder_id", "reason", "updated_at")})
    for row in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'worktree_pool_waiting:%'"):
        value = util.jload(row["value"])
        if not isinstance(value, dict):
            continue
        request = db.get_request(conn, value.get("request_id"))
        if request is not None and request["status"] == "queued":
            result["waiting"].append(value)
    return result
