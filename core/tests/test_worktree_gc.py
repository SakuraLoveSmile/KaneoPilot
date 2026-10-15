"""GC runs only against disposable Git repositories and an isolated SQLite DB."""

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanpilot import cmd_worktree, db, util


OLD = "2020-01-01T00:00:00+00:00"


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def estate(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(root / "support"))
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "file").write_text("original\n")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "initial")
    database = root / "state.sqlite3"
    conn = db.connect(database)
    worktrees, runs = root / "worktrees", root / "runs"
    db.set_setting(conn, "worktrees_root", str(worktrees))
    db.set_setting(conn, "session_runs_root", str(runs))
    ctx = SimpleNamespace(conn=conn, db_path=database, now=datetime.now(timezone.utc), now_iso=util.now_iso())
    value = SimpleNamespace(ctx=ctx, repo=repo, root=worktrees, runs=runs, sha=git(repo, "rev-parse", "HEAD"))
    yield value
    conn.close()


def old(path):
    for current, dirs, files in os.walk(path):
        for name in files + dirs:
            os.utime(Path(current) / name, (1577836800, 1577836800), follow_symlinks=False)
    os.utime(path, (1577836800, 1577836800))


def snapshot(e, task="task", purpose="plan", status="succeeded", with_request=True):
    path = e.root / task / (purpose + "-" + e.sha[:12])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(e.repo, "worktree", "add", "--detach", str(path), e.sha)
    conn = e.ctx.conn
    conn.execute("INSERT OR IGNORE INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (task, "project", "Project", str(e.repo), "plan", "done", OLD, OLD))
    if with_request:
        conn.execute("INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,run_id,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (task + "-req", task + "-key", task, purpose, purpose, status, task + "-run", json.dumps({"sha": e.sha}), OLD, OLD))
    old(path)
    return path


def entry(e, path):
    return next(item for item in cmd_worktree.cmd_worktree_audit(e.ctx, {})["entries"] if item["path"] == str(path))


def confirmation(e, paths):
    preview = cmd_worktree.cmd_worktree_gc(e.ctx, {})
    return {"dry_run": False, "confirm": True, "paths": [str(path) for path in paths],
            "expected_audit_sha256": preview["audit_sha256"]}


def test_audit_is_read_only_and_finds_real_terminal_snapshot(estate):
    e = estate
    path = snapshot(e)
    before = e.ctx.conn.total_changes
    tree_before = git(e.repo, "worktree", "list", "--porcelain")
    first = cmd_worktree.cmd_worktree_audit(e.ctx, {})
    second = cmd_worktree.cmd_worktree_gc(e.ctx, {})
    assert first["candidates"] == [str(path)]
    assert first["audit_sha256"] == second["audit_sha256"]
    assert e.ctx.conn.total_changes == before
    assert git(e.repo, "worktree", "list", "--porcelain") == tree_before
    assert not (e.ctx.db_path.parent / "backups").exists()


def test_confirmed_gc_backs_up_wal_and_commits_intent_before_git(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    payload = confirmation(e, [path])
    original = cmd_worktree._git
    saw_intent = []

    def checked_git(directory, *args):
        if args[:2] == ("worktree", "remove"):
            observer = sqlite3.connect(str(e.ctx.db_path))
            receipt = observer.execute("SELECT status,result_json FROM runtime_operations WHERE tool='worktree_remove'").fetchone()
            observer.close()
            assert receipt[0] == "unknown"
            assert json.loads(receipt[1])["path"] == str(path)
            assert "--force" not in args
            saw_intent.append(True)
        return original(directory, *args)

    monkeypatch.setattr(cmd_worktree, "_git", checked_git)
    result = cmd_worktree.cmd_worktree_gc(e.ctx, payload)
    assert saw_intent and not path.exists()
    assert result["results"][0]["status"] == "accepted"
    backup = Path(result["backup"]["path"])
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == result["backup"]["sha256"]
    saved = sqlite3.connect(str(backup))
    assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert saved.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert saved.execute("SELECT COUNT(*) FROM runtime_operations").fetchone()[0] == 0
    saved.close()


@pytest.mark.parametrize("change", ["dirty", "head", "request", "mtime", "task"])
def test_changed_confirmation_never_removes(estate, change):
    e = estate
    path = snapshot(e)
    payload = confirmation(e, [path])
    if change == "dirty":
        (path / "file").write_text("edited\n")
    elif change == "head":
        (e.repo / "next").write_text("next\n")
        git(e.repo, "add", "next")
        git(e.repo, "commit", "-m", "next")
        git(path, "checkout", "--detach", git(e.repo, "rev-parse", "HEAD"))
    elif change == "request":
        e.ctx.conn.execute("UPDATE requests SET status='claimed'")
    elif change == "task":
        e.ctx.conn.execute("UPDATE tasks SET extra_json=?", (json.dumps({"delivery": {"head_sha": "new"}}),))
    else:
        os.utime(path / "file", None)
    with pytest.raises(util.KanpilotError, match="审计结果已变化"):
        cmd_worktree.cmd_worktree_gc(e.ctx, payload)
    assert path.exists()
    assert e.ctx.conn.execute("SELECT COUNT(*) FROM runtime_operations").fetchone()[0] == 0


def test_missing_task_and_requests_never_imply_orphan(estate):
    e = estate
    path = snapshot(e, with_request=False)
    e.ctx.conn.execute("DELETE FROM tasks")
    result = entry(e, path)
    assert not result["candidate"]
    assert {"task_identity_unknown", "terminal_request_evidence_missing"} <= set(result["reasons"])


def test_task_repository_identity_must_match_actual_worktree(estate):
    e = estate
    path = snapshot(e)
    other = e.repo.parent / "other"
    other.mkdir()
    git(other, "init")
    e.ctx.conn.execute("UPDATE tasks SET repository=?", (str(other),))
    assert "task_repository_mismatch" in entry(e, path)["reasons"]


def test_purged_running_round_and_valid_historical_material_are_protected(estate):
    e = estate
    path = snapshot(e)
    conn = e.ctx.conn
    conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,save_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                 ("task", "task-run", "pi_desktop", "running", "purged", OLD, OLD))
    directory = e.runs / "task" / "task-run"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({"task_id": "task", "run_id": "task-run", "backend": "pi_desktop",
                                                   "status": "completed", "pid": 2147483647,
                                                   "worktree_path": str(path), "material_dir": str(path / "materials")}))
    result = entry(e, path)
    assert not result["candidate"]
    assert {"round_not_terminal", "persistent_job_path"} <= set(result["reasons"])
    assert any(ref["kind"] == "job" and ref["identity_valid"] for ref in result["references"])


@pytest.mark.parametrize("corrupt", ["{", "[]", "null"])
def test_corrupt_job_and_wrong_backend_are_not_ignored(estate, corrupt):
    e = estate
    path = snapshot(e)
    directory = e.runs / "task" / "task-run"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(corrupt)
    assert "job_identity_unknown" in entry(e, path)["reasons"]
    e.ctx.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                       ("task", "task-run", "codex", "finished", OLD, OLD))
    (directory / "job.json").write_text(json.dumps({"task_id": "task", "run_id": "task-run", "backend": "pi_desktop",
                                                   "status": "completed", "pid": 2147483647}))
    assert "job_backend_or_request_identity_mismatch" in entry(e, path)["reasons"]


