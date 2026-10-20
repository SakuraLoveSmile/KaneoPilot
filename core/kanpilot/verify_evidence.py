"""Strict plan verification declarations and tool-result matching (§5.27.9)."""

from __future__ import annotations

import json
import re
import shlex

from . import util


# Only these scalar fields carry execution identity/provenance through adapters.
WINDOW_FIELDS = ("turn_id", "source_timestamp_missing", "execution_turn_id",
                 "execution_started_at", "approval_requested_at", "tool_call_id",
                 "parent_tool_call_id", "agent_name", "event_type", "is_error")


def window_metadata(source: dict) -> dict:
    metadata = {}
    for name in WINDOW_FIELDS:
        value = source.get(name)
        if name in ("source_timestamp_missing", "is_error"):
            if isinstance(value, bool):
                metadata[name] = value
        elif isinstance(value, str) and value:
            metadata[name] = value
    return metadata


def normalize(command: str) -> str:
    # Fold only shell word separators. Quoted/escaped whitespace belongs to
    # an argument, and physical newlines can separate commands rather than
    # arguments; neither can safely be replaced by a space for matching.
    normalized, quote, pending_space = [], None, False
    index = 0
    while index < len(command):
        char = command[index]
        if quote is None and char in " \t":
            pending_space = True
            index += 1
            continue
        if pending_space and normalized and normalized[-1] not in "\r\n":
            normalized.append(" ")
        pending_space = False
        if char == "\\" and quote != "'":
            normalized.append(command[index:index + 2])
            index += 2
            continue
        normalized.append(char)
        if char == quote:
            quote = None
        elif quote is None and char in ("'", '"'):
            quote = char
        index += 1
    return "".join(normalized)


def parse_verify_block(plan_text: str) -> dict:
    blocks = []
    fence = None
    body = []
    is_verify = False
    for line in plan_text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None and match:
            fence = match.group(1)
            is_verify = fence[0] == "`" and match.group(2).strip() == "verify"
            body = []
        elif fence is not None and match and match.group(1)[0] == fence[0] \
                and len(match.group(1)) >= len(fence) and not match.group(2).strip():
            if is_verify:
                blocks.append(body)
            fence = None
        elif fence is not None:
            body.append(line)
    if fence is not None and is_verify:
        return {"ok": False, "error": "verify 围栏块未闭合"}
    if len(blocks) != 1:
        if not blocks and _has_verify_fence_line(plan_text):
            # 纯诊断：文本里确有 info 为 verify 的围栏行，却没有一个被识别成
            # 顶层 verify 块——最常见原因是计划把 verify 围栏嵌进了另一个围栏
            # （外层 ``` 遇 info 非空不闭合，于是把内层整段吞掉）。不改变判定
            # 结果（仍然 error），只把定位时间从人工翻计划降到几秒。
            return {"ok": False, "error": _NESTED_VERIFY_ERROR}
        return {"ok": False, "error": "计划必须恰好包含一个 verify 围栏块"}
    commands = []
    for line in blocks[0]:
        command = normalize(line)
        if not command or command.startswith("#"):
            continue
        if command.endswith("\\") or "<<" in command:
            return {"ok": False, "error": "验证命令不允许续行或 heredoc"}
        if re.match(r"^(?:git (?:commit|push|reset|checkout|switch)(?:\s|$)|gh(?:\s|$))", command):
            return {"ok": False, "error": "验证命令不允许 Git 写操作或 gh"}
        commands.append(command)
    if not 1 <= len(commands) <= 5:
        return {"ok": False, "error": "verify 块必须包含 1–5 条非空命令"}
    return {"ok": True, "commands": commands}


# info 为 verify 的围栏行（不论是否被解析识别为顶层块）。
_VERIFY_FENCE_LINE = re.compile(r"^\s{0,3}(`{3,}|~{3,})verify\s*$")

_NESTED_VERIFY_ERROR = (
    "verify 围栏似被其他围栏嵌套：计划中的 ```verify 块必须位于顶层，"
    "不得嵌在任何其他代码围栏内（空 info 的 ``` 不会闭合，会把内层整段吞掉）。"
    "请去掉外层围栏，或把围栏内的示例代码改成缩进代码"
)


def _has_verify_fence_line(plan_text: str) -> bool:
    """文本里是否存在 info 为 ``verify`` 的围栏行（诊断用，不做意图判断）。"""
    return any(_VERIFY_FENCE_LINE.match(line) for line in plan_text.splitlines())


