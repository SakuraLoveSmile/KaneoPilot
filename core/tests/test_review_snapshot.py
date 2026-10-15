"""Review snapshots against disposable local Git repos; no model or product runs."""
import os
import subprocess

import pytest

from helpers import CORE  # noqa: F401
from kanpilot import pi_desktop_review as review, util, db


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True).stdout


@pytest.fixture
def scene(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Isolated Snapshot Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "core.filemode", "true")
    for name, body in {"tracked.txt": b"base\n", "deleted.txt": b"remove me\n",
                       "data.bin": b"\x00base\x01", "tool.sh": b"#!/bin/sh\nexit 0\n"}.items():
        (repo / name).write_bytes(body)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    execution = tmp_path / "execution"
    git(repo, "worktree", "add", "--detach", str(execution), "HEAD")
    root = tmp_path / "worktrees" / "task1234" / "review-manual"
    task = {"task_id": "task1234", "repository": str(repo), "delivery": {"mode": "manual"}}
    return repo, execution, root, task


@pytest.fixture
def pooled(scene, tmp_path, monkeypatch):
    repository, execution, _root, task = scene
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(tmp_path / "support"))
    database = tmp_path / "snapshot.db"
    conn = db.connect(database)
    now = util.now_iso()
    with db.write_txn(conn):
        db.set_setting(conn, "worktrees_root", str(tmp_path / "worktrees"))
        conn.execute("INSERT INTO requests(request_id,idempotency_key,stage,kind,status,run_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     ("review-request", "fixture", "review", "review", "claimed", "review-run", now, now))
    directory = tmp_path / "invocation"
    directory.mkdir()
    job = {"request_id": "review-request", "run_id": "review-run", "invocation_dir": str(directory)}
    def prepare(snapshot=None):
        return review.prepare_pooled_worktree(conn, database, job, task, execution, snapshot)
    yield prepare
    conn.close()


def test_manual_snapshot_includes_full_worktree_without_touching_source(scene, pooled):
    repo, execution, root, task = scene
    (execution / "tracked.txt").write_bytes(b"staged\n")
    git(execution, "add", "tracked.txt")
    (execution / "tracked.txt").write_bytes(b"staged plus unstaged\n")
    (execution / "staged-new.txt").write_bytes(b"staged addition\n")
    git(execution, "add", "staged-new.txt")
    (execution / "staged-new.txt").write_bytes(b"staged addition plus unstaged\n")
    (execution / "data.bin").write_bytes(b"\x00changed\xff\x01")
    (execution / "deleted.txt").unlink()
    (execution / "tool.sh").chmod(0o755)
    (execution / "test").mkdir()
    (execution / "test" / "new test.py").write_bytes(b"assert True\n")
    (execution / "test" / "new test.py").chmod(0o755)
    before_head = git(execution, "rev-parse", "HEAD")
    before_status = git(execution, "status", "--porcelain")
    before_index = git(execution, "diff", "--cached", "--binary")
    before_diff = git(execution, "diff", "HEAD", "--binary", "--full-index", "--no-color")

    root = pooled()

    assert git(root, "rev-parse", "HEAD") == before_head
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == b"HEAD"
    assert git(root, "diff", "HEAD", "--binary", "--full-index", "--no-color") == before_diff
    for name in ("tracked.txt", "staged-new.txt", "data.bin", "tool.sh", "test/new test.py"):
        assert (root / name).read_bytes() == (execution / name).read_bytes()
        assert os.access(root / name, os.X_OK) == os.access(execution / name, os.X_OK)
    assert not (root / "deleted.txt").exists()
    assert git(execution, "rev-parse", "HEAD") == before_head
    assert git(execution, "status", "--porcelain") == before_status
    assert git(execution, "diff", "--cached", "--binary") == before_index
    assert git(repo, "status", "--porcelain") == b""


def test_manual_snapshot_reuses_only_matching_content(scene, pooled):
    _repo, execution, root, task = scene
    (execution / "tracked.txt").write_bytes(b"reviewed work\n")
    (execution / "new.py").write_bytes(b"assert True\n")
    root = pooled()
    before = (root / "tracked.txt").read_bytes(), (root / "new.py").read_bytes()
    assert pooled() == root
    assert before == ((root / "tracked.txt").read_bytes(), (root / "new.py").read_bytes())
    (execution / "tracked.txt").write_bytes(b"later source edit\n")
    with pytest.raises(util.KanpilotError):
        pooled()
    assert (root / "tracked.txt").read_bytes() == before[0]


def test_manual_snapshot_rejects_existing_old_head_snapshot_without_repairing_it(scene):
    repo, execution, root, task = scene
    (execution / "tracked.txt").write_bytes(b"dirty execution change\n")
    (execution / "new.py").write_bytes(b"assert True\n")
    root.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "--detach", str(root), "HEAD")
    assert review.prepare_worktree(task, execution, root) is not None
    assert (root / "tracked.txt").read_bytes() == b"base\n"
    assert not (root / "new.py").exists()
    assert git(root, "status", "--porcelain") == b""


