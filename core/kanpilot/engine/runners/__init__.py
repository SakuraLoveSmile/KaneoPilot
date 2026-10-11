"""Plus / per-stage runner adapters (protocol §5.27).

The engine does **not** reimplement the Plus planning / execution / review
protocol. It enqueues a request and launches the existing, tested per-stage
runner subprocess to claim + execute + report it:

- ``dispatch`` / ``monitor`` / ``review`` / ``delivery`` →
  ``python -m kanpilot.executor_runner --stage <stage> [--resume]``
- ``plan`` → ``python -m kanpilot.planner_runner``

This keeps the Plus session logic (``pi_desktop_worker`` / ``pi_desktop_planner``
/ ``pi_desktop_review``) in one place; the engine only owns scheduling,
failure-ladder policy, the attention inbox and the single-instance lease.
Worker processes launched here inherit ``KANPILOT_EXECUTOR_INSTANCE_ID`` so
``process_owner`` can attribute them to the owning engine instance.
"""
from __future__ import annotations

import os
import subprocess
import sys
from ... import constants, process_owner

# Stages served by executor_runner (dispatch/monitor/review/delivery).
_EXECUTOR_STAGES = ("dispatch", "monitor", "review", "delivery")


def _child_env(db_path, instance_id, mode="legacy", force=False):
    env = dict(os.environ)
    env["KANPILOT_DB"] = str(db_path)
    env["KANPILOT_ENGINE_WORKER"] = "1"
    env["KANPILOT_ENGINE_MODE"] = mode
    env["KANPILOT_ENGINE_FORCE"] = "1" if force else "0"
    if instance_id:
        env[process_owner.INSTANCE_ENV] = instance_id
    return env


def spawn_stage(stage, db_path, *, instance_id=None, resume=False, mode="legacy", force=False) -> subprocess.Popen:
    """Launch ``executor_runner`` for a deterministic/Plus stage."""
    if stage not in _EXECUTOR_STAGES:
        raise ValueError(f"spawn_stage: unsupported stage {stage!r}")
    argv = [sys.executable, "-m", "kanpilot.executor_runner", "--stage", stage]
    if resume:
        argv.append("--resume")
    return subprocess.Popen(
        argv, cwd=str(constants.core_root()), env=_child_env(db_path, instance_id, mode, force),
        stdin=subprocess.DEVNULL, start_new_session=True)


def spawn_planner(db_path, *, instance_id=None, mode="legacy", force=False) -> subprocess.Popen:
    """Launch ``planner_runner`` (planning stage)."""
    return subprocess.Popen(
        [sys.executable, "-m", "kanpilot.planner_runner"],
        cwd=str(constants.core_root()), env=_child_env(db_path, instance_id, mode, force),
        stdin=subprocess.DEVNULL, start_new_session=True)


def spawn(stage, db_path, *, instance_id=None, resume=False, mode="legacy", force=False) -> subprocess.Popen:
    """Dispatch to the right runner for a stage."""
    if stage == "plan":
        return spawn_planner(db_path, instance_id=instance_id, mode=mode, force=force)
    return spawn_stage(stage, db_path, instance_id=instance_id, resume=resume, mode=mode, force=force)
