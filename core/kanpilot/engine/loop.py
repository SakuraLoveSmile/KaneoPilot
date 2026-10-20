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

from .. import cli, core, db, derive, util
from . import decide, fallback, health, ladder, rounds, steps
# Stages served by a spawned worker (runners.spawn). "plan" -> planner_runner.
STAGES_WITH_WORKERS = ("dispatch", "monitor", "review", "delivery", "plan")
# Valid per-project engine modes (state-model §strangler).
ENGINE_MODES = ("legacy", "shadow", "live")
# Task-wide remediation accounting, retained across replans (at most two).
FIX_ROUNDS_KEY = "engine_fix_rounds"
# extra key where the engine records the rounds it settled after a missed
# terminal-state registration (sandbox §13.2). Bounded per run: a monitor
# stage that keeps reporting ``succeeded`` without advancing the task must
# never spin the engine into endless re-enqueues.
ROUND_SETTLE_KEY = "engine_round_settle"
MAX_ROUND_SETTLE = 2
# Stages whose spawned workers perform Kaneo remote writes under the global
# lifecycle serial (``core.acquire_lifecycle``, state-model §5):
#   dispatch → ``dispatch-begin-send`` (owner ``begin-send:<run_id>``)
#   monitor  → ``advance-task`` from ``auto_monitor._advance``
#   delivery → ``advance-task`` from ``delivery_runner`` (merge → done)
# ``plan`` and ``review`` do not take the serial (register-review is local).
LIFECYCLE_SERIAL_STAGES = frozenset({"dispatch", "monitor", "delivery"})


def plan_spawns(queued_stages, live_stages, *, foreign_active: bool,
                lifecycle_busy: bool = False):
    """Which stages to spawn a worker for (pure).

    Spawn a stage only when it has queued work, no live runner already handles
    it, and no foreign (App) scheduler is driving workers (avoid double-spawn).

    ``lifecycle_busy`` additionally defers :data:`LIFECYCLE_SERIAL_STAGES`:
    starting one of them while another owner holds the serial loses the race,
    and the loser is recorded as a *definite* outcome even though nothing was
    written — dispatch becomes ``send_state=unknown`` (protocol: after
    begin-send, always unknown) and the task lands in needs_user for a send
    that never happened (sandbox evidence §4.1: tasks 2/4/5 stuck). Deferring is
    safe: the request stays queued and the next tick retries.
    """
    if foreign_active:
        return []
    return [s for s in STAGES_WITH_WORKERS
            if queued_stages.get(s, 0) > 0 and s not in (live_stages or set())
            and not (lifecycle_busy and s in LIFECYCLE_SERIAL_STAGES)]