@pytest.mark.parametrize("payload", [
    {"dry_run": False},
    {"dry_run": False, "confirm": True, "paths": ["/x"] * 11, "expected_audit_sha256": "x"},
    {"dry_run": False, "confirm": True, "paths": ["/x", "/x"], "expected_audit_sha256": "x"},
    {"retention_days": True},
])
def test_confirmation_limits(estate, payload):
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_gc(estate.ctx, payload)


def test_unknown_remove_is_reported_and_never_resent(estate, monkeypatch):
    e = estate
    path = snapshot(e)
    payload = confirmation(e, [path])
    original = cmd_worktree._git
    sends = []

    def uncertain(directory, *args):
        if args[:2] == ("worktree", "remove"):
            sends.append(True)
            raise subprocess.TimeoutExpired("git", 30)
        return original(directory, *args)

    monkeypatch.setattr(cmd_worktree, "_git", uncertain)
    result = cmd_worktree.cmd_worktree_gc(e.ctx, payload)
    assert result["results"][0]["status"] == "unknown"
    assert "previous_gc_unknown" in entry(e, path)["reasons"]
    with pytest.raises(util.KanpilotError):
        cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [path]))
    assert len(sends) == 1 and path.exists()


def pool_slot(e, number, state="free"):
    common = str((e.repo / ".git").resolve())
    key = hashlib.sha256(common.encode()).hexdigest()[:16]
    path = e.root / "pool" / key / ("slot-" + str(number))
    path.parent.mkdir(parents=True, exist_ok=True)
    git(e.repo, "worktree", "add", "--detach", str(path), e.sha)
    e.ctx.conn.execute("INSERT INTO worktree_slots(slot_id,repo_key,path,repository,git_common_dir,worktrees_root,state,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       (str(number), key, str(path), str(e.repo), common, str(e.root), state, OLD))
    old(path)
    return path


def test_pool_is_reclaimed_only_as_a_complete_idle_group(estate):
    e = estate
    first, second = pool_slot(e, 1), pool_slot(e, 2)
    assert set(cmd_worktree.cmd_worktree_audit(e.ctx, {})["candidates"]) == {str(first), str(second)}
    with pytest.raises(util.KanpilotError, match="池内全部槽位"):
        cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [first]))
    result = cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [first, second]))
    assert [item["status"] for item in result["results"]] == ["accepted", "accepted"]
    assert not first.exists() and not second.exists()
    assert e.ctx.conn.execute("SELECT COUNT(*) FROM worktree_slots").fetchone()[0] == 0


