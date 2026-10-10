import json
import subprocess

import pytest

from kanpilot.verify_evidence import extract_command_results, match_verification, normalize, parse_verify_block


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
    assert result["failed"] == [{"command": "pytest", "exit_code": None}]
    assert not result["ok"]


@pytest.mark.parametrize("prefix", [
    'export PATH="$PATH:/Users/sakurasep/flutter/bin:/opt/homebrew/bin:/usr/local/bin"\n',
    'export PATH="${PATH}:/opt/homebrew/bin" && ',
    'export PATH=$PATH:/usr/local/bin\n',
    'export PATH=/bin:/usr/bin && ',
])
def test_path_export_prefix_proves_single_success(tmp_path, prefix):
    command = prefix + "echo verified"
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    assert match_verification(["echo verified"], [{
        "command": command, "exit_code": completed.returncode}]) == {
            "ok": True, "matched": ["echo verified"], "missing": [], "failed": []}


@pytest.mark.parametrize("command", [
    '  export PATH="$PATH:/bin"\n\n \tprintf ok\n ',
    'export PATH="$PATH:/bin" &&\n\nprintf ok\n',
    'export PATH="$PATH:/bin"\nprintf ok && true\n',
])
def test_path_wrapper_blank_lines_and_outer_whitespace_are_inert(tmp_path, command):
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0 and completed.stdout == b"ok"
    assert match_verification(["printf ok"], [{"command": command,
        "exit_code": completed.returncode}])["ok"]


@pytest.mark.parametrize("separator", ["\n", " && "])
def test_path_prefix_uses_real_executable_and_keeps_real_failures(tmp_path, separator):
    executable = tmp_path / "verification-check"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    wrapper = 'export PATH="$PATH:' + str(tmp_path) + '"' + separator + "verification-check"
    successful = subprocess.run(["/bin/sh", "-c", wrapper], cwd=tmp_path,
                                capture_output=True, timeout=5)
    assert successful.returncode == 0
    assert match_verification(["verification-check"], [{
        "command": wrapper, "exit_code": successful.returncode}])["ok"]
    executable.write_text("#!/bin/sh\nexit 7\n")
    failure = subprocess.run(["/bin/sh", "-c", wrapper], cwd=tmp_path,
                             capture_output=True, timeout=5)
    assert failure.returncode == 7
    results = [{"command": wrapper, "exit_code": successful.returncode},
               {"command": wrapper, "exit_code": failure.returncode}]
    # The wrapper's status remains unknown for this individual command, but
    # still invalidates prior success and never allows verification to pass.
    assert match_verification(["verification-check"], results)["failed"] == [
        {"command": "verification-check", "exit_code": None}]


@pytest.mark.parametrize("command", [
    'export PATH="$PATH:/bin"\nfalse\ntrue',
    'export PATH="$PATH:/bin"\nfalse; true',
    'export PATH="$PATH:/bin"\nfalse || true',
    'export PATH="$PATH:/bin"\nfalse | true',
    'export PATH="$PATH:/bin"\n\nfalse\ntrue\n',
    'export PATH="$PATH:/bin"\nfalse # unsupported comment\ntrue\n',
    'export PATH="$PATH:/bin"\nexit 0 && false',
    'export PATH="$PATH:/bin"\nexec true && false',
    'export PATH="$PATH:/bin"\neval "exit 0" && false',
    'export PATH="$PATH:/bin"\ntrap "exit 0" EXIT && false',
    'export PATH="$PATH:/bin"\n. /dev/null && false || true',
    'export PATH="$(printf /bin)" && false || true',
    'export PATH="`printf /bin`" && false || true',
])
def test_path_prefix_cannot_mask_failed_or_skipped_verification(tmp_path, command):
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    assert match_verification(["false"], [{"command": command,
        "exit_code": completed.returncode}])["failed"] == [{"command": "false", "exit_code": None}]


