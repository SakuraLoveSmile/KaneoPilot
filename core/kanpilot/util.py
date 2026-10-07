"""Shared helpers: ISO-8601 UTC time, hashing, JSON, safe paths, frontmatter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Vendored PyYAML (pure-Python safe loader); never pip-installed.
_VENDOR = Path(__file__).resolve().parent.parent / "_vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
import yaml  # noqa: E402

TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class KanpilotError(Exception):
    """Command failure carrying a stable protocol error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def bad_request(message: str) -> KanpilotError:
    return KanpilotError("bad_request", message)


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_dt().isoformat()


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def plus_seconds(seconds: float, base: Optional[datetime] = None) -> str:
    return to_iso((base or now_dt()) + timedelta(seconds=seconds))


def parse_ts(value: Any) -> Optional[datetime]:
    """Lenient ISO-8601 parse; returns None for missing/unparseable input."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ts_before(a: Any, b: Any) -> bool:
    """True when timestamp a is earlier than b (None-safe: None never <)."""
    pa, pb = parse_ts(a), parse_ts(b)
    if pa is None or pb is None:
        return False
    return pa < pb


def ts_due(not_before: Any, now: datetime) -> bool:
    """A request is claimable when not_before is empty or already past."""
    parsed = parse_ts(not_before)
    return parsed is None or parsed <= now


def lease_lost(lease_expires_at: Any, now: datetime) -> bool:
    parsed = parse_ts(lease_expires_at)
    return parsed is not None and parsed < now


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def process_state(pid: Any) -> Optional[str]:
    """``ps -o stat=`` 的进程状态首字段（R/S/T/Z…）。

    进程不存在、ps 不可用或超时 → None（与「存在但状态未知」区分不了时，
    调用方按存活处理，见 :func:`pid_alive`）。
    """
    ps = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.run(
            [ps, "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (out.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else None


def pid_alive(pid: Any) -> bool:
    """本机进程存活探测（``os.kill(pid, 0)`` + 排除僵尸进程）。

    只用于判断**本机观察进程**是否仍在（是否可以续接），**绝不**作为轮次
    终态依据（protocol §5.18.4 完成判定只认三证据）。``kill 0`` 对未被
    ``wait`` 回收的僵尸进程同样成功，因此存活结论再经 ``ps`` 状态复核：
    查询成功但无该进程、或状态为 Z（僵尸）→ 已退出；``ps`` 不可用/超时 →
    保守视为存活（宁可不重复拉起观察者，也不并发两个写入者）。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OverflowError:
        # A malformed stored value outside pid_t cannot identify a local process.
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return _ps_alive(pid)


def _ps_alive(pid: int) -> bool:
    ps = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.run(
            [ps, "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True                     # 查不到结论 → 保守视为存活
    lines = (out.stdout or "").strip().splitlines()
    if not lines:
        return False                    # ps 成功但无该 PID → 已退出
    return lines[0].strip()[:1].upper() != "Z"


def jload(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def inside(path: Path, root: Path, what: str = "path") -> Path:
    """Resolve path, require it strictly inside root, reject symlinks
    within the allowed tree (a symlinked ancestor of root itself is fine —
    e.g. /var -> /private/var on macOS tempdirs)."""
    path = Path(path).expanduser()
    root = Path(root).expanduser()
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise KanpilotError("validation_failed", f"{what} outside allowed root: {path}")
    probe = root
    for part in resolved.relative_to(resolved_root).parts:
        probe = probe / part
        if probe.is_symlink():
            raise KanpilotError(
                "validation_failed", f"{what} uses a symlink: {probe}"
            )
    return path


def read_frontmatter(path: Path) -> tuple[dict, bytes]:
    """Parse `---\n<yaml>\n---` frontmatter; returns (meta, raw_bytes)."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    if not text.startswith("---\n"):
        raise KanpilotError("validation_failed", f"plan missing frontmatter: {path}")
    parts = text.split("---", 2)
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise KanpilotError("validation_failed", f"plan frontmatter invalid: {exc}") from exc
    if not isinstance(meta, dict):
        raise KanpilotError("validation_failed", "plan frontmatter is not a mapping")
    return meta, raw

def require(condition: Any, message: str, code: str = "validation_failed") -> Any:
    if not condition:
        raise KanpilotError(code, message)
    return condition


# 认证值形态的脱敏模式（日志/事件落盘前调用，保留状态码与错误正文）。
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|bearer[_-]?token|"
               r"refresh[_-]?token|secret)(\s*[=:]\s*)[\"']?([^\s\"']{8,})"),
)


def redact(text: Any, secrets: Any = None) -> Any:
    """凭据脱敏（protocol §5.22.3）：返回脱敏后的字符串；非 str 原样返回。

    - ``secrets``：已知凭据值列表（如 provider.env 的值），长度 ≥8 才替换，
      避免过短值误伤普通文本；替换为 ``«redacted»``。
    - 同时对常见认证形态（Authorization: Bearer、api_key=/token= 等）的值
      脱敏。HTTP 状态码、模型名、错误原因不受影响。
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for secret in secrets or ():
        if isinstance(secret, str) and len(secret) >= 8:
            out = out.replace(secret, "«redacted»")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda m: m.group(1)
            + (m.group(2) if (m.lastindex or 0) >= 3 else "")
            + "«redacted»",
            out,
        )
    return out


def new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def monitor_interval(conn) -> int:
    """Read the effective interval without rewriting legacy seeded settings."""
    from . import constants, db
    interval = db.get_setting(conn, "monitor_interval_seconds", constants.DEFAULT_MONITOR_INTERVAL_SECONDS)
    return 120 if interval == 900 else interval