@pytest.mark.parametrize("state", ["leased", "quarantined"])
def test_busy_or_quarantined_slot_protects_entire_pool(estate, state):
    e = estate
    first, second = pool_slot(e, 1), pool_slot(e, 2, state)
    assert not cmd_worktree.cmd_worktree_audit(e.ctx, {})["candidates"]
    assert "whole_pool_not_idle" in entry(e, first)["reasons"]


def test_terminal_pool_location_does_not_keep_idle_pool_forever(estate):
    e = estate
    path = pool_slot(e, 1)
    e.ctx.conn.execute("INSERT INTO tasks(task_id,project_id,project_name,stage,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       ("task", "project", "Project", "plan", "done", OLD, OLD))
    e.ctx.conn.execute("INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,run_id,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("request", "key", "task", "plan", "plan", "succeeded", "run", json.dumps({"cwd": str(path)}), OLD, OLD))
    e.ctx.conn.execute("UPDATE worktree_slots SET holder_id='run',holder_stage='plan'")
    directory = e.runs / "task" / "run"
    directory.mkdir(parents=True)
    job = {"task_id": "task", "run_id": "run", "backend": "pi_desktop", "status": "finished",
           "pid": 2147483647, "worktree_path": str(path), "owner_stopped": True}
    (directory / "job.json").write_text(json.dumps(job))
    assert entry(e, path)["candidate"]
    job["material_dir"] = str(path / "material")
    (directory / "job.json").write_text(json.dumps(job))
    assert "persistent_job_path" in entry(e, path)["reasons"]


def test_missing_historical_pool_holder_is_unknown(estate):
    e = estate
    path = pool_slot(e, 1)
    e.ctx.conn.execute("UPDATE worktree_slots SET holder_id='missing-run'")
    assert "pool_holder_identity_unknown" in entry(e, path)["reasons"]


def test_pool_larger_than_batch_limit_is_reported_as_blocked(estate):
    e = estate
    paths = [pool_slot(e, number) for number in range(11)]
    result = cmd_worktree.cmd_worktree_audit(e.ctx, {})
    assert not result["candidates"]
    assert all("whole_pool_exceeds_batch_limit" in item["reasons"] for item in result["entries"])


def test_branch_uuid_unknown_and_symlink_are_report_only(estate):
    e = estate
    for name in ("branch", "4c80b8d6-45eb-44f1-a0ab-9a62c7f4b711", "odd"):
        path = e.root / "task" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        git(e.repo, "worktree", "add", "--detach", str(path), e.sha)
        old(path)
        assert not entry(e, path)["candidate"]
    link = e.root / "escape"
    link.symlink_to(e.repo, target_is_directory=True)
    assert "unsafe_symlink" in entry(e, link)["reasons"]


def run_directory(e, category, *, status="succeeded", owner_stopped=True):
    original = snapshot(e)
    run_id = "a54b7aa8-2da3-43ef-bbc6-98a944fa592c"
    names = {"plan_run": run_id, "plan_state": "plan-state-" + run_id, "review_run": "review-" + run_id}
    path = original.parent / names[category]
    git(e.repo, "worktree", "move", str(original), str(path))
    stage = "review" if category == "review_run" else "plan"
    e.ctx.conn.execute("UPDATE requests SET stage=?,kind=?,status=?,run_id=?,payload_json=?",
                       (stage, stage, status, run_id, json.dumps({"delivery_head_sha": e.sha})))
    job = {"task_id": "task", "run_id": run_id, "request_id": "task-req", "backend": "pi_desktop",
           "status": "finished", "pid": 2147483647, "owner_stopped": owner_stopped,
           "head_sha": e.sha, "plan_kind": "standard"}
    if stage == "review":
        invocation = "invocation-42"
        directory = Path(os.environ["KANPILOT_APP_SUPPORT"]) / "executor-sessions" / "review" / invocation
        job.update(stage="review", invocation_id=invocation, review_root=str(path), manual_snapshot_head=e.sha)
    else:
        directory = e.runs / "task" / run_id
        job["worktree_path"] = str(path)
    directory.mkdir(parents=True)
    file = directory / "job.json"
    file.write_text(json.dumps(job))
    old(path)
    return path, file, job


@pytest.mark.parametrize("category", ["plan_run", "plan_state", "review_run"])
def test_terminal_run_named_planning_and_review_directories_can_be_reclaimed(estate, category):
    e = estate
    path, _, _ = run_directory(e, category)
    result = entry(e, path)
    assert result["category"] == category
    assert result["candidate"], result["reasons"]
    assert cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [path]))["results"][0]["status"] == "accepted"
    assert not path.exists()


