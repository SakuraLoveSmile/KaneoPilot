"""Delivery Git acceptance against disposable local bare repositories."""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import CORE
from kanpilot import git_delivery as gd


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, env=gd.git_env()).stdout.decode().strip()


def make_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    bare, repo = root / "origin.git", root / "repo"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "Delivery Test")
    git(repo, "config", "user.email", "delivery@example.invalid")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    return repo


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path)
    # Existing user changes in the primary checkout must survive every operation.
    (repo / "primary-untracked.txt").write_text("preserve me")
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))
    yield repo
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))


def branch(repo, root, task_id="task1234", base=None):
    return dict(gd.prepare_branch_worktree(str(repo), task_id, base_branch=base,
                                         worktrees_root=root), repository=str(repo))


def commit(delivery, run="run1"):
    return gd.commit_worktree(delivery, task_id="task1234", title="修复反馈",
                              execution_run_id=run, fix_round=0)


def test_branch_base_and_snapshot_reuse(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    assert delivery["base_branch"] == "main"
    assert delivery["branch"] == "kaneo/task1234"
    assert gd.resolve_base(str(repo), "main") == ("main", delivery["base_sha"])
    preferred = branch(repo, tmp_path / "worktrees", task_id="task5678", base="main")
    assert preferred["base_branch"] == "main"
    gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    snapshot = gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                           purpose="review", worktrees_root=tmp_path / "worktrees")
    assert snapshot == gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                                    purpose="review", worktrees_root=tmp_path / "worktrees")
    assert git(snapshot, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    plan = gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                       purpose="plan", worktrees_root=tmp_path / "worktrees")
    assert Path(plan).name == "plan-" + delivery["head_sha"][:12]
    (Path(snapshot) / "dirty").touch()
    with pytest.raises(gd.GitDeliveryError, match="已有快照"):
        gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                     purpose="review", worktrees_root=tmp_path / "worktrees")


def plan_snapshot(repo, root):
    return Path(gd.prepare_snapshot_worktree(str(repo), "task1234", sha=git(repo, "rev-parse", "HEAD"),
                                            purpose="plan", worktrees_root=root))


def test_plan_snapshot_reuses_two_runs_of_regular_materials(repo, tmp_path):
    root = tmp_path / "worktrees"
    original_config = (repo / ".git" / "config").read_bytes()
    original_exclude = (repo / ".git" / "info" / "exclude").read_bytes()
    path = plan_snapshot(repo, root)
    for run in ("plan-run-one", "plan-run-two"):
        materials = path / ".kaneopilot-plan-material" / run
        materials.mkdir(parents=True)
        (materials / "manifest.json").write_text('{"files": []}')
        (materials / "screen shot.png").write_bytes(b"image fixture")
        assert plan_snapshot(repo, root) == path
    assert (path / ".kaneopilot-plan-material" / "plan-run-one" / "manifest.json").is_file()
    assert git(path, "diff", "--name-only") == ""
    assert git(path, "diff", "--cached", "--name-only") == ""
    assert (repo / ".git" / "config").read_bytes() == original_config
    assert (repo / ".git" / "info" / "exclude").read_bytes() == original_exclude


@pytest.mark.parametrize("kind", ["tracked", "outside", "file-link", "directory-link", "root-link", "ignored-link", "fifo"])
def test_plan_snapshot_rejects_other_changes_and_unsafe_materials(repo, tmp_path, kind):
    root = tmp_path / "worktrees"
    path = plan_snapshot(repo, root)
    materials = path / ".kaneopilot-plan-material" / "run1"
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret").write_text("external material")
    if kind == "tracked":
        (path / "tracked.txt").write_text("code change")
    elif kind == "outside":
        (path / "outside.json").write_text("not runner material")
    elif kind == "root-link":
        (path / ".kaneopilot-plan-material").symlink_to(external, target_is_directory=True)
    else:
        materials.mkdir(parents=True)
        if kind == "file-link":
            (materials / "attachment").symlink_to(external / "secret")
        elif kind == "directory-link":
            (materials / "nested").symlink_to(external, target_is_directory=True)
        elif kind == "ignored-link":
            # Worktree-local ignore entry demonstrates that filesystem checking
            # is required even when porcelain omits the linked attachment.
            (materials / ".gitignore").write_text("attachment\n")
            (materials / "attachment").symlink_to(external / "secret")
        else:
            os.mkfifo(materials / "attachment")
    with pytest.raises(gd.GitDeliveryError) as error:
        plan_snapshot(repo, root)
    assert error.value.code == "worktree_invalid"


def test_review_and_branch_do_not_allow_plan_materials(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    for purpose in ("branch", "review"):
        path = Path(delivery["worktree_path"]) if purpose == "branch" else Path(gd.prepare_snapshot_worktree(
            str(repo), "task1234", sha=delivery["head_sha"], purpose="review", worktrees_root=tmp_path / "worktrees"))
        materials = path / ".kaneopilot-plan-material" / "run1"
        materials.mkdir(parents=True)
        (materials / "manifest.json").write_text("{}")
        with pytest.raises(gd.GitDeliveryError):
            if purpose == "branch":
                gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
            else:
                gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                             purpose="review", worktrees_root=tmp_path / "worktrees")


