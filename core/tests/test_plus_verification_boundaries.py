"""The advance/review boundaries must use Plus proof, never retired local checks."""

import json
from types import SimpleNamespace

import pytest

from kanpilot import cmd_remote, constants, db, pi_desktop_review, util


def _gate(monkeypatch, tmp_path, *, turn="execution", code=0, history=False):
    extra = {"plan_verify": {"plan_sha256": "plan", "commands": ["pytest"]}}
    if history:
        extra["verification_checks"] = {"binding": {"execution_run_id": "run"},
                                         "receipt": {"verification_ok": False}}
    task = {"task_id": "task", "run_id": "run", "plan_sha256": "plan",
            "extra_json": json.dumps(extra)}
    job = {"session_id": "session", "turn_id": "execution", "status": "finished",
           "exit_code": 0, "plan_sha256": "plan", "verification_evidence_version": 1}
    events = [
        {"seq": 1, "type": "session", "session_id": "session"},
        {"seq": 2, "type": "command_output", "name": "Bash", "turn_id": turn,
         "input": json.dumps({"command": "pytest"}), "exit_code": code},
        {"seq": 3, "type": "terminal", "outcome": "completed"},
    ]
    monkeypatch.setattr(cmd_remote, "_require_command_monitor_claim", lambda *args: None)
    monkeypatch.setattr(cmd_remote.dsh_events, "iter_events", lambda path: (events, None))
    return cmd_remote._require_command_verification_gate(
        SimpleNamespace(conn=None), {}, task, job, tmp_path)


def test_old_local_failure_cannot_override_current_plus_success(monkeypatch, tmp_path):
    _gate(monkeypatch, tmp_path, history=True)


@pytest.mark.parametrize("turn,code", [("planning", 0), ("previous", 0),
                                      ("execution", None), ("execution", 1)])
def test_direct_advance_rejects_wrong_turn_or_unproven_exit(monkeypatch, tmp_path, turn, code):
    with pytest.raises(util.KanpilotError, match="验证命令缺失或失败"):
        _gate(monkeypatch, tmp_path, turn=turn, code=code)


def test_retired_local_review_output_is_only_history(monkeypatch, tmp_path):
    current = {"extra_json": json.dumps({"verification_checks": {
        "operation_id": "old", "state": "completed",
        "binding": {"execution_run_id": "run"},
        "receipt": {"verification_ok": False,
                    "checks": [{"output_path": "/missing/historical.log"}]},
    }})}
    monkeypatch.setattr(db, "require_task", lambda *args: current)
    files = pi_desktop_review._collect_core_verification(
        object(), {"task_id": "task"}, "run", tmp_path)
    data = json.loads((tmp_path / "historical-core-verification.json").read_text())
    assert data["verification_ok"] is False
    assert "历史" in data["note"]
    assert list(files) == ["historical-core-verification.json"]


def test_all_branch_modes_instruct_plus_to_run_the_checks():
    assert constants.DISPATCH_INSTRUCTION_BRANCH_LIVE == constants.DISPATCH_INSTRUCTION_BRANCH
    assert "调度台不会替你运行检查" in constants.EXECUTION_VERIFICATION_INSTRUCTION


def test_execution_instruction_requires_goal_report_and_drops_transcript_rules():
    text = constants.EXECUTION_VERIFICATION_INSTRUCTION
    assert "SubmitGoalReport" in text and "exitCode" in text and "passed" in text
    assert "使证据失效" not in text and "只读工具" not in text
    assert "PATH" not in constants.DISPATCH_INSTRUCTION_BRANCH
