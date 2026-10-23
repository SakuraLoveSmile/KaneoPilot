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


def legacy_snapshot(repo, root, purpose, sha=None, task_id="task1234"):
    sha = sha or git(repo, "rev-parse", "HEAD")
    path = Path(root) / task_id / (purpose + "-" + sha[:12])
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(path), sha],
                   check=True, capture_output=True, env=gd.git_env())
    return path


def test_branch_base_and_snapshot_reuse(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    assert delivery["base_branch"] == "main"
    assert delivery["branch"] == "kaneo/task1234"
    assert gd.resolve_base(str(repo), "main") == ("main", delivery["base_sha"])
    preferred = branch(repo, tmp_path / "worktrees", task_id="task5678", base="main")
    assert preferred["base_branch"] == "main"
    gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])
    root = tmp_path / "worktrees"
    legacy_snapshot(repo, root, "review", delivery["head_sha"])
    snapshot = gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                           purpose="review", worktrees_root=root)
    assert snapshot == gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                                    purpose="review", worktrees_root=root)
    assert git(snapshot, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    legacy_snapshot(repo, root, "plan", delivery["head_sha"])
    plan = gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                       purpose="plan", worktrees_root=root)
    assert Path(plan).name == "plan-" + delivery["head_sha"][:12]
    (Path(snapshot) / "dirty").touch()
    with pytest.raises(gd.GitDeliveryError, match="已有快照"):
        gd.prepare_snapshot_worktree(str(repo), "task1234", sha=delivery["head_sha"],
                                     purpose="review", worktrees_root=tmp_path / "worktrees")


def plan_snapshot(repo, root):
    sha = git(repo, "rev-parse", "HEAD")
    path = Path(root) / "task1234" / ("plan-" + sha[:12])
    if not path.exists():
        legacy_snapshot(repo, root, "plan", sha)
    return Path(gd.prepare_snapshot_worktree(str(repo), "task1234", sha=sha,
                                            purpose="plan", worktrees_root=root))


def test_missing_legacy_snapshot_is_not_created(repo, tmp_path):
    root = tmp_path / "worktrees"
    path = root / "task1234" / ("plan-" + git(repo, "rev-parse", "HEAD")[:12])
    with pytest.raises(gd.GitDeliveryError, match="新快照必须经核心槽位租约创建"):
        gd.prepare_snapshot_worktree(str(repo), "task1234", sha=git(repo, "rev-parse", "HEAD"),
                                     purpose="plan", worktrees_root=root)
    assert not path.exists()


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