@pytest.mark.parametrize("prefix", [
    'export PATH="$HOME:/bin"', 'export PATH="$PATH_EXTRA:/bin"',
    'export PATH="${PATH:-/bin}"', 'export PATH="${PATH%:*}"',
    'export PATH="/bin" X=x', 'export X=x', 'source /dev/null',
    'export PATH="/bin"\nexport PATH="/usr/bin"',
])
def test_only_bounded_path_export_is_supported(prefix):
    result = match_verification(["echo ok"], [{"command": prefix + "\necho ok", "exit_code": 0}])
    assert result["failed"] == [{"command": "echo ok", "exit_code": None}]


def test_path_chain_success_proves_all_and_commands_but_not_newline_commands(tmp_path):
    for separator, passed in [(" && ", True), ("\n", False)]:
        command = 'export PATH="$PATH:/bin"\necho first' + separator + "echo second"
        completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                                   capture_output=True, timeout=5)
        assert completed.returncode == 0
        result = match_verification(["echo first", "echo second"], [{
            "command": command, "exit_code": completed.returncode}])
        assert result["ok"] is passed


@pytest.mark.parametrize("command", ["true && false", "false && true", "false && false"])
def test_nonzero_and_wrapper_does_not_blame_every_segment(tmp_path, command):
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 1
    result = match_verification(["true", "false"], [{"command": command,
        "exit_code": completed.returncode}])
    assert all(item["exit_code"] is None for item in result["failed"])


@pytest.mark.parametrize("code", [None, "0", False, 2])
def test_latest_path_wrapper_unknown_cannot_reuse_previous_success(code):
    wrapper = 'export PATH="$PATH:/bin"\npytest'
    result = match_verification(["pytest"], [
        {"command": "pytest", "exit_code": 0}, {"command": wrapper, "exit_code": code}])
    assert result["failed"] == [{"command": "pytest", "exit_code": None}]
    assert match_verification(["pytest"], [
        {"command": wrapper, "exit_code": code}, {"command": "pytest", "exit_code": 0}])["ok"]


def test_three_real_style_path_verifications_are_sorted_by_sequence():
    commands = [
        "dart format --output=none --set-exit-if-changed lib/core/providers.dart",
        "flutter analyze --no-pub lib/core/providers.dart",
        "flutter test --no-pub test/daily_recommend_shuffle_test.dart",
    ]
    messages = [{"seq": seq, "type": "command_output", "tool": {"exit_code": 0,
        "input": {"command": 'export PATH="$PATH:/Users/sakurasep/flutter/bin:/opt/homebrew/bin:/usr/local/bin"\n' + command}}}
        for seq, command in zip((235, 237, 239), commands)]
    old_failure = {"seq": 100, "type": "command_output", "tool": {
        "exit_code": 1, "input": {"command": commands[0]}}}
    results = extract_command_results(list(reversed(messages)) + [old_failure])
    assert match_verification(commands, results)["ok"]


def test_normalization_preserves_shell_argument_whitespace():
    assert normalize('  echo  "a  b"  ') == 'echo "a  b"'
    assert normalize("echo 'a  b'") == "echo 'a  b'"
    assert normalize(r"echo a\ \ b") == r"echo a\ \ b"
    assert normalize("false\ntrue") == "false\ntrue"
    declaration = parse_verify_block("```verify\n  test  'a  b'  =  'a b'  \n```")
    assert declaration["commands"] == ["test 'a  b' = 'a b'"]


@pytest.mark.parametrize("expected,actual", [
    ("false true", "false\ntrue"),
    ("test 'a  b' = 'a b'", "test 'a b' = 'a b'"),
    ('test "a  b" = "a b"', 'test "a b" = "a b"'),
])
def test_normalization_cannot_turn_different_success_into_verification(tmp_path, expected, actual):
    failed = subprocess.run(["/bin/sh", "-c", expected], cwd=tmp_path,
                            capture_output=True, timeout=5)
    completed = subprocess.run(["/bin/sh", "-c", actual], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert failed.returncode == 1 and completed.returncode == 0
    assert not match_verification([expected], [{"command": actual,
        "exit_code": completed.returncode}])["ok"]


def test_path_wrapper_does_not_collapse_quoted_arguments(tmp_path):
    expected = "test 'a  b' = 'a b'"
    command = 'export PATH="$PATH:/bin"\n' + "test 'a b' = 'a b'"
    completed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    assert not match_verification([expected], [{"command": command,
        "exit_code": completed.returncode}])["ok"]
