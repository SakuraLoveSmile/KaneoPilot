"""Long-blocked merge gates escalate to attention instead of spinning forever."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from kanpilot import (cmd_delivery_pr as commands, constants, core, db, gh_client,
                     project_config, util)
from kanpilot.engine import attention
from test_delivery_pr import DeliveryPR


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
        yield value
    finally:
        value.doCleanups()
        value.tearDown()


def pr(case, **changes):
    value = dict(case.pr(), isDraft=False, mergeable="MERGEABLE", mergeStateStatus="CLEAN",
                 reviewDecision="", statusCheckRollup=[])
    value.update(changes)
    return value


def sync_at(case, when):
    case.ctx.now = when
    case.ctx.now_iso = util.to_iso(when)
    return commands.cmd_delivery_sync(case.ctx, case.payload)


def test_repeated_unstable_escalates_to_needs_user(case):
    unstable = pr(case, mergeStateStatus="UNSTABLE")
    start = case.ctx.now
    with patch.object(gh_client, "pr_view", return_value=unstable), \
         patch.object(gh_client, "pr_merge", create=True) as merge:
        first = sync_at(case, start + timedelta(seconds=300))
        assert first["outcome"] == "waiting"
        assert core.delivery_of(case.task())["auto_merge_stall"]["count"] == 1
        second = sync_at(case, start + timedelta(seconds=900))
        assert second["outcome"] == "waiting"
        assert core.delivery_of(case.task())["auto_merge_stall"]["count"] == 2
        third = sync_at(case, start + timedelta(seconds=2500))
    merge.assert_not_called()
    assert third["outcome"] == "blocked"
    assert third["failure_code"] == "merge_checks_stalled"
    task = case.task()
    assert task["status"] == "needs_user"
    assert task["failure_code"] == "merge_checks_stalled"
    assert task["failure_stage"] == "delivery"
    value = core.delivery_of(task)
    assert value["auto_merge_stall"]["escalated_at"]
    assert case.ctx.conn.execute(
        "SELECT count(*) FROM events WHERE kind='merge_checks_stalled'").fetchone()[0] == 1


def test_single_blocked_observation_does_not_escalate(case):
    unstable = pr(case, mergeStateStatus="UNSTABLE")
    with patch.object(gh_client, "pr_view", return_value=unstable), \
         patch.object(gh_client, "pr_merge", create=True):
        result = sync_at(case, case.ctx.now + timedelta(seconds=300))
    assert result["outcome"] == "waiting"
    assert case.task()["status"] == "in_review"


def test_waiting_checks_never_escalates(case):
    pending = pr(case, statusCheckRollup=[{"__typename": "CheckRun", "name": "Test",
                                "status": "IN_PROGRESS", "conclusion": ""}])
    start = case.ctx.now
    with patch.object(gh_client, "pr_view", return_value=pending), \
         patch.object(gh_client, "pr_merge", create=True):
        for offset in (300, 900, 1900, 4000):
            result = sync_at(case, start + timedelta(seconds=offset))
            assert result["outcome"] == "waiting"
    assert case.task()["status"] == "in_review"
    assert core.delivery_of(case.task())["auto_merge_stall"] is None


def test_reason_change_resets_the_streak(case):
    start = case.ctx.now
    first = pr(case, mergeStateStatus="UNSTABLE")
    second = pr(case, mergeStateStatus="BLOCKED")
    with patch.object(gh_client, "pr_view", side_effect=[first, second]), \
         patch.object(gh_client, "pr_merge", create=True):
        sync_at(case, start + timedelta(seconds=300))
        sync_at(case, start + timedelta(seconds=900))
    value = core.delivery_of(case.task())["auto_merge_stall"]
    assert value["count"] == 1


def test_stall_maps_to_attention_and_is_resumable(case):
    assert attention.reason_code_for("merge_checks_stalled", status="needs_user",
                                     failure_stage="delivery") == attention.DELIVERY_BLOCKED
    from kanpilot import cmd_queue
    assert cmd_queue._AUTO_RESUME_TARGETS["merge_checks_stalled"] == ("in_review", "delivery")
    assert constants.AUTOMATION_FAILURE_STAGES["merge_checks_stalled"] == "delivery"
    from kanpilot.engine import ladder
    assert ladder.classify("merge_checks_stalled") == ladder.LADDER_ENVIRONMENTAL
    assert not ladder.auto_retry_allowed(ladder.LADDER_ENVIRONMENTAL)