@pytest.mark.parametrize("change,expected", [
    ("request", "request_not_terminal"),
    ("head", "run_head_identity_mismatch"),
    ("owner", "plus_turn_terminal_unproven"),
    ("material", "persistent_job_path"),
    ("identity", "run_job_identity_unproven"),
])
def test_run_named_directory_still_requires_all_identity_and_lifecycle_guards(estate, change, expected):
    e = estate
    path, file, job = run_directory(e, "plan_run")
    if change == "request":
        e.ctx.conn.execute("UPDATE requests SET status='claimed'")
    elif change == "head":
        job["head_sha"] = "f" * 40
    elif change == "owner":
        job.pop("owner_stopped")
    elif change == "material":
        job["material_dir"] = str(path / "material")
    else:
        job["task_id"] = "wrong-task"
    file.write_text(json.dumps(job))
    result = entry(e, path)
    assert not result["candidate"]
    assert expected in result["reasons"]


def test_plan_material_identity_is_cross_checked(estate):
    e = estate
    path, _, job = run_directory(e, "plan_state")
    material = path / ".kaneopilot-plan-material" / job["run_id"] / "manifest.json"
    material.parent.mkdir(parents=True)
    material.write_text(json.dumps({"task": {"task_id": "other-task"}, "execution_worktree": {"head_sha": e.sha}}))
    assert "plan_material_identity_unproven" in entry(e, path)["reasons"]


