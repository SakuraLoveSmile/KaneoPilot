"""Engine lease + status health (protocol §5.27.1 / §5.27.2).

The long-lived single-instance lease lives in ``settings.executor_instances``
(§5.27.1) — the same key the App scheduler and detached workers already use
(:mod:`kanpilot.process_owner`). We *add* the engine fields (``pid`` /
``stage`` / ``lease_expires_at``) to the entry; ``process_owner`` only reads
``state``/``started_at``/``workers``, so the extra fields are inert to it, and
worker liveness for ``owned_workers`` is delegated to ``process_owner``.

No schema change: this is a JSON settings value. Every write is a
``BEGIN IMMEDIATE`` read-modify-write via :func:`kanpilot.db.write_txn`.
"""
from __future__ import annotations

from typing import Optional

from .. import db, process_owner, util


def _instances(conn) -> dict:
    value = db.get_setting(conn, process_owner.SETTING, {})
    return value if isinstance(value, dict) else {}


def _is_engine_entry(entry) -> bool:
    return isinstance(entry, dict) and "lease_expires_at" in entry


def lease_seconds(conn) -> int:
    from .. import constants
    try:
        return int(db.get_setting(conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS))
    except (TypeError, ValueError):
        return int(constants.DEFAULT_LEASE_SECONDS)


def acquire_or_renew_lease(conn, now_iso, *, instance_id, stage, pid, lease_seconds) -> dict:
    """Create or renew this engine instance's lease (idempotent per instance_id).

    Single-instance: refuse if another *active, unexpired* engine instance
    holds the lease (an expired lease may be taken over).
    """
    now = util.parse_ts(now_iso) or util.now_dt()
    expires = util.plus_seconds(lease_seconds, now)
    with db.write_txn(conn):
        instances = _instances(conn)
        for other_id, other in instances.items():
            if other_id == instance_id or not _is_engine_entry(other):
                continue
            if (other.get("state") == "active"
                    and not util.lease_lost(other.get("lease_expires_at"), now)):
                raise util.KanpilotError(
                    "conflict", f"引擎租约由实例 {other_id} 持有，拒绝第二个长驻引擎")
        entry = dict(instances.get(instance_id) or {})
        entry.update(state="active", instance_id=instance_id, pid=pid, stage=stage,
                     lease_expires_at=expires,
                     started_at=entry.get("started_at") or now_iso)
        entry.setdefault("workers", [])
        instances[instance_id] = entry
        db.set_setting(conn, process_owner.SETTING, instances)
    return {"instance_id": instance_id, "lease_expires_at": expires}


def renew_lease(conn, now_iso, *, instance_id, lease_seconds) -> dict:
    now = util.parse_ts(now_iso) or util.now_dt()
    expires = util.plus_seconds(lease_seconds, now)
    with db.write_txn(conn):
        instances = _instances(conn)
        entry = instances.get(instance_id)
        if not entry or entry.get("state") != "active":
            raise util.KanpilotError("conflict", "引擎实例不存在或已关闭")
        instances[instance_id] = dict(entry, lease_expires_at=expires)
        db.set_setting(conn, process_owner.SETTING, instances)
    return {"instance_id": instance_id, "lease_expires_at": expires}


def release_lease(conn, now_iso, *, instance_id) -> dict:
    with db.write_txn(conn):
        instances = _instances(conn)
        entry = instances.get(instance_id)
        if not entry:
            return {"instance_id": instance_id, "released": False}
        instances[instance_id] = dict(
            entry, state="stopped", stopped_at=entry.get("stopped_at") or now_iso)
        db.set_setting(conn, process_owner.SETTING, instances)
    return {"instance_id": instance_id, "released": True}


def active_instance_count(conn, now) -> int:
    now = util.parse_ts(now) or util.now_dt()
    return sum(
        1 for entry in _instances(conn).values()
        if _is_engine_entry(entry) and entry.get("state") == "active"
        and not util.lease_lost(entry.get("lease_expires_at"), now)
    )


def engine_status(conn, now, instance_id: Optional[str] = None) -> dict:
    """``engine status`` (§5.27.2): current engine instances + live workers."""
    instances = _instances(conn)
    ids = [instance_id] if instance_id else list(instances.keys())
    out = []
    for iid in ids:
        entry = instances.get(iid)
        if not _is_engine_entry(entry):
            continue
        workers = process_owner.owned_workers(conn, iid)
        out.append({
            "instance_id": iid,
            "stage": entry.get("stage"),
            "pid": entry.get("pid"),
            "started_at": entry.get("started_at"),
            "lease_expires_at": entry.get("lease_expires_at"),
            "owned_workers": [
                {"pid": w.get("pid"), "started_seconds": w.get("started_seconds"),
                 "task_id": w.get("task_id"), "run_id": w.get("run_id"),
                 "spec_path": w.get("spec_path")}
                for w in workers
            ],
        })
    return {"instances": out}
