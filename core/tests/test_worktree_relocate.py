"""Managed-root migration against disposable Git repositories and SQLite only."""
import json
import hashlib
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanpilot import cmd_worktree_relocate as relocate
from kanpilot import db, stage_contract, task_workspace, util, worktree_pool


def git(path, *args):
    return relocate._git(path, *args)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    temporary = tmp_path.resolve()
    support = temporary / "support"
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(support))
    monkeypatch.delenv("KANPILOT_WORKTREES_ROOT", raising=False)
    monkeypatch.setattr(relocate, "_process_references", lambda root: [])
    repo = temporary / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Isolated Migration Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_bytes(b"base\n")
    (repo / ".gitignore").write_text("cache.txt\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    source = temporary / "worktrees"
    branch = source / "task" / "branch"
    branch.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-b", "task-branch", str(branch), "HEAD")
    (branch / "file.txt").write_bytes(b"staged\n")
    git(branch, "add", "file.txt")
    (branch / "file.txt").write_bytes(b"unstaged\n")
    (branch / "untracked.txt").write_bytes(b"unique material\n")
    (branch / "cache.txt").write_bytes(b"ignored evidence\n")
    conn = db.connect(temporary / "fixture.db")
    db.set_setting(conn, "worktrees_root", str(source))
    conn.execute("INSERT INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,updated_at,extra_json) "
                 "VALUES('task','project','fixture',?,'review','needs_user','then','then',?)",
                 (str(repo), util.jdump({"delivery": {"worktree_path": str(branch)},
                                        "verification_checks": {"receipt": {"binding": str(branch)}}})))
    conn.execute("INSERT INTO execution_rounds(task_id,run_id,worktree_path,created_at,updated_at) "
                 "VALUES('task','old-round',?,'then','then')", (str(branch),))
    db.add_event(conn, util.now_iso(), "old-evidence", detail={"path": str(branch), "immutable": True})
    ctx = SimpleNamespace(conn=conn, db_path=temporary / "fixture.db", now_iso=util.now_iso())
    payload = {"source_root": str(source), "destination_root": str(temporary / "central" / "managed"),
               "backup_dir": str(temporary / "backup"), "operation_id": "move-fixture"}
    yield SimpleNamespace(ctx=ctx, conn=conn, source=source, destination=Path(payload["destination_root"]),
                          repo=repo, branch=branch, payload=payload, support=support)
    conn.close()


def add_lease(scene):
    lease = worktree_pool.acquire(scene.conn, scene.repo, stage="plan", holder_id="leased-run", sha="HEAD")
    scene.conn.execute("UPDATE worktree_slots SET owner_pid=2147483647,owner_state='unknown',reason='owner_stop_unconfirmed' WHERE slot_id=?",
                       (lease["slot_id"],))
    lease = dict(scene.conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (lease["slot_id"],)).fetchone())
    request, _ = db.create_request(scene.conn, util.now_iso(), "plan", "lease-request", task_id="task",
                                   payload={"pool_lease": lease, "frozen_text": str(scene.branch)})
    db.update_request(scene.conn, request["request_id"], util.now_iso(), run_id=lease["holder_id"], status="failed")
    directory = scene.support / "runs" / "task" / lease["holder_id"]
    directory.mkdir(parents=True)
    material = Path(lease["path"]) / ".kaneopilot-plan-material" / "input"
    material.mkdir(parents=True)
    (material / "source.txt").write_bytes(b"unpublished plan material")
    contract = stage_contract.make_contract("plan", task_id="task", request_id=request["request_id"],
        run_id=lease["holder_id"], harness_id="fixture.harness", workspace=lease["path"],
        inputs={"worktree_identity": {"slot_id": lease["slot_id"], "holder_id": lease["holder_id"]}}, config={})
    result = stage_contract.make_result(contract, {"code": "fixture_failed", "detail": "retain pending stop proof"}, outcome="failed")
    job = {"task_id": "task", "request_id": request["request_id"], "run_id": lease["holder_id"],
           "backend": "codex", "pool_lease": lease, "status": "failed", "send_state": "sent",
           "pid": 2147483647, "worktree_path": lease["path"], "material_dir": str(material),
           "managed_material_paths": [str(material / "source.txt")],
           "stage_contract": contract, "stage_result": result}
    path = directory / "job.json"
    path.write_text(json.dumps(job, indent=2))
    return lease, request["request_id"], path, job


