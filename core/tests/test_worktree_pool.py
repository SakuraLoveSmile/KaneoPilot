"""Disposable Git/SQLite fixtures only; no installed app, services or models."""
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from helpers import CORE  # noqa: F401
from kanpilot import constants, db, review_snapshot, util, worktree_pool as pool


def git(root, *args):
    return pool._git(root, *args)


@pytest.fixture
def scene(tmp_path):
    environment = patch.dict(os.environ, {"KANPILOT_APP_SUPPORT": str(tmp_path / "support")})
    environment.start()
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Isolated Pool Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_bytes(b"base\n")
    (repo / ".gitignore").write_text("cache.txt\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    conn = db.connect(tmp_path / "isolated.db")
    root = tmp_path / "worktrees"
    db.set_setting(conn, "worktrees_root", str(root))
    yield repo, conn, root
    conn.close()
    environment.stop()


def acquire(scene, holder="run-1", stage="plan", sha="HEAD"):
    repo, conn, root = scene
    return pool.acquire(conn, repo, stage=stage, holder_id=holder, sha=sha, worktrees_root=root)


def release(conn, lease):
    return pool.release(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], owner_stopped=True)


def test_twenty_rounds_reuse_one_slot_and_new_generation(scene):
    repo, conn, root = scene
    old = None
    for number in range(20):
        lease = acquire(scene, "run-" + str(number), "plan" if number % 2 else "review")
        if old:
            assert lease["path"] == old["path"]
            assert lease["lease_token"] != old["lease_token"]
            with pytest.raises(util.KanpilotError):
                release(conn, old)
        assert release(conn, lease)["state"] == "free"
        old = lease
    assert len(list((root / "pool").glob("*/slot-*"))) == 1
    assert conn.execute("SELECT count(*) FROM worktree_slots").fetchone()[0] == 1
    projection = json.loads((constants.app_support() / "worktree-slots.json").read_text())
    assert projection["slots"][0]["repository"] == str(repo)
    assert projection["slots"][0]["state"] == "free"
    assert conn.execute("SELECT count(*) FROM runtime_operations WHERE harness_id='worktree_pool'").fetchone()[0] >= 40


def test_parallel_two_plans_one_review_and_full_pool_wait(scene):
    repo, conn, root = scene
    db.set_setting(conn, "worktree_pool_size", 3)
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    def run(index):
        local = db.connect(path)
        try:
            return pool.acquire(local, repo, stage="review" if index == 2 else "plan", holder_id=str(index), sha="HEAD", worktrees_root=root)
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=3) as executor:
        leases = list(executor.map(run, range(3)))
    assert len({lease["slot_id"] for lease in leases}) == 3
    assert acquire(scene, "fourth")["state"] == "waiting"
    assert conn.execute("SELECT count(*) FROM worktree_slots").fetchone()[0] == 3
    second = root.parent / "another-checkout"
    git(repo, "worktree", "add", "--detach", str(second), "HEAD")
    assert pool.acquire(conn, second, stage="review", holder_id="other-tree", sha="HEAD", worktrees_root=root)["state"] == "waiting"


def test_snapshot_release_preserves_head_and_restores_bytes(scene):
    repo, conn, _root = scene
    (repo / "file.txt").write_bytes(b"changed\r\n")
    (repo / "new.txt").write_bytes(b"frozen\n")
    snapshot = review_snapshot.freeze(repo)
    lease = acquire(scene)
    pool.register_snapshot(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], snapshot)
    slot = Path(lease["path"])
    assert git(slot, "rev-parse", "HEAD").strip().decode() == snapshot["head"]
    assert (slot / "file.txt").read_bytes() == b"changed\r\n"
    (slot / ".kaneopilot-plan-material").mkdir()
    (slot / ".kaneopilot-plan-material" / "manifest.json").write_text("{}")
    (slot / ".pi" / "plan").mkdir(parents=True)
    (slot / ".pi" / "plan" / "result.md").write_text("plan")
    result = release(conn, lease)
    assert result["state"] == "free", result["reason"]
    assert (slot / "file.txt").read_bytes() == b"base\n"
    assert not (slot / "new.txt").exists()
    assert git(slot, "status", "--porcelain") == b""
    assert (repo / "file.txt").read_bytes() == b"changed\r\n"


