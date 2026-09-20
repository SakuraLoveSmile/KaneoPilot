"""Shared helpers: ISO-8601 UTC time, hashing, JSON, safe paths, frontmatter."""

from __future__ import annotations

import hashlib
import json
import re
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


def new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())
