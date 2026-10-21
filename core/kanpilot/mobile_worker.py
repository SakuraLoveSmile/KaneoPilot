"""One stdin request per process: execution-answer PID ownership stays intact."""
from __future__ import annotations
import json
import sys
from . import cli, util
from .mobile_access import require_existing_database

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise util.KanpilotError("too_large", "手机请求超过大小限制")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise util.bad_request("手机请求不是有效 JSON") from None
        path = cli.resolve_db_path(None)
        require_existing_database(path)
        result = cli.run("mobile-request", payload, path)
    except util.KanpilotError as exc:
        # Existing handlers may include filesystem paths in diagnostics. Stable
        # error codes cross this boundary; local diagnostics never do.
        messages = {"bad_request": "请求字段无效", "unauthorized": "设备或配对授权已失效",
                    "forbidden": "当前不允许此操作", "not_found": "请求的内容不存在",
                    "conflict": "内容或状态已变化，请刷新后核对", "too_large": "内容超过大小限制"}
        code = exc.code if exc.code in messages else "unavailable"
        result = {"error": messages.get(code, "服务暂不可用，请在 Mac 检查"), "code": code}
    except Exception:
        result = {"error": "服务暂不可用，请在 Mac 检查", "code": "unavailable"}
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE:
        encoded = b'{"error":"response too large","code":"too_large"}'
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
