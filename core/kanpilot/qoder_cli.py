"""Qoder stream-json adapter for read-only planning and Review."""
from __future__ import annotations

import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from . import constants, util

READONLY_TOOLS = frozenset(("Read", "Grep", "Glob"))
_PROVIDER_API_KEY_ENV = "KANPILOT_QODER_PROVIDER_API_KEY"
_ENV_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
_REQUIRED_FLAGS = (
    "--print", "--output-format", "--tools", "--permission-mode",
    "--strict-mcp-config", "--setting-sources", "--settings",
    "--no-session-persistence", "--model", "--reasoning-effort",
    "--add-dir", "--max-model-request-retries",
)


def build_argv(*, model, reasoning_effort="inherit", add_dirs=(), _providers=None) -> list:
    settings = {"disableAllHooks": True, "general": {"enableAutoUpdate": False},
                "skills": {"enabled": False, "loadFromAgentsDirectory": False}}
    if _providers is not None:
        settings["providers"] = _providers
    argv = [constants.qoder_bin(), "--print", "--output-format", "stream-json",
            "--tools", "Read,Grep,Glob", "--permission-mode", "dont_ask",
            "--strict-mcp-config", "--setting-sources", "", "--settings",
            json.dumps(settings, separators=(",", ":")),
            "--no-session-persistence", "--max-model-request-retries", "0",
            "--model", str(model)]
    if reasoning_effort != "inherit":
        argv += ["--reasoning-effort", str(reasoning_effort)]
    for directory in add_dirs:
        argv += ["--add-dir", str(directory)]
    return argv


def child_env() -> dict:
    # Authentication stays in child memory; settings/hooks are excluded by argv.
    env = dict(os.environ)
    env.update(NO_COLOR="1", TERM="dumb")
    return env


def _provider_error():
    return util.KanpilotError("validation_failed", "Qoder 自定义模型设置或认证无效，请检查用户设置中的完整 provider/型号与认证")


def _settings_path(env):
    """Resolve non-China Qoder user settings without reading project settings."""
    if ("QODERCN_CONFIG_DIR_NAME" in env
            or any(name in env and not env[name] for name in
                   ("QODER_CONFIG_DIR", "QODER_CLI_HOME", "GEMINI_CLI_HOME", "QODER_CONFIG_DIR_NAME"))):
        raise _provider_error()
    if env.get("QODER_CONFIG_DIR"):
        directory = Path(env["QODER_CONFIG_DIR"]).expanduser()
    else:
        base = Path(env.get("QODER_CLI_HOME") or env.get("GEMINI_CLI_HOME") or Path.home()).expanduser()
        name = env.get("QODER_CONFIG_DIR_NAME") or ".qoder"
        if not base.is_absolute() or Path(name).is_absolute() or "/" in name or "\\" in name or name in (".", ".."):
            raise _provider_error()
        directory = base / name
    if not directory.is_absolute():
        raise _provider_error()
    return directory / "settings.json"


def _api_key(value, env):
    if not isinstance(value, str) or not value.strip():
        raise _provider_error()

    def replace(match):
        name, fallback, short_name = match.groups()
        resolved = env.get(name or short_name)
        if not resolved and fallback is not None:
            resolved = fallback
        if not isinstance(resolved, str) or not resolved:
            raise _provider_error()
        return resolved

    # One substitution pass, never eval or recursively resolve environment data.
    resolved = _ENV_REFERENCE.sub(replace, value)
    if not resolved.strip() or "\x00" in resolved or re.search(r"\$(?:[A-Za-z_]|\{)", resolved):
        raise _provider_error()
    return resolved


def _contains_auth(value, actual, source):
    if isinstance(value, dict):
        return any(_contains_auth(key, actual, source) or _contains_auth(item, actual, source)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_auth(item, actual, source) for item in value)
    return isinstance(value, str) and (actual in value or source in value)


