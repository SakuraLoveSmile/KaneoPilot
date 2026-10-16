from kanpilot import dsh_events, headless_adapter


def test_projection_keeps_tool_pairing_and_delegation_provenance():
    event = {"seq": 1, "type": "tool_result", "name": "Read", "input": "{}", "output": "ok",
             "tool_call_id": "call", "parent_tool_call_id": "parent", "agent_name": "worker"}
    message = dsh_events.to_messages([event])[0]
    for key in ("tool_call_id", "parent_tool_call_id", "agent_name"):
        assert message[key] == event[key]
        assert headless_adapter.normalize_message(message)[key] == event[key]
    delegated = dsh_events.to_messages([{"seq": 2, "type": "subagent_end", "name": "worker"}])[0]
    assert delegated["event_type"] == "subagent_end"
    assert headless_adapter.normalize_message(delegated)["event_type"] == "subagent_end"


def test_round_detail_projects_job_verification_contract(monkeypatch):
    from kanpilot import round_store
    monkeypatch.setattr(round_store, "round_view", lambda row: {})
    detail = dsh_events.round_detail({}, {"verification_evidence_version": 1,
                                         "verification_source_sha256": "a" * 64}, [])
    assert detail["verification_evidence_version"] == 1
    assert detail["verification_source_sha256"] == "a" * 64


def test_error_flag_is_a_bool_and_part_of_native_event_revision():
    event = {"seq": 1, "type": "command_output", "name": "Bash", "input": '{"command":"pytest"}',
             "output": "output", "exit_code": 0, "is_error": False}
    success = dsh_events.to_messages([event])[0]
    error = dsh_events.to_messages([dict(event, is_error=True)])[0]
    assert success["is_error"] is False and error["is_error"] is True
    assert success["revision"] != error["revision"]
    assert headless_adapter.normalize_message(error)["is_error"] is True
    assert "is_error" not in dsh_events.to_messages([dict(event, is_error="true")])[0]
