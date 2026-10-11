"""Engine lease + status health (protocol §5.27.1 / §5.27.2).

The long-lived single-instance lease lives in ``settings.executor_instances``
(§5.27.1) — the same key the App scheduler and detached workers already use
(:mod:`kanpilot.process_owner`). We *add* the engine fields (``pid`` /
``stage`` / ``lease_expires_at``) to the entry; ``process_owner`` only reads
``state``/``started_at``/``workers``, so the extra fields are inert to it, and
worker liveness for ``owned_workers`` is delegated to ``process_owner``.

Holder liveness (§5.27.1 失联持有者接管): the entry records the holder's
process birth (``started_seconds``/``started_microseconds`` — the same 口径 as
``process_owner`` worker registration). A second engine may take the lease
when the other entry is *expired* **or** when its recorded birth proves the
holder process is gone (``SIGKILL`` — the ``finally`` release never ran). A
taken-over entry is marked ``state=stopped`` with
``stopped_reason=holder_dead``. pid reuse cannot masquerade as a live holder
(birth must match exactly), and legacy entries without a birth stay
conservative: unverifiable ⇒ treated as alive, so only expiry frees them.

No schema change: this is a JSON settings value. Every write is a
``BEGIN IMMEDIATE`` read-modify-write via :func:`kanpilot.db.write_txn`.
"""
from __future__ import annotations

from typing import Optional

from .. import db, process_owner, util

# Entry keys that make up a holder's process birth (same 口径 as the
# ``process_owner`` worker registration, protocol §5.27.1).
_BIRTH_KEYS = ("started_seconds", "started_microseconds")


def _instances(conn) -> dict:
    value = db.get_setting(conn, process_owner.SETTING, {})
    return value if isinstance(value, dict) else {}


def _is_engine_entry(entry) -> bool:
    return isinstance(entry, dict) and "lease_expires_at" in entry


def _holder_alive(entry) -> bool:
    """Whether the entry's holder process can still be verified alive.

    pid existence alone proves nothing (pids get reused): the recorded birth
    must match the process that currently owns that pid. A recorded birth
    whose pid is gone — or now belongs to a different process — means the
    holder is dead. Entries **without** a birth (written before this rule, or
    on a platform where the birth is unreadable) cannot be verified, so they
    stay conservative and count as alive until their lease expires.
    """
    recorded = {key: entry.get(key) for key in _BIRTH_KEYS}
    if not all(type(value) is int for value in recorded.values()):
        return True
    pid = entry.get("pid")
    if type(pid) is not int:
        return True
    current = process_owner.process_birth(pid)
    return bool(current and all(current[key] == value
                                for key, value in recorded.items()))


def _entry_active(entry, now) -> bool:
    """Active engine entry: ``state=active``, unexpired, holder still alive.

    Shared by :func:`active_instance_count` and :func:`engine_status` so both
    report the same 口径 (§5.27.1: a dead holder is not an active instance).
    """
    return bool(
        _is_engine_entry(entry) and entry.get("state") == "active"
        and not util.lease_lost(entry.get("lease_expires_at"), now)
        and _holder_alive(entry)
    )


def lease_seconds(conn) -> int:
    from .. import constants
    try:
        return int(db.get_setting(conn, "lease_seconds", constants.DEFAULT_LEASE_SECONDS))
    except (TypeError, ValueError):
        return int(constants.DEFAULT_LEASE_SECONDS)


def acquire_or_renew_lease(conn, now_iso, *, instance_id, stage, pid, lease_seconds) -> dict:
    """Create or renew this engine instance's lease (idempotent per instance_id).

    Single-instance: refuse while another *active, unexpired, verifiably live*
    engine instance holds the lease (§5.27.1). Besides expiry, one thing frees
    the lease: the holder's recorded birth proving its process is gone (an
    engine killed by a signal never runs its ``finally`` release). The dead
    entry is then marked ``state=stopped`` / ``stopped_reason=holder_dead``
    before this instance takes over, so a killed engine can never block
    recovery until ``lease_expires_at``. Legacy entries without a birth cannot
    be verified and stay conservative — only their expiry frees them.
    """
    now = util.parse_ts(now_iso) or util.now_dt()
    expires = util.plus_seconds(lease_seconds, now)
    with db.write_txn(conn):
        instances = _instances(conn)
        for other_id, other in instances.items():
            if other_id == instance_id or not _is_engine_entry(other):
                continue
            if (other.get("state") != "active"
                    or util.lease_lost(other.get("lease_expires_at"), now)):
                continue
            if _holder_alive(other):
                raise util.KanpilotError(
                    "conflict", f"引擎租约由实例 {other_id} 持有，拒绝第二个长驻引擎")
            # 失联持有者（如被 SIGKILL、finally 未执行）：标记 stopped 后放行
            # 接管，不得因 lease_expires_at 未到就无限期拒绝恢复。
            instances[other_id] = dict(
                other, state="stopped",
                stopped_at=other.get("stopped_at") or now_iso,
                stopped_reason="holder_dead")
        entry = dict(instances.get(instance_id) or {})
        entry.update(state="active", instance_id=instance_id, pid=pid, stage=stage,
                     lease_expires_at=expires,
                     started_at=entry.get("started_at") or now_iso)
        # birth 是进程身份的一部分：pid 会被复用，只有它能在持有者死后识破
        # 「pid 还在但不是原来那个进程」。取不到 birth（不支持的平台）时条目
        # 不带 birth，后续接管按历史条目保守处理（只认过期）。
        birth = process_owner.process_birth(pid) if type(pid) is int else None
        if birth:
            entry.update(birth)
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
    return sum(1 for entry in _instances(conn).values() if _entry_active(entry, now))


def engine_status(conn, now, instance_id: Optional[str] = None) -> dict:
    """``engine status`` (§5.27.2): current engine instances + live workers.

    Every engine entry is listed (a dead holder must stay *visible* for
    diagnosis) with the same active/liveness 口径 as
    :func:`active_instance_count`: ``state`` is the stored value, while
    ``holder_alive``/``active`` are derived — an entry whose holder died
    without releasing reads ``state=active, holder_alive=false, active=false``.
    """
    now = util.parse_ts(now) or util.now_dt()
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
            "state": entry.get("state"),
            "holder_alive": _holder_alive(entry),
            "active": _entry_active(entry, now),
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
