"""Frozen Flutter tool environment; never runs a task's verification commands."""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError
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


def _cache_source(variable, default):
    path = Path(os.environ.get(variable) or default).expanduser()
    _require(path.is_absolute(), "Codex 工具缓存来源必须是绝对路径：" + variable)
    return str(path.resolve())


def _flutter_settings():
    """Carry only build tool locations/features, never user auth or analytics."""
    legacy = Path.home() / ".flutter_settings"
    config = Path(os.environ["XDG_CONFIG_HOME"]) if os.environ.get("XDG_CONFIG_HOME") else Path.home() / ".config/flutter"
    path = legacy if legacy.exists() else config / "settings"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise util.KanpilotError("codex_environment_unavailable", "Flutter 宿主构建设置不可读") from exc
    _require(type(value) is dict, "Flutter 宿主构建设置不是对象")
    result = {}
    for key in ("jdk-dir", "android-sdk", "android-studio-dir"):
        if key in value:
            _require(type(value[key]) is str and Path(value[key]).expanduser().is_absolute(),
                     "Flutter 构建路径设置无效：" + key)
            result[key] = str(Path(value[key]).expanduser().resolve())
    for key in ("enable-swift-package-manager", "enable-macos-desktop"):
        if key in value:
            _require(type(value[key]) is bool, "Flutter 构建开关不是布尔值：" + key)
            result[key] = value[key]
    return result


def _check_java_ipv4_override():
    # HotSpot parses _JAVA_OPTIONS after command-line arguments. Its quotes
    # group values, but backslashes are literal rather than shell escapes.
    options = os.environ.get("_JAVA_OPTIONS", "")
    if not options:
        return
    lexer = shlex.shlex(options, posix=True)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r\n\v\f"
    lexer.commenters = ""
    lexer.escape = ""
    try:
        for argument in lexer:
            key, separator, value = argument.partition("=")
            if key == "-Djava.net.preferIPv4Stack":
                _require(bool(separator) and value.lower() == "true",
                         "Android 受限构建要求 IPv4；请先移除 _JAVA_OPTIONS 中冲突的同名 JVM 参数")
    except ValueError as exc:
        raise util.KanpilotError("codex_environment_unavailable", "Android _JAVA_OPTIONS 引号格式无效，无法核对 IPv4 参数") from exc


def _expected_v2(spec, *, restricted=True):
    from .codex_execution import verification_flutter_commands
    result = _expected(spec)
    commands = verification_flutter_commands(spec["required_verification_commands"], spec["cwd"])
    platforms = sorted({"android" if args[1] in ("apk", "appbundle") else "macos"
                        for _, args in commands if len(args) >= 2 and args[0] == "build"
                        and args[1] in ("macos", "apk", "appbundle")})
    root = Path(result["environment"]["TMPDIR"]).parent
    result.update(v=2, platforms=platforms, writable_roots=[])
    result["environment"]["BOT"] = "true"
    # The Dart analyzer otherwise writes ~/.dartServer even when Flutter's
    # own configuration has been redirected with XDG_CONFIG_HOME.
    result["environment"]["ANALYZER_STATE_LOCATION_OVERRIDE"] = str(root / "analyzer")
    native = bool(platforms)
    binaries = {binary for binary, _ in commands}
    _require(not native or len(binaries) == 1, "同轮原生构建必须使用同一个冻结 Flutter SDK")
    setup = {"flutter": commands[0][0] if commands else None, "pub_offline": native,
             "android": "android" in platforms, "pub_cache_source": None,
             "gradle_cache_source": None, "flutter_settings": {}}
    if native:
        setup["pub_cache_source"] = _cache_source("PUB_CACHE", Path.home() / ".pub-cache")
        setup["flutter_settings"] = _flutter_settings()
        result["environment"]["PUB_CACHE"] = str(root / "pub-cache")
    if setup["android"] and restricted:
        _check_java_ipv4_override()
        java_options = "-Djava.net.preferIPv4Stack=true"
        _require(not os.environ.get("JDK_JAVA_OPTIONS") or os.environ["JDK_JAVA_OPTIONS"] == java_options,
                 "Android 受限构建要求固定 JDK_JAVA_OPTIONS；请先移除冲突的宿主 JVM 参数")
        result["environment"]["JDK_JAVA_OPTIONS"] = java_options
        plugin = Path(setup["flutter"]).parent.parent / "packages/flutter_tools/gradle"
        _require(plugin.is_dir() and plugin.resolve(strict=True) == plugin,
                 "Flutter Gradle 插件目录缺失或含符号链接")
        # The included SDK plugin owns its build-local Gradle state and
        # compilation outputs; its scripts and src tree remain read-only.
        for name in (".gradle", "build"):
            generated = plugin / name
            _require(not generated.is_symlink() and generated.resolve() == generated,
                     "Flutter Gradle 生成目录含符号链接")
            result["writable_roots"].append(str(generated))
    if setup["android"]:
        setup["gradle_cache_source"] = _cache_source("GRADLE_USER_HOME", Path.home() / ".gradle")
        result["environment"]["GRADLE_USER_HOME"] = str(root / "gradle")
        # Gradle's documented read-only dependency cache does not contain user
        # properties, init scripts, credentials or wrapper lock files.
        cache_root = Path(setup["gradle_cache_source"]) / "caches"
        dependencies = cache_root / "modules-2"
        _require(not cache_root.is_symlink() and not dependencies.is_symlink()
                 and cache_root.resolve() == cache_root and dependencies.resolve() == dependencies,
                 "Gradle 只读依赖缓存含符号链接")
        if dependencies.is_dir():
            # Gradle appends modules-2 to this configured cache base itself.
            result["environment"]["GRADLE_RO_DEP_CACHE"] = str(cache_root)
        for key, variable in (("jdk-dir", "JAVA_HOME"), ("android-sdk", "ANDROID_HOME")):
            if key in setup["flutter_settings"]:
                result["environment"][variable] = setup["flutter_settings"][key]
        if "ANDROID_HOME" in result["environment"]:
            result["environment"]["ANDROID_SDK_ROOT"] = result["environment"]["ANDROID_HOME"]
    if "macos" in platforms:
        _require(sys.platform == "darwin", "冻结的 macOS 构建需要 macOS 执行端")
        if restricted:
            _macos_restricted_settings(result, setup, root)
    result["setup"] = setup
    if not restricted:
        result.update(v=3, isolation="none", network={"mode": "unrestricted"})
    return result