@pytest.mark.parametrize("category", ["plan_run", "review_run"])
@pytest.mark.parametrize("owner_stopped", [False, True])
def test_codex_desktop_requires_consumer_stop_proof_beyond_dead_claim_process(estate, category, owner_stopped):
    e = estate
    path, file, job = run_directory(e, category)
    job["backend"] = "codex_desktop"
    row = e.ctx.conn.execute("SELECT stage,payload_json FROM requests").fetchone()
    payload = json.loads(row["payload_json"])
    payload["planner_backend" if row["stage"] == "plan" else "review_backend"] = "codex_desktop"
    e.ctx.conn.execute("UPDATE requests SET payload_json=?", (json.dumps(payload),))
    job.pop("owner_stopped")
    # Neither a dead claim CLI, a terminal request nor a stale not_sent field
    # proves that the external Desktop consumer finished using this cwd.
    job["send_state"] = "not_sent"
    if owner_stopped:
        job["owner_stopped"] = True
    file.write_text(json.dumps(job))
    result = entry(e, path)
    assert result["candidate"] is owner_stopped, result["reasons"]
    if owner_stopped:
        assert cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [path]))["results"][0]["status"] == "accepted"
        assert not path.exists()
    else:
        assert "desktop_owner_stop_unproven" in result["reasons"]
        assert "job_activity_unproven" not in result["reasons"]
        with pytest.raises(util.KanpilotError):
            cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [path]))
        assert path.exists()


@pytest.mark.parametrize("authority", ["request", "review_config", "round"])
def test_missing_codex_desktop_job_does_not_imply_the_consumer_stopped(estate, authority):
    e = estate
    path = snapshot(e)
    if authority == "request":
        e.ctx.conn.execute("UPDATE requests SET payload_json=?", (json.dumps({"sha": e.sha, "planner_backend": "codex_desktop"}),))
    elif authority == "review_config":
        e.ctx.conn.execute("UPDATE requests SET stage='review',payload_json=?", (json.dumps({"sha": e.sha, "review_config": {"backend": "codex_desktop"}}),))
    else:
        e.ctx.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,worktree_path,pid,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           ("task", "task-run", "codex_desktop", "finished", str(path), 2147483647, OLD, OLD))
    result = entry(e, path)
    assert not result["candidate"]
    assert "desktop_owner_stop_unproven" in result["reasons"]


def merged_branch(e):
    original = snapshot(e)
    path = original.parent / "branch"
    git(e.repo, "worktree", "move", str(original), str(path))
    delivery = {"merged_at": OLD, "pr_state": "MERGED", "head_sha": e.sha,
                "base_branch": git(e.repo, "symbolic-ref", "--short", "HEAD"),
                "branch": "source-feature", "worktree_path": str(path),
                "commits": [{"execution_run_id": "task-run", "sha": e.sha}]}
    e.ctx.conn.execute("UPDATE tasks SET run_id='task-run',extra_json=?", (json.dumps({"delivery": delivery}),))
    e.ctx.conn.execute("UPDATE requests SET stage='dispatch',kind='dispatch'")
    e.ctx.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,worktree_path,pid,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       ("task", "task-run", "pi_desktop", "finished", str(path), 2147483647, OLD, OLD))
    directory = e.runs / "task" / "task-run"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({"task_id": "task", "run_id": "task-run", "backend": "pi_desktop",
                                                   "request_id": "task-req", "status": "finished", "pid": 2147483647,
                                                   "owner_stopped": True, "worktree_path": str(path)}))
    old(path)
    return path, delivery


def test_done_merged_detached_and_cleaned_branch_is_a_reachable_candidate(estate):
    e = estate
    path, _ = merged_branch(e)
    result = entry(e, path)
    assert result["candidate"], result["reasons"]
    assert result["branch_evidence"]["merge_base"] == e.sha
    assert cmd_worktree.cmd_worktree_gc(e.ctx, confirmation(e, [path]))["results"][0]["status"] == "accepted"
    assert not path.exists()


def test_dispatch_review_backend_setting_does_not_change_the_execution_owner(estate):
    e = estate
    path, _ = merged_branch(e)
    e.ctx.conn.execute("UPDATE requests SET payload_json=?", (json.dumps({"review_backend": "codex_desktop"}),))
    result = entry(e, path)
    assert result["candidate"], result["reasons"]


