"""Explicit evidence-only rounds cannot borrow old source as fresh evidence.

Use disposable Git/SQLite and simulated Plus reports from the continuation
fixtures; no product database, real task, model or device is contacted.
"""
import pytest

from kanpilot import core, db
from test_committed_result_continuation import case, continue_run, initial_commit, reverify


@pytest.mark.parametrize("evidence,exit_code,passed", [
    (False, 0, False),
    (True, 0, True),
    (True, 1, False),
])
def test_evidence_only_continuation_requires_new_evidence_and_current_green_checks(
        case, evidence, exit_code, passed):
    prior_sha = initial_commit(case)
    continue_run(case, exit_code=exit_code)
    task = db.require_task(case.ctx.conn, "task1234")
    db.set_extra(case.ctx.conn, task, case.ctx.now_iso, plan_delivery_kind="evidence_only")
    if evidence:
        report = case.wt / ".pi" / "evidence" / "acceptance.json"
        report.parent.mkdir(parents=True)
        report.write_text('{"result":"observed in this round"}\n')

    receipt = reverify(case)
    assert receipt["verification_ok"] is passed
    assert receipt["empty_diff"] is True
    assert receipt["effective_changes"] == []
    if passed:
        assert receipt["evidence_only"] is True
        result = case.call_commit()
        assert result == {"outcome": "unchanged", "sha": prior_sha, "files": 0}
        task = db.require_task(case.ctx.conn, "task1234")
        entry = core.delivery_of(task)["commits"][-1]
        assert entry["evidence_only"] is True
        assert entry["execution_run_id"] == "exec2"
        assert entry["sha"] == prior_sha
