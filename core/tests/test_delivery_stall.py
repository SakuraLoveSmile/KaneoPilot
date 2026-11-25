"""Long-blocked merge gates escalate to attention instead of spinning forever."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from kanpilot import (cmd_delivery_pr as commands, constants, core, db, gh_client,
                     pr_auto_merge, project_config, util)
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


def _check(conclusion, status="COMPLETED", name="analyze-test"):
    return {"__typename": "CheckRun", "name": name, "workflowName": "app-ci",
            "status": status, "conclusion": conclusion}


def test_pending_checks_while_unstable_do_not_escalate(case):
    pending = pr(case, mergeStateStatus="UNSTABLE",
                 statusCheckRollup=[_check("", status="IN_PROGRESS")])
    start = case.ctx.now
    with patch.object(gh_client, "pr_view", return_value=pending), \
         patch.object(gh_client, "pr_merge", create=True):
        for offset in (300, 900, 1900, 2500):
            assert sync_at(case, start + timedelta(seconds=offset))["outcome"] == "waiting"
    assert case.task()["status"] == "in_review"
    value = core.delivery_of(case.task())
    assert value["auto_merge_stall"] is None
    assert value["auto_merge"]["state"] == "waiting_checks"
    assert "analyze-test" in value["auto_merge"]["reason"]


def test_failed_check_escalates_with_the_check_name(case):
    failing = pr(case, mergeStateStatus="UNSTABLE",
                 statusCheckRollup=[_check("FAILURE")])
    start = case.ctx.now
    with patch.object(gh_client, "pr_view", return_value=failing), \
         patch.object(gh_client, "pr_merge", create=True):
        for offset in (300, 900, 2500):
            result = sync_at(case, start + timedelta(seconds=offset))
    assert result["outcome"] == "blocked"
    assert result["failure_code"] == "merge_checks_stalled"
    task = case.task()
    assert task["status"] == "needs_user"
    assert "analyze-test" in task["blocked_summary"]
    assert "已暂停自动合并" in task["blocked_summary"]
    assert "修复检查或保护规则后继续" not in task["blocked_summary"]


def test_refresh_notice_names_the_blocking_check_read_only(case):
    case.update(pr_state="OPEN")
    reader = lambda repo, number, merge_checks=False: pr(
        case, statusCheckRollup=[_check("FAILURE")])
    before = case.task()
    notice = pr_auto_merge.refresh_notice(case.ctx.conn, before, case.ctx.now_iso, reader=reader)
    assert notice["state"] == "blocked"
    assert "analyze-test" in notice["reason"]
    after = case.task()
    assert (after["status"], after["stage"]) == (before["status"], before["stage"])
    value = core.delivery_of(after)
    assert value["auto_merge"]["reason"] == notice["reason"]
    assert value.get("auto_merge_stall") is None


def test_refresh_notice_reports_a_ready_gate(case):
    case.update(pr_state="OPEN")
    reader = lambda repo, number, merge_checks=False: pr(
        case, mergeStateStatus="CLEAN", statusCheckRollup=[_check("SUCCESS")])
    notice = pr_auto_merge.refresh_notice(case.ctx.conn, case.task(), case.ctx.now_iso,
                                          reader=reader)
    assert notice["state"] == "waiting_checks"
    assert "已满足" in notice["reason"]
    assert notice["gate_state"] == "ready"
    assert "gate_state" not in core.delivery_of(case.task())["auto_merge"]


def test_refresh_notice_keeps_an_unconfirmed_merge_receipt(case):
    case.update(pr_state="OPEN", auto_merge={"state": "unknown", "reason": "待确认",
                                             "head_sha": case.delivery["head_sha"]})
    calls = []
    reader = lambda *a, **k: calls.append(1) or case.pr()
    assert pr_auto_merge.refresh_notice(case.ctx.conn, case.task(), case.ctx.now_iso,
                                        reader=reader) is None
    assert not calls


def test_refresh_notice_skips_a_moved_head(case):
    case.update(pr_state="OPEN")
    reader = lambda *a, **k: dict(case.pr(), headRefOid="f" * 40)
    assert pr_auto_merge.refresh_notice(case.ctx.conn, case.task(), case.ctx.now_iso,
                                        reader=reader) is None


@pytest.mark.parametrize(("check", "gate_state"), [
    (_check("FAILURE"), "blocked"),
    (_check(None, status="IN_PROGRESS"), "waiting"),
])
def test_manual_recheck_refreshes_the_gate_line(case, check, gate_state):
    from kanpilot import cmd_remote, remote
    case.update(pr_state="OPEN")
    failing = pr(case, statusCheckRollup=[check])
    source = {"task": {"id": "task1234", "projectId": case.task()["project_id"],
                       "status": "in-review"},
              "requirements_sha256": case.task()["requirements_sha256"]}
    with patch.object(remote, "get_client", return_value=None), \
         patch.object(remote, "get_task", return_value=source), \
         patch.object(gh_client, "pr_view", return_value=failing):
        result = cmd_remote.cmd_recheck(case.ctx, {"task_id": "task1234"})
    names = [item["name"] for item in result["checks"]]
    assert "pr_merge_gate" in names
    gate = next(item for item in result["checks"] if item["name"] == "pr_merge_gate")
    assert result["ok"] is True and gate["ok"] is True
    assert gate["gate_state"] == gate_state
    detail = next(item["detail"] for item in result["checks"]
                  if item["name"] == "pr_merge_gate")
    assert "analyze-test" in detail
    assert "analyze-test" in core.delivery_of(case.task())["auto_merge"]["reason"]
