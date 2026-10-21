"""Restricted mobile console commands; all durable state stays in core SQLite."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from . import constants, db, derive, util

API_VERSION = 1
PAIR_SECONDS = 300
MAX_DOCUMENT = 2 * 1024 * 1024
_CONFIG = "mobile.config"
_DEVICES = "mobile.devices"
_PAIR = "mobile.pairing"


def require_existing_database(path):
    """Do this before Context (which can initialize a new database)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise util.KanpilotError("unavailable", "手机连接需要已初始化的数据库，请先在 Mac 完成设置")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT version FROM schema_meta").fetchall()
            if len(rows) != 1 or rows[0][0] != constants.SCHEMA_VERSION:
                raise util.KanpilotError("unavailable", "数据库版本不匹配，请先在 Mac 完成迁移")
            required = {"settings", "runtime_operations", "tasks"}
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= tables:
                raise util.KanpilotError("unavailable", "数据库结构不完整，请先在 Mac 修复")
        finally:
            conn.close()
    except sqlite3.Error:
        raise util.KanpilotError("unavailable", "无法读取手机连接数据库") from None


def _config(conn):
    return db.get_setting(conn, _CONFIG, {}) or {}


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise util.bad_request("请求字段不正确")
    return dict(value)


def _string(value, name, maximum=512):
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise util.bad_request(name + " 无效")
    return value


def local_addresses():
    """Enumerate interfaces without opening network connections or doing DNS."""
    addresses = {"127.0.0.1": "lo0"}
    if Path("/sbin/ifconfig").exists():
        try:
            output = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5, check=True).stdout
            name = ""
            for line in output.splitlines():
                if line and not line[0].isspace():
                    name = line.split(":", 1)[0]
                found = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
                if found:
                    addresses[found[1]] = name
        except (OSError, subprocess.SubprocessError):
            pass
    else:  # Linux test runners; ioctl only reads local interface state.
        try:
            import fcntl
            import struct
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for _, name in socket.if_nameindex():
                    try:
                        raw = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
                        addresses[socket.inet_ntoa(raw[20:24])] = name
                    except OSError:
                        continue
        except (ImportError, OSError):
            pass
    return [{"address": address, "name": name} for address, name in sorted(addresses.items())]


def _identity(host):
    root = constants.app_support() / "mobile"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise util.KanpilotError("unavailable", "手机证书目录不安全")
    root.chmod(0o700)
    # New immutable directory makes concurrent pairing see either complete identity.
    directory = Path(tempfile.mkdtemp(prefix="identity-", dir=str(root)))
    directory.chmod(0o700)
    config = directory / "openssl.cnf"
    key, cert = directory / "key.pem", directory / "certificate.pem"
    config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                      "[dn]\nCN=KaneoPilot Mobile\n[ext]\nsubjectAltName=IP:" + host + "\n"
                      "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
                      "extendedKeyUsage=serverAuth\n", encoding="ascii")
    config.chmod(0o600)
    try:
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                        "-nodes", "-days", "825", "-config", str(config), "-keyout", str(key),
                        "-out", str(cert)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=20, check=True)
        for path in (key, cert):
            path.chmod(0o600)
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
        return {"certificate_file": str(cert), "key_file": str(key),
                "certificate_der": base64.b64encode(der).decode("ascii"),
                "certificate_sha256": hashlib.sha256(der).hexdigest()}
    except (OSError, subprocess.SubprocessError, ValueError):
        for path in (key, cert, config):
            path.unlink(missing_ok=True)
        directory.rmdir()
        raise util.KanpilotError("unavailable", "无法生成手机连接证书，请检查 /usr/bin/openssl") from None
    finally:
        config.unlink(missing_ok=True)


def cmd_mobile_status(ctx, payload):
    config = _config(ctx.conn)
    devices = db.get_setting(ctx.conn, _DEVICES, {}) or {}
    return {"api_version": API_VERSION, "server_id": config.get("server_id"),
            "enabled": bool(config.get("enabled", False)), "bind_host": config.get("bind_host"),
            "port": config.get("port", 47831), "endpoint": config.get("endpoint"),
            "certificate_sha256": config.get("certificate_sha256"), "addresses": local_addresses(),
            "devices": [{k: device.get(k) for k in ("device_id", "name", "created_at", "revoked_at")}
                        for device in devices.values()]}


