"""Manual Plus recovery against disposable repositories, jobs and backups."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_worktree_pool import scene, acquire, stopped_checkpoint  # noqa: F401
from kanpilot import cli, db, runtime_operations, task_workspace, util
from kanpilot import worktree_pool as pool, worktree_recovery as recovery


def backup(directory, lease):
    directory.mkdir()
    slot = Path(lease["path"])
    archive = directory / (slot.name + ".tar.gz")
    with tarfile.open(archive, "w:gz", dereference=False) as handle:
        handle.add(slot, arcname=slot.name)
    manifest = {"slots": [{"slot_id": lease["slot_id"], "holder_id": lease["holder_id"],
                            "path": lease["path"], "sha": lease["sha"], "archive": archive.name,
                            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}]}
    (directory / "manifest.json").write_text(util.jdump(manifest))


@pytest.fixture(params=["plan", "review"])
def abandoned(scene, request):
    _repo, conn, root = scene
    lease = acquire(scene, stage=request.param)
    original, directory, job = stopped_checkpoint(scene, lease, backend="pi_desktop", status="failed")
    job.pop("owner_stopped")
    material = Path(lease["path"]) / ".kaneopilot-plan-material"
    material.mkdir()
    (material / "input.txt").write_text("unique planning attachment")
    job["material_dir"] = str(material)
    (Path(lease["path"]) / "cache.txt").write_text("ignored user cache remains")
    task_workspace.pool_owner(conn, job, state="unknown")
    path = directory / "job.json"
    path.write_text(util.jdump(job))
    saved = root.parent / "backup"
    backup(saved, lease)
    payload = {k: lease[k] for k in ("slot_id", "holder_id", "lease_token")}
    payload.update(operation_id="manual-fixture", backup_dir=str(saved),
                   reason="Explicit fixture human authorization")
    ctx = SimpleNamespace(conn=conn, db_path=root.parent / "isolated.db")
    return SimpleNamespace(ctx=ctx, conn=conn, lease=lease, request=original,
                           path=path, job=job, directory=directory, payload=payload, backup=saved)


def execute(case, **changes):
    return recovery.cmd_worktree_slot_recover(case.ctx, {**case.payload, "dry_run": False,
                                                        "confirm": True, **changes})


def test_preview_is_read_only_and_cli_registration_uses_same_guard(abandoned):
    case = abandoned
    before = case.conn.total_changes
    job = case.path.read_bytes()
    result = recovery.cmd_worktree_slot_recover(case.ctx, case.payload)
    assert result["state"] == "ready" and result["plus_terminal"] == "unknown"
    assert case.conn.total_changes == before and case.path.read_bytes() == job
    assert not (case.directory / ".settle.lock").exists()
    assert case.lease["lease_token"] not in json.dumps(result)
    assert cli.run("worktree-slot-recover", case.payload, case.ctx.db_path) == result


def test_manual_recovery_preserves_material_and_cache_without_fabricating_terminal(abandoned):
    case = abandoned
    result = execute(case)
    assert result["state"] == "free"
    saved = json.loads(case.path.read_text())
    assert "owner_stopped" not in saved
    assert saved["pool_released"] is True
    assert saved["pool_manual_recovery"]["plus_terminal"] == "unknown"
    assert Path(saved["material_dir"]).joinpath("input.txt").read_text() == "unique planning attachment"
    assert (Path(case.lease["path"]) / "cache.txt").read_text() == "ignored user cache remains"
    assert not (Path(case.lease["path"]) / ".kaneopilot-plan-material").exists()
    receipt = case.conn.execute("SELECT * FROM runtime_operations WHERE tool='worktree-slot-recover'").fetchone()
    assert receipt["status"] == "accepted" and receipt["harness_id"] == "core_manual"
    assert json.loads(receipt["result_json"])["recovery_kind"] == "human_confirmed"
    assert case.lease["lease_token"] not in receipt["result_json"]
    with patch.object(pool, "release", side_effect=AssertionError("must not release twice")):
        assert execute(case) == result
    # An accepted receipt remains readable without accessing a reused slot.
    case.conn.execute("UPDATE worktree_slots SET holder_id='new-run',lease_token='new-generation',state='leased'")
    with patch.object(recovery, "_preflight", side_effect=AssertionError("must not access new checkout")):
        assert execute(case) == result
    assert case.conn.execute("SELECT holder_id FROM worktree_slots").fetchone()[0] == "new-run"


@pytest.mark.parametrize("damage", ["confirm", "live_pid", "job", "lease", "session", "turn",
                                    "request", "backend", "new_generation", "pending_operation"])
def test_manual_recovery_rejects_ineligible_or_changed_identity(abandoned, damage):
    case = abandoned
    if damage == "confirm":
        changes = {"confirm": False}
    else:
        changes = {}
        if damage == "live_pid":
            case.job["pid"] = os.getpid()
            case.conn.execute("UPDATE worktree_slots SET owner_pid=?", (os.getpid(),))
        elif damage in ("job", "session", "turn"):
            case.job[{"job": "run_id", "session": "session_id", "turn": "turn_id"}[damage]] = "foreign"
        elif damage == "lease":
            case.job["pool_lease"]["lease_token"] = "foreign"
        elif damage == "request":
            db.update_request(case.conn, case.request["request_id"], util.now_iso(), status="claimed")
        elif damage == "backend":
            case.job["backend"] = "codex_desktop"
        elif damage == "new_generation":
            case.conn.execute("UPDATE worktree_slots SET lease_token='new-generation'")
        else:
            runtime_operations.begin_operation(case.conn, "worktree-slot:" + case.lease["slot_id"],
                                               "unresolved", "worktree_pool", "worktree_checkout", {})
        case.path.write_text(util.jdump(case.job))
    with pytest.raises(util.KanpilotError):
        execute(case, **changes)
    assert case.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "leased"
    assert (Path(case.lease["path"]) / ".kaneopilot-plan-material" / "input.txt").exists()
    assert not case.conn.execute("SELECT 1 FROM runtime_operations WHERE tool='worktree-slot-recover'").fetchone()


@pytest.mark.parametrize("damage", ["archive", "changed", "extra", "missing", "mode", "manifest_identity"])
def test_backup_must_authenticate_current_complete_contents(abandoned, damage):
    case = abandoned
    slot = Path(case.lease["path"])
    if damage == "archive":
        archive = next(case.backup.glob("*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"tampered")
    elif damage == "changed":
        (slot / "cache.txt").write_text("new user content")
    elif damage == "extra":
        (slot / "new-attachment.txt").write_text("not backed up")
    elif damage == "missing":
        (slot / "cache.txt").unlink()
    elif damage == "mode":
        (slot / "cache.txt").chmod(0o700)
    else:
        path = case.backup / "manifest.json"
        value = json.loads(path.read_text())
        value["slots"][0]["holder_id"] = "foreign"
        path.write_text(util.jdump(value))
    with pytest.raises(util.KanpilotError):
        execute(case)
    assert case.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "leased"


def test_manual_receipt_does_not_turn_unknown_owner_into_automatic_stop_proof(abandoned):
    case = abandoned
    case.conn.execute("UPDATE worktree_slots SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    assert pool.reap_expired(case.conn)[0]["state"] == "leased"
    assert "owner_stopped" not in json.loads(case.path.read_text())


def test_unknown_user_changes_are_quarantined_and_not_deleted(abandoned):
    case = abandoned
    slot = Path(case.lease["path"])
    unknown = slot / "user-notes.txt"
    unknown.write_text("retain user notes")
    # A current complete backup is necessary but is not permission to discard.
    import shutil
    shutil.rmtree(case.backup)
    backup(case.backup, case.lease)
    assert execute(case)["state"] == "quarantined"
    assert unknown.read_text() == "retain user notes"


def test_interrupted_manual_operation_is_not_replayed_blindly(abandoned):
    case = abandoned
    with patch.object(task_workspace, "preserve_pool_artifacts", side_effect=OSError("fixture copy failure")):
        with pytest.raises(OSError):
            execute(case)
    assert case.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "leased"
    receipt = case.conn.execute("SELECT * FROM runtime_operations WHERE tool='worktree-slot-recover'").fetchone()
    assert receipt["status"] == "unknown"
    intent = json.loads(receipt["result_json"])
    assert intent["reason"] == case.payload["reason"]
    assert intent["request_id"] == case.request["request_id"]
    assert intent["intent"]["owner_pid"] == case.job["pid"]
    assert intent["intent"]["session_id"] == case.job["session_id"]
    assert intent["plus_terminal"] == "unknown" and intent["backup_sha256"]
    assert case.lease["lease_token"] not in receipt["result_json"]
    with pytest.raises(util.KanpilotError, match="前次人工恢复"):
        execute(case)
    assert "owner_stopped" not in json.loads(case.path.read_text())


def test_owner_change_at_release_reservation_preserves_the_checkout(abandoned):
    case = abandoned
    original = pool.release

    def race(*args, **kwargs):
        case.conn.execute("UPDATE worktree_slots SET owner_state='running',owner_pid=?", (os.getpid(),))
        return original(*args, **kwargs)

    with patch.object(pool, "release", side_effect=race):
        with pytest.raises(util.KanpilotError, match="持有者状态已变化"):
            execute(case)
    assert case.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "leased"
    assert (Path(case.lease["path"]) / ".kaneopilot-plan-material" / "input.txt").exists()
    assert "owner_stopped" not in json.loads(case.path.read_text())


def test_other_manual_intent_appearing_after_outer_check_blocks_side_effects(abandoned):
    case = abandoned
    original = recovery._preflight
    calls = []

    def race(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 1:
            scope = "manual-worktree-recovery:" + case.lease["slot_id"]
            runtime_operations.begin_operation(case.conn, scope, "other-manual", "core_manual",
                                               "worktree-slot-recover", {"fixture": "interrupted"})
            runtime_operations.mark_operation_unknown(case.conn, scope, "other-manual")
        return result

    with patch.object(recovery, "_preflight", side_effect=race), \
            patch.object(task_workspace, "preserve_pool_artifacts", side_effect=AssertionError("must not copy")):
        with pytest.raises(util.KanpilotError, match="未确认的人工恢复"):
            execute(case)
    assert not case.conn.execute("SELECT 1 FROM runtime_operations WHERE operation_id='manual-fixture'").fetchone()
    assert case.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "leased"


@pytest.mark.parametrize("field", ["task_id", "run_id", "stage", "status", "payload_json"])
def test_request_change_at_release_reservation_preserves_checkout(abandoned, field):
    case = abandoned
    original = pool.release

    def race(*args, **kwargs):
        value = "claimed" if field == "status" else "{}" if field == "payload_json" else "foreign"
        if field == "task_id":
            case.conn.execute("INSERT INTO tasks(task_id,project_id,project_name,stage,status,created_at,updated_at) "
                              "VALUES('foreign','project','fixture','plan','needs_user','then','then')")
        case.conn.execute("UPDATE requests SET " + field + "=? WHERE request_id=?", (value, case.request["request_id"]))
        return original(*args, **kwargs)

    with patch.object(pool, "release", side_effect=race):
        with pytest.raises(util.KanpilotError, match="原请求身份或冻结输入已变化"):
            execute(case)
    assert case.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "leased"
    assert (Path(case.lease["path"]) / ".kaneopilot-plan-material" / "input.txt").exists()