def test_worktree_state_hash_content_status_delete_and_rename(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    clean = gd.worktree_state_sha256(delivery)
    assert len(clean) == 64
    assert clean == gd.worktree_state_sha256(delivery)
    (wt / "tracked.txt").write_text("same length 1")
    changed = gd.worktree_state_sha256(delivery)
    (wt / "tracked.txt").write_text("same length 2")
    assert changed != gd.worktree_state_sha256(delivery)
    (wt / "tracked.txt").unlink()
    deleted = gd.worktree_state_sha256(delivery)
    assert deleted not in (clean, changed)
    (wt / "tracked.txt").write_text("base\n")
    assert gd.worktree_state_sha256(delivery) == clean
    git(wt, "mv", "tracked.txt", "renamed file.txt")
    renamed = gd.worktree_state_sha256(delivery)
    assert renamed not in (clean, changed, deleted)
    assert {x["path"] for x in gd.worktree_changes(str(wt))} == {"tracked.txt", "renamed file.txt"}
    (wt / "new-untracked.bin").write_bytes(b"a" * (3 * 1024 * 1024))
    untracked = gd.worktree_state_sha256(delivery)
    assert untracked != renamed
    with (wt / "new-untracked.bin").open("r+b") as file:
        file.seek(2 * 1024 * 1024)
        file.write(b"b")
    assert gd.worktree_state_sha256(delivery) != untracked


def test_worktree_state_hash_symlink_target_not_external_contents(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    outside = tmp_path / "external-secret.txt"
    outside.write_text("secret external content")
    link = wt / "link"
    link.symlink_to(outside)
    fingerprint = gd.worktree_state_sha256(delivery)
    outside.write_text("changed external credential content")
    assert gd.worktree_state_sha256(delivery) == fingerprint
    link.unlink()
    link.symlink_to(tmp_path / "missing-secret.txt")
    assert gd.worktree_state_sha256(delivery) != fingerprint


def test_worktree_state_hash_never_follows_parent_symlink(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    (wt / "nested").mkdir()
    (wt / "nested" / "tracked.txt").write_text("tracked nested content")
    git(wt, "add", "nested/tracked.txt")
    git(wt, "commit", "-m", "nested baseline")
    delivery["head_sha"] = git(wt, "rev-parse", "HEAD")
    (wt / "nested" / "tracked.txt").unlink()
    (wt / "nested").rmdir()
    outside = tmp_path / "external-directory"
    outside.mkdir()
    (outside / "tracked.txt").write_text("external secret content")
    (wt / "nested").symlink_to(outside, target_is_directory=True)
    fingerprint = gd.worktree_state_sha256(delivery)
    (outside / "tracked.txt").write_text("external modified secret")
    assert gd.worktree_state_sha256(delivery) == fingerprint


def test_worktree_state_hash_checks_head_and_rejects_special_files(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    original = gd.worktree_state_sha256(delivery)
    (wt / "change").touch()
    result = commit(delivery)
    with pytest.raises(gd.GitDeliveryError) as caught:
        gd.worktree_state_sha256(delivery)
    assert caught.value.code == "worktree_invalid"
    delivery["head_sha"] = result["sha"]
    assert gd.worktree_state_sha256(delivery) != original
    (wt / "tracked.txt").unlink()
    os.mkfifo(wt / "tracked.txt")
    with pytest.raises(gd.GitDeliveryError) as caught:
        gd.worktree_state_sha256(delivery)
    assert caught.value.code == "worktree_invalid"


@pytest.mark.parametrize("kind", ["local", "remote", "path"])
def test_branch_exists(repo, tmp_path, kind):
    root = tmp_path / "worktrees"
    if kind == "path":
        (root / "task1234" / "branch").mkdir(parents=True)
    else:
        git(repo, "branch", "kaneo/task1234")
        if kind == "remote":
            git(repo, "push", "origin", "kaneo/task1234")
            git(repo, "branch", "-d", "kaneo/task1234")
    with pytest.raises(gd.GitDeliveryError) as error:
        branch(repo, root)
    assert error.value.code == "branch_exists"


def test_base_unknown_and_invalid_id(repo, tmp_path):
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.resolve_base(str(repo), None)
    assert error.value.code == "delivery_base_unknown"
    with pytest.raises(gd.GitDeliveryError) as error:
        branch(repo, tmp_path / "worktrees", base="missing")
    assert error.value.code == "delivery_base_unknown"
    for task_id in ("../escape", "abc", "UPPERCASE"):
        with pytest.raises(gd.GitDeliveryError):
            branch(repo, tmp_path / "worktrees", task_id=task_id)


@pytest.mark.parametrize("kind", ["missing", "repo", "branch", "head", "dirty", "main"])
def test_verify_mismatch(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    expected = delivery["head_sha"]
    if kind == "missing":
        delivery["worktree_path"] += "-missing"
    elif kind == "repo":
        delivery["repository"] = str(make_repo(tmp_path / "other"))
    elif kind == "branch":
        delivery["branch"] = "wrong"
    elif kind == "head":
        expected = "0" * 40
    elif kind == "dirty":
        (Path(delivery["worktree_path"]) / "dirty").touch()
    else:
        delivery.update(worktree_path=str(repo), branch="main")
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=expected)
    assert error.value.code == "worktree_invalid"


def test_commit_and_crash_recovery(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    (Path(delivery["worktree_path"]) / "change.txt").write_text("changed")
    result = commit(delivery)
    assert result["sha"] != delivery["head_sha"]
    assert result["files"] == ["change.txt"]
    assert commit(delivery)["sha"] == result["sha"]
    assert "KaneoPilot-Run: run1" in git(delivery["worktree_path"], "show", "-s", "--format=%B")
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery, run="wrongrun")
    assert error.value.code == "delivery_commit_failed"


@pytest.mark.parametrize("name", [".env", ".env.production", "id_rsa", "secret.pem", "secret.key", "secret.p12", "large.bin"])
def test_blocked_files(repo, tmp_path, name):
    delivery = branch(repo, tmp_path / "worktrees")
    (Path(delivery["worktree_path"]) / name).write_bytes(b"a" * (6 * 1024 * 1024 if name == "large.bin" else 1))
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "delivery_blocked_files"
    assert error.value.files == [name]
    assert git(delivery["worktree_path"], "diff", "--cached", "--name-only") == ""


def test_env_example_and_rename(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    git(wt, "mv", "tracked.txt", "renamed file.txt")
    changes = gd.worktree_changes(str(wt))
    assert {x["path"] for x in changes} == {"tracked.txt", "renamed file.txt"}
    (wt / ".env.example").write_text("EXAMPLE=value")
    assert commit(delivery)["sha"]


def test_no_changes_and_hook_failure(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "no_changes"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'token=secret-test-token' >&2\nexit 1\n")
    hook.chmod(0o755)
    (Path(delivery["worktree_path"]) / "change").touch()
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "delivery_commit_failed"
    assert "secret-test-token" not in error.value.message
    assert git(delivery["worktree_path"], "rev-parse", "HEAD") == delivery["head_sha"]


def test_no_commit_identity(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "user.useConfigOnly", "true")
    (Path(delivery["worktree_path"]) / "change").touch()
    with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
        with pytest.raises(gd.GitDeliveryError) as error:
            commit(delivery)
    assert error.value.code == "delivery_commit_failed"


def test_push_and_divergence(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    (Path(delivery["worktree_path"]) / "first").touch()
    delivery["head_sha"] = commit(delivery)["sha"]
    assert gd.push_branch(delivery) == {"pushed_sha": delivery["head_sha"]}
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", str(repo.parent / "origin.git"), str(other)], check=True, capture_output=True)
    git(other, "config", "user.name", "Delivery Test")
    git(other, "config", "user.email", "delivery@example.invalid")
    git(other, "checkout", "kaneo/task1234")
    (other / "remote-change").touch()
    git(other, "add", "-A")
    git(other, "commit", "-m", "remote")
    git(other, "push", "origin", "kaneo/task1234")
    (Path(delivery["worktree_path"]) / "local-change").touch()
    delivery["head_sha"] = commit(delivery, "run2")["sha"]
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.push_branch(delivery)
    assert error.value.code == "branch_diverged"


def test_github_repo_never_returns_url(repo):
    git(repo, "remote", "set-url", "origin", "https://credential@github.com/owner/repo.git")
    assert gd.origin_github_repo(str(repo)) == "owner/repo"
    git(repo, "remote", "set-url", "origin", "git@github.com:owner/repo.git")
    assert gd.origin_github_repo(str(repo)) == "owner/repo"
    git(repo, "remote", "set-url", "origin", "https://elsewhere.invalid/owner/repo")
    assert gd.origin_github_repo(str(repo)) is None


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://credential@github.com/owner/repo.git", "owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ("git@github.com:owner/repo.git", "owner/repo"),
    ("http://github.com/owner/repo.git", None),
    ("git://github.com/owner/repo.git", None),
    ("https://github.com/owner/repo?token=credential", None),
    ("https://github.com/owner/repo#fragment", None),
    ("https://github.com:invalid/owner/repo", None),
    ("https://github.com//owner/repo", None),
    ("https://github.com/owner/repo/extra", None),
    ("https://github.com.evil.invalid/owner/repo", None),
    (None, None),
])
def test_supported_github_origins_match_preflight(url, expected):
    from kanpilot import automation_policy
    assert automation_policy.github_repository is gd.github_repository
    assert gd.github_repository(url) == expected