def test_unknown_changes_quarantine_without_discarding(scene):
    _repo, conn, _root = scene
    lease = acquire(scene)
    slot = Path(lease["path"])
    (slot / "unknown.txt").write_text("keep")
    assert release(conn, lease)["state"] == "quarantined"
    assert (slot / "unknown.txt").read_text() == "keep"


def test_no_stop_proof_never_releases_or_deletes(scene):
    _repo, conn, _root = scene
    lease = acquire(scene)
    slot = Path(lease["path"])
    (slot / ".kaneopilot-plan-material").mkdir()
    assert pool.release(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"])["state"] == "leased"
    conn.execute("UPDATE worktree_slots SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    assert pool.reap_expired(conn)[0]["state"] == "leased"
    assert (slot / ".kaneopilot-plan-material").exists()


def test_recover_same_holder_keeps_artifacts_and_rejects_old_token(scene):
    _repo, conn, _root = scene
    lease = acquire(scene)
    slot = Path(lease["path"])
    (slot / ".kaneopilot-plan-material").mkdir()
    recovered = acquire(scene)
    assert recovered["lease_token"] == lease["lease_token"]
    assert (slot / ".kaneopilot-plan-material").exists()
    with pytest.raises(util.KanpilotError):
        pool.heartbeat(conn, lease["slot_id"], lease["holder_id"], "stale")


def test_snapshot_with_unknown_edit_is_retained(scene):
    repo, conn, _root = scene
    (repo / "file.txt").write_text("frozen input\n")
    snapshot = review_snapshot.freeze(repo)
    lease = acquire(scene)
    pool.register_snapshot(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], snapshot)
    slot = Path(lease["path"])
    (slot / "file.txt").write_text("later user change\n")
    result = release(conn, lease)
    assert result["state"] == "quarantined"
    assert (slot / "file.txt").read_text() == "later user change\n"


def test_frozen_input_at_artifact_path_is_verified_before_removal(scene):
    repo, conn, _root = scene
    material = repo / ".kaneopilot-plan-material"
    material.mkdir()
    (material / "input.txt").write_text("frozen input")
    snapshot = review_snapshot.freeze(repo)
    lease = acquire(scene)
    pool.register_snapshot(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], snapshot)
    result = release(conn, lease)
    assert result["state"] == "free", result["reason"]
    assert not (Path(lease["path"]) / ".kaneopilot-plan-material" / "input.txt").exists()


def test_user_symlink_root_is_rejected(scene, tmp_path):
    repo, conn, root = scene
    root.mkdir()
    alias = tmp_path / "forged-root"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(util.KanpilotError):
        pool.acquire(conn, repo, stage="plan", holder_id="forged", sha="HEAD", worktrees_root=alias)
    assert conn.execute("SELECT count(*) FROM worktree_slots").fetchone()[0] == 0


def test_expired_lease_requires_terminal_request_dead_pid_and_known_owner(scene):
    _repo, conn, _root = scene
    lease = acquire(scene)
    conn.execute("INSERT INTO requests(request_id,idempotency_key,stage,kind,status,run_id,created_at,updated_at) VALUES('request','request','plan','plan','succeeded',?,'then','then')", (lease["holder_id"],))
    conn.execute("UPDATE worktree_slots SET lease_expires_at='2000-01-01T00:00:00+00:00',owner_pid=2147483647,owner_state='unknown'")
    assert pool.reap_expired(conn)[0]["state"] == "leased"
    conn.execute("UPDATE worktree_slots SET owner_state='stopped'")
    assert pool.reap_expired(conn)[0]["state"] == "free"


def test_ignored_conflict_is_retained_and_checkout_is_quarantined(scene):
    repo, conn, _root = scene
    lease = acquire(scene)
    slot = Path(lease["path"])
    (slot / "cache.txt").write_text("do not overwrite")
    assert release(conn, lease)["state"] == "free"
    (repo / "cache.txt").write_text("tracked next")
    git(repo, "add", "-f", "cache.txt")
    git(repo, "commit", "-qm", "cache tracked")
    with pytest.raises(util.KanpilotError):
        acquire(scene, "second")
    assert (slot / "cache.txt").read_text() == "do not overwrite"
    assert conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "quarantined"


@pytest.mark.parametrize("kind", ["symlink", "tracked"])
def test_artifact_allowlist_cannot_delete_links_or_tracked_files(scene, kind):
    repo, conn, _root = scene
    if kind == "tracked":
        (repo / ".kaneopilot-plan-material").mkdir()
        (repo / ".kaneopilot-plan-material" / "keep.txt").write_text("keep")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "tracked material")
    lease = acquire(scene)
    slot = Path(lease["path"])
    if kind == "symlink":
        (slot / ".kaneopilot-plan-material").symlink_to(repo, target_is_directory=True)
    assert release(conn, lease)["state"] == ("quarantined" if kind == "symlink" else "free")
    assert os.path.lexists(slot / ".kaneopilot-plan-material")


@pytest.mark.parametrize("relative", [".pi/plan/keep.md", ".kaneopilot-plan-material/keep.txt"])
def test_head_tracked_artifact_removed_from_index_is_never_deleted(scene, relative):
    repo, conn, _root = scene
    source = repo / relative
    source.parent.mkdir(parents=True)
    source.write_text("tracked original")
    git(repo, "add", "--", relative)
    git(repo, "commit", "-qm", "tracked product path")
    lease = acquire(scene)
    slot = Path(lease["path"])
    git(slot, "rm", "--cached", "--", relative)
    target = slot / relative
    target.write_bytes(b"user bytes after untracking\r\n")
    assert release(conn, lease)["state"] == "quarantined"
    assert target.read_bytes() == b"user bytes after untracking\r\n"


def test_stale_recovery_cannot_free_a_new_lease(scene):
    _repo, conn, _root = scene
    lease = acquire(scene)
    pool.quarantine(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], "fixture")
    original = pool._git
    replacement = {}
    def race(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if args == ("status", "--porcelain") and not replacement:
            # Simulate another generation winning while a stale caller is
            # between filesystem validation and its final SQLite transition.
            conn.execute("UPDATE worktree_slots SET state='free',reason=NULL")
            with patch.object(pool, "_git", original):
                replacement.update(acquire(scene, "new-holder"))
        return result
    with patch.object(pool, "_git", race):
        with pytest.raises(util.KanpilotError):
            pool.unquarantine(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], confirm=True)
    row = conn.execute("SELECT * FROM worktree_slots").fetchone()
    assert row["state"] == "leased"
    assert row["holder_id"] == "new-holder"
    assert row["lease_token"] == replacement["lease_token"]


def test_forged_slot_path_never_mutates_user_checkout(scene):
    repo, conn, _root = scene
    lease = acquire(scene)
    release(conn, lease)
    (repo / "file.txt").write_text("user data")
    conn.execute("UPDATE worktree_slots SET path=?", (str(repo),))
    with pytest.raises(util.KanpilotError):
        acquire(scene, "second")
    assert (repo / "file.txt").read_text() == "user data"


def test_v5_migration_preserves_records_and_does_not_create_slots(scene, tmp_path):
    _repo, conn, _root = scene
    path = tmp_path / "v5.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA.split("CREATE TABLE IF NOT EXISTS worktree_slots")[0])
    old.execute("INSERT INTO schema_meta VALUES(5)")
    old.execute("INSERT INTO settings VALUES('user-setting','42')")
    old.execute("INSERT INTO events(at,kind,detail_json) VALUES('then','keep','{}')")
    old.commit()
    old.close()
    migrated = db.connect(path, allow_upgrade=True)
    try:
        assert migrated.execute("SELECT version FROM schema_meta").fetchone()[0] == 6
        assert db.get_setting(migrated, "user-setting") == 42
        assert migrated.execute("SELECT kind FROM events").fetchone()[0] == "keep"
        assert migrated.execute("SELECT count(*) FROM worktree_slots").fetchone()[0] == 0
        assert db.get_setting(migrated, "worktree_pool_size") == 4
        assert list((constants.app_support() / "backups").glob("schema-*-v5-to-v6/*"))
    finally:
        migrated.close()


