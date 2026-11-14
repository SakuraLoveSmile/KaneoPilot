"""Re-bind of a base-advanced PR head: isolated Git/SQLite, fake GitHub only."""
from unittest.mock import patch

import pytest

from kanpilot import (cli, cmd_delivery_pr as commands, core, db, gh_client, project_config,
                     remote, util)
from test_delivery_pr import DeliveryPR
from test_git_delivery import git


@pytest.fixture
def case():
    value = DeliveryPR()
    value.setUp()
    try:
        value.update(pr_number=7, pushed_sha=value.delivery["head_sha"])
        task = value.task()
        db.set_setting(value.ctx.conn, project_config.SETTINGS_KEY, {
            "items": {task["project_id"]: {"repository": str(value.repo), "enabled": False,
                "automation": {"enabled": False, "auto_merge_pr": True, "policy_revision": 1}}}})
        with db.write_txn(value.ctx.conn):
            for row in value.ctx.conn.execute("SELECT request_id FROM requests WHERE status IN ('queued','claimed')"):
                db.update_request(value.ctx.conn, row["request_id"], value.ctx.now_iso, status="succeeded")
        yield value
    finally:
        value.doCleanups()
        value.tearDown()


def claim_delivery(case):
    """Claim the newest queued delivery request and return a sync payload."""
    row = case.ctx.conn.execute(
        "SELECT request_id FROM requests WHERE kind='delivery' AND status='queued' "
        "ORDER BY updated_at DESC LIMIT 1").fetchone()
    assert row is not None
    with db.write_txn(case.ctx.conn):
        db.update_request(case.ctx.conn, row["request_id"], case.ctx.now_iso, status="claimed",
                          run_id="delivery2", lease_expires_at=util.plus_seconds(3600, case.ctx.now))
    return {"task_id": "task1234", "request_id": row["request_id"], "run_id": "delivery2"}


def advance_branch(case, *, plain_commit=False, extra_parent=False):
    """Create what GitHub's "Update branch" produces, on the bare origin."""
    worktree = case.delivery["worktree_path"]
    frozen = case.delivery["head_sha"]
    base_tip = git(case.repo, "rev-parse", "origin/main")
    base_tree = git(case.repo, "rev-parse", base_tip + "^{tree}")
    base_commit = git(case.repo, "commit-tree", base_tree, "-p", base_tip, "-m", "base advance")
    git(case.repo, "push", "-q", "origin", base_commit + ":refs/heads/main")
    tree = git(worktree, "merge-tree", "--write-tree", frozen, base_commit).splitlines()[0].strip()
    if plain_commit:
        head = git(worktree, "commit-tree", tree, "-p", frozen, "-m", "plain child")
    else:
        parents = ["-p", frozen, "-p", base_commit]
        if extra_parent:
            unrelated = git(worktree, "commit-tree",
                            git(case.repo, "rev-parse", base_commit + "^{tree}"),
                            "-m", "unrelated base")
            parents += ["-p", unrelated]
        head = git(worktree, "commit-tree", tree, *parents, "-m", "update branch")
    git(case.repo, "push", "-q", "origin", head + ":refs/heads/kaneo/task1234")
    return frozen, base_commit, head


def rebind(case, head, pr_head, **extra):
    payload = dict(task_id="task1234", expected_head_sha=head, expected_pr_head_sha=pr_head,
                   confirm=True, note="test")
    payload.update(extra)
    with patch.object(gh_client, "pr_view", return_value=case.pr(headRefOid=pr_head)):
        return cli.run("rebind-delivery-head", payload, case.ctx.db_path)


def test_head_advanced_by_base_merge_is_rebound(case):
    frozen, base_commit, merge = advance_branch(case)
    result = rebind(case, frozen, merge)
    assert result["merge_head_sha"] == merge
    task = case.task()
    assert task["status"] == "in_review"
    value = core.delivery_of(task)
    assert value["head_sha"] == frozen
    assert value["merge_head_sha"] == merge
    assert value["head_advance"]["base_tip"] == base_commit
    assert value["head_advance"]["reviewed_paths"]
    assert value.get("auto_merge_stall") is None
    assert case.ctx.conn.execute(
        "SELECT count(*) FROM events WHERE kind='delivery_head_advanced'").fetchone()[0] == 1
    assert case.ctx.conn.execute(
        "SELECT count(*) FROM requests WHERE kind='delivery' AND status='queued'").fetchone()[0] == 1


def test_rebound_head_is_what_sync_merges(case):
    frozen, base_commit, merge = advance_branch(case)
    rebind(case, frozen, merge)
    payload = claim_delivery(case)
    task = case.task()
    source = {"task": {"id": task["task_id"], "projectId": task["project_id"],
                       "status": "in-review"},
              "requirements_sha256": task["requirements_sha256"]}
    clean = dict(case.pr(headRefOid=merge), isDraft=False, mergeable="MERGEABLE",
                 mergeStateStatus="CLEAN", reviewDecision="", statusCheckRollup=[])
    merged = dict(clean, state="MERGED", mergedAt="2026-11-14T00:00:00Z", headRefOid=merge)
    calls = []
    def merge_pr(repo, number, *, sha):
        calls.append(sha)
        return {"merged": True}
    with patch.object(gh_client, "pr_view", side_effect=[clean, merged, merged]), \
         patch.object(remote, "get_task", side_effect=case.outside("get_task", source)), \
         patch.object(gh_client, "pr_merge", side_effect=merge_pr):
        assert commands.cmd_delivery_sync(case.ctx, payload)["outcome"] == "merged"
    assert calls == [merge]


def test_plain_child_commit_is_rejected(case):
    frozen, base_commit, head = advance_branch(case, plain_commit=True)
    with pytest.raises(util.KanpilotError) as caught:
        rebind(case, frozen, head)
    assert caught.value.code == "conflict"
    assert "merge_head_sha" not in core.delivery_of(case.task())


def test_three_parent_merge_is_rejected(case):
    frozen, base_commit, head = advance_branch(case, extra_parent=True)
    with pytest.raises(util.KanpilotError) as caught:
        rebind(case, frozen, head)
    assert caught.value.code == "conflict"


def test_wrong_expected_head_is_rejected(case):
    frozen, base_commit, merge = advance_branch(case)
    with pytest.raises(util.KanpilotError) as caught:
        rebind(case, "0" * 40, merge)
    assert caught.value.code == "conflict"


def test_head_not_containing_base_tip_is_rejected(case):
    frozen, base_commit, merge = advance_branch(case)
    # Advance main further so the old merge no longer contains the current tip.
    newer = git(case.repo, "commit-tree", git(case.repo, "rev-parse", base_commit + "^{tree}"),
                "-p", base_commit, "-m", "further advance")
    git(case.repo, "push", "-q", "origin", newer + ":refs/heads/main")
    with pytest.raises(util.KanpilotError) as caught:
        rebind(case, frozen, merge)
    assert caught.value.code == "conflict"


def test_missing_confirm_is_rejected(case):
    frozen, base_commit, merge = advance_branch(case)
    with pytest.raises(util.KanpilotError) as caught:
        cli.run("rebind-delivery-head", dict(task_id="task1234", expected_head_sha=frozen,
                                             expected_pr_head_sha=merge), case.ctx.db_path)
    assert caught.value.code == "bad_request"