def prepare_launch(*, model, reasoning_effort="inherit", add_dirs=()):
    """Project one explicitly selected provider; keep its credential in env only."""
    env = child_env()
    if not isinstance(model, str) or not model or model.startswith("-") or any(ord(c) < 32 for c in model):
        raise _provider_error()
    if "/" not in model:
        return build_argv(model=model, reasoning_effort=reasoning_effort, add_dirs=add_dirs), env
    provider_id, upstream = model.split("/", 1)
    if not provider_id or not upstream or not upstream.strip():
        raise _provider_error()
    try:
        settings = json.loads(_settings_path(env).read_text(encoding="utf-8"), parse_constant=_invalid_constant)
    except (OSError, ValueError, RuntimeError):
        raise _provider_error() from None
    providers = settings.get("providers") if isinstance(settings, dict) else None
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict) or not isinstance(provider.get("models"), list):
        raise _provider_error()
    models = [entry for entry in provider["models"]
              if isinstance(entry, dict) and entry.get("model") == upstream]
    if len(models) != 1:
        raise _provider_error()
    if (provider.get("type") != "openai-compatible" or provider.get("protocol") != "openai"
            or provider.get("authType") != "bearer"):
        raise _provider_error()
    base_url = provider.get("baseUrl")
    if not isinstance(base_url, str) or any(char.isspace() or ord(char) < 32 for char in base_url):
        raise _provider_error()
    try:
        url = urlsplit(base_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or "?" in base_url or "#" in base_url):
            raise _provider_error()
    except ValueError:
        raise _provider_error() from None
    actual_key = _api_key(provider.get("apiKey"), env)
    selected = {}
    for key in ("model", "displayName", "contextWindow", "maxOutputTokens", "maxTokensField"):
        if key in models[0]:
            value = models[0][key]
            if key in ("contextWindow", "maxOutputTokens"):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise _provider_error()
            elif not isinstance(value, str):
                raise _provider_error()
            selected[key] = value
    if "capabilities" in models[0]:
        capabilities = models[0]["capabilities"]
        if not isinstance(capabilities, dict):
            raise _provider_error()
        selected["capabilities"] = {key: capabilities[key] for key in
                                    ("tools", "vision", "cacheControl", "thinking") if key in capabilities}
        for key, value in selected["capabilities"].items():
            if not isinstance(value, dict if key == "thinking" else bool):
                raise _provider_error()
    projected = {key: provider[key] for key in ("type", "protocol", "authType", "baseUrl")}
    projected.update(model=upstream, models=[selected])
    # Refuse auth mistakenly repeated in metadata rather than changing a route,
    # URL, display name or other field to a redacted, nonfunctional value.
    if _contains_auth({provider_id: projected}, actual_key, provider["apiKey"]):
        raise _provider_error()
    projected["apiKey"] = "${" + _PROVIDER_API_KEY_ENV + "}"
    env[_PROVIDER_API_KEY_ENV] = actual_key
    argv = build_argv(model=model, reasoning_effort=reasoning_effort, add_dirs=add_dirs,
                      _providers={provider_id: projected})
    return argv, env


def map_event(event: dict) -> Optional[dict]:
    ts = event.get("timestamp") or util.now_iso()
    if event.get("type") == "system" and event.get("subtype") == "init":
        return {"type": "session", "ts": ts,
                "session_id": event.get("session_id"), "session_title": "Qoder · 只读"}
    if event.get("type") == "assistant":
        message = event.get("message")
        blocks = message.get("content", []) if isinstance(message, dict) else []
        content = "\n".join(block["text"] for block in blocks
                            if isinstance(block, dict) and block.get("type") == "text"
                            and isinstance(block.get("text"), str)) if isinstance(blocks, list) else ""
        if content:
            return {"type": "assistant_message", "ts": ts, "content": content}
    if event.get("type") == "result":
        return {"type": "turn_end", "ts": ts, "will_retry": False}
    return None


