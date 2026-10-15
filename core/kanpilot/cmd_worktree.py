"""Read-only worktree inventory and explicitly confirmed, receipted GC (§5.29.3)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import constants, db, runtime_operations, util

_REQUEST_TERMINAL = {"succeeded", "failed", "cancelled"}
_ROUND_TERMINAL = {"finished", "failed", "completed", "cancelled", "aborted", "timeout", "error"}
_SNAPSHOT = re.compile(r"^(plan|review)-([0-9a-f]{12,64})$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _sha(value):
    return hashlib.sha256(util.jdump(value).encode("utf-8")).hexdigest()


def _rows(conn, table):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return [dict(row) for row in conn.execute("SELECT * FROM " + table)]
    return []


def _json(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, (dict, list)) else {}
    except (ValueError, TypeError):
        return {}


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _path_mentions(value, path):
    # Only absolute paths count; arbitrary task text is never path authority.
    for item in _strings(value):
        if item.startswith("/"):
            candidate = Path(item).resolve()
            target = path.resolve()
            if candidate == target or target in candidate.parents:
                return True
    return False


def _persistent_path(value, path, pool=False):
    """A terminal pool's old cwd is a location, not retained material authority."""
    if not pool:
        return _path_mentions(value, path)
    if isinstance(value, dict):
        return any(_persistent_path(item, path, pool=True) for key, item in value.items()
                   if key not in ("worktree_path", "review_root", "cwd", "plan_cwd", "review_cwd"))
    if isinstance(value, list):
        return any(_persistent_path(item, path, pool=True) for item in value)
    return _path_mentions(value, path)


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _git(path, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                            text=True, timeout=30, env=env)
    if result.returncode:
        raise ValueError("git " + args[0] + " failed")
    return result.stdout.strip()


def _common(path):
    repository = Path(path).expanduser()
    common = Path(_git(repository, "rev-parse", "--git-common-dir"))
    return (repository / common).resolve() if not common.is_absolute() else common.resolve()