def cmd_set_mobile_access(ctx, payload):
    data = _object(payload, {"enabled", "bind_host", "port"}, {"enabled"})
    if type(data["enabled"]) is not bool:
        raise util.bad_request("enabled 必须是布尔值")
    with db.write_txn(ctx.conn):
        config = _config(ctx.conn)
        host = data.get("bind_host", config.get("bind_host"))
        port = data.get("port", config.get("port", 47831))
        if type(port) is not int or not 1024 <= port <= 65535:
            raise util.bad_request("端口须为 1024–65535")
        if data["enabled"] or host is not None:
            try:
                address = ipaddress.IPv4Address(host)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError()
            except (ValueError, TypeError, ipaddress.AddressValueError):
                raise util.bad_request("请选择本机 IPv4 地址") from None
            if (data["enabled"] or "bind_host" in data) and host not in {item["address"] for item in local_addresses()}:
                raise util.KanpilotError("unavailable", "所选地址不在本机，请检查局域网或 VPN")
        changed = host != config.get("bind_host")
        if data["enabled"] and (host != config.get("identity_host") or not config.get("certificate_file")):
            identity = _identity(host)
            config.update(identity, server_id=str(uuid.uuid4()), identity_host=host)
        if changed:
            devices = db.get_setting(ctx.conn, _DEVICES, {}) or {}
            for device in devices.values():
                device["revoked_at"] = device.get("revoked_at") or ctx.now_iso
            db.set_setting(ctx.conn, _DEVICES, devices)
        # Every configuration change invalidates an outstanding pairing QR.
        db.set_setting(ctx.conn, _PAIR, None)
        config.update(enabled=data["enabled"], bind_host=host, port=port,
                      endpoint=f"https://{host}:{port}" if host else None)
        db.set_setting(ctx.conn, _CONFIG, config)
    return cmd_mobile_status(ctx, {})


def cmd_create_mobile_pairing(ctx, payload):
    token = secrets.token_urlsafe(32)
    expires = util.plus_seconds(PAIR_SECONDS, ctx.now)
    with db.write_txn(ctx.conn):
        config = _config(ctx.conn)
        if not config.get("enabled") or not config.get("certificate_der"):
            raise util.KanpilotError("unavailable", "请先启用手机连接")
        db.set_setting(ctx.conn, _PAIR, {"token_sha256": _hash(token), "expires_at": expires,
                                       "server_id": config["server_id"]})
    value = {key: config[key] for key in ("server_id", "endpoint", "certificate_der", "certificate_sha256")}
    value.update(api_version=API_VERSION, pairing_token=token, expires_at=expires)
    encoded = base64.urlsafe_b64encode(util.jdump(value).encode("utf-8")).decode("ascii").rstrip("=")
    return {"pairing_uri": "kaneopilot://pair?data=" + quote(encoded, safe=""), "expires_at": expires}


def cmd_revoke_mobile_device(ctx, payload):
    device_id = _string(payload.get("device_id"), "device_id")
    with db.write_txn(ctx.conn):
        devices = db.get_setting(ctx.conn, _DEVICES, {}) or {}
        if device_id not in devices:
            raise util.KanpilotError("not_found", "设备不存在")
        devices[device_id]["revoked_at"] = ctx.now_iso
        db.set_setting(ctx.conn, _DEVICES, devices)
    return {"ok": True}


