"""Claude stream-json adapter shared by read-only planning and Review."""
from __future__ import annotations

import json
import math
import os
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import constants, util

READONLY_TOOLS = frozenset(("Read", "Grep", "Glob"))
_MODEL_ENV_KEYS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                   "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                   "ANTHROPIC_SMALL_FAST_MODEL")
_USER_ENV_KEYS = frozenset(("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                           "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL")
                          + _MODEL_ENV_KEYS + tuple(key + "_NAME" for key in _MODEL_ENV_KEYS))


def build_argv(*, model, effort, schema: dict, budget_usd,
               add_dirs=(), tools=("Read", "Grep", "Glob")) -> list:
    if not set(tools).issubset(READONLY_TOOLS):
        raise util.bad_request("Claude 工具集不符合只读要求")
    budget = float(budget_usd)
    if not math.isfinite(budget) or budget <= 0:
        raise util.bad_request("Claude 预算必须是正数")
    argv = [constants.claude_bin(), "-p", "--output-format", "stream-json",
            "--verbose", "--restricted", "--tools", ",".join(tools),
            "--strict-mcp-config", "--permission-mode", "dontAsk",
            "--no-session-persistence", "--json-schema",
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "--model", str(model), "--effort", str(effort),
            "--max-budget-usd", str(budget)]
    for directory in add_dirs:
        argv += ["--add-dir", str(directory)]
    return argv


def child_env() -> dict:
    """Carry only global auth/routes/model aliases into restricted CLI memory."""
    env = dict(os.environ)
    configured_dir = env.get("CLAUDE_CONFIG_DIR", "")
    try:
        if configured_dir.strip():
            directory = Path(configured_dir).expanduser()
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
            directory = Path.home() / ".claude"
    except RuntimeError:
        raise util.KanpilotError("validation_failed", "Claude CLAUDE_CONFIG_DIR 无法解析为绝对路径") from None
    if not directory.is_absolute():
        raise util.KanpilotError("validation_failed", "Claude CLAUDE_CONFIG_DIR 必须是绝对路径")
    try:
        settings = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError):
        # Never include JSON fragments or secret values in diagnostic messages.
        raise util.KanpilotError("validation_failed", "Claude 用户全局 settings.json 不可读取或不是合法 JSON") from None
    if not isinstance(settings, dict):
        raise util.KanpilotError("validation_failed", "Claude 用户全局 settings.json 必须是对象")
    configured = settings.get("env", {})
    if not isinstance(configured, dict):
        raise util.KanpilotError("validation_failed", "Claude 用户全局 settings.json.env 必须是对象")
    for key in _USER_ENV_KEYS:
        if key in configured and key not in env:
            value = configured[key]
            if not isinstance(value, str):
                raise util.KanpilotError("validation_failed", f"Claude 用户全局 env.{key} 必须是字符串")
            env[key] = value
    env.update(NO_COLOR="1", TERM="dumb")
    return env


def map_event(event: dict) -> Optional[dict]:
    ts = event.get("timestamp") or util.now_iso()
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return {"type": "session", "ts": ts,
                "session_id": event.get("session_id"),
                "session_title": "Claude · 只读"}
    if kind == "assistant":
        blocks = (event.get("message") or {}).get("content") or []
        content = "\n".join(b.get("text", "") for b in blocks
                            if isinstance(b, dict) and b.get("type") == "text")
        if content:
            return {"type": "assistant_message", "ts": ts, "content": content}
    if kind == "result":
        return {"type": "turn_end", "ts": ts, "will_retry": False}
    return None


def _terminate(proc) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)


def run(argv, *, prompt, cwd, env, timeout, log_path, on_event=None) -> dict:
    """Run once; callbacks receive raw events and None idle pulses for leases.

    Callback exceptions stop the child before propagating (e.g. stale_run).
    Logs retain non-JSON lines, with environment credentials redacted.
    """
    result = {"exit_code": None, "is_error": True, "subtype": None,
              "structured_output": None, "total_cost_usd": None,
              "session_id": None, "tools": [], "error": None}
    secrets = [v for k, v in env.items() if isinstance(v, str)
               and (k == "ANTHROPIC_BASE_URL"
                    or any(word in k.upper() for word in ("TOKEN", "KEY", "SECRET", "PASSWORD")))]
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = None
    seen_init = False
    seen_result = False
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    # Claude 2.1.251 exposes its schema output tool in init even though it is
    # not part of --tools. It neither reads nor writes repository files.
    init_tools = READONLY_TOOLS | ({"StructuredOutput"} if "--json-schema" in argv else set())

    def consume(raw, log):
        nonlocal seen_init, seen_result
        line = util.redact(raw.decode("utf-8", errors="replace"), secrets).strip()
        log.write(line + "\n")
        log.flush()
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "system" and event.get("subtype") == "init":
            tool_list = event.get("tools")
            if (not isinstance(tool_list, list)
                    or any(not isinstance(t, str) or t not in init_tools
                           for t in tool_list)):
                result["error"] = "Claude 工具集不符合只读要求"
                _terminate(proc)
                return
            seen_init = True
            result["tools"] = tool_list
            result["session_id"] = event.get("session_id")
        if event.get("type") == "result":
            seen_result = True
            for key in ("subtype", "structured_output", "total_cost_usd", "session_id"):
                result[key] = event.get(key)
            result["is_error"] = bool(event.get("is_error")) or event.get("subtype") != "success"
            if result["is_error"]:
                result["error"] = str(event.get("errors") or event.get("result")
                                      or event.get("subtype") or "Claude 运行失败")
            elif not isinstance(result["structured_output"], dict):
                result["is_error"] = True
                result["error"] = "Claude result 缺少 structured_output"
            cost = result["total_cost_usd"]
            if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                                     or not math.isfinite(cost) or cost < 0):
                result["total_cost_usd"] = None
                result["is_error"] = True
                result["error"] = "Claude 费用字段不合法"
        if on_event is not None:
            on_event(event)

    try:
        with log_path.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, cwd=str(cwd), env=env,
                                    start_new_session=True, bufsize=0)
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            fd = proc.stdout.fileno()
            while not result["error"]:
                if time.monotonic() >= deadline:
                    result["error"] = "Claude 运行超时"
                    result["subtype"] = "timeout"
                    break
                if on_event is not None:
                    on_event(None)
                ready, _, _ = select.select([fd], [], [], 0.25)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n")
                    consume(raw, log)
                    if result["error"]:
                        break
            if buffer and not result["error"]:
                consume(buffer, log)
            if result["error"]:
                _terminate(proc)
            else:
                try:
                    proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    result["error"] = "Claude 运行超时"
                    result["subtype"] = "timeout"
                    _terminate(proc)
            result["exit_code"] = proc.returncode
    except OSError as exc:
        result["error"] = util.redact(f"Claude 拉起失败：{exc}", secrets)
    finally:
        if proc is not None:
            _terminate(proc)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
    if not result["error"] and not seen_init:
        result["error"] = "Claude 缺少只读工具 init 断言"
    if not result["error"] and not seen_result:
        result["error"] = "Claude 缺少 result/structured_output"
    if not result["error"] and result["exit_code"] != 0:
        result["error"] = f"Claude 退出码 {result['exit_code']}"
    result["is_error"] = bool(result["error"]) or result["is_error"]
    return result
