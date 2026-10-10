"""Bounded control of Codex Desktop's current local heartbeat TOML format.

This is an installed-format compatibility adapter, not a public Codex API. It
never opens Codex's SQLite database. BEGIN IMMEDIATE serializes KaneoPilot
callers only; identity/content checks cannot provide CAS against Codex edits.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import sqlite3
import stat
import time
from xml.parsers.expat import ExpatError

from . import db, desktop_queue, util

# util installs core/_vendor on sys.path, including Python 3.9-compatible tomli.
import tomli

_ROLES = ("plan", "review")
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_REQUIRED = {"version", "id", "kind", "name", "prompt", "status", "rrule",
             "target_thread_id", "created_at", "updated_at"}
_OPTIONAL = {"model", "reasoning_effort", "notification_policy"}
_LIMIT = 2 * 1024 * 1024
_NOFOLLOW = os.O_NOFOLLOW
_APP_VERSIONS = (
    ("26.1002.52244", "13536"),
    ("26.1007.21159", "20052"),
)


class _Unavailable(Exception):
    pass


def _home():
    value = os.environ.get("KANPILOT_CODEX_HOME") or os.environ.get("CODEX_HOME") or "~/.codex"
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise _Unavailable("Codex 本地目录必须为不含上级跳转的绝对路径")
    return path


def _check_app_version():
    value = os.environ.get("KANPILOT_CODEX_APP") or "/Applications/ChatGPT.app"
    app = Path(value).expanduser()
    if not app.is_absolute() or ".." in app.parts:
        raise _Unavailable("Codex 应用路径必须为不含上级跳转的绝对路径")
    with _directory(app / "Contents") as contents_fd:
        raw, _ = _read(contents_fd, "Info.plist")
    try:
        metadata = plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError, ExpatError):
        raise _Unavailable("Codex 应用版本信息无效") from None
    if (not isinstance(metadata, dict)
            or (metadata.get("CFBundleShortVersionString"), metadata.get("CFBundleVersion")) not in _APP_VERSIONS):
        raise _Unavailable("当前 Codex 应用版本未验证本地自动任务兼容性，无法控制")


@contextmanager
def _directory(path):
    """Traverse every component without following symlinks, including parents."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def _read(directory_fd, name):
    # O_NONBLOCK prevents a malicious FIFO from blocking before regular-file check.
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _LIMIT:
            raise _Unavailable("Codex 配置须为正常大小的普通文件")
        chunks, size = [], 0
        while True:
            block = os.read(fd, min(65536, _LIMIT + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > _LIMIT:
                raise _Unavailable("Codex 配置文件过大")
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(named):
            raise _Unavailable("读取时 Codex 配置发生变化，请刷新后重试")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _empty_role(detail, status="UNSUPPORTED", automation_id=None):
    return {"automation_id": automation_id, "name": None, "status": status,
            "enabled": None, "controllable": False, "detail": detail,
            **_schedule_fields(None)}


def _parse_schedule(rrule):
    """Recognize only the two schedules whose semantics this editor preserves."""
    if not isinstance(rrule, str):
        return None
    value = rrule[6:] if rrule.startswith("RRULE:") else rrule
    fields = {}
    for part in value.split(";"):
        if part.count("=") != 1:
            return None
        key, field_value = part.split("=")
        if key in fields:
            return None
        fields[key] = field_value
    if fields.get("FREQ") == "MINUTELY" and set(fields) == {"FREQ", "INTERVAL"}:
        minutes = fields["INTERVAL"]
        if re.fullmatch(r"[0-9]{1,4}", minutes) and 1 <= int(minutes) <= 1440:
            return {"kind": "interval", "minutes": int(minutes)}
    elif (fields.get("FREQ") == "DAILY"
          and set(fields) in ({"FREQ", "BYHOUR", "BYMINUTE"},
                              {"FREQ", "BYHOUR", "BYMINUTE", "BYSECOND"})
          and fields.get("BYSECOND", "0") == "0"):
        hour, minute = fields["BYHOUR"], fields["BYMINUTE"]
        if (re.fullmatch(r"[0-9]{1,2}", hour) and 0 <= int(hour) <= 23
                and re.fullmatch(r"[0-9]{1,2}", minute) and 0 <= int(minute) <= 59):
            return {"kind": "daily", "hour": int(hour), "minute": int(minute)}
    return None


def _schedule_fields(rrule):
    schedule = _parse_schedule(rrule)
    local = datetime.now().astimezone()
    offset = local.strftime("%z")
    zone = f"{local.tzname()} (UTC{offset[:3]}:{offset[3:]})"
    summary = "尚未读取运行时间" if rrule is None else "当前周期不支持编辑"
    if schedule is not None:
        summary = (f"每{schedule['minutes']}分钟" if schedule["kind"] == "interval" else
                   f"每天{schedule['hour']:02d}:{schedule['minute']:02d}")
    return {"rrule": rrule, "schedule": schedule, "schedule_summary": summary,
            "schedule_editable": schedule is not None, "time_zone": zone}


def _schedule_rrule(schedule):
    if not isinstance(schedule, dict):
        raise util.bad_request("运行时间须为间隔分钟或每天时间")
    if schedule.get("kind") == "interval" and set(schedule) == {"kind", "minutes"}:
        minutes = schedule["minutes"]
        if type(minutes) is int and 1 <= minutes <= 1440:
            return f"RRULE:FREQ=MINUTELY;INTERVAL={minutes}"
    elif schedule.get("kind") == "daily" and set(schedule) == {"kind", "hour", "minute"}:
        hour, minute = schedule["hour"], schedule["minute"]
        if type(hour) is int and type(minute) is int and 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"RRULE:FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"
    raise util.bad_request("间隔须为 1 到 1440 分钟，每天时间须为 00:00 到 23:59")


def _validate_record(data, automation_id):
    if not isinstance(data, dict) or set(data) - _REQUIRED - _OPTIONAL or not _REQUIRED <= set(data):
        raise _Unavailable("Codex 自动任务格式未知或包含账户/云端迁移字段，无法控制")
    if type(data["version"]) is not int or data["version"] != 1 or data["kind"] != "heartbeat":
        raise _Unavailable("仅支持版本 1 的本地 heartbeat 自动任务")
    if data["id"] != automation_id:
        raise _Unavailable("自动任务文件 ID 与绑定不一致")
    for key in ("id", "name", "prompt", "rrule", "target_thread_id"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise _Unavailable("Codex 自动任务必要字段无效")
    if data["status"] not in ("ACTIVE", "PAUSED"):
        raise _Unavailable("Codex 自动任务状态格式不支持")
    if any(type(data[key]) is not int or data[key] < 0 for key in ("created_at", "updated_at")):
        raise _Unavailable("Codex 自动任务时间格式不支持")
    if any(key in data and not isinstance(data[key], str) for key in ("model", "reasoning_effort")):
        raise _Unavailable("Codex 自动任务模型格式不支持")
    if "notification_policy" in data and data["notification_policy"] != "failed_runs_only":
        raise _Unavailable("Codex 自动任务通知格式不支持")


def _load(conn):
    result = {"supported": False, "detail": "", "roles": {
        role: _empty_role("尚未检查 Codex 自动任务") for role in _ROLES}}
    records = {}
    try:
        _check_app_version()
        home = _home()
        with _directory(home) as home_fd:
            auth_bytes, _ = _read(home_fd, "auth.json")
        auth = json.loads(auth_bytes)
        if not isinstance(auth, dict) or auth.get("auth_mode") != "apikey":
            raise _Unavailable("只支持 API Key 模式的 Codex 本地自动任务")
        with _directory(home / "automations"):
            pass
    except (OSError, ValueError, _Unavailable) as exc:
        detail = str(exc) if isinstance(exc, _Unavailable) else "无法安全读取 Codex 本地 API Key 自动任务配置"
        result["detail"] = detail
        result["roles"] = {role: _empty_role(detail) for role in _ROLES}
        return result, records

    result["supported"] = True
    cfg = desktop_queue.config(conn)
    ids = cfg.get("automation_ids")
    threads = {"plan": cfg.get("planner_thread_id"), "review": cfg.get("review_thread_id")}
    if (not isinstance(ids, list) or len(ids) != 2
            or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in ids)
            or len(set(ids)) != 2
            or any(not isinstance(value, str) or not value.strip() for value in threads.values())
            or threads["plan"] == threads["review"]):
        detail = "必须绑定两个不同的合法自动任务 ID 和对应的规划 / Review 聊天"
        result["detail"] = detail
        result["roles"] = {role: _empty_role(detail) for role in _ROLES}
        return result, records

    problems = []
    candidates = {role: [] for role in _ROLES}
    for automation_id in ids:
        path = home / "automations" / automation_id
        try:
            with _directory(path) as directory_fd:
                raw, info = _read(directory_fd, "automation.toml")
                directory_info = os.fstat(directory_fd)
            data = tomli.loads(raw.decode("utf-8"))
            _validate_record(data, automation_id)
            if set(_value_spans(raw.decode("utf-8"))) != set(data):
                raise _Unavailable("Codex 自动任务字段布局未知，无法安全修改")
            matches = [role for role in _ROLES if data["target_thread_id"] == threads[role]]
            if len(matches) != 1:
                raise _Unavailable("自动任务不属于已绑定的规划 / Review 聊天")
            role = matches[0]
            candidates[role].append({"path": path, "raw": raw, "info": info,
                                     "directory_identity": (directory_info.st_dev, directory_info.st_ino),
                                     "data": data})
        except FileNotFoundError:
            problems.append(_empty_role("绑定的 Codex 自动任务文件缺失", "MISSING", automation_id))
        except (OSError, ValueError, _Unavailable) as exc:
            detail = str(exc) if isinstance(exc, _Unavailable) else "无法安全读取绑定的 Codex 自动任务文件"
            problems.append(_empty_role(detail, automation_id=automation_id))

    unresolved = []
    for role in _ROLES:
        matches = candidates[role]
        if len(matches) == 1:
            record = matches[0]
            records[role] = record
            data = record["data"]
            result["roles"][role] = {"automation_id": data["id"], "name": data["name"],
                                     "status": data["status"], "enabled": data["status"] == "ACTIVE",
                                     "controllable": True, "detail": "已读取 Codex 本地自动任务实际状态",
                                     **_schedule_fields(data["rrule"])}
        elif len(matches) > 1:
            result["roles"][role] = _empty_role("多个绑定自动任务指向同一聊天，无法控制")
        else:
            unresolved.append(role)
    for role in unresolved:
        result["roles"][role] = (problems[0] if len(problems) == 1 else
                                  _empty_role("无法唯一识别该角色的绑定自动任务"))
    result["detail"] = ("已读取 Codex 本地兼容适配的自动任务；暂停不终止已开始轮次，恢复可能立即处理到期任务"
                        if len(records) == 2 else "部分自动任务绑定不可控制，请检查绑定与本地文件")
    return result, records


def cmd_desktop_automation_status(ctx, payload):
    desktop_queue._validate(payload)
    return _load(ctx.conn)[0]


def _value_spans(text):
    """Locate bare root assignments, skipping strings/comments/collection values.

    TOML parsing validates syntax separately. This scanner preserves all bytes
    outside the two scalar values, including prompt text resembling assignments.
    Tables and quoted keys are intentionally unsupported in this fixed format.
    """
    spans, pos, length = {}, 0, len(text)
    while pos < length:
        while pos < length and text[pos] in " \t\r\n":
            pos += 1
        if pos == length:
            break
        if text[pos] == "#":
            end = text.find("\n", pos)
            pos = length if end == -1 else end + 1
            continue
        match = re.match(r"([A-Za-z0-9_-]+)[ \t]*=[ \t]*", text[pos:])
        if not match:
            raise _Unavailable("Codex 自动任务字段布局未知，无法安全修改")
        key = match.group(1)
        start = pos + match.end()
        pos = start
        depth = 0
        while pos < length:
            char = text[pos]
            if char in "\"'":
                delimiter = char * 3 if text.startswith(char * 3, pos) else char
                pos += len(delimiter)
                while pos < length:
                    if char == '"' and text[pos] == "\\":
                        pos += 2
                    elif text.startswith(delimiter, pos):
                        pos += len(delimiter)
                        # A multiline delimiter may have one/two extra quote chars.
                        if len(delimiter) == 3:
                            for _ in range(2):
                                if pos < length and text[pos] == char:
                                    pos += 1
                        break
                    else:
                        pos += 1
                continue
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == "#":
                if depth == 0:
                    break
                end = text.find("\n", pos)
                pos = length if end == -1 else end + 1
                continue
            elif char in "\r\n" and depth == 0:
                break
            pos += 1
        end = pos
        while end > start and text[end - 1] in " \t":
            end -= 1
        if key in spans:
            raise _Unavailable("Codex 自动任务字段重复")
        spans[key] = (start, end)
        if pos < length and text[pos] == "#":
            newline = text.find("\n", pos)
            pos = length if newline == -1 else newline + 1
    return spans


def _replacement(record, desired):
    return _replacement_fields(record, {"status": desired})


def _replacement_fields(record, changes):
    text = record["raw"].decode("utf-8")
    spans = _value_spans(text)
    if not (set(changes) | {"updated_at"}) <= set(spans):
        raise _Unavailable("Codex 自动任务字段布局未知，无法安全修改")
    timestamp = max(int(time.time() * 1000), record["data"]["updated_at"] + 1)
    values = {key: json.dumps(value) for key, value in changes.items()}
    values["updated_at"] = str(timestamp)
    for key in sorted(values, key=lambda key: spans[key][0], reverse=True):
        start, end = spans[key]
        text = text[:start] + values[key] + text[end:]
    new_bytes = text.encode("utf-8")
    expected = {**record["data"], **changes, "updated_at": timestamp}
    if tomli.loads(text) != expected:
        raise _Unavailable("Codex 自动任务修改校验失败")
    return new_bytes


def _replace(record, new_bytes):
    temporary = ".kanpilot-" + secrets.token_hex(16) + ".tmp"
    with _directory(record["path"]) as directory_fd:
        directory_info = os.fstat(directory_fd)
        if (directory_info.st_dev, directory_info.st_ino) != record["directory_identity"]:
            raise util.KanpilotError("conflict", "Codex 自动任务目录发生变化，请刷新后重试")
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            created = True
            try:
                temporary_info = os.fstat(fd)
                if (temporary_info.st_uid, temporary_info.st_gid) != (record["info"].st_uid, record["info"].st_gid):
                    os.fchown(fd, record["info"].st_uid, record["info"].st_gid)
                os.fchmod(fd, stat.S_IMODE(record["info"].st_mode))
                view = memoryview(new_bytes)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise _Unavailable("写入 Codex 自动任务临时文件失败")
                    view = view[count:]
                os.fsync(fd)
                new_info = os.fstat(fd)
            finally:
                os.close(fd)
            before_bytes, before_info = _read(directory_fd, "automation.toml")
            if _identity(before_info) != _identity(record["info"]) or before_bytes != record["raw"]:
                raise util.KanpilotError("conflict", "Codex 自动任务发生变化，请刷新后重试")
            os.replace(temporary, "automation.toml", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            created = False
            after_bytes, after_info = _read(directory_fd, "automation.toml")
            if ((after_info.st_dev, after_info.st_ino, after_info.st_mode, after_info.st_uid, after_info.st_gid)
                    != (new_info.st_dev, new_info.st_ino, new_info.st_mode, new_info.st_uid, new_info.st_gid)
                    or after_bytes != new_bytes):
                raise _Unavailable("Codex 自动任务写入后发生变化，未确认成功，请刷新实际状态")
            os.fsync(directory_fd)
        finally:
            if created:
                os.unlink(temporary, dir_fd=directory_fd)


def cmd_set_desktop_automation(ctx, payload):
    desktop_queue._validate(payload, required=("role", "enabled", "expected_status"))
    role = payload["role"]
    if role not in _ROLES or type(payload["enabled"]) is not bool or payload["expected_status"] not in ("ACTIVE", "PAUSED"):
        raise util.bad_request("自动任务控制须提供规划 / Review、布尔开关和实际期望状态")
    desired = "ACTIVE" if payload["enabled"] else "PAUSED"
    try:
        # This transaction locks our project DB, never Codex's DB or task files.
        with db.write_txn(ctx.conn):
            state, records = _load(ctx.conn)
            if role not in records:
                raise _Unavailable(state["roles"][role]["detail"])
            record = records[role]
            if record["data"]["status"] != payload["expected_status"]:
                raise util.KanpilotError("conflict", "Codex 自动任务状态已变化，请刷新后重试")
            if record["data"]["status"] != desired:
                _replace(record, _replacement(record, desired))
            actual, _ = _load(ctx.conn)
            role_state = actual["roles"][role]
            if (not role_state["controllable"] or role_state["status"] != desired
                    or role_state["automation_id"] != record["data"]["id"]):
                raise _Unavailable("Codex 自动任务写入后未确认预期状态，请刷新实际状态")
            db.add_event(ctx.conn, ctx.now_iso, "desktop_automation_status_changed", actor="user",
                         detail={"automation_id": record["data"]["id"], "role": role,
                                 "previous_status": record["data"]["status"], "status": desired})
            return actual
    except (OSError, ValueError, sqlite3.Error, _Unavailable) as exc:
        detail = str(exc) if isinstance(exc, _Unavailable) else "无法安全修改 Codex 本地自动任务，请刷新实际状态"
        raise util.KanpilotError("desktop_automation_unavailable", detail) from None


def cmd_set_desktop_automation_schedule(ctx, payload):
    """Edit recurrence without enabling a paused task or changing its other inputs.

    Daily rules use Codex's machine-local timezone. The native scheduler has
    roughly +/-5 minute jitter; this adapter does not predict a next-run time.
    """
    desktop_queue._validate(payload, required=("role", "schedule", "expected_status", "expected_rrule"))
    role = payload["role"]
    if (role not in _ROLES or payload["expected_status"] not in ("ACTIVE", "PAUSED")
            or not isinstance(payload["expected_rrule"], str) or not payload["expected_rrule"]):
        raise util.bad_request("运行时间控制须提供规划 / Review、实际期望状态与原始周期")
    desired = _schedule_rrule(payload["schedule"])
    try:
        with db.write_txn(ctx.conn):
            state, records = _load(ctx.conn)
            if role not in records:
                raise _Unavailable(state["roles"][role]["detail"])
            record = records[role]
            original = record["data"]
            if (original["status"] != payload["expected_status"]
                    or original["rrule"] != payload["expected_rrule"]):
                raise util.KanpilotError("conflict", "Codex 自动任务状态或运行时间已变化，请刷新后重试")
            if not state["roles"][role]["schedule_editable"]:
                raise _Unavailable("当前 Codex 自动任务周期不支持编辑，已保留原始配置")
            expected = original
            if original["rrule"] != desired:
                replacement = _replacement_fields(record, {"rrule": desired})
                expected = tomli.loads(replacement.decode("utf-8"))
                _replace(record, replacement)
            actual, actual_records = _load(ctx.conn)
            if role not in actual_records or actual_records[role]["data"] != expected:
                raise _Unavailable("Codex 自动任务写入后未确认运行时间及原有状态，请刷新实际状态")
            db.add_event(ctx.conn, ctx.now_iso, "desktop_automation_schedule_changed", actor="user",
                         detail={"automation_id": original["id"], "role": role,
                                 "from_rrule": original["rrule"], "to_rrule": desired})
            return actual
    except (OSError, ValueError, sqlite3.Error, _Unavailable) as exc:
        detail = str(exc) if isinstance(exc, _Unavailable) else "无法安全修改 Codex 本地自动任务运行时间，请刷新实际状态"
        raise util.KanpilotError("desktop_automation_unavailable", detail) from None