def _pair(ctx, data):
    data = _object(data, {"pairing_token", "device_name"}, {"pairing_token", "device_name"})
    token = _string(data["pairing_token"], "配对令牌")
    name = _string(data["device_name"], "设备名称", 100).strip()
    if not name:
        raise util.bad_request("设备名称不能为空")
    new_token, device_id = secrets.token_urlsafe(32), str(uuid.uuid4())
    with db.write_txn(ctx.conn):
        config, pairing = _config(ctx.conn), db.get_setting(ctx.conn, _PAIR, {}) or {}
        expires = util.parse_ts(pairing.get("expires_at"))
        if (not config.get("enabled") or not expires or expires <= util.now_dt()
                or pairing.get("server_id") != config.get("server_id")
                or not hmac.compare_digest(_hash(token), pairing.get("token_sha256", ""))):
            raise util.KanpilotError("unauthorized", "配对内容已失效，请在 Mac 重新生成")
        devices = db.get_setting(ctx.conn, _DEVICES, {}) or {}
        devices[device_id] = {"device_id": device_id, "name": name, "created_at": ctx.now_iso,
                              "revoked_at": None, "token_sha256": _hash(new_token),
                              "server_id": config["server_id"]}
        db.set_setting(ctx.conn, _DEVICES, devices)
        db.set_setting(ctx.conn, _PAIR, None)
    return {"api_version": API_VERSION, "server_id": config["server_id"],
            "device_id": device_id, "device_token": new_token}


def _authorize(conn, token, device_id=None):
    if not isinstance(token, str) or not token or len(token) > 512:
        raise util.KanpilotError("unauthorized", "缺少有效的设备授权")
    config = _config(conn)
    if not config.get("enabled"):
        raise util.KanpilotError("unauthorized", "手机连接已关闭")
    digest = _hash(token)
    devices = db.get_setting(conn, _DEVICES, {}) or {}
    for identity, device in devices.items():
        if (hmac.compare_digest(digest, device.get("token_sha256", ""))
                and not device.get("revoked_at") and device.get("server_id") == config.get("server_id")
                and (device_id is None or device_id == identity)):
            return identity
    raise util.KanpilotError("unauthorized", "设备授权已失效，请重新配对")


# Only these arguments ever reach existing command handlers.
QUERY = {
    "overview": (set(), set(), {}),
    "attention-list": (set(), set(), {"save_state": "active", "include_historical": False}),
    "list-tasks": ({"stage", "status", "board_column", "needs_attention"}, set(), {"save_state": "active"}),
    "get-task": ({"task_id"}, {"task_id"}, {}),
    "get-plan": ({"task_id", "expected_plan_sha256"}, {"task_id", "expected_plan_sha256"}, {}),
    "list-events": ({"task_id", "before_id", "stage", "result", "query", "limit"}, {"task_id"}, {}),
    "list-requests": ({"status", "limit"}, set(), {"include_removed": False}),
    "get-request": ({"request_id"}, {"request_id"}, {}),
    "list-sessions": ({"task_id"}, {"task_id"}, {"save_state": "active", "include_removed": False}),
    "get-session": ({"task_id", "run_id", "status_only"}, {"task_id", "run_id"}, {}),
    "session-messages": ({"task_id", "run_id", "cursor", "limit", "source_version"}, {"task_id", "run_id"}, {}),
    "get-review-report": ({"task_id", "review_key", "expected_execution_run_id", "expected_report_sha256"},
                          {"task_id", "review_key", "expected_execution_run_id", "expected_report_sha256"}, {}),
}
ACTION = {
    "save-plan-answers": ({"task_id", "clarification_id", "answers", "supplement"}, {"task_id", "clarification_id", "answers"}, "answer_plan_questions"),
    "submit-plan-answers": ({"task_id", "clarification_id", "answers", "supplement"}, {"task_id", "clarification_id", "answers"}, "answer_plan_questions"),
    "save-execution-answers": ({"task_id", "request_id", "questions_sha256", "answers"}, {"task_id", "request_id", "questions_sha256", "answers"}, "answer_execution_questions"),
    "submit-execution-answers": ({"task_id", "run_id", "request_id", "questions_sha256", "answers"}, {"task_id", "run_id", "request_id", "questions_sha256", "answers"}, "answer_execution_questions"),
    "confirm-dispatch": ({"task_id", "expected_plan_sha256"}, {"task_id", "expected_plan_sha256"}, "confirm_dispatch"),
    "set-paused": ({"paused", "expected_paused"}, {"paused", "expected_paused"}, None),
}
_ACTION_NAMES = {"confirm_dispatch": "confirm-dispatch", "answer_plan_questions": "submit-plan-answers",
                 "answer_execution_questions": "submit-execution-answers"}