def capabilities(timeout=10) -> dict:
    """Probe only the installed CLI, without starting a model session."""
    candidate = constants.qoder_bin()
    path = shutil.which(candidate)
    result = {"available": False, "path": path, "version": None, "detail": None}
    if path is None:
        result["detail"] = "未找到 Qoder CLI；请安装 CLI 或设置 KANPILOT_QODER_BIN"
        return result
    secrets = _secrets(os.environ)
    try:
        version = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=timeout, env=child_env())
        if version.returncode != 0:
            result["detail"] = "Qoder CLI --version 检查失败"
            return result
        result["version"] = _redact_text(version.stdout.strip(), secrets)
        parsed_version = re.search(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.])", version.stdout)
        if parsed_version is None or tuple(map(int, parsed_version.groups())) < (1, 1, 74):
            result["detail"] = "Qoder CLI 版本无法解析或低于本适配器支持基线 1.1.74"
            return result
        help_result = subprocess.run([path, "--help"], capture_output=True, text=True,
                                     timeout=timeout, env=child_env())
        if help_result.returncode != 0:
            result["detail"] = "Qoder CLI --help 检查失败"
            return result
        help_text = help_result.stdout + "\n" + help_result.stderr
        missing = [flag for flag in _REQUIRED_FLAGS
                   if not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text)]
        if missing:
            result["detail"] = "Qoder CLI 缺少所需参数：" + ", ".join(missing)
            return result
    except (OSError, subprocess.TimeoutExpired):
        result["detail"] = "Qoder CLI 本地能力检查失败或超时"
        return result
    result.update(available=True, detail="Qoder CLI 本地参数检查通过；尚未调用模型")
    return result


def _catalog_error(detail):
    return util.KanpilotError("validation_failed", "Qoder 模型列表读取失败：" + detail)


