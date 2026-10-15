"""Generated goals must stay local without weakening delivery Git boundaries."""
from pathlib import Path
from unittest.mock import patch

import pytest

from kanpilot import git_delivery as gd
from test_git_delivery import branch, git, legacy_snapshot, make_repo, repo


def new_goal(wt, name="goal [literal].md"):
    goal = Path(wt) / ".pi" / "goal" / name
    goal.parent.mkdir(parents=True, exist_ok=True)
    goal.write_text("# Generated goal\n", encoding="utf-8")
    return goal


def commit(delivery, live=False, receipt=None):
    wt = delivery["worktree_path"]
    receipt = receipt or ({"verified_source_sha256": gd.source_state_sha256(wt),
                           "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {})
    return gd.commit_worktree(delivery, task_id="task1234", title="goal exclusion",
                              execution_run_id="run1", fix_round=0, **receipt)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("staged", [False, True])
def test_goal_only_is_clean_and_no_changes(repo, tmp_path, live, staged):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    goal = new_goal(wt)
    if staged:
        git(wt, "add", ".pi/goal")
        goal.write_text("unstaged goal revision")
    before = git(wt, "rev-parse", "HEAD"), git(wt, "ls-files", "--stage"), goal.read_bytes()
    gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery, live)
    assert error.value.code == "no_changes"
    assert before == (git(wt, "rev-parse", "HEAD"), git(wt, "ls-files", "--stage"), goal.read_bytes())


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("ignored", [False, True])
def test_commit_skips_goal_and_preserves_original_staging(repo, tmp_path, live, staged, ignored):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    if ignored:
        with (repo / ".git" / "info" / "exclude").open("a") as stream:
            stream.write("\n.pi/goal/\n")
    goal = new_goal(wt)
    if staged:
        git(wt, "add", "-f", ".pi/goal")
        goal.write_text("unstaged goal revision")
    staged_before = git(wt, "ls-files", "--stage", "--", ".pi/goal")
    (wt / "tracked.txt").unlink()
    (wt / "replacement.txt").write_text("base\n")
    git(wt, "add", "-A", "--", "tracked.txt", "replacement.txt")
    artifact = wt / "__pycache__" / "local.pyc"
    artifact.parent.mkdir()
    artifact.write_bytes(b"local artifact")
    receipt = {"verified_source_sha256": gd.source_state_sha256(wt),
               "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {}
    result = commit(delivery, receipt=receipt)
    assert sorted(result["files"]) == ["replacement.txt", "tracked.txt"]
    assert git(wt, "ls-files", "--stage", "--", ".pi/goal") == staged_before
    assert goal.read_text() == ("unstaged goal revision" if staged else "# Generated goal\n")
    assert not git(wt, "ls-tree", "-r", "--name-only", "HEAD", "--", ".pi/goal", "__pycache__")
    assert artifact.read_bytes() == b"local artifact"
    gd.verify_branch_worktree(delivery, require_clean=True, expected_head=result["sha"])
    assert commit(delivery, receipt=receipt) == result


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("kind", ["modified", "deleted", "other-pi"])
def test_existing_goals_and_other_pi_remain_real_changes(tmp_path, live, kind):
    source = make_repo(tmp_path / "source")
    existing = new_goal(source)
    git(source, "add", ".pi/goal")
    git(source, "commit", "-m", "tracked goal")
    git(source, "push", "origin", "main")
    delivery = branch(source, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    goal = wt / existing.relative_to(source)
    if kind == "deleted":
        goal.unlink()
        expected = ".pi/goal/goal [literal].md"
    elif kind == "modified":
        goal.write_text("changed repository goal")
        expected = ".pi/goal/goal [literal].md"
    else:
        (wt / ".pi" / "settings.json").write_text("{}")
        expected = ".pi/settings.json"
    with pytest.raises(gd.GitDeliveryError):
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    assert commit(delivery, live)["files"] == [expected]


@pytest.mark.parametrize("kind", ["file-link", "goal-link", "pi-link", "nested", "non-md", "deleted-staged"])
def test_safe_goal_boundary_is_narrow(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "goal.md").write_text("outside")
    if kind == "pi-link":
        (wt / ".pi").symlink_to(outside, target_is_directory=True)
    elif kind == "goal-link":
        (wt / ".pi").mkdir()
        (wt / ".pi" / "goal").symlink_to(outside, target_is_directory=True)
    elif kind == "file-link":
        goal = new_goal(wt)
        goal.unlink()
        goal.symlink_to(outside / "goal.md")
    elif kind == "deleted-staged":
        goal = new_goal(wt)
        git(wt, "add", ".pi/goal")
        goal.unlink()
    else:
        new_goal(wt, "nested/goal.md" if kind == "nested" else "goal.txt")
    with pytest.raises(gd.GitDeliveryError):
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])


@pytest.mark.parametrize("live", [False, True])
def test_hook_failure_preserves_staged_goal(repo, tmp_path, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    goal = new_goal(wt)
    git(wt, "add", ".pi/goal")
    before = git(wt, "ls-files", "--stage", "--", ".pi/goal")
    goal.write_text("unstaged revision")
    (wt / "tracked.txt").write_text("verified change")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(gd.GitDeliveryError):
        commit(delivery, live)
    assert git(wt, "rev-parse", "HEAD") == delivery["head_sha"]
    assert git(wt, "ls-files", "--stage", "--", ".pi/goal") == before
    assert goal.read_text() == "unstaged revision"


@pytest.mark.parametrize("live", [False, True])
def test_concurrent_goal_index_change_is_preserved(repo, tmp_path, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    goal = new_goal(wt)
    git(wt, "add", ".pi/goal")
    (wt / "tracked.txt").write_text("verified change")
    original = gd._git
    def changed(args, *a, **kw):
        result = original(args, *a, **kw)
        if args[:1] == ["commit"]:
            blob = original(["hash-object", "-w", "--stdin"], wt, 10, input=b"concurrent goal stage").stdout.decode().strip()
            original(["update-index", "--add", "--cacheinfo", "100644," + blob + "," + str(goal.relative_to(wt))], wt, 10)
        return result
    with patch.object(gd, "_git", side_effect=changed), pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery, live)
    assert "普通索引已变化" in str(error.value)
    assert git(wt, "show", ":" + str(goal.relative_to(wt))) == "concurrent goal stage"
    assert git(wt, "rev-parse", "HEAD") != delivery["head_sha"]
    with pytest.raises(gd.GitDeliveryError):
        commit(delivery, live)


@pytest.mark.parametrize("kind,live", [("source-drift", True), ("hook-tree", False), ("hook-tree", True)])
def test_goal_exclusion_preserves_verification_guards(repo, tmp_path, kind, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    new_goal(wt)
    (wt / "tracked.txt").write_text("verified change")
    receipt = {"verified_source_sha256": gd.source_state_sha256(wt),
               "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {}
    if kind == "source-drift":
        (wt / "tracked.txt").write_text("unverified")
    else:
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add .pi/goal\n")
        hook.chmod(0o755)
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery, receipt=receipt)
    assert error.value.code == "delivery_commit_failed"
    assert (git(wt, "rev-parse", "HEAD") == delivery["head_sha"]) == (kind == "source-drift")
    if kind == "hook-tree":
        # Even after an operator reconciles ordinary staging with that commit,
        # the recovery path must not publish a generated goal inserted by hooks.
        git(wt, "reset", "--mixed", "HEAD")
        with pytest.raises(gd.GitDeliveryError) as recovered:
            commit(delivery, receipt=receipt)
        assert "新生成 goal" in str(recovered.value)


@pytest.mark.parametrize("kind", ["staged-link", "gitlink", "conflict"])
def test_unsafe_goal_index_entries_remain_changes(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    goal = new_goal(wt)
    name = str(goal.relative_to(wt))
    if kind == "gitlink":
        git(wt, "update-index", "--add", "--cacheinfo", "160000," + delivery["head_sha"] + "," + name)
    else:
        blob = gd._git(["hash-object", "-w", "--stdin"], wt, 10, input=b"outside").stdout.decode().strip()
        if kind == "staged-link":
            git(wt, "update-index", "--add", "--cacheinfo", "120000," + blob + "," + name)
        else:
            gd._git(["update-index", "--index-info"], wt, 10,
                    input=("100644 " + blob + " 1\t" + name + "\n100644 " + blob + " 2\t" + name + "\n").encode())
    assert name in {change["path"] for change in gd.effective_worktree_changes(str(wt))}
    with pytest.raises(gd.GitDeliveryError):
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])


@pytest.mark.parametrize("live", [False, True])
def test_tracked_goal_rename_can_commit_and_recover(tmp_path, live):
    source = make_repo(tmp_path / "source")
    new_goal(source)
    git(source, "add", ".pi/goal")
    git(source, "commit", "-m", "tracked goal")
    git(source, "push", "origin", "main")
    delivery = branch(source, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    git(wt, "mv", ".pi/goal/goal [literal].md", ".pi/goal/renamed.md")
    receipt = {"verified_source_sha256": gd.source_state_sha256(wt),
               "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {}
    result = commit(delivery, receipt=receipt)
    assert set(result["files"]) == {".pi/goal/goal [literal].md", ".pi/goal/renamed.md"}
    assert commit(delivery, receipt=receipt) == result


def test_source_hash_and_detached_worktree_hash_keep_existing_contract(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    before = gd.source_state_sha256(wt), gd.expected_index_source_sha256(wt)
    new_goal(wt)
    (wt / ".pi" / "settings.json").write_text("{}")
    assert before == (gd.source_state_sha256(wt), gd.expected_index_source_sha256(wt))
    root = tmp_path / "worktrees"
    legacy_snapshot(repo, root, "review", delivery["head_sha"])
    snapshot = gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                           purpose="review", worktrees_root=root)
    detached = dict(delivery, worktree_path=snapshot)
    clean = gd.worktree_state_sha256(detached, detached=True)
    new_goal(snapshot)
    assert gd.worktree_state_sha256(detached, detached=True) != clean
    with pytest.raises(gd.GitDeliveryError):
        gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                     purpose="review", worktrees_root=root)
