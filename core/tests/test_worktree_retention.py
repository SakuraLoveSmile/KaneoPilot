"""Isolated retention sweep for task stage trees (protocol §5.29.6)."""
import json
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from helpers import CORE  # noqa: F401
from kanpilot import cli, constants, db, util, worktree_pool as pool

OLD = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()


@pytest.fixture
def scene(tmp_path):
    environment = patch.dict(os.environ, {"KANPILOT_APP_SUPPORT": str(tmp_path / "support")})
    environment.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    pool._git(repo, "init", "-q")
    pool._git(repo, "config", "user.name", "Isolated Retention Fixture")
    pool._git(repo, "config", "user.email", "fixture@example.invalid")
    pool._git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_bytes(b"base\n")
    pool._git(repo, "add", ".")
    pool._git(repo, "commit", "-qm", "base")
    root = tmp_path / "worktrees"
    root.mkdir()
    db_path = tmp_path / "isolated.db"
    conn = db.connect(db_path)
    db.set_setting(conn, "worktrees_root", str(root))
    db.set_setting(conn, "worktree_retention_days", 7)
    yield SimpleNamespace(repo=repo, root=root, conn=conn, db_path=db_path, tmp=tmp_path)
    conn.close()
    environment.stop()


def make_task(scene, task_id="task00001", status="done", stage="plan"):
    now = util.now_iso()
    scene.conn.execute(
        "INSERT INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,"
        "updated_at,extra_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (task_id, "project", "Project", str(scene.repo), "review", status, now, now,
         util.jdump({"delivery": {"branch": constants.DELIVERY_BRANCH_PREFIX + task_id,
                                  "worktree_path": str(scene.root / task_id / "run")}})))
    scene.conn.commit()
    lease = pool.acquire(scene.conn, scene.repo, stage=stage, holder_id="run-1", sha="HEAD",
                         task_id=task_id)
    book = scene.tmp / (task_id + "-plan.md")
    book.write_text("# plan\n", encoding="utf-8")
    db.update_task(scene.conn, task_id, util.now_iso(), plan_path=str(book))
    pool.release(scene.conn, lease["slot_id"], lease["holder_id"], lease["lease_token"],
                 owner_stopped=True)
    db.update_task(scene.conn, task_id, util.now_iso(), status=status)
    scene.conn.execute("UPDATE worktree_slots SET updated_at=? WHERE task_id=?", (OLD, task_id))
    scene.conn.commit()
    return lease, book


def retention(scene, **payload):
    body = {"dry_run": True}
    body.update(payload)
    return cli.run("worktree-retention", body, scene.db_path)


def test_dry_run_lists_expired_task_without_changes(scene):
    lease, _book = make_task(scene)
    result = retention(scene)
    assert result["dry_run"] is True
    assert [item["task_id"] for item in result["eligible"]] == ["task00001"]
    assert Path(lease["path"]).is_dir()
    assert scene.conn.execute("SELECT COUNT(*) FROM worktree_slots").fetchone()[0] == 1


def test_confirm_archives_deletes_and_keeps_meta_and_artifacts(scene):
    lease, book = make_task(scene)
    plan_dir = Path(lease["path"])
    run_dir = scene.root / "task00001" / "run"
    run_dir.mkdir()
    (run_dir / "keep.txt").write_text("execution evidence", encoding="utf-8")
    meta = json.loads((plan_dir.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["task_id"] == "task00001" and "plan" in meta["stages"]
    assert (plan_dir.parent / "artifacts" / "plan" / "run-1" / "plan.md").is_file()

    result = retention(scene, dry_run=False, confirm=True)
    assert result["dry_run"] is False
    assert result["results"][0]["deleted"], result
    assert not plan_dir.exists()
    assert run_dir.is_dir() and (run_dir / "keep.txt").is_file()
    assert (plan_dir.parent / "meta.json").is_file()
    assert (plan_dir.parent / "artifacts" / "plan" / "run-1" / "plan.md").is_file()
    assert scene.conn.execute("SELECT COUNT(*) FROM worktree_slots").fetchone()[0] == 0
    archives = list((scene.root / ".archives").glob("task00001-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], "r:gz") as handle:
        assert "task00001/plan/file.txt" in handle.getnames()


def test_replay_is_idempotent(scene):
    make_task(scene)
    first = retention(scene, dry_run=False, confirm=True)
    again = retention(scene, dry_run=False, confirm=True)
    assert again["eligible"] == []
    assert first["results"][0]["task_id"] == "task00001"


def test_non_terminal_task_is_skipped(scene):
    make_task(scene, status="ready")
    result = retention(scene)
    assert result["eligible"] == []
    assert result["skipped"][0]["blocked"][0]["code"] == "task_not_terminal"


def test_leased_stage_tree_blocks(scene):
    make_task(scene)
    scene.conn.execute("UPDATE worktree_slots SET state='leased'")
    scene.conn.commit()
    result = retention(scene)
    assert result["eligible"] == []
    assert any(item["code"] == "stage_not_free" for item in result["skipped"][0]["blocked"])


def test_dirty_tree_is_quarantined_not_removed(scene):
    lease, _book = make_task(scene)
    plan_dir = Path(lease["path"])
    (plan_dir / "user-change.txt").write_text("unexpected", encoding="utf-8")
    result = retention(scene, dry_run=False, confirm=True)
    assert result["results"][0].get("quarantined"), result
    assert plan_dir.is_dir()
    assert (plan_dir / "user-change.txt").is_file()
    assert scene.conn.execute("SELECT state FROM worktree_slots").fetchone()[0] == "quarantined"


def test_missing_directory_is_forgotten_without_archive(scene):
    lease, _book = make_task(scene)
    import shutil
    shutil.rmtree(lease["path"])
    result = retention(scene, dry_run=False, confirm=True)
    assert result["results"][0]["forgotten"] is True
    assert scene.conn.execute("SELECT COUNT(*) FROM worktree_slots").fetchone()[0] == 0
    assert not (scene.root / ".archives").exists()