@pytest.mark.parametrize("ignored", [False, True])
def test_plan_snapshot_reuses_plus_markdown_without_removing_evidence(repo, tmp_path, ignored):
    if ignored:
        with (repo / ".git" / "info" / "exclude").open("a") as exclude:
            exclude.write("\n.pi/\n")
    root = tmp_path / "worktrees"
    path = plan_snapshot(repo, root)
    plan = path / ".pi" / "plan" / "每日总结-20261006.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# 旧规划\n完整产物保持原样\n", encoding="utf-8")
    original = plan.read_bytes()
    exclude = (repo / ".git" / "info" / "exclude").read_bytes()
    assert plan_snapshot(repo, root) == path
    assert plan.read_bytes() == original
    assert (repo / ".git" / "info" / "exclude").read_bytes() == exclude
    assert git(path, "diff", "--name-only") == ""
    assert git(path, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize("kind", ["other-pi", "non-md", "nested", "file-link", "plan-link", "pi-link", "fifo", "ignored-link"])
def test_plan_snapshot_rejects_unsafe_plus_artifacts(repo, tmp_path, kind):
    if kind == "ignored-link":
        with (repo / ".git" / "info" / "exclude").open("a") as exclude:
            exclude.write("\n.pi/\n")
    root = tmp_path / "worktrees"
    path = plan_snapshot(repo, root)
    external = tmp_path / "external"
    external.mkdir()
    (external / "plan.md").write_text("external")
    if kind == "pi-link":
        (path / ".pi").symlink_to(external, target_is_directory=True)
    elif kind == "plan-link":
        (path / ".pi").mkdir()
        (path / ".pi" / "plan").symlink_to(external, target_is_directory=True)
    else:
        plans = path / ".pi" / "plan"
        plans.mkdir(parents=True)
        if kind == "other-pi":
            (path / ".pi" / "settings.json").write_text("{}")
        elif kind == "non-md":
            (plans / "script.py").write_text("print('unsafe')")
        elif kind == "nested":
            (plans / "nested").mkdir()
        elif kind == "fifo":
            os.mkfifo(plans / "plan.md")
        else:
            (plans / "plan.md").symlink_to(external / "plan.md")
    with pytest.raises(gd.GitDeliveryError) as error:
        plan_snapshot(repo, root)
    assert error.value.code == "worktree_invalid"


def test_plan_snapshot_preserves_clean_tracked_pi_configuration(tmp_path):
    repo = make_repo(tmp_path / "source")
    (repo / ".pi" / "extensions").mkdir(parents=True)
    (repo / ".pi" / "settings.json").write_text("{}")
    (repo / ".pi" / "extensions" / "custom.ts").write_text("export {}")
    (repo / ".gitignore").write_text(".pi/extensions/local/\n")
    git(repo, "add", ".pi", ".gitignore")
    git(repo, "commit", "-qm", "existing Plus configuration")
    root = tmp_path / "worktrees"
    path = plan_snapshot(repo, root)
    assert plan_snapshot(repo, root) == path
    plan = path / ".pi" / "plan" / "result.md"
    plan.parent.mkdir()
    plan.write_text("# 规划产物")
    assert plan_snapshot(repo, root) == path
    hidden = path / ".pi" / "extensions" / "local"
    hidden.mkdir()
    (hidden / "unsafe.json").write_text("{}")
    with pytest.raises(gd.GitDeliveryError):
        plan_snapshot(repo, root)


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


@pytest.mark.parametrize("artifact", ["materials", "plus-plan"])
def test_review_and_branch_do_not_allow_plan_materials(repo, tmp_path, artifact):
    delivery = branch(repo, tmp_path / "worktrees")
    for purpose in ("branch", "review"):
        if purpose == "branch":
            path = Path(delivery["worktree_path"])
        else:
            root = tmp_path / "worktrees"
            legacy_snapshot(repo, root, "review", delivery["head_sha"])
            path = Path(gd.prepare_snapshot_worktree(
                str(repo), "task1234", sha=delivery["head_sha"], purpose="review", worktrees_root=root))
        materials = path / (".kaneopilot-plan-material" if artifact == "materials" else ".pi") / ("run1" if artifact == "materials" else "plan")
        materials.mkdir(parents=True)
        (materials / ("manifest.json" if artifact == "materials" else "plan.md")).write_text("{}")
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
    real_parser = gd.github_repository
    bare = str(repo.parent / "origin.git")
    with patch.object(gd, "github_repository", side_effect=lambda url, **kwargs: "owner/repo" if url == bare else real_parser(url, **kwargs)):
        assert gd.push_branch(delivery, expected_repo="owner/repo") == {"pushed_sha": delivery["head_sha"]}
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
    with patch.object(gd, "github_repository", side_effect=lambda url, **kwargs: "owner/repo" if url == bare else real_parser(url, **kwargs)), pytest.raises(gd.GitDeliveryError) as error:
        gd.push_branch(delivery, expected_repo="owner/repo")
    assert error.value.code == "branch_diverged"


def test_github_repo_never_returns_url(repo):
    git(repo, "remote", "set-url", "origin", "https://credential@github.com/owner/repo.git")
    assert gd.origin_github_repo(str(repo)) == "owner/repo"
    git(repo, "remote", "set-url", "origin", "git@github.com:owner/repo.git")
    assert gd.origin_github_repo(str(repo)) == "owner/repo"
    git(repo, "remote", "set-url", "origin", "https://elsewhere.invalid/owner/repo")
    assert gd.origin_github_repo(str(repo)) is None


@pytest.mark.parametrize("target", ["wrong-bare", "wrong-host", "wrong-repo", "multiple", "rewrite"])
def test_push_wrong_effective_target_never_writes_either_bare(repo, tmp_path, target):
    delivery = branch(repo, tmp_path / "worktrees")
    wrong = make_repo(tmp_path / "wrong").parent / "origin.git"
    correct = repo.parent / "origin.git"
    before = {p: git(p, "show-ref") for p in (correct, wrong)}
    if target in ("wrong-bare", "rewrite"):
        if target == "rewrite":
            git(repo, "config", f"url.{wrong}.pushInsteadOf", str(correct))
        else:
            git(repo, "config", "remote.origin.pushurl", str(wrong))
    elif target == "wrong-host":
        git(repo, "config", "remote.origin.pushurl", "https://elsewhere.invalid/owner/repo")
    elif target == "wrong-repo":
        git(repo, "config", "remote.origin.pushurl", "https://github.com/other/repo")
    else:
        git(repo, "config", "--add", "remote.origin.pushurl", str(correct))
        git(repo, "config", "--add", "remote.origin.pushurl", str(wrong))
    real_parser = gd.github_repository
    with patch.object(gd, "github_repository", side_effect=lambda url, **kwargs: "owner/repo" if url == str(correct) else real_parser(url, **kwargs)), pytest.raises(gd.GitDeliveryError) as error:
        gd.push_branch(delivery, expected_repo="owner/repo")
    assert error.value.code == "branch_diverged"
    assert before == {p: git(p, "show-ref") for p in (correct, wrong)}


def test_push_uses_checked_target_despite_origin_change_and_keeps_hook(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    correct = repo.parent / "origin.git"
    wrong = make_repo(tmp_path / "wrong").parent / "origin.git"
    before = git(wrong, "show-ref")
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\ntouch '" + str(tmp_path / "hook-ran") + "'\n")
    hook.chmod(0o755)
    real_git, real_parser = gd._git, gd.github_repository
    def redirect(args, *a, **kw):
        if args[0] == "push":
            git(repo, "config", "remote.origin.pushurl", str(wrong))
            assert args[1] == str(correct)
        return real_git(args, *a, **kw)
    with patch.object(gd, "github_repository", side_effect=lambda url, **kwargs: "owner/repo" if url == str(correct) else real_parser(url, **kwargs)), patch.object(gd, "_git", side_effect=redirect):
        gd.push_branch(delivery, expected_repo="owner/repo")
    assert git(correct, "rev-parse", "refs/heads/" + delivery["branch"]) == delivery["head_sha"]
    assert git(wrong, "show-ref") == before
    assert (tmp_path / "hook-ran").is_file()


def test_push_refuses_second_rewrite(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    wrong = make_repo(tmp_path / "wrong").parent / "origin.git"
    git(repo, "config", "remote.origin.pushurl", "alias://delivery")
    git(repo, "config", "url.https://github.com/owner/repo.pushInsteadOf", "alias://delivery")
    git(repo, "config", f"url.{wrong}.insteadOf", "https://github.com/owner/repo")
    before = git(wrong, "show-ref")
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.push_branch(delivery, expected_repo="owner/repo")
    assert error.value.code == "branch_diverged"
    assert git(wrong, "show-ref") == before


def test_push_allows_stable_single_rewrite(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    correct = repo.parent / "origin.git"
    git(repo, "remote", "set-url", "origin", "alias://delivery")
    git(repo, "config", f"url.{correct}.pushInsteadOf", "alias://delivery")
    real_parser = gd.github_repository
    with patch.object(gd, "github_repository", side_effect=lambda url, **kwargs: "owner/repo" if url == str(correct) else real_parser(url, **kwargs)):
        assert gd.push_branch(delivery, expected_repo="owner/repo")["pushed_sha"] == delivery["head_sha"]
    assert git(correct, "rev-parse", "refs/heads/" + delivery["branch"]) == delivery["head_sha"]


def test_push_exports_reviewed_sha_if_pre_push_hook_moves_branch(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    correct = repo.parent / "origin.git"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nprintf secret > .env\ngit add .env\ngit commit -qm 'late hook commit'\n")
    hook.chmod(0o755)
    real_parser = gd.github_repository
    with patch.object(gd, "github_repository", side_effect=lambda url, **kwargs: "owner/repo" if url == str(correct) else real_parser(url, **kwargs)):
        gd.push_branch(delivery, expected_repo="owner/repo")
    assert git(delivery["worktree_path"], "rev-parse", "HEAD") != delivery["head_sha"]
    assert git(correct, "rev-parse", "refs/heads/" + delivery["branch"]) == delivery["head_sha"]


@pytest.mark.parametrize("name", [".env", "large.bin"])
@pytest.mark.parametrize("source", ["recovery", "hook"])
def test_actual_commit_safety_recovery_and_hook(repo, tmp_path, name, source):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    if source == "recovery":
        (wt / name).write_bytes(b"x" * (6 * 1024 * 1024 if name == "large.bin" else 1))
        git(wt, "add", name)
        git(wt, "commit", "-m", "model commit\n\nKaneoPilot-Run: run1")
    else:
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\n" +
                        ("dd if=/dev/zero of=large.bin bs=1048576 count=6 2>/dev/null\n" if name == "large.bin" else "printf secret > .env\n") +
                        "git add " + name + "\n")
        hook.chmod(0o755)
        (wt / "safe.txt").write_text("safe")
    before = git(repo.parent / "origin.git", "show-ref")
    with pytest.raises(gd.GitDeliveryError) as error:
        commit(delivery)
    assert error.value.code == "delivery_blocked_files"
    assert error.value.files == [name]
    assert git(repo.parent / "origin.git", "show-ref") == before


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


@pytest.fixture
def ssh_aliases(tmp_path, monkeypatch):
    """Run actual OpenSSH expansion using only this disposable configuration."""
    config = tmp_path / "ssh_config"
    include = tmp_path / "accounts.conf"
    include.write_text("Host github-alt github_work\n  HostName ssh.github.com\n  User git\n  Port 443\n")
    config.write_text(f'Include "{include}"\nHost github-personal\n  HostName github.com\n'
                      '  User git\n  Port 22\nHost other-server\n  HostName example.invalid\n')
    git_config = tmp_path / "git_config"
    git_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH_VARIANT", raising=False)
    calls = []
    real_run = subprocess.run
    def run(args, **kwargs):
        if args[0] == "ssh":
            calls.append((args, kwargs))
            assert args[1:3] == ["-G", "-oCanonicalizeHostname=no"]
            assert 0 < kwargs["timeout"] <= 5
            args = [args[0], "-F", str(config), *args[1:]]
        return real_run(args, **kwargs)
    monkeypatch.setattr(gd.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("url", [
    "git@github-personal:owner/repo.git",
    "git@github-alt:owner/repo",
    "git@github_work:owner/repo.git",
    "ssh://git@github-alt/owner/repo.git",
    "ssh://git@github-alt:443/owner/repo",
    "ssh://git@github-personal:22/owner/repo",
])
def test_real_openssh_host_include_github_alias(repo, ssh_aliases, url):
    assert gd.github_repository(url, cwd=str(repo)) == "owner/repo"
    assert ssh_aliases[-1][1]["cwd"] == str(repo)


@pytest.mark.parametrize("url", [
    "git@unknown:owner/repo.git", "git@other-server:owner/repo.git",
    "ssh://git@other-server/owner/repo.git",
])
def test_non_github_and_unknown_ssh_aliases_rejected(repo, ssh_aliases, url):
    assert gd.github_repository(url, cwd=str(repo)) is None
    assert len(ssh_aliases) == 1


@pytest.mark.parametrize("url", [
    "https://github-alt/owner/repo", "https://github-personal/owner/repo",
    "git@-oProxyCommand=bad:owner/repo", "ssh://git@-bad/owner/repo",
    "git@host with space:owner/repo", "git@host\nname:owner/repo",
    "git@host%20name:owner/repo", "ssh://git@github-alt:2222/owner/repo",
    "ssh://other@github-alt/owner/repo", "ssh://git:secret@github-alt/owner/repo",
    "git@github-alt:owner/repo/extra", "ssh://git@github-alt/owner/repo?secret=yes",
    "ssh://git@github-\nalt/owner/repo", "ssh://git@github-alt/owner/re\tpo",
    "GIT@github-alt:owner/repo",
])
def test_invalid_aliases_rejected_before_ssh(repo, ssh_aliases, url):
    assert gd.github_repository(url, cwd=str(repo)) is None
    assert ssh_aliases == []


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, "", "private config details"),
    subprocess.TimeoutExpired("ssh", .1, stderr="private config details"),
    OSError("unavailable"),
    subprocess.CompletedProcess([], 0, "hostname github.com\nuser other\nport 22\n", ""),
    subprocess.CompletedProcess([], 0, "hostname github.com\nuser git\nport 2222\n", ""),
    subprocess.CompletedProcess([], 0, "hostname github.com\nuser git\nport 22\nhostname evil\n", ""),
    subprocess.CompletedProcess([], 0, "hostname github.com\n", ""),
])
def test_ssh_expansion_failures_are_bounded_and_private(result, capsys):
    def run(args, **kwargs):
        assert args[0] == "ssh" and args[1] == "-G"
        assert 0 < kwargs["timeout"] <= 5
        if isinstance(result, Exception):
            raise result
        return result
    with patch.object(gd.subprocess, "run", side_effect=run):
        assert gd.github_repository("git@github-alt:owner/repo") is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("override", ["GIT_SSH", "GIT_SSH_COMMAND", "core.sshCommand"])
def test_alias_transport_override_not_executed(repo, ssh_aliases, monkeypatch, override):
    if override == "core.sshCommand":
        git(repo, "config", override, "arbitrary shell command")
    else:
        monkeypatch.setenv(override, "arbitrary shell command")
    assert gd.github_repository("git@github-alt:owner/repo", cwd=str(repo)) is None
    assert ssh_aliases == []
    # Keep the standard legacy branch unchanged.
    assert gd.github_repository("git@github.com:owner/repo", cwd=str(repo)) == "owner/repo"


@pytest.mark.parametrize("source", ["GIT_SSH_VARIANT", "ssh.variant"])
@pytest.mark.parametrize("variant", ["plink", "simple", "unknown", ""])
def test_non_openssh_variant_rejected_before_expansion(repo, ssh_aliases, monkeypatch, source, variant):
    if source == "GIT_SSH_VARIANT":
        monkeypatch.setenv(source, variant)
    else:
        monkeypatch.delenv("GIT_SSH_VARIANT", raising=False)
        git(repo, "config", source, variant)
    assert gd.github_repository("git@github-alt:owner/repo", cwd=str(repo)) is None
    assert ssh_aliases == []
    assert gd.github_repository("git@github.com:owner/repo", cwd=str(repo)) == "owner/repo"


@pytest.mark.parametrize("variant", ["auto", "ssh"])
def test_openssh_variant_environment_overrides_git_config(repo, ssh_aliases, monkeypatch, variant):
    git(repo, "config", "ssh.variant", "plink")
    monkeypatch.setenv("GIT_SSH_VARIANT", variant)
    assert gd.github_repository("git@github-alt:owner/repo", cwd=str(repo)) == "owner/repo"
    assert len(ssh_aliases) == 1


@pytest.mark.parametrize("variant", ["auto", "ssh"])
def test_openssh_variant_git_config_accepted(repo, ssh_aliases, monkeypatch, variant):
    monkeypatch.delenv("GIT_SSH_VARIANT", raising=False)
    git(repo, "config", "ssh.variant", variant)
    assert gd.github_repository("git@github-alt:owner/repo", cwd=str(repo)) == "owner/repo"
    assert len(ssh_aliases) == 1


def test_alias_pr_identity_and_push_target_preserve_original_transport(repo, ssh_aliases):
    url = "git@github-alt:Owner/Repo.git"
    git(repo, "remote", "set-url", "origin", url)
    assert gd.origin_github_repo(str(repo)) == "Owner/Repo"
    assert gd._push_target(str(repo), "owner/repo") == url
    assert len(ssh_aliases) == 2  # Each check expands fresh configuration.
    git(repo, "config", "remote.origin.pushurl", "git@github-alt:other/repo.git")
    with pytest.raises(gd.GitDeliveryError, match="单一 GitHub 仓库"):
        gd._push_target(str(repo), "owner/repo")
    git(repo, "config", "--add", "remote.origin.pushurl", url)
    with pytest.raises(gd.GitDeliveryError, match="单一 GitHub 仓库"):
        gd._push_target(str(repo), "owner/repo")


def test_strict_origin_resolves_real_openssh_alias_without_changing_transport(repo, ssh_aliases):
    git(repo, "remote", "set-url", "origin", "git@github-alt:Owner/Repo.git")
    before = (repo / ".git" / "config").read_bytes()
    assert gd.require_github_origin(str(repo), check_push=True) == "Owner/Repo"
    assert len(ssh_aliases) == 2
    assert (repo / ".git" / "config").read_bytes() == before


def test_alias_push_target_rewrite_chain_rejected(repo, ssh_aliases):
    url = "git@github-alt:owner/repo.git"
    git(repo, "remote", "set-url", "origin", "alias://delivery")
    git(repo, "config", f"url.{url}.pushInsteadOf", "alias://delivery")
    git(repo, "config", "url.https://github.com/owner/repo.git.insteadOf", url)
    with pytest.raises(gd.GitDeliveryError, match="重复 URL 改写"):
        gd._push_target(str(repo), "owner/repo")


def test_adopt_registered_empty_local_branch_fast_forwards(repo, tmp_path):
    root = tmp_path / "worktrees"
    delivery = branch(repo, root)
    # Advance only the remote/base, leaving the registered task branch behind.
    other = tmp_path / "publisher"
    subprocess.run(["git", "clone", str(repo.parent / "origin.git"), str(other)], check=True, capture_output=True)
    git(other, "config", "user.name", "Publisher")
    git(other, "config", "user.email", "publisher@example.invalid")
    (other / "new-source.txt").write_text("upstream change")
    git(other, "add", ".")
    git(other, "commit", "-m", "advance")
    git(other, "push", "origin", "main")
    adopted = branch(repo, root)
    assert adopted["adopted"] is True
    assert adopted["head_sha"] != delivery["head_sha"]
    assert git(adopted["worktree_path"], "rev-parse", "HEAD") == adopted["base_sha"]
    assert git(adopted["worktree_path"], "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.parametrize("reason", ["remote", "unregistered", "own_commit", "dirty", "missing_directory"])
def test_adoption_rejects_five_unsafe_shapes(repo, tmp_path, reason):
    root = tmp_path / "worktrees"
    if reason in ("remote", "unregistered"):
        git(repo, "branch", "kaneo/task1234")
        if reason == "remote":
            git(repo, "push", "origin", "kaneo/task1234")
    elif reason == "missing_directory":
        delivery = branch(repo, root)
        import shutil
        shutil.rmtree(delivery["worktree_path"])
    else:
        delivery = branch(repo, root)
        worktree = Path(delivery["worktree_path"])
        (worktree / "untracked-source.txt").write_text("preserve work")
        if reason == "own_commit":
            git(worktree, "add", ".")
            git(worktree, "commit", "-m", "own work")
    with pytest.raises(gd.GitDeliveryError) as caught:
        branch(repo, root)
    assert caught.value.code == "branch_exists"
    expected = {"remote": "origin", "unregistered": "预期路径", "own_commit": "自有提交", "dirty": "未跟踪", "missing_directory": "目录缺失"}
    assert expected[reason] in caught.value.message


def test_missing_base_uses_real_default_name(repo):
    with pytest.raises(gd.GitDeliveryError, match="origin 上不存在分支 missing（默认分支 main）"):
        gd.resolve_base(str(repo), "missing")
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    with pytest.raises(gd.GitDeliveryError) as caught:
        gd.resolve_base(str(repo), "missing")
    assert "默认分支" not in caught.value.message


@pytest.mark.parametrize("relative", ["protected.bin", "build/cache.bin"])
@pytest.mark.parametrize("late", [False, True])
def test_adoption_preserves_ignored_files_when_upstream_tracks_same_path(repo, tmp_path, relative, late):
    root = tmp_path / "worktrees"
    delivery = branch(repo, root)
    worktree = Path(delivery["worktree_path"])
    with (repo / ".git/info/exclude").open("a") as exclude:
        exclude.write("\n" + relative + "\n")
    local = worktree / relative
    local.parent.mkdir(parents=True, exist_ok=True)
    if not late:
        local.write_text("private ignored local data")
    other = tmp_path / "publisher"
    subprocess.run(["git", "clone", str(repo.parent / "origin.git"), str(other)], check=True, capture_output=True)
    git(other, "config", "user.name", "Publisher")
    git(other, "config", "user.email", "publisher@example.invalid")
    upstream = other / relative
    upstream.parent.mkdir(parents=True, exist_ok=True)
    upstream.write_text("upstream tracked contents")
    git(other, "add", relative)
    git(other, "commit", "-m", "track previously ignored path")
    git(other, "push", "origin", "main")
    original = gd._git
    def appeared_after_status(args, directory, *a, **kw):
        result = original(args, directory, *a, **kw)
        if late and args[0] == "status" and Path(directory) == worktree:
            local.write_text("private ignored local data")
        return result
    with patch.object(gd, "_git", side_effect=appeared_after_status):
        with pytest.raises(gd.GitDeliveryError) as caught:
            branch(repo, root)
    assert caught.value.code == "branch_exists"
    assert local.read_text() == "private ignored local data"
    assert git(worktree, "rev-parse", "HEAD") == delivery["head_sha"]
