"""Task branch naming and compatibility against disposable local Git state."""
import sqlite3
import subprocess

import pytest

from kanpilot import branch_naming, git_delivery


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True,
                          env=git_delivery.git_env()).stdout.strip()


def test_branch_date_uses_taipei_calendar_day():
    branch = branch_naming.new_branch("task1234", kind="fix", slug="repair-player",
                                      created_at="2026-11-11T16:05:00+00:00")
    assert branch == "fix/20261112-task1234-repair-player"
    assert branch_naming.matches(branch, "task1234")


@pytest.mark.parametrize("kind", ["feat", "fix", "docs", "chore", "refactor", "test"])
def test_conventional_title_selects_branch_type(kind):
    branch = branch_naming.new_branch("task1234", {
        "title": kind + ": Repair player", "created_at": "2026-11-12T00:00:00Z"})
    assert branch.startswith(kind + "/20261112-task1234-")
    assert branch_naming.matches(branch, "task1234")


def test_sqlite_task_metadata_and_chinese_title_fallback():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        task = conn.execute("SELECT '修复播放器' AS title, 'review_fix' AS plan_kind, "
                            "'2026-11-12T00:00:00Z' AS created_at").fetchone()
        branch = branch_naming.new_branch("Task_1234", task)
        assert branch == "fix/20261112-task_1234-task"
        assert branch_naming.matches(branch, "Task_1234")
    finally:
        conn.close()


def test_branch_slug_is_safe_for_git():
    branch = branch_naming.new_branch("task1234", slug="  Player / API.. FIX@{retry} _ v2  ",
                                      created_at="2026-11-12T00:00:00Z")
    subprocess.run(["git", "check-ref-format", "--branch", branch], check=True,
                   capture_output=True, env=git_delivery.git_env())
    assert branch_naming.matches(branch, "task1234")
    assert branch == branch.lower()


def test_registered_branch_identity_does_not_depend_on_current_title():
    original = branch_naming.new_branch("task1234", {"title": "feat: Old title"},
                                        created_at="2026-11-12T00:00:00Z")
    renamed = branch_naming.new_branch("task1234", {"title": "docs: New title"},
                                       created_at="2026-11-13T00:00:00Z")
    assert original != renamed
    assert branch_naming.matches(original, "task1234")


@pytest.mark.parametrize("branch", [
    "kaneo/task1234", "feat/20261112-task1234-player", "fix/20261112-task1234-repair",
])
def test_legacy_and_standard_names_match_only_their_task(branch):
    assert branch_naming.matches(branch, "task1234")
    assert not branch_naming.matches(branch, "task123")
    assert not branch_naming.matches(branch, "other1234")


@pytest.mark.parametrize("branch", [
    "main", "kaneo/task1234-build-1234", "feat/20261112-task1234",
    "other/20261112-task1234-player", "feat/20261112-task1234-../player",
    "feat/20261112-task1234-player/extra", None,
])
def test_invalid_or_unrelated_branches_are_rejected(branch):
    assert not branch_naming.matches(branch, "task1234")


def test_standard_branch_creates_verified_worktree_without_touching_primary(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Branch Fixture")
    git(repo, "config", "user.email", "branch@example.invalid")
    (repo / "tracked.txt").write_text("baseline\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "baseline")
    (repo / "tracked.txt").write_text("primary change\n")
    primary = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))
    name = "fix/20261112-task1234-player"
    delivery = git_delivery.prepare_local_branch_worktree(
        str(repo), "task1234", worktrees_root=tmp_path / "worktrees", branch=name)
    assert delivery["branch"] == name
    assert git(delivery["worktree_path"], "branch", "--show-current") == name
    git_delivery.verify_branch_worktree(dict(delivery, repository=str(repo)),
                                       require_clean=True, expected_head=primary[0])
    assert primary == (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))


def test_reserved_name_survives_midnight_title_change_and_rejects_wrong_repository(tmp_path, monkeypatch):
    from kanpilot import db, util
    conn = db.connect(tmp_path / "state.db")
    try:
        monkeypatch.setattr(util, "now_iso", lambda: "2026-11-11T15:59:00Z")
        first = branch_naming.reserve(conn, "task1234", str(tmp_path / "repo"), {"title": "fix: Player"})
        monkeypatch.setattr(util, "now_iso", lambda: "2026-11-11T16:01:00Z")
        assert first == "fix/20261111-task1234-player"
        assert branch_naming.reserve(conn, "task1234", str(tmp_path / "repo"), {"title": "feat: Renamed"}) == first
        with pytest.raises(util.KanpilotError):
            branch_naming.reserve(conn, "task1234", str(tmp_path / "other"))
    finally:
        conn.close()


def test_upgrade_reserves_legacy_tree_then_applies_existing_adoption_gates(tmp_path):
    from kanpilot import db
    from test_git_delivery import make_repo
    repo = make_repo(tmp_path / "git")
    root = tmp_path / "worktrees"
    legacy = "kaneo/task1234"
    git_delivery.prepare_branch_worktree(str(repo), "task1234", base_branch="main",
                                         worktrees_root=root, branch=legacy)
    conn = db.connect(tmp_path / "state.db")
    try:
        name = branch_naming.reserve(conn, "task1234", str(repo), worktrees_root=root)
        assert name == legacy
        adopted = git_delivery.prepare_branch_worktree(str(repo), "task1234", base_branch="main",
                                                       worktrees_root=root, branch=name)
        assert adopted["adopted"]
        (root / "task1234" / "run" / "user.txt").write_text("preserve")
        with pytest.raises(git_delivery.GitDeliveryError):
            git_delivery.prepare_branch_worktree(str(repo), "task1234", base_branch="main",
                                                 worktrees_root=root, branch=name)
    finally:
        conn.close()