class Reconciler:
    """Decision-driven scheduler over automation-managed tasks (per-project mode)."""

    def __init__(self, db_path, *, mode: str = "live",
                 executor_id: str = "kaneo-engine", log: Optional[Callable] = None,
                 run_command: Optional[Callable] = None,
                 enable_intake: bool = False, intake_interval_seconds: int = 300,
                 enable_spawn: bool = False, enable_tick: bool = False,
                 instance_id: Optional[str] = None,
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
        self.enable_tick = enable_tick
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
            repair = core.review_repair_of(task)
            if repair.get("state") == "held":
                phase, condition = "review", "repairing"
            elif repair.get("state") == "awaiting_review":
                phase, condition = "review", "reviewing"
            elif delivery.get("mode") == "pr" and (delivery.get("pr_number") or reviewing_done):
                phase, condition = "delivery", "syncing"
            else:
                phase, condition = "review", "reviewing"
        policy = automation_policy.policy_for(conn, task["project_id"]) or {}
        budget = core.automation_budget(task, policy, now)
        paused_global = bool(db.get_setting(conn, "paused_global", False))
        from ..execution_questions import view as question_view
        return {
            "task_id": task["task_id"],
            "status": status,
            "send_state": task["send_state"],
            "phase": phase, "condition": condition,
            "fix_round": automation.get("fix_round", 0),
            "fix_open": (extra.get("fix_request") or {}).get("state") in ("held", "open"),
            "review_repair": core.review_repair_of(task),
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
            "execution_run_id": task["run_id"],
            "run_id":task["run_id"],
            "execution_question":question_view(task,now,conn),
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
            "SELECT kind, payload_json, result_json FROM requests "
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
        # 「≤2/阶段」必须**跨请求累积**：commands_for(RETRY) 重入队的是新请求，
        # 它的 attempt_no 从零开始，所以已花费的重试次数随 payload 的 retry_base
        # 传递。只读失败请求自身的 attempt_no 会让上限永远达不到（无限退避）。
        base = db.request_payload(row).get("retry_base")
        spent = base if isinstance(base, int) and base >= 0 else 0
        fd = decide.failure_decision(code, attempt_no=spent)
        if fd.action != decide.RETRY:
            return None
        not_before = util.plus_seconds(fd.params["backoff_seconds"], now)
        return decide.Decision(
            decide.RETRY, reason=code or "transient", ladder=ladder.LADDER_TRANSIENT,
            params={"kind": kind, "not_before": not_before,
                    "attempt_no": fd.params["attempt_no"],
                    "backoff_seconds": fd.params["backoff_seconds"],
                    "retry_base": fd.params["attempt_no"]})

    # ------------------------------------------------------------- quality fix
    def _project_fallback(self, conn, project_id):
        """Project-configured fallback model (``{provider_id, model_id}``) or None."""
        from .. import project_config
        _, _, items, _ = project_config.load_raw_mappings(conn)
        return fallback.configured_model(items.get(project_id) or {})

    @staticmethod
    def _engine_fix_rounds(task, *, include_reverted=False) -> list:
        """Remediation rounds the engine already armed for this task (per plan)."""
        value = db.task_extra(task).get(FIX_ROUNDS_KEY)
        return [row for row in value if include_reverted or not row.get("reverted_at")] if isinstance(value, list) else []

    def _clear_consumed_override(self, conn, task, now):
        """Drop the one-shot model override once a new execution round started.

        The override is armed for exactly one retry round (scenarios.md #5); when
        the task moves on to another run it must not silently apply to an
        unrelated round.
        """
        extra = db.task_extra(task)
        if not fallback.read_override(extra):
            return
        receipt = extra.get("engine_verdict") or {}
        run_id = task["run_id"]
        if not run_id or run_id == receipt.get("execution_run_id"):
            return
        with db.write_txn(conn):
            fresh = db.require_task(conn, task["task_id"])
            db.set_extra(conn, fresh, now, **{fallback.MODEL_OVERRIDE_KEY: None})

    def _quality_retry(self, conn, task, now, snap, pmode):
        """Choose a bounded new round; resolve commits accounting + enqueue atomically."""
        from . import verdict
        extra = db.task_extra(task)
        if core.review_repair_of(task).get("source_kind") == "merge_conflict":
            return None
        receipt = extra.get("engine_verdict")
        if (task["status"] != "needs_user" or task["failure_code"] != "verification_unconfirmed"
                or snap.get("paused") or snap.get("manual_takeover")
                or not isinstance(receipt, dict) or receipt.get("verification_ok")
                or receipt.get("environment_failure") or verdict.environment_reasons(receipt.get("checks"))
                or receipt.get("execution_run_id") != task["run_id"]
                or receipt.get("plan_sha256") != task["plan_sha256"]
                or not receipt.get("binding")
                or receipt.get("kind") not in ("empty_diff", "verify_failed")):
            return None
        rounds = self._engine_fix_rounds(task)
        if any(row.get("execution_run_id") == task["run_id"] for row in self._engine_fix_rounds(task, include_reverted=True)):
            return None
        if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                        (task["task_id"],)).fetchone():
            return None
        reason = receipt["kind"]
        configured = self._project_fallback(conn, task["project_id"]) if reason == "empty_diff" else None
        fd = decide.failure_decision("empty_diff" if reason == "empty_diff" else "verification_unconfirmed",
                                     fix_round=len(rounds), budget_ok=snap.get("budget_ok", True),
                                     fallback_model=configured)
        block = None
        if reason == "verify_failed" and not core.same_branch_fix_ready(task, receipt["binding"])[0]:
            block = "fix_branch_unavailable"
        elif fd.action != decide.START_FIX_ROUND:
            block = fd.reason
        elif reason == "empty_diff" and any(row.get("reason") == "empty_diff"
             and row.get("plan_sha256") == task["plan_sha256"] for row in rounds):
            # The same confirmed plan is retried at most once; verify repairs
            # retain the separate task-wide <=2 accounting.
            block = "fix_rounds_exhausted"
        plan = {"model": "primary", "snapshot": None, "validation_failed": False,
                "events": [], "probe_detail": ""}
        if block is None and reason == "empty_diff":
            primary, _error = fallback.primary_snapshot(conn) if configured else (None, None)
            plan = fallback.plan_retry(configured, primary=primary,
                                       connection_file=(primary or {}).get("connection_file"))
            if plan["validation_failed"]:
                block = "validation_failed"
        fix = {"execution_run_id": task["run_id"], "plan_sha256": task["plan_sha256"],
               "reason": reason, "plan": plan}
        if block:
            fix["block"] = block
            return decide.Decision(decide.BLOCK, reason=block,
                ladder=ladder.classify(block), attention=True, params={"engine_fix": fix})
        return decide.Decision(decide.START_FIX_ROUND, reason=reason, ladder=ladder.LADDER_QUALITY,
            params=dict(fd.params, engine_fix=fix, probe_detail=plan["probe_detail"]))

    # ------------------------------------------- round terminal fallback
    @staticmethod
    def _round_settles(task) -> list:
        """Rounds the engine already settled for this task (§13.2 receipts)."""
        value = db.task_extra(task).get(ROUND_SETTLE_KEY)
        return value if isinstance(value, list) else []

    def _round_settle(self, conn, task, now, snap, pmode):
        """Pre-decide world probe: settle an exited round still in observe/running
        (protocol §5.27.15; sandbox §13.2).

        ``execution_rounds.run_status`` is a *last observed* value whose only
        writers are the worker's own收尾 and the read-layer backfill inside
        ``get-session``. Neither writer schedules a monitor, so even a terminal
        observation can leave the task in observe/running indefinitely.
        This probe supplies the missing monitor for that shape —

        1. the task is still in observe/running (a task that moved on is not
           our business);
        2. no queued/claimed request exists (a worker owns the step — probing
           would race it, and the enqueue dedups on the active key anyway);
        3. ``run_status`` is diagnostic only; the task leaving observe/running
           is the signal that the round has been settled;
        4. the identity-verified ``job.json`` says the process is gone and
           recorded ``ended_at``/``exit_code`` (otherwise keep waiting — an
           unproven exit belongs to lease recovery, D3).

        It then enqueues one monitor request per bounded budget
        (:data:`MAX_ROUND_SETTLE`) and lets the deterministic monitor stage do
        the completion (exit 0) or the failure-ladder attribution (non-0).
        The engine itself never writes round state; SQLite stays the single
        source of truth. Shadow computes and logs the same decision, writes
        nothing (including the budget receipt).
        """
        resolved = decide.phase_condition(
            snap.get("status"), phase=snap.get("phase"),
            condition=snap.get("condition"),
            fix_round=int(snap.get("fix_round") or 0),
            fix_open=bool(snap.get("fix_open")))
        if resolved != ("observe", "running"):
            return None
        if derive.active_request_for_task(conn, task["task_id"]) is not None:
            return None
        run_id = task["run_id"]
        world = rounds.round_exit(conn, task["task_id"], run_id)
        if not world.get("exited"):
            return None
        decision = decide.round_settled_decision(world["exit_code"])
        spent = len([row for row in self._round_settles(task)
                     if row.get("run_id") == run_id])
        if spent >= MAX_ROUND_SETTLE:
            # 自动额度已用尽：不再入队，交人工（引擎不写 needs_user，只把
            # 「需人工处理」落进行动日志/影子差异，避免无限重入队）。
            return decide.Decision(
                decide.BLOCK, reason="round_settle_exhausted",
                ladder=ladder.classify("execution_failed"), attention=True,
                params=dict(decision.params, run_id=run_id, spent=spent))
        if pmode == "live":
            now_iso = util.to_iso(now)
            with db.write_txn(conn):
                fresh = db.require_task(conn, task["task_id"])
                entries = list(self._round_settles(fresh))
                entries.append({"run_id": run_id,
                                "exit_code": world["exit_code"],
                                "pid": world.get("pid"),
                                "ended_at": world.get("ended_at"),
                                "at": now_iso})
                # 一次 set_extra 写全：分两次调用会用陈旧快照覆盖前一次写入。
                db.set_extra(conn, fresh, now_iso,
                             **{ROUND_SETTLE_KEY: entries})
        return decision

    # ------------------------------------------------------------- reconcile
    def _schedule_engine_plan(self, conn, task_id, now):
        """Prepare only this authorized repair; avoid the broad intake interval."""
        from .. import auto_intake, automation_policy
        task = db.require_task(conn, task_id)
        if (task["status"] != "needs_plan" or not self._engine_fix_rounds(task)
                or not core.automation_of(task).get("plan_wanted")
                or self._has_active_request(conn, task_id, "plan")):
            return None
        policy = automation_policy.policy_for(conn, task["project_id"])
        if not policy or not policy.get("enabled"):
            return None
        issues = automation_policy.automation_preflight(conn, task["project_id"])
        if issues:
            return {"task_id": task_id, "reason": issues[0]["code"]}
        return auto_intake._submit(conn, self.db_path, task_id, policy, now,
                                   task["requirements_sha256"], wanted=True)

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
                if pmode == "live":
                    # A consumed one-shot model override must not leak into a
                    # later, unrelated round of the same task.
                    self._clear_consumed_override(conn, task, now)
                snap = self._snapshot(conn, task, now)
                if (snap.get("execution_question") or {}).get("state")=="open":
                    # Human input owns this round. Historical failed requests
                    # cannot take priority over its BLOCK/observer recovery.
                    decision=decide.decide(snap)
                else:
                    decision = (self._transient_retry(conn, task, now)
                                or self._quality_retry(conn, task, now, snap, pmode)
                                or self._round_settle(conn, task, now, snap, pmode)
                                or decide.decide(snap))
                base = {"task_id": task["task_id"], "action": decision.action,
                        "reason": decision.reason, "ladder": decision.ladder,
                        "attention": decision.attention,
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
                    if decision.action == decide.ENQUEUE_PLAN:
                        submitted = self._schedule_engine_plan(conn, task["task_id"], now_iso)
                        if submitted is not None:
                            actions.append(dict(base, command="start-plans", executed=bool(submitted.get("request_id")), result=submitted))
                            continue
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
                        from .. import engine_claim_scope
                        with engine_claim_scope.command_mode(self.mode, self.force_mode):
                            result = self.run_command(call.command, call.payload)
                        actions.append(dict(record, executed=True,
                                            deduplicated=bool(result.get("deduplicated"))))
                        if call.command == "resolve" and result.get("task_status") == "needs_plan" and call.payload.get("engine_fix"):
                            submitted = self._schedule_engine_plan(conn, task["task_id"], now_iso)
                            if submitted is not None:
                                actions.append({"task_id": task["task_id"], "action": decide.ENQUEUE_PLAN,
                                    "engine_mode": pmode, "command": "start-plans", "executed": bool(submitted.get("request_id")), "result": submitted})
                    except util.KanpilotError as exc:
                        actions.append(dict(record, error=exc.code, detail=util.redact(exc.message)))

            if self.enable_intake and self._intake_due(now):
                actions.append(self.intake_step(conn, now, modes))

            if self.enable_tick:
                actions.append(self.business_tick_step(conn, modes))

            if self.enable_spawn:
                actions.append(self.spawn_step(conn, modes))
        finally:
            conn.close()
        return {"mode": self.mode, "project_modes": modes,
                "at": now_iso, "actions": actions}

    def business_tick_step(self, conn, modes) -> dict:
        """Top up live-project observations before spawning (§5.27.15).

        Scope reads, command execution and result processing are all isolated:
        a failed business tick must not interrupt spawn or the engine heartbeat.
        Core commands own their transactions; do not hold a write txn here.
        """
        action = {"action": "business_tick", "executed": False}
        try:
            candidates = {row["project_id"] for row in conn.execute(
                "SELECT DISTINCT project_id FROM tasks "
                "WHERE save_state='active' AND status!='done'").fetchall()}
            # Explicit historical releases have no unfinished task to keep a
            # business tick alive. Include only their existing active batches.
            from .. import release_delivery
            for row in conn.execute("SELECT result_json FROM runtime_operations WHERE tool=?",
                                    (release_delivery.BATCH_TOOL,)).fetchall():
                batch = util.jload(row["result_json"]) or {}
                if batch.get("state") in ("queued", "building", "publishing") and batch.get("project_id"):
                    candidates.add(batch["project_id"])
            project_ids = sorted(pid for pid in candidates if modes.get(pid, self.mode) == "live")
            if not project_ids:
                return dict(action, reason="no_live_projects")
            from .. import engine_claim_scope
            with engine_claim_scope.command_mode(self.mode, self.force_mode):
                result = self.run_command("tick", {"project_ids": project_ids})
            created, skipped = result["created"], result["skipped"]
            return dict(action, executed=True, project_ids=project_ids,
                        created=len(created), skipped=len(skipped),
                        created_requests=created, skipped_requests=skipped)
        except Exception as exc:
            code = exc.code if isinstance(exc, util.KanpilotError) else "internal_error"
            detail = util.redact(str(exc))
            self.log("业务 tick 失败 [%s]: %s" % (code, detail))
            return dict(action, error=code, detail=detail)

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
        """Live automation plus explicitly user-authorized manual planning."""
        from .. import engine_claim_scope
        counts = {}
        rows = conn.execute(
            "SELECT r.* FROM requests r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.status='queued' AND t.save_state='active' "
            "AND t.auto_advance_paused=0").fetchall()
        for request in rows:
            task = db.require_task(conn, request["task_id"])
            if modes.get(task["project_id"], self.mode) != "live" and not engine_claim_scope.manual_plan(request) and not engine_claim_scope.manual_review_repair(conn, request, task):
                continue
            counts[request["stage"]] = counts.get(request["stage"], 0) + 1
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
        return any(isinstance(entry, dict) and "lease_expires_at" not in entry
                   and process_owner.scheduler_active(entry)
                   for entry in instances.values())

    def _default_spawn(self, stage, db_path, instance_id):
        from . import runners
        return runners.spawn(stage, db_path, instance_id=instance_id,
                             mode=self.mode, force=self.force_mode)

    def spawn_step(self, conn, modes) -> dict:
        """Spawn per-stage workers for queued requests of live projects (no App scheduler).

        Lifecycle-serial stages (dispatch/monitor/delivery) are deferred while
        another owner holds ``lifecycle_owner`` — see :data:`LIFECYCLE_SERIAL_STAGES`.
        """
        if self.foreign_scheduler_active(conn):
            return {"action": "spawn", "executed": False,
                    "reason": "app_scheduler_active", "spawned": []}
        queued = self._queued_stages(conn, modes)
        live = self._live_runner_stages(conn)
        busy = self.lifecycle_busy(conn)
        stages = plan_spawns(queued, live, foreign_active=False, lifecycle_busy=busy)
        spawner = self.spawn or self._default_spawn
        spawned = []
        for stage in stages:
            spawner(stage, self.db_path, self.instance_id)
            spawned.append(stage)
            self.log("engine spawned worker: " + stage)
        result = {"action": "spawn", "executed": True, "spawned": spawned}
        deferred = [stage for stage in LIFECYCLE_SERIAL_STAGES
                    if busy and queued.get(stage, 0) > 0
                    and stage not in (live or set()) and stage not in spawned]
        if deferred:
            # Observability: queued lifecycle work deliberately not started this
            # tick. It stays queued; the next tick retries once the serial frees.
            result["deferred"] = {"stages": deferred,
                                  "reason": "lifecycle_serial_busy"}
        return result

    @staticmethod
    def lifecycle_busy(conn) -> bool:
        """True while an owner holds the global lifecycle serial (state-model §5).

        ``lifecycle_owner`` is a plain setting with no timestamp, so a worker
        that dies between ``acquire_lifecycle`` and its ``finally`` release
        would leak it and defer these stages forever. Both holders release in a
        ``finally`` around a single remote write, so the exposure is a hard
        crash only — and a deferred spawn is recoverable (the request stays
        queued), unlike a task wrongly pushed to needs_user. A reaper would need
        a protocol decision about staleness for a serial with no heartbeat.
        """
        return bool(db.get_setting(conn, "lifecycle_owner", "") or "")

    # ------------------------------------------------------------- long-lived
    def run(self, *, instance_id=None, stage=None, lease_seconds=None,
            stop_event=None, max_ticks=None, sleeper=None) -> dict:
        """Long-lived loop: lease → tick+heartbeat → backoff → release (§5.27.1)."""
        instance_id = instance_id or util.new_uuid()
        self.instance_id = instance_id
        pid = os.getpid()
        sleeper = sleeper or (stop_event.wait if stop_event is not None else time.sleep)
        conn = db.connect(self.db_path)
        try:
            lease = lease_seconds or health.lease_seconds(conn)
            health.acquire_or_renew_lease(
                conn, util.now_iso(), instance_id=instance_id, stage=stage,
                pid=pid, lease_seconds=lease, mode=self.mode, force=self.force_mode)
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