def _ordered(messages: list) -> list:
    # Core messages carry monotonically increasing seq; timestamp breaks ties.
    return sorted(enumerate(messages), key=lambda pair: (
        pair[1].get("seq") if isinstance(pair[1].get("seq"), int) else float("inf"),
        str(pair[1].get("source_at") or pair[1].get("timestamp") or pair[1].get("ts") or ""), pair[0]))


def _window_field(message: dict, name: str):
    raw = message.get("raw")
    return message.get(name, raw.get(name) if isinstance(raw, dict) else None)


def _timestamp(message: dict):
    if _window_field(message, "source_timestamp_missing") is True:
        return None
    return util.parse_ts(message.get("source_at") or message.get("timestamp") or message.get("ts"))


def execution_window(messages: list, execution_turn_id=None) -> tuple:
    """Select trusted Goal execution evidence, never by collection order/time.

    A transcript may have been backfilled after the local approval event. Turn
    identity wins; timestamps are used only for messages without a turn label.
    Unscoped historical Goal rounds remain explicitly whole_round, so callers
    can refuse to treat their drafting evidence as execution verification.
    """
    ordered = [message for _, message in _ordered(messages)]
    markers = [message for message in ordered
               if message.get("type", message.get("kind")) == "system"
               and not message.get("source_message_id")
               and not str(message.get("id") or "").startswith("desktop:")
               and isinstance(message.get("content"), str)
               and message["content"].startswith(("goal_approved:", "goal_execution_recovered:"))]
    if not markers:
        return ordered, {"evidence_window": "whole_round", "window_basis": "whole_round"}
    marker = markers[-1]
    marker_turn = _window_field(marker, "execution_turn_id")
    turn_id = execution_turn_id or marker_turn
    turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
    started_at = _window_field(marker, "execution_started_at")
    boundary = util.parse_ts(started_at)
    basis = "execution_started_at"
    if boundary is None:
        boundary = util.parse_ts(_window_field(marker, "approval_requested_at"))
        basis = "approval_request"
    if execution_turn_id and marker_turn and execution_turn_id != marker_turn:
        # A continuation's identity does not make the original execution's
        # start time its own. Without a current-turn start proof, accept only
        # messages explicitly labelled with the bound continuation turn.
        boundary = None
    scoped = turn_id is not None and any(_window_field(message, "turn_id") == turn_id
                                         for message in ordered)
    if boundary is None and not scoped:
        return ordered, {"evidence_window": "whole_round", "window_basis": "unscoped_goal_window"}
    selected = []
    for message in ordered:
        source_turn = _window_field(message, "turn_id")
        if isinstance(source_turn, str) and source_turn:
            if turn_id is None or source_turn != turn_id:
                continue
        else:
            timestamp = _timestamp(message)
            if boundary is None or timestamp is None or timestamp < boundary:
                continue
        selected.append(message)
    return selected, {"evidence_window": "after_goal_approved",
                      "window_basis": "execution_turn" if scoped else basis}


def extract_command_results(messages: list) -> list:
    results = []
    ordered = _ordered(messages)
    for index, message in ordered:
        raw = message.get("raw") or {}
        tool = message.get("tool") or (raw.get("tool") if isinstance(raw, dict) else None)
        if not isinstance(tool, dict):
            continue
        # Calls/assistant prose are not command results, even with forged text.
        if message.get("type", message.get("kind", raw.get("type"))) not in ("command_output", "tool_result"):
            continue
        arguments = tool.get("input")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                continue
        if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
            continue
        code = tool.get("exit_code")
        results.append({"command": arguments["command"],
                        "exit_code": code if type(code) is int else None,
                        "message_id": message.get("id"), "index": index})
    return results


# These commands can stop/replace the shell, install later control flow, or hide
# a shell builtin behind a wrapper. This is a bounded recognizer, not a shell
# interpreter: unsupported syntax needs a separate exact command result.
_SHELL_CONTROL = frozenset((
    "!", ".", "alias", "break", "builtin", "command", "continue", "declare",
    "enable", "eval", "exec", "exit", "export", "function", "if", "for",
    "case", "while", "until", "select", "do", "done", "then", "else", "elif",
    "fi", "esac", "in", "let", "local", "readonly", "return", "set", "shift",
    "shopt", "source", "trap", "typeset", "unalias", "unset", "time",
    "coproc", "noglob", "nocorrect",
))


