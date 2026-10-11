"""Frozen branch Review guard: real local Git, no DB or model calls."""
import subprocess

import pytest

from kanpilot import core, git_delivery, pi_desktop_review as review, util


@pytest.fixture
def frozen_branch(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repository), *args], check=True,
                              capture_output=True, env=git_delivery.git_env()).stdout.decode().strip()
    git("init", "-q")
    git("config", "user.name", "Review Guard Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "app.py").write_text("baseline\n")
    (repository / "tracked.pyc").write_bytes(b"tracked artifact")
    git("add", ".")
    git("commit", "-qm", "baseline")
    head = git("rev-parse", "HEAD")
    git("checkout", "--detach", "-q", head)
    snapshot = {"head_sha": head}
    monkeypatch.setattr(core, "branch_review_snapshot", lambda task: snapshot)
    monkeypatch.setattr(core, "require_committed_review", lambda *args: None)
    job = {"review_branch_snapshot": dict(snapshot), "execution_run_id": "execution",
           "material_hashes": {}, "review_root": str(repository)}
    return repository, git, job


@pytest.mark.parametrize("change", ["tracked_modify", "tracked_delete", "untracked", "artifact_rename"])
def test_build_artifact_changes_are_allowed(frozen_branch, change):
    root, git, job = frozen_branch
    if change == "tracked_modify":
        (root / "tracked.pyc").write_bytes(b"generated again")
    elif change == "tracked_delete":
        (root / "tracked.pyc").unlink()
    elif change == "artifact_rename":
        git("mv", "tracked.pyc", "renamed.pyc")
    else:
        cache = root / "nested" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "评审 cache\nmodule.pyc").write_bytes(b"generated")
    assert git("status", "--porcelain")
    review._guard_branch_job(job, {})


@pytest.mark.parametrize("change", ["tracked_source", "untracked_source", "head", "attached",
                                   "rename_source", "rename_target", "untracked_quoted_source"])
def test_source_and_identity_changes_remain_blocked(frozen_branch, change):
    root, git, job = frozen_branch
    if change == "tracked_source":
        (root / "app.py").write_text("changed\n")
    elif change == "untracked_source":
        (root / "new.py").write_text("new\n")
    elif change == "head":
        git("commit", "--allow-empty", "-qm", "another head")
    elif change == "attached":
        git("checkout", "-qb", "attached")
    elif change == "rename_source":
        git("mv", "app.py", "app.pyc")
    elif change == "rename_target":
        git("mv", "tracked.pyc", "restored.py")
    else:
        (root / "源码 name\nfile.py").write_text("new\n")
    with pytest.raises(util.KanpilotError) as caught:
        review._guard_branch_job(job, {})
    assert caught.value.code == "review_blocked"


def test_git_status_failure_remains_blocked(frozen_branch, monkeypatch):
    _, _, job = frozen_branch
    def failed_status(root):
        raise git_delivery.GitDeliveryError("delivery_commit_failed", "fixture failure")
    monkeypatch.setattr(git_delivery, "worktree_changes", failed_status)
    with pytest.raises(util.KanpilotError) as caught:
        review._guard_branch_job(job, {})
    assert caught.value.code == "review_blocked"