# Remove configuration containers, credentials and local filesystem metadata,
# but preserve ordinary message/question/Markdown text (even if it mentions a path).
_PRIVATE = {"extra", "extra_json", "config", "config_json", "settings", "environment", "env", "credentials",
            "connection", "connection_file", "repository", "cwd", "argv", "command_line", "payload", "payload_json",
            "evidence_json", "raw", "raw_json", "auth", "authorization", "certificate_der", "runtime", "spec"}
_TEXT = {"markdown", "content", "text", "title", "description", "summary", "detail", "failure_reason",
         "blocked_reason", "note", "input", "output", "question", "label", "prompt", "message"}


def project(value, field=""):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lower = key.lower()
            if key == "connection" and isinstance(item, dict):
                result[key] = project({name: item[name] for name in ("state", "detail", "subscription", "poll_seconds", "last_checked_at") if name in item})
                continue
            if (lower in _PRIVATE or any(word in lower for word in ("token", "password", "secret", "credential", "private_key", "apikey", "api_key", "access_key", "environment", "config"))
                    or lower.endswith(("_path", "_root", "_file", "_dir", "_json")) or lower in {"path", "paths", "files", "roots"}):
                continue
            if key == "allowed_actions":
                actions = []
                for action in item if isinstance(item, list) else []:
                    if action in _ACTION_NAMES:
                        mobile = _ACTION_NAMES[action]
                        actions.extend([mobile.replace("submit-", "save-"), mobile] if mobile.startswith("submit-") else [mobile])
                result[key] = actions
            else:
                result[key] = project(item, key)
        return result
    if isinstance(value, list):
        return [project(item, field) for item in value]
    if isinstance(value, str) and field not in _TEXT:
        if value.startswith(("/", "~/", "file:", "http:")):
            return None
        if (field.endswith("url") or field.endswith("link")) and not value.startswith("https://"):
            return None
    return value


def _invoke(ctx, command, args):
    from . import cli
    validated = cli.validate_payload(command, args)
    return cli.COMMANDS[command][1](ctx, validated)


def _check_action(ctx, action, args):
    expected = ACTION[action][2]
    if expected is not None:
        task = db.require_task(ctx.conn, args["task_id"])
        if expected not in derive.allowed_actions(ctx.conn, task, util.now_dt()):
            raise util.KanpilotError("forbidden", "当前任务不允许此操作，请刷新任务")
        if action == "submit-execution-answers":
            from .execution_questions import records
            current = next((question for question in records(task)
                            if question.get("request_id") == args.get("request_id")
                            and question.get("run_id") == args.get("run_id")), None)
            if current and current.get("delivery") in ("unknown", "submitting", "not_pending"):
                raise util.KanpilotError("conflict", "上次答案的结果待核实，请在 Mac 核对；手机不会重新发送")


def _receipt(row):
    if row["result_json"]:
        return json.loads(row["result_json"])
    return {"api_version": API_VERSION, "operation_id": row["operation_id"], "status": "unknown"}


def cmd_mobile_request(ctx, payload):
    payload = _object(payload, {"kind", "token", "data"}, {"kind", "data"})
    kind, data = payload["kind"], payload["data"]
    if not isinstance(kind, str):
        raise util.bad_request("手机请求类型必须是字符串")
    if not isinstance(data, dict):
        raise util.bad_request("data 必须是对象")
    if kind == "pair":
        if "token" in payload:
            raise util.bad_request("配对请求字段不正确")
        return _pair(ctx, data)
    if kind not in {"query", "action", "operation"}:
        raise util.bad_request("未知手机请求")
    device_id = _authorize(ctx.conn, payload.get("token"))
    original_guard = ctx.conn.authorization_guard

    def guard(conn):
        if original_guard:
            original_guard(conn)
        _authorize(conn, payload.get("token"), device_id)
    ctx.conn.authorization_guard = guard
    try:
        if kind == "operation":
            data = _object(data, {"operation_id"}, {"operation_id"})
            operation = _string(data["operation_id"], "operation_id", 128)
            row = db.get_runtime_operation(ctx.conn, "mobile:" + device_id, operation)
            if row is None:
                raise util.KanpilotError("not_found", "操作回执尚未登记，请稍后核对原操作")
            return _receipt(row)
        if kind == "query":
            data = _object(data, {"type", "args"}, {"type", "args"})
            name = data["type"]
            if not isinstance(name, str) or name not in QUERY:
                raise util.KanpilotError("forbidden", "手机不支持此查询")
            allowed, required, fixed = QUERY[name]
            args = _object(data["args"], allowed, required)
            if "limit" in args and (type(args["limit"]) is not int or not 1 <= args["limit"] <= 200):
                raise util.bad_request("limit 须为 1–200")
            result = _invoke(ctx, name, dict(args, **fixed))
            guard(ctx.conn)  # Revocation while a long read is in flight also fails closed.
            return {"api_version": API_VERSION, "data": project(result)}
        return _action(ctx, device_id, payload["token"], data, guard)
    finally:
        ctx.conn.authorization_guard = original_guard


