"""Read-only worktree inventory and explicitly confirmed, receipted GC (§5.29.3)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
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


def _persistent_path(value, path, pool=False, lease_location=False):
    """A terminal pool's old cwd is a location, not retained material authority."""
    if not pool:
        return _path_mentions(value, path)
    if isinstance(value, dict):
        return any(_persistent_path({name: field for name, field in item.items() if name != "path"}
                                    if lease_location and key == "pool_lease" and isinstance(item, dict) else item,
                                    path, pool=True) for key, item in value.items()
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


def _desktop_returned(item, slot, requests, operations):
    """Core receipt relinquishes this pool generation, not the native process."""
    if not slot or not item["identity_valid"]:
        return False
    job = item["job"]
    lease = job.get("pool_lease")
    if (job.get("backend") != "codex_desktop" or job.get("status") not in _ROUND_TERMINAL
            or not isinstance(lease, dict) or not isinstance(lease.get("lease_token"), str) or not lease["lease_token"]
            or lease.get("slot_id") != slot["slot_id"] or lease.get("path") != slot["path"]
            or not job.get("run_id") or lease.get("holder_id") != job["run_id"]):
        return False
    request = next((req for req in requests if req["request_id"] == job.get("request_id")), None)
    if (not request or request["task_id"] != job.get("task_id") or request.get("run_id") != job["run_id"]
            or request["stage"] not in ("plan", "review") or request["status"] not in _REQUEST_TERMINAL
            or lease.get("holder_stage") != request["stage"]
            or job.get("role", request["stage"]) != request["stage"]):
        return False
    payload = _json(request.get("payload_json"))
    frozen = payload.get("pool_lease") if isinstance(payload, dict) else None
    if (not _frozen_desktop(payload, request["stage"]) or not isinstance(frozen, dict)
            or any(frozen.get(key) != lease.get(key) for key in
                   ("slot_id", "holder_id", "holder_stage", "lease_token", "path"))):
        return False
    expected = {"request_id": request["request_id"], "run_id": job["run_id"],
                "slot_id": slot["slot_id"], "holder_id": job["run_id"], "lease_token": lease["lease_token"],
                "backend": "codex_desktop", "contract": "no_more_directory_access"}
    for operation in operations:
        if (operation["scope_id"] != job["run_id"] or operation["operation_id"] != "desktop-return:" + lease["lease_token"]
                or operation["status"] != "accepted" or operation["harness_id"] != "desktop_queue"
                or operation["tool"] != "worktree_consumer_return"):
            continue
        proof = _json(operation.get("result_json"))
        if isinstance(proof, dict) and all(proof.get(key) == value for key, value in expected.items()):
            return True
    return False


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


def _pair(value):
    """(task_id, run_id) only when both are non-empty strings, so a missing identity never matches a run."""
    task, run = value.get("task_id"), value.get("run_id")
    return (task, run) if isinstance(task, str) and isinstance(run, str) and task and run else None


def _unproven_job(item, jobs, rounds, requests, refs):
    """Manual-path proof for a job whose identity is invalid (§5.29.3 item 2). Returns (reasons, identity pairs).

    The auto path stops at job_identity_unknown. Here the claimed pair, the directory pair and the claimed
    request_id widen the scope, and no check takes the `returned` bypass of the valid-job path.
    """
    job, reasons = item["job"], []
    pairs = {pair for pair in (_pair(job), _pair(item["directory_identity"])) if pair}
    claimed = job.get("request_id")
    request_ids = {claimed} if isinstance(claimed, str) and claimed else set()
    if _process(job.get("pid")) != "absent" or job.get("status") not in _ROUND_TERMINAL:
        reasons.append("job_activity_unproven")
    else:
        reasons.append("job_identity_unknown")
    if not _owner_terminal(job):
        reasons.append("plus_turn_terminal_unproven" if job.get("backend") == "pi_desktop" else "desktop_owner_stop_unproven")
    for run in rounds:
        if (run.get("task_id"), run.get("run_id")) not in pairs:
            continue
        if run["run_status"] not in _ROUND_TERMINAL:
            reasons.append("round_not_terminal")
        if run["backend"] == "codex_desktop" and not _desktop_owner_stopped(jobs, run["task_id"], run["run_id"]):
            reasons.append("desktop_owner_stop_unproven")
        if run.get("pid") and _process(run["pid"]) != "absent":
            reasons.append("round_process_unproven")
    for req in requests:
        if (req.get("task_id"), req.get("run_id")) not in pairs and req.get("request_id") not in request_ids:
            continue
        if not any(ref.get("request_id") == req["request_id"] for ref in refs):
            refs.append({"kind": "request", "request_id": req["request_id"], "run_id": req.get("run_id"),
                         "stage": req["stage"], "status": req["status"], "updated_at": req.get("updated_at"),
                         "fingerprint": _sha(req)})
        if req["status"] not in _REQUEST_TERMINAL:
            reasons.append("request_not_terminal")
        if (_frozen_desktop(_json(req.get("payload_json")), req["stage"])
                and not _desktop_owner_stopped(jobs, req.get("task_id"), req.get("run_id"))):
            reasons.append("desktop_owner_stop_unproven")
    # Jobs before this one in the scan were never linked to the entry, so every job is checked against the identities.
    for other in jobs:
        record = other["job"]
        other_request = record.get("request_id")
        if other is item or not (_pair(record) in pairs or _pair(other["directory_identity"]) in pairs
                                 or (isinstance(other_request, str) and other_request in request_ids)):
            continue
        if _process(record.get("pid")) != "absent" or record.get("status") not in _ROUND_TERMINAL or not _owner_terminal(record):
            reasons.append("job_activity_unproven")
        if not any(ref.get("path") == other["file"] for ref in refs):
            refs.append({"kind": "job", "path": other["file"], "sha256": other["sha256"],
                         "identity_valid": other["identity_valid"]})
    return reasons, pairs


def _audit(ctx, retention_days, own_operation=None, manual=False):
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
            returned_jobs = [item["job"] for item in jobs if _desktop_returned(item, slot, requests, operations)]
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
                if slot.get("owner_pid") or slot.get("owner_state") == "returned":
                    process = _process(slot["owner_pid"])
                    returned = any(job["run_id"] == slot.get("holder_id")
                                   and job["pool_lease"]["lease_token"] == slot.get("lease_token")
                                   and job["pool_lease"]["holder_stage"] == slot.get("holder_stage") for job in returned_jobs)
                    activity.append({"kind": "slot", "process": process, "owner_state": slot.get("owner_state"),
                                     "consumer_returned": returned})
                    if not returned and (process != "absent" or slot.get("owner_state") not in ("stopped", "not_started")):
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
                        and not _desktop_owner_stopped(jobs, req.get("task_id"), req.get("run_id"))
                        and not any(job["request_id"] == req["request_id"] for job in returned_jobs)):
                    reasons.append("desktop_owner_stop_unproven")
                if direct and (_persistent_path(data["payload"], path, pool=location_only or branch_proven, lease_location=bool(slot))
                               or _persistent_path(data["result"], path, pool=location_only or branch_proven)):
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
                    if not manual:
                        reasons.append("job_identity_unknown")
                        continue
                    found, pairs = _unproven_job(item, jobs, rounds, requests, refs)
                    reasons.extend(found)
                    related_run_ids.update(pairs)
                    if _persistent_path(job, path, pool=location_only or branch_proven, lease_location=bool(slot)):
                        reasons.append("persistent_job_path")
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
                returned = job in returned_jobs
                activity.append({"kind": "job", "run_id": job["run_id"], "status": job.get("status"), "process": process,
                                 "consumer_returned": returned})
                if job.get("status") not in _ROUND_TERMINAL or (process != "absent" and not returned):
                    reasons.append("job_activity_unproven")
                if not _owner_terminal(job) and not returned:
                    reasons.append("plus_turn_terminal_unproven" if job.get("backend") == "pi_desktop"
                                   else "desktop_owner_stop_unproven")
                if _persistent_path(job, path, pool=location_only or branch_proven, lease_location=bool(slot)):
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
                related_run_ids.add((run["task_id"], run["run_id"]))
                if run["run_status"] not in _ROUND_TERMINAL:
                    reasons.append("round_not_terminal")
                returned = any(job["task_id"] == run["task_id"] and job["run_id"] == run["run_id"] for job in returned_jobs)
                if run["backend"] == "codex_desktop" and not _desktop_owner_stopped(jobs, run["task_id"], run["run_id"]) and not returned:
                    reasons.append("desktop_owner_stop_unproven")
                if run.get("pid"):
                    process = _process(run["pid"])
                    activity.append({"kind": "round", "run_id": run["run_id"], "process": process,
                                     "consumer_returned": returned})
                    if process != "absent" and not returned:
                        reasons.append("round_process_unproven")
                if direct and _persistent_path(run_paths, path, pool=location_only or branch_proven):
                    reasons.append("persistent_round_path")
            for record in tasks.values():
                data = {key: record.get(key) for key in ("plan_path", "source_path", "review_report_path")}
                data["extra"] = _json(record.get("extra_json"))
                if _persistent_path(data, path, pool=branch_proven and record["task_id"] == task_id):
                    refs.append({"kind": "task", "task_id": record["task_id"], "status": record["status"], "data_sha256": _sha(data)})
                    reasons.append("persistent_task_path")
            if slot:
                scopes = {run_id for _, run_id in related_run_ids if run_id}
                scopes.update(("worktree-slot:" + slot["slot_id"], slot.get("holder_id")))
                sessions = {item["job"].get("session_id") for item in jobs
                            if (item["job"].get("task_id"), item["job"].get("run_id")) in related_run_ids}
                sessions.update(run.get("session_id") for run in rounds
                                if (run.get("task_id"), run.get("run_id")) in related_run_ids)
                sessions.add(slot.get("session_id"))
            for operation in operations:
                if slot and operation["status"] in ("prepared", "unknown"):
                    if operation["scope_id"] in scopes or (operation.get("session_id") and operation["session_id"] in sessions):
                        refs.append({"kind": "operation", "operation_id": operation["operation_id"], "status": operation["status"]})
                        reasons.append("pool_operation_unconfirmed")
                if (operation["tool"] == "worktree_remove" and operation["status"] in ("prepared", "unknown")
                        and _path_mentions(_json(operation.get("result_json")), path)):
                    refs.append({"kind": "operation", "operation_id": operation["operation_id"], "status": operation["status"]})
                    reasons.append("previous_gc_unknown")
                if (operation["tool"] == "worktree_archive" and operation["status"] in ("prepared", "unknown")
                        and operation["operation_id"] != own_operation
                        and _path_mentions(_json(operation.get("result_json")), path)):
                    refs.append({"kind": "operation", "operation_id": operation["operation_id"], "status": operation["status"]})
                    reasons.append("previous_cleanup_unknown")
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


