"""Versioned, local-only stage envelopes; business evidence stays with its owner.

Contracts freeze trusted inputs. Result identities are copied by adapter code,
never accepted from a model's payload. A valid envelope alone proves no command
execution, review acceptance, or delivery side effect.
"""
from __future__ import annotations

import json
import math
import os
import re

from . import plan_questions, util

VERSION = "kp-stage-v1"
MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64
_MAX_NODES = 100000
_IDENTITY_KEYS = ("task_id", "request_id", "run_id", "harness_id", "stage")
_OUTPUT_CONTRACTS = {
    stage: "kp-" + stage + "-result-v1"
    for stage in ("plan", "execution", "review", "delivery")
}
_CONTRACT_KEYS = set(_IDENTITY_KEYS) | {
    "version", "workspace", "inputs", "config", "verification_commands",
    "allowed_actions", "output_contract", "contract_sha256",
}
_RESULT_KEYS = set(_IDENTITY_KEYS) | {
    "version", "contract_sha256", "outcome", "payload", "evidence",
}
_OUTCOMES = ("succeeded", "failed", "blocked", "partial", "cancelled")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SECRET_SUFFIXES = (
    "apikey", "accesstoken", "refreshtoken", "authtoken", "bearertoken",
    "token", "password", "passwd", "secret", "privatekey", "credential",
    "credentials", "authorization", "cookie", "setcookie",
)
_REFERENCE_SUFFIXES = ("path", "ref", "reference", "env", "envvar")


def _fail(message):
    raise util.KanpilotError("validation_failed", message)


def _json_snapshot(value, label):
    """Reject non-JSON values before using the shared canonical encoder."""
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > _MAX_NODES:
            _fail(label + " JSON 超过嵌套或元素上限")
        kind = type(item)
        if item is None or kind in (str, bool, int):
            return
        if kind is float:
            if not math.isfinite(item):
                _fail(label + " JSON 不允许非有限数")
            return
        if kind is list:
            for child in item:
                visit(child, depth + 1)
            return
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _fail(label + " JSON 对象键必须是字符串")
                visit(child, depth + 1)
            return
        _fail(label + " 必须只包含 JSON 类型")

    visit(value, 0)
    try:
        encoded = util.jdump(value)
        if len(encoded.encode("utf-8")) > MAX_BYTES:
            _fail(label + " JSON 超过 2 MiB")
        return json.loads(encoded)
    except (ValueError, UnicodeError, RecursionError):
        _fail(label + " JSON 编码无效")


def _strict_keys(value, required, label):
    if type(value) is not dict or set(value) != set(required):
        _fail(label + " 字段必须是冻结集合 " + ", ".join(sorted(required)))


def _reject_secrets(value, label):
    if type(value) is dict:
        for key, child in value.items():
            compact = re.sub(r"[^a-z0-9]", "", key.lower())
            reference = any(compact.endswith(suffix) for suffix in _REFERENCE_SUFFIXES)
            if not reference and any(compact.endswith(suffix) for suffix in _SECRET_SUFFIXES):
                # Never include the rejected value in an error or log.
                _fail(label + " 包含凭据字段；请使用脱敏配置或路径/环境变量引用")
            _reject_secrets(child, label)
    elif type(value) is list:
        for child in value:
            _reject_secrets(child, label)


def _validate_contract_fields(value):
    _strict_keys(value, _CONTRACT_KEYS, "阶段契约")
    if value["version"] != VERSION:
        _fail("不支持的阶段契约版本")
    for key in _IDENTITY_KEYS:
        if type(value[key]) is not str or not _ID_RE.fullmatch(value[key]):
            _fail("阶段契约缺少有效 " + key)
    if value["stage"] not in _OUTPUT_CONTRACTS:
        _fail("不支持的阶段")
    if value["output_contract"] != _OUTPUT_CONTRACTS[value["stage"]]:
        _fail("阶段输出契约不匹配")
    workspace = value["workspace"]
    if (type(workspace) is not str or not workspace
            or any(ord(char) < 32 or ord(char) == 127 for char in workspace)
            or not os.path.isabs(workspace) or os.path.normpath(workspace) != workspace):
        _fail("workspace 必须是规范绝对路径")
    for key in ("inputs", "config"):
        if type(value[key]) is not dict:
            _fail(key + " 必须是 JSON 对象")
        _reject_secrets(value[key], key)
    for key in ("verification_commands", "allowed_actions"):
        if type(value[key]) is not list or any(
                type(item) is not str or not item.strip() or "\x00" in item
                for item in value[key]):
            _fail(key + " 必须是非空文本项列表")


def _digest(contract):
    return util.sha256_text(util.jdump({
        key: value for key, value in contract.items() if key != "contract_sha256"
    }))


def _text_list(value, label):
    if isinstance(value, (str, bytes, dict)):
        _fail(label + " 必须是文本项序列")
    try:
        return list(value)
    except TypeError:
        _fail(label + " 必须是文本项序列")


