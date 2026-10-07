"""Replay sanitized real event shapes; no model or production access."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kanpilot import auto_monitor, dsh_events, headless_adapter, pi_desktop_worker, verify_evidence


class Controller:
    def __init__(self, count=0):
        self.count = count
        self.calls = []
        self.reports = []

    def call(self, name, payload):
        self.calls.append((name, payload))
        assert name == "execution-activity"
        return {"effective_change_count": self.count}

    def report(self, outcome, **fields):
        self.reports.append(dict(outcome=outcome, **fields))


def task(mode="pr"):
    return {"task_id": "task-fixture", "run_id": "execution-fixture", "plan_sha256": "hash",
            "extra": {"automation": {"fix_round": 0}, "delivery": {"mode": mode},
                      "plan_verify": {"plan_sha256": "hash", "commands": ["fixture-baseline"]}}}


def replay(causal_boundary=False):
    # Names, event ordering, timestamps, exit codes and reply lengths derive from
    # the 2026-10-07 incident. Prose/inputs/paths/identities are synthetic or absent.
    events = json.loads((Path(__file__).parent / "fixtures" / "execution_noop_events.json").read_text())
    if causal_boundary:
        marker = next(event for event in events if event.get("content", "").startswith("goal_approved:"))
        marker["approval_requested_at"] = "2026-10-07T03:27:24.400Z"
    return [headless_adapter.normalize_message(raw, run_id="execution-fixture")
            for raw in dsh_events.to_messages(events)]


def test_real_replay_excludes_baseline_and_delayed_goal_result():
    messages = replay(causal_boundary=True)
    selected, metadata = verify_evidence.execution_window(messages)
    assert metadata == {"evidence_window": "after_goal_approved", "window_basis": "approval_request"}
    assert [message["tool"]["name"] for message in selected if message.get("tool")] == [
        "UpdateGoalProgress", "Glob", *["Read"] * 6]
    assert verify_evidence.extract_command_results(selected) == []
    assert not auto_monitor._verification(task(), messages)["ok"]
    assert auto_monitor._verification(task(), messages)["evidence_window"] == "after_goal_approved"


@pytest.mark.parametrize("mode,signals", [
    ("pr", ["no_effective_changes", "short_reply_read_only"]),
    ("manual", ["short_reply_read_only"]),
])
def test_real_noop_is_reported_before_verification(mode, signals):
    controller = Controller()
    with patch.object(auto_monitor, "_verification", side_effect=AssertionError("must not verify")):
        auto_monitor._complete(controller, task(mode), {"backend": "pi_desktop"}, "in-progress", replay(causal_boundary=True))
    assert len(controller.reports) == 1
    report = controller.reports[0]
    assert report["failure_code"] == "execution_noop"
    assert report["outcome"] == "needs_user"
    assert report["evidence"]["signals"] == signals
    assert report["evidence"]["reply_length"] == 1
    assert report["evidence"]["tool_event_count"] == 8
    assert set(report["evidence"]) <= {"signals", "evidence_window", "window_basis", "reply_length",
                                       "tool_event_count", "action_tool_event_count", "effective_change_count"}
    assert controller.calls == ([("execution-activity", {"task_id": "task-fixture", "run_id": "execution-fixture"})]
                                if mode == "pr" else [])


@pytest.mark.parametrize("name", ["Write", "Edit", "Bash", "UnknownTool", None])
def test_short_reply_with_any_action_or_unknown_tool_is_not_noop(name):
    messages = [{"type": "tool_call", "tool": {"name": name}}, {"type": "assistant", "content": "x"}]
    assert auto_monitor._noop_evidence(Controller(1), task("manual"), {"backend": "pi_desktop"},
                                       messages, {"evidence_window": "whole_round"}) is None


def test_no_effective_changes_is_noop_even_with_write_tool():
    messages = [{"type": "tool_result", "tool": {"name": "Write"}},
                {"type": "assistant", "content": "A sufficiently long final response"}]
    evidence = auto_monitor._noop_evidence(Controller(), task(), {"backend": "pi_desktop"}, messages, {})
    assert evidence["signals"] == ["no_effective_changes"]


@pytest.mark.parametrize("content,expected", [(" \n x \t", True), ("界" * 15, True), ("界" * 16, False)])
def test_reply_threshold_counts_trimmed_unicode_codepoints(content, expected):
    evidence = auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"},
                                          [{"type": "assistant", "content": content}], {})
    assert bool(evidence) is expected


@pytest.mark.parametrize("kind", ["manual_takeover", "unmanaged", "other_backend"])
def test_noop_is_only_for_managed_plus_execution(kind):
    value = task()
    detail = {"backend": "pi_desktop"}
    if kind == "manual_takeover": value["extra"]["automation"]["manual_takeover"] = True
    if kind == "unmanaged": value["extra"].pop("automation")
    if kind == "other_backend": detail["backend"] = "pi"
    assert auto_monitor._noop_evidence(Controller(), value, detail, [], {}) is None


def command(seq, ts=None, turn_id=None):
    return {"type": "command_output", "seq": seq, "source_at": ts, "turn_id": turn_id or "execution-turn",
            "tool": {"name": "Bash", "input": {"command": "fixture-baseline"}, "exit_code": 0}}


def test_approval_window_uses_turn_identity_before_timestamp():
    marker = {"seq": 2, "type": "system", "content": "goal_approved: fixture", "source_at": "2026-10-07T03:27:24Z"}
    messages = [command(1, turn_id="planning-turn"), marker,
                command(3, "2026-10-07T03:27:25Z", "planning-turn"),
                command(4, "2026-10-07T03:27:23Z", "execution-turn")]
    selected, metadata = verify_evidence.execution_window(messages, "execution-turn")
    assert selected == [messages[3]]
    assert metadata["window_basis"] == "execution_turn"
    assert verify_evidence.extract_command_results(messages, window_start=marker,
                                                   execution_turn_id="execution-turn")[0]["index"] == 3


def test_approved_commands_pass_and_missing_marker_fallback_is_visible():
    marker = {"seq": 2, "type": "system", "content": "goal_approved: fixture", "execution_turn_id": "execution-turn"}
    assert auto_monitor._verification(task(), [command(1, turn_id="planning-turn"), marker, {"type": "assistant", "turn_id": "execution-turn", "content": "x"}])["missing"] == ["fixture-baseline"]
    result = auto_monitor._verification(task(), [command(1, turn_id="planning-turn"), marker, command(3)])
    assert result["ok"] and result["evidence_window"] == "after_goal_approved"
    fallback = auto_monitor._verification(task(), [command(1)])
    assert fallback["ok"] and fallback["evidence_window"] == "whole_round"


def test_registered_commit_before_advance_does_not_report_noop():
    value = task()
    value["extra"]["delivery"]["commits"] = [{"execution_run_id": value["run_id"], "sha": "committed"}]
    controller = Controller()
    with patch.object(auto_monitor, "_verification", return_value={"ok": True}), \
         patch.object(auto_monitor, "_commit", return_value={"outcome": "already_committed"}) as commit, \
         patch.object(auto_monitor, "_advance") as advance, patch.object(auto_monitor, "_record_cost"):
        auto_monitor._complete(controller, value, {"backend": "pi_desktop"}, "in-progress", replay())
    commit.assert_called_once()
    advance.assert_called_once()
    assert controller.calls == []
    assert controller.reports[0]["outcome"] == "succeeded"


def test_old_goal_without_trusted_boundary_has_visible_fallback_and_no_signal_b():
    messages, metadata = verify_evidence.execution_window(replay())
    assert metadata == {"evidence_window": "whole_round", "window_basis": "unscoped_goal_window"}
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"},
                                       [{"type": "assistant", "content": "x"}], metadata) is None
    evidence = auto_monitor._noop_evidence(Controller(), task(), {"backend": "pi_desktop"}, messages, metadata)
    assert evidence["signals"] == ["no_effective_changes"]


def test_delivery_recovery_skips_short_reply_signal_b():
    controller = Controller(1)
    controller.call = lambda name, payload: {"effective_change_count": 1, "delivery_recovered": True}
    assert auto_monitor._noop_evidence(controller, task(), {"backend": "pi_desktop"},
                                       [{"type": "assistant", "content": "x"}], {}) is None


def test_actual_collector_chain_uses_approval_request_before_local_marker(tmp_path):
    # Current Plus UiMessage DTO has createdAt but no per-message turnId. These
    # fields pass through the actual collector and both production normalizers.
    class Client:
        messages = [{"id": "baseline", "role": "toolResult", "toolName": "Bash",
                     "toolArgs": {"command": "fixture-baseline"}, "exitCode": 0,
                     "createdAt": "2026-10-07T03:27:24.000Z"}]
        def session_get(self, session_id, **kwargs):
            return {"session": {"id": session_id, "messages": self.messages,
                                "messageStart": 0, "messageEnd": len(self.messages),
                                "hasMoreBefore": False, "hasMoreAfter": False}}
    client = Client()
    log = pi_desktop_worker._EventLog(tmp_path / "events.jsonl")
    collector = pi_desktop_worker._Collector(client, "session-fixture", log, "")
    collector.gate_open = True
    collector.poll()
    # Emission happens after execution starts. Only the success-causal request
    # boundary, not this later timestamp, is used to select unscoped messages.
    log.emit("system", "2026-10-07T03:27:24.500Z", content="goal_approved: fixture",
             execution_turn_id="execution-turn", approval_requested_at="2026-10-07T03:27:24.100Z")
    client.messages += [
        {"id": "late-goal", "role": "toolResult", "toolName": "SubmitGoal", "toolResult": "",
         "createdAt": "2026-10-07T03:27:24.050Z"},
        {"id": "write", "role": "toolResult", "toolName": "Write", "toolResult": "",
         "createdAt": "2026-10-07T03:27:24.200Z"},
        {"id": "verify", "role": "toolResult", "toolName": "Bash",
         "toolArgs": {"command": "fixture-baseline"}, "exitCode": 0,
         "createdAt": "2026-10-07T03:27:24.300Z"},
        {"id": "answer", "role": "assistant", "content": "x",
         "createdAt": "2026-10-07T03:27:24.400Z"},
    ]
    collector.poll()
    events, error = dsh_events.iter_events(log.path)
    assert error is None
    messages = [headless_adapter.normalize_message(raw) for raw in dsh_events.to_messages(events)]
    selected, metadata = verify_evidence.execution_window(messages)
    assert metadata["window_basis"] == "approval_request"
    assert [m["tool"]["name"] for m in selected if m.get("tool")] == ["Write", "Bash"]
    assert len(verify_evidence.extract_command_results(selected)) == 1
    assert auto_monitor._verification(task(), messages)["ok"]
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"}, selected, metadata) is None


def test_collector_missing_source_timestamp_never_manufactures_verification(tmp_path):
    log = pi_desktop_worker._EventLog(tmp_path / "events.jsonl")
    log.emit("system", "2000-01-01T00:00:00Z", content="goal_approved: fixture",
             approval_requested_at="2000-01-01T00:00:00Z")
    collector = pi_desktop_worker._Collector(None, "session", log, "")
    collector.gate_open = True
    collector._emit({"id": "no-source-clock", "role": "toolResult", "toolName": "Bash",
                     "toolArgs": {"command": "fixture-baseline"}, "exitCode": 0})
    events, _ = dsh_events.iter_events(log.path)
    messages = [headless_adapter.normalize_message(raw) for raw in dsh_events.to_messages(events)]
    selected, metadata = verify_evidence.execution_window(messages)
    assert not verify_evidence.extract_command_results(selected)
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"}, selected, metadata) is None


def test_collector_unknown_assistant_ownership_suppresses_short_reply_signal(tmp_path):
    log = pi_desktop_worker._EventLog(tmp_path / "events.jsonl")
    log.emit("system", "2026-10-07T03:27:24.500Z", content="goal_approved: fixture",
             approval_requested_at="2026-10-07T03:27:24.100Z")
    collector = pi_desktop_worker._Collector(None, "session", log, "")
    collector.gate_open = True
    collector._emit({"id": "read", "role": "toolResult", "toolName": "Read", "toolResult": "",
                     "createdAt": "2026-10-07T03:27:24.200Z"})
    collector._emit({"id": "answer", "role": "assistant", "content": "A legitimate sufficiently long final reply"})
    events, _ = dsh_events.iter_events(log.path)
    messages = [headless_adapter.normalize_message(raw) for raw in dsh_events.to_messages(events)]
    selected, metadata = verify_evidence.execution_window(messages)
    assert metadata["reply_unconfirmed"] is True
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"}, selected, metadata) is None


def test_only_planning_turn_ids_do_not_claim_execution_window_is_scoped():
    messages = [command(1, turn_id="planning-turn"),
                {"seq": 2, "type": "system", "content": "goal_approved: fixture", "execution_turn_id": "execution-turn"},
                {"seq": 3, "type": "tool_result", "tool": {"name": "Read"}},
                {"seq": 4, "type": "assistant", "content": "x"}]
    selected, metadata = verify_evidence.execution_window(messages)
    assert metadata["window_basis"] == "unscoped_goal_window"
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"}, selected, metadata) is None


def test_scoped_execution_with_unknown_tools_cannot_prove_no_actions():
    messages = [{"seq": 1, "type": "tool_result", "tool": {"name": "Write"}},
                {"seq": 2, "type": "system", "content": "goal_approved: fixture", "execution_turn_id": "execution-turn"},
                {"seq": 3, "type": "assistant", "turn_id": "execution-turn", "content": "x"}]
    selected, metadata = verify_evidence.execution_window(messages)
    assert metadata["window_basis"] == "execution_turn"
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"}, selected, metadata) is None


def test_recovered_goal_without_window_metadata_is_unscoped():
    selected, metadata = verify_evidence.execution_window([
        {"type": "system", "content": "goal_execution_recovered: fixture"},
        {"type": "assistant", "content": "x"}])
    assert metadata["window_basis"] == "unscoped_goal_window"
    assert auto_monitor._noop_evidence(Controller(), task("manual"), {"backend": "pi_desktop"}, selected, metadata) is None