def _macos_restricted_settings(result, setup, root):
    # Xcode's private compatibility setting avoids nesting SwiftPM's
    # manifest sandbox inside the already restricted Codex process.
    # Its availability must be checked with each supported Xcode version.
    result["environment"]["XCODE_USER_DEFAULTS_FILE"] = str(root / "config/xcode-user-defaults.plist")
    setup["xcode_user_defaults"] = {"IDEPackageSupportDisableManifestSandbox": True}
    # Foundation NSTemporaryDirectory uses confstr, even with TMPDIR set.
    system_tmp = os.confstr(65537)  # _CS_DARWIN_USER_TEMP_DIR (Darwin ABI)
    _require(bool(system_tmp), "无法确定 macOS 系统用户临时目录")
    manifests = Path.home().resolve() / "Library/Caches/org.swift.swiftpm/manifests"
    _require(manifests.resolve() == manifests, "SwiftPM manifest 缓存路径含符号链接")
    result["writable_roots"].extend([
        str((Path.home() / "Library/Developer/Xcode/DerivedData").resolve()),
        str(Path(system_tmp).resolve(strict=True)), str(manifests)])


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
        names = ["config", "tmp"]
        if (spec.get("tool_environment") or {}).get("v") in (2, 3):
            names.append("analyzer")
            setup = spec["tool_environment"]["setup"]
            if setup["pub_offline"]:
                names.append("pub-cache")
            if setup["android"]:
                names.append("gradle")
        for name in names:
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