# ---------------------------------------------------------------- 人工确认归档与删除（§5.29.3 人工确认路径）

_CLEANUP_SCOPE = "worktree-cleanup"
_CLEANUP_CATEGORIES = {"plan_snapshot", "review_snapshot", "plan_run", "review_run", "plan_state"}
_CLEANUP_OVERRIDABLE = {"dirty_worktree", "retention_period", "plus_turn_terminal_unproven", "task_identity_unknown",
                        "terminal_request_evidence_missing", "run_head_evidence_missing", "persistent_job_path",
                        "job_identity_unknown"}
_CLEANUP_BATCH = 10
_CLEANUP_IDENTITY = {"GIT_AUTHOR_NAME": "KaneoPilot", "GIT_AUTHOR_EMAIL": "archive@kaneopilot.invalid",
                     "GIT_COMMITTER_NAME": "KaneoPilot", "GIT_COMMITTER_EMAIL": "archive@kaneopilot.invalid"}
_FINGERPRINT_FIELDS = ("path", "category", "head", "branch", "dirty", "status", "allocated_bytes", "modified_ns",
                       "contains_symlinks", "identity", "references", "activity", "task_id", "run_id",
                       "task_fingerprint", "branch_evidence")
_ARCHIVE_ID = re.compile(r"^[0-9a-f]{32}$")
_LSOF = "/usr/sbin/lsof"


