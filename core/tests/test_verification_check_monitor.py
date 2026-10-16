"""Historical core receipts never replace current Plus tool verification."""
import pytest
from test_auto_monitor import invoke, task, report, message


@pytest.mark.parametrize("state,verified,expected", [
    ("completed", True, "needs_user"),
    ("completed", False, "needs_user"),
    ("running", None, "needs_user"),
    ("interrupted", None, "needs_user"),
])
def test_core_receipt_cannot_override_ambiguous_plus_results(monkeypatch, tmp_path, state, verified, expected):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "core-check",
        "binding": {"execution_run_id": "execution", "plan_sha256": "plan"}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
        messages=[message("pytest; echo $?", 0)],
        responses={"get-verification-check-status": {"ok": True, "state": state,
            "verification_ok": verified, "detail": "core receipt result"}})
    assert report(calls)["outcome"] == expected
    assert any(command == "advance-task" for command, _ in calls) == (expected == "succeeded")
    assert report(calls)["failure_code"] == "verification_unconfirmed"
    assert not any(command == "get-verification-check-status" for command, _ in calls)


def test_old_execution_receipt_cannot_override_current_unknown(monkeypatch, tmp_path):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "old-check",
        "binding": {"execution_run_id": "old-execution", "plan_sha256": "plan"}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[message("pytest; echo $?", 0)])
    assert report(calls)["outcome"] == "needs_user"
    assert not any(command == "get-verification-check-status" for command, _ in calls)


@pytest.mark.parametrize("state,verified", [("completed", False), ("running", None), ("interrupted", None)])
def test_current_plus_pass_is_not_replaced_by_historical_core_failure(monkeypatch, tmp_path, state, verified):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "old-core-check",
        "binding": {"execution_run_id": "execution", "plan_sha256": "plan"}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
        messages=[message("pytest", 0)],
        responses={"get-verification-check-status": {"ok": True, "state": state, "verification_ok": verified}})
    assert report(calls)["outcome"] == "succeeded"
    assert not any(command == "get-verification-check-status" for command, _ in calls)
