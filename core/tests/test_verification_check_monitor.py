"""Historical local receipts never replace the current structured verdict."""
import pytest
from test_auto_monitor import invoke, task, report, message


def assert_current_structured_verdict_only(calls):
    verdicts = [payload for command, payload in calls if command == "engine-verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["expected_execution_run_id"] == "execution"
    assert verdicts[0]["expected_plan_sha256"] == "plan"
    assert not any(command in ("get-verification-check-status", "run-verification-checks")
                   for command, _ in calls)


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
        responses={"engine-verdict": {"verification_ok": False, "kind": "unconfirmed",
            "evidence_source": "goal_report", "detail": "当前结构化报告缺少可核验的命令结果"},
            "get-verification-check-status": {"ok": True, "state": state,
            "verification_ok": verified, "detail": "core receipt result"}})
    assert report(calls)["outcome"] == expected
    assert any(command == "advance-task" for command, _ in calls) == (expected == "succeeded")
    assert report(calls)["failure_code"] == "verification_unconfirmed"
    assert not any(command == "delivery-commit" for command, _ in calls)
    assert_current_structured_verdict_only(calls)


def test_old_execution_receipt_cannot_override_current_unknown(monkeypatch, tmp_path):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "old-check",
        "binding": {"execution_run_id": "old-execution", "plan_sha256": "plan"}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[message("pytest; echo $?", 0)],
        responses={"engine-verdict": {"verification_ok": False, "kind": "unconfirmed",
            "evidence_source": "goal_report", "detail": "当前执行的结构化报告不可确认"}})
    assert report(calls)["outcome"] == "needs_user"
    assert report(calls)["failure_code"] == "verification_unconfirmed"
    assert not any(command in ("delivery-commit", "advance-task") for command, _ in calls)
    assert_current_structured_verdict_only(calls)


@pytest.mark.parametrize("state,verified", [("completed", False), ("running", None), ("interrupted", None)])
def test_current_plus_pass_is_not_replaced_by_historical_core_failure(monkeypatch, tmp_path, state, verified):
    value = task()
    value["extra"]["verification_checks"] = {"operation_id": "old-core-check",
        "binding": {"execution_run_id": "execution", "plan_sha256": "plan"}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
        messages=[message("pytest", 0)],
        responses={"engine-verdict": {"verification_ok": True, "evidence_source": "goal_report"},
            "get-verification-check-status": {"ok": True, "state": state, "verification_ok": verified}})
    assert report(calls)["outcome"] == "succeeded"
    assert any(command == "advance-task" for command, _ in calls)
    assert_current_structured_verdict_only(calls)
