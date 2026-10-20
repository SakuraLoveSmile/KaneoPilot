"""Review children retain the reviewed code while keeping their PR destination."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from kanpilot import auto_intake, core, db, git_delivery, task_workspace, util
from test_git_delivery import git, make_repo

NOW = "2026-10-18T12:00:00+00:00"


@pytest.fixture
def fix(tmp_path, tmp_env):
    repo = make_repo(tmp_path / "git")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "reviewed.txt").write_text("reviewed code\n")
    git(repo, "add", "reviewed.txt")
    git(repo, "commit", "-m", "reviewed source")
    reviewed = git(repo, "rev-parse", "HEAD")
    conn = db.connect(tmp_path / "fix.db")
    db.set_setting(conn, "worktrees_root", str(tmp_path / "trees"))
    for tid in ("source123", "fix12345"):
        conn.execute("INSERT INTO tasks(task_id,project_id,project_name,repository,stage,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (tid, "project1", "Test", str(repo), "plan", "needs_plan", NOW, NOW))
    db.update_task(conn, "source123", NOW, fix_task_id="fix12345", review_key="review-key")
    db.set_extra(conn, db.require_task(conn, "source123"), NOW,
                 review_history=[{"review_key": "review-key", "head_sha": reviewed,
                                  "fix_task_id": "fix12345", "conclusion": "changes_requested"}])
    db.update_task(conn, "fix12345", NOW, plan_kind="review_fix",
                   review_source_task_id="source123", review_key="review-key")
    delivery = git_delivery.prepare_branch_worktree(str(repo), "fix12345", base_branch="main",
                                                    worktrees_root=tmp_path / "trees")
    delivery["mode"] = "pr"
    core.update_delivery(conn, db.require_task(conn, "fix12345"), NOW, **delivery)
    ctx = SimpleNamespace(conn=conn, now_iso=NOW)
    payload = {"task_id": "fix12345", "expected_head_sha": base, "review_key": "review-key"}
    yield SimpleNamespace(repo=repo, base=base, reviewed=reviewed, conn=conn, ctx=ctx,
                          payload=payload, delivery=delivery, path=Path(delivery["worktree_path"]))
    conn.close()


def repair(fix):
    return task_workspace.cmd_repair_review_fix_workspace(fix.ctx, fix.payload)


def test_new_pr_branch_starts_at_reviewed_sha_and_keeps_policy_base(fix, tmp_path):
    delivery = git_delivery.prepare_branch_worktree(str(fix.repo), "newfix12", base_branch="main",
        initial_head_sha=fix.reviewed, worktrees_root=tmp_path / "newtrees")
    assert delivery["base_branch"] == "main"
    assert delivery["base_sha"] == fix.base
    assert delivery["head_sha"] == fix.reviewed
    assert git(delivery["worktree_path"], "rev-parse", "HEAD") == fix.reviewed


def test_repair_fast_forwards_and_registers_receipt_without_changing_pr_base(fix):
    original = git(fix.repo, "rev-parse", "HEAD")
    result = repair(fix)
    assert result["head_sha"] == fix.reviewed
    delivery = core.delivery_of(db.require_task(fix.conn, "fix12345"))
    assert delivery["base_sha"] == fix.base
    assert delivery["base_branch"] == "main"
    assert delivery["head_sha"] == fix.reviewed
    assert git(fix.path, "rev-parse", "HEAD") == fix.reviewed
    assert git(fix.repo, "rev-parse", "HEAD") == original
    assert fix.conn.execute("SELECT status FROM runtime_operations").fetchone()[0] == "accepted"
    assert repair(fix) == result


def test_git_success_before_database_commit_can_recover_same_intent(fix):
    def crash(*args, **kwargs):
        assert git(fix.path, "rev-parse", "HEAD") == fix.reviewed
        raise RuntimeError("simulated registration crash")
    with mock.patch.object(core, "update_delivery", side_effect=crash):
        with pytest.raises(RuntimeError):
            repair(fix)
    assert core.delivery_of(db.require_task(fix.conn, "fix12345"))["head_sha"] == fix.base
    assert fix.conn.execute("SELECT status FROM runtime_operations").fetchone()[0] == "unknown"
    assert repair(fix)["head_sha"] == fix.reviewed


def test_repair_intent_gates_planning_and_git_runs_outside_transaction(fix):
    original = git_delivery._git
    def checked(args, *rest, **kwargs):
        assert not fix.conn.in_transaction
        if args[0] == "merge":
            with pytest.raises(util.KanpilotError, match="恢复尚未完成"):
                task_workspace.require_no_workspace_repair(fix.conn, "fix12345")
            receipt = fix.conn.execute("SELECT status FROM runtime_operations").fetchone()
            assert receipt[0] == "unknown"
        return original(args, *rest, **kwargs)
    with mock.patch.object(git_delivery, "_git", side_effect=checked):
        repair(fix)
    task_workspace.require_no_workspace_repair(fix.conn, "fix12345")


def test_divergent_review_commit_is_never_reset_or_merged(fix):
    git(fix.repo, "checkout", "--orphan", "unrelated")
    git(fix.repo, "add", ".")
    git(fix.repo, "commit", "-m", "unrelated history")
    unrelated = git(fix.repo, "rev-parse", "HEAD")
    db.set_extra(fix.conn, db.require_task(fix.conn, "source123"), NOW,
                 review_history=[{"review_key": "review-key", "head_sha": unrelated,
                                  "fix_task_id": "fix12345", "conclusion": "changes_requested"}])
    with pytest.raises(util.KanpilotError):
        repair(fix)
    assert git(fix.path, "rev-parse", "HEAD") == fix.base
    assert fix.conn.execute("SELECT COUNT(*) FROM runtime_operations").fetchone()[0] == 0


def test_dirty_recovery_is_rejected(fix):
    with mock.patch.object(core, "update_delivery", side_effect=RuntimeError("crash")):
        with pytest.raises(RuntimeError):
            repair(fix)
    (fix.path / "note.txt").write_text("preserve recovered user changes")
    with pytest.raises(util.KanpilotError):
        repair(fix)
    assert (fix.path / "note.txt").read_text() == "preserve recovered user changes"
    assert core.delivery_of(db.require_task(fix.conn, "fix12345"))["head_sha"] == fix.base


@pytest.mark.parametrize("unsafe", ["dirty", "untracked", "ignored", "own_commit", "active",
                                    "plan", "run", "sent", "wrong_key", "wrong_link", "same_task",
                                    "commits", "pushed_sha", "pr_url", "pr_number", "pr_state",
                                    "wrong_root", "dispatch_history"])
def test_unsafe_repair_never_moves_head(fix, unsafe):
    if unsafe == "dirty":
        (fix.path / "tracked.txt").write_text("dirty")
    elif unsafe == "untracked":
        (fix.path / "note.txt").write_text("user note")
    elif unsafe == "ignored":
        exclude = Path(git(fix.path, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = fix.path / exclude
        with exclude.open("a") as stream:
            stream.write("\nignored.txt\n")
        (fix.path / "ignored.txt").write_text("ignored user data")
    elif unsafe == "own_commit":
        (fix.path / "own.txt").write_text("own")
        git(fix.path, "add", "own.txt")
        git(fix.path, "commit", "-m", "own change")
    elif unsafe == "active":
        db.create_request(fix.conn, NOW, "plan", "plan:fix12345", task_id="fix12345")
    elif unsafe in ("plan", "run", "sent"):
        field, value = {"plan": ("plan_path", "existing.md"), "run": ("run_id", "execution"),
                        "sent": ("sent", 1)}[unsafe]
        db.update_task(fix.conn, "fix12345", NOW, **{field: value})
    elif unsafe == "wrong_key":
        fix.payload["review_key"] = "other-key"
    elif unsafe == "wrong_link":
        db.update_task(fix.conn, "source123", NOW, fix_task_id="otherfix")
    elif unsafe in ("commits", "pushed_sha", "pr_url", "pr_number", "pr_state"):
        core.update_delivery(fix.conn, db.require_task(fix.conn, "fix12345"), NOW, **{unsafe: "published"})
    elif unsafe == "wrong_root":
        db.set_setting(fix.conn, "worktrees_root", str(fix.path.parent / "elsewhere"))
    elif unsafe == "dispatch_history":
        request, _ = db.create_request(fix.conn, NOW, "dispatch", "dispatch:fix12345", task_id="fix12345")
        db.update_request(fix.conn, request["request_id"], NOW, status="failed")
    else:
        db.update_task(fix.conn, "fix12345", NOW, review_source_task_id="fix12345")
    before = git(fix.path, "rev-parse", "HEAD")
    with pytest.raises(util.KanpilotError):
        repair(fix)
    assert git(fix.path, "rev-parse", "HEAD") == before


def test_auto_intake_passes_exact_frozen_review_head(fix, tmp_path):
    task = db.require_task(fix.conn, "fix12345")
    core.update_delivery(fix.conn, task, NOW, branch=None)
    prepared = {"input_id": "test-input", "input_sha256": "hash", "can_start": True}
    def run(command, *args):
        if command == "prepare-plan-input":
            return prepared
        return {"results": [{"request_id": "queued-plan"}]}
    with mock.patch.object(auto_intake.cli, "run", side_effect=run), \
         mock.patch.object(git_delivery, "prepare_branch_worktree", return_value=fix.delivery) as prepare, \
         mock.patch.object(auto_intake.planner_config, "merged", return_value={"backend": "pi_desktop", "default_method": "builtin"}):
        response = auto_intake._submit(fix.conn, tmp_path / "fix.db", "fix12345",
            {"delivery_base": "main", "policy_revision": 1}, NOW, "requirements", wanted=True)
    assert response["request_id"] == "queued-plan"
    assert prepare.call_args.kwargs["initial_head_sha"] == fix.reviewed
    assert prepare.call_args.kwargs["base_branch"] == "main"


def test_intake_waits_for_review_registration_without_blocking_child(fix, tmp_path):
    source = db.require_task(fix.conn, "source123")
    frozen = db.task_extra(source)["review_history"]
    db.set_extra(fix.conn, source, NOW, review_history=[])
    core.update_delivery(fix.conn, db.require_task(fix.conn, "fix12345"), NOW, branch=None)
    with mock.patch.object(auto_intake.cli, "run") as run, \
         mock.patch.object(auto_intake, "_failure") as failure:
        result = auto_intake._submit(fix.conn, tmp_path / "fix.db", "fix12345",
            {"delivery_base": "main", "policy_revision": 1}, NOW, "requirements", wanted=True)
    assert result["reason"] == "review_fix_waiting"
    run.assert_not_called()
    failure.assert_not_called()
    assert db.require_task(fix.conn, "fix12345")["status"] == "needs_plan"
    assert core.automation_of(db.require_task(fix.conn, "fix12345")) == {}
    db.set_extra(fix.conn, db.require_task(fix.conn, "source123"), NOW, review_history=frozen)
    test_auto_intake_passes_exact_frozen_review_head(fix, tmp_path)


def test_registered_review_without_head_is_a_real_failure(fix):
    source = db.require_task(fix.conn, "source123")
    entries = db.task_extra(source)["review_history"]
    entries[0].pop("head_sha")
    db.set_extra(fix.conn, source, NOW, review_history=entries)
    with pytest.raises(util.KanpilotError) as error:
        task_workspace.review_fix_head(fix.conn, db.require_task(fix.conn, "fix12345"), allow_pending=True)
    assert error.value.code == "worktree_invalid"


def test_registered_review_with_missing_history_cannot_wait_forever(fix):
    db.set_extra(fix.conn, db.require_task(fix.conn, "source123"), NOW, review_history=[])
    db.add_event(fix.conn, NOW, "review_registered", task_id="source123",
                 detail={"review_key": "review-key", "fix_task_id": "fix12345"})
    with pytest.raises(util.KanpilotError) as error:
        task_workspace.review_fix_head(fix.conn, db.require_task(fix.conn, "fix12345"), allow_pending=True)
    assert error.value.code == "worktree_invalid"
