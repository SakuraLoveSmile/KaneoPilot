import json
import subprocess

import pytest

from kanpilot.verify_evidence import extract_command_results, match_verification, parse_verify_block


def test_valid_verify_and_normalization():
    assert parse_verify_block("```verify\n# note\n python  -m pytest \n\nmake test\n```") == {
        "ok": True, "commands": ["python -m pytest", "make test"]}


@pytest.mark.parametrize("text", ["none", "```verify\n```", "```verify\n# note\n```",
    "```verify\nx\n```\n```verify\ny\n```", "```verify\nx\n",
    "```verify\n" + "x\n" * 6 + "```", "```verify\nx \\\n```", "```verify\nx << EOF\n```"] +
    ["```verify\n" + command + "\n```" for command in
     ["git commit -m x", "git push", "git reset --hard", "git checkout x", "git switch x", "gh pr view"]])
def test_invalid_verify(text):
    assert parse_verify_block(text)["ok"] is False


def test_nested_fence_does_not_declare_verify():
    assert not parse_verify_block("````markdown\n```verify\nx\n```\n````")["ok"]


@pytest.mark.parametrize("raw", [True, False])
@pytest.mark.parametrize("code,expected", [(0, 0), (2, 2), (None, None), ("0", None), (False, None)])
def test_extract_normalized_and_raw_tool_events(raw, code, expected):
    tool = {"input": json.dumps({"command": "pytest"}), "exit_code": code}
    message = {"id": "result", "seq": 2, "type": "command_output"}
    message.update({"raw": {"tool": tool}} if raw else {"tool": tool})
    events = [{"id": "prose", "type": "assistant", "content": "pytest passed"},
              {"id": "call", "type": "tool_call", "tool": tool}, message]
    assert extract_command_results(events) == [{"command": "pytest", "exit_code": expected,
                                               "message_id": "result", "index": 2}]


def test_three_adapter_tool_inputs_and_time_order():
    messages = [
        {"type": "command_output", "seq": 3, "raw": {"tool": {"input": '{"command":"c"}', "exit_code": 0}}},
        {"type": "command_output", "seq": 1, "tool": {"input": '{"command":"a"}', "exit_code": 1}},
        {"type": "command_output", "seq": 2, "tool": {"input": {"command": "b"}, "exit_code": None}},
        {"type": "command_output", "seq": 4, "tool": {"input": "malformed", "exit_code": 0}},
    ]
    assert [r["command"] for r in extract_command_results(messages)] == ["a", "b", "c"]


def test_last_match_and_segments():
    results = [{"command": "pytest", "exit_code": 1},
               {"command": "cd repo &&  pytest  && make test", "exit_code": 0}]
    assert match_verification(["pytest", "make  test"], results)["ok"]
    result = match_verification(["pytest", "missing"], results + [{"command": "pytest", "exit_code": None}])
    assert result["missing"] == ["missing"]
    assert result["failed"] == [{"command": "pytest", "exit_code": None}]


def test_does_not_match_substrings():
    assert not match_verification(["pytest"], [{"command": "pytest -q", "exit_code": 0}])["ok"]


@pytest.mark.parametrize("command,expected", [
    ("false && echo ok || true", "echo ok"),
    ("false && echo ok; true", "echo ok"),
    ("exit 0 && false", "false"),
    ("exec true && false", "false"),
    ("command exec true && false", "false"),
    ("eval 'exit 0' && false", "false"),
    ("trap 'exit 0' EXIT && false", "false"),
    ("false | true && echo ok", "false"),
    ("false\necho ok", "false"),
    ("true && echo ok # a comment", "echo ok"),
    ("(true) && echo ok", "echo ok"),
    ("echo $(true) && echo ok", "echo ok"),
    ("echo `true` && echo ok", "echo ok"),
])
def test_shell_success_does_not_prove_unsupported_subcommand(tmp_path, command, expected):
    # These really return zero, including the commands which skip verification.
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    result = match_verification([expected], [{"command": command, "exit_code": completed.returncode}])
    assert result == {"ok": False, "matched": [], "missing": [],
                      "failed": [{"command": expected, "exit_code": None}]}


@pytest.mark.parametrize("command", [
    "builtin exec true && false", "return 0 && false", "source script && false",
    ". script && false", "X=x exit 0 && false", "set -e && false", "export X=x && false",
    "true && $COMMAND 0 && false", "time exec true && false", "true && ! false",
    "true && false > output", "true && false &", "true && false \\\n",
])
def test_unsupported_shell_control_never_proves_false(command):
    result = match_verification(["false"], [{"command": command, "exit_code": 0}])
    assert not result["ok"]


def test_dynamic_command_word_can_expand_to_exit_and_skip_verification(tmp_path):
    (tmp_path / "exit").touch()
    command = "ex* 0 && false"
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    result = match_verification(["false"], [{"command": command, "exit_code": 0}])
    assert result["failed"] == [{"command": "false", "exit_code": None}]


def test_literal_and_quoted_operators_are_not_command_boundaries(tmp_path):
    command = "printf '%s' 'x && false && y'"
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    result = match_verification(["false"], [{"command": command, "exit_code": 0}])
    assert result["missing"] == ["false"]
    assert not result["ok"]
    # Even a literal semicolon/OR/pipeline in an argument does not change the
    # success semantics of the enclosing top-level AND chain.
    assert match_verification(["echo ok"], [{
        "command": "printf '%s' 'x && false || y; z | q' && echo ok", "exit_code": 0}])["ok"]
    assert not match_verification(["false"], [{
        "command": 'printf "%s" "x && false && y" && true', "exit_code": 0}])["ok"]
    assert not match_verification(["false"], [{
        "command": r"printf '%s' x\ \&\&\ false\ \&\&\ y && true", "exit_code": 0}])["ok"]


@pytest.mark.parametrize("command", [
    "cd repo && pytest && make test", "cd 'repo && other' && pytest && make test",
    'cd "repo || other; with | characters" && pytest && make test',
    r"cd repo\ \&\&\ other && pytest && make test",
    "printf '%s' '# literal' && pytest && make test",
])
def test_plain_and_chain_preserves_supported_verifications(command):
    assert match_verification(["pytest", "make test"], [{"command": command, "exit_code": 0}])["ok"]


@pytest.mark.parametrize("ambiguous", ["echo ok || true", "false && echo ok || true",
    "false && echo ok; true", "exit 0 && echo ok", "exec true && echo ok"])
def test_new_ambiguous_result_invalidates_old_exact_success(tmp_path, ambiguous):
    results = []
    for command in ("echo ok", ambiguous):
        completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                                   capture_output=True, timeout=5)
        assert completed.returncode == 0
        results.append({"command": command, "exit_code": completed.returncode})
    result = match_verification(["echo ok"], results)
    assert result["failed"] == [{"command": "echo ok", "exit_code": None}]
    assert not result["ok"]
    assert match_verification(["echo ok"], results + [{"command": "echo ok", "exit_code": 0}])["ok"]


def test_exact_result_and_last_result_behavior_remain_authoritative():
    command = "false || true"
    assert match_verification([command], [{"command": command, "exit_code": 0}])["ok"]
    result = match_verification(["pytest"], [{"command": "pytest", "exit_code": 0},
        {"command": "cd repo && pytest", "exit_code": 2}])
    assert result["failed"] == [{"command": "pytest", "exit_code": 2}]
    assert not result["ok"]
