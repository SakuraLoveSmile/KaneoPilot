"""Goal evidence metadata uses temporary files, fixture DBs and fake Plus only."""
import json

import pytest

from kanpilot import dsh_events, headless_adapter, pi_desktop_worker, util
import test_pi_desktop_backend as backend_fixture
import test_process_owner as owner_fixture
from test_process_owner import plus_scene


def test_collector_keeps_turn_and_marks_unknown_source_time(tmp_path):
    log = pi_desktop_worker._EventLog(tmp_path / "events.jsonl")
    collector = pi_desktop_worker._Collector(None, "session", log, "")
    collector.gate_open = True
    source = {"id": "opaque", "role": "tool_result", "toolName": "bash",
              "toolArgs": {"command": "pytest"}, "toolResult": {"exitCode": 0},
              "turnId": "execution"}
    collector._emit(source)
    # A later source timestamp/turn correction must be a visible revision.
    collector._emit(dict(source, timestamp="2026-10-07T13:00:00Z", turnId="continued"))
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert events[0]["turn_id"] == "execution"
    assert events[0]["source_timestamp_missing"] is True
    assert util.parse_ts(events[0]["ts"]) is not None  # collection time only
    assert events[1]["turn_id"] == "continued"
    assert "source_timestamp_missing" not in events[1]
    assert events[1]["source_revision"] == events[0]["source_revision"] + 1
    messages = dsh_events.to_messages(events)
    assert len(messages) == 1
    normalized = headless_adapter.normalize_message(messages[0])
    assert normalized["turn_id"] == "continued"
    assert normalized["source_at"] == "2026-10-07T13:00:00Z"


@pytest.fixture
def approval_scene(monkeypatch):
    # Report publication is covered by its own suite; this fixture checks the
    # approval boundary without waiting for the historical report grace period.
    monkeypatch.setattr(pi_desktop_worker, "_GOAL_REPORT_GRACE_SECONDS", 0)
    case = backend_fixture.TestRunReal("test_goal_mode_contract_auto_approved_then_execution_observed")
    case.setUp()
    try:
        case.server.turn_statuses = ["running", "completed"]
        case.server.turn_status = "completed"
        case.server.plans = [case._goal_contract()]
        case.server.planning_state = "awaiting_approval"
        case.server.execution_turn = "turn-goal-exec"
        case.server.messages = case.default_messages()
        yield case
    finally:
        case.tearDown()
        case.doCleanups()


@pytest.mark.parametrize("concurrent", [False, True])
def test_approval_request_boundary_only_records_successful_local_resolve(approval_scene, concurrent):
    case = approval_scene
    if concurrent:
        original = case.server.tool_call

        def approved_elsewhere(name, args):
            response = original(name, args)
            if name == "pi_plans_resolve":
                return {"structuredContent": {"error": {"code": "INTERNAL", "message": "concurrent approval"}}}
            return response

        case.server.tool_call = approved_elsewhere
    spec, directory = case._base()
    assert case._run(spec) == 0
    approval = case.read_job(directory)["goal_approval"]
    marker = next(event for event in case.read_events(directory)
                  if str(event.get("content", "")).startswith("goal_approved:"))
    assert marker["execution_turn_id"] == approval["execution_turn_id"] == "turn-goal-exec"
    assert marker["approval_requested_at"] == approval["approval_requested_at"]
    assert approval["execution_started_at"] is None  # fake Plus has no source start
    if concurrent:
        assert approval["approval_requested_at"] is None
    else:
        assert util.parse_ts(approval["approval_requested_at"]) is not None
    assert case.server.prompt_count == 1


@pytest.mark.parametrize("started_at", [None, "2026-10-07T13:00:00Z"])
def test_recovery_keeps_real_start_without_using_recovery_time(plus_scene, monkeypatch, started_at):
    monkeypatch.setattr(pi_desktop_worker, "_GOAL_REPORT_GRACE_SECONDS", 0)
    case, spec, path, _, _, candidate = owner_fixture.prepare_approved_goal_recovery(plus_scene, monkeypatch)
    original = case.server.tool_call

    def with_start(name, args):
        response = original(name, args)
        if name == "pi_turn_get" and args.get("turnId") == candidate:
            response["structuredContent"]["startedAt"] = started_at
        return response

    case.server.tool_call = with_start
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 0
    approval = case.read_job(path.parent)["goal_approval"]
    marker = next(event for event in case.read_events(path.parent)
                  if str(event.get("content", "")).startswith("goal_execution_recovered:"))
    assert approval["execution_started_at"] == marker["execution_started_at"] == started_at
    assert marker["execution_turn_id"] == candidate
    assert "approval_requested_at" not in approval
    assert case.server.prompt_count == 0
    assert case.server.plans_resolve_calls == []