def test_frozen_manual_input_rejects_source_changes_before_preparation(scene):
    from kanpilot import review_snapshot
    _repo, execution, root, task = scene
    (execution / "tracked.txt").write_bytes(b"frozen body\n")
    added = execution / "new.py"
    added.write_bytes(b"frozen new test\n")
    frozen = review_snapshot.freeze(execution)
    added.write_bytes(b"later new test\n")
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_source(execution, frozen)
    assert review.prepare_worktree(task, execution, root, snapshot=frozen) is not None


def test_frozen_manual_snapshot_verifies_untracked_executable_mode(scene, pooled):
    from kanpilot import review_snapshot
    _repo, execution, root, task = scene
    added = execution / "new.py"
    added.write_bytes(b"print('fixture')\n")
    added.chmod(0o644)
    frozen = review_snapshot.freeze(execution)
    root = pooled(frozen)
    review_snapshot.verify_root(root, frozen)
    (root / "new.py").chmod(0o755)
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_root(root, frozen)
    added.chmod(0o755)
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_source(execution, frozen)


def test_materials_still_validate_frozen_untracked_file_when_git_now_ignores_it(scene, pooled):
    from kanpilot import review_snapshot
    repo, execution, root, task = scene
    (execution / "new.py").write_bytes(b"frozen new test\n")
    frozen = review_snapshot.freeze(execution)
    root = pooled(frozen)
    materials = repo.parent / "materials"
    materials.mkdir()
    files = review_snapshot.write_materials(materials, frozen)
    # Git's ignore rules can hide a file from a fresh ls-files fingerprint.
    # Frozen material validation must still read that originally listed file.
    (repo / ".git" / "info" / "exclude").write_text("new.py\n")
    (root / "new.py").write_bytes(b"hidden changed test\n")
    assert review_snapshot.freeze(root)["untracked"] == []
    from_materials = review_snapshot.from_materials(files, frozen["head"])
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_root(root, from_materials)


def test_crlf_tracked_bytes_survive_snapshot_and_materials_detect_normalization(scene, pooled):
    from kanpilot import review_snapshot
    repo, execution, root, task = scene
    (repo / ".gitattributes").write_text("*.txt text eol=lf\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "isolated LF attributes")
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    git(execution, "checkout", "--detach", head)
    raw = b"changed CRLF line\r\nsecond CRLF line\r\n"
    (execution / "tracked.txt").write_bytes(raw)
    frozen = review_snapshot.freeze(execution)
    root = pooled(frozen)
    assert (root / "tracked.txt").read_bytes() == raw
    materials = repo.parent / "materials"
    materials.mkdir()
    files = review_snapshot.write_materials(materials, frozen)
    recovered = review_snapshot.from_materials(files, frozen["head"])
    review_snapshot.verify_root(root, recovered)
    canonical_before = git(root, "diff", "HEAD", "--binary", "--full-index", "--no-color")
    (root / "tracked.txt").write_bytes(raw.replace(b"\r\n", b"\n"))
    assert git(root, "diff", "HEAD", "--binary", "--full-index", "--no-color") == canonical_before
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_root(root, recovered)
    (execution / "tracked.txt").write_bytes(raw.replace(b"\r\n", b"\n"))
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_source(execution, frozen)


def test_pr_snapshot_still_uses_registered_clean_commit_and_rejects_dirty_execution(scene, pooled):
    repo, execution, manual_root, task = scene
    git(execution, "switch", "-c", "kaneo/task1234")
    (execution / "tracked.txt").write_bytes(b"committed delivery\n")
    git(execution, "add", "tracked.txt")
    git(execution, "commit", "-qm", "delivery")
    head = git(execution, "rev-parse", "HEAD").decode().strip()
    root = manual_root.with_name("review-" + head[:12])
    task["run_id"] = "execution-fixture"
    task["delivery"] = {"mode": "pr", "branch": "kaneo/task1234",
                        "worktree_path": str(execution), "head_sha": head,
                        "base_branch": "main", "base_sha": git(execution, "rev-parse", "HEAD^").decode().strip(),
                        "commits": [{"execution_run_id": task["run_id"], "sha": head}]}
    root = pooled()
    assert (root / "tracked.txt").read_bytes() == b"committed delivery\n"
    assert git(root, "status", "--porcelain") == b""
    (execution / "tracked.txt").write_bytes(b"uncommitted later change\n")
    with pytest.raises(util.KanpilotError):
        pooled()
    assert (root / "tracked.txt").read_bytes() == b"committed delivery\n"