def _run_git(path, args, env=None, data=None):
    """Manual-path git call. Callers pass fixed argv; force, reset, clean and checkout -f never appear."""
    result = subprocess.run(["git", "-C", str(path), *args], input=data, capture_output=True, timeout=60,
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", **(env or {})))
    if result.returncode:
        raise ValueError("git " + args[0] + " failed")
    return result.stdout


def _text(path, *args, env=None, data=None):
    return _run_git(path, args, env=env, data=data).decode("utf-8", "surrogateescape").strip()


def _status(path):
    # Raw porcelain: stripping would drop the leading space of the first status line.
    return _run_git(path, ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"]).decode(
        "utf-8", "surrogateescape")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def _index_file(path):
    return Path(path) / _text(path, "rev-parse", "--git-path", "index")


def _open_plain(path):
    # O_NOFOLLOW refuses a link swapped in after lstat; O_NONBLOCK keeps a swapped-in FIFO from hanging.
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")


def _read_plain(path):
    with _open_plain(path) as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise ValueError("not a regular file")
        return reader.read()


def _file_digest(path):
    digest = hashlib.sha256()
    with _open_plain(path) as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise ValueError("not a regular file")
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_regular(source, dest):
    """Copy without following links; the source must keep its lstat identity before, during and after."""
    before = os.lstat(source)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("not a regular file")
    digest = hashlib.sha256()
    with _open_plain(source) as reader, open(dest, "xb") as writer:
        if _identity(os.fstat(reader.fileno())) != _identity(before):
            raise ValueError("file changed while opening")
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
            writer.write(chunk)
    if _identity(os.lstat(source)) != _identity(before):
        raise ValueError("file changed while copying")
    os.chmod(dest, stat.S_IMODE(before.st_mode) | 0o600)
    return digest.hexdigest(), before


def _write_new(path, data):
    with open(path, "xb") as handle:
        handle.write(data)


def _registered(common):
    records = _run_git(common, ["worktree", "list", "--porcelain", "-z"]).decode("utf-8", "surrogateescape")
    return [Path(item[len("worktree "):]).resolve() for item in records.split("\0") if item.startswith("worktree ")]


def _lsof_cwd_output():
    result = subprocess.run([_LSOF, "-a", "-d", "cwd", "-Fpn"], capture_output=True, timeout=30)
    return result.returncode, result.stdout


def _cwd_listing():
    """Live process cwd paths, or None when the probe cannot be trusted (fail closed). Supporting evidence only."""
    try:
        code, output = _lsof_cwd_output()
    except (OSError, subprocess.SubprocessError):
        return None
    if code not in (0, 1):
        return None
    cwds, expect = [], "p"
    for line in output.decode("utf-8", "surrogateescape").splitlines():
        if expect == "p" and line[:1] == "p" and line[1:].isdigit():
            expect = "f"
        elif expect == "f" and line == "fcwd":
            expect = "n"
        elif expect == "n" and line[:1] == "n" and line[1:].startswith("/"):
            cwds.append(line[1:])
            expect = "p"
        else:
            return None
    if expect != "p" or (code == 0) != bool(cwds):
        return None
    return cwds


def _inside(candidate, target):
    resolved = Path(candidate).resolve()
    return resolved == target or target in resolved.parents


def _cwd_reasons(path, cwds):
    if cwds is None:
        return ["cwd_probe_unproven"]
    return ["live_cwd_holder"] if any(_inside(item, path) for item in cwds) else []


def _file_plan(path):
    """Untracked and ignored entries the archive preserves; anything it cannot copy faithfully is refused."""
    plan = []
    for group, flags in (("untracked", ["--exclude-standard"]), ("ignored", ["--ignored", "--exclude-standard"])):
        for raw in _run_git(path, ["ls-files", "-z", "--others", *flags]).split(b"\0"):
            if not raw:
                continue
            rel = os.fsdecode(raw)
            if rel.endswith("/") or Path(rel).is_absolute() or ".." in Path(rel).parts:
                raise ValueError("archive entry refused")
            info = os.lstat(Path(path) / rel)
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                raise ValueError("archive entry refused")
            plan.append({"group": group, "rel": rel, "kind": kind, "info": info})
    return sorted(plan, key=lambda item: item["rel"])


def _verdict(entry, cwds):
    """Manual-path verdict for one audited entry: blockers, overrides, fingerprint and file plan. Read-only."""
    path = Path(entry["path"])
    reasons, plan = list(entry["reasons"]), None
    if entry["category"] not in _CLEANUP_CATEGORIES:
        reasons.append("category_out_of_scope")
    else:
        try:
            plan = _file_plan(path)
        except (OSError, ValueError, subprocess.SubprocessError):
            reasons.append("archive_entry_refused")
        reasons.extend(_cwd_reasons(path.resolve(), cwds))
    hard = sorted({item for item in reasons if item not in _CLEANUP_OVERRIDABLE})
    overridden = sorted({item for item in reasons if item in _CLEANUP_OVERRIDABLE})
    # retention_period moves with the clock, so it stays out of the fingerprint.
    fingerprint = _sha({"fields": {key: entry.get(key) for key in _FINGERPRINT_FIELDS}, "hard": hard,
                        "overridden": [item for item in overridden if item != "retention_period"]})
    return {"entry": entry, "plan": plan, "hard": hard, "overridden": overridden,
            "eligible": not hard, "entry_sha256": fingerprint}


def _entry_for(report, path):
    return next((item for item in report["entries"] if item["path"] == path), None)


def _entry_reasons(report, cwds, path, fingerprint=None):
    entry = _entry_for(report, path)
    if entry is None:
        return None, ["path_not_in_audit"]
    verdict = _verdict(entry, cwds)
    reasons = list(verdict["hard"])
    if fingerprint is not None and verdict["entry_sha256"] != fingerprint:
        reasons.append("fingerprint_changed")
    return verdict, reasons


def _paths_payload(payload):
    paths = payload.get("paths")
    if (not isinstance(paths, list) or not 1 <= len(paths) <= _CLEANUP_BATCH
            or any(not isinstance(item, str) or not Path(item).is_absolute() for item in paths)
            or len(set(paths)) != len(paths)):
        raise util.bad_request("每批 paths 须为 1–10 个不重复的绝对路径")
    return paths


def _batches(audit_sha256, paths):
    chunks = [paths[start:start + _CLEANUP_BATCH] for start in range(0, len(paths), _CLEANUP_BATCH)]
    return [{"index": index, "paths": chunk, "sha256": _sha({"audit_sha256": audit_sha256, "paths": chunk})}
            for index, chunk in enumerate(chunks, 1)]


def _dirty_summary(entry, plan):
    lines = _status(Path(entry["path"])).splitlines()
    tracked = [line for line in lines if not line.startswith(("?? ", "!! "))]
    untracked = [item for item in plan if item["group"] == "untracked"]
    ignored = [item for item in plan if item["group"] == "ignored"]
    return {"tracked": len(tracked), "untracked": len(untracked), "ignored": len(ignored),
            "untracked_bytes": sum(item["info"].st_size for item in untracked),
            "ignored_bytes": sum(item["info"].st_size for item in ignored),
            "samples": [line[3:] for line in tracked][:5] + [item["rel"] for item in untracked + ignored][:5]}


def _plan_summary(verdict):
    plan = verdict["plan"] or []
    return {"files": len(plan), "bytes": sum(item["info"].st_size for item in plan if item["kind"] == "file")}


def _utc(nanoseconds):
    return datetime.fromtimestamp(nanoseconds / 1e9, timezone.utc).isoformat() if isinstance(nanoseconds, int) else None


def _state(path):
    """HEAD, branch, real index bytes and status: archiving must leave all four unchanged."""
    branch = None
    if _text(path, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        branch = _text(path, "symbolic-ref", "-q", "HEAD")
    return {"head": _text(path, "rev-parse", "HEAD"), "branch": branch,
            "index": _file_digest(_index_file(path)), "status": _status(path)}


def _archive_commit(path, temp, head):
    """Index and worktree snapshots from a temporary index copy; the worktree's real index is never written."""
    _copy_regular(_index_file(path), temp)
    env = {"GIT_INDEX_FILE": str(temp)}
    index_tree = _text(path, "write-tree", env=env)
    parent = head
    if index_tree != _text(path, "rev-parse", head + "^{tree}"):
        parent = _text(path, "commit-tree", index_tree, "-p", head, "--no-gpg-sign", "-m",
                       "KaneoPilot archive: index", env=_CLEANUP_IDENTITY)
    _run_git(path, ["add", "-A"], env=env)
    work_tree = _text(path, "write-tree", env=env)
    commit = parent
    if work_tree != _text(path, "rev-parse", parent + "^{tree}"):
        commit = _text(path, "commit-tree", work_tree, "-p", parent, "--no-gpg-sign", "-m",
                       "KaneoPilot archive: worktree", env=_CLEANUP_IDENTITY)
    temp.unlink()
    return index_tree, work_tree, commit


def _sums(records):
    lines = [record["sha256"] + "  files/" + record["rel"] + "\n"
             for record in sorted(records, key=lambda item: item["rel"]) if record["kind"] == "file"]
    return "".join(lines).encode("utf-8", "surrogateescape")


def _archive_dir(ctx, day, archive_id):
    return Path(ctx.db_path).resolve().parent / "KaneoPilot-archives" / day / archive_id


def _write_archive(path, archive_id, day, archive_dir, entry_sha256):
    """Copy untracked and ignored entries, commit the worktree snapshot and write the manifest. Deletes nothing."""
    before = _state(path)
    plan = _file_plan(path)
    archive_dir.mkdir(parents=True)
    (archive_dir / "files").mkdir()
    records, copied = [], 0
    for item in plan:
        source, dest = path / item["rel"], archive_dir / "files" / item["rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        record = {"group": item["group"], "rel": item["rel"], "source": str(source), "kind": item["kind"],
                  "size": item["info"].st_size, "mode": stat.S_IMODE(item["info"].st_mode),
                  "mtime_ns": item["info"].st_mtime_ns}
        if item["kind"] == "symlink":
            record["target"] = os.readlink(source)
            os.symlink(record["target"], dest)
            if _identity(os.lstat(source)) != _identity(item["info"]):
                raise ValueError("link changed while archiving")
        else:
            record["sha256"], info = _copy_regular(source, dest)
            if _identity(info) != _identity(item["info"]):
                raise ValueError("file changed while archiving")
            copied += info.st_size
        records.append(record)
    index_tree, work_tree, commit = _archive_commit(path, archive_dir / "index.tmp", before["head"])
    ref = "refs/archive/worktree-cleanup/" + day + "/" + archive_id
    _run_git(path, ["update-ref", ref, commit, ""])
    manifest = {"schema": 1, "archive_id": archive_id, "date": day, "worktree_path": str(path),
                "head": before["head"], "branch": before["branch"], "ref": ref, "commit": commit,
                "index_tree": index_tree, "work_tree": work_tree, "entry_sha256": entry_sha256,
                "tracked_changes": [line for line in before["status"].splitlines()
                                    if not line.startswith(("?? ", "!! "))],
                "files": records}
    data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("ascii")
    _write_new(archive_dir / "manifest.json", data)
    _write_new(archive_dir / "SHA256SUMS", _sums(records))
    if _state(path) != before:
        raise ValueError("worktree state changed during archive")
    receipt = {"path": str(path), "archive_id": archive_id, "date": day, "ref": ref, "commit": commit,
               "head": before["head"], "branch": before["branch"], "index_tree": index_tree, "work_tree": work_tree,
               "archive_dir": str(archive_dir), "manifest_sha256": hashlib.sha256(data).hexdigest(),
               "entry_sha256": entry_sha256, "file_count": len(records), "bytes": copied}
    _verify_archive(path, receipt)
    return receipt


def _load_manifest(archive_dir, expected_sha256):
    data = _read_plain(archive_dir / "manifest.json")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("manifest hash mismatch")
    manifest = json.loads(data)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ValueError("manifest malformed")
    return manifest


def _verify_archive(path, receipt):
    """Static archive checks before any removal: manifest, copies, SHA256SUMS, ref, trees and blobs. Deletes nothing."""
    archive_dir = Path(receipt["archive_dir"])
    manifest = _load_manifest(archive_dir, receipt["manifest_sha256"])
    if (manifest.get("worktree_path") != receipt["path"]
            or any(manifest.get(key) != receipt.get(key)
                   for key in ("archive_id", "entry_sha256", "commit", "index_tree", "work_tree", "ref"))):
        raise ValueError("manifest identity mismatch")
    for record in manifest["files"]:
        copy = archive_dir / "files" / record["rel"]
        info = os.lstat(copy)
        if record["kind"] == "symlink":
            if not stat.S_ISLNK(info.st_mode) or os.readlink(copy) != record["target"]:
                raise ValueError("archived link mismatch")
        elif not stat.S_ISREG(info.st_mode) or _file_digest(copy) != record["sha256"]:
            raise ValueError("archived file mismatch")
    if _read_plain(archive_dir / "SHA256SUMS") != _sums(manifest["files"]):
        raise ValueError("SHA256SUMS mismatch")
    if _text(path, "rev-parse", "--verify", "-q", receipt["ref"] + "^{commit}") != receipt["commit"]:
        raise ValueError("archive ref mismatch")
    if _text(path, "rev-parse", receipt["commit"] + "^{tree}") != receipt["work_tree"]:
        raise ValueError("archive tree mismatch")
    for record in manifest["files"]:
        if record["group"] != "untracked":
            continue
        if record["kind"] == "symlink":
            expected = _text(path, "hash-object", "--stdin", data=os.fsencode(record["target"]))
        else:
            expected = _text(path, "hash-object", "--path=" + record["rel"], "--",
                             str(archive_dir / "files" / record["rel"]))
        if _text(path, "rev-parse", "--verify", receipt["commit"] + ":" + record["rel"]) != expected:
            raise ValueError("archived blob mismatch")
    return manifest


def _content_proof(path, manifest, scratch):
    """Remove check b: trees, file set and per-file identity must still match the archive manifest."""
    temp = Path(scratch) / "index.proof"
    _copy_regular(_index_file(path), temp)
    env = {"GIT_INDEX_FILE": str(temp)}
    index_tree = _text(path, "write-tree", env=env)
    _run_git(path, ["add", "-A"], env=env)
    work_tree = _text(path, "write-tree", env=env)
    temp.unlink()
    if (index_tree, work_tree) != (manifest["index_tree"], manifest["work_tree"]):
        raise ValueError("tree identity changed")
    current = {(item["group"], item["rel"]): item for item in _file_plan(path)}
    expected = {(record["group"], record["rel"]): record for record in manifest["files"]}
    if set(current) != set(expected):
        raise ValueError("file set changed")
    for key, record in expected.items():
        info = current[key]["info"]
        if (current[key]["kind"] != record["kind"] or info.st_size != record["size"]
                or stat.S_IMODE(info.st_mode) != record["mode"] or info.st_mtime_ns != record["mtime_ns"]):
            raise ValueError("file identity changed")
        source = path / record["rel"]
        if record["kind"] == "symlink":
            if os.readlink(source) != record["target"]:
                raise ValueError("link target changed")
        elif _file_digest(source) != record["sha256"]:
            raise ValueError("file content changed")


def _discard(path, manifest):
    """Remove step d, three actions only: unlink verified manifest files, rmdir emptied directories, restore tracked changes."""
    for record in manifest["files"]:
        source = path / record["rel"]
        info = os.lstat(source)
        is_kind = stat.S_ISLNK(info.st_mode) if record["kind"] == "symlink" else stat.S_ISREG(info.st_mode)
        if (not is_kind or info.st_size != record["size"] or stat.S_IMODE(info.st_mode) != record["mode"]
                or info.st_mtime_ns != record["mtime_ns"]):
            raise ValueError("file changed before discard")
        os.unlink(source)
    parents = {parent for record in manifest["files"] for parent in Path(record["rel"]).parents
               if str(parent) != "."}
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            os.rmdir(path / parent)
        except OSError:
            pass
    _run_git(path, ["restore", "--source=HEAD", "--staged", "--worktree", "--", "."])
    if _status(path):
        raise ValueError("worktree not clean after discard")


def _post_remove_check(common, root, path):
    """Remove step f: the path is gone from disk and from git, and no registered worktree under root is dangling."""
    registered = _registered(common)
    if os.path.lexists(path) or path.resolve() in registered:
        raise ValueError("removed worktree still present")
    if any(root in item.parents and not item.is_dir() for item in registered):
        raise ValueError("registered worktree missing on disk")


def _archive_receipt(conn, archive_id):
    if not isinstance(archive_id, str) or not _ARCHIVE_ID.fullmatch(archive_id):
        return None
    row = runtime_operations.get_operation(conn, _CLEANUP_SCOPE, "archive:" + archive_id)
    if row is None or row["tool"] != "worktree_archive" or row["status"] != "accepted":
        return None
    return _json(row["result_json"])


def _existing_archive(conn, path, fingerprint):
    rows = conn.execute("SELECT result_json FROM runtime_operations WHERE scope_id=? AND tool='worktree_archive' "
                        "AND status='accepted'", (_CLEANUP_SCOPE,)).fetchall()
    for row in rows:
        receipt = _json(row["result_json"])
        if receipt.get("path") == path and receipt.get("entry_sha256") == fingerprint:
            return receipt
    return None


def _locked(path):
    """True when git reports this worktree as locked; `worktree remove` would refuse it after the discard."""
    locked, current = False, None
    records = _run_git(path, ["worktree", "list", "--porcelain", "-z"]).decode("utf-8", "surrogateescape")
    for item in records.split("\0"):
        if item.startswith("worktree "):
            current = Path(item[len("worktree "):]).resolve()
        elif item.startswith("locked") and current == Path(path).resolve():
            locked = True
    return locked


def _proof_reasons(conn, path, archive, scratch):
    """Remove checks a and b (accepted archive, content proof) and the lock state. Reads and hashes files, holds no lock."""
    receipt = _archive_receipt(conn, archive["archive_id"])
    if receipt is None or receipt.get("path") != path or receipt.get("manifest_sha256") != archive["manifest_sha256"]:
        return ["archive_receipt_mismatch"], None, None
    try:
        if _locked(path):
            return ["worktree_locked"], None, None
    except (OSError, ValueError, subprocess.SubprocessError):
        return ["lock_state_unproven"], None, None
    try:
        manifest = _verify_archive(Path(path), receipt)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ["archive_verification_failed"], None, None
    try:
        _content_proof(Path(path), manifest, scratch)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ["content_proof_failed"], None, None
    return [], receipt, manifest


def _remove_reasons(ctx, report, cwds, path, archive, scratch):
    """Batch precheck (read-only): proofs first, then the audit fingerprint, blockers and probe."""
    reasons, receipt, _ = _proof_reasons(ctx.conn, path, archive, scratch)
    if reasons:
        return reasons
    return _entry_reasons(report, cwds, path, receipt["entry_sha256"])[1]


def _archive_one(ctx, days, path, root):
    conn = ctx.conn
    with db.write_txn(conn):
        report = _audit(ctx, days, manual=True)
        verdict, reasons = _entry_reasons(report, _cwd_listing(), path)
        if reasons:
            return {"path": path, "status": "refused", "reasons": reasons}
        reused = _existing_archive(conn, path, verdict["entry_sha256"])
        if reused is None:
            archive_id = uuid.uuid4().hex
            day = datetime.now().astimezone().strftime("%Y%m%d")
            archive_dir = _archive_dir(ctx, day, archive_id)
            if archive_dir == root or root in archive_dir.parents:
                raise util.KanpilotError("conflict", "归档目录不得位于 worktrees_root 内")
            fingerprint = verdict["entry_sha256"]
            operation_id = "archive:" + archive_id
            op = runtime_operations.begin_operation(conn, _CLEANUP_SCOPE, operation_id, "core", "worktree_archive",
                                                    {"path": path, "archive_id": archive_id, "entry_sha256": fingerprint})
            if op["status"] != "prepared":
                raise util.KanpilotError("conflict", "已有归档回执，禁止重发未知或已完成的归档")
            runtime_operations.mark_operation_unknown(conn, _CLEANUP_SCOPE, operation_id)
            runtime_operations.complete_operation(conn, _CLEANUP_SCOPE, operation_id, status="unknown",
                                                  result={"path": path, "archive_id": archive_id, "date": day,
                                                          "entry_sha256": fingerprint, "intent": "archive"})
    if reused is not None:
        # Verification reads and hashes files, so it runs outside the write lock.
        try:
            _verify_archive(Path(path), reused)
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"path": path, "status": "refused", "reasons": ["existing_archive_verification_failed"]}
        return dict(reused, status="reused")
    # The intent is durable before any archive byte is written; a crash here leaves the receipt unknown.
    try:
        receipt = _write_archive(Path(path), archive_id, day, archive_dir, fingerprint)
        status = "accepted"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        receipt = {"path": path, "archive_id": archive_id, "date": day, "entry_sha256": fingerprint,
                   "error": str(error)}
        status = "rejected"
    with db.write_txn(conn):
        if status == "accepted":
            # Our own intent is still unknown here; it must not count against the state it describes.
            _, reasons = _entry_reasons(_audit(ctx, days, operation_id, manual=True), _cwd_listing(), path, fingerprint)
            if reasons:
                receipt = {"path": path, "archive_id": archive_id, "entry_sha256": fingerprint, "reasons": reasons}
                status = "rejected"
        runtime_operations.complete_operation(conn, _CLEANUP_SCOPE, operation_id, status=status, result=receipt)
    return dict(receipt, status=status)


def _remove_one(ctx, days, path, archive, backup, scratch):
    conn = ctx.conn
    operation_id = "remove:" + archive["archive_id"]
    # Proofs hash files and read the disk, so they run before the write lock is taken.
    reasons, receipt, manifest = _proof_reasons(conn, path, archive, scratch)
    if reasons:
        return {"path": path, "status": "refused", "reasons": reasons}
    with db.write_txn(conn):
        report = _audit(ctx, days, manual=True)
        _, reasons = _entry_reasons(report, _cwd_listing(), path, receipt["entry_sha256"])
        if reasons:
            return {"path": path, "status": "refused", "reasons": reasons}
        if runtime_operations.get_operation(conn, _CLEANUP_SCOPE, operation_id) is not None:
            return {"path": path, "status": "refused", "reasons": ["remove_receipt_exists"]}
        common = _entry_for(report, path)["git_common_dir"]
        root = Path(report["worktrees_root"]).resolve()
        op = runtime_operations.begin_operation(conn, _CLEANUP_SCOPE, operation_id, "core", "worktree_remove",
                                                {"path": path, "archive_id": archive["archive_id"],
                                                 "manifest_sha256": archive["manifest_sha256"]})
        if op["status"] != "prepared":
            return {"path": path, "status": "refused", "reasons": ["remove_receipt_exists"]}
        runtime_operations.mark_operation_unknown(conn, _CLEANUP_SCOPE, operation_id)
        runtime_operations.complete_operation(conn, _CLEANUP_SCOPE, operation_id, status="unknown",
                                              result={"path": path, "archive_id": archive["archive_id"],
                                                      "intent": "remove", "backup": backup})
    # The intent is durable; any failure from here leaves the receipt unknown, stops the batch and is never retried.
    try:
        # Proved again right before the first change: the proofs above ran before the write transaction.
        _content_proof(Path(path), manifest, scratch)
        _discard(Path(path), manifest)
        _run_git(common, ["worktree", "remove", path])
        _run_git(common, ["worktree", "prune"])
        _post_remove_check(common, root, Path(path))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"path": path, "status": "unknown", "operation_id": operation_id}
    with db.write_txn(conn):
        runtime_operations.complete_operation(conn, _CLEANUP_SCOPE, operation_id,
                                              result={"path": path, "archive_id": archive["archive_id"],
                                                      "removed": True, "backup": backup})
    return {"path": path, "status": "accepted", "operation_id": operation_id}


