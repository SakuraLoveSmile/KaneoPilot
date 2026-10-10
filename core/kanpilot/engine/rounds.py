"""Read-only execution-round world probe (sandbox §13.2 兜底).

``execution_rounds.run_status`` is a *last observed* value: its only writers
are the worker's own收尾 (``pi_desktop_controller`` / ``desktop_queue`` write
``job.json`` at process exit; the claim path refreshes the index) plus the
read-layer backfill inside ``get-session``. When a worker exits normally but
the monitor's observation window closes **before** ``job.json`` lands, nobody
records the terminal state: the task stays in observe/running and
:func:`kanpilot.engine.decide.decide` can only AWAIT forever (sandbox evidence
§13.2 — monitor finished 20:14:00 vs ``job.json ended_at`` 20:14:30).

This module is the engine's read-only兜底 for exactly that shape. It reads the
round index row plus the identity-verified ``job.json``
(:func:`kanpilot.dsh_events.read_job` — verified by ``task_id``/``run_id``,
never by title) and reports whether the round has *actually* exited. The
reconciler turns that into a core-command enqueue; the deterministic monitor
stage then performs the completion (exit 0 → core-owned verify → in-review) or
the failure-ladder attribution (non-0 → ``needs_user``/``execution_failed``).
This module never writes: SQLite stays the single source of truth.

Out of scope by design: a round whose ``job.json`` never recorded a result
(pid gone, no ``ended_at``) is indistinguishable from "still starting up" and
belongs to the lease/request recovery path (D3), not here.
"""
from __future__ import annotations

from typing import Optional

from .. import constants, dsh_events, headless_adapter as headless, round_store, util

# Stored run_status values that already record a terminal outcome
# (constants.ROUND_RUN_STATUSES minus pending/running/unknown).
_TERMINAL_RUN_STATUSES = ("finished", "failed")


def round_exit(conn, task_id: str, run_id: Optional[str]) -> dict:
    """Read-only view of whether ``task_id/run_id`` has actually exited.

    Never raises for unreadable/missing records — an unavailable probe simply
    reports ``available=False`` with a ``reason`` (+ ``detail``) so the
    reconciler keeps waiting instead of guessing.

      available         — the round row **and** the identity-verified job.json
                          were read (i.e. the probe had something to say)
      reason/detail     — why not available (audit trail)
      run_status        — the *stored* index value (what is registered)
      terminal_recorded — a terminal outcome is already registered in the index
      pid/pid_alive     — round process identity + liveness (diagnostic only)
      ended_at/exit_code— job.json 进程结果（退出时才由 worker 写入）
      terminal          — terminal event view (events.jsonl) or None
      exited            — process gone **and** job.json recorded its result
    """
    view = {
        "available": False, "reason": None, "detail": None,
        "run_status": "unknown", "terminal_recorded": False,
        "pid": None, "pid_alive": False, "ended_at": None,
        "exit_code": None, "terminal": None, "exited": False,
    }
    if not run_id:
        view["reason"] = "no_run_id"
        return view
    row = round_store.get_round(conn, task_id, run_id)
    if row is None:
        view["reason"] = "no_round_row"
        return view
    stored = row["run_status"]
    view["run_status"] = stored if stored in constants.ROUND_RUN_STATUSES \
        else "unknown"
    # 已登记终态 = 没有「登记缺口」可补（幂等：不重复动作）。
    view["terminal_recorded"] = view["run_status"] in _TERMINAL_RUN_STATUSES
    source = headless.resolve_source(conn)
    try:
        directory = source.run_dir(task_id, run_id)
    except headless.HeadlessError as exc:
        view["reason"] = "run_dir_unavailable"
        view["detail"] = getattr(exc, "detail", None) or str(exc)
        return view
    if not directory.is_dir():
        view["reason"] = "run_dir_missing"
        return view
    job, error = dsh_events.read_job(directory, task_id, run_id, backend=None)
    if job is None:
        # 身份不符/半写入/缺失一律「不可采信」，不当成已退出。
        view["reason"] = "job_unreadable"
        view["detail"] = error
        return view
    events, _events_detail = dsh_events.iter_events(
        directory / dsh_events.EVENT_LOG_NAME
    )
    detail = dsh_events.round_detail(row, job, events) or {}
    pid = detail.get("pid")
    view["available"] = True
    view["pid"] = pid if isinstance(pid, int) and not isinstance(pid, bool) \
        else None
    view["pid_alive"] = util.pid_alive(view["pid"])
    view["ended_at"] = job.get("ended_at")
    exit_code = job.get("exit_code")
    view["exit_code"] = exit_code if isinstance(exit_code, int) \
        and not isinstance(exit_code, bool) else None
    view["terminal"] = detail.get("terminal")
    # 退出证据：进程已不在 **且** job.json 已落 ended_at/exit_code（worker 只在
    # 进程结束时写这两个字段，因此二者合起来才是「确实退出」的证明）。
    view["exited"] = bool(
        view["ended_at"] and view["exit_code"] is not None
        and not view["pid_alive"]
    )
    return view