def _catalog_identifier(value):
    return (isinstance(value, str) and bool(value.strip()) and not value.startswith("-")
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _custom_catalog(env):
    """Project names only; do not resolve authentication or copy provider metadata."""
    try:
        settings = json.loads(_settings_path(env).read_text(encoding="utf-8"),
                              parse_constant=_invalid_constant)
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RuntimeError, util.KanpilotError):
        raise _catalog_error("用户设置无效或无法读取，请检查 Qoder settings.json") from None
    if not isinstance(settings, dict):
        raise _catalog_error("用户设置结构无效")
    providers = settings.get("providers", {})
    if not isinstance(providers, dict):
        raise _catalog_error("用户设置的 providers 结构无效")
    models = []
    seen = set()
    for provider_id, provider in providers.items():
        if (not _catalog_identifier(provider_id) or "/" in provider_id
                or not isinstance(provider, dict)):
            raise _catalog_error("用户设置的自定义模型结构无效")
        if "models" not in provider:
            continue
        if not isinstance(provider["models"], list):
            raise _catalog_error("用户设置的自定义模型结构无效")
        for entry in provider["models"]:
            if not isinstance(entry, dict) or not _catalog_identifier(entry.get("model")):
                raise _catalog_error("用户设置的自定义模型标识无效")
            upstream = entry["model"]
            name = entry.get("displayName", upstream)
            if (not isinstance(name, str) or not name.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                raise _catalog_error("用户设置的自定义模型名称无效")
            model_id = provider_id + "/" + upstream
            if model_id in seen:
                raise _catalog_error("用户设置含重复的自定义模型标识")
            seen.add(model_id)
            models.append({"id": model_id, "name": name, "billing_type": "external",
                           "credit_multiplier": None, "original_credit_multiplier": None})
    return models


_CATALOG_OUTPUT_LIMIT = 2 * 1024 * 1024


def _catalog_stop(proc):
    # Signal the group even after its leader exits: descendants may own pipe fds.
    def signal_group(sig):
        deadline = time.monotonic() + .5
        while True:
            try:
                _signal_group(proc, sig)
                return
            except PermissionError:
                # XNU may retain a just-terminated orphan as a zombie briefly.
                # Recheck within a bound; a persistent permission error fails.
                if proc.poll() is None or time.monotonic() >= deadline:
                    raise
                time.sleep(.025)

    signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    signal_group(signal.SIGKILL)
    proc.wait(timeout=2)


def _catalog_control_models(argv, env, timeout):
    """Use two control frames only; never send a user message or inference prompt."""
    proc = None
    deadline = time.monotonic() + timeout
    expected_id = "catalog-init"
    models = None
    completed = False
    buffer = bytearray()
    output_size = 0

    def send(request_id, request):
        frame = {"type": "control_request", "request_id": request_id, "request": request}
        proc.stdin.write((json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def consume(raw):
        nonlocal expected_id, models, deadline
        if not raw.strip():
            return
        try:
            frame = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise _catalog_error("CLI 控制协议格式无效") from None
        if not isinstance(frame, dict):
            raise _catalog_error("CLI 控制协议格式无效")
        if frame.get("type") == "system" and frame.get("subtype") in ("init", "status", "artifacts_update"):
            return
        response = frame.get("response")
        if (frame.get("type") != "control_response" or not isinstance(response, dict)
                or response.get("request_id") != expected_id or expected_id is None):
            raise _catalog_error("CLI 控制协议响应身份不匹配")
        if response.get("subtype") != "success":
            raise _catalog_error("CLI 目录查询失败，请检查 Qoder CLI 的登录状态与网络")
        if expected_id == "catalog-init":
            expected_id = "catalog-models"
            send(expected_id, {"subtype": "get_models", "fetchStrategy": "live"})
        else:
            payload = response.get("response")
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise _catalog_error("CLI 模型目录结构无效")
            models = payload["models"]
            expected_id = None
            proc.stdin.close()
            deadline = min(deadline, time.monotonic() + 5)

    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=tempfile.gettempdir(),
                                start_new_session=True, bufsize=0)
        send(expected_id, {"subtype": "initialize"})
        stdout_fd = proc.stdout.fileno()
        readers = [stdout_fd, proc.stderr.fileno()]
        while readers or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _catalog_error("CLI 查询或退出超时，请稍后重新获取")
            ready, _, _ = select.select(readers, [], [], min(.1, remaining))
            for fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    readers.remove(fd)
                    if fd == stdout_fd and buffer:
                        consume(bytes(buffer))
                        buffer.clear()
                    continue
                output_size += len(chunk)
                if output_size > _CATALOG_OUTPUT_LIMIT:
                    raise _catalog_error("CLI 控制协议输出超出限制")
                if fd == stdout_fd:
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        raw, _, rest = buffer.partition(b"\n")
                        buffer[:] = rest
                        consume(raw)
        if proc.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise _catalog_error("CLI 目录查询失败，请检查 Qoder CLI 的登录状态与网络")
        if models is None:
            raise _catalog_error("CLI 控制协议缺少完整目录响应")
        completed = True
        return models
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise _catalog_error("CLI 无法执行或控制协议中断") from None
    finally:
        if proc is not None:
            try:
                # On errors this also stops descendants after an exited leader.
                if not completed:
                    _catalog_stop(proc)
            except (OSError, subprocess.TimeoutExpired):
                raise _catalog_error("CLI 查询进程清理失败") from None
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()


def _credit_multiplier(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def model_catalog(timeout=30) -> dict:
    """Read the live CLI catalog and user custom names without an inference turn."""
    path = shutil.which(constants.qoder_bin())
    if path is None:
        raise _catalog_error("未找到 Qoder CLI；请安装 CLI 或设置 KANPILOT_QODER_BIN")
    env = child_env()
    custom = _custom_catalog(env)
    settings = {"general": {"enableAutoUpdate": False}, "disableAllHooks": True,
                "skills": {"enabled": False, "loadFromAgentsDirectory": False}}
    argv = [path, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
            "--no-session-persistence", "--tools", "", "--permission-mode", "dont_ask",
            "--strict-mcp-config", "--setting-sources", "", "--settings",
            json.dumps(settings, separators=(",", ":")), "--max-model-request-retries", "0"]
    builtins = _catalog_control_models(argv, env, timeout)
    models = []
    seen = set()
    for entry in builtins:
        if not isinstance(entry, dict) or entry.get("source") != "system":
            raise _catalog_error("CLI 模型来源未知，无法核对 Credits 计费")
        name = entry.get("displayName")
        if not _catalog_identifier(name) or name in seen:
            raise _catalog_error("CLI 模型目录标识无效或重复")
        seen.add(name)
        current = _credit_multiplier(entry.get("priceFactor"))
        promotion = entry.get("promotion")
        original = _credit_multiplier(
            promotion.get("before_promotion_price_factor")
            if isinstance(promotion, dict) and promotion.get("active") is True
            else entry.get("originalPriceFactor"))
        if current is None or original is None or original <= current:
            original = None
        models.append({"id": name, "name": name, "billing_type": "qoder_credits",
                       "credit_multiplier": current, "original_credit_multiplier": original})
    for model in custom:
        if model["id"] in seen:
            raise _catalog_error("内置与自定义模型标识冲突")
        seen.add(model["id"])
        models.append(model)
    return {"models": models, "multiplier_source": "qoder_cli_live"}


def _secrets(env):
    return [value for key, value in env.items() if isinstance(value, str)
            and any(word in key.upper() for word in ("TOKEN", "KEY", "SECRET", "PASSWORD", "BASE_URL"))]


def _redact(value, secrets):
    if isinstance(value, dict):
        return {_redact_text(key, secrets): _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return _redact_text(value, secrets)


def _redact_text(value, secrets):
    value = util.redact(value, secrets)
    if isinstance(value, str):
        # util.redact intentionally ignores short values in general logs; the
        # child environment can still contain a short, explicit credential.
        for secret in secrets:
            if secret and len(secret) < 8:
                value = value.replace(secret, "«redacted»")
    return value


def _cost(model_usage):
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    total = 0.0
    for usage in model_usage.values():
        value = usage.get("costUSD") if isinstance(usage, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except OverflowError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        total += value
    return total if math.isfinite(total) else None


def _invalid_constant(value):
    raise ValueError("invalid JSON constant")


def _signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        # XNU can return EPERM for a group containing only an unreaped zombie.
        # Reap within a bound, then signal again: an exited leader alone says
        # nothing about descendants, and persistent EPERM must remain visible.
        try:
            proc.wait(timeout=.25)
        except subprocess.TimeoutExpired:
            raise exc from None
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return


def _terminate(proc):
    if proc.poll() is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            proc.wait(timeout=5)


def run(argv, *, prompt, schema, cwd, env, timeout, log_path, on_event=None) -> dict:
    """Run once; raw stream frames are parsed before redacting persisted output.

    Callback exceptions stop and reap the process group, including on idle pulses.
    Schema enforcement beyond a JSON object remains with the existing caller.
    """
    result = {"exit_code": None, "is_error": True, "subtype": None,
              "structured_output": None, "total_cost_usd": None,
              "session_id": None, "tools": [], "error": None}
    secrets = _secrets(env)
    prompt = (str(prompt) + "\n\n最终结果必须严格符合以下 JSON Schema，且仅输出一个 JSON 对象。"
              "不要使用 Markdown 代码围栏、前言或后记。\n"
              + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    pending = memoryview(prompt.encode("utf-8"))
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = None
    seen_init = False
    seen_result = False
    deadline = time.monotonic() + timeout
    buffer = bytearray()

    def consume(raw, log):
        nonlocal seen_init, seen_result
        line = raw.decode("utf-8", errors="replace").strip()
        try:
            event = json.loads(line, parse_constant=_invalid_constant)
        except ValueError:
            log.write(_redact_text(line, secrets) + "\n")
            log.flush()
            return
        safe_event = _redact(event, secrets)
        log.write(json.dumps(safe_event, ensure_ascii=False) + "\n")
        log.flush()
        if not isinstance(event, dict):
            return
        if event.get("type") == "system" and event.get("subtype") == "init":
            tools = event.get("tools")
            if (seen_init or not isinstance(tools, list)
                    or any(not isinstance(tool, str) or tool not in READONLY_TOOLS for tool in tools)):
                result["error"] = "Qoder 工具集不符合只读要求或 init 重复"
                return
            if any(event.get(key) for key in ("mcp_servers", "plugins", "skills", "hooks")):
                result["error"] = "Qoder init 包含未禁用的 MCP、插件、技能或 hooks"
                return
            if event.get("permissionMode", "dont_ask") not in ("dont_ask", "dontAsk"):
                result["error"] = "Qoder init 权限模式不符合只读要求"
                return
            seen_init = True
            result["tools"] = tools
            result["session_id"] = safe_event.get("session_id")
        if event.get("type") == "result":
            if not seen_init or seen_result:
                result["error"] = "Qoder result 缺少只读 init 或 result 重复"
                return
            seen_result = True
            result["subtype"] = event.get("subtype")
            result["session_id"] = safe_event.get("session_id")
            result["total_cost_usd"] = _cost(event.get("modelUsage"))
            result["is_error"] = (event.get("is_error") is not False
                                  or event.get("subtype") != "success")
            if result["is_error"]:
                result["error"] = _redact_text(str(event.get("errors") or event.get("result")
                                                    or event.get("subtype") or "Qoder 运行失败"), secrets)
            else:
                try:
                    output = json.loads(event["result"], parse_constant=_invalid_constant)
                    if not isinstance(output, dict):
                        raise ValueError("not an object")
                except (KeyError, TypeError, ValueError):
                    result["error"] = "Qoder result 不是严格 JSON 对象（不支持 Markdown 代码围栏）"
                else:
                    result["structured_output"] = _redact(output, secrets)
        if on_event is not None:
            on_event(safe_event)

    try:
        with log_path.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, cwd=str(cwd), env=env,
                                    start_new_session=True, bufsize=0)
            os.set_blocking(proc.stdin.fileno(), False)
            stdout_fd = proc.stdout.fileno()
            eof = False
            while not result["error"]:
                if time.monotonic() >= deadline:
                    result.update(error="Qoder 运行超时", subtype="timeout")
                    break
                if on_event is not None:
                    on_event(None)
                if eof and proc.poll() is not None:
                    break
                writable = [proc.stdin.fileno()] if pending and not proc.stdin.closed else []
                ready, write_ready, _ = select.select([] if eof else [stdout_fd], writable, [],
                                                       min(0.25, max(0, deadline - time.monotonic())))
                if write_ready:
                    try:
                        pending = pending[os.write(write_ready[0], pending[:65536]):]
                    except (BrokenPipeError, OSError):
                        pending = memoryview(b"")
                    if not pending:
                        proc.stdin.close()
                if not ready:
                    continue
                chunk = os.read(stdout_fd, 65536)
                if not chunk:
                    eof = True
                    continue
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
            result["exit_code"] = proc.poll()
    except OSError as exc:
        result["error"] = _redact_text(f"Qoder 拉起失败：{exc}", secrets)
    finally:
        if proc is not None:
            try:
                _terminate(proc)
                # An exited leader does not prove descendants have stopped.
                _signal_group(proc, signal.SIGKILL)
            finally:
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None:
                        stream.close()
    if not result["error"] and not seen_init:
        result["error"] = "Qoder 缺少只读工具 init 断言"
    if not result["error"] and not seen_result:
        result["error"] = "Qoder 缺少 result JSON 对象"
    if not result["error"] and result["exit_code"] != 0:
        result["error"] = f"Qoder 退出码 {result['exit_code']}"
    result["is_error"] = bool(result["error"]) or result["is_error"]
    return result
