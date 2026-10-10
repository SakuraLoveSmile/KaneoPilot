#!/usr/bin/env python3
"""One-shot launcher for the KaneoPilot engine in live mode (sandbox acceptance).

Configures a project's automation mapping + global capacity, prints a live
preflight, then becomes the long-lived reconciler (``kanpilot engine run``),
which adopts Kaneo To Do, schedules the golden path, and — when no App
scheduler is running — spawns per-stage workers itself.

Create the sandbox GitHub repo + Kaneo sandbox project + synthetic To Do tasks
first; the engine adopts and drives them to PR merge / done.

Usage::

    python3 scripts/engine_live.py --db /path/kaneo.db \\
        --project-id <kaneo-project-id> --repository /path/to/local/repo \\
        [--max-active-tasks 2] [--lease-seconds 1200] \\
        [--configure-only] [--no-spawn] [--mode live|shadow]

``--configure-only`` applies mapping/automation/global + prints the preflight
without starting the long-lived loop. ``--no-spawn`` schedules but never spawns
workers (rely on an external App scheduler). ``--mode shadow`` previews only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
KANPILOT = CORE / "kanpilot.py"
sys.path.insert(0, str(CORE))

from kanpilot import cli, util  # noqa: E402


def _run(command, payload, db_path):
    return cli.run(command, payload or {}, db_path)


def configure(db_path, project_id, repository, max_active, mode):
    steps = []

    mappings = _run("get-project-mappings", {}, db_path)
    revision = mappings.get("revision", 0)
    _run("set-project-mapping", {
        "project_id": project_id, "repository": repository,
        "enabled": True, "expected_revision": revision}, db_path)
    steps.append(f"mapped project {project_id} -> {repository} (rev {revision} -> {revision + 1})")

    _run("set-automation-global", {"max_active_tasks": max_active}, db_path)
    steps.append(f"global max_active_tasks = {max_active}")

    automation = {
        "enabled": True, "auto_confirm_plan": True, "label_filter": [],
        "max_fix_rounds": 2, "max_task_minutes": 240,
        "max_task_cost_usd": 10.0, "delivery_base": None,
    }
    _run("set-project-automation", {"project_id": project_id, "automation": automation}, db_path)
    steps.append(f"automation enabled for {project_id} (auto_confirm_plan=true, fix_rounds<=2)")
    return steps


def preflight(db_path):
    report = {}
    for command, payload in (("doctor", {"live": True}),
                             ("engine-status", {}),
                             ("attention-list", {})):
        try:
            report[command] = _run(command, payload, db_path)
        except util.KanpilotError as exc:
            report[command] = {"error": exc.message, "code": exc.code}
        except Exception as exc:  # noqa: BLE001
            report[command] = {"error": str(exc), "code": "internal"}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="engine_live")
    parser.add_argument("--db", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--max-active-tasks", type=int, default=2)
    parser.add_argument("--lease-seconds", type=int, default=1200)
    parser.add_argument("--mode", choices=("live", "shadow"), default="live")
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--no-spawn", action="store_true")
    args = parser.parse_args(argv)

    db_path = str(Path(args.db).expanduser().resolve())

    print("== configuring ==", file=sys.stderr)
    for line in configure(db_path, args.project_id, args.repository,
                          args.max_active_tasks, args.mode):
        print("  " + line, file=sys.stderr)

    print("== preflight ==", file=sys.stderr)
    for name, result in preflight(db_path).items():
        print(f"  {name}: {json.dumps(result, ensure_ascii=False)}", file=sys.stderr)

    if args.configure_only:
        print("configure-only: not starting the engine loop.", file=sys.stderr)
        return 0

    payload = {"mode": args.mode, "intake": True,
               "spawn": not args.no_spawn, "lease_seconds": args.lease_seconds}
    argv_run = [sys.executable, str(KANPILOT), "engine", "run",
                "--db", db_path, "--json", json.dumps(payload)]
    print("== launching engine (foreground; Ctrl-C / SIGTERM to stop) ==", file=sys.stderr)
    print("  " + " ".join(argv_run), file=sys.stderr)
    sys.stderr.flush()
    os.execvp(sys.executable, argv_run)
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
