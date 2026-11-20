"""Reruns only the failed jobs of the exact run behind a failed check."""
import sys

import pytest

from kanpilot import (cmd_rerun_checks as rerun, core, db, derive, gh_client,
                      release_github, runtime_operations, util)
from test_delivery_pr import DeliveryPR


@pytest.fixture
def case():
    value = DeliveryPR()
    value.setUp()
    try:
        value.update(pr_number=7, pr_state="OPEN", pushed_sha=value.delivery["head_sha"])
        yield value
    finally:
        value.doCleanups()
        value.tearDown()


def blocked(case):
    case.update(auto_merge={"state": "blocked",
                            "reason": "检查未通过：app-ci/analyze-test，修复或重跑后会自动合并",
                            "head_sha": case.delivery["head_sha"]})


def failing_pr(case):
    value = dict(case.pr(), mergeStateStatus="UNSTABLE", statusCheckRollup=[
        {"__typename": "CheckRun", "name": "analyze-test", "workflowName": "app-ci",
         "status": "COMPLETED", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/owner/repo/actions/runs/44211608370/job/134491096466"}])
    return value


def run(case):
    return rerun.cmd_rerun_checks(case.ctx, {"task_id": case.task()["task_id"]})


@pytest.fixture
def gh_cli(tmp_path, monkeypatch):
    """A `gh` stand-in that answers `run rerun` like the real CLI: exit 0 and no JSON."""
    log = tmp_path / "gh-calls.txt"
    script = tmp_path / "gh-stub"
    script.write_text("#!" + sys.executable + "\n" + '''import os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ.get("FAKE_GH_STDOUT", ""))
''')
    script.chmod(0o755)
    monkeypatch.setenv("KANPILOT_GH_BIN", str(script))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    return log


def test_rerun_needs_a_named_failing_check(case):
    with pytest.raises(util.KanpilotError):
        run(case)
    blocked(case)
    assert "rerun_checks" in derive.allowed_actions(case.ctx.conn, case.task(), case.ctx.now)


def test_rerun_hidden_when_the_notice_is_from_an_old_head(case):
    case.update(auto_merge={"state": "blocked",
                            "reason": "检查未通过：app-ci/analyze-test，修复或重跑后会自动合并",
                            "head_sha": "e" * 40})
    assert "rerun_checks" not in derive.allowed_actions(case.ctx.conn, case.task(), case.ctx.now)
    with pytest.raises(util.KanpilotError, match="旧提交"):
        run(case)


def test_rerun_hidden_when_the_notice_has_no_head(case):
    case.update(auto_merge={"state": "blocked",
                            "reason": "检查未通过：app-ci/analyze-test，修复或重跑后会自动合并"})
    assert "rerun_checks" not in derive.allowed_actions(case.ctx.conn, case.task(), case.ctx.now)
    with pytest.raises(util.KanpilotError, match="旧提交"):
        run(case)


def test_rerun_checks_the_notice_before_reading_github(case, monkeypatch):
    case.update(auto_merge={"state": "blocked",
                            "reason": "检查未通过：app-ci/analyze-test，修复或重跑后会自动合并",
                            "head_sha": "e" * 40})
    calls = []
    monkeypatch.setattr(gh_client, "pr_view", lambda *a, **k: calls.append(a))
    with pytest.raises(util.KanpilotError):
        run(case)
    assert calls == []


def test_rerun_requires_the_pr_to_match(case, monkeypatch):
    blocked(case)
    monkeypatch.setattr(gh_client, "pr_view", lambda *a, **k: dict(failing_pr(case), headRefOid="f" * 40))
    with pytest.raises(util.KanpilotError):
        run(case)


def test_rerun_sends_once_per_run_attempt(case, monkeypatch):
    blocked(case)
    calls = []
    monkeypatch.setattr(gh_client, "pr_view", lambda *a, **k: failing_pr(case))
    monkeypatch.setattr(release_github, "run", lambda repo, run_id: {
        "head_sha": case.delivery["head_sha"], "status": "completed",
        "run_attempt": 1, "conclusion": "failure"})
    monkeypatch.setattr(gh_client, "run_rerun_failed", lambda repo, run_id: calls.append(run_id))
    first = run(case)
    assert first["runs"] == [{"run_id": 44211608370, "status": "accepted"}]
    second = run(case)
    assert second["runs"][0]["reused"] is True
    assert calls == [44211608370]
    assert case.ctx.conn.execute("SELECT count(*) FROM events WHERE kind='checks_rerun'").fetchone()[0] == 1


@pytest.mark.parametrize("stdout", ["", "✓ Requested rerun (failed jobs) of run 44211608370\n"],
                         ids=["no-output", "human-line"])
def test_rerun_is_accepted_when_gh_succeeds_without_json(case, gh_cli, monkeypatch, stdout):
    # Unlike the tests above, nothing replaces gh_client.run_rerun_failed: the real
    # command runs against a CLI that exits 0 with no structured output.
    blocked(case)
    monkeypatch.setenv("FAKE_GH_STDOUT", stdout)
    monkeypatch.setattr(gh_client, "pr_view", lambda *a, **k: failing_pr(case))
    monkeypatch.setattr(release_github, "run", lambda repo, run_id: {
        "head_sha": case.delivery["head_sha"], "status": "completed",
        "run_attempt": 1, "conclusion": "failure"})
    assert run(case)["runs"] == [{"run_id": 44211608370, "status": "accepted"}]
    sent = gh_cli.read_text().splitlines()
    assert len(sent) == 1 and sent[0].split()[:3] == ["run", "rerun", "44211608370"]
    row = case.ctx.conn.execute("SELECT status FROM runtime_operations WHERE tool=?",
                                (rerun.TOOL,)).fetchone()
    assert row["status"] == "accepted"
    assert case.ctx.conn.execute("SELECT count(*) FROM events WHERE kind='checks_rerun'").fetchone()[0] == 1
    assert run(case)["runs"][0]["reused"] is True
    assert len(gh_cli.read_text().splitlines()) == 1


def test_rerun_keeps_unknown_when_the_connection_drops(case, monkeypatch):
    blocked(case)
    monkeypatch.setattr(gh_client, "pr_view", lambda *a, **k: failing_pr(case))
    monkeypatch.setattr(release_github, "run", lambda repo, run_id: {
        "head_sha": case.delivery["head_sha"], "status": "completed",
        "run_attempt": 1, "conclusion": "failure"})
    def drop(repo, run_id):
        raise gh_client.GhError("transient", "connection reset")
    monkeypatch.setattr(gh_client, "run_rerun_failed", drop)
    with pytest.raises(util.KanpilotError):
        run(case)
    row = case.ctx.conn.execute("SELECT status FROM runtime_operations WHERE tool=?",
                                (rerun.TOOL,)).fetchone()
    assert row["status"] == "unknown"