def _path_prefix(command: str):
    """Recognize only an inert PATH export before the remaining command.

    No shell evaluation is performed. PATH is the only permitted expansion;
    quotes, substitutions, options, and other environment assignments cannot
    turn the prefix into shell control flow.
    """
    match = re.fullmatch(
        r'[ \t]*(export[ \t]+PATH=(?:"[^"\r\n]*"|[^ \t\r\n&;|]+))'
        r'[ \t]*(?:&&|\r?\n)[ \t\n]*(.+)', command, re.DOTALL)
    if not match:
        return None
    prefix, remaining = match.groups()
    value = prefix.split("=", 1)[1]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
        literal = r'[\w/.:+ -]'
    else:
        literal = r'[\w/.:+-]'
    if not re.fullmatch(r'(?:\$(?:PATH(?![A-Za-z0-9_])|\{PATH\})|' + literal + r')+', value):
        return None
    return normalize(prefix), remaining


def _command_segments(command: str, *, allow_single: bool = False) -> tuple:
    """Return top-level segments and whether success proves all AND segments.

    Even unsupported chains retain their recognizable top-level segments so a
    newer ambiguous result cannot resurrect older passing verification evidence.
    Quoted/escaped operators are argument data and never segment separators.
    """
    command = command.strip(" \t\r\n")
    path_prefix = _path_prefix(command) if not allow_single else None
    if path_prefix is not None:
        prefix, remaining = path_prefix
        # Only this first export may precede a newline. A later newline could
        # hide failure behind the exit status of another verification command.
        segments, supported = _command_segments(remaining, allow_single=True)
        return [prefix] + segments, supported
    segments, operators = [], []
    start, index, quote = 0, 0, None
    supported = True
    while index < len(command):
        char = command[index]
        if char in "\r\n":
            supported = False
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(command) or command[index + 1] in "\r\n":
                supported = False
            index += 2
            continue
        if char == "`" or (char == "$" and command[index + 1:index + 2] in ("(", "{", "'", '"')):
            supported = False
        if quote == '"':
            if char == '"':
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        # Shell comments only start at a word boundary. Their contents cannot
        # introduce command segments, even if they contain an operator.
        if char == "#" and (index == 0 or command[index - 1].isspace()
                            or command[index - 1] in "&|;()"):
            supported = False
            segments.append(normalize(command[start:index]))
            newline = command.find("\n", index)
            if newline == -1:
                start = len(command)
                break
            operators.append("comment")
            index = start = newline + 1
            continue
        if char in "&|;\r\n":
            operator = command[index:index + 2] if command[index:index + 2] in ("&&", "||") else char
            segments.append(normalize(command[start:index]))
            operators.append(operator)
            if operator != "&&":
                supported = False
            index += len(operator)
            start = index
            continue
        if char in "()<>{}":
            supported = False
        index += 1
    if quote is not None:
        supported = False
    segments.append(normalize(command[start:]))
    if (not operators and not allow_single) or any(not segment for segment in segments):
        supported = False
    for segment in segments:
        try:
            words = shlex.split(segment)
        except ValueError:
            supported = False
            continue
        # Assignments before a builtin do not make that builtin an ordinary
        # external process. Preserve their raw spelling for segment matching.
        while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
            words.pop(0)
        if not words or words[0] in _SHELL_CONTROL or any(char in words[0] for char in "$*?["):
            supported = False
    return segments, supported


def match_verification(commands: list, results: list) -> dict:
    matched, missing, failed = [], [], []
    for command in commands:
        expected = normalize(command)
        matches = []
        for result in results:
            raw_command = result["command"].strip(" \t\r\n")
            if not any(char in raw_command for char in "\r\n") and expected == normalize(raw_command):
                matches.append(result)
                continue
            segments, supported = _command_segments(result["command"])
            if expected in segments:
                # A zero exit proves every segment in a supported AND chain
                # ran successfully. A nonzero wrapper status cannot identify
                # which segment failed or which later commands were skipped.
                proven = supported and type(result.get("exit_code")) is int and result["exit_code"] == 0
                matches.append(result if proven else dict(result, exit_code=None))
        if not matches:
            missing.append(command)
        else:
            last = matches[-1]
            if type(last.get("exit_code")) is int and last["exit_code"] == 0:
                matched.append(command)
            else:
                failed.append({"command": command, "exit_code": last.get("exit_code")})
    return {"ok": not missing and not failed, "matched": matched,
            "missing": missing, "failed": failed}


# Exact names only: unknown tools, including delegated/Team tools, can write.
# These are the existing filesystem inspection tools, not arbitrary read* names.
_READ_ONLY_TOOLS = frozenset(("read", "grep", "glob"))
_SHELL_TOOLS = frozenset(("bash", "shell", "exec", "run_command", "terminal", "command"))