def apply(scene):
    report = relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    return relocate.cmd_worktree_relocate(scene.ctx, {**scene.payload, "dry_run": False, "confirm": True,
                                            "expected_audit_sha256": report["audit_sha256"]})


def test_preview_is_read_only_and_dirty_material_and_index_survive(scene):
    index = Path(relocate._value(scene.branch, "rev-parse", "--absolute-git-dir")) / "index"
    before_index = index.read_bytes()
    before_status = git(scene.branch, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    before_tasks = scene.conn.execute("SELECT extra_json FROM tasks").fetchone()[0]
    before_rounds = [tuple(r) for r in scene.conn.execute("SELECT * FROM execution_rounds")]
    before_events = [tuple(r) for r in scene.conn.execute("SELECT * FROM events")]
    report = relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    assert report["status"] == "planned"
    assert scene.source.is_dir() and not scene.destination.exists()
    assert not Path(scene.payload["backup_dir"]).exists()
    assert not scene.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=?", (relocate._SCOPE,)).fetchone()
    result = apply(scene)
    moved = scene.destination / "task" / "branch"
    assert result["worktree_count"] == 1
    assert scene.source.is_symlink() and scene.source.resolve() == scene.destination
    assert relocate._value(moved, "rev-parse", "--show-toplevel") == str(moved)
    assert b"worktree " + os.fsencode(moved) in git(scene.repo, "worktree", "list", "--porcelain", "-z").split(b"\0")
    assert index.read_bytes() == before_index
    assert git(moved, "status", "--porcelain=v1", "-z", "--untracked-files=all") == before_status
    for name, content in (("file.txt", b"unstaged\n"), ("untracked.txt", b"unique material\n"),
                          ("cache.txt", b"ignored evidence\n")):
        assert (moved / name).read_bytes() == content
        assert (scene.branch / name).read_bytes() == content
    assert scene.conn.execute("SELECT extra_json FROM tasks").fetchone()[0] == before_tasks
    assert [tuple(r) for r in scene.conn.execute("SELECT * FROM execution_rounds")] == before_rounds
    assert [tuple(r) for r in scene.conn.execute("SELECT * FROM events")] == before_events
    assert db.get_setting(scene.conn, "worktrees_root") == str(scene.destination)
    assert db.get_setting(scene.conn, "paused_global") is False
    backup = Path(scene.payload["backup_dir"])
    assert (backup / "database.sqlite").is_file()
    integrity = json.loads((backup / "database-integrity.json").read_bytes())
    assert integrity == {"integrity_check": "ok", "sha256": hashlib.sha256((backup / "database.sqlite").read_bytes()).hexdigest()}
    assert (backup.stat().st_mode & 0o777) == 0o700


def test_current_lease_location_migrates_but_contract_result_and_unknown_owner_are_preserved(scene):
    lease, request_id, job_path, original = add_lease(scene)
    request_before = db.request_payload(db.get_request(scene.conn, request_id))
    historical, _ = db.create_request(scene.conn, util.now_iso(), "plan", "historical-lease",
        task_id="task", payload={"pool_lease": {**lease, "holder_id": "old-run", "lease_token": "old-generation"}})
    db.update_request(scene.conn, historical["request_id"], util.now_iso(), run_id="old-run", status="succeeded")
    historical_bytes = db.get_request(scene.conn, historical["request_id"])["payload_json"]
    result = apply(scene)
    assert result["current_lease_count"] == 1
    updated = json.loads(job_path.read_bytes())
    current = dict(scene.conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (lease["slot_id"],)).fetchone())
    frozen = db.request_payload(db.get_request(scene.conn, request_id))
    assert frozen["frozen_text"] == request_before["frozen_text"]
    assert current["path"] == str(scene.destination / Path(lease["path"]).relative_to(scene.source))
    assert current["worktrees_root"] == str(scene.destination)
    for key in set(lease) - {"path", "worktrees_root"}:
        assert current[key] == lease[key]
    assert current["state"] == "leased" and current["owner_state"] == "unknown"
    assert current["reason"] == "owner_stop_unconfirmed"
    assert updated["pool_lease"] == frozen["pool_lease"]
    task_workspace.validate_pool(scene.conn, updated["pool_lease"])
    assert updated["stage_contract"] == original["stage_contract"]
    assert updated["stage_result"] == original["stage_result"]
    stage_contract.validate_contract(updated["stage_contract"])
    stage_contract.validate_result(updated["stage_result"], updated["stage_contract"])
    assert updated["worktree_path"] == original["worktree_path"]
    assert updated["material_dir"].startswith(str(scene.destination))
    evidence = scene.support / "preserved"
    evidence.mkdir()
    task_workspace.preserve_pool_artifacts(updated, evidence)
    assert (evidence / "plan-materials" / "input" / "source.txt").read_bytes() == b"unpublished plan material"
    assert not worktree_pool.consumer_returned(scene.conn, current)
    assert (Path(scene.payload["backup_dir"]) / "lease-0000-job.json").read_bytes() != job_path.read_bytes()
    assert json.loads((Path(scene.payload["backup_dir"]) / "lease-0000-job.json").read_bytes()) == original
    assert db.get_request(scene.conn, historical["request_id"])["payload_json"] == historical_bytes


