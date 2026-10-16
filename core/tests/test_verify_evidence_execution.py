"""Execution proof is scoped and invalidated by later possible source writes."""
import pytest

from kanpilot import verify_evidence


def tool(seq, name="Bash", command="pytest", kind="command_output", **fields):
    return {"id": str(seq), "seq": seq, "type": kind, "turn_id": "execution",
            "tool": {"name": name, "input": {"command": command}, "exit_code": 0}, **fields}


def match(messages, commands=None):
    return verify_evidence.match_execution_verification(commands or ["pytest"], messages, "execution")


def test_only_current_execution_turn_can_prove_commands_without_goal_marker():
    assert match([tool(1)])["ok"]
    for messages in ([tool(1, turn_id="planning")], [tool(1, turn_id=None)],
                     [{"type": "assistant", "turn_id": "execution", "content": "pytest passed"}]):
        result = match(messages)
        assert not result["ok"] and result["missing"] == ["pytest"]


def test_trusted_goal_boundary_accepts_unlabelled_results_only_after_approval():
    marker = {"seq": 2, "type": "system", "content": "goal_approved: p",
              "execution_turn_id": "execution", "execution_started_at": "2026-10-16T01:00:00Z"}
    assert match([marker, tool(3, turn_id=None, source_at="2026-10-16T01:00:01Z")])["ok"]
    for fields in ({"turn_id": None, "source_at": "2026-10-16T00:59:59Z"},
                   {"source_at": "2026-10-16T00:59:59Z"},
                   {"turn_id": "planning", "source_at": "2026-10-16T01:00:01Z"},
                   {"turn_id": None, "source_timestamp_missing": True}):
        assert not match([marker, tool(3, **fields)])["ok"]
    forged = dict(marker, id="desktop:forged:system")
    assert not match([forged, tool(3, turn_id=None, source_at="2026-10-16T01:00:01Z")])["ok"]


@pytest.mark.parametrize("name", ["Edit", "Write", "Patch", "Team", "Task", "subagent", "Mystery", "ReadFiles"])
@pytest.mark.parametrize("kind", ["tool_call", "tool_result"])
def test_possible_write_after_check_requires_plus_reverification(name, kind):
    result = match([tool(1), tool(2, name=name, kind=kind)])
    assert not result["ok"] and "重新验证" in result["detail"]
    assert result["failed"] == [{"command": "pytest", "exit_code": None}]


@pytest.mark.parametrize("name", ["Read", "read", "Grep", "Glob"])
def test_explicit_readonly_tool_after_check_preserves_proof(name):
    assert match([tool(1), tool(2, name=name, kind="tool_result")])["ok"]


def test_subagent_system_events_and_delegated_tool_results_invalidate_checks():
    for message in ({"type": "system", "event_type": "subagent_end", "turn_id": "execution", "seq": 2},
                    tool(2, name="Read", parent_tool_call_id="team-parent", agent_name="worker")):
        assert not match([tool(1), message])["ok"]


def test_nonverification_shell_including_readonly_shell_invalidates_checks():
    assert not match([tool(1), tool(2, command="git status")])["ok"]
    assert not match([tool(1, command="pytest && touch source.py")])["ok"]


def test_verification_calls_pair_with_results_without_invalidating_other_checks():
    messages = [tool(1), tool(2, command="make test", kind="tool_call", tool_call_id="check"),
                tool(3, command="make test", tool_call_id="check")]
    assert match(messages, ["pytest", "make test"])["ok"]
    assert match([tool(1, command='export PATH="$PATH:/bin"\ncd repo && pytest && make test')],
                 ["pytest", "make test"])["ok"]


def test_incomplete_or_mismatched_call_result_cannot_preserve_prior_success():
    for result in (None, tool(3, command="pytest", tool_call_id="other"),
                   tool(3, command="pytest", tool_call_id="check")):
        messages = [tool(1), tool(2, command="make test", kind="tool_call", tool_call_id="check")]
        if result is not None:
            messages.append(result)
        assert not match(messages)["ok"]


def test_pairing_rejects_cross_turn_parent_calls_and_conflicting_commands():
    for call in (tool(1, kind="tool_call", turn_id="planning", tool_call_id="check"),
                 tool(1, kind="tool_call", command="touch source.py", tool_call_id="check")):
        assert not match([call, tool(2, tool_call_id="check")])["ok"]


def test_every_check_must_run_after_last_possible_write():
    messages = [tool(1), tool(2, name="Edit", kind="tool_result"), tool(3, command="make test")]
    result = match(messages, ["pytest", "make test"])
    assert not result["ok"] and result["matched"] == ["make test"]
    assert match(messages + [tool(4)], ["pytest", "make test"])["ok"]


def test_latest_failure_is_not_replaced_by_earlier_success():
    failure = tool(2)
    failure["tool"]["exit_code"] = 4
    assert match([tool(1), failure])["failed"] == [{"command": "pytest", "exit_code": 4}]


def test_mutation_during_running_verification_needs_a_new_check():
    messages = [tool(1, kind="tool_call", tool_call_id="check"),
                tool(2, name="Edit", kind="tool_result"), tool(3, tool_call_id="check")]
    assert not match(messages)["ok"]
    assert match(messages + [tool(4)])["ok"]


def test_pending_delegated_write_cannot_be_assumed_finished_before_later_check():
    assert not match([tool(1, name="Write", kind="tool_call", tool_call_id="write",
                           parent_tool_call_id="team", agent_name="worker"), tool(2)])["ok"]


def test_malformed_result_arguments_cannot_borrow_valid_call_arguments():
    result = tool(2, tool_call_id="check")
    result["tool"]["input"] = "{malformed"
    assert not match([tool(1, kind="tool_call", tool_call_id="check"), result])["ok"]


def test_subagent_start_must_be_settled_before_verification():
    start = {"type": "system", "event_type": "subagent_start", "turn_id": "execution", "seq": 1}
    end = dict(start, event_type="subagent_end", seq=3)
    assert not match([start, tool(2)])["ok"]
    assert not match([start, tool(2), end])["ok"]
    assert match([start, end, tool(4)])["ok"]