def _tool(message):
    value = _window_field(message, "tool")
    return value if isinstance(value, dict) else {}


def _tool_input(tool):
    arguments = tool.get("input")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    return arguments if isinstance(arguments, dict) else None


def _verification_shell(command, commands):
    """Only declared checks and inert cwd/PATH prefixes may share a shell.

    Matching a check inside `pytest && edit-source` must not make the entire
    invocation exempt from the write barrier. The plan's exact commands remain
    authoritative; only wrappers get this additional bounded recognition.
    """
    expected = {normalize(item) for item in commands}
    if normalize(command.strip(" \t\r\n")) in expected:
        return True
    prefix = _path_prefix(command)
    remaining = prefix[1] if prefix is not None else command
    segments, supported = _command_segments(remaining, allow_single=True)
    if not supported:
        return False
    has_check = False
    for segment in segments:
        if segment in expected:
            has_check = True
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if (not words or words[0] != "cd" or len(words) not in (2, 3)
                or (len(words) == 3 and words[1] != "--")
                or any(char in segment for char in "$`<>")):
            return False
    return has_check


def _strict_execution_window(messages, execution_turn_id):
    ordered = _ordered(messages)
    # Only locally generated approval markers carry a trustworthy time bound.
    markers = [message for _, message in ordered
               if message.get("type", message.get("kind")) == "system"
               and not message.get("source_message_id")
               and not str(message.get("id") or "").startswith("desktop:")
               and isinstance(message.get("content"), str)
               and message["content"].startswith(("goal_approved:", "goal_execution_recovered:"))
               and _window_field(message, "execution_turn_id") == execution_turn_id]
    marker = markers[-1] if markers else {}
    boundary = util.parse_ts(_window_field(marker, "execution_started_at"))
    basis = "execution_started_at"
    if boundary is None:
        boundary = util.parse_ts(_window_field(marker, "approval_requested_at"))
        basis = "approval_request"
    selected = []
    scoped = False
    for index, message in ordered:
        turn = _window_field(message, "turn_id")
        timestamp = _timestamp(message)
        if isinstance(turn, str) and turn:
            if turn != execution_turn_id:
                continue
            if boundary is not None and timestamp is not None and timestamp < boundary:
                continue
            scoped = True
        elif boundary is None or timestamp is None or timestamp < boundary:
            continue
        selected.append((index, message))
    return selected, {"evidence_window": "current_execution_turn",
                      "window_basis": "execution_turn" if scoped else basis if boundary else "unconfirmed_execution_turn"}


