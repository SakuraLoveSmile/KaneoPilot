import json

import pytest

from kanpilot import auto_dispatch, delivery_runner, util
from test_auto_dispatch import FakeClock


def invoke(monkeypatch, tmp_path, *, delivery=None, publish=None, sync=None, advance=None, error_at=None):
    calls, records, clock = [], [], FakeClock()
    claims = [{"claimed": True, "request_id": "request", "run_id": "delivery-run",
               "context": {"task_id": "task", "lease_seconds": 180}}, {"claimed": False}]
    def run(command, payload, db_path):
        calls.append((command, payload))
        if error_at and error_at[0] == command:
            raise util.KanpilotError(error_at[1], "https://secret@server.invalid")
        if command == "claim":
            return claims.pop(0)
        if command == "get-task":
            return {"task": {"task_id": "task", "run_id": "execution-run", "delivery": delivery or {"head_sha": "new"}}}
        if command == "delivery-publish":
            return publish or {"outcome": "published"}
        if command == "delivery-sync":
            return sync or {"outcome": "merged"}
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
    assert [c[0] for c in calls] == ["claim", "get-task", "delivery-publish", "delivery-sync", "advance-task", "report", "claim"]
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
    ("publish", "blocked", "failed"), ("sync", "waiting", "succeeded"),
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