def test_applied_operation_replay_only_verifies_existing_result(scene):
    first = apply(scene)
    before = [tuple(r) for r in scene.conn.execute("SELECT * FROM runtime_operations")]
    second = relocate.cmd_worktree_relocate(scene.ctx, {**scene.payload, "dry_run": False, "confirm": True,
                                                       "expected_audit_sha256": first["audit_sha256"]})
    assert second["replayed"] is True
    assert [tuple(r) for r in scene.conn.execute("SELECT * FROM runtime_operations")] == before


def test_changed_ignored_material_invalidates_preview_without_moving(scene):
    report = relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    (scene.branch / "cache.txt").write_bytes(b"newer evidence")
    with pytest.raises(util.KanpilotError, match="审计摘要"):
        relocate.cmd_worktree_relocate(scene.ctx, {**scene.payload, "dry_run": False, "confirm": True,
                                                  "expected_audit_sha256": report["audit_sha256"]})
    assert not scene.source.is_symlink() and not scene.destination.exists()
    assert not Path(scene.payload["backup_dir"]).exists()


def test_existing_destination_is_not_overwritten(scene):
    scene.destination.mkdir(parents=True)
    (scene.destination / "keep.txt").write_bytes(b"unrelated")
    with pytest.raises(util.KanpilotError, match="目的目录已存在"):
        relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    assert (scene.destination / "keep.txt").read_bytes() == b"unrelated"
    assert scene.branch.is_dir()


def test_repair_failure_rolls_back_partially_changed_git_registration(scene, monkeypatch):
    original = relocate._repair
    def fail_after_partial(inventory, root):
        if root == scene.destination:
            first = inventory["worktrees"][0]
            git(first["repository"], "worktree", "repair", str(root / first["relative"]))
            raise RuntimeError("isolated partial repair failure")
        return original(inventory, root)
    monkeypatch.setattr(relocate, "_repair", fail_after_partial)
    with pytest.raises(util.KanpilotError, match="已回滚"):
        apply(scene)
    assert not scene.source.is_symlink() and scene.branch.is_dir()
    assert not scene.destination.exists()
    assert relocate._value(scene.branch, "rev-parse", "--show-toplevel") == str(scene.branch)
    assert db.get_setting(scene.conn, "worktrees_root") == str(scene.source)
    assert db.get_setting(scene.conn, "paused_global") is False
    operation = db.get_runtime_operation(scene.conn, relocate._SCOPE, scene.payload["operation_id"])
    assert operation["status"] == "rejected"
    assert util.jload(operation["result_json"])["rolled_back"] is True


