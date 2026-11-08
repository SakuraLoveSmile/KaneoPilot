"""Execution material stays local while normal delivery guards remain strict."""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kanpilot import git_delivery as gd
from test_git_delivery import branch, git, make_repo, repo

RUN = "12345678-1234-1234-1234-123456789abc"
PREFIX = ".pi/kanpilot/codex/" + RUN


def material(wt, name="gradle/caches/large.jar", content=b"cache fixture"):
    path = Path(wt) / PREFIX / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def commit(delivery, live=False, receipt=None):
    wt = delivery["worktree_path"]
    receipt = receipt or ({"verified_source_sha256": gd.source_state_sha256(wt),
                           "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {})
    return gd.commit_worktree(delivery, task_id="task1234", title="runtime exclusion",
                              execution_run_id="run1", fix_round=0, **receipt)


@pytest.mark.parametrize("live", [False, True])
def test_large_runtime_material_is_never_added_and_recovery_preserves_it(repo, tmp_path, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    large = material(wt, content=b"runtime blob" * (600 * 1024))
    apk = material(wt, "native-acceptance/installed-before.apk", b"apk fixture")
    config = material(wt, "config/.env", b"non-secret fixture")
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / ".env").write_text("external fixture")
    link = wt / PREFIX / "pub-cache" / "dependency"
    link.parent.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    before = {p: p.read_bytes() for p in (large, apk, config)}
    blob = git(wt, "hash-object", str(large.relative_to(wt)))
    (wt / "tracked.txt").write_text("verified source change")
    receipt = {"verified_source_sha256": gd.source_state_sha256(wt),
               "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {}
    original, calls = gd._git, []
    def observed(args, *a, **kw):
        if args[:1] == ["add"]:
            calls.append((args, kw.get("input")))
        return original(args, *a, **kw)
    with patch.object(gd, "_git", side_effect=observed):
        result = commit(delivery, receipt=receipt)
    assert result["files"] == ["tracked.txt"]
    assert not git(wt, "ls-tree", "-r", "--name-only", "HEAD", "--", ".pi")
    assert calls and all("--pathspec-file-nul" in args for args, _ in calls)
    assert all(PREFIX.encode() not in (raw or b"") for _, raw in calls)
    assert subprocess.run(["git", "-C", str(wt), "cat-file", "-e", blob], capture_output=True).returncode != 0
    assert before == {p: p.read_bytes() for p in before}
    assert link.is_symlink() and os.readlink(link) == str(outside)
    gd.verify_branch_worktree(delivery, require_clean=True, expected_head=result["sha"])
    assert commit(delivery, receipt=receipt) == result
    material(wt, "gradle/daemon/late.log", b"later daemon output")
    assert commit(delivery, receipt=receipt) == result


def test_runtime_only_remains_no_changes_and_preserves_index(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    cache = material(wt, content=b"x" * (6 * 1024 * 1024))
    before = git(wt, "ls-files", "--stage"), cache.read_bytes()
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "no_changes"
    assert git(wt, "rev-parse", "HEAD") == delivery["head_sha"]
    assert before == (git(wt, "ls-files", "--stage"), cache.read_bytes())


@pytest.mark.parametrize("name", [".env", "large.bin"])
@pytest.mark.parametrize("prefix", ["", ".pi/other/", ".pi/kanpilot/codex/not-a-uuid/", PREFIX + "/"])
def test_non_runtime_and_staged_material_still_use_file_safety(repo, tmp_path, name, prefix):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    target = wt / (prefix + name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * (6 * 1024 * 1024 if name == "large.bin" else 1))
    if prefix == PREFIX + "/":
        git(wt, "add", str(target.relative_to(wt)))
    before = git(wt, "ls-files", "--stage")
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "delivery_blocked_files"
    assert str(target.relative_to(wt)) in error.value.files
    assert before == git(wt, "ls-files", "--stage")
    assert git(wt, "rev-parse", "HEAD") == delivery["head_sha"]


def test_small_staged_runtime_addition_is_explicitly_rejected(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    cache = material(wt)
    git(wt, "add", PREFIX)
    before = git(wt, "ls-files", "--stage")
    with pytest.raises(gd.GitDeliveryError, match="新运行材料已暂存") as error:
        commit(delivery)
    assert error.value.code == "delivery_commit_failed"
    assert before == git(wt, "ls-files", "--stage")
    assert cache.read_bytes() == b"cache fixture"


@pytest.mark.parametrize("kind", ["modified", "deleted", "rename"])
def test_head_owned_runtime_paths_remain_real_changes(tmp_path, kind):
    source = make_repo(tmp_path / "source")
    cache = material(source, "owned.txt", b"repository-owned fixture")
    git(source, "add", PREFIX)
    git(source, "commit", "-m", "repository-owned runtime path")
    git(source, "push", "origin", "main")
    delivery = branch(source, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    owned = wt / cache.relative_to(source)
    if kind == "modified":
        owned.write_text("modified owned data")
    elif kind == "deleted":
        owned.unlink()
    else:
        git(wt, "mv", str(owned.relative_to(wt)), PREFIX + "/renamed.txt")
    with pytest.raises(gd.GitDeliveryError):
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    result = commit(delivery)
    assert str(owned.relative_to(wt)) in result["files"]
    assert commit(delivery) == result


@pytest.mark.parametrize("live", [False, True])
def test_selected_staging_handles_rename_delete_and_literal_names(repo, tmp_path, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    material(wt)
    git(wt, "mv", "tracked.txt", "renamed [literal]\nfile.txt")
    (wt / ":(glob)*.txt").write_text("literal pathspec source")
    result = commit(delivery, live)
    assert set(result["files"]) == {"tracked.txt", "renamed [literal]\nfile.txt", ":(glob)*.txt"}
    assert git(wt, "show", "HEAD::(glob)*.txt") == "literal pathspec source"
    assert commit(delivery, live) == result


def test_classification_rejects_ancestor_replacement_without_reading_targets(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    cache = material(wt)
    external = tmp_path / "external"
    external.mkdir()
    (external / "large.jar").write_bytes(b"outside fixture")
    original, replaced = gd._git, False
    def changed(args, *a, **kw):
        nonlocal replaced
        result = original(args, *a, **kw)
        if args[:2] == ["ls-files", "--stage"] and not replaced:
            cache.parent.rename(cache.parent.with_name("old-caches"))
            cache.parent.symlink_to(external, target_is_directory=True)
            replaced = True
        return result
    with patch.object(gd, "_git", side_effect=changed), pytest.raises(gd.GitDeliveryError, match="目录身份"):
        gd.effective_worktree_changes(str(wt))
    assert (external / "large.jar").read_bytes() == b"outside fixture"


@pytest.mark.parametrize("source", ["recovery", "hook"])
@pytest.mark.parametrize("link", [False, True])
def test_new_runtime_commit_cannot_be_accepted_after_hook_or_crash(repo, tmp_path, source, link):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    cache = material(wt)
    if link:
        cache.unlink()
        cache.symlink_to(tmp_path / "nonexistent-external-cache")
    if source == "recovery":
        git(wt, "add", PREFIX)
        git(wt, "commit", "-m", "model commit\n\nKaneoPilot-Run: run1")
    else:
        (wt / "tracked.txt").write_text("source change")
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add .pi/kanpilot/codex\n")
        hook.chmod(0o755)
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "delivery_commit_failed"
    assert git(wt, "rev-parse", "HEAD") != delivery["head_sha"]
    git(wt, "reset", "--mixed", "HEAD")
    with pytest.raises(gd.GitDeliveryError, match="新运行材料"):
        commit(delivery)


def test_runtime_classification_preserves_concurrent_index_change(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    material(wt)
    original, changed = gd._git, False
    def mutate(args, *a, **kw):
        nonlocal changed
        result = original(args, *a, **kw)
        if args[:2] == ["ls-files", "--stage"] and not changed:
            (wt / "unique.txt").write_text("concurrent staging")
            git(wt, "add", "unique.txt")
            changed = True
        return result
    with patch.object(gd, "_git", side_effect=mutate), pytest.raises(gd.GitDeliveryError, match="索引已变化"):
        gd.effective_worktree_changes(str(wt))
    assert git(wt, "show", ":unique.txt") == "concurrent staging"


def test_large_runtime_candidate_set_uses_bounded_staging_arguments(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    root = material(wt).parent
    for number in range(6000):
        (root / ("runtime-cache-" + str(number) + ".txt")).write_bytes(b"fixture")
    (wt / "tracked.txt").write_text("business change")
    original, additions = gd._git, []
    def observed(args, *a, **kw):
        if args[:1] == ["add"]:
            additions.append((args, kw.get("input")))
        return original(args, *a, **kw)
    with patch.object(gd, "_git", side_effect=observed):
        result = commit(delivery)
    assert result["files"] == ["tracked.txt"]
    assert len(additions) == 1
    args, paths = additions[0]
    assert len(args) == 4 and paths == b":(literal)tracked.txt\0"
    assert len(list(root.iterdir())) == 6001


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("staged", [False, True])
def test_directory_replaced_by_file_commits_deleted_children(tmp_path, live, staged):
    source = make_repo(tmp_path / "source")
    (source / "dir").mkdir()
    (source / "dir" / "a.txt").write_text("old child")
    git(source, "add", "dir")
    git(source, "commit", "-m", "nested baseline")
    git(source, "push", "origin", "main")
    delivery = branch(source, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    (wt / "dir" / "a.txt").unlink()
    (wt / "dir").rmdir()
    (wt / "dir").write_text("replacement file")
    if staged:
        git(wt, "add", "-A", "--", "dir")
    changes = gd.effective_worktree_changes(str(wt))
    assert {c["path"] for c in changes} == {"dir", "dir/a.txt"}
    assert next(c for c in changes if c["path"] == "dir/a.txt")["size"] == 0
    result = commit(delivery, live)
    assert set(result["files"]) == {"dir", "dir/a.txt"}
    assert git(wt, "show", "HEAD:dir") == "replacement file"
    assert commit(delivery, live) == result


@pytest.mark.parametrize("live", [False, True])
def test_private_git_checkout_stays_opaque_and_is_not_delivered(repo, tmp_path, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    nested = wt / PREFIX / "pub-cache" / "git" / "dependency-checkout"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(nested)], check=True, capture_output=True)
    git(nested, "config", "user.name", "Delivery Test")
    git(nested, "config", "user.email", "delivery@example.invalid")
    (nested / "package.dart").write_text("library package_fixture;")
    git(nested, "add", "package.dart")
    git(nested, "commit", "-m", "cached dependency")
    # A dirty checkout and private git config are both evidence to preserve.
    (nested / "package.dart").write_text("library dirty_fixture;")
    (nested / "large.bin").write_bytes(b"x" * (6 * 1024 * 1024))
    before = {str(p.relative_to(nested)): p.read_bytes() for p in nested.rglob("*") if p.is_file()}
    assert str(nested.relative_to(wt)) + "/" in {c["path"] for c in gd.worktree_changes(str(wt))}
    (wt / "tracked.txt").write_text("business change")
    receipt = {"verified_source_sha256": gd.source_state_sha256(wt),
               "verified_index_sha256": gd.expected_index_source_sha256(wt)} if live else {}
    original = gd._git
    def no_nested_git(args, cwd, *a, **kw):
        assert Path(cwd) != nested
        return original(args, cwd, *a, **kw)
    with patch.object(gd, "_git", side_effect=no_nested_git):
        result = commit(delivery, receipt=receipt)
        assert result["files"] == ["tracked.txt"]
        assert commit(delivery, receipt=receipt) == result
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=result["sha"])
    assert not git(wt, "ls-tree", "-r", "--name-only", "HEAD", "--", PREFIX)
    assert before == {str(p.relative_to(nested)): p.read_bytes() for p in nested.rglob("*") if p.is_file()}


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("kind", ["goal", "runtime", "nested-runtime", "business"])
def test_material_staged_between_classification_and_index_snapshot_is_preserved(repo, tmp_path, kind, live):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    if kind == "goal":
        local = wt / ".pi" / "goal" / "new-goal.md"
        local.parent.mkdir(parents=True)
        local.write_text("newly staged goal")
    elif kind == "business":
        material(wt)  # The original material selects the private-index path.
        local = wt / "unique-new.txt"
    elif kind == "nested-runtime":
        local = wt / PREFIX / "pub-cache" / "git" / "dependency-checkout"
        local.mkdir(parents=True)
        subprocess.run(["git", "init", "--initial-branch=main", str(local)], check=True, capture_output=True)
        git(local, "config", "user.name", "Delivery Test")
        git(local, "config", "user.email", "delivery@example.invalid")
        (local / "package.dart").write_text("library nested_fixture;")
        git(local, "add", "package.dart")
        git(local, "commit", "-m", "cached dependency")
    else:
        local = material(wt, content=b"newly staged runtime")
    name = str(local.relative_to(wt))
    (wt / "tracked.txt").write_text("business change")
    original, staged = gd._git, False
    def mutate(args, *a, **kw):
        nonlocal staged
        result = original(args, *a, **kw)
        if args == ["rev-parse", "--git-path", "index"] and not staged:
            if kind == "business":
                local.write_text("new concurrent business entry")
            git(wt, "add", name)
            staged = True
        return result
    with patch.object(gd, "_git", side_effect=mutate), pytest.raises(gd.GitDeliveryError, match="分类后索引条目已变化"):
        commit(delivery, live)
    assert git(wt, "rev-parse", "HEAD") == delivery["head_sha"]
    assert staged
    if kind == "nested-runtime":
        assert git(wt, "ls-files", "--stage", "--", name).startswith("160000 " + git(local, "rev-parse", "HEAD"))
    else:
        assert git(wt, "show", ":" + name) == local.read_text()


@pytest.mark.parametrize("kind", ["file", "nested-checkout"])
def test_runtime_staged_between_status_and_index_sampling_remains_a_change(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    if kind == "file":
        cache = material(wt)
    else:
        cache = wt / PREFIX / "pub-cache" / "git" / "checkout"
        cache.mkdir(parents=True)
        subprocess.run(["git", "init", "--initial-branch=main", str(cache)], check=True, capture_output=True)
        git(cache, "config", "user.name", "Delivery Test")
        git(cache, "config", "user.email", "delivery@example.invalid")
        (cache / "package.dart").write_text("library nested_fixture;")
        git(cache, "add", "package.dart")
        git(cache, "commit", "-m", "cached dependency")
    name = str(cache.relative_to(wt))
    (wt / "tracked.txt").write_text("business change")
    original, staged = gd._git, False
    def mutate(args, *a, **kw):
        nonlocal staged
        result = original(args, *a, **kw)
        if args[:3] == ["ls-tree", "-r", "--name-only"] and not staged:
            git(wt, "add", name)
            staged = True
        return result
    with patch.object(gd, "_git", side_effect=mutate):
        changes = gd.effective_worktree_changes(str(wt))
    assert name in {c["path"].removesuffix("/") for c in changes}
    with pytest.raises(gd.GitDeliveryError, match="新运行材料已暂存"):
        commit(delivery)
    assert staged
    assert git(wt, "rev-parse", "HEAD") == delivery["head_sha"]
    if kind == "file":
        assert git(wt, "show", ":" + name) == cache.read_text()
    else:
        assert git(wt, "ls-files", "--stage", "--", name).startswith("160000 " + git(cache, "rev-parse", "HEAD"))
