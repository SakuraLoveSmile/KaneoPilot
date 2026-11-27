"""Project-scoped plans and merged local branches, with real isolated Git repositories."""
from pathlib import Path

import pytest

from kanpilot import (constants, db, git_delivery, merged_worktrees as cleanup, planfile,
                      planner_runner, util, worktree_pool as pool)


def git(repo, *args):
    return pool._value(repo, *args)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(tmp_path / "support"))
    monkeypatch.setenv("KANPILOT_PLAN_ROOT", str(tmp_path / "old-plans"))
    monkeypatch.setattr(cleanup.audit, "_cwd_listing", lambda: [])
    repo = tmp_path / "MediaPlayer"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README").write_text("base\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "base")
    conn = db.connect(tmp_path / "test.db")
    unified = tmp_path / "Project Worktree"
    db.set_setting(conn, "worktrees_root", str(unified / "personal-kaneopilot" / "managed-worktrees"))
    tid = "task01"
    stamp = util.now_iso()
    conn.execute("INSERT INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,updated_at,"
                 "extra_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 (tid, "media", repo.name, str(repo), "review", "in_review", stamp, stamp, "{}"))
    conn.commit()
    try:
        yield repo, conn, tid, unified
    finally:
        conn.close()


def plan_and_merge(project):
    repo, conn, tid, unified = project
    root = pool.project_root(conn, repo, tid)
    assert root == unified / repo.name / "managed-worktrees"
    assert pool.project_root(conn, repo.with_name("Music"), "other") != root
    run = root / tid / "run"
    branch = "kaneo/" + tid
    git(repo, "worktree", "add", "-qb", branch, str(run), "main")
    for stage in ("plan", "review"):
        lease = pool.acquire(conn, repo, task_id=tid, stage=stage, holder_id=stage, sha="HEAD")
        assert Path(lease["path"]) == run.parent / stage
        pool.release(conn, lease["slot_id"], lease["holder_id"], lease["lease_token"], owner_stopped=True)
    task = dict(db.require_task(conn, tid))
    context = {"task_id": tid, "task": dict(task, title="测试"), "payload": {}}
    frozen = planner_runner.write_plan_file(context, {}, "## 目标\n\n可读的项目计划", util.now_iso())
    task.update(plan_path=str(frozen), plan_sha256=util.sha256_file(frozen))
    rel = planfile.checkout_plan(task, run, install=True)
    assert Path(rel).parts[:2] == ("docs", "plans")
    assert not (repo / rel).exists()
    assert planfile.checkout_plan(task, run, install=True) == rel
    assert not constants.plan_root().exists()
    git_delivery.verify_branch_worktree({"branch": branch, "worktree_path": str(run), "repository": str(repo)},
                                         require_clean=True, expected_head=git(repo, "rev-parse", "HEAD"),
                                         pending_document=planfile.dispatch_document(task, run))
    (run / "feature").write_text("implemented\n")
    git(run, "add", ".")
    git(run, "commit", "-qm", "feature with plan")
    git(repo, "merge", "--no-ff", "--no-edit", branch)
    head = git(run, "rev-parse", "HEAD")
    identity = {"branch": branch, "head": head, "pr": 21, "path": str(run), "base": "main",
                "repository": str(repo), "merged_at": util.now_iso(), "merged_head": head}
    delivery = {"branch": branch, "head_sha": head, "pr_number": 21, "pr_state": "MERGED",
                "merged_at": identity["merged_at"], "base_branch": "main", "mode": "pr",
                "worktree_path": str(run), cleanup.KEY: identity}
    db.update_task(conn, tid, util.now_iso(), status="done", plan_path=str(frozen),
                   plan_sha256=task["plan_sha256"], extra_json={"delivery": delivery})
    return frozen, rel, run, identity


def test_merge_cleanup_preserves_plan_and_remote(project):
    repo, conn, tid, _ = project
    frozen, rel, run, identity = plan_and_merge(project)
    remote = repo.with_name("origin.git")
    git(repo, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main", identity["branch"])
    ref = "refs/remotes/origin/" + identity["branch"]
    (run / ".pi").mkdir()
    (run / ".pi" / "evidence.txt").write_text("session evidence")
    cleanup.sweep(conn, util.now_iso())
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + identity["branch"])
    assert git(repo, "rev-parse", ref) == identity["head"]
    assert git(remote, "rev-parse", identity["branch"]) == identity["head"]
    assert (repo / rel).read_bytes() == planfile.load_plan(str(frozen), tid, repository=repo)[1]
    assert list((constants.app_support() / "KaneoPilot-archives").rglob("evidence.txt"))
    cleanup.sweep(conn, util.now_iso())
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='merged_worktrees_removed'").fetchone()[0] == 1


def test_dirty_or_busy_tree_is_retained(project, monkeypatch):
    repo, conn, tid, _ = project
    frozen, _, run, identity = plan_and_merge(project)
    (run / "personal.txt").write_text("unique")
    cleanup.sweep(conn, util.now_iso())
    assert (run / "personal.txt").read_text() == "unique"
    (run / "personal.txt").unlink()
    monkeypatch.setattr(cleanup.audit, "_cwd_listing", lambda: [str(run)])
    cleanup.sweep(conn, util.now_iso())
    assert run.is_dir() and git(repo, "rev-parse", identity["branch"]) == identity["head"]
    with pytest.raises(util.KanpilotError):
        planfile.load_plan(str(frozen), "different", repository=repo)
    with pytest.raises(util.KanpilotError):
        planfile.load_plan(str(frozen), tid, repository=repo.parent)


def test_registered_old_task_remains_at_its_original_root(project):
    repo, conn, tid, unified = project
    old = unified / "personal-kaneopilot" / "managed-worktrees"
    db.update_task(conn, tid, util.now_iso(), extra_json={
        "delivery": {"branch": "kaneo/" + tid, "worktree_path": str(old / tid / "run")}})
    assert pool.project_root(conn, repo, tid) == old
