"""Fixed Desktop pull probe, isolated from real task and runtime commands.

The three-tool stdio server never starts an executor or accesses a repository.
Only its dedicated settings value is changed; claimed work is a fixed fixture.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

from . import db, util

SETTING_KEY = "desktop_intake_probe"
LEASE_SECONDS = 900
MAX_OUTPUT_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"
_REQUIREMENT = "本地输入框只接受 1 到 10 的整数，按钮显示平方；空输入、非整数、非数字和超范围输入显示明确错误。"
_CODE = "function calculate(raw) { const n = Number(raw); if (n < 1 || n > 10) return {error: '超范围'}; return {result: n * n}; }"
_BOUNDARY = (
    "这是固定夹具的 Desktop 主动领取验证，不是正式任务或生产验收。"
    "仅使用本工具提供的材料，不读取或修改文件、不访问仓库或远端、不派生智能体、不询问用户。"
    "完成后只调用 kp_desktop_probe_report 回传中文结果；不要把 claim_token 写进结果。"
    "thread_id 和 model 仅在确知当前会话身份时填写，不得猜测。"
)


def _validate(payload, required=(), optional=()):
    if not isinstance(payload, dict) or set(payload) - set(required) - set(optional):
        raise util.bad_request("验证参数无效")
    for key in required:
        if key not in payload or not isinstance(payload[key], str) or not payload[key].strip():
            raise util.bad_request("验证参数缺失或无效")
    for key in optional:
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
            raise util.bad_request("验证参数无效")


def _public(batch):
    if not batch:
        return {"batch_id": None, "requests": [], "detail": "尚未创建 Desktop 主动领取验证。"}
    rows = []
    for request in batch["requests"]:
        item = {key: request[key] for key in (
            "request_id", "role", "status", "output", "thread_id", "model", "execution_evidence"
        ) if key in request}
        rows.append(item)
    completed = sum(item["status"] == "completed" for item in rows)
    return {"batch_id": batch["batch_id"], "requests": rows,
            "detail": "固定验证已完成 %d/2；不影响正式任务。" % completed}


def cmd_desktop_probe_create(ctx, payload):
    _validate(payload)
    with db.write_txn(ctx.conn):
        batch = db.get_setting(ctx.conn, SETTING_KEY)
        if not batch or all(row["status"] == "completed" for row in batch["requests"]):
            batch = {"batch_id": str(uuid.uuid4()), "requests": [
                {"request_id": str(uuid.uuid4()), "role": role, "status": "queued"}
                for role in ("plan", "review")
            ]}
            db.set_setting(ctx.conn, SETTING_KEY, batch)
        return _public(batch)


def cmd_desktop_probe_status(ctx, payload):
    _validate(payload)
    return _public(db.get_setting(ctx.conn, SETTING_KEY))


def _expired(request, now):
    expiry = util.parse_ts(request.get("lease_expires_at"))
    return expiry is None or now >= expiry


def cmd_desktop_probe_claim(ctx, payload):
    _validate(payload, ("role",))
    role = payload["role"]
    if role not in ("plan", "review"):
        raise util.bad_request("验证角色必须为 plan 或 review")
    with db.write_txn(ctx.conn):
        batch = db.get_setting(ctx.conn, SETTING_KEY)
        if not batch:
            return {"available": False, "reason": "no_batch"}
        plan = next(row for row in batch["requests"] if row["role"] == "plan")
        if role == "review" and plan["status"] != "completed":
            return {"available": False, "reason": "plan_pending"}
        request = next(row for row in batch["requests"] if row["role"] == role)
        if request["status"] == "completed":
            return {"available": False, "reason": "completed"}
        now = util.now_dt()
        if request["status"] == "claimed" and not _expired(request, now):
            return {"available": False, "reason": "already_claimed"}
        request.update(status="claimed", claim_token=secrets.token_urlsafe(32),
                       lease_expires_at=util.plus_seconds(LEASE_SECONDS, now))
        db.set_setting(ctx.conn, SETTING_KEY, batch)
        instructions = _BOUNDARY + "\n需求：" + _REQUIREMENT
        if role == "plan":
            instructions += "\n仅输出两步实现计划和三条验收条件，不实现。"
        else:
            instructions += "\n只读审查以下固定代码，指出实际缺陷及触发输入（包括 1.5 和 NaN），不修复：\n" + _CODE
        result = {"available": True, "request_id": request["request_id"], "role": role,
                  "claim_token": request["claim_token"], "instructions": instructions}
        if role == "review":
            result["upstream_plan"] = plan["output"]
        return result


def cmd_desktop_probe_report(ctx, payload):
    _validate(payload, ("request_id", "claim_token", "output"), ("thread_id", "model"))
    try:
        output_size = len(payload["output"].encode("utf-8"))
        for key in ("request_id", "claim_token", "thread_id", "model"):
            if key in payload:
                payload[key].encode("utf-8")
    except UnicodeError:
        raise util.bad_request("验证结果编码无效")
    if output_size > MAX_OUTPUT_BYTES or any(len(payload[key]) > 256 for key in
                                           ("request_id", "claim_token", "thread_id", "model") if key in payload):
        raise util.bad_request("验证结果或身份字段过大")
    report = {key: payload[key] for key in ("output", "thread_id", "model") if key in payload}
    with db.write_txn(ctx.conn):
        batch = db.get_setting(ctx.conn, SETTING_KEY)
        request = next((row for row in batch["requests"] if row["request_id"] == payload["request_id"]), None) if batch else None
        if request is None or not hmac.compare_digest(request.get("claim_token", "").encode("utf-8"), payload["claim_token"].encode("utf-8")):
            raise util.KanpilotError("claim_denied", "验证领取身份不匹配")
        tokens = [row.get("claim_token", "") for row in batch["requests"]]
        if any(token and any(token in value for value in report.values()) for token in tokens):
            raise util.bad_request("验证结果不得包含领取凭据")
        if request["status"] == "completed":
            original = {key: request[key] for key in ("output", "thread_id", "model") if key in request}
            if original != report:
                raise util.KanpilotError("conflict", "验证结果已经提交且内容不同")
            return {"ok": True, "status": "completed"}
        if request["status"] != "claimed":
            raise util.KanpilotError("claim_denied", "验证请求尚未领取")
        if _expired(request, util.now_dt()):
            raise util.KanpilotError("lease_expired", "验证领取租约已过期，请重新领取")
        request.update(report)
        request.update(status="completed", execution_evidence={"pid": os.getpid(), "ppid": os.getppid()})
        db.set_setting(ctx.conn, SETTING_KEY, batch)
        return {"ok": True, "status": "completed"}


_TOOLS = [
    {"name": "kp_desktop_probe_claim", "description": "领取固定 Desktop 规划或 Review 验证；Review 等待规划完成。",
     "inputSchema": {"type": "object", "properties": {"role": {"type": "string", "enum": ["plan", "review"]}},
                     "required": ["role"], "additionalProperties": False}},
    {"name": "kp_desktop_probe_report", "description": "用领取凭据回传固定验证结果；不会写入正式任务。",
     "inputSchema": {"type": "object", "properties": {
         "request_id": {"type": "string"}, "claim_token": {"type": "string"}, "output": {"type": "string"},
         "thread_id": {"type": "string"}, "model": {"type": "string"}},
         "required": ["request_id", "claim_token", "output"], "additionalProperties": False}},
    {"name": "kp_desktop_probe_status", "description": "只读查询当前固定验证批次，不返回领取凭据。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
]
_COMMANDS = {"kp_desktop_probe_claim": "desktop-probe-claim", "kp_desktop_probe_report": "desktop-probe-report",
             "kp_desktop_probe_status": "desktop-probe-status"}


def _tool_result(name, arguments, db_path):
    try:
        if name not in _COMMANDS:
            raise util.bad_request("未知验证工具")
        from . import cli  # Avoid importing the CLI while its registry loads this module.
        result = cli.run(_COMMANDS[name], arguments, Path(db_path))
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}
    except util.KanpilotError as exc:
        code = exc.code if exc.code in ("bad_request", "claim_denied", "lease_expired", "conflict", "migration_needed") else "probe_error"
    except Exception:
        code = "probe_error"
    result = {"error": "Desktop 验证工具调用失败", "code": code}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result, "isError": True}


def _rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _dispatch(message, db_path):
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid request")
    request_id = message.get("id")
    if (isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None)))
            or (isinstance(request_id, str) and len(request_id) > 256)):
        return _rpc_error(None, -32600, "Invalid request id")
    if isinstance(request_id, str):
        try:
            request_id.encode("utf-8")
        except UnicodeError:
            return _rpc_error(None, -32600, "Invalid request id")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(request_id, -32600, "Invalid request")
    if "id" not in message:
        return None  # Notifications, including initialized, cannot mutate work.
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid params")
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "kaneopilot-desktop-probe", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _TOOLS}
    elif method == "tools/call":
        if set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
            return _rpc_error(request_id, -32602, "Invalid tool params")
        result = _tool_result(params["name"], params.get("arguments", {}), db_path)
    else:
        return _rpc_error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def run_stdio(db_path):
    """Serve JSONL MCP with bounded input/output and no diagnostic payload logs."""
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    while True:
        line = stream.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        size = len(line) if isinstance(line, bytes) else len(line.encode("utf-8"))
        if size > MAX_MESSAGE_BYTES:
            while not line.endswith(b"\n" if isinstance(line, bytes) else "\n"):
                line = stream.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
            response = _rpc_error(None, -32600, "Message too large")
        elif not line.strip():
            continue
        else:
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                response = _rpc_error(None, -32700, "Parse error")
            else:
                response = _dispatch(message, db_path)
        if response is None:
            continue
        encoded = json.dumps(response, ensure_ascii=False) + "\n"
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            encoded = json.dumps(_rpc_error(None, -32603, "Response too large")) + "\n"
        sys.stdout.write(encoded)
        sys.stdout.flush()
