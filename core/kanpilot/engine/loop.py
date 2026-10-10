"""Long-lived reconciler (protocol §5.27.1, A12 per-project engine_mode).

Each tick: read a snapshot of automation-managed tasks → :func:`decide.decide`
→ execute the forward intents from :mod:`kanpilot.engine.steps` through
``cli.run`` (so every write still goes through the core command surface).
Crash recovery is by lease + world probe: a lost request is only *derived*
lost (never auto-released), and external side effects are idempotent via
``runtime_operations`` inside the core commands themselves.

``engine_mode`` is resolved **per project** (strangler three-state, A12):
- ``live``   — execute the decided forward commands + intake + spawn.
- ``shadow`` — compute decisions + planned commands + ``explain()``, log them,
  write **nothing** (no intake, no spawn, no commands).
- ``legacy`` — the engine does not drive the task at all (filtered before
  ``decide``); the legacy scheduler链路 handles it.

A project's effective mode = its ``project_mappings.items[pid].automation.
engine_mode`` when set, else the process default (``engine run --mode``);
``force_mode`` makes every project use the process default (drills/debug). One
process can serve legacy + shadow + live projects simultaneously.

Single-instance lease + heartbeat + exponential backoff live in
:mod:`kanpilot.engine.health`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

from .. import cli, core, db, util
from . import decide, health, steps
from . import decide, health, ladder, steps
# Stages served by a spawned worker (runners.spawn). "plan" -> planner_runner.
STAGES_WITH_WORKERS = ("dispatch", "monitor", "review", "delivery", "plan")
# Valid per-project engine modes (state-model §strangler).
ENGINE_MODES = ("legacy", "shadow", "live")


def plan_spawns(queued_stages, live_stages, *, foreign_active: bool):
    """Which stages to spawn a worker for (pure).

    Spawn a stage only when it has queued work, no live runner already handles
    it, and no foreign (App) scheduler is driving workers (avoid double-spawn).
    """
    if foreign_active:
        return []
    return [s for s in STAGES_WITH_WORKERS
            if queued_stages.get(s, 0) > 0 and s not in (live_stages or set())]


class Reconciler:
    """Decision-driven scheduler over automation-managed tasks (per-project mode)."""

    def __init__(self, db_path, *, mode: str = "live",
                 executor_id: str = "kaneo-engine", log: Optional[Callable] = None,
                 run_command: Optional[Callable] = None,
                 enable_intake: bool = False, intake_interval_seconds: int = 300,
                 enable_spawn: bool = False, instance_id: Optional[str] = None,
                 spawn: Optional[Callable] = None, force_mode: bool = False):
        self.db_path = Path(db_path)
        self.mode = mode                      # process default engine_mode
        self.force_mode = force_mode          # True => every project uses self.mode
        self.executor_id = executor_id
        self.log = log or (lambda msg: None)
        self.run_command = run_command or (
            lambda command, payload: cli.run(command, payload, self.db_path))
        # Intake (adopting Kaneo To Do) is opt-in so scheduling-only tests stay
        # isolated; `engine run` enables it. Reuses auto_intake.run_once.
        self.enable_intake = enable_intake
        self.intake_interval_seconds = intake_interval_seconds
        self._last_intake = None
        # Optional worker spawn: the engine launches per-stage runners for
        # queued requests of live projects when no App scheduler drives them.
        self.enable_spawn = enable_spawn
        self.instance_id = instance_id
        self.spawn = spawn  # callable(stage, db_path, instance_id) -> handle|None

    # ------------------------------------------------------------- modes (A12)
    def _project_modes(self, conn) -> dict:
        """Effective engine_mode per project (entry-level config > process default).

        ``engine_mode`` lives at the mapping **entry** level
        (``items[pid].engine_mode``), not inside ``automation``:
        ``validate_automation`` rebuilds that sub-dict from a fixed key
        whitelist and would silently drop it.
        """
        from .. import project_config
        _, _, items, _ = project_config.load_raw_mappings(conn)
        modes = {}
        for pid in items:
            if self.force_mode:
                modes[pid] = self.mode
                continue
            entry = items.get(pid) or {}
            configured = entry.get("engine_mode")
            if configured not in ("legacy", "shadow", "live"):
                configured = (entry.get("automation") or {}).get("engine_mode")
            modes[pid] = (configured if configured in ("legacy", "shadow", "live")
                          else self.mode)
        return modes

    def _live_project_ids(self, conn, modes) -> set:
        """Live projects that are enabled + preflight-ready (for intake adoption)."""
        from .. import automation_policy
        live = set()
        for pid, mode in modes.items():
            if mode != "live":
                continue
            policy = automation_policy.policy_for(conn, pid)
            if not policy or not policy.get("enabled"):
                continue
            if automation_policy.automation_preflight(conn, pid):
                continue
            live.add(pid)
        return live

    # ------------------------------------------------------------- snapshot
    def _snapshot(self, conn, task, now) -> dict:
        from .. import automation_policy
        extra = db.task_extra(task)
        delivery = core.delivery_of(task)
        automation = core.automation_of(task)
        status = task["status"]
        phase = condition = None
        if status == "in_review":
            # Distinguish review vs delivery sub-phase for PR tasks.
            reviewing_done = (extra.get("review_conclusion") == "pass"
                              or delivery.get("pushed_sha"))
            if delivery.get("mode") == "pr" and (delivery.get("pr_number") or reviewing_done):
                phase, condition = "delivery", "syncing"
            else:
                phase, condition = "review", "reviewing"
        policy = automation_policy.policy_for(conn, task["project_id"]) or {}
        budget = core.automation_budget(task, policy, now)
        paused_global = bool(db.get_setting(conn, "paused_global", False))
        return {
            "task_id": task["task_id"],
            "status": status,
            "send_state": task["send_state"],
            "phase": phase, "condition": condition,
            "fix_round": automation.get("fix_round", 0),
            "fix_open": bool(extra.get("fix_request")),
            "automation_managed": core.is_automation_managed(task),
            "manual_takeover": automation.get("manual_takeover", False),
            "paused": paused_global,
            "budget_ok": budget.get("ok", True),
            # Capacity is enforced at intake adoption + the dispatch claim
            # serial; an already-adopted task already holds its slot.
            "capacity_ok": True,
            "failure_code": task["failure_code"],
            "failure_stage": task["failure_stage"],
            "failure_reason": task["failure_reason"],
            "plan_sha256": task["plan_sha256"],
            "plan_execution_hold": bool(extra.get("plan_execution_hold")),
        }

    def _candidate_tasks(self, conn):
        rows = conn.execute(
            "SELECT * FROM tasks WHERE save_state='active' AND status != 'done' "
            "ORDER BY updated_at").fetchall()
        return [r for r in rows if core.is_automation_managed(r)]

    @staticmethod
    def _has_active_request(conn, task_id, kind) -> bool:
        return conn.execute(
            "SELECT 1 FROM requests WHERE task_id=? AND kind=? "
            "AND status IN ('queued','claimed') LIMIT 1",
            (task_id, kind)).fetchone() is not None

    def _transient_retry(self, conn, task, now):
        """Ladder-1: build a backoff re-enqueue decision for a transiently-failed
        request (state-model §8: ≤2/stage, then escalate). Returns None when there
        is nothing to retry, the failure is not transient, a same-kind request is
        already active, the retry budget is spent (escalate → the failed request
        surfaces via attention), or a dispatch prompt was already sent/unknown.
        """
        row = conn.execute(
            "SELECT kind, attempt_no, result_json FROM requests "
            "WHERE task_id=? AND status='failed' ORDER BY updated_at DESC LIMIT 1",
            (task["task_id"],)).fetchone()
        if row is None:
            return None
        code = (util.jload(row["result_json"]) or {}).get("failure_code")
        if ladder.classify(code) != ladder.LADDER_TRANSIENT:
            return None
        kind = row["kind"]
        if kind == "dispatch" and task["send_state"] in ("sent", "unknown"):
            return None
        if self._has_active_request(conn, task["task_id"], kind):
            return None
        attempt_no = row["attempt_no"] or 0
        fd = decide.failure_decision(code, attempt_no=attempt_no)
        if fd.action != decide.RETRY:
            return None
        not_before = util.plus_seconds(fd.params["backoff_seconds"], now)
        return decide.Decision(
            decide.RETRY, reason=code or "transient", ladder=ladder.LADDER_TRANSIENT,
            params={"kind": kind, "not_before": not_before,
                    "attempt_no": fd.params["attempt_no"]})

    # ------------------------------------------------------------- reconcile
    def reconcile_once(self, now=None) -> dict:
        now = now or util.now_dt()
        now_iso = util.to_iso(now)
        conn = db.connect(self.db_path)
        actions = []
        try:
            modes = self._project_modes(conn)
            for task in self._candidate_tasks(conn):
                pmode = modes.get(task["project_id"], self.mode)
                if pmode == "legacy":
                    # The engine does not drive legacy projects at all.
                    continue
                snap = self._snapshot(conn, task, now)
                decision = self._transient_retry(conn, task, now) or decide.decide(snap)
                base = {"task_id": task["task_id"], "action": decision.action,
                        "reason": decision.reason, "ladder": decision.ladder,
                        "engine_mode": pmode, "explain": decide.explain(snap, decision)}
                calls = steps.commands_for(decision, snap)
                if pmode == "shadow":
                    # Compute + log only; never write (no commands/intake/spawn).
                    if calls:
                        for call in calls:
                            actions.append(dict(base, command=call.command,
                                                executed=False, payload=call.payload))
                    else:
                        actions.append(dict(base, command=None))
                    continue
                # live: execute forward commands.
                if not calls:
                    actions.append(dict(base, command=None))
                    continue
                for call in calls:
                    record = dict(base, command=call.command)
                    if call.requires_hold and not snap.get("plan_execution_hold"):
                        actions.append(dict(record, skipped="already_confirmed"))
                        continue
                    if call.kind and self._has_active_request(conn, task["task_id"], call.kind):
                        actions.append(dict(record, skipped="already_scheduled"))
                        continue
                    try:
                        result = self.run_command(call.command, call.payload)
                        actions.append(dict(record, executed=True,
                                            deduplicated=bool(result.get("deduplicated"))))
                    except util.KanpilotError as exc:
                        actions.append(dict(record, error=exc.code))

            if self.enable_intake and self._intake_due(now):
                actions.append(self.intake_step(conn, now, modes))

            if self.enable_spawn:
                actions.append(self.spawn_step(conn, modes))
        finally:
            conn.close()
        return {"mode": self.mode, "project_modes": modes,
                "at": now_iso, "actions": actions}

    def _intake_due(self, now) -> bool:
        if self._last_intake is None:
            return True
        return (now - self._last_intake).total_seconds() >= self.intake_interval_seconds

    def intake_step(self, conn, now, modes) -> dict:
        """Adopt Kaneo To Do for LIVE projects only (reuses auto_intake; not rewritten)."""
        from .. import auto_intake
        live_pids = self._live_project_ids(conn, modes)
        self._last_intake = now
        if not live_pids:
            return {"action": "intake", "executed": False,
                    "reason": "no_live_projects", "adopted": 0,
                    "projects": 0, "errors": []}
        summary = auto_intake.run_once(self.db_path, dry_run=False,
                                       now=util.to_iso(now), project_ids=live_pids)
        adopted = sum(len(p.get("adopted") or []) for p in summary.get("projects", []))
        return {"action": "intake", "executed": True, "adopted": adopted,
                "projects": len(summary.get("projects", [])),
                "errors": summary.get("errors", [])}

    def _live_runner_stages(self, conn):
        """Stages with a live runner subprocess (executor_runs pids)."""
        runs = db.get_setting(conn, "executor_runs") or {}
        return {stage for stage, run in runs.items()
                if isinstance(run, dict) and util.pid_alive(run.get("pid"))}

    def _queued_stages(self, conn, modes):
        """Queued-request counts per stage, restricted to LIVE projects' tasks."""
        live = {pid for pid, mode in modes.items() if mode == "live"}
        if not live:
            return {}
        placeholders = ",".join("?" * len(live))
        counts = {}
        for row in conn.execute(
                "SELECT r.stage AS stage, COUNT(*) AS n FROM requests r "
                "JOIN tasks t ON t.task_id = r.task_id "
                f"WHERE r.status='queued' AND t.project_id IN ({placeholders}) "
                "GROUP BY r.stage", tuple(live)):
            counts[row["stage"]] = row["n"]
        return counts

    @staticmethod
    def foreign_scheduler_active(conn) -> bool:
        """True when an App scheduler (a non-engine executor_instances entry) is active.

        Engine lease entries carry ``lease_expires_at``; the App's registered
        instance does not. A live App instance means the engine must not also
        spawn workers (avoid double-drive).
        """
        from .. import process_owner
        instances = db.get_setting(conn, process_owner.SETTING, {}) or {}
        return any(isinstance(entry, dict) and entry.get("state") == "active"
                   and "lease_expires_at" not in entry
                   for entry in instances.values())

    def _default_spawn(self, stage, db_path, instance_id):
        from . import runners
        return runners.spawn(stage, db_path, instance_id=instance_id)

    def spawn_step(self, conn, modes) -> dict:
        """Spawn per-stage workers for queued requests of live projects (no App scheduler)."""
        if self.foreign_scheduler_active(conn):
            return {"action": "spawn", "executed": False,
                    "reason": "app_scheduler_active", "spawned": []}
        queued = self._queued_stages(conn, modes)
        live = self._live_runner_stages(conn)
        stages = plan_spawns(queued, live, foreign_active=False)
        spawner = self.spawn or self._default_spawn
        spawned = []
        for stage in stages:
            spawner(stage, self.db_path, self.instance_id)
            spawned.append(stage)
            self.log("engine spawned worker: " + stage)
        return {"action": "spawn", "executed": True, "spawned": spawned}

    # ------------------------------------------------------------- long-lived
    def run(self, *, instance_id=None, stage=None, lease_seconds=None,
            stop_event=None, max_ticks=None, sleeper=None) -> dict:
        """Long-lived loop: lease → tick+heartbeat → backoff → release (§5.27.1)."""
        instance_id = instance_id or util.new_uuid()
        self.instance_id = instance_id
        pid = os.getpid()
        sleeper = sleeper or time.sleep
        conn = db.connect(self.db_path)
        try:
            lease = lease_seconds or health.lease_seconds(conn)
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=instance_id, stage=stage,
                pid=pid, lease_seconds=lease)
        finally:
            conn.close()
        interval = min(lease / 3.0, 60.0)
        backoff, failures, ticks = 1.0, 0, 0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if max_ticks is not None and ticks >= max_ticks:
                    break
                ticks += 1
                try:
                    summary = self.reconcile_once()
                    self.log("engine tick %d: %d action(s)" % (ticks, len(summary["actions"])))
                    failures, backoff = 0, 1.0
                    beat = db.connect(self.db_path)
                    try:
                        health.renew_lease(beat, util.now_iso(), instance_id=instance_id,
                                           lease_seconds=lease)
                    finally:
                        beat.close()
                    sleeper(interval)
                except Exception as exc:  # noqa: BLE001 — backoff on any tick failure
                    failures += 1
                    backoff = min(60.0, float(2 ** (failures - 1)))
                    self.log("engine tick %d failed (#%d): %s; backoff %.0fs"
                             % (ticks, failures, exc, backoff))
                    sleeper(backoff)
        finally:
            done = db.connect(self.db_path)
            try:
                health.release_lease(done, util.now_iso(), instance_id=instance_id)
            finally:
                done.close()
        return {"instance_id": instance_id, "ticks": ticks, "failures": failures}
