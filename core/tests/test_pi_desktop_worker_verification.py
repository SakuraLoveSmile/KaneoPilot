"""Only fake local Plus and isolated Git repositories are used here."""
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from kanpilot import dsh_events, git_delivery, headless_adapter, pi_desktop_worker as worker, verify_evidence
import test_pi_desktop_backend as fixture


def git_repo(path):
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "source.py").write_text("initial\n")
    return path


@pytest.fixture
def scene(monkeypatch):
    monkeypatch.setattr(worker, "_GOAL_REPORT_GRACE_SECONDS", 0)
    case = fixture.TestRunReal("test_completed_flow_events_and_order")
    case.setUp()
    try:
        case.server.turn_statuses = ["running", "completed"]
        case.server.messages = case.default_messages()
        yield case
    finally:
        case.tearDown()
        case.doCleanups()


def test_real_completed_turn_binds_source_without_running_verify(scene):
    spec, directory = scene._base(body="```verify\nexit 99\n```\n")
    git_repo(Path(spec["cwd"]))
    expected = git_delivery.source_state_sha256(spec["cwd"])
    assert scene._run(spec) == 0
    job = scene.read_job(directory)
    assert job["verification_evidence_version"] == 1
    assert job["verification_source_sha256"] == expected


def test_snapshot_failure_is_explicit_and_cannot_fabricate_hash(scene):
    spec, directory = scene._base()
    assert scene._run(spec) == 0  # not a Git repository
    job = scene.read_job(directory)
    assert "verification_source_sha256" not in job
    assert job["verification_source_error"]


def test_finished_historical_resume_does_not_backfill_snapshot(scene):
    spec, directory = scene._base()
    assert scene._run(spec) == 0
    path = directory / "job.json"
    job = json.loads(path.read_text())
    for key in list(job):
        if key.startswith("verification_"):
            job.pop(key)
    path.write_text(json.dumps(job))
    with mock.patch.object(git_delivery, "source_state_sha256", side_effect=AssertionError("history")):
        assert worker.run_real(spec, resume=True) == 0
    assert json.loads(path.read_text()) == job


def test_observing_historical_resume_does_not_create_version_or_snapshot(scene):
    spec, directory = scene._base()
    scene.write_lost_observer_scene(spec, directory)
    with mock.patch.object(git_delivery, "source_state_sha256", side_effect=AssertionError("history")):
        with scene.fast_clock():
            assert worker.run_real(spec, resume=True) == 0
    job = scene.read_job(directory)
    assert "verification_evidence_version" not in job
    assert "verification_source_sha256" not in job


def test_collector_provenance_correction_is_a_new_revision(tmp_path):
    log = worker._EventLog(tmp_path / "events.jsonl")
    collector = worker._Collector(None, "session", log, "")
    collector.gate_open = True
    message = {"id": "result", "role": "tool_result", "toolName": "Read", "toolCallId": "call",
               "turnId": "execution", "toolResult": "text"}
    collector._emit(message)
    collector._emit(dict(message, parentToolCallId="team", agentName="worker"))
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert len(events) == 2
    assert events[-1]["parent_tool_call_id"] == "team"
    assert events[-1]["agent_name"] == "worker"


def test_collector_exit_code_correction_revokes_old_success(tmp_path):
    log = worker._EventLog(tmp_path / "events.jsonl")
    collector = worker._Collector(None, "session", log, "")
    collector.gate_open = True
    message = {"id": "result", "role": "tool_result", "toolName": "Bash", "toolCallId": "call",
               "turnId": "execution", "toolArgs": {"command": "pytest"}, "toolResult": "output", "exitCode": 0}
    collector._emit(message)
    collector._emit(dict(message, exitCode=7))
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert len(events) == 2 and events[-1]["exit_code"] == 7


@pytest.mark.parametrize("correction", [None, {"isError": True}, {"toolStatus": "error"}])
def test_plus_error_with_zero_exit_code_survives_full_evidence_pipeline(tmp_path, correction):
    log = worker._EventLog(tmp_path / "events.jsonl")
    collector = worker._Collector(None, "session", log, "")
    collector.gate_open = True
    source = {"id": "result", "role": "tool_result", "toolName": "Bash", "toolCallId": "check",
              "turnId": "execution", "toolArgs": {"command": "pytest"}, "toolResult": "output",
              "toolStatus": "success", "isError": correction is None, "exitCode": 0}
    collector._emit(source)
    if correction is not None:
        initial_events, _ = dsh_events.iter_events(log.path)
        initial_messages = [headless_adapter.normalize_message(message)
                            for message in dsh_events.to_messages(initial_events)]
        assert verify_evidence.match_execution_verification(["pytest"], initial_messages, "execution")["ok"]
        collector._emit(dict(source, **correction))
    events, _ = dsh_events.iter_events(log.path)
    assert len(events) == (1 if correction is None else 2)
    assert events[-1]["exit_code"] == 0 and events[-1]["is_error"] is True
    messages = [headless_adapter.normalize_message(message) for message in dsh_events.to_messages(events)]
    assert len(messages) == 1 and messages[0]["is_error"] is True
    assert messages[0]["revision"] == (1 if correction is None else 2)
    outcome = verify_evidence.match_execution_verification(["pytest"], messages, "execution")
    assert not outcome["ok"] and outcome["failed"] == [{"command": "pytest", "exit_code": None}]


def test_fake_completion_has_real_source_snapshot_and_bound_commands(tmp_path):
    cwd = git_repo(tmp_path / "repo")
    plan = tmp_path / "plan.md"
    plan.write_text("```verify\nnever-execute-this\n```\n")
    spec = {"task_id": "task", "run_id": "run", "runs_dir": str(tmp_path / "run"),
            "cwd": str(cwd), "plan_path": str(plan)}
    assert worker._run_fake(spec, "finished", mock.Mock()) == 0
    job = json.loads((tmp_path / "run" / "job.json").read_text())
    assert job["verification_evidence_version"] == 1
    assert job["verification_source_sha256"] == git_delivery.source_state_sha256(cwd)
    events = [json.loads(line) for line in (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    proof = next(event for event in events if event["type"] == "command_output")
    assert json.loads(proof["input"])["command"] == "never-execute-this"
    assert proof["turn_id"] == job["turn_id"]