def load_tests(loader, standard_tests, pattern):
    import sys
    from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])


@pytest.mark.parametrize("operation_state", ["prepared", "unknown", "accepted", "rejected"])
@pytest.mark.parametrize("owner_state", ["not_started", "stopped"])
def test_unresolved_slot_git_receipt_blocks_cleanup(scene, operation_state, owner_state):
    from kanpilot import runtime_operations
    _repo, conn, _root = scene
    lease = acquire(scene)
    material = Path(lease["path"]) / ".kaneopilot-plan-material"
    material.mkdir()
    sentinel = material / "pending-git.txt"
    sentinel.write_text("keep until operation is resolved")
    conn.execute("INSERT INTO requests(request_id,idempotency_key,stage,kind,status,run_id,created_at,updated_at) VALUES('request','request','plan','plan','succeeded',?,'then','then')", (lease["holder_id"],))
    conn.execute("UPDATE worktree_slots SET lease_expires_at='2000-01-01T00:00:00+00:00',owner_pid=2147483647,owner_state=?", (owner_state,))
    scope = "worktree-slot:" + lease["slot_id"]
    runtime_operations.begin_operation(conn, scope, "interrupted-git", "worktree_pool", "worktree_checkout", {})
    runtime_operations.complete_operation(conn, scope, "interrupted-git", status=operation_state)
    if operation_state in ("prepared", "unknown"):
        with patch.object(pool, "_artifacts", side_effect=AssertionError("cleanup must not start")):
            assert pool.reap_expired(conn)[0]["state"] == "leased"
            assert release(conn, lease)["state"] == "leased"
        assert sentinel.read_text() == "keep until operation is resolved"
        assert acquire(scene, "next")["path"] != lease["path"]
    else:
        assert pool.reap_expired(conn)[0]["state"] == "free"
        assert not sentinel.exists()


def test_unquarantine_cannot_override_an_unknown_git_operation(scene):
    from kanpilot import runtime_operations
    _repo, conn, _root = scene
    lease = acquire(scene)
    scope = "worktree-slot:" + lease["slot_id"]
    runtime_operations.begin_operation(conn, scope, "lost-result", "worktree_pool", "worktree_checkout", {})
    runtime_operations.mark_operation_unknown(conn, scope, "lost-result")
    pool.quarantine(conn, **{key: lease[key] for key in ("slot_id", "holder_id", "lease_token")}, reason="interrupted")
    with pytest.raises(util.KanpilotError, match="Git操作尚未确认"):
        pool.unquarantine(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], confirm=True)
    assert conn.execute("SELECT state FROM worktree_slots WHERE slot_id=?", (lease["slot_id"],)).fetchone()[0] == "quarantined"


@pytest.mark.parametrize("owner_state", ["not_started", "running", "unknown"])
def test_unexposed_release_reserves_only_a_not_started_owner(scene, owner_state):
    _repo, conn, _root = scene
    lease = acquire(scene)
    artifact = Path(lease["path"]) / ".kaneopilot-plan-material"
    artifact.mkdir()
    (artifact / "keep.txt").write_text("reader may still use this")
    pool.mark_owner(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], state=owner_state)
    result = pool.release(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"],
                          owner_stopped=True, only_not_started=True)
    if owner_state == "not_started":
        assert result["state"] == "free"
        assert not artifact.exists()
    else:
        assert result["state"] == "leased"
        assert result["owner_state"] == owner_state
        assert (artifact / "keep.txt").read_text() == "reader may still use this"