def match_execution_verification(commands: list, messages: list, execution_turn_id) -> dict:
    """Match current Plus checks and revoke proof after any possible source write.

    Completed transcript tools can be represented by a single result; where a
    separate call exists, its turn, name, input and completion must agree. Calls
    left pending are not proof and cannot be assumed to have finished writing.
    `command_results` carries original message indexes for further cwd/output
    checks by the verdict; it is not a second persisted evidence stream.
    """
    if not isinstance(execution_turn_id, str) or not execution_turn_id:
        return dict(match_verification(commands, []), command_results=[],
                    evidence_window="current_execution_turn", window_basis="unconfirmed_execution_turn",
                    detail="无法确认 Plus 当前执行轮次，请由 Plus 重新验证")
    selected, metadata = _strict_execution_window(messages, execution_turn_id)
    selected_indexes = {index for index, _ in selected}
    all_calls = {}
    for index, message in _ordered(messages):
        if message.get("type", message.get("kind")) == "tool_call":
            call_id = _window_field(message, "tool_call_id")
            if isinstance(call_id, str) and call_id:
                all_calls.setdefault(call_id, []).append((index, message))
    positions = {index: position for position, (index, _) in enumerate(selected)}
    results, barriers, completed_calls = [], [], set()
    pending = []
    active_subagents = {}
    for position, (index, message) in enumerate(selected):
        kind = message.get("type", message.get("kind"))
        tool = _tool(message)
        delegated = bool(_window_field(message, "parent_tool_call_id") or _window_field(message, "agent_name"))
        if kind not in ("tool_call", "tool_result", "command_output"):
            event_type = _window_field(message, "event_type")
            if event_type in ("subagent_start", "subagent_end"):
                agent = _window_field(message, "agent_name") or ""
                active_subagents[agent] = max(0, active_subagents.get(agent, 0)
                                             + (1 if event_type == "subagent_start" else -1))
            if delegated or event_type in ("subagent_start", "subagent_end"):
                barriers.append(position)
            continue
        name = str(tool.get("name") or "").strip().lower()
        arguments = _tool_input(tool)
        call_id = _window_field(message, "tool_call_id")
        calls = all_calls.get(call_id, []) if isinstance(call_id, str) else []
        pair_ok = True
        start_position = position
        if kind != "tool_call" and calls:
            # A result cannot borrow another turn's invocation or contradict
            # the invocation's command. Multiple call identities are ambiguous.
            pair_ok = len(calls) == 1 and calls[0][0] in selected_indexes
            if pair_ok:
                call_index, call = calls[0]
                call_tool = _tool(call)
                call_arguments = _tool_input(call_tool)
                pair_ok = (positions[call_index] < position
                           and str(call_tool.get("name") or "").strip().lower() == name
                           and ((arguments is None and tool.get("input") is None)
                                or arguments == call_arguments))
                if pair_ok:
                    arguments = call_arguments
                    completed_calls.add(call_index)
                    start_position = positions[call_index]
                    delegated = delegated or bool(_window_field(call, "parent_tool_call_id")
                                                  or _window_field(call, "agent_name"))
        if name in _READ_ONLY_TOOLS and pair_ok and not delegated:
            continue
        command = arguments.get("command") if isinstance(arguments, dict) else None
        is_verify = (pair_ok and not delegated and name in _SHELL_TOOLS and isinstance(command, str)
                     and _verification_shell(command, commands))
        if kind == "tool_call":
            pending.append(index)
            if not is_verify:
                barriers.append(position)
            continue
        if not is_verify:
            barriers.append(position)
            continue
        code = tool.get("exit_code")
        if _window_field(message, "is_error") is True:
            code = None
        results.append({"command": command, "exit_code": code if type(code) is int else None,
                        "message_id": message.get("id"), "index": index, "_position": start_position})
    for index in pending:
        if index not in completed_calls:
            # An unfinished tool may still change source after a later check.
            barriers.append(len(selected))
    if any(active_subagents.values()):
        barriers.append(len(selected))
    barrier = max(barriers, default=-1)
    for result in results:
        if result.pop("_position") <= barrier:
            result["exit_code"] = None
    outcome = dict(match_verification(commands, results), **metadata, command_results=results)
    if barriers and not outcome["ok"]:
        outcome["detail"] = "Plus 验证后存在可能修改源码或尚未完成的工具调用，请由 Plus 重新验证"
    elif not selected:
        outcome["detail"] = "缺少当前执行轮次的可信工具结果，请由 Plus 重新验证"
    return outcome


# Plus structured Goal report checks (protocol §5.18.6, evidence_source=goal_report).
_GOAL_ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+")
_GOAL_NOTE = re.compile(r"\s*（.*", re.S)


def goal_command_key(command) -> str:
    """Compare a frozen verify command with a report check by its executed text.

    Trailing Chinese notes and inline ``VAR=value`` prefixes are dropped; the
    remaining shell words are folded with ``normalize``.
    """
    if not isinstance(command, str):
        return ""
    text = _GOAL_NOTE.sub("", command.strip(), count=1).strip()
    return normalize(_GOAL_ENV_PREFIX.sub("", text, count=1).strip())


def match_goal_checks(commands: list, checks) -> dict:
    """Pair every frozen verify command with the last matching Plus report check.

    A command passes only when that last record has ``exitCode`` 0 and
    ``result`` "passed". A real nonzero exit is a failure; anything else (no
    record, a non-integer exit, a contradictory result) stays unconfirmed.
    """
    records = [item for item in checks or [] if isinstance(item, dict)]
    rows, matched, failed, missing = [], [], [], []
    for command in commands or []:
        key = goal_command_key(command)
        matches = [item for item in records if key and goal_command_key(item.get("command")) == key]
        last = matches[-1] if matches else None
        code = last.get("exitCode") if last is not None else None
        exit_code = code if type(code) is int else None
        row = {"command": command, "exit_code": exit_code, "report_check_id": (last or {}).get("id"),
               "detail": str((last or {}).get("detail") or "")}
        rows.append(row)
        if last is not None and exit_code == 0 and last.get("result") == "passed":
            matched.append(command)
        elif last is not None and exit_code not in (None, 0):
            failed.append(row)
        else:
            row["unconfirmed_reason"] = ("missing_record" if last is None else
                                         "missing_exit_code" if exit_code is None else
                                         "contradictory_result")
            missing.append(command)
    unconfirmed = [{"command": command, "exit_code": None} for command in missing]
    return {"ok": bool(rows) and not failed and not missing, "matched": matched, "missing": missing,
            "failed": failed + unconfirmed, "rows": rows}