def cmd_worktree_cleanup_list(ctx, payload):
    days = _retention(payload)
    report = _audit(ctx, days, manual=True)
    cwds = _cwd_listing()
    verdicts = [(entry, _verdict(entry, cwds)) for entry in sorted(report["entries"], key=lambda item: item["path"])]
    batches = _batches(report["audit_sha256"], [entry["path"] for entry, verdict in verdicts if verdict["eligible"]])
    batch_of = {path: batch["index"] for batch in batches for path in batch["paths"]}
    rows = [{"path": entry["path"], "category": entry["category"], "eligible": verdict["eligible"],
             "batch": batch_of.get(entry["path"]), "hard_blockers": verdict["hard"],
             "overridden": verdict["overridden"],
             "dirty": _dirty_summary(entry, verdict["plan"]) if verdict["plan"] is not None else None,
             "allocated_bytes": entry.get("allocated_bytes"), "modified_utc": _utc(entry.get("modified_ns")),
             "retained_until": entry.get("retained_until"), "entry_sha256": verdict["entry_sha256"]}
            for entry, verdict in verdicts]
    return {"audit_sha256": report["audit_sha256"], "worktrees_root": report["worktrees_root"],
            "retention_days": days, "entries": rows, "batches": batches}


def cmd_worktree_cleanup_archive(ctx, payload):
    days = _retention(payload)
    paths = _paths_payload(payload)
    report = _audit(ctx, days, manual=True)
    root = Path(report["worktrees_root"]).resolve()
    cwds = _cwd_listing()
    items = []
    for path in paths:
        verdict, reasons = _entry_reasons(report, cwds, path)
        items.append({"path": path, "eligible": not reasons, "reasons": reasons,
                      "overridden": verdict["overridden"] if verdict else [],
                      "entry_sha256": verdict["entry_sha256"] if verdict else None,
                      "plan": _plan_summary(verdict) if verdict and verdict["plan"] is not None else None})
    if payload.get("dry_run", True) is not False:
        return {"dry_run": True, "audit_sha256": report["audit_sha256"], "items": items}
    if payload.get("confirm") is not True or not isinstance(payload.get("expected_audit_sha256"), str):
        raise util.bad_request("执行归档须 dry_run:false、confirm:true 与 expected_audit_sha256")
    if payload["expected_audit_sha256"] != report["audit_sha256"]:
        raise util.KanpilotError("conflict", "审计结果已变化，请重新预览确认")
    refused = [item["path"] for item in items if not item["eligible"]]
    if refused:
        raise util.KanpilotError("conflict", "整批拒绝，以下路径不可人工确认：" + "、".join(refused))
    results = []
    for path in paths:
        outcome = _archive_one(ctx, days, path, root)
        results.append(outcome)
        if outcome["status"] not in ("accepted", "reused"):
            break
    results.extend({"path": path, "status": "not_started"} for path in paths[len(results):])
    return {"dry_run": False, "audit_sha256": report["audit_sha256"], "results": results}


