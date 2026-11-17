"""Verification-only continuations retain authentic task work and still enter Review."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import test_cmd_delivery as fixtures
from kanpilot import core, db, git_delivery as gd, round_store, util
from kanpilot.engine import verdict
from test_git_delivery import branch, commit, git, repo


@pytest.fixture
def case():
    instance = fixtures.DeliveryCommit(methodName="test_commit_idempotent_and_event")
    instance.setUp()
    try:
        yield instance
    finally:
        instance.tearDown()
        instance.doCleanups()


def continue_run(case, run="exec2", *, exit_code=0):
    """Fresh executor-produced evidence; never execute frozen verify in Pilot."""
    db.update_task(case.ctx.conn, "task1234", case.ctx.now_iso, run_id=run)
    directory = case.runs_root / "task1234" / run
    directory.mkdir()
    old = case.runs_root / "task1234" / "exec1"
    for name in ("job.json", "spec.json"):
        data = json.loads((old / name).read_text())
        data["run_id"] = run
        if name == "spec.json":
            data["runs_dir"] = str(directory)
        (directory / name).write_text(json.dumps(data))
    events = [json.loads(line) for line in (old / "events.jsonl").read_text().splitlines()]
    events[1]["exit_code"] = exit_code
    (directory / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    config = {"connection_file": str(case.root / "fake-plus.json")}
    round_store.upsert_on_claim(case.ctx.conn, "task1234", run, config,
                                case.ctx.now_iso, backend="pi_desktop")
    round_store.mark_started(case.ctx.conn, "task1234", run, case.ctx.now_iso, worktree_path=str(case.wt))
    round_store.upsert_discovered(case.ctx.conn, "task1234", run, case.ctx.now_iso,
                                 backend="pi_desktop", session_id="delivery-session", run_status="finished")
    case.payload["expected_execution_run_id"] = run


def reverify(case):
    task = db.require_task(case.ctx.conn, "task1234")
    return case.call("engine-verdict", dict(case.payload, expected_plan_sha256=task["plan_sha256"],
                                          operation_id="verify-" + task["run_id"]))


def initial_commit(case):
    (case.wt / "change").write_text("verified")
    case.live_verdict()
    return case.call_commit()["sha"]


def test_verified_prior_commit_reused_then_enters_independent_review(case):
    sha = initial_commit(case)
    evidence = case.wt / ".pi" / "evidence" / "native" / "acceptance.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"passed":true}')
    before_index = git(case.wt, "ls-files", "--stage")
    continue_run(case)
    receipt = reverify(case)
    assert receipt["verification_ok"] is True
    assert receipt["empty_diff"] is False
    with pytest.raises(util.KanpilotError, match="本轮执行尚未完成"):
        core.require_committed_review(db.require_task(case.ctx.conn, "task1234"))
    assert case.call_commit() == {"outcome": "already_committed", "sha": sha, "files": ["change"]}
    assert case.call_commit() == {"outcome": "already_committed", "sha": sha}
    task = db.require_task(case.ctx.conn, "task1234")
    ledger = core.delivery_of(task)["commits"]
    assert [(row["execution_run_id"], row["sha"]) for row in ledger] == [("exec1", sha), ("exec2", sha)]
    assert ledger[-1]["reused_from_execution_run_id"] == "exec1"
    assert verdict.valid_committed_receipt(case.ctx.conn, task, receipt)
    core.require_committed_review(task)
    assert git(case.wt, "rev-parse", "HEAD") == sha
    assert git(case.wt, "ls-files", "--stage") == before_index
    assert evidence.read_text() == '{"passed":true}'
    assert case.ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind='delivery_commit_reused'").fetchone()[0] == 1
    case.fixture["remote"]["status"] = "in-progress"
    db.update_request(case.ctx.conn, case.payload["request_id"], case.ctx.now_iso, claimed_by="kaneo-monitor")
    result = case.call("advance-task", {"task_id": "task1234", "run_id": "monitor1",
                                        "target": "in-review", "expected_current": "in-progress"})
    assert result["task_status"] == "in_review"
    assert case.fixture["remote"]["status"] == "in-review"
    assert case.ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE task_id='task1234' AND kind='review'").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["failed-verify", "source-drift"])
def test_prior_commit_does_not_replace_current_verification(case, kind):
    initial_commit(case)
    continue_run(case, exit_code=1 if kind == "failed-verify" else 0)
    if kind == "source-drift":
        (case.wt / "change").write_text("unverified")
        assert reverify(case)["verification_ok"] is False
    else:
        assert reverify(case)["verification_ok"] is False
    assert case.call_commit()["outcome"] == "blocked"
    assert len(core.delivery_of(db.require_task(case.ctx.conn, "task1234"))["commits"]) == 1


def registered(delivery, run="run1"):
    result = commit(delivery, run=run)
    return dict(delivery, head_sha=result["sha"],
                commits=[{"sha": result["sha"], "execution_run_id": run}])


@pytest.mark.parametrize("kind", ["missing-ledger", "wrong-run", "wrong-task", "unregistered-intermediate", "head-drift"])
def test_registered_proof_rejects_unowned_history(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    if kind == "unregistered-intermediate":
        (wt / "foreign").write_text("foreign")
        git(wt, "add", "foreign")
        git(wt, "commit", "-m", "foreign commit")
        delivery["head_sha"] = git(wt, "rev-parse", "HEAD")
    (wt / "change").write_text("source")
    proof = registered(delivery)
    if kind == "missing-ledger":
        proof["commits"] = []
    elif kind == "wrong-run":
        proof["commits"][0]["execution_run_id"] = "other"
    elif kind == "head-drift":
        git(wt, "commit", "--allow-empty", "-m", "drift")
    with pytest.raises(gd.GitDeliveryError):
        gd.registered_task_changes(proof, task_id="other" if kind == "wrong-task" else "task1234")


@pytest.mark.parametrize("kind", ["evidence-only", "reverted"])
def test_registered_history_without_net_source_stays_empty(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    if kind == "evidence-only":
        path = wt / ".pi" / "settings.json"
        path.parent.mkdir()
        path.write_text("{}")
    else:
        (wt / "change").write_text("source")
    proof = registered(delivery)
    if kind == "reverted":
        (wt / "change").unlink()
        result = commit(proof, run="run2")
        proof["head_sha"] = result["sha"]
        proof["commits"].append({"sha": result["sha"], "execution_run_id": "run2"})
    assert gd.registered_task_changes(proof, task_id="task1234")["files"] == []


@pytest.mark.parametrize("kind", ["file-link", "parent-link", "fifo", "nested-repo"])
def test_untracked_evidence_rejects_unsafe_paths(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    evidence = wt / ".pi" / "evidence"
    evidence.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "proof").write_text("outside")
    if kind == "file-link":
        (evidence / "proof").symlink_to(outside / "proof")
    elif kind == "parent-link":
        (evidence / "linked").symlink_to(outside, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(evidence / "proof")
    else:
        git(evidence, "init", "nested")
        (evidence / "nested" / "proof").write_text("nested")
    with pytest.raises(gd.GitDeliveryError):
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])


@pytest.mark.parametrize("kind", ["staged", "tracked-modification"])
def test_explicit_evidence_changes_remain_delivery_changes(repo, tmp_path, kind):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    path = wt / ".pi" / "evidence" / "proof.json"
    path.parent.mkdir(parents=True)
    path.write_text("original")
    git(wt, "add", ".pi/evidence")
    if kind == "tracked-modification":
        git(wt, "commit", "-m", "tracked proof")
        delivery["head_sha"] = git(wt, "rev-parse", "HEAD")
        path.write_text("changed")
    assert gd.effective_worktree_changes(wt)[0]["path"] == ".pi/evidence/proof.json"
    with pytest.raises(gd.GitDeliveryError):
        gd.verify_branch_worktree(delivery, require_clean=True, expected_head=delivery["head_sha"])


@pytest.mark.parametrize("with_source", [False, True])
def test_untracked_ordinary_evidence_is_preserved_outside_source_commit(repo, tmp_path, with_source):
    delivery = branch(repo, tmp_path / "worktrees")
    wt = Path(delivery["worktree_path"])
    evidence = wt / ".pi" / "evidence" / "native.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("native acceptance")
    if with_source:
        (wt / "change").write_text("source")
        result = commit(delivery)
        assert result["files"] == ["change"]
        assert not git(wt, "ls-tree", "-r", "--name-only", "HEAD", "--", ".pi/evidence")
    else:
        assert verdict.effective_changes(wt) == []
        with pytest.raises(gd.GitDeliveryError) as caught:
            commit(delivery)
        assert caught.value.code == "no_changes"
    assert evidence.read_text() == "native acceptance"


def test_concurrent_original_ledger_change_is_not_overwritten(case):
    initial_commit(case)
    continue_run(case)
    reverify(case)
    original = gd.registered_task_changes

    def changed(*args, **kwargs):
        proof = original(*args, **kwargs)
        task = db.require_task(case.ctx.conn, "task1234")
        core.update_delivery(case.ctx.conn, task, case.ctx.now_iso, commits=[])
        return proof

    with patch.object(gd, "registered_task_changes", side_effect=changed):
        with pytest.raises(util.KanpilotError) as caught:
            case.call_commit()
    assert caught.value.code == "conflict"
    assert core.delivery_of(db.require_task(case.ctx.conn, "task1234"))["commits"] == []