def test_projection_failure_restores_database_and_job_bytes(scene, monkeypatch):
    lease, request_id, path, original = add_lease(scene)
    raw = path.read_bytes()
    payload_raw = db.get_request(scene.conn, request_id)["payload_json"]
    projection = (scene.support / "worktree-slots.json").read_bytes()
    monkeypatch.setattr(worktree_pool, "_projection", lambda conn: (_ for _ in ()).throw(RuntimeError("projection unavailable")))
    with pytest.raises(util.KanpilotError, match="已回滚"):
        apply(scene)
    assert path.read_bytes() == raw
    assert db.get_request(scene.conn, request_id)["payload_json"] == payload_raw
    assert dict(scene.conn.execute("SELECT * FROM worktree_slots WHERE slot_id=?", (lease["slot_id"],)).fetchone()) == lease
    assert (scene.support / "worktree-slots.json").read_bytes() == projection
    assert scene.source.is_dir() and not scene.source.is_symlink()
    assert not scene.destination.exists()


def test_live_owner_or_open_path_blocks_before_backup(scene, monkeypatch):
    lease, _, _, _ = add_lease(scene)
    scene.conn.execute("UPDATE worktree_slots SET owner_pid=? WHERE slot_id=?", (os.getpid(), lease["slot_id"]))
    with pytest.raises(util.KanpilotError, match="持有者进程仍存在"):
        relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    assert not Path(scene.payload["backup_dir"]).exists()
    scene.conn.execute("UPDATE worktree_slots SET owner_pid=2147483647 WHERE slot_id=?", (lease["slot_id"],))
    monkeypatch.setattr(relocate, "_process_references", lambda root: [{"pid": 999, "path": str(scene.branch)}])
    with pytest.raises(util.KanpilotError, match="仍被进程占用"):
        relocate.cmd_worktree_relocate(scene.ctx, scene.payload)


def test_missing_or_foreign_job_is_not_silently_migrated(scene):
    _, _, path, job = add_lease(scene)
    job["pool_lease"]["lease_token"] = "foreign"
    path.write_text(util.jdump(job))
    with pytest.raises(util.KanpilotError, match="没有唯一且与请求一致"):
        relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    assert not scene.destination.exists()


def test_all_repository_registrations_are_repaired(scene):
    other = scene.repo.parent / "other-repository"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "config", "user.name", "Isolated Migration Fixture")
    git(other, "config", "user.email", "fixture@example.invalid")
    git(other, "config", "commit.gpgsign", "false")
    (other / "other.txt").write_bytes(b"independent repository")
    git(other, "add", ".")
    git(other, "commit", "-qm", "base")
    second = scene.source / "other-task" / "detached"
    second.parent.mkdir()
    git(other, "worktree", "add", "--detach", str(second), "HEAD")
    head = git(second, "rev-parse", "HEAD")
    result = apply(scene)
    assert result["worktree_count"] == 2 and result["repository_count"] == 2
    moved = scene.destination / "other-task" / "detached"
    assert git(moved, "rev-parse", "HEAD") == head
    assert relocate._value(moved, "rev-parse", "--show-toplevel") == str(moved)
    assert (second / "other.txt").read_bytes() == b"independent repository"


def test_sqlite_reservation_excludes_other_slot_and_request_writers(scene, monkeypatch):
    lease, request_id, _, _ = add_lease(scene)
    original = relocate._repair
    attempts = []
    def try_other_writers(inventory, root):
        if root == scene.destination:
            other = sqlite3.connect(str(scene.ctx.db_path), timeout=0, isolation_level=None)
            try:
                for sql, args in (("UPDATE requests SET status='claimed' WHERE request_id=?", (request_id,)),
                                  ("UPDATE worktree_slots SET holder_id='foreign' WHERE slot_id=?", (lease["slot_id"],))):
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        other.execute(sql, args)
                    attempts.append(sql)
            finally:
                other.close()
        return original(inventory, root)
    monkeypatch.setattr(relocate, "_repair", try_other_writers)
    apply(scene)
    assert len(attempts) == 2
    assert db.get_request(scene.conn, request_id)["status"] == "failed"
    assert scene.conn.execute("SELECT holder_id FROM worktree_slots WHERE slot_id=?", (lease["slot_id"],)).fetchone()[0] == lease["holder_id"]