def _unsafe_links(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            if (str(item) in ("/var", "/tmp", "/etc") and item.lstat().st_uid == 0
                    and item.resolve() == Path("/private") / item.name):
                continue
            return True
    return False


def _process(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "absent"
    except (PermissionError, OSError):
        return "unknown"


def _owner_terminal(job):
    # A Desktop claim process is not the external Codex consumer. Its exit or a
    # terminal request cannot establish that the consumer released the cwd.
    if job.get("backend") == "codex_desktop":
        return job.get("owner_stopped") is True
    return (job.get("backend") != "pi_desktop" or job.get("owner_stopped") is True
            or (job.get("send_state") == "not_sent" and not job.get("session_id") and not job.get("turn_id"))
            or (job.get("turn_status") in ("completed", "error", "aborted") and job.get("session_id") and job.get("turn_id")))


def _desktop_owner_stopped(jobs, task_id, run_id):
    return any(item["identity_valid"] and item["job"].get("task_id") == task_id
               and run_id and item["job"].get("run_id") == run_id
               and item["job"].get("backend") == "codex_desktop"
               and item["job"].get("status") in _ROUND_TERMINAL
               and _process(item["job"].get("pid")) == "absent"
               and _owner_terminal(item["job"]) for item in jobs)


def _frozen_desktop(payload, stage):
    if not isinstance(payload, dict) or stage not in ("plan", "review"):
        return False
    key = "planner_backend" if stage == "plan" else "review_backend"
    if payload.get(key):
        return payload[key] == "codex_desktop"
    config = payload.get("review_config")
    return stage == "review" and isinstance(config, dict) and config.get("backend") == "codex_desktop"


def _disk(path):
    """lstat only: report allocation and latest local change without following links."""
    total, latest, symlink = 0, 0, False
    todo = [path]
    while todo:
        item = todo.pop()
        stat = item.lstat()
        total += getattr(stat, "st_blocks", 0) * 512
        latest = max(latest, stat.st_mtime_ns)
        if item.is_symlink():
            symlink = True
        elif item.is_dir():
            todo.extend(item.iterdir())
    return total, latest, symlink


def _jobs(conn):
    root = Path(db.get_setting(conn, "session_runs_root") or constants.default_session_runs_root()).expanduser().absolute()
    jobs = []
    def read(directory, identity, source):
        file = directory / "job.json"
        if _unsafe_links(file) or not file.is_file():
            return
        raw = None
        try:
            raw = file.read_bytes()
            job = json.loads(raw)
        except (OSError, ValueError):
            job = None
        if not isinstance(job, dict):
            jobs.append({"job": identity, "identity_valid": False, "file": str(file),
                         "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                         "source": source, "directory_identity": identity})
            return
        valid = (all(job.get(key) == value for key, value in identity.items())
                 and job.get("backend") in constants.ROUND_BACKENDS + constants.LEGACY_ROUND_BACKENDS)
        if source == "executor":
            request = db.get_request(conn, job.get("request_id")) if job.get("request_id") else None
            valid = bool(valid and request and request["stage"] == job.get("stage")
                         and request["task_id"] == job.get("task_id") and request["run_id"] == job.get("run_id"))
        jobs.append({"job": job, "identity_valid": valid, "file": str(file),
                     "sha256": hashlib.sha256(raw).hexdigest(), "source": source, "directory_identity": identity})
    if root.is_dir() and not _unsafe_links(root):
        for task_dir in sorted(root.iterdir()):
            if _unsafe_links(task_dir) or not task_dir.is_dir():
                continue
            for run_dir in sorted(task_dir.iterdir()):
                if not _unsafe_links(run_dir) and run_dir.is_dir():
                    read(run_dir, {"task_id": task_dir.name, "run_id": run_dir.name}, "run")
    executor = constants.app_support() / "executor-sessions"
    if executor.is_dir() and not _unsafe_links(executor):
        for stage in sorted(executor.iterdir()):
            if _unsafe_links(stage) or not stage.is_dir():
                continue
            for directory in sorted(stage.iterdir()):
                if not _unsafe_links(directory) and directory.is_dir():
                    read(directory, {"stage": stage.name, "invocation_id": directory.name}, "executor")
    return jobs


def _run_directory(path, task_id, requests, jobs, head):
    stage, run_id, category = None, None, None
    for prefix, candidate_stage, candidate_category in (("plan-state-", "plan", "plan_state"),
                                                       ("review-", "review", "review_run"),
                                                       ("plan-", "plan", "plan_run")):
        if path.name.startswith(prefix):
            stage, run_id, category = candidate_stage, path.name[len(prefix):], candidate_category
            break
    if not stage:
        stage, run_id, category = "plan", path.name, "plan_run"
    linked = [req for req in requests if req.get("task_id") == task_id and req.get("run_id") == run_id]
    if not linked or not any(req["stage"] == stage for req in linked):
        return None, None, []
    reasons = []
    if any(req["stage"] != stage for req in linked):
        reasons.append("run_stage_identity_conflict")
    records = [item for item in jobs if item["job"].get("task_id") == task_id and item["job"].get("run_id") == run_id]
    valid = [item["job"] for item in records if item["identity_valid"]]
    if not valid:
        reasons.append("run_job_identity_unproven")
    expected = []
    payloads = [_json(req.get("payload_json")) for req in linked]
    material = path / ".kaneopilot-plan-material" / run_id / "manifest.json"
    values = [*payloads, *valid]
    if material.exists():
        try:
            if _unsafe_links(material):
                raise ValueError("unsafe material")
            manifest = json.loads(material.read_bytes())
            if not isinstance(manifest, dict) or (manifest.get("task") or {}).get("task_id") != task_id:
                raise ValueError("material task mismatch")
            from . import planner_inputs
            digest = planner_inputs._manifest_sha(manifest)
            hashes = [value.get("planner_input_sha256") for value in values if isinstance(value, dict) and value.get("planner_input_sha256")]
            if hashes and any(value != digest for value in hashes):
                reasons.append("plan_material_hash_mismatch")
            values.append(manifest)
        except (OSError, ValueError, TypeError, AttributeError):
            reasons.append("plan_material_identity_unproven")
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in ("head_sha", "delivery_head_sha", "manual_snapshot_head"):
            if isinstance(value.get(key), str):
                expected.append(value[key])
        frozen = value.get("review_branch_snapshot") or value.get("execution_worktree") or {}
        if isinstance(frozen, dict) and frozen.get("head_sha"):
            expected.append(frozen["head_sha"])
        manifest = value.get("manifest") or {}
        if isinstance(manifest, dict):
            frozen = manifest.get("execution_worktree") or {}
            if isinstance(frozen, dict) and frozen.get("head_sha"):
                expected.append(frozen["head_sha"])
    if not expected:
        reasons.append("run_head_evidence_missing")
    elif any(value != head for value in expected):
        reasons.append("run_head_identity_mismatch")
    if not any(_path_mentions({key: job.get(key) for key in ("worktree_path", "review_root")}, path) for job in valid):
        reasons.append("run_worktree_identity_unproven")
    return category, run_id, reasons


def _branch_evidence(task, path, common, head, branch):
    reasons, evidence = [], {}
    if not task:
        return ["task_identity_unknown"], evidence
    extra = _json(task.get("extra_json"))
    delivery = (extra.get("delivery") if isinstance(extra, dict) else {}) or {}
    if not isinstance(delivery, dict):
        delivery = {}
    evidence["task_fingerprint"] = _sha(task)
    if task.get("status") != "done":
        reasons.append("branch_task_not_done")
    if _timestamp(delivery.get("merged_at")) is None or delivery.get("pr_state") != "MERGED":
        reasons.append("branch_merge_receipt_unproven")
    if delivery.get("head_sha") != head:
        reasons.append("branch_head_identity_mismatch")
    commits = delivery.get("commits") or []
    if (not task.get("run_id") or not isinstance(commits, list)
            or not any(isinstance(commit, dict) and commit.get("execution_run_id") == task["run_id"]
                       and commit.get("sha") == head for commit in commits)):
        reasons.append("branch_execution_identity_unproven")
    if not task.get("repository") or _common(task["repository"]) != common:
        reasons.append("task_repository_mismatch")
    if branch:
        reasons.append("branch_not_detached")
    base, source = delivery.get("base_branch"), delivery.get("branch")
    if not isinstance(base, str) or not isinstance(source, str) or not base or not source:
        reasons.append("branch_ref_identity_missing")
    else:
        refs = _git(path, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
        source_refs = [ref for ref in refs if ref.split(" ", 1)[0] == "refs/heads/" + source
                       or (ref.startswith("refs/remotes/") and ref.split(" ", 1)[0].split("/", 3)[-1] == source)]
        evidence["source_refs"] = source_refs
        if source_refs:
            reasons.append("branch_source_refs_present")
        evidence["remotes"] = _git(path, "remote").splitlines()
        if evidence["remotes"]:
            reasons.append("remote_cleanup_unproven")
        bases = [ref for ref in refs if ref.split(" ", 1)[0] == "refs/heads/" + base]
        evidence["base_refs"] = bases
        if not bases:
            reasons.append("branch_base_ref_missing")
        else:
            base_sha = bases[0].split(" ", 1)[1]
            ancestor = _git(path, "merge-base", head, base_sha)
            evidence["merge_base"] = ancestor
            if ancestor != head:
                reasons.append("branch_head_not_merged_locally")
    return reasons, evidence


def _directories(root, slots):
    paths = set()
    if root.is_dir() and not root.is_symlink():
        for task in root.iterdir():
            if task.name == "pool" and task.is_dir() and not task.is_symlink():
                for repo in task.iterdir():
                    if repo.is_dir() and not repo.is_symlink():
                        paths.update(repo.iterdir())
                    else:
                        paths.add(repo)
            elif task.is_dir() and not task.is_symlink() and not (task / ".git").exists():
                children = list(task.iterdir())
                paths.update(children or [task])
            else:
                paths.add(task)
    paths.update(Path(slot["path"]) for slot in slots)
    return sorted(paths, key=str)


def _audit(ctx, retention_days):
    conn = ctx.conn
    configured_root = Path(os.path.abspath(constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None)))
    root_unsafe = _unsafe_links(configured_root)
    root = configured_root if root_unsafe else configured_root.resolve()
    tasks = {row["task_id"]: row for row in _rows(conn, "tasks")}
    requests, rounds, slots = (_rows(conn, name) for name in ("requests", "execution_rounds", "worktree_slots"))
    operations = _rows(conn, "runtime_operations")
    slot_paths = {row["path"]: row for row in slots}
    jobs = _jobs(conn)
    now = ctx.now.timestamp() if hasattr(ctx, "now") else datetime.now(timezone.utc).timestamp()
    entries = []
    for path in ([root] if root_unsafe else _directories(root, slots)):
        reasons, refs, activity = [], [], []
        entry = {"path": str(path), "category": "unknown", "references": refs,
                 "reasons": reasons, "activity": activity, "candidate": False}
        entries.append(entry)
        try:
            relative = path.relative_to(root)
            if root_unsafe or _unsafe_links(path):
                reasons.append("unsafe_symlink")
                continue
            stat = path.lstat()
            allocation, modified, symlinks = _disk(path)
            entry.update(allocated_bytes=allocation, modified_ns=modified, contains_symlinks=symlinks,
                         identity={"device": stat.st_dev, "inode": stat.st_ino})
            if not path.is_dir() or not (path / ".git").is_file() or (path / ".git").is_symlink():
                reasons.append("not_registered_git_worktree")
                continue
            top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
            common = _common(path)
            registered = _git(path, "worktree", "list", "--porcelain", "-z").split("\0")
            if top != path.resolve() or "worktree " + str(path.resolve()) not in registered:
                reasons.append("git_identity_mismatch")
                continue
            head = _git(path, "rev-parse", "HEAD")
            branch = _git(path, "symbolic-ref", "-q", "HEAD") if _git(path, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD" else None
            status = _git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")
            entry.update(head=head, branch=branch, git_common_dir=str(common), dirty=bool(status), status=status)
            if status:
                reasons.append("dirty_worktree")
            match = _SNAPSHOT.fullmatch(path.name)
            slot = slot_paths.get(str(path))
            task_id = relative.parts[0] if len(relative.parts) == 2 else None
            task = tasks.get(task_id)
            run_category, named_run, run_reasons = _run_directory(path, task_id, requests, jobs, head) if task_id else (None, None, [])
            if slot:
                entry.update(category="pool_slot", pool_id=slot["repo_key"], slot_id=slot["slot_id"], slot_state=slot["state"])
                entry["slot_fingerprint"] = _sha(slot)
                expected = root / "pool" / slot["repo_key"] / path.name
                if (path != expected or not re.fullmatch(r"slot-[0-9]+", path.name)
                        or common != Path(slot["git_common_dir"]).resolve() or branch
                        or common != _common(slot["repository"])
                        or hashlib.sha256(str(common).encode()).hexdigest()[:16] != slot["repo_key"]
                        or Path(slot["worktrees_root"]) != root):
                    reasons.append("slot_identity_mismatch")
                if slot["state"] != "free":
                    reasons.append("pool_not_free")
                if slot.get("owner_pid"):
                    process = _process(slot["owner_pid"])
                    activity.append({"kind": "slot", "process": process, "owner_state": slot.get("owner_state")})
                    if process != "absent" or slot.get("owner_state") not in ("stopped", "not_started"):
                        reasons.append("pool_owner_unproven")
            elif task_id and match and head.startswith(match.group(2)):
                entry.update(category=match.group(1) + "_snapshot", task_id=task_id)
                entry["task_fingerprint"] = _sha(task) if task else None
                if not head.startswith(match.group(2)) or branch:
                    reasons.append("snapshot_identity_mismatch")
                if not task:
                    reasons.append("task_identity_unknown")
                elif not task.get("repository") or _common(task["repository"]) != common:
                    reasons.append("task_repository_mismatch")
            elif task_id and path.name == "branch":
                entry.update(category="execution_branch", task_id=task_id)
                branch_reasons, evidence = _branch_evidence(task, path, common, head, branch)
                reasons.extend(branch_reasons)
                entry["branch_evidence"] = evidence
            elif task_id and run_category:
                entry.update(category=run_category, task_id=task_id, run_id=named_run,
                             task_fingerprint=_sha(task) if task else None)
                reasons.extend(run_reasons)
                if branch:
                    reasons.append("snapshot_identity_mismatch")
                if not task:
                    reasons.append("task_identity_unknown")
                elif not task.get("repository") or _common(task["repository"]) != common:
                    reasons.append("task_repository_mismatch")
            elif task_id and _SAFE_ID.fullmatch(path.name):
                entry.update(category="execution_or_state", task_id=task_id)
                reasons.append("persistent_execution_directory")
            else:
                reasons.append("unclassified_directory")

            location_only = bool(slot) or entry["category"] in ("plan_snapshot", "review_snapshot", "plan_run", "review_run", "plan_state")
            branch_proven = entry["category"] == "execution_branch" and not any(reason.startswith("branch_") or reason in ("task_identity_unknown", "task_repository_mismatch", "remote_cleanup_unproven") for reason in reasons)
            related_run_ids = {(task_id, named_run)} if named_run else set()
            for req in requests:
                data = {"payload": _json(req.get("payload_json")), "result": _json(req.get("result_json"))}
                direct = _path_mentions(data, path)
                holder_match = slot and slot.get("holder_id") and slot["holder_id"] == req.get("run_id")
                run_match = task_id == req.get("task_id") and named_run and named_run == req.get("run_id")
                branch_match = entry["category"] == "execution_branch" and task_id == req.get("task_id")
                sha_match = (task_id == req.get("task_id") and match and any(
                    re.fullmatch(r"[0-9a-f]{12,64}", s) and (head.startswith(s) or s.startswith(head))
                    for s in _strings(data)))
                if not (direct or sha_match or holder_match or run_match or branch_match):
                    continue
                refs.append({"kind": "request", "request_id": req["request_id"], "run_id": req.get("run_id"),
                             "stage": req["stage"], "status": req["status"], "updated_at": req.get("updated_at"), "fingerprint": _sha(req)})
                related_run_ids.add((req.get("task_id"), req.get("run_id")))
                if req["status"] not in _REQUEST_TERMINAL:
                    reasons.append("request_not_terminal")
                frozen = data["payload"]
                if (_frozen_desktop(frozen, req["stage"])
                        and not _desktop_owner_stopped(jobs, req.get("task_id"), req.get("run_id"))):
                    reasons.append("desktop_owner_stop_unproven")
                if direct and _persistent_path(data, path, pool=location_only or branch_proven):
                    reasons.append("persistent_request_path")
            if slot and slot.get("holder_id") and not related_run_ids:
                reasons.append("pool_holder_identity_unknown")
            for item in jobs:
                job = item["job"]
                directory_identity = item["directory_identity"]
                if not (_path_mentions(job, path) or (job.get("task_id"), job.get("run_id")) in related_run_ids
                        or (directory_identity.get("task_id"), directory_identity.get("run_id")) in related_run_ids
                        or (entry["category"] == "execution_branch" and job.get("task_id") == task_id)):
                    continue
                refs.append({"kind": "job", "path": item["file"], "sha256": item["sha256"], "identity_valid": item["identity_valid"]})
                if not item["identity_valid"]:
                    reasons.append("job_identity_unknown")
                    continue
                linked_round = next((run for run in rounds if run["task_id"] == job["task_id"] and run["run_id"] == job["run_id"]), None)
                linked_request = next((req for req in requests if req.get("task_id") == job["task_id"] and req.get("run_id") == job["run_id"]), None)
                if ((linked_round and linked_round["backend"] != job["backend"])
                        or (linked_request and job.get("request_id") and linked_request["request_id"] != job["request_id"])):
                    reasons.append("job_backend_or_request_identity_mismatch")
                if linked_request:
                    frozen = _json(linked_request.get("payload_json"))
                    if _frozen_desktop(frozen, linked_request["stage"]) and job["backend"] != "codex_desktop":
                        reasons.append("job_backend_or_request_identity_mismatch")
                related_run_ids.add((job["task_id"], job["run_id"]))
                process = _process(job.get("pid"))
                activity.append({"kind": "job", "run_id": job["run_id"], "status": job.get("status"), "process": process})
                if job.get("status") not in _ROUND_TERMINAL or process != "absent":
                    reasons.append("job_activity_unproven")
                if not _owner_terminal(job):
                    reasons.append("plus_turn_terminal_unproven" if job.get("backend") == "pi_desktop"
                                   else "desktop_owner_stop_unproven")
                if _persistent_path(job, path, pool=location_only or branch_proven):
                    reasons.append("persistent_job_path")
            for run in rounds:
                run_paths = {"worktree_path": run.get("worktree_path"), "config": _json(run.get("config_json"))}
                direct = _path_mentions(run_paths, path)
                if not (direct or (run.get("task_id"), run.get("run_id")) in related_run_ids
                        or (entry["category"] == "execution_branch" and run.get("task_id") == task_id)):
                    continue
                refs.append({"kind": "round", "task_id": run["task_id"], "run_id": run["run_id"],
                             "status": run["run_status"], "save_state": run["save_state"], "updated_at": run["updated_at"],
                             "fingerprint": _sha(run)})
                if run["run_status"] not in _ROUND_TERMINAL:
                    reasons.append("round_not_terminal")
                if run["backend"] == "codex_desktop" and not _desktop_owner_stopped(jobs, run["task_id"], run["run_id"]):
                    reasons.append("desktop_owner_stop_unproven")
                if run.get("pid"):
                    process = _process(run["pid"])
                    activity.append({"kind": "round", "run_id": run["run_id"], "process": process})
                    if process != "absent":
                        reasons.append("round_process_unproven")
                if direct and _persistent_path(run_paths, path, pool=location_only or branch_proven):
                    reasons.append("persistent_round_path")
            for record in tasks.values():
                data = {key: record.get(key) for key in ("plan_path", "source_path", "review_report_path")}
                data["extra"] = _json(record.get("extra_json"))
                if _persistent_path(data, path, pool=branch_proven and record["task_id"] == task_id):
                    refs.append({"kind": "task", "task_id": record["task_id"], "status": record["status"], "data_sha256": _sha(data)})
                    reasons.append("persistent_task_path")
            for operation in operations:
                if (operation["tool"] == "worktree_remove" and operation["status"] in ("prepared", "unknown")
                        and _path_mentions(_json(operation.get("result_json")), path)):
                    refs.append({"kind": "operation", "operation_id": operation["operation_id"], "status": operation["status"]})
                    reasons.append("previous_gc_unknown")
            if entry["category"] == "execution_branch":
                execution_run_id = task.get("run_id") if task else None
                terminal_requests = []
                for req in requests:
                    if req.get("task_id") != task_id or req["status"] not in _REQUEST_TERMINAL or not req.get("run_id"):
                        continue
                    if req["stage"] == "dispatch" and execution_run_id and req["run_id"] == execution_run_id:
                        terminal_requests.append(req)
                    elif req["stage"] == "review" and execution_run_id:
                        payload, result = _json(req.get("payload_json")), _json(req.get("result_json"))
                        frozen_ids = [value.get(key) for value in (payload, result) if isinstance(value, dict)
                                      for key in ("expected_execution_run_id", "execution_run_id")]
                        if execution_run_id in frozen_ids or any(item["identity_valid"] and item["job"].get("task_id") == task_id
                                and item["job"].get("run_id") == req["run_id"]
                                and item["job"].get("execution_run_id") == execution_run_id for item in jobs):
                            terminal_requests.append(req)
                if not terminal_requests:
                    reasons.append("terminal_request_evidence_missing")
                owners = [item["job"] for item in jobs if item["identity_valid"]
                          and item["job"].get("task_id") == task_id and execution_run_id
                          and item["job"].get("run_id") == execution_run_id]
                if not any(job.get("status") in _ROUND_TERMINAL and _process(job.get("pid")) == "absent"
                           and _owner_terminal(job) for job in owners):
                    reasons.append("branch_owner_stop_unproven")
                if not any(run["task_id"] == task_id and execution_run_id and run["run_id"] == execution_run_id
                           and run["run_status"] in _ROUND_TERMINAL for run in rounds):
                    reasons.append("branch_round_terminal_unproven")
            if entry["category"] in ("plan_snapshot", "review_snapshot", "plan_run", "review_run", "plan_state") and not any(ref["kind"] == "request" for ref in refs):
                reasons.append("terminal_request_evidence_missing")
            newest = max([modified / 1e9] + [t for ref in refs if (t := _timestamp(ref.get("updated_at"))) is not None]
                         + ([_timestamp(slot["updated_at"]) or now] if slot else []))
            entry["retained_until"] = datetime.fromtimestamp(newest + retention_days * 86400, timezone.utc).isoformat()
            if newest + retention_days * 86400 > now:
                reasons.append("retention_period")
            if entry["category"] not in ("plan_snapshot", "review_snapshot", "plan_run", "review_run", "plan_state", "pool_slot", "execution_branch"):
                reasons.append("report_only")
            entry["reasons"] = sorted(set(reasons))
            entry["candidate"] = not entry["reasons"]
        except (OSError, ValueError, subprocess.SubprocessError):
            reasons.append("inspection_incomplete")
    # A pool is reclaimed only as one complete, confirmed set of slots.
    for pool_id in sorted({entry.get("pool_id") for entry in entries if entry.get("pool_id")}):
        group = [entry for entry in entries if entry.get("pool_id") == pool_id]
        disk_group = [entry for entry in entries if Path(entry["path"]).parent == root / "pool" / pool_id]
        if len(group) > 10:
            for entry in group:
                entry["candidate"] = False
                entry["reasons"] = sorted(set(entry["reasons"] + ["whole_pool_exceeds_batch_limit"]))
        elif len(group) != len(disk_group) or not all(entry["candidate"] for entry in group):
            for entry in group:
                entry["candidate"] = False
                entry["reasons"] = sorted(set(entry["reasons"] + ["whole_pool_not_idle"]))
    report = {"worktrees_root": str(root), "retention_days": retention_days, "entries": entries,
              "candidates": [entry["path"] for entry in entries if entry["candidate"]],
              "largest": [entry["path"] for entry in sorted(entries, key=lambda item: item.get("allocated_bytes", 0), reverse=True)[:10]]}
    report["audit_sha256"] = _sha(report)
    return report


def _retention(payload):
    days = payload.get("retention_days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise util.bad_request("retention_days 必须为非负整数")
    return days


def cmd_worktree_audit(ctx, payload):
    return _audit(ctx, _retention(payload))


def _backup(ctx):
    database = Path(ctx.db_path)
    directory = database.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("worktree-gc-" + uuid.uuid4().hex + ".sqlite3")
    destination = sqlite3.connect(str(target))
    try:
        ctx.conn.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise util.KanpilotError("conflict", "回收前数据库备份校验失败")
    finally:
        destination.close()
    return {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "integrity_check": "ok"}


def cmd_worktree_gc(ctx, payload):
    days = _retention(payload)
    report = _audit(ctx, days)
    if payload.get("dry_run", True) is not False:
        return {"dry_run": True, **report}
    paths = payload.get("paths")
    if (payload.get("confirm") is not True or not isinstance(paths, list) or not 1 <= len(paths) <= 10
            or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)
            or not isinstance(payload.get("expected_audit_sha256"), str)):
        raise util.bad_request("执行回收须 confirm=true、1–10个不重复paths与expected_audit_sha256")
    if report["audit_sha256"] != payload["expected_audit_sha256"]:
        raise util.KanpilotError("conflict", "审计结果已变化，请重新预览确认")
    selected = [entry for entry in report["entries"] if entry["path"] in paths]
    if len(selected) != len(paths) or any(not entry["candidate"] for entry in selected):
        raise util.KanpilotError("conflict", "确认范围含非候选或未知目录")
    for entry in selected:
        if entry.get("pool_id"):
            group = {item["path"] for item in report["entries"] if item.get("pool_id") == entry["pool_id"]}
            if not group.issubset(paths):
                raise util.KanpilotError("conflict", "整池回收必须确认池内全部槽位")
    backup = _backup(ctx)
    # Check again after backup, before granting any deletion intent.
    if _audit(ctx, days)["audit_sha256"] != report["audit_sha256"]:
        raise util.KanpilotError("conflict", "备份期间审计证据已变化，请重新预览")
    results = []
    for entry in selected:
        operation_id = "gc:" + _sha({"audit": report["audit_sha256"], "path": entry["path"]})
        with db.write_txn(ctx.conn):
            current_report = _audit(ctx, days)
            current = next((item for item in current_report["entries"] if item["path"] == entry["path"]), None)
            if current != entry:
                raise util.KanpilotError("conflict", "删除前目录身份、状态或引用已变化，停止本批")
            if entry.get("pool_id"):
                current_paths = {item["path"] for item in current_report["entries"] if item.get("pool_id") == entry["pool_id"]}
                if not current_paths.issubset(paths):
                    raise util.KanpilotError("conflict", "池成员已变化，停止本批")
            # Reused confirmations must never grant a second send slot, even if
            # the previous invocation lost its response after changing Git.
            existing = runtime_operations.get_operation(ctx.conn, "worktree-gc", operation_id)
            if existing:
                raise util.KanpilotError("conflict", "已有回收回执，禁止重发未知或已完成的删除")
            if entry.get("slot_id"):
                slot = ctx.conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (entry["slot_id"],)).fetchone()
                if slot is None or _sha(dict(slot)) != entry["slot_fingerprint"]:
                    raise util.KanpilotError("conflict", "槽位已重新分配，停止本批")
                # Reserve against concurrent acquisition before leaving the txn.
                ctx.conn.execute("UPDATE worktree_slots SET state='quarantined', reason='gc_pending' WHERE slot_id=?", (entry["slot_id"],))
            op = runtime_operations.begin_operation(ctx.conn, "worktree-gc", operation_id, "core", "worktree_remove", {"entry": entry, "backup": backup})
            if op["status"] != "prepared":
                raise util.KanpilotError("conflict", "已有回收回执，禁止重发未知或已完成的删除")
            runtime_operations.mark_operation_unknown(ctx.conn, "worktree-gc", operation_id)
            runtime_operations.complete_operation(ctx.conn, "worktree-gc", operation_id, status="unknown",
                                                   result={"path": entry["path"], "intent": "remove", "backup": backup})
        if entry.get("slot_id"):
            from . import worktree_pool
            worktree_pool.projection(ctx.conn)
        try:
            _git(entry["git_common_dir"], "worktree", "remove", entry["path"])
            _git(entry["git_common_dir"], "worktree", "prune")
        except (OSError, ValueError, subprocess.SubprocessError):
            # The operation may have removed the directory before prune/response failed.
            # Keep unknown and stop the batch; never retry it automatically.
            results.append({"path": entry["path"], "status": "unknown", "operation_id": operation_id})
            break
        with db.write_txn(ctx.conn):
            if entry.get("slot_id"):
                ctx.conn.execute("DELETE FROM worktree_slots WHERE slot_id=? AND state='quarantined' AND reason='gc_pending'", (entry["slot_id"],))
            runtime_operations.complete_operation(ctx.conn, "worktree-gc", operation_id,
                                                   result={"path": entry["path"], "removed": True, "backup": backup})
        results.append({"path": entry["path"], "status": "accepted", "operation_id": operation_id})
        if entry.get("slot_id"):
            from . import worktree_pool
            worktree_pool.projection(ctx.conn)
    return {"dry_run": False, "backup": backup, "results": results, "audit_sha256": report["audit_sha256"]}
