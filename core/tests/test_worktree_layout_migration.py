"""Isolated fixture for ``worktree-layout-migrate`` (no app, engine or models)."""
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from helpers import CORE  # noqa: F401
from kanpilot import cli, constants, db, util, worktree_pool as pool


def git(root, *args):
    return pool._git(root, *args)


def head(repo):
    return pool._value(Path(repo), "rev-parse", "HEAD")


@pytest.fixture
def scene(tmp_path):
    environment = patch.dict(os.environ, {"KANPILOT_APP_SUPPORT": str(tmp_path / "support")})
    environment.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Isolated Migration Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_bytes(b"base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    root = tmp_path / "worktrees"
    root.mkdir()
    db_path = tmp_path / "isolated.db"
    conn = db.connect(db_path)
    db.set_setting(conn, "worktrees_root", str(root))
    yield SimpleNamespace(repo=repo, root=root, conn=conn, db_path=db_path, tmp=tmp_path)
    conn.close()
    environment.stop()


def make_task(scene, task_id="task00001"):
    branch = constants.DELIVERY_BRANCH_PREFIX + task_id
    path = scene.root / task_id / "branch"
    path.parent.mkdir(parents=True)
    git(scene.repo, "worktree", "add", "-b", branch, str(path), "HEAD")
    delivery = {"branch": branch, "worktree_path": str(path), "head_sha": head(scene.repo)}
    scene.conn.execute(
        "INSERT INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,"
        "updated_at,extra_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (task_id, "project", "Project", str(scene.repo), "review", "in_review", "then", "then",
         util.jdump({"delivery": delivery})))
    scene.conn.commit()
    return path, delivery


def make_legacy_pool(scene):
    key = "0123456789abcdef"
    slot = scene.root / "pool" / key / "slot-1"
    slot.mkdir(parents=True)
    (slot / "leftover.txt").write_text("legacy pool evidence", encoding="utf-8")
    scene.conn.execute(
        "INSERT INTO worktree_slots(slot_id,repo_key,path,repository,git_common_dir,"
        "worktrees_root,state,updated_at) VALUES('legacy-row',?,?,?,?,?,'free','then')",
        (key, str(slot), str(scene.repo), str(scene.repo / ".git"), str(scene.root)))
    scene.conn.commit()
    return slot


def plan(scene, **overrides):
    payload = {"backup_dir": str(scene.tmp / "backup"), "operation_id": "layout-1"}
    payload.update(overrides)
    return cli.run("worktree-layout-migrate", payload, scene.db_path)


def delivery_of(scene, task_id="task00001"):
    task = db.require_task(scene.conn, task_id)
    return db.task_extra(task)["delivery"]


def test_dry_run_reports_plan_without_touching_filesystem(scene):
    branch, _delivery = make_task(scene)
    pool_dir = make_legacy_pool(scene)
    result = plan(scene)
    assert result["dry_run"] is True
    assert result["status"] == "planned", result["blockers"]
    assert [item["task_id"] for item in result["moves"]] == ["task00001"]
    assert result["moves"][0]["from"] == str(branch) and result["moves"][0]["to"] == str(branch.parent / "run")
    assert result["pool"] == str(scene.root / "pool")
    assert result["legacy_rows"] == 1
    assert branch.is_dir() and pool_dir.is_dir()
    assert not (scene.tmp / "backup").exists()


def test_confirm_moves_execution_tree_and_archives_legacy_pool(scene):
    branch, delivery = make_task(scene)
    make_legacy_pool(scene)
    planned = plan(scene)
    result = plan(scene, dry_run=False, confirm=True,
                  expected_audit_sha256=planned["audit_sha256"])
    assert result["ok"] is True
    target = branch.parent / "run"
    assert target.is_dir() and not branch.exists()
    assert git(scene.repo, "worktree", "list").count(b"run") >= 1
    assert pool._value(target, "rev-parse", "--abbrev-ref", "HEAD") == delivery["branch"]
    assert delivery_of(scene)["worktree_path"] == str(target)
    assert not (scene.root / "pool").exists()
    archives = list((scene.root / ".archives").glob("pool-legacy-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], "r:gz") as handle:
        assert "pool/0123456789abcdef/slot-1/leftover.txt" in handle.getnames()
    assert scene.conn.execute(
        "SELECT COUNT(*) FROM worktree_slots WHERE task_id IS NULL").fetchone()[0] == 0
    receipt = db.get_runtime_operation(scene.conn, "worktree-layout-migrate", "layout-1")
    assert receipt["status"] == "accepted"
    assert json.loads(receipt["result_json"])["moved"][0]["task_id"] == "task00001"


def test_replay_returns_recorded_result(scene):
    make_task(scene)
    planned = plan(scene)
    first = plan(scene, dry_run=False, confirm=True, expected_audit_sha256=planned["audit_sha256"])
    again = plan(scene, dry_run=False, confirm=True, expected_audit_sha256=planned["audit_sha256"])
    assert again["replayed"] is True
    assert again["moved"] == first["moved"]


def test_active_request_blocks_and_confirm_refuses(scene):
    make_task(scene)
    scene.conn.execute(
        "INSERT INTO requests(request_id,idempotency_key,stage,kind,status,created_at,updated_at) "
        "VALUES('req','key','plan','plan','queued','then','then')")
    scene.conn.commit()
    planned = plan(scene)
    assert planned["status"] == "blocked"
    assert any(item["code"] == "request_active" for item in planned["blockers"])
    with pytest.raises(util.KanpilotError):
        plan(scene, dry_run=False, confirm=True, expected_audit_sha256=planned["audit_sha256"])


def test_leased_stage_tree_blocks(scene):
    make_task(scene)
    scene.conn.execute(
        "INSERT INTO worktree_slots(slot_id,task_id,stage,repo_key,path,repository,git_common_dir,"
        "worktrees_root,state,updated_at) VALUES('stage-row','task00002','plan','k',?,?,?,?,'leased','then')",
        (str(scene.root / "task00002" / "plan"), str(scene.repo), str(scene.repo / ".git"),
         str(scene.root)))
    scene.conn.commit()
    planned = plan(scene)
    assert planned["status"] == "blocked"
    assert any(item["code"] == "stage_tree_leased" for item in planned["blockers"])


def test_audit_change_refuses_confirm(scene):
    make_task(scene)
    planned = plan(scene)
    make_task(scene, task_id="task00009")
    with pytest.raises(util.KanpilotError):
        plan(scene, dry_run=False, confirm=True, expected_audit_sha256=planned["audit_sha256"])


def test_failure_keeps_state_and_writes_unknown_receipt(scene):
    branch, delivery = make_task(scene)
    planned = plan(scene)
    with patch("kanpilot.cmd_worktree_layout._move_worktree",
               side_effect=RuntimeError("git move failed")):
        with pytest.raises(util.KanpilotError):
            plan(scene, dry_run=False, confirm=True, expected_audit_sha256=planned["audit_sha256"])
    assert branch.is_dir() and not (branch.parent / "run").exists()
    assert delivery_of(scene)["worktree_path"] == delivery["worktree_path"]
    receipt = db.get_runtime_operation(scene.conn, "worktree-layout-migrate", "layout-1")
    assert receipt["status"] == "unknown"


def test_backup_inside_root_is_refused(scene):
    make_task(scene)
    with pytest.raises(util.KanpilotError):
        plan(scene, backup_dir=str(scene.root / "backup"))
