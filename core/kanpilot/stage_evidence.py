"""Exact coverage of frozen commands by adapter-normalized evidence rows.

This module does not run commands, infer shell equivalence, or authenticate an
adapter's claims. In particular, model_declared evidence remains model_declared.
"""
from __future__ import annotations

import copy
import re

from . import util

_PROVENANCE = ("model_declared", "harness_observed", "core_verified")
_CHECK_KEYS = ("id", "command", "exit_code", "result", "detail", "provenance", "source_sha256")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _fail(message):
    raise util.KanpilotError("validation_failed", message)


def match_checks(commands, checks, *, expected_source_sha256=None) -> dict:
    """Classify each frozen command as passed, failed, or unconfirmed.

    Only one exact record with a known provenance, the expected source (when
    supplied), an integer exit code, and a consistent result can be confirmed.
    ``failed`` contains actual confirmed failures; ``missing`` contains commands
    with unconfirmed evidence, including duplicate or contradictory records.
    """
    if (type(commands) is not list or not commands
            or any(type(command) is not str or not command.strip()
                   or any(char in command for char in "\x00\r\n") for command in commands)
            or len(set(commands)) != len(commands)):
        _fail("冻结验证命令必须是非空、无重复、无 NUL/CR/LF 的字符串列表")
    if checks is None:
        checks = []
    elif type(checks) is not list:
        _fail("归一化 checks 必须是列表")
    if expected_source_sha256 is not None and (
            type(expected_source_sha256) is not str or not _SHA256.fullmatch(expected_source_sha256)):
        _fail("预期源码指纹必须是小写 SHA-256")

    rows, matched, missing, failed = [], [], [], []
    for command in commands:
        candidates = [item for item in checks if type(item) is dict
                      and type(item.get("command")) is str and item["command"] == command]
        record = candidates[0] if len(candidates) == 1 else {}
        code = record.get("exit_code")
        row = {"command": command, "exit_code": code if type(code) is int else None,
               "id": copy.deepcopy(record.get("id")),
               "report_check_id": copy.deepcopy(record.get("id")),
               "detail": copy.deepcopy(record.get("detail", "")),
               "result": copy.deepcopy(record.get("result")),
               "provenance": copy.deepcopy(record.get("provenance")),
               "source_sha256": copy.deepcopy(record.get("source_sha256"))}
        reason = None
        if not candidates:
            reason = "missing_record"
        elif len(candidates) != 1:
            reason = "duplicate_records"
            row["records"] = [copy.deepcopy({key: item[key] for key in _CHECK_KEYS if key in item})
                              for item in candidates]
        elif record.get("provenance") not in _PROVENANCE:
            reason = "unknown_provenance"
        elif expected_source_sha256 is not None and record.get("source_sha256") != expected_source_sha256:
            reason = "source_mismatch"
        elif code is None:
            reason = "missing_exit_code"
        elif type(code) is not int:
            reason = "invalid_exit_code"
        elif record.get("result") is None:
            reason = "missing_result"
        elif record.get("result") not in ("passed", "failed"):
            reason = "unknown_result"
        elif (code == 0) != (record["result"] == "passed"):
            reason = "contradictory_result"

        if reason is not None:
            row["unconfirmed_reason"] = reason
            missing.append(command)
        elif code == 0:
            matched.append(command)
        else:
            failed.append(row)
        rows.append(row)
    return {"ok": not missing and not failed, "matched": matched, "missing": missing,
            "failed": failed, "rows": rows}