def _walk_directory(path, *, create=False):
    """Validate/create only the explicitly frozen canonical host directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for name in Path(path).parts[1:]:
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
    finally:
        os.close(fd)


def _prepare_settings(spec):
    environment = spec["tool_environment"]
    if environment["v"] not in (2, 3):
        return
    _require(not (Path.home() / ".flutter_tool_state").exists(),
             "Flutter 旧式 .flutter_tool_state 会绕过私有状态目录；请先迁移为 XDG Flutter 状态")
    if not environment["setup"]["pub_offline"]:
        return
    # Old Flutter config files take priority over XDG_CONFIG_HOME. Do not grant
    # them write access or claim that a private XDG directory redirects them.
    _require(not (Path.home() / ".flutter_settings").exists(),
             "Flutter 旧式 .flutter_settings 会绕过私有配置目录；请先迁移为 XDG Flutter 设置")
    directory = Path(environment["environment"]["XDG_CONFIG_HOME"])
    _walk_directory(str(directory), create=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(directory, flags)
    expected = environment["setup"]["flutter_settings"]
    try:
        try:
            target = os.open("settings", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            target = os.open("settings", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(target, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                _require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536,
                         "本轮 Flutter 构建设置被特殊文件替换或过大")
                actual = json.loads(stream.read(65537))
            _require(util.jdump(actual) == util.jdump(expected), "本轮冻结的 Flutter 构建设置已变化")
        else:
            with os.fdopen(target, "w", encoding="utf-8") as stream:
                stream.write(util.jdump(expected))
        if "xcode_user_defaults" in environment["setup"]:
            expected = environment["setup"]["xcode_user_defaults"]
            try:
                target = os.open("xcode-user-defaults.plist", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=fd)
            except FileExistsError:
                target = os.open("xcode-user-defaults.plist", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=fd)
                with os.fdopen(target, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    _require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536,
                             "本轮 Xcode 构建设置被特殊文件替换或过大")
                    try:
                        actual = plistlib.loads(stream.read(65537))
                    except (ValueError, ExpatError) as exc:
                        raise util.KanpilotError("codex_environment_unavailable", "本轮 Xcode 构建设置不是有效 plist") from exc
                _require(type(actual) is dict and set(actual) == set(expected)
                         and all(type(actual[key]) is type(value) and actual[key] == value
                                 for key, value in expected.items()), "本轮冻结的 Xcode 构建设置已变化")
            else:
                with os.fdopen(target, "wb") as stream:
                    plistlib.dump(expected, stream)
    finally:
        os.close(fd)


def freeze(spec):
    """Opt in only new Flutter executions with an explicitly frozen SDK cache."""
    if spec.get("backend") != "codex" or not spec.get("sandbox_writable_roots"):
        return None
    spec["tool_environment"] = _expected_v2(
        spec, restricted=spec.get("config", {}).get("sandbox", "workspace-write") != "danger-full-access")
    return validate(spec)


def validate(spec):
    if "tool_environment" not in spec:
        return None
    try:
        actual = spec["tool_environment"]
        _require(type(actual) is dict and type(actual.get("v")) is int
                 and actual["v"] in (1, 2, 3), "Codex 工具环境版本无效")
        sandbox = spec.get("config", {}).get("sandbox", "workspace-write")
        _require(sandbox == ("danger-full-access" if actual["v"] == 3 else "workspace-write"),
                 "Codex 工具环境与冻结沙箱模式不符；旧轮次不能通过设置变更扩大权限")
        expected = _expected(spec) if actual["v"] == 1 else _expected_v2(spec, restricted=actual["v"] == 2)
        # JSON comparison distinguishes bool from int, unlike dict equality.
        _require(type(actual) is dict and util.jdump(actual) == util.jdump(expected),
                 "Codex 冻结的工具环境或网络权限不符")
        _directory(spec)
        for path in actual.get("writable_roots", []):
            _walk_directory(path)
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
    if environment["v"] == 3:
        return ["-c", "shell_environment_policy.set=" + _toml(environment["environment"])]
    filesystem = {"/": "read", spec["cwd"]: "write"}
    filesystem.update({path: "write" for path in spec["sandbox_writable_roots"]})
    filesystem.update({path: "write" for path in environment.get("writable_roots", [])})
    filesystem.update({str(Path(spec["cwd"]) / name): "read" for name in (".git", ".codex", ".agents")})
    if "XCODE_USER_DEFAULTS_FILE" in environment["environment"]:
        filesystem[environment["environment"]["XCODE_USER_DEFAULTS_FILE"]] = "read"
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
MACOS_WORKSPACE_UTI_PROBE = r'''
def workspace_uti():
    import ctypes as C
    cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cs = C.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
    cf.CFStringCreateWithCString.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32]
    cf.CFStringCreateWithCString.restype = C.c_void_p
    cf.CFStringGetCString.argtypes = [C.c_void_p, C.c_char_p, C.c_long, C.c_uint32]
    cf.CFStringGetCString.restype = C.c_bool
    cf.CFRelease.argtypes = [C.c_void_p]
    cf.CFRelease.restype = None
    cs.UTTypeConformsTo.argtypes = [C.c_void_p, C.c_void_p]
    cs.UTTypeConformsTo.restype = C.c_bool
    cs.UTTypeCreatePreferredIdentifierForTag.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
    cs.UTTypeCreatePreferredIdentifierForTag.restype = C.c_void_p
    pointers = []
    try:
        for text in ('public.filename-extension', 'xcworkspace',
                     'com.apple.dt.document.workspace', 'com.apple.package'):
            pointer = cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)
            if not pointer:
                return False
            pointers.append(pointer)
        preferred = cs.UTTypeCreatePreferredIdentifierForTag(pointers[0], pointers[1], None)
        if not preferred:
            return False
        pointers.append(preferred)
        buffer = C.create_string_buffer(4096)
        return (bool(cf.CFStringGetCString(preferred, buffer, len(buffer), 0x08000100))
                and buffer.value == b'com.apple.dt.document.workspace'
                and bool(cs.UTTypeConformsTo(pointers[2], pointers[3])))
    finally:
        for pointer in reversed(pointers):
            cf.CFRelease(pointer)
'''


_PROBE = MACOS_WORKSPACE_UTI_PROBE + r'''
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
if os.environ.get("KANPILOT_PROBE_WORKSPACE_UTI") == "1":
    checks["workspace_uti"] = workspace_uti()
roots = {}
for path in json.loads(os.environ.get("KANPILOT_PROBE_WRITABLE_ROOTS", "[]")):
    try:
        with tempfile.TemporaryDirectory(dir=path) as nested:
            with tempfile.TemporaryFile(dir=nested) as stream:
                stream.write(b"kanpilot"); stream.seek(0)
                roots[path] = stream.read() == b"kanpilot"
    except OSError:
        roots[path] = False
result = {"v": 1, "status": "passed" if all(checks.values()) and all(roots.values()) else "failed", "checks": checks}
if roots:
    result["writable_roots"] = roots
print(json.dumps(result))
'''


GRADLE_SDK_WRITE_PROBE = r'''
import json, os, sys
path = sys.argv[1]
print(json.dumps({"exists": os.path.exists(path), "is_directory": os.path.isdir(path),
                  "writable": os.access(path, os.W_OK)}))
'''


def _preflight_android_gradle_sdk(spec, env, argv, *, check=None):
    from . import codex_tool_setup
    requirement = codex_tool_setup.android_gradle_sdk_requirement(spec)
    if requirement is None:
        return None
    if check:
        check()
    root = requirement["project_root"]
    identity = "Gradle " + requirement["gradle_version"] + " 的 Flutter SDK 插件项目根 " + root
    hint = "请在设置→运行端→执行→Codex CLI 选择无沙箱，并创建新轮次"
    compatibility_argv = argv[:argv.index("--") + 1] + [sys.executable, "-c", GRADLE_SDK_WRITE_PROBE, root]
    try:
        probe = subprocess.run(compatibility_argv, cwd=spec["cwd"], env=child_env(spec, env),
                               capture_output=True, text=True, timeout=20)
        _require(probe.returncode == 0, identity + " 兼容预检未成功启动；未调用模型；" + hint)
        result = json.loads(probe.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("codex_environment_unavailable", identity + " 兼容预检不可用；未调用模型；" + hint) from exc
    _require(type(result) is dict and set(result) == {"exists", "is_directory", "writable"}
             and all(type(value) is bool for value in result.values()), identity + " 预检回执无效；未调用模型；" + hint)
    _require(result["exists"] and result["is_directory"], identity + " 缺失或不是目录；未调用模型")
    _require(result["writable"], identity + " 在受限沙箱中不可写，Gradle 9 起要求项目根可写；" + hint)
    return {**requirement, **result}


def preflight(spec, env, *, check=None):
    environment = validate(spec)
    if environment is None:
        return None
    try:
        if check:
            check()
        _directory(spec, create=True)
        roots = environment.get("writable_roots", [])
        for path in roots:
            _walk_directory(path, create=True)
        private = environment["environment"]
        probe_roots = list(roots) + [private[key] for key in
                                   ("ANALYZER_STATE_LOCATION_OVERRIDE", "PUB_CACHE", "GRADLE_USER_HOME")
                                   if key in private]
        _prepare_settings(spec)
        if environment["v"] == 3:
            checks = {}
            writable = {}
            for name, path in ([("config_write", private["XDG_CONFIG_HOME"]),
                                ("temp_write", private["TMPDIR"])]
                               + [(path, path) for path in probe_roots]):
                with tempfile.TemporaryFile(dir=path) as stream:
                    stream.write(b"kanpilot")
                    stream.seek(0)
                    _require(stream.read() == b"kanpilot", "Codex 私有工具目录不可写")
                (writable if name in probe_roots else checks)[name] = True
            receipt = {"v": 3, "status": "passed", "isolation": "none",
                       "network": {"mode": "unrestricted"}, "checks": checks}
            if probe_roots:
                receipt["writable_roots"] = writable
        else:
            argv = [constants.codex_bin(), "sandbox", "-P", permission_profile(spec), "-C", spec["cwd"]]
            argv += launcher_config(spec) + ["--", sys.executable, "-c", _PROBE]
            probe_env = child_env(spec, env)
            probe_env["KANPILOT_PROBE_WRITABLE_ROOTS"] = json.dumps(probe_roots)
            macos_uti = environment["v"] == 2 and "macos" in environment["platforms"]
            expected_checks = CHECKS + (("workspace_uti",) if macos_uti else ())
            probe_env.pop("KANPILOT_PROBE_WORKSPACE_UTI", None)
            if macos_uti:
                probe_env["KANPILOT_PROBE_WORKSPACE_UTI"] = "1"
            probe = subprocess.run(argv, cwd=spec["cwd"], env=probe_env,
                                   capture_output=True, text=True, timeout=20)
            _require(probe.returncode == 0, "Codex 工具环境沙箱预检未成功启动")
            result = json.loads(probe.stdout)
            _require(type(result) is dict, "Codex 工具环境预检回执不是对象")
            checks = result.get("checks")
            if macos_uti:
                _require(type(checks) is dict and checks.get("workspace_uti") is True,
                         "当前 Codex 受限沙箱无法识别 Xcode workspace 类型；请在设置→运行端→执行→Codex CLI 选择无沙箱，并创建新轮次")
            _require(type(result.get("v")) is int and result["v"] == 1 and result.get("status") == "passed"
                     and type(checks) is dict and set(checks) == set(expected_checks)
                     and all(value is True for value in checks.values()),
                     "Codex 工具环境预检失败：" + ", ".join(name for name in expected_checks if not isinstance(checks, dict) or checks.get(name) is not True))
            if probe_roots:
                _require(result.get("writable_roots") == {path: True for path in probe_roots},
                         "Codex 工具状态、原生构建缓存或宿主临时目录不可写")
            receipt = {"v": environment["v"], "status": "passed", "checks": checks}
            if probe_roots:
                receipt["writable_roots"] = result["writable_roots"]
        if environment["v"] in (2, 3):
            from . import codex_setup_process, codex_tool_setup, git_delivery
            if check:
                check()
            counts = codex_tool_setup.seed_caches(spec)
            if environment["v"] == 2:
                compatibility = _preflight_android_gradle_sdk(spec, env, argv, check=check)
                if compatibility is not None:
                    receipt["android_gradle_sdk"] = compatibility
            command = codex_tool_setup.setup_argv(spec)
            if command:
                before = git_delivery.source_state_sha256(spec["cwd"])
                setup_argv = command if environment["v"] == 3 else argv[:argv.index("--") + 1] + command
                try:
                    setup = codex_setup_process.run(setup_argv, cwd=spec["cwd"], env=child_env(spec, env),
                                                    timeout=180, check=check)
                except subprocess.TimeoutExpired as exc:
                    raise util.KanpilotError("codex_environment_unavailable",
                                             "Flutter 离线平台依赖准备超时；准备进程组已终止，未调用模型") from exc
                except RuntimeError as exc:
                    raise util.KanpilotError("codex_environment_unavailable",
                                             "Flutter 离线平台依赖准备未完成：" + util.redact(str(exc))) from exc
                # Setup may write only ignored generation/cache files. A lock
                # or tracked project migration must not pass as preparation.
                _require(git_delivery.source_state_sha256(spec["cwd"]) == before,
                         "Flutter 离线准备改动了源码或锁文件；已保留现场，未调用模型")
                secrets = [value for key, value in env.items() if value and
                           re.search(r"TOKEN|SECRET|API_KEY|PASSWORD", key, re.I)]
                detail = util.redact((setup.stdout + "\n" + setup.stderr).strip(), secrets=secrets)[-4096:]
                _require(setup.returncode == 0, "Flutter 离线平台依赖准备失败（退出码 "
                         + str(setup.returncode) + "）；请在宿主补齐锁定依赖缓存：" + detail)
                if "macos" in environment["platforms"]:
                    project = Path(spec["cwd"]) / "macos/Runner.xcodeproj/project.pbxproj"
                    if project.is_file() and "FlutterGeneratedPluginSwiftPackage" in project.read_text(encoding="utf-8"):
                        generated = Path(spec["cwd"]) / "macos/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage/Package.swift"
                        _require(generated.is_file(), "Flutter macOS 平台生成包仍缺失：" + str(generated))
                # A cold worktree may gain ignored local.properties during
                # pub get. Re-identify the actual SDK/runtime after successful
                # source-preserving setup before allowing any model turn.
                if environment["v"] == 2:
                    compatibility = _preflight_android_gradle_sdk(spec, env, argv, check=check)
                    if compatibility is not None:
                        receipt["android_gradle_sdk"] = compatibility
                receipt["setup"] = {"status": "passed", "command": command,
                                    "exit_code": setup.returncode, "cache_seed": counts}
        return receipt
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("codex_environment_unavailable", "Codex 工具环境预检不可用；未调用模型") from exc
