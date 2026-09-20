"""migrate: backup + idempotent import of legacy Kaneo* state
(protocol.md §5.12, state-model.md §6)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from . import constants, core, db, util

COMPONENTS = ("KaneoPlanner", "KaneoExecutor", "KaneoReviewer")

EXECUTOR_STATUS_MAP = {
    "ready": "ready",
    "retry_ready": "ready",
    "dispatching": "needs_user",
    "prepared": "needs_user",
    "submitting": "needs_user",
    "running": "running",
    "in_review": "in_review",
    "needs_user": "needs_user",
    "execution_failed": "needs_user",
    "done": "done",
    "completed": "done",
}


def _load_manifest(path: Path) -> dict:
    if not path.is_file():
        return {"tasks": {}}
    value = util.jload(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"tasks": {}}


def _import_executor_entry(conn, now: str, task_id: str, entry: dict, warnings: list) -> Optional[str]:
    """Map one Executor manifest entry -> task row. Returns final status."""
    status = EXECUTOR_STATUS_MAP.get(entry.get("status"))
    if status is None:
        warnings.append(
            f"{task_id}: unknown executor status {entry.get('status')}; "
            "imported as needs_user"
        )
        status = "needs_user"
    extra = {}
    for key in (
        "last_observed", "recovery_history", "validated_candidate",
        "completion_evidence", "latest_blocker", "consumed_retry_authorizations",
        "retry_authorization_id", "duplicate_risk_accepted", "workspace",
        "evidence_manifest", "attachments",
    ):
        if key in entry:
            extra[f"legacy_{key}"] = entry[key]
    if entry.get("attempt_history"):
        extra["legacy_attempt_history_count"] = len(entry["attempt_history"])

    fields = {
        "task_id": task_id,
        "project_id": entry.get("project_id") or "",
        "project_name": entry.get("project_name") or "",
        "title": entry.get("title"),
        "repository": entry.get("repository") or entry.get("workspace"),
        "stage": core.stage_for_status(status),
        "status": status,
        "kaneo_status": entry.get("kaneo_status"),
        "requirements_sha256": entry.get("requirements_sha256"),
        "source_sha256": entry.get("source_sha256"),
        "source_path": entry.get("requirements_source_path"),
        "plan_path": entry.get("plan_path"),
        "plan_sha256": entry.get("plan_sha256"),
        "plan_kind": entry.get("plan_kind"),
        "send_state": entry.get("send_state"),
        "sent": 1 if entry.get("sent") is True else (
            0 if entry.get("sent") is False else None
        ),
        "run_id": entry.get("run_id"),
        "session_marker": entry.get("session_marker"),
        "session_id": entry.get("session_id"),
        "session_title": entry.get("session_title"),
        "dispatch_lease_expires_at": entry.get("dispatch_lease_expires_at"),
        "original_todo_slug": entry.get("original_todo_slug"),
        "transition_started_at": entry.get("submitting_at")
        or entry.get("transition_started_at"),
        "sent_at": entry.get("sent_at") or entry.get("submitted_at"),
        "review_key": entry.get("review_key"),
        "review_source_task_id": entry.get("review_source_task_id"),
        "review_report_path": entry.get("review_report_path"),
        "review_report_sha256": entry.get("review_report_sha256"),
        "failure_code": entry.get("failure_code"),
        "failure_reason": entry.get("failure_reason"),
        "failure_stage": entry.get("failure_stage") or entry.get("previous_status"),
        "blocked_at": entry.get("failed_at"),
        "blocked_summary": entry.get("failure_reason"),
        "lock_retained": 1 if entry.get("lock_retained") else 0,
        "manual_recovery_required": (
            1 if entry.get("manual_recovery_required") else 0
        ),
        "migrated_from": "KaneoExecutor",
        "created_at": entry.get("created_at") or now,
        "updated_at": entry.get("updated_at") or now,
        "extra_json": extra or None,
    }
    if status == "needs_user" and not fields["failure_code"]:
        fields["failure_code"] = (
            "interrupted_dispatch"
            if entry.get("status") in ("dispatching", "prepared", "submitting")
            else entry.get("status") or "migrated"
        )
    ui_evidence = entry.get("prepared_evidence") or entry.get("ui_evidence")
    if ui_evidence is not None:
        fields["ui_evidence"] = ui_evidence
    if entry.get("send_evidence") is not None:
        fields["send_evidence"] = entry["send_evidence"]

    columns = ", ".join(fields)
    conn.execute(
        f"INSERT INTO tasks({columns}) VALUES "
        f"({','.join('?' for _ in fields)})",
        tuple(
            util.jdump(v) if isinstance(v, (dict, list)) else v
            for v in fields.values()
        ),
    )
    db.add_event(
        conn, now, "migrated", task_id=task_id, actor="migration",
        detail={"from": "KaneoExecutor", "status": entry.get("status"),
                "to": status},
    )
    for index, attempt in enumerate(entry.get("attempt_history") or []):
        if not isinstance(attempt, dict):
            continue
        db.add_attempt(
            conn,
            attempt.get("request_id")
            or attempt.get("run_id")
            or f"legacy:{task_id}:{index}",
            task_id, attempt.get("run_id"), "dispatch",
            "dispatch", attempt.get("created_at"), "failed",
            finished_at=attempt.get("failed_at") or attempt.get("updated_at"),
            send_state=attempt.get("send_state"),
            note=attempt.get("failure_reason"),
        )
    return status


def _merge_planner_entry(conn, now: str, task_id: str, entry: dict, warnings: list) -> bool:
    """Merge Planner manifest fields into a task (create if absent)."""
    task = db.get_task(conn, task_id)
    plan_path = entry.get("plan_path")
    if not plan_path and entry.get("latest_plan"):
        plan_path = str(constants.plan_root() / entry["latest_plan"])
    if task is not None:
        updates = {}
        for ours, theirs in (
            ("plan_path", plan_path),
            ("plan_sha256", entry.get("plan_sha256")),
            ("source_sha256", entry.get("source_sha256")),
            ("source_path", entry.get("source_snapshot")),
            ("repository", entry.get("repository")),
            ("planner_model", entry.get("planner_model")),
            ("reasoning_effort", entry.get("reasoning_effort")),
            ("plan_generated_at", entry.get("generated_at")),
            ("project_id", entry.get("project_id")),
            ("project_name", entry.get("project_name")),
        ):
            if theirs is not None and not task[ours]:
                updates[ours] = theirs
        if updates:
            db.update_task(conn, task_id, now, **updates)
        return False

    has_plan = bool(entry.get("plan_sha256") and plan_path)
    unstarted = entry.get("implementation_status") in (None, "未开始")
    status = "ready" if (has_plan and unstarted) else "needs_plan"
    fields = {
        "task_id": task_id,
        "project_id": entry.get("project_id") or "",
        "project_name": entry.get("project_name") or "",
        "repository": entry.get("repository"),
        "stage": core.stage_for_status(status),
        "status": status,
        "kaneo_status": entry.get("source_status"),
        "source_sha256": entry.get("source_sha256"),
        "source_path": entry.get("source_snapshot"),
        "plan_path": plan_path if has_plan else None,
        "plan_sha256": entry.get("plan_sha256") if has_plan else None,
        "plan_kind": entry.get("plan_kind"),
        "plan_generated_at": entry.get("generated_at"),
        "planner_model": entry.get("planner_model"),
        "reasoning_effort": entry.get("reasoning_effort"),
        "migrated_from": "KaneoPlanner",
        "created_at": entry.get("generated_at") or now,
        "updated_at": now,
    }
    if not unstarted:
        warnings.append(
            f"{task_id}: planner implementation_status "
            f"{entry.get('implementation_status')}; imported as {status}"
        )
    columns = ", ".join(fields)
    conn.execute(
        f"INSERT INTO tasks({columns}) VALUES ({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )
    db.add_event(
        conn, now, "migrated", task_id=task_id, actor="migration",
        detail={"from": "KaneoPlanner", "to": status},
    )
    return True


def _merge_reviewer_entry(conn, now: str, task_id: str, entry: dict, warnings: list) -> bool:
    """Merge Reviewer manifest entry into the reviewed task. True if created."""
    task = db.get_task(conn, task_id)
    extra = {
        "review_conclusion": entry.get("conclusion"),
        "reviewed_at": entry.get("reviewed_at"),
        "legacy_dedupe_key": entry.get("dedupe_key"),
        "legacy_repository_fingerprint": entry.get("repository_fingerprint"),
        "legacy_branch": entry.get("branch"),
        "legacy_head": entry.get("head"),
    }
    if entry.get("repair_plan_path"):
        extra["repair_plan_path"] = entry["repair_plan_path"]
        extra["repair_plan_sha256"] = entry.get("repair_plan_sha256")
    created = False
    if task is None:
        status = (
            "done" if entry.get("kaneo_status") in constants.DONE_SLUGS
            else "in_review"
        )
        conn.execute(
            "INSERT INTO tasks(task_id, project_id, project_name, repository, "
            "stage, status, kaneo_status, review_report_path, "
            "review_report_sha256, review_key, fix_task_id, plan_path, "
            "plan_sha256, source_sha256, migrated_from, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, entry.get("project_id") or "",
                entry.get("project_name") or "", entry.get("repository"),
                core.stage_for_status(status), status,
                entry.get("kaneo_status"), entry.get("report_path"),
                entry.get("report_sha256"), entry.get("review_key"),
                entry.get("fix_task_id"), entry.get("plan_path"),
                entry.get("plan_sha256"), entry.get("source_sha256"),
                "KaneoReviewer",
                entry.get("reviewed_at") or now, now,
            ),
        )
        created = True
        db.add_event(
            conn, now, "migrated", task_id=task_id, actor="migration",
            detail={"from": "KaneoReviewer", "to": status},
        )
        task = db.get_task(conn, task_id)
    else:
        updates = {
            "review_report_path": task["review_report_path"]
            or entry.get("report_path"),
            "review_report_sha256": task["review_report_sha256"]
            or entry.get("report_sha256"),
            "review_key": task["review_key"] or entry.get("review_key"),
            "fix_task_id": task["fix_task_id"] or entry.get("fix_task_id"),
            "kaneo_status": task["kaneo_status"] or entry.get("kaneo_status"),
        }
        db.update_task(conn, task_id, now, **updates)
        db.add_event(
            conn, now, "migrated", task_id=task_id, actor="migration",
            detail={"from": "KaneoReviewer", "merged": True},
        )
    db.set_extra(conn, db.require_task(conn, task_id), now, **extra)
    return created


def _scan_orphan_plans(conn, plan_root: Path, warnings: list) -> None:
    if not plan_root.is_dir():
        return
    known = {
        row["task_id"] for row in conn.execute("SELECT task_id FROM tasks")
    }
    for path in sorted(plan_root.glob("*/*.md")):
        if path.parent.name.startswith("."):
            continue
        try:
            meta, _raw = util.read_frontmatter(path)
        except Exception as exc:
            warnings.append(f"plan parse failed {path}: {exc}")
            continue
        task_id = meta.get("task_id")
        if task_id and task_id not in known:
            warnings.append(f"orphan plan without task: {path}")


def _import_locks(conn, now: str, legacy_root: Path) -> None:
    locks_dir = legacy_root / "KaneoExecutor" / "locks"
    if not locks_dir.is_dir():
        return
    for lock in sorted(locks_dir.glob("*.lock")):
        owner_file = lock / "owner.json"
        if not owner_file.is_file():
            continue
        owner = util.jload(owner_file.read_text(encoding="utf-8")) or {}
        task_id = owner.get("task_id") or lock.name[: -len(".lock")]
        task = db.get_task(conn, task_id) if task_id else None
        if task is None:
            continue
        extra = db.task_extra(task)
        legacy_locks = extra.setdefault("legacy_locks", [])
        legacy_locks.append(
            {
                "name": lock.name,
                "owner_run_id": owner.get("owner_run_id"),
                "task_id": task_id,
                "created_at": owner.get("created_at"),
            }
        )
        db.update_task(conn, task_id, now, extra_json=extra)


def cmd_migrate(ctx, payload: dict) -> dict:
    conn, now = ctx.conn, ctx.now_iso
    legacy_root = Path(
        payload.get("legacy_root") or constants.legacy_state_root()
    ).expanduser()
    plan_root = Path(
        payload.get("plan_root") or constants.plan_root()
    ).expanduser()
    backup_root = Path(
        payload.get("backup_root") or constants.app_support()
    ).expanduser()
    dry_run = bool(payload.get("dry_run"))

    warnings: list = []
    imported = {"tasks": 0, "requests": 0, "events": 0}

    manifests = {}
    for component in COMPONENTS:
        path = legacy_root / component / "state" / "manifest.json"
        if path.is_file():
            manifests[component] = (
                path, util.sha256_file(path), _load_manifest(path)
            )
        else:
            warnings.append(f"missing manifest: {path}")

    migration = db.get_setting(conn, "migration", {}) or {}
    seen_hashes = set(migration.get("manifests_sha256") or [])

    if dry_run:
        return {
            "ok": True, "backup_path": None,
            "imported": {
                "tasks": sum(
                    len(m[2].get("tasks") or {}) for m in manifests.values()
                ),
                "requests": 0, "events": 0,
            },
            "warnings": warnings + ["dry_run: nothing written"],
        }

    # 1. backup originals (never modified) ----------------------------------
    stamp = ctx.now.strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_root / f"migration-{stamp}"
    backup_path.mkdir(parents=True, exist_ok=True)
    for component in COMPONENTS:
        source = legacy_root / component
        if source.is_dir():
            shutil.copytree(source, backup_path / component, dirs_exist_ok=True)
    if plan_root.is_dir():
        shutil.copytree(plan_root, backup_path / "Plan", dirs_exist_ok=True)

    # 2. import (skip manifests already imported by hash) -------------------
    with db.write_txn(conn):
        events_before = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='migrated'"
        ).fetchone()["n"]
        new_hashes = set(seen_hashes)
        if "KaneoExecutor" in manifests:
            path, sha, manifest = manifests["KaneoExecutor"]
            if sha not in seen_hashes:
                for task_id, entry in (manifest.get("tasks") or {}).items():
                    if db.get_task(conn, task_id) is not None:
                        continue
                    if not isinstance(entry, dict):
                        warnings.append(f"{task_id}: invalid executor entry")
                        continue
                    _import_executor_entry(
                        conn, now, task_id, entry, warnings
                    )
                    imported["tasks"] += 1
                new_hashes.add(sha)
        if "KaneoPlanner" in manifests:
            path, sha, manifest = manifests["KaneoPlanner"]
            if sha not in seen_hashes:
                for task_id, entry in (manifest.get("tasks") or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    if _merge_planner_entry(conn, now, task_id, entry, warnings):
                        imported["tasks"] += 1
                new_hashes.add(sha)
        if "KaneoReviewer" in manifests:
            path, sha, manifest = manifests["KaneoReviewer"]
            if sha not in seen_hashes:
                for task_id, entry in (manifest.get("tasks") or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    if _merge_reviewer_entry(
                        conn, now, task_id, entry, warnings
                    ):
                        imported["tasks"] += 1
                new_hashes.add(sha)

        # Follow-up requests: running -> monitor; in_review w/o conclusion -> review.
        for row in conn.execute(
            "SELECT * FROM tasks WHERE migrated_from IS NOT NULL"
        ).fetchall():
            if row["status"] == "running":
                _r, made = db.create_request(
                    conn, now, "monitor", f"monitor:{row['task_id']}",
                    task_id=row["task_id"], requested_by="migration",
                )
                imported["requests"] += int(made)
            elif row["status"] == "in_review" and not row["review_report_path"]:
                _r, made = db.create_request(
                    conn, now, "review", f"review:{row['task_id']}",
                    task_id=row["task_id"], requested_by="migration",
                )
                imported["requests"] += int(made)
            elif row["status"] == "needs_plan":
                _r, made = db.create_request(
                    conn, now, "plan", f"plan:{row['task_id']}",
                    task_id=row["task_id"], requested_by="migration",
                )
                imported["requests"] += int(made)

        _import_locks(conn, now, legacy_root)
        _scan_orphan_plans(conn, plan_root, warnings)

        imported["events"] = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE kind='migrated'"
            ).fetchone()["n"]
            - events_before
        )
        db.set_setting(
            conn, "migration",
            {
                "at": now,
                "manifests_sha256": sorted(new_hashes),
                "backup_path": str(backup_path),
            },
        )
    return {
        "ok": True, "backup_path": str(backup_path),
        "imported": imported, "warnings": warnings,
    }