def _action(ctx, device_id, token, data, device_guard):
    from . import cli, verification_checks, execution_questions
    data = _object(data, {"action", "operation_id", "args"}, {"action", "operation_id", "args"})
    action = data["action"]
    if not isinstance(action, str) or action not in ACTION:
        raise util.KanpilotError("forbidden", "手机不支持此操作")
    allowed, required, _ = ACTION[action]
    args = _object(data["args"], allowed, required)
    operation = _string(data["operation_id"], "operation_id", 128)
    # Identity fields and nested answer contents are checked by the original handler.
    for key in required - {"answers", "paused", "expected_paused"}:
        _string(args[key], key)
    command = "enqueue" if action == "confirm-dispatch" else action
    wire = dict(args)
    if action.startswith("submit-"):
        wire["operation_id"] = operation
    if action == "confirm-dispatch":
        wire.update(kind="dispatch", confirm_plan=True, actor="user")
    if action == "set-paused":
        wire["stage"] = None
    cli.validate_payload(command, wire)
    request_sha256 = _hash(util.jdump({"action": action, "args": args}))
    scope = "mobile:" + device_id
    with db.write_txn(ctx.conn):
        row = db.get_runtime_operation(ctx.conn, scope, operation)
        if row is not None:
            if row["request_sha256"] != request_sha256 or row["tool"] != action:
                raise util.KanpilotError("conflict", "操作编号已用于不同参数")
            return _receipt(row)
        _check_action(ctx, action, args)
        db.insert_runtime_operation(ctx.conn, scope, operation, device_id, action, request_sha256,
                                    "unknown", ctx.now_iso)

    # Compose, never replace, all existing task guards; every handler write_txn
    # rechecks device authorization and task availability after acquiring its lock.
    business_started = False

    def action_guard(conn):
        nonlocal business_started
        device_guard(conn)
        if conn.in_transaction and not business_started:
            _check_action(ctx, action, args)
            business_started = True
        task_id = args.get("task_id")
        if task_id:
            verification_checks.require_not_running(conn, task_id)
            if command == "enqueue":
                execution_questions.guard_active(db.require_task(conn, task_id))
    ctx.conn.authorization_guard = action_guard
    try:
        action_guard(ctx.conn)
        _check_action(ctx, action, args)
        result = _invoke(ctx, command, wire)
        status = "accepted"
        if isinstance(result, dict):
            states = [result.get("status")]
            if action == "submit-execution-answers":
                question = result.get("execution_question")
                if isinstance(question, dict):
                    states.append(question.get("delivery"))
            # not_pending is an observation, not proof that this send succeeded.
            # The original question may have been answered by another client.
            if any(state in ("unknown", "submitting", "not_pending") for state in states):
                status = "unknown"
            elif any(state in ("rejected", "not_applied") for state in states):
                status = "rejected"
        response = {"api_version": API_VERSION, "operation_id": operation, "status": status, "data": project(result)}
    except util.KanpilotError as exc:
        # An explicit downstream failure can follow an external send. Only known
        # validation/authorization failures are a definite rejection.
        send_may_have_started = False
        if action == "submit-execution-answers":
            try:
                current = db.require_task(ctx.conn, args["task_id"])
                send_may_have_started = any(
                    record.get("operation_id") == operation
                    for question in execution_questions.records(current)
                    if question.get("request_id") == args.get("request_id")
                    and question.get("run_id") == args.get("run_id")
                    for record in question.get("operations", []))
            except Exception:
                send_may_have_started = True
        status = "rejected" if (not send_may_have_started and exc.code in {
            "bad_request", "forbidden", "unauthorized", "conflict", "not_found", "validation_failed"}) else "unknown"
        response = {"api_version": API_VERSION, "operation_id": operation, "status": status,
                    "error": "操作未完成，请刷新任务后核对" if status == "unknown" else "操作已拒绝，请刷新任务后核对",
                    "code": exc.code if exc.code in {"bad_request", "forbidden", "unauthorized", "conflict", "not_found"} else "unavailable"}
    except Exception:
        response = {"api_version": API_VERSION, "operation_id": operation, "status": "unknown",
                    "error": "结果待核实，请查询原操作回执", "code": "unavailable"}
    finally:
        ctx.conn.authorization_guard = device_guard
    # Recheck device authorization, but do not recheck task's pre-action state:
    # a successful confirmation/question answer intentionally changes that state.
    with db.write_txn(ctx.conn):
        db.update_runtime_operation(ctx.conn, scope, operation, util.now_iso(), status=response["status"],
                                    result_json=util.jdump(response))
    return response