def make_contract(stage, *, task_id, request_id, run_id, harness_id, workspace,
                  inputs, config, verification_commands=(), allowed_actions=()):
    """Freeze a trusted snapshot without reading paths, environment, or services."""
    value = _json_snapshot({
        "version": VERSION, "stage": stage, "task_id": task_id,
        "request_id": request_id, "run_id": run_id, "harness_id": harness_id,
        "workspace": workspace, "inputs": inputs, "config": config,
        "verification_commands": _text_list(verification_commands, "verification_commands"),
        "allowed_actions": _text_list(allowed_actions, "allowed_actions"),
        "output_contract": _OUTPUT_CONTRACTS.get(stage) if type(stage) is str else None,
        "contract_sha256": "",
    }, "阶段契约")
    _validate_contract_fields(value)
    value["contract_sha256"] = _digest(value)
    return validate_contract(value)


def validate_contract(value, *, expected=None):
    """Check an immutable snapshot and optional trusted expected field values."""
    value = _json_snapshot(value, "阶段契约")
    _validate_contract_fields(value)
    digest = value["contract_sha256"]
    if type(digest) is not str or not _DIGEST_RE.fullmatch(digest) or digest != _digest(value):
        _fail("阶段契约指纹不匹配")
    if expected is not None:
        expected = _json_snapshot(expected, "预期契约")
        if type(expected) is not dict or set(expected) - _CONTRACT_KEYS:
            _fail("预期契约只能校验已有字段")
        for key, item in expected.items():
            if util.jdump(value[key]) != util.jdump(item):
                _fail("阶段契约预期字段不匹配：" + key)
    return value


def _validate_result_envelope(result, contract):
    result = _json_snapshot(result, "阶段结果")
    _strict_keys(result, _RESULT_KEYS, "阶段结果")
    for key in ("version", "contract_sha256") + _IDENTITY_KEYS:
        if result[key] != contract[key]:
            _fail("阶段结果不属于当前契约：" + key)
    if result["outcome"] not in _OUTCOMES:
        _fail("阶段结果 outcome 无效")
    if type(result["payload"]) is not dict or type(result["evidence"]) is not list:
        _fail("阶段结果 payload 必须是对象且 evidence 必须是列表")
    return result


def make_result(contract, payload, *, outcome="succeeded", evidence=None):
    """Build the identity envelope in trusted adapter code, not in model output."""
    contract = validate_contract(contract)
    result = {key: contract[key] for key in ("version", "contract_sha256") + _IDENTITY_KEYS}
    result.update(outcome=outcome, payload=payload, evidence=[] if evidence is None else evidence)
    return _validate_result_envelope(result, contract)


def validate_result(result, contract, *, payload_validator=None):
    """Validate binding and stage payload; this does not authenticate evidence."""
    contract = validate_contract(contract)
    result = _validate_result_envelope(result, contract)
    payload = result["payload"]
    if result["outcome"] == "succeeded":
        if contract["stage"] == "plan":
            validate_plan_result(payload)
        elif contract["stage"] == "review":
            from . import review_settle
            review_settle.validate_result(payload)
    else:
        _strict_keys(payload, ("code", "detail"), "未成功阶段结果")
        if any(type(payload[key]) is not str or not payload[key].strip()
               for key in ("code", "detail")):
            _fail("未成功阶段结果 code/detail 必须是非空字符串")
    if payload_validator is not None:
        payload_validator(payload)
    return result


def validate_plan_result(value):
    """Strict planner schema plus existing question semantics, without repairs."""
    value = _json_snapshot(value, "规划结果")
    _strict_keys(value, ("status", "plan_markdown", "questions", "evidence"), "规划结果")
    if value["status"] not in ("ready", "needs_input"):
        _fail("规划结果 status 无效")
    if type(value["plan_markdown"]) is not str:
        _fail("plan_markdown 必须是字符串")
    if type(value["questions"]) is not list:
        _fail("questions 必须是列表")
    if type(value["evidence"]) is not list or any(type(item) is not str for item in value["evidence"]):
        _fail("规划 evidence 必须是文本列表")
    for question in value["questions"]:
        if type(question) is str:
            if not question.strip():
                _fail("历史文本问题不得为空")
            continue
        fields = ("id", "title", "description", "options", "recommended_option_id", "allow_custom", "required")
        _strict_keys(question, fields, "规划问题")
        if type(question["options"]) is not list:
            _fail("问题 options 必须是列表")
        for option in question["options"]:
            _strict_keys(option, ("id", "label", "description"), "规划问题选项")
    normalized = plan_questions.normalize(value["questions"])
    if value["status"] == "ready":
        if not value["plan_markdown"].strip() or normalized:
            _fail("ready 必须包含非空计划且没有问题")
    elif not normalized:
        _fail("needs_input 必须包含问题")
    return value
