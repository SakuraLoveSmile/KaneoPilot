"""Frozen Flutter tool environment; never runs a task's verification commands."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import constants, util

PROFILE = "kanpilot_flutter"
CHECKS = ("config_write", "temp_write", "ipv4", "ipv6", "direct_denied", "proxy_active", "proxy_denied")


def _require(condition, message):
    util.require(condition, message, "codex_environment_unavailable")


def _expected(spec):
    from .codex_execution import verification_writable_roots
    roots = verification_writable_roots(spec["required_verification_commands"], spec["cwd"])
    _require(type(spec.get("sandbox_writable_roots")) is list
             and spec["sandbox_writable_roots"] == roots and bool(roots),
             "Codex 冻结的 Flutter SDK 缓存范围已变化")
    cwd = Path(spec["cwd"])
    _require(cwd.is_absolute() and str(cwd.resolve(strict=True)) == str(cwd),
             "Codex 工具工作区必须是原冻结的规范路径")
    run_id = spec.get("run_id")
    _require(type(run_id) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id),
             "Codex 工具环境轮次身份无效")
    root = cwd / ".pi" / "kanpilot" / "codex" / run_id
    return {"v": 1, "environment": {
        "XDG_CONFIG_HOME": str(root / "config"), "TMPDIR": str(root / "tmp"),
        "FLUTTER_SUPPRESS_ANALYTICS": "true"},
        "network": {"enabled": True, "mode": "limited", "allow_local_binding": True,
                    "allow_upstream_proxy": False, "domains": {}}}


def _directory(spec, *, create=False):
    """Walk from the frozen workspace using descriptors, never following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(spec["cwd"], flags)
    try:
        for name in (".pi", "kanpilot", "codex", spec["run_id"]):
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return
                raise
            os.close(fd)
            fd = child
        for name in ("config", "tmp"):
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    continue
                raise
            os.close(child)
    finally:
        os.close(fd)


def freeze(spec):
    """Opt in only new Flutter executions with an explicitly frozen SDK cache."""
    if spec.get("backend") != "codex" or not spec.get("sandbox_writable_roots"):
        return None
    spec["tool_environment"] = _expected(spec)
    return validate(spec)


def validate(spec):
    if "tool_environment" not in spec:
        return None
    try:
        expected = _expected(spec)
        actual = spec["tool_environment"]
        # JSON comparison distinguishes bool from int, unlike dict equality.
        _require(type(actual) is dict and util.jdump(actual) == util.jdump(expected),
                 "Codex 冻结的工具环境或网络权限不符")
        _directory(spec)
        return actual
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise util.KanpilotError("codex_environment_unavailable", "Codex 工具环境路径无效或含符号链接") from exc


def _toml(value):
    if type(value) is dict:
        return "{" + ",".join(json.dumps(key) + "=" + _toml(item) for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def permission_profile(spec):
    # CLI config tables merge recursively: {} cannot erase an existing domain
    # rule. A fresh run UUID gives this frozen policy its own namespace.
    return PROFILE + "_" + util.sha256_text(spec["run_id"] + "\n" + spec["cwd"])[:24]


def launcher_config(spec):
    environment = validate(spec)
    if environment is None:
        return []
    filesystem = {"/": "read", spec["cwd"]: "write"}
    filesystem.update({path: "write" for path in spec["sandbox_writable_roots"]})
    filesystem.update({str(Path(spec["cwd"]) / name): "read" for name in (".git", ".codex", ".agents")})
    profile = permission_profile(spec)
    values = {"default_permissions": profile, "features.network_proxy": True,
              "permissions." + profile + ".filesystem": filesystem,
              "permissions." + profile + ".network": environment["network"],
              "shell_environment_policy.set": environment["environment"]}
    return [arg for key, value in values.items() for arg in ("-c", key + "=" + _toml(value))]


def child_env(spec, base_env):
    result = dict(base_env)
    environment = validate(spec)
    if environment is not None:
        result.update(environment["environment"])
    return result


# Run with the exact permission configuration used by exec, with no model call,
# no task command, no host configuration writes and no secrets in the receipt.
_PROBE = r'''
import errno, json, os, socket, tempfile, urllib.error, urllib.request
checks = {}
for name, variable in (("config_write", "XDG_CONFIG_HOME"), ("temp_write", "TMPDIR")):
    try:
        with tempfile.TemporaryFile(dir=os.environ[variable]) as stream:
            stream.write(b"kanpilot"); stream.seek(0)
            checks[name] = stream.read() == b"kanpilot"
    except OSError:
        checks[name] = False
for name, family, address in (("ipv4", socket.AF_INET, "127.0.0.1"), ("ipv6", socket.AF_INET6, "::1")):
    try:
        with socket.socket(family) as listener, socket.socket(family) as client:
            listener.settimeout(2); client.settimeout(2)
            listener.bind((address, 0)); listener.listen(1); client.connect(listener.getsockname())
            with listener.accept()[0] as accepted:
                accepted.settimeout(2)
                client.sendall(b"x"); received = accepted.recv(1)
                accepted.sendall(b"y")
                checks[name] = received == b"x" and client.recv(1) == b"y"
    except OSError:
        checks[name] = False
checks["direct_denied"] = False
try:
    with socket.socket() as sock:
        sock.settimeout(.5); sock.connect(("198.51.100.1", 443))
except OSError as exc:
    checks["direct_denied"] = exc.errno in (errno.EPERM, errno.EACCES)
checks["proxy_active"] = bool(os.environ.get("CODEX_NETWORK_PROXY_ACTIVE"))
checks["proxy_denied"] = False
try:
    # The proxy must reject this before reaching any external service.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy} if proxy else {}))
    with opener.open("https://example.com/", timeout=2):
        pass
except urllib.error.URLError as exc:
    checks["proxy_denied"] = (not isinstance(exc, urllib.error.HTTPError)
                              and isinstance(exc.reason, OSError)
                              and str(exc.reason).startswith("Tunnel connection failed: 403 "))
except OSError:
    pass
print(json.dumps({"v": 1, "status": "passed" if all(checks.values()) else "failed", "checks": checks}))
'''


def preflight(spec, env):
    if validate(spec) is None:
        return None
    try:
        _directory(spec, create=True)
        argv = [constants.codex_bin(), "sandbox", "-P", permission_profile(spec), "-C", spec["cwd"]]
        argv += launcher_config(spec) + ["--", sys.executable, "-c", _PROBE]
        probe = subprocess.run(argv, cwd=spec["cwd"], env=child_env(spec, env),
                               capture_output=True, text=True, timeout=20)
        _require(probe.returncode == 0, "Codex 工具环境沙箱预检未成功启动")
        result = json.loads(probe.stdout)
        _require(type(result) is dict, "Codex 工具环境预检回执不是对象")
        checks = result.get("checks")
        _require(type(result.get("v")) is int and result["v"] == 1 and result.get("status") == "passed"
                 and type(checks) is dict and set(checks) == set(CHECKS)
                 and all(value is True for value in checks.values()),
                 "Codex 工具环境预检失败：" + ", ".join(name for name in CHECKS if not isinstance(checks, dict) or checks.get(name) is not True))
        return {"v": 1, "status": "passed", "checks": checks}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("codex_environment_unavailable", "Codex 工具环境沙箱预检不可用；未调用模型") from exc