def _safe_report(path):
    root = constants.review_reports_root().absolute()
    path = Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise util.KanpilotError("forbidden", "评审报告不在可信目录") from None
    if not parts or any(part in (".", "..") for part in parts):
        raise util.KanpilotError("forbidden", "评审报告路径无效")
    # Open each component with O_NOFOLLOW; a rename/symlink race cannot escape
    # through a path checked before an ordinary read(). Include root ancestors.
    descriptor = None
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in root.parts[1:] + parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT:
                raise util.KanpilotError("too_large", "评审报告不是普通文件或超过大小限制")
            content = stream.read(MAX_DOCUMENT + 1)
        if len(content) > MAX_DOCUMENT:
            raise util.KanpilotError("too_large", "评审报告超过大小限制")
        return content
    except OSError:
        raise util.KanpilotError("not_found", "评审报告缺失或包含符号链接") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def cmd_get_review_report(ctx, payload):
    keys = {"task_id", "review_key", "expected_execution_run_id", "expected_report_sha256"}
    payload = _object(payload, keys, keys)
    for key in keys:
        _string(payload[key], key)
    task = db.require_task(ctx.conn, payload["task_id"])
    if (task["review_key"] != payload["review_key"] or task["run_id"] != payload["expected_execution_run_id"]
            or task["review_report_sha256"] != payload["expected_report_sha256"]):
        raise util.KanpilotError("conflict", "评审身份已变化，请刷新任务")
    if not task["review_report_path"]:
        raise util.KanpilotError("not_found", "当前任务没有登记评审报告")
    history = db.task_extra(task).get("review_history") or []
    review = history[-1] if history and isinstance(history[-1], dict) else {}
    if (review.get("execution_run_id") != task["run_id"] or review.get("review_key") != task["review_key"]
            or review.get("report_sha256") != task["review_report_sha256"]
            or review.get("report_path") != task["review_report_path"]):
        raise util.KanpilotError("conflict", "当前评审登记与执行轮次不一致")
    raw = _safe_report(task["review_report_path"])
    if hashlib.sha256(raw).hexdigest() != task["review_report_sha256"]:
        raise util.KanpilotError("conflict", "评审报告内容已变化")
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise util.KanpilotError("conflict", "评审报告不是有效 UTF-8") from None
    fresh = db.require_task(ctx.conn, payload["task_id"])
    if any(fresh[key] != task[key] for key in ("review_key", "run_id", "review_report_sha256", "review_report_path")):
        raise util.KanpilotError("conflict", "读取时评审身份已变化")
    return {"task_id": task["task_id"], "review_key": task["review_key"], "execution_run_id": task["run_id"],
            "report_sha256": task["review_report_sha256"], "markdown": markdown, "conclusion": review.get("conclusion")}


def cmd_mobile_serve(ctx, payload):
    from .mobile_server import serve
    return serve(ctx.db_path)
