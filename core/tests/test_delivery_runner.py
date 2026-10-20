import json
import copy

import pytest

from kanpilot import auto_dispatch, delivery_contract, delivery_runner, stage_contract, util
from test_auto_dispatch import FakeClock


def invoke(monkeypatch, tmp_path, *, delivery=None, publish=None, sync=None, advance=None, error_at=None,
           result_mutator=None):
    calls, records, clock = [], [], FakeClock()
    claims = [{"claimed": True, "request_id": "request", "run_id": "delivery-run",
               "context": {"task_id": "task", "lease_seconds": 180}}, {"claimed": False}]
    task = {"task_id": "task", "run_id": "execution-run", "repository": str(tmp_path),
            "plan_sha256": "a" * 64, "requirements_sha256": "b" * 64,
            "review_key": "review", "review_report_path": str(tmp_path / "review.md"),
            "review_report_sha256": "c" * 64,
            "delivery": dict({"worktree_path": str(tmp_path / "worktree"), "branch": "task",
                              "head_sha": "new", "base_branch": "main"}, **(delivery or {}))}
    def envelope(operation, payload, result):
        contract = delivery_contract.freeze(task, payload, task["delivery"], operation)
        result = dict(result)
        if result["outcome"] == "published":
            result.update(pr_number=7, pr_url="https://github.com/owner/repo/pull/7")
        terminal = result["outcome"] in ("published", "merged", "kaneo_done")
        report = stage_contract.make_result(contract,
            result if terminal else {"code": result.get("failure_code") or "waiting", "detail": "waiting"},
            outcome="succeeded" if terminal else "blocked",
            evidence=[{"source": "core_independent", "kind": "delivery_observation", "outcome": result["outcome"]}])
        if operation == "publish" and result["outcome"] == "published":
            task["delivery"].update(pr_number=7, pushed_sha=task["delivery"]["head_sha"])
        response = dict(result, stage_contract=contract, stage_result=report)
        return result_mutator(response) if result_mutator else response
    def run(command, payload, db_path):
        calls.append((command, payload))
        if error_at and error_at[0] == command:
            raise util.KanpilotError(error_at[1], "https://secret@server.invalid")
        if command == "claim":
            return claims.pop(0)
        if command == "get-task":
            return {"task": copy.deepcopy(task)}
        if command == "delivery-publish":
            return envelope("publish", payload, publish or {"outcome": "published"})
        if command == "delivery-sync":
            return envelope("sync", payload, sync or {"outcome": "merged"})
        if command == "advance-task" and advance:
            raise util.KanpilotError(advance, "remote failure")
        return {}
    monkeypatch.setattr(auto_dispatch.cli, "run", run)
    rc = delivery_runner.run_stage("delivery", conn=None, db_path=tmp_path / "unused",
        timeout=600, log_path=tmp_path / "log", record_run=lambda **kw: records.append(kw),
        sleep=clock.sleep, clock=clock)
    return rc, calls, records


def test_publish_sync_advance_order_and_identity(monkeypatch, tmp_path):
    rc, calls, records = invoke(monkeypatch, tmp_path)
    assert rc == 0
    assert [c[0] for c in calls] == ["claim", "get-task", "delivery-publish", "get-task", "delivery-sync", "advance-task", "report", "claim"]
    payload = next(p for c, p in calls if c == "advance-task")
    assert payload == {"task_id": "task", "request_id": "request", "run_id": "delivery-run",
                       "expected_execution_run_id": "execution-run", "target": "done", "expected_current": "in-review"}
    assert records[-1]["exit_code"] == 0


def test_already_pushed_skips_publish_passive_done(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, delivery={"pr_number": 7, "pushed_sha": "sha", "head_sha": "sha"},
                         sync={"outcome": "kaneo_done"})
    assert "delivery-publish" not in [c for c, _ in calls]
    assert next(p for c, p in calls if c == "advance-task")["expected_current"] == "done"


@pytest.mark.parametrize("phase,outcome,expected", [("publish", "retry", "succeeded"),
    ("publish", "blocked", "failed"), ("publish", "waiting", "succeeded"), ("sync", "waiting", "succeeded"),
    ("sync", "retry", "succeeded"), ("sync", "blocked", "failed")])
def test_report_mapping(monkeypatch, tmp_path, phase, outcome, expected):
    _, calls, _ = invoke(monkeypatch, tmp_path, **{phase: {"outcome": outcome, "failure_code": "pr_closed"}})
    assert next(p for c, p in calls if c == "report")["outcome"] == expected
    assert "advance-task" not in [c for c, _ in calls]
    if phase == "publish":
        assert "delivery-sync" not in [c for c, _ in calls]


@pytest.mark.parametrize("code", ["remote_conflict", "remote_unavailable"])
def test_advance_failure_reported(monkeypatch, tmp_path, code):
    _, calls, _ = invoke(monkeypatch, tmp_path, advance=code)
    assert next(p for c, p in calls if c == "report") == {
        "request_id": "request", "run_id": "delivery-run", "outcome": "failed", "note": code}


@pytest.mark.parametrize("command", ["delivery-publish", "delivery-sync", "advance-task"])
def test_stale_stops_without_report_and_logs_no_credentials(monkeypatch, tmp_path, command):
    rc, calls, _ = invoke(monkeypatch, tmp_path, error_at=(command, "stale_run"))
    assert rc == 0 and calls[-1][0] == command
    entries = [json.loads(line) for line in (tmp_path / "log").read_text().splitlines()]
    assert all(set(e) == {"time", "step", "task_id", "request_id", "code"} for e in entries)
    assert "secret" not in (tmp_path / "log").read_text()


@pytest.mark.parametrize("corruption", ["missing", "old_run", "wrong_outcome"])
def test_unverified_result_stops_before_sync_and_advance(monkeypatch, tmp_path, corruption):
    def mutate(result):
        if corruption == "missing":
            result.pop("stage_result")
        elif corruption == "old_run":
            result["stage_result"]["run_id"] = "old-delivery-run"
        else:
            result["outcome"] = "merged"
        return result
    _, calls, _ = invoke(monkeypatch, tmp_path, result_mutator=mutate)
    assert not any(command in ("delivery-sync", "advance-task") for command, _ in calls)
    report = next(payload for command, payload in calls if command == "report")
    assert report["outcome"] == "failed" and report["note"] == "validation_failed"
