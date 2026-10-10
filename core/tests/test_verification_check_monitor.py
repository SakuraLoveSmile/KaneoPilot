"""Monitor consumes validated core receipts, including current failure/drift."""
import pytest
from test_auto_monitor import invoke, task, report, message


@pytest.mark.parametrize("state,verified,expected", [
    ("completed", True, "succeeded"),
    ("completed", False, "needs_user"),
    ("running", None, "needs_user"),
    ("interrupted", None, "needs_user"),
])
def test_core_receipt_gates_monitor_over_ambiguous_or_old_passing_results(monkeypatch, tmp_path, state, verified, expected):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "core-check",
        "binding": {"execution_run_id": "execution", "plan_sha256": "plan"}}
    value["extra"]["verification_override"] = {"execution_run_id": "execution"}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
        messages=[message("pytest; echo $?", 0)],
        responses={"get-verification-check-status": {"ok": True, "state": state,
            "verification_ok": verified, "detail": "core receipt result"}})
    assert report(calls)["outcome"] == expected
    assert any(command == "advance-task" for command, _ in calls) == (expected == "succeeded")
    if expected == "needs_user":
        assert report(calls)["failure_code"] == "verification_unconfirmed"


def test_old_execution_receipt_cannot_override_current_unknown(monkeypatch, tmp_path):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "old-check",
        "binding": {"execution_run_id": "old-execution", "plan_sha256": "plan"}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[message("pytest; echo $?", 0)])
    assert report(calls)["outcome"] == "needs_user"
    assert not any(command == "get-verification-check-status" for command, _ in calls)
