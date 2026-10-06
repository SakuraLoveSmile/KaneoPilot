"""Plus shell results retain structured verification evidence across tool casing."""

import pytest

from kanpilot import dsh_events, pi_desktop_worker, verify_evidence


@pytest.mark.parametrize("name", ["Bash", "bash", "BASH"])
@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_plus_bash_details_reach_verification(tmp_path, name, exit_code):
    # Shape observed in a real Plus Goal round: role=tool, title-case toolName,
    # and the actual command exit code nested under toolResult.details.
    command = "python3 -c \"from pathlib import Path; assert Path('smoke.txt').read_text()=='ok\\n'; print('verified')\""
    message = {
        "id": "bash-result-1", "role": "tool", "toolName": name,
        "toolStatus": "success", "toolCallId": "bash-result-1",
        "toolArgs": {"command": command},
        "toolResult": {
            "content": [{"type": "text", "text": "verified\n"}],
            "details": {"commandShellId": "bash", "exitCode": exit_code,
                        "stdout": "verified\n", "stderr": "", "truncated": False},
            "isError": False,
        },
        "timestamp": "2026-10-05T06:29:18Z",
    }

    class Client:
        def session_get(self, session_id, **kwargs):
            return {"session": {
                "id": session_id, "messages": [message],
                "messageStart": 0, "messageEnd": 1,
                "hasMoreBefore": False, "hasMoreAfter": False,
            }}

    path = tmp_path / "events.jsonl"
    collector = pi_desktop_worker._Collector(
        Client(), "session-1", pi_desktop_worker._EventLog(path), "",
    )
    collector.drain()
    collector.poll()
    assert collector.read_error is None
    assert not collector.truncated
    events, error = dsh_events.iter_events(path)
    assert error is None
    assert len(events) == 1
    assert events[0]["type"] == "command_output"
    assert events[0]["name"] == name
    assert events[0]["exit_code"] == exit_code

    results = verify_evidence.extract_command_results(dsh_events.to_messages(events))
    assert len(results) == 1
    assert results[0]["command"] == command
    assert results[0]["exit_code"] == exit_code
    assert verify_evidence.match_verification([command], results)["ok"] == (exit_code == 0)