@pytest.mark.parametrize("change,expected", [
    ("status", "branch_task_not_done"),
    ("merged", "branch_merge_receipt_unproven"),
    ("source", "branch_source_refs_present"),
    ("remote_ref", "branch_source_refs_present"),
    ("remote", "remote_cleanup_unproven"),
    ("base", "branch_base_ref_missing"),
    ("unmerged", "branch_head_not_merged_locally"),
    ("plan", "persistent_task_path"),
])
def test_branch_gc_refuses_each_unproven_delivery_or_cleanup_condition(estate, change, expected):
    e = estate
    path, delivery = merged_branch(e)
    if change == "status":
        e.ctx.conn.execute("UPDATE tasks SET status='running'")
    elif change == "merged":
        delivery["pr_state"] = "OPEN"
    elif change == "source":
        git(e.repo, "branch", delivery["branch"], e.sha)
    elif change == "remote_ref":
        git(e.repo, "update-ref", "refs/remotes/origin/" + delivery["branch"], e.sha)
    elif change == "remote":
        git(e.repo, "remote", "add", "origin", str(e.repo.parent / "unused-remote"))
    elif change == "base":
        delivery["base_branch"] = "missing"
    elif change == "unmerged":
        (path / "new").write_text("new\n")
        git(path, "add", "new")
        git(path, "commit", "-m", "unmerged")
        delivery["head_sha"] = git(path, "rev-parse", "HEAD")
        delivery["commits"] = [{"execution_run_id": "task-run", "sha": delivery["head_sha"]}]
        old(path)
    else:
        e.ctx.conn.execute("UPDATE tasks SET plan_path=?", (str(path / "plan.md"),))
    e.ctx.conn.execute("UPDATE tasks SET extra_json=?", (json.dumps({"delivery": delivery}),))
    result = entry(e, path)
    assert not result["candidate"]
    assert expected in result["reasons"]


def test_macos_root_owned_var_alias_is_canonicalized_without_allowing_user_links(estate):
    e = estate
    path = snapshot(e)
    if not str(e.root).startswith("/private/var/") or not Path("/var").is_symlink():
        pytest.skip("macOS /var alias fixture")
    alias = "/var/" + str(e.root)[len("/private/var/"):]
    db.set_setting(e.ctx.conn, "worktrees_root", alias)
    result = cmd_worktree.cmd_worktree_audit(e.ctx, {})
    assert result["worktrees_root"] == str(e.root)
    assert result["candidates"] == [str(path)]


def test_user_created_worktree_root_alias_is_reported_without_traversing(estate, monkeypatch):
    e = estate
    snapshot(e)
    link = e.root.parent / "user-alias"
    link.symlink_to(e.root, target_is_directory=True)
    db.set_setting(e.ctx.conn, "worktrees_root", str(link))
    def unexpected_disk(path):
        raise AssertionError("must not inventory an unsafe alias")
    monkeypatch.setattr(cmd_worktree, "_disk", unexpected_disk)
    result = cmd_worktree.cmd_worktree_audit(e.ctx, {})
    assert not result["candidates"]
    assert result["entries"][0]["reasons"] == ["unsafe_symlink"]


def test_malformed_delivery_metadata_is_protected(estate):
    e = estate
    path, _ = merged_branch(e)
    e.ctx.conn.execute("UPDATE tasks SET extra_json=?", (json.dumps({"delivery": []}),))
    assert "branch_merge_receipt_unproven" in entry(e, path)["reasons"]


@pytest.mark.parametrize("missing,expected", [("request", "terminal_request_evidence_missing"),
                                             ("job", "branch_owner_stop_unproven"),
                                             ("round", "branch_round_terminal_unproven"),
                                             ("commit", "branch_execution_identity_unproven"),
                                             ("current_run", "branch_execution_identity_unproven")])
def test_delivery_receipt_cannot_substitute_for_current_execution_history(estate, missing, expected):
    e = estate
    path, delivery = merged_branch(e)
    if missing == "request":
        e.ctx.conn.execute("DELETE FROM requests")
    elif missing == "job":
        (e.runs / "task" / "task-run" / "job.json").unlink()
    elif missing == "round":
        e.ctx.conn.execute("DELETE FROM execution_rounds")
    elif missing == "commit":
        delivery.pop("commits")
        e.ctx.conn.execute("UPDATE tasks SET extra_json=?", (json.dumps({"delivery": delivery}),))
    else:
        e.ctx.conn.execute("UPDATE tasks SET run_id='new-run'")
    result = entry(e, path)
    assert not result["candidate"]
    assert expected in result["reasons"]