def test_external_job_change_before_commit_is_preserved_and_pauses_recovery(scene, monkeypatch):
    _, request_id, path, _ = add_lease(scene)
    original = worktree_pool._projection
    external = b'{"external":"changed during maintenance"}'
    def change_external_file(conn):
        original(conn)
        path.write_bytes(external)
    monkeypatch.setattr(worktree_pool, "_projection", change_external_file)
    with pytest.raises(util.KanpilotError, match="回滚不完整"):
        apply(scene)
    assert path.read_bytes() == external
    assert db.get_request(scene.conn, request_id)["status"] == "failed"
    assert scene.destination.is_dir() and scene.source.is_symlink()
    assert db.get_setting(scene.conn, "paused_global") is True
    operation = db.get_runtime_operation(scene.conn, relocate._SCOPE, scene.payload["operation_id"])
    assert util.jload(operation["result_json"])["rolled_back"] is False


def test_interrupt_after_root_rename_uses_actual_state_to_restore(scene, monkeypatch):
    original = Path.rename
    interrupted = []
    def interrupt_after_rename(path, target):
        result = original(path, target)
        if path == scene.source and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("after successful rename")
        return result
    monkeypatch.setattr(Path, "rename", interrupt_after_rename)
    with pytest.raises(util.KanpilotError, match="已回滚"):
        apply(scene)
    assert interrupted
    assert scene.branch.is_dir() and not scene.source.is_symlink()
    assert not scene.destination.exists()
    assert db.get_setting(scene.conn, "worktrees_root") == str(scene.source)
    assert db.get_setting(scene.conn, "paused_global") is False


def test_interrupt_after_job_replace_uses_actual_bytes_to_restore(scene, monkeypatch):
    _, request_id, path, _ = add_lease(scene)
    raw = path.read_bytes()
    payload_raw = db.get_request(scene.conn, request_id)["payload_json"]
    original = relocate._write
    interrupted = []
    def interrupt_after_write(target, content, mode=0o600):
        original(target, content, mode)
        if target == path and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("after successful job replacement")
    monkeypatch.setattr(relocate, "_write", interrupt_after_write)
    with pytest.raises(util.KanpilotError, match="已回滚"):
        apply(scene)
    assert interrupted and path.read_bytes() == raw
    assert db.get_request(scene.conn, request_id)["payload_json"] == payload_raw
    assert scene.branch.is_dir() and not scene.source.is_symlink()
    assert not scene.destination.exists()
    assert db.get_setting(scene.conn, "paused_global") is False


def test_incomplete_workspace_preparation_refuses_without_rewriting_intent(scene):
    value = {"delivery": {"worktree_path": str(scene.branch)}, "frozen": "preparation intent"}
    db.set_setting(scene.conn, "task_workspace_preparing:task", value)
    before = scene.conn.execute("SELECT value FROM settings WHERE key='task_workspace_preparing:task'").fetchone()[0]
    with pytest.raises(util.KanpilotError, match="工作区准备意图"):
        relocate.cmd_worktree_relocate(scene.ctx, scene.payload)
    assert scene.conn.execute("SELECT value FROM settings WHERE key='task_workspace_preparing:task'").fetchone()[0] == before
    assert scene.branch.is_dir() and not scene.source.is_symlink()
    assert not scene.destination.exists() and not Path(scene.payload["backup_dir"]).exists()
    db.set_setting(scene.conn, "task_workspace_preparing:task", None)
    assert relocate.cmd_worktree_relocate(scene.ctx, scene.payload)["status"] == "planned"