def cmd_worktree_cleanup_remove(ctx, payload):
    paths = _paths_payload(payload)
    archives = payload.get("archives")
    if (not isinstance(archives, list) or len(archives) != len(paths)
            or any(not isinstance(item, dict) or not all(isinstance(item.get(key), str)
                                                          for key in ("path", "archive_id", "manifest_sha256"))
                   for item in archives)
            or {item["path"] for item in archives} != set(paths)):
        raise util.bad_request("archives 须与 paths 一一对应，且含 path、archive_id、manifest_sha256")
    by_path = {item["path"]: item for item in archives}
    days = _retention({})
    report = _audit(ctx, days, manual=True)
    cwds = _cwd_listing()
    with tempfile.TemporaryDirectory() as scratch:
        checks = [(path, _remove_reasons(ctx, report, cwds, path, by_path[path], scratch)) for path in paths]
        if payload.get("dry_run", True) is not False:
            return {"dry_run": True, "audit_sha256": report["audit_sha256"],
                    "items": [{"path": path, "archive_id": by_path[path]["archive_id"], "eligible": not reasons,
                               "reasons": reasons} for path, reasons in checks]}
        if payload.get("confirm") is not True:
            raise util.bad_request("执行删除须 dry_run:false 且 confirm:true")
        refused = [path for path, reasons in checks if reasons]
        if refused:
            raise util.KanpilotError("conflict", "整批拒绝，以下路径未通过归档或删除前核验：" + "、".join(refused))
        backup = _backup(ctx)
        results = []
        for path in paths:
            outcome = _remove_one(ctx, days, path, by_path[path], backup, scratch)
            results.append(outcome)
            if outcome["status"] != "accepted":
                break
    results.extend({"path": path, "status": "not_started"} for path in paths[len(results):])
    return {"dry_run": False, "backup": backup, "results": results, "audit_sha256": report["audit_sha256"]}
