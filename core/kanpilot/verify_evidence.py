"""Strict plan verification declarations and tool-result matching (§5.27.9)."""

from __future__ import annotations

import json
import re
import shlex


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


def extract_command_results(messages: list) -> list:
    results = []
    # Core messages carry monotonically increasing seq; timestamp breaks ties.
    ordered = sorted(enumerate(messages), key=lambda pair: (
        pair[1].get("seq") if isinstance(pair[1].get("seq"), int) else float("inf"),
        str(pair[1].get("source_at") or pair[1].get("timestamp") or pair[1].get("ts") or ""), pair[0]))
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
