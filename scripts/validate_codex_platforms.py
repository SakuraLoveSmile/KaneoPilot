#!/usr/bin/env python3
"""Cloud-only verification of both frozen Codex execution modes, without a model.

The online preparation builds a disposable app to populate tool caches. A new
frozen execution then starts with no Flutter config or platform-generated files.
Fresh restricted and explicitly unrestricted executions provide separate evidence. No production DB,
installed KaneoPilot, provider credentials, or business worktree is used.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import traceback
import uuid


REPOSITORY = Path(__file__).resolve().parents[1]
FLUTTER_VERSION = "3.47.7"
FLUTTER_COMMIT = "abaf9c523780a608bd46686fd5e53740a07077f8"
DART_VERSION = "3.13.5"
CODEX_VERSION = "0.164.0"
# Every environment read by Flutter 3.47.7's BotDetector, including the IDE and
# analytics overrides which explicitly skip Azure metadata detection. The
# pinned SDK source is checked at runtime so this list cannot silently drift.
BOT_DETECTOR_SIGNALS = (
    "BOT", "FLUTTER_HOST", "FLUTTER_ANALYTICS_LOG_FILE", "TRAVIS",
    "CONTINUOUS_INTEGRATION", "CI", "APPVEYOR", "CIRRUS_CI", "AWS_REGION",
    "CODEBUILD_INITIATOR", "JENKINS_URL", "GITHUB_ACTIONS", "CHROME_HEADLESS",
    "BUILDBOT_BUILDERNAME", "SWARMING_TASK_ID", "BORG_ALLOC_DIR",
)
GENERATED_PACKAGE = "macos/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage/Package.swift"
GENERATED_PATHS = (
    ".dart_tool", "build", ".flutter-plugins", ".flutter-plugins-dependencies",
    "macos/Flutter/ephemeral", "macos/Pods", "android/.gradle", "android/local.properties",
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def require_cloud():
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "Native platform fixtures run only in GitHub-hosted Actions.")
    require(platform.system() == "Darwin" and platform.machine() == "arm64",
            "Native platform fixtures require a macOS arm64 runner.")


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def xcode_defaults_support(developer):
    """Check the pinned Xcode implementation before using its private defaults."""
    contents = developer.parent.resolve(strict=True)
    required = {
        contents / "Frameworks/Xcode3Core.framework/Xcode3Core": b"XCODE_USER_DEFAULTS_FILE",
        contents / "SharedFrameworks/SwiftPM.framework/SwiftPM": b"IDEPackageSupportDisableManifestSandbox",
    }
    records = []
    for entry, marker in required.items():
        require(entry.is_file(), "Pinned Xcode compatibility binary is missing: " + str(entry))
        # Follow the framework's version link, but remain in the selected bundle.
        binary = entry.resolve(strict=True)
        require(contents in binary.parents, "Pinned Xcode compatibility binary escapes its bundle")
        tail, found = b"", False
        with binary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                combined = tail + chunk
                if marker in combined:
                    found = True
                    break
                tail = combined[-len(marker):]
        require(found, "Pinned Xcode lacks the required private defaults entry: " + marker.decode())
        records.append({"binary": str(binary), "marker": marker.decode(), "present": True})
    return records


JAVA_LOOPBACK_PROBE = r'''
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
public class LoopbackProbe {
  public static void main(String[] args) {
    try (ServerSocketChannel server = ServerSocketChannel.open()) {
      server.bind(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0));
      try (SocketChannel client = SocketChannel.open(server.getLocalAddress());
           SocketChannel accepted = server.accept()) {
        client.write(ByteBuffer.wrap(new byte[] {42}));
        ByteBuffer received = ByteBuffer.allocate(1);
        accepted.read(received);
        if (received.position() != 1 || received.array()[0] != 42) throw new AssertionError("roundtrip");
        System.out.println("{\"passed\":true,\"prefer_ipv4\":"
          + Boolean.getBoolean("java.net.preferIPv4Stack") + "}");
      }
    } catch (Throwable error) {
      System.out.println("{\"passed\":false,\"prefer_ipv4\":"
        + Boolean.getBoolean("java.net.preferIPv4Stack") + "}");
      error.printStackTrace(System.err);
      System.exit(1);
    }
  }
}
'''


def _log_tail(path, limit=12000):
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")


def freeze_timeout_log(path, evidence, limit=4 * 1024 * 1024):
    """Detach the inherited log inode, then hash only a bounded snapshot."""
    live = evidence.parent / "unconfirmed-live-logs"
    require(not live.is_symlink(), "Unconfirmed log directory is a symlink")
    live.mkdir(exist_ok=True)
    raw = live / path.name
    require(not raw.exists() and not raw.is_symlink(), "Unconfirmed raw log already exists")
    path.rename(raw)
    with raw.open("rb") as source:
        observed = os.fstat(source.fileno()).st_size
        offset = max(0, observed - limit)
        source.seek(offset)
        snapshot = source.read(min(observed, limit))
    with path.open("xb") as target:
        target.write(snapshot)
    return {"kind": "bounded_tail_snapshot", "max_bytes": limit,
            "raw_bytes_observed": observed, "snapshot_bytes": len(snapshot),
            "truncated": offset > 0, "raw_path": str(raw.relative_to(evidence.parent)),
            "raw_may_still_be_written": True}


def run(argv, *, cwd, env, evidence, label, timeout=900, check=True, separate_stderr=False):
    """Stream native output to evidence files; never buffer full build output."""
    print("Starting " + label, flush=True)
    log = evidence / (label + ".log")
    error_log = evidence / (label + ".stderr.log") if separate_stderr else None
    timeout_error = None
    with contextlib.ExitStack() as streams:
        output = streams.enter_context(log.open("w", encoding="utf-8"))
        errors = (streams.enter_context(error_log.open("w", encoding="utf-8"))
                  if error_log else subprocess.STDOUT)
        try:
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=output,
                                    stderr=errors, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timeout_error = exc
    snapshots = {}
    if timeout_error:
        # ExitStack has closed the parent's streams. Descendants may still
        # write their inherited descriptors, which now follow the moved inode
        # outside uploaded evidence rather than changing these new snapshots.
        snapshots["log_snapshot"] = freeze_timeout_log(log, evidence)
        if error_log:
            snapshots["stderr_snapshot"] = freeze_timeout_log(error_log, evidence)
    record = {"argv": [str(arg) for arg in argv], "exit_code": None if timeout_error else result.returncode,
              "log": log.name, "log_sha256": sha256(log), **snapshots}
    if timeout_error:
        record.update(timed_out=True, timeout_seconds=timeout, descendant_cleanup="unconfirmed")
    if error_log:
        record.update(stderr_log=error_log.name, stderr_sha256=sha256(error_log))
    write_json(evidence / (label + ".json"), record)
    if timeout_error:
        print("Timed out " + label + "; shared fixture resources cannot be reused", flush=True)
        raise timeout_error
    print("Finished " + label + ": exit " + str(result.returncode), flush=True)
    if result.returncode:
        print(_log_tail(log), file=sys.stderr)
        if error_log:
            print(_log_tail(error_log), file=sys.stderr)
    if check:
        require(result.returncode == 0, label + " failed; see " + log.name)
    return log


def restricted_environment(base):
    """Keep cloud control signals out of the actual restricted tool process."""
    return {key: value for key, value in base.items()
            if key not in BOT_DETECTOR_SIGNALS
            and key != "ORG_GRADLE_PROJECT_kotlin.compiler.execution.strategy"}


def without_ci_signals(command, *, remove_bot=False):
    # Codex may apply shell_environment_policy.set after starting. Strip the
    # alternate signals again inside the sandbox, immediately before exec.
    return ["/usr/bin/env", *[item for name in BOT_DETECTOR_SIGNALS
                             if remove_bot or name != "BOT" for item in ("-u", name)], *command]


def validate_bot_detector_source(flutter):
    source = flutter.parent.parent / "packages/flutter_tools/lib/src/base/bot_detector.dart"
    text = source.read_text(encoding="utf-8")
    reads = re.findall(r"_platform\.environment(?:\['([^']+)'\]|\.containsKey\('([^']+)'\))", text)
    actual = {left or right for left, right in reads}
    require(actual == set(BOT_DETECTOR_SIGNALS), "Pinned Flutter BotDetector environment signals differ")
    require("http://169.254.169.254/metadata/instance" in text, "Pinned Flutter metadata endpoint differs")
    return {"source_sha256": sha256(source), "removed_signals": sorted(actual)}


def metadata_denials(output):
    lines = output.splitlines()
    matches = []
    for index, line in enumerate(lines):
        if "169.254.169.254" not in line:
            continue
        context = "\n".join(lines[max(0, index - 2):index + 3])
        if re.search(r"deny|denied|forbidden|not permitted|blocked|403", line, re.IGNORECASE):
            matches.append(context)
    return matches


METADATA_URL = "http://169.254.169.254/metadata/instance"
PROXY_DENY_BODY = {"status": "blocked", "host": "169.254.169.254", "reason": "not_allowed",
                   "decision": "deny", "source": "baseline_policy", "protocol": "http", "port": 80,
                   "message": "Domain not in allowlist."}
BOT_DETECTOR_PROBE = r'''
import 'dart:convert';
import 'dart:io' as io;
import 'package:file/local.dart';
import 'package:flutter_tools/src/base/bot_detector.dart';
import 'package:flutter_tools/src/base/config.dart';
import 'package:flutter_tools/src/base/logger.dart';
import 'package:flutter_tools/src/base/platform.dart';
import 'package:flutter_tools/src/persistent_tool_state.dart';
const signals = <String>__SIGNALS__;
const metadataUrl = '__METADATA_URL__';
void recordSocketFailure(Map<String, Object?> row, io.SocketException error) {
  final code = error.osError?.errorCode;
  row.addAll({'outcome': 'socket_exception', 'errno': code,
    'address': error.address?.address, 'port': error.port});
  if (row['url'] == metadataUrl && (code == 1 || code == 13)) {
    io.stderr.writeln('metadata request transport permission denied url=${row['url']} '
      'errno=$code address=${error.address?.address} port=${error.port}');
  }
}
final class ObservedRequest implements io.HttpClientRequest {
  ObservedRequest(this.delegate, this.row, this.responses);
  final io.HttpClientRequest delegate;
  final Map<String, Object?> row;
  final List<MapEntry<io.HttpClientResponse, Map<String, Object?>>> responses;
  @override io.HttpHeaders get headers => delegate.headers;
  @override Future<io.HttpClientResponse> close() async {
    try {
      final response = await delegate.close();
      row.addAll({'outcome': 'http_response', 'status_code': response.statusCode,
        'content_type': response.headers.value(io.HttpHeaders.contentTypeHeader),
        'proxy_error': response.headers.value('x-proxy-error')});
      responses.add(MapEntry(response, row));
      return response;
    } on io.SocketException catch (error) {
      recordSocketFailure(row, error);
      rethrow;
    }
  }
  @override Never noSuchMethod(Invocation invocation) {
    throw UnsupportedError('Unexpected HttpClientRequest member: ${invocation.memberName}');
  }
}
final class ObservedHttpClient implements io.HttpClient {
  ObservedHttpClient(this.delegate, this.attempts, this.responses);
  final io.HttpClient delegate;
  final List<Map<String, Object?>> attempts;
  final List<MapEntry<io.HttpClientResponse, Map<String, Object?>>> responses;
  @override Duration? get connectionTimeout => delegate.connectionTimeout;
  @override set connectionTimeout(Duration? value) { delegate.connectionTimeout = value; }
  @override Future<io.HttpClientRequest> getUrl(Uri url) async {
    final row = <String, Object?>{'url': url.toString(), 'outcome': 'pending'};
    attempts.add(row);
    try {
      final request = await delegate.getUrl(url);
      row['outcome'] = 'connected';
      return ObservedRequest(request, row, responses);
    } on io.SocketException catch (error) {
      recordSocketFailure(row, error);
      rethrow;
    }
  }
  @override void close({bool force = false}) { delegate.close(force: force); }
  @override Never noSuchMethod(Invocation invocation) {
    throw UnsupportedError('Unexpected HttpClient member: ${invocation.memberName}');
  }
}
Future<void> observeResponse(io.HttpClientResponse response, Map<String, Object?> row) async {
  var exceeded = false;
  try {
    final bytes = await response.fold<List<int>>(<int>[], (buffer, chunk) {
      if (buffer.length + chunk.length > 4096) {
        exceeded = true;
        throw StateError('response body exceeds 4096 bytes');
      }
      buffer.addAll(chunk);
      return buffer;
    }).timeout(const Duration(seconds: 3));
    final body = utf8.decode(bytes);
    row.addAll({'body': body, 'body_bytes': bytes.length,
      'body_complete': true, 'body_truncated': false});
    final decoded = jsonDecode(body);
    final expected = <String, Object?>__PROXY_DENY_BODY__;
    if (row['url'] == metadataUrl && row['status_code'] == 403 &&
        row['content_type'] == 'application/json' && row['proxy_error'] == 'blocked-by-allowlist' &&
        decoded is Map<String, dynamic> && decoded.length == expected.length &&
        expected.entries.every((entry) => decoded[entry.key] == entry.value)) {
      io.stderr.writeln('metadata request Codex proxy denied url=${row['url']} '
        'status=403 source=baseline_policy decision=deny');
    }
  } catch (error) {
    row.addAll({'body_complete': false, 'body_truncated': exceeded, 'body_error': error.toString()});
  }
}
Future<void> main(List<String> args) async {
  if (args.length != 1) throw ArgumentError('new XDG directory required');
  const fs = LocalFileSystem();
  const platform = LocalPlatform();
  final logger = BufferLogger.test();
  final home = platform.environment['HOME'];
  if (home == null || fs.file(fs.path.join(home, '.flutter_tool_state')).existsSync()) {
    throw StateError('legacy HOME state is not allowed');
  }
  if (platform.environment['XDG_CONFIG_HOME'] != args.single) {
    throw StateError('controlled XDG directory was not selected');
  }
  final directory = fs.directory(args.single);
  if (directory.existsSync()) throw StateError('XDG state directory is not cold');
  directory.createSync(recursive: true);
  final config = Config('tool_state', fileSystem: fs, logger: logger, platform: platform);
  final expectedPath = fs.path.join(args.single, 'tool_state');
  if (config.configPath != expectedPath) throw StateError('tool state escaped controlled XDG');
  final initialExists = fs.file(config.configPath).existsSync();
  if (initialExists) throw StateError('tool state file is not cold');
  final state = PersistentToolState(fileSystem: fs, logger: logger, platform: platform);
  final before = state.isRunningOnBot;
  if (before != null) throw StateError('bot state was already cached');
  final clients = <ObservedHttpClient>[];
  final attempts = <Map<String, Object?>>[];
  final responses = <MapEntry<io.HttpClientResponse, Map<String, Object?>>>[];
  final detector = BotDetector(
    httpClientFactory: () {
      final client = ObservedHttpClient(io.HttpClient(), attempts, responses);
      clients.add(client);
      return client;
    }, platform: platform, persistentToolState: state);
  try {
    final result = await detector.isRunningOnBot;
    for (final observed in responses) { await observeResponse(observed.key, observed.value); }
    print(jsonEncode({'config_path': config.configPath,
      'initial_config_exists': initialExists,
      'persisted_config_exists': fs.file(config.configPath).existsSync(),
      'initial_state': before, 'is_bot': result, 'persisted_state': state.isRunningOnBot,
      'http_client_count': clients.length, 'http_attempts': attempts,
      'present_signals': signals.where(io.Platform.environment.containsKey).toList()..sort()}));
  } finally {
    for (final client in clients) { client.close(force: true); }
  }
}
'''.replace("__SIGNALS__", json.dumps(list(BOT_DETECTOR_SIGNALS))).replace("__METADATA_URL__", METADATA_URL).replace("__PROXY_DENY_BODY__", json.dumps(PROXY_DENY_BODY))


def bot_detector_command(flutter, source, state):
    sdk = flutter.parent.parent
    dart = sdk / "bin/cache/dart-sdk/bin/dart"
    packages = sdk / "packages/flutter_tools/.dart_tool/package_config.json"
    require(dart.is_file() and packages.is_file(), "Pinned Flutter Dart runtime/package configuration is missing")
    require(not state.exists() and not state.is_symlink(), "BotDetector state directory is not cold")
    return ["XDG_CONFIG_HOME=" + str(state), str(dart), "--packages=" + str(packages), str(source), str(state)], packages


def bot_denial_kind(row):
    socket_fields = {"url", "outcome", "errno", "address", "port"}
    if (set(row) == socket_fields and row["url"] == METADATA_URL and row["outcome"] == "socket_exception"
            and type(row["errno"]) is int and row["errno"] in (1, 13)
            and (row["address"] is None or type(row["address"]) is str)
            and (row["port"] is None or type(row["port"]) is int)):
        return "os_permission_denied"
    response_fields = {"url", "outcome", "status_code", "content_type", "proxy_error", "body",
                       "body_bytes", "body_complete", "body_truncated"}
    if (set(row) == response_fields and row["url"] == METADATA_URL and row["outcome"] == "http_response"
            and type(row["status_code"]) is int and row["status_code"] == 403
            and row["content_type"] == "application/json" and row["proxy_error"] == "blocked-by-allowlist"
            and type(row["body"]) is str and type(row["body_bytes"]) is int
            and row["body_bytes"] == len(row["body"].encode("utf-8")) <= 4096
            and row["body_complete"] is True and row["body_truncated"] is False):
        def unique_object(pairs):
            if len({key for key, value in pairs}) != len(pairs):
                raise ValueError("Duplicate proxy-deny JSON field")
            return dict(pairs)
        try:
            body = json.loads(row["body"], object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            pass
        else:
            if json.dumps(body, sort_keys=True) == json.dumps(PROXY_DENY_BODY, sort_keys=True):
                return "codex_proxy_denied"
    raise AssertionError("No-BOT control lacks a permission errno or exact Codex proxy deny response")


def parse_bot_detector_receipt(output, *, bot, state):
    receipt = json.loads(output)
    expected_keys = {"config_path", "initial_config_exists", "persisted_config_exists", "initial_state",
                     "is_bot", "persisted_state", "http_client_count", "present_signals", "http_attempts"}
    require(type(receipt) is dict and set(receipt) == expected_keys,
            "Explicit SDK BotDetector getter receipt fields differ")
    attempts = receipt["http_attempts"]
    if bot:
        require(attempts == [], "BOT positive control made an HTTP request")
        actual_bot = True
    else:
        require(type(attempts) is list and len(attempts) == 1 and type(attempts[0]) is dict,
                "No-BOT control did not make exactly one metadata request")
        actual_bot = bot_denial_kind(attempts[0]) == "codex_proxy_denied"
    expected = {"config_path": str(state / "tool_state"), "initial_config_exists": False,
                "persisted_config_exists": True, "initial_state": None,
                "is_bot": actual_bot, "persisted_state": actual_bot,
                "http_client_count": 0 if bot else 1, "present_signals": ["BOT"] if bot else []}
    require(json.dumps({key: receipt[key] for key in expected}, sort_keys=True) == json.dumps(expected, sort_keys=True),
            "Explicit SDK BotDetector getter receipt differs from the cold expected path")
    return receipt


def snapshot_flutter_tool_state(private_config, evidence, *, label="restricted", bot=True):
    source = private_config / "tool_state"
    require(source.is_file() and not source.is_symlink(), "Frozen XDG tool_state was not created by real Flutter verification")
    values = json.loads(source.read_text(encoding="utf-8"))
    require(type(values) is dict and values.get("is-bot") is bot,
            "Real Flutter did not persist the expected BOT value in its frozen XDG tool_state")
    artifact = evidence / (label + "-flutter-tool-state.json")
    shutil.copyfile(source, artifact)
    return {"source_path": str(source), "artifact": artifact.name, "sha256": sha256(artifact),
            "bytes": artifact.stat().st_size, "is_bot": values["is-bot"]}


def bot_detector_probe(flutter, source, state, spec, environment, constants, child_env,
                       evidence, *, label, bot):
    command, packages = bot_detector_command(flutter, source, state)
    argv = native_argv(environment, constants, spec, without_ci_signals(command, remove_bot=not bot),
                       log_denials=True)
    output = run(argv, cwd=spec["cwd"], env=child_env, evidence=evidence, label=label,
                 timeout=300, separate_stderr=True)
    record = json.loads((evidence / (label + ".json")).read_text(encoding="utf-8"))
    receipt = parse_bot_detector_receipt(output.read_text(encoding="utf-8"), bot=bot, state=state)
    result = {"kind": "explicit_sdk_bot_detector_getter", "state_directory": str(state),
              "source": source.name, "source_sha256": sha256(source),
              "package_config": str(packages), "package_config_sha256": sha256(packages),
              "receipt": receipt, "denial_kind": "not_requested" if bot else bot_denial_kind(receipt["http_attempts"][0]),
              "tool_state": snapshot_flutter_tool_state(state, evidence, label=label, bot=receipt["is_bot"]),
              **record}
    denied = metadata_denials(output.read_text(encoding="utf-8") + "\n"
                             + (evidence / record["stderr_log"]).read_text(encoding="utf-8"))
    if not bot:
        require(denied, "No-BOT counterexample lacks a specific 169.254.169.254 denial")
    return result, denied


def git(cwd, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                            cwd=cwd, text=True, capture_output=True, timeout=30)
    require(result.returncode == 0, "Disposable fixture Git command failed: " + result.stderr)
    return result.stdout.strip()


def commit_fixture(path):
    git(path, "init", "--quiet")
    git(path, "config", "user.name", "KaneoPilot isolated fixture")
    git(path, "config", "user.email", "fixture@invalid.example")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "-m", "Freeze disposable platform fixture")
    return git(path, "rev-parse", "HEAD")


def reset_generated(app):
    """Delete only named generated paths of this script's disposable fixture."""
    require((app / ".kanpilot-codex-platform-fixture").is_file(), "Not an owned disposable fixture")
    for relative in GENERATED_PATHS:
        path = app / relative
        require(not path.is_symlink(), "Generated fixture path was replaced by a symlink: " + relative)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def build_fixture(flutter, root, evidence, env):
    plugin, app, dependency = root / "fixture_plugin", root / "app", root / "fixture_git"
    run([str(flutter), "create", "--template=plugin", "--no-pub", "--platforms=macos,android",
         "--project-name=fixture_plugin", str(plugin)], cwd=root, env=env,
        evidence=evidence, label="online-create-plugin")
    require((plugin / "macos/fixture_plugin/Package.swift").is_file(),
            "Flutter did not create the required SwiftPM plugin")
    dependency.mkdir()
    (dependency / "lib").mkdir()
    (dependency / "pubspec.yaml").write_text(
        "name: fixture_git\nversion: 1.0.0\nenvironment:\n  sdk: ^3.13.0\n", encoding="utf-8")
    (dependency / "lib/fixture_git.dart").write_text("int fixtureValue() => 42;\n", encoding="utf-8")
    ref = commit_fixture(dependency)
    run([str(flutter), "create", "--no-pub", "--platforms=macos,android",
         "--project-name=fixture_app", str(app)], cwd=root, env=env,
        evidence=evidence, label="online-create-app")
    (app / ".kanpilot-codex-platform-fixture").write_text("disposable CI fixture\n", encoding="utf-8")
    (app / "pubspec.yaml").write_text(
        "name: fixture_app\npublish_to: none\nversion: 1.0.0+1\n"
        "environment:\n  sdk: ^3.13.0\ndependencies:\n"
        "  flutter:\n    sdk: flutter\n  collection: 1.19.1\n"
        "  fixture_plugin:\n    path: ../fixture_plugin\n"
        "  fixture_git:\n    git:\n      url: " + dependency.as_uri() + "\n      ref: " + ref + "\n"
        "dev_dependencies:\n  flutter_test:\n    sdk: flutter\n"
        "flutter:\n  uses-material-design: true\n", encoding="utf-8")
    (app / "analysis_options.yaml").write_text("analyzer:\n  errors:\n    unused_import: error\n", encoding="utf-8")
    (app / "lib/main.dart").write_text(
        "import 'package:flutter/material.dart';\n"
        "void main() => runApp(const MaterialApp(home: Text('native fixture')));\n", encoding="utf-8")
    for test in (app / "test").glob("*.dart"):
        test.unlink()
    (app / "test/dependencies_test.dart").write_text(
        "import 'package:collection/collection.dart';\n"
        "import 'package:fixture_git/fixture_git.dart';\n"
        "import 'package:flutter_test/flutter_test.dart';\n"
        "void main() { test('offline hosted and git dependencies', () {\n"
        "  expect(const ListEquality<int>().equals([fixtureValue()], [42]), isTrue);\n"
        "}); }\n", encoding="utf-8")
    # Keep Gradle daemon ownership inside this disposable execution, rather than
    # letting a warmed, unrestricted daemon service the restricted build.
    with (app / "android/gradle.properties").open("a", encoding="utf-8") as properties:
        properties.write("\norg.gradle.daemon=false\n")
    run([str(flutter), "pub", "get"], cwd=app, env=env, evidence=evidence, label="online-pub-get")
    run([str(flutter), "build", "macos", "--debug", "--no-pub"], cwd=app, env=env,
        evidence=evidence, label="online-warm-macos", timeout=1800)
    # Warm dependencies without leaving an unrestricted Kotlin daemon that
    # could service the subsequent restricted build. Keep its default strategy
    # for the restricted phase so native sandbox requirements remain visible.
    warm_android_env = {**env, "ORG_GRADLE_PROJECT_kotlin.compiler.execution.strategy": "in-process"}
    run([str(flutter), "build", "apk", "--debug", "--no-pub"], cwd=app, env=warm_android_env,
        evidence=evidence, label="online-warm-android", timeout=1800)
    require((app / GENERATED_PACKAGE).is_file(), "Warm build did not generate the SwiftPM package")
    commit_fixture(app)
    reset_generated(app)
    require(not (app / GENERATED_PACKAGE).exists(), "Platform fixture was not reset")
    require(not (app / "android/local.properties").exists(), "Android fixture retained warm local.properties")
    return app


def sandbox_argv(environment, constants, spec, command):
    require(spec["config"].get("sandbox", "workspace-write") == "workspace-write",
            "codex sandbox cannot represent an unrestricted exec")
    return [constants.codex_bin(), "sandbox", "-P", environment.permission_profile(spec),
            "-C", spec["cwd"], *environment.launcher_config(spec), "--", *command]


def native_argv(environment, constants, spec, command, *, log_denials=False):
    if spec["config"].get("sandbox", "workspace-write") == "danger-full-access":
        require(spec["tool_environment"]["v"] == 3
                and spec["tool_environment"]["isolation"] == "none", "Unrestricted fixture requires v3")
        # codex sandbox always installs a base OS policy, even with a full-access
        # profile. Direct children reproduce exec's lack of OS sandboxing.
        return command
    argv = sandbox_argv(environment, constants, spec, command)
    if log_denials:
        argv.insert(argv.index("--"), "--log-denials")
    return argv


def verification_commands(flutter, *, macos=True, android=True):
    binary = shlex.quote(str(flutter))
    commands = [binary + " analyze --no-pub", binary + " test --no-pub"]
    if macos:
        commands.append("LANG=en_US.UTF-8 " + binary + " build macos --debug --no-pub")
    if android:
        commands.append(binary + " build apk --debug --no-pub")
    return commands


def reset_sdk_plugin_generated(flutter):
    # This SDK belongs to the disposable cloud fixture. Force the restricted
    # build to recreate exactly the two generated roots granted by production.
    plugin = flutter.parent.parent / "packages/flutter_tools/gradle"
    for name in (".gradle", "build"):
        path = plugin / name
        require(not path.is_symlink(), "SDK plugin generated path is a symlink")
        if path.exists():
            require(path.is_dir(), "SDK plugin generated path is not a directory")
            shutil.rmtree(path)


def make_spec(app, flutter, plan, adapter, environment, contract, util, *, suffix,
              sandbox="workspace-write", macos=True, android=True, commands=None):
    commands = commands if commands is not None else verification_commands(flutter, macos=macos, android=android)
    spec = {"v": 1, "backend": "codex", "cwd": str(app), "repository": str(app),
            "run_id": "fixture-" + suffix + "-" + uuid.uuid4().hex,
            "task_id": "fixture-codex-platforms", "request_id": "fixture-request",
            "config": {"backend": "codex", "model": "no-model-is-called", "reasoning_effort": "low"},
            "worktree_mode": "branch", "plan_path": str(plan), "plan_sha256": util.sha256_file(plan),
            "completion_policy": "command_verification", "instruction": "Isolated platform fixture; no model.",
            "required_verification_commands": commands}
    if sandbox != "workspace-write":
        spec["config"]["sandbox"] = sandbox
    spec["sandbox_writable_roots"] = adapter.verification_writable_roots(commands, app)
    environment.freeze(spec)
    require(spec.get("tool_environment", {}).get("v") == (3 if sandbox == "danger-full-access" else 2),
            "Fixture frozen environment version differs from its selected sandbox")
    require(spec["tool_environment"]["environment"].get("BOT") == "true", "Cold Flutter bot detection is not frozen")
    contract.freeze_spec(spec, requirements_sha256="a" * 64)
    contract.validate_spec(spec, starting=True)
    return spec


def verify_native(spec, environment, constants, contract, child_env, evidence, *, label):
    results = []
    for index, command in enumerate(spec["required_verification_commands"], 1):
        contract.validate_spec(spec)
        name = label + "-verify-" + str(index)
        argv = native_argv(environment, constants, spec,
                           without_ci_signals(["/bin/sh", "-c", command]), log_denials=True)
        try:
            run(argv, cwd=spec["cwd"], env=child_env, evidence=evidence, label=name,
                timeout=1800, check=False, separate_stderr=True)
        except subprocess.TimeoutExpired:
            record = json.loads((evidence / (name + ".json")).read_text(encoding="utf-8"))
            results.append({"command": command, **record})
            write_json(evidence / (label + "-native-results.json"), results)
            raise
        record = json.loads((evidence / (name + ".json")).read_text(encoding="utf-8"))
        results.append({"command": command, **record})
        write_json(evidence / (label + "-native-results.json"), results)
    # Store original outcomes before running any diagnostics or asserting.
    write_json(evidence / (label + "-native-results.json"), results)
    for index, item in enumerate(results, 1):
        if item["exit_code"] == 0:
            continue
        argv = native_argv(environment, constants, spec,
                           without_ci_signals(["/bin/sh", "-c", item["command"] + " --verbose"]),
                           log_denials=True)
        run(argv, cwd=spec["cwd"], env=child_env, evidence=evidence,
            label=label + "-diagnostic-verbose-" + str(index), timeout=300, check=False)
    require(all(item["exit_code"] == 0 for item in results), label + " original native commands failed")
    return results


@contextlib.contextmanager
def mode_attempt(report, name, evidence):
    """Preserve a mode's failure while allowing the other mode to run."""
    report[name] = {"status": "failed", "model_calls": 0}
    try:
        yield
    except Exception as exc:
        result = report[name]
        result.update(status="failed", error=str(exc), error_type=type(exc).__name__)
        if isinstance(exc, subprocess.TimeoutExpired):
            result["shared_resource_reuse"] = "blocked_descendant_cleanup_unconfirmed"
        native_results = evidence / (name + "-native-results.json")
        if native_results.is_file():
            result["native_commands"] = json.loads(native_results.read_text(encoding="utf-8"))
        if name == "restricted":
            for child, label in (("restricted_android", "restricted-android"), ("restricted_macos", "restricted-macos")):
                records = evidence / (label + "-native-results.json")
                if child in report and records.is_file():
                    report[child]["native_commands"] = json.loads(records.read_text(encoding="utf-8"))
                    report[child]["native_status"] = ("passed" if all(
                        item["exit_code"] == 0 for item in report[child]["native_commands"]) else "failed")
        write_json(evidence / (name + "-failure.json"), result)
        traceback.print_exc()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise  # subprocess.run cannot confirm that every descendant stopped.


def require_modes_passed(report):
    failures = [name for name in ("restricted", "unrestricted")
                if report.get(name, {}).get("status") != "passed"]
    require(not failures, "Native execution mode acceptance failed: " + ", ".join(failures))


def launcher_evidence(spec, adapter, evidence):
    from types import SimpleNamespace
    provider = SimpleNamespace(config_args=[])
    records = {}
    for name, session in (("exec", None), ("resume", "fixture-session-no-model")):
        argv = adapter.launcher_argv(spec, evidence / "schema.json", evidence / "output.json", provider,
                                     session_id=session)
        require(argv[argv.index("-s") + 1] == "danger-full-access", "Launcher did not freeze no-sandbox mode")
        require('approval_policy="never"' in argv, "Launcher approval policy differs")
        require(not any(token in " ".join(argv) for token in
                        ("default_permissions=", "features.network_proxy", "--add-dir", "-P ")),
                "Unrestricted launcher retained restricted permissions")
        require(("resume" in argv) == bool(session), "Launcher resume identity differs")
        records[name] = argv
    write_json(evidence / "unrestricted-launchers.json", records)
    return records


def restricted_macos_preflight(spec, environment, base, uti, util, evidence):
    require(uti["outside"] == {"workspace_uti": True}, "Host Xcode workspace type is unavailable")
    result = {"status": "failed", "spec": "restricted-macos-spec.json", "uti": uti, "model_calls": 0}
    try:
        receipt = environment.preflight(spec, base)
    except util.KanpilotError as exc:
        require(uti["inside"] == {"workspace_uti": False}
                and exc.code == "codex_environment_unavailable"
                and "workspace" in str(exc) and "设置" in str(exc) and "无沙箱" in str(exc),
                "Restricted macOS did not fail with its specific UTI/settings explanation")
        result.update(status="blocked", code=exc.code, detail=util.redact(str(exc)))
        write_json(evidence / "restricted-macos-preflight.json", result)
    else:
        require(uti["inside"] == {"workspace_uti": True} and receipt["status"] == "passed",
                "Restricted macOS preflight disagrees with the UTI probe")
        write_json(evidence / "restricted-macos-preflight.json", receipt)
        result["status"] = "supported"
    return result


def restricted_android_preflight(spec, environment, base, requirement, probe, util, evidence,
                                 *, label="restricted-android", capability=None):
    result = {"status": "failed", "model_calls": 0,
              "initial_gradle_sdk_requirement": requirement,
              "native_execution_performed": False, "native_status": "not_run", "native_commands": []}

    def refresh():
        actual, actual_probe = capability() if capability else (requirement, probe)
        if actual is not None:
            require(type(actual_probe) is dict and set(actual_probe) == {"exists", "is_directory", "writable"}
                    and all(type(value) is bool for value in actual_probe.values()), "Android SDK capability probe is invalid")
        result.update(gradle_sdk_requirement=actual, sdk_write_probe=actual_probe)
        denied = actual is not None and actual_probe == {"exists": True, "is_directory": True, "writable": False}
        return actual, actual_probe, denied

    try:
        receipt = environment.preflight(spec, base)
    except util.KanpilotError as exc:
        # Offline setup may create local.properties. Read the real helper and
        # probe again after production preflight, including its failure path.
        actual, actual_probe, blocked = refresh()
        require(blocked and exc.code == "codex_environment_unavailable"
                and all(text in str(exc) for text in
                        (actual["gradle_version"], actual["project_root"], "SDK", "设置", "无沙箱", "新轮次")),
                "Restricted Android did not fail with its specific Gradle SDK/settings explanation")
        result.update(status="blocked", code=exc.code, detail=util.redact(str(exc)))
        write_json(evidence / (label + "-preflight.json"), result)
    else:
        actual, actual_probe, blocked = refresh()
        require(not blocked and receipt["status"] == "passed",
                "Restricted Android preflight disagrees with the same-profile SDK write probe")
        if actual is not None:
            expected = {**actual, **actual_probe}
            require(receipt.get("android_gradle_sdk") == expected,
                    "Android preflight receipt does not bind its actual Gradle version and SDK capability")
        result.update(status="supported", preflight=receipt)
        write_json(evidence / (label + "-preflight.json"), receipt)
    return result


_PERMISSION_PROBE = r'''
import errno, json, os, sys
checks = {}
for name, path in json.loads(sys.argv[1]).items():
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as exc:
        checks[name] = exc.errno in (errno.EPERM, errno.EACCES)
    else:
        os.close(fd)
        checks[name] = False
print(json.dumps(checks))
raise SystemExit(0 if all(checks.values()) else 1)
'''

_BOT_ENV_PROBE = r'''
import json, os, sys
signals = json.loads(sys.argv[1])
present = {name: os.environ[name] for name in signals if name in os.environ}
print(json.dumps({"bot": os.environ.get("BOT"), "present_signals": sorted(present)}))
raise SystemExit(0 if present == {"BOT": "true"} else 1)
'''


def validate(args):
    require_cloud()  # Before creating a directory or starting any tool.
    root = Path(args.output).expanduser().resolve()
    require(not root.exists(), "Fixture output must be a new directory")
    root.mkdir(parents=True)
    evidence = root / "evidence"
    evidence.mkdir()
    report = {"v": 2, "status": "failed", "source_sha": git(REPOSITORY, "rev-parse", "HEAD"),
              "model_calls": 0, "production_access": False, "checks": []}
    try:
        flutter = Path(args.flutter).expanduser().resolve(strict=True)
        require(git(flutter.parent.parent, "rev-parse", "HEAD") == FLUTTER_COMMIT, "Flutter source revision differs")
        report["bot_detector"] = validate_bot_detector_source(flutter)
        require(not (Path.home() / ".flutter_tool_state").exists(),
                "Legacy host .flutter_tool_state would invalidate the cold configuration fixture")
        env = dict(os.environ, BOT="true", FLUTTER_SUPPRESS_ANALYTICS="true",
                   PUB_CACHE=str(root / "host-pub-cache"), GRADLE_USER_HOME=str(root / "host-gradle-cache"),
                   XDG_CONFIG_HOME=str(root / "host-config"), CODEX_HOME=str(root / "codex-home"))
        for name in ("PUB_CACHE", "GRADLE_USER_HOME", "XDG_CONFIG_HOME", "CODEX_HOME"):
            Path(env[name]).mkdir()
        developer = Path(env["DEVELOPER_DIR"]).resolve(strict=True)
        report["xcode_private_defaults_support"] = xcode_defaults_support(developer)
        xcode_version = run(["/usr/bin/xcrun", "xcodebuild", "-version"], cwd=root, env=env,
                            evidence=evidence, label="tool-xcode-version", separate_stderr=True).read_text()
        require(xcode_version.startswith("Xcode 16.4\n"), "Pinned Xcode version differs")
        java = Path(env["JAVA_HOME"]) / "bin/java"
        java_classes = root / "java-probe-classes"
        java_classes.mkdir()
        java_source = evidence / "LoopbackProbe.java"
        java_source.write_text(JAVA_LOOPBACK_PROBE, encoding="utf-8")
        run([str(java.with_name("javac")), "-d", str(java_classes), str(java_source)],
            cwd=root, env=env, evidence=evidence, label="online-java-probe-compile")
        # shared.sh bootstraps the tool on first invocation. Its Dart download
        # helper redirects curl progress to stdout, even for --machine. Finish
        # that explicit online preparation before requesting strict JSON.
        run([str(flutter), "precache", "--android", "--macos"], cwd=root, env=env,
            evidence=evidence, label="online-precache", timeout=1800)
        version_log = run([str(flutter), "--version", "--machine"], cwd=root, env=env,
                          evidence=evidence, label="tool-flutter-version", separate_stderr=True)
        version = json.loads(version_log.read_text(encoding="utf-8"))
        require(version["frameworkVersion"] == FLUTTER_VERSION
                and version["dartSdkVersion"] == DART_VERSION, "Flutter/Dart version differs")
        codex = shutil.which("codex")
        require(codex, "Codex CLI is unavailable")
        codex_version = run([codex, "--version"], cwd=root, env=env, evidence=evidence,
                            label="tool-codex-version", separate_stderr=True).read_text(encoding="utf-8")
        require(codex_version.strip() == "codex-cli " + CODEX_VERSION, "Codex CLI version differs")
        report["tools"] = {"flutter": version, "codex": codex_version.strip(), "python": sys.version,
                           "macos": platform.mac_ver(), "machine": platform.machine(),
                           "xcode": xcode_version.strip()}
        report["kotlin_compilation"] = {"online_warm_strategy": "in-process",
                                        "restricted_strategy": "project default; no environment override"}
        app = build_fixture(flutter, root, evidence, env)
        # Import only after the cloud guard and isolated cache environment exist.
        sys.path.insert(0, str(REPOSITORY / "core"))
        from kanpilot import codex_environment as environment, codex_execution as adapter
        from kanpilot import constants, codex_tool_setup, execution_contract as contract, git_delivery, util
        plan = evidence / "frozen-plan.md"
        plan.write_text("Disposable native platform verification fixture.\n", encoding="utf-8")
        previous = dict(os.environ)
        os.environ.update(env)
        try:
            base = restricted_environment(env)
            before = git_delivery.source_state_sha256(app)
            lock_before = sha256(app / "pubspec.lock")
            with mode_attempt(report, "restricted", evidence):
                # Probe the exact same OS API used by production preflight, both
                # outside and inside the selected restricted policy.
                macos_spec = make_spec(app, flutter, plan, adapter, environment, contract, util,
                                       suffix="restricted-macos", commands=[verification_commands(flutter)[2]])
                write_json(evidence / "restricted-macos-spec.json", macos_spec)
                uti_command = [sys.executable, "-c", environment.MACOS_WORKSPACE_UTI_PROBE
                               + "\nimport json; print(json.dumps({'workspace_uti': workspace_uti()}))"]
                uti = {}
                for label, argv in (("outside", uti_command),
                                    ("inside", sandbox_argv(environment, constants, macos_spec, uti_command))):
                    output = run(argv, cwd=app, env=environment.child_env(macos_spec, base), evidence=evidence,
                                 label="workspace-uti-" + label, separate_stderr=True)
                    uti[label] = json.loads(output.read_text(encoding="utf-8"))
                macos_result = restricted_macos_preflight(macos_spec, environment, base, uti, util, evidence)
                macos_result["spec_sha256"] = sha256(evidence / "restricted-macos-spec.json")
                report["restricted_macos"] = macos_result
                if macos_result["status"] == "supported":
                    macos_result["native_commands"] = verify_native(
                        macos_spec, environment, constants, contract, environment.child_env(macos_spec, base),
                        evidence, label="restricted-macos")
                    reset_generated(app)
                report["checks"].append("restricted_macos_uti_matches_preflight_without_fallback")

                # A separate default-mode run validates the repair which does not
                # need Xcode's unsupported type registry access.
                reset_sdk_plugin_generated(flutter)
                spec = make_spec(app, flutter, plan, adapter, environment, contract, util,
                                 suffix="restricted", macos=False, android=False)
                write_json(evidence / "restricted-spec.json", spec)
                restricted = {"status": "failed", "spec": "restricted-spec.json",
                              "spec_sha256": sha256(evidence / "restricted-spec.json"),
                              "execution_kind": "codex_sandbox", "model_calls": 0,
                              "source_snapshot_before": git_delivery.source_state_sha256(app)}
                report["restricted"] = restricted
                private = spec["tool_environment"]["environment"]
                private_config = Path(private["XDG_CONFIG_HOME"])
                private_analyzer = Path(private["ANALYZER_STATE_LOCATION_OVERRIDE"])
                require(not private_config.exists() and not private_analyzer.exists(), "Restricted tool state is not cold")
                child_env = environment.child_env(spec, base)
                bot_log = run(sandbox_argv(environment, constants, spec, without_ci_signals(
                    [sys.executable, "-c", _BOT_ENV_PROBE, json.dumps(BOT_DETECTOR_SIGNALS)])),
                    cwd=app, env=child_env, evidence=evidence, label="cold-bot-environment", separate_stderr=True)
                restricted["bot_environment"] = json.loads(bot_log.read_text(encoding="utf-8"))
                require(not private_config.exists(), "Environment probe warmed Flutter configuration")
                receipt = environment.preflight(spec, base)
                write_json(evidence / "restricted-cold-preflight.json", receipt)
                require(receipt["status"] == "passed", "Restricted preparation failed")
                require(sha256(app / "pubspec.lock") == lock_before
                        and git_delivery.source_state_sha256(app) == before, "Preparation changed fixture source/lock")
                # Native Android has its own APK-only contract. Seed only its
                # private caches, then compare the production admission result
                # with a real SDK capability probe under that same profile.
                android_spec = make_spec(app, flutter, plan, adapter, environment, contract, util,
                                         suffix="restricted-android", commands=[verification_commands(flutter)[-1]])
                write_json(evidence / "restricted-android-spec.json", android_spec)
                cache_seed = codex_tool_setup.seed_caches(android_spec)
                requirement = codex_tool_setup.android_gradle_sdk_requirement(android_spec)
                local_properties = app / "android/local.properties"
                require(not local_properties.exists(), "Cold Android preflight retained warm local.properties")
                android_private = android_spec["tool_environment"]["environment"]
                android_env = environment.child_env(android_spec, base)

                def android_capability(label="restricted-android-sdk-write-probe"):
                    actual = codex_tool_setup.android_gradle_sdk_requirement(android_spec)
                    if actual is None:
                        return None, None
                    output = run(sandbox_argv(environment, constants, android_spec,
                        [sys.executable, "-c", environment.GRADLE_SDK_WRITE_PROBE, actual["project_root"]]),
                        cwd=app, env=android_env, evidence=evidence, label=label, separate_stderr=True)
                    return actual, json.loads(output.read_text(encoding="utf-8"))

                android_result = restricted_android_preflight(android_spec, environment, base,
                    requirement, None, util, evidence, capability=android_capability)
                require(local_properties.is_file(), "Offline preparation did not generate Android local.properties")
                android_result.update(local_properties_before_preflight={"exists": False},
                                      local_properties_after_preflight={"exists": True, "sha256": sha256(local_properties)})
                requirement = android_result["gradle_sdk_requirement"]
                sdk_probe = android_result["sdk_write_probe"]
                android_result.update(spec="restricted-android-spec.json",
                                      spec_sha256=sha256(evidence / "restricted-android-spec.json"),
                                      cache_seed=cache_seed)
                report["restricted_android"] = android_result
                if android_result["status"] == "supported":
                    android_result["native_execution_performed"] = True
                    android_result["native_status"] = "failed"
                    android_result["native_commands"] = verify_native(android_spec, environment, constants, contract,
                        android_env, evidence, label="restricted-android")
                    android_result["native_status"] = "passed"
                else:
                    # Analysis-only fixture preparation never admits the blocked
                    # native execution or invokes a model. Keep its own receipt.
                    preparation = codex_tool_setup.setup_argv(android_spec)
                    run(sandbox_argv(environment, constants, android_spec, without_ci_signals(preparation)),
                        cwd=app, env=android_env, evidence=evidence, label="restricted-analysis-offline-preparation",
                        timeout=180, separate_stderr=True)
                    android_result["analysis_only_preparation"] = json.loads(
                        (evidence / "restricted-analysis-offline-preparation.json").read_text(encoding="utf-8"))
                require((app / ".dart_tool/package_config.json").is_file(), "Analysis fixture dependencies were not prepared")
                require(sha256(app / "pubspec.lock") == lock_before
                        and git_delivery.source_state_sha256(app) == before, "Offline analysis preparation changed source/lock")
                private_pub = Path(android_private["PUB_CACHE"])
                require(any((private_pub / "hosted").glob("*/collection-1.19.1"))
                        and any((private_pub / "git").glob("fixture_git-*")), "Private locked dependency cache is incomplete")
                restricted["private_dependency_cache"] = {"hosted_collection": True, "git_checkout": True,
                                                          "spec": "restricted-android-spec.json"}
                require(android_private["GRADLE_RO_DEP_CACHE"] == str(Path(env["GRADLE_USER_HOME"]) / "caches"),
                        "Read-only Gradle cache must be its caches base, without a duplicate modules-2")
                restricted["gradle_ro_dep_cache"] = android_private["GRADLE_RO_DEP_CACHE"]
                child_env = environment.child_env(spec, dict(base, PUB_CACHE=str(private_pub)))
                sentinel = root / "readonly-sentinel"
                sentinel.write_text("unchanged\n", encoding="utf-8")
                host_config = Path.home() / ".config/kanpilot-codex-platform-fixture"
                host_config.mkdir(parents=True, exist_ok=True)
                host_sentinel = host_config / "sentinel"
                host_sentinel.write_text("unchanged\n", encoding="utf-8")
                host_analyzer = Path.home() / ".dartServer/kanpilot-fixture-sentinel"
                host_analyzer.parent.mkdir(parents=True, exist_ok=True)
                host_analyzer.write_text("unchanged\n", encoding="utf-8")
                plugin = flutter.parent.parent / "packages/flutter_tools/gradle"
                targets = {"sdk_source_denied": str(flutter.parent.parent / "packages/flutter_tools/lib/src/base/bot_detector.dart"),
                           "sdk_plugin_source_denied": str(plugin / "src/main/kotlin/FlutterPlugin.kt"),
                           "sdk_plugin_script_denied": str(plugin / "build.gradle.kts"),
                           "host_config_denied": str(host_sentinel), "host_analyzer_denied": str(host_analyzer),
                           "other_directory_denied": str(sentinel)}
                output = run(sandbox_argv(environment, constants, android_spec, without_ci_signals(
                    [sys.executable, "-c", _PERMISSION_PROBE, json.dumps(targets)])), cwd=app, env=android_env,
                    evidence=evidence, label="restricted-denials", separate_stderr=True)
                restricted["permission_checks"] = json.loads(output.read_text(encoding="utf-8"))
                java_probes = {}
                for mode in ("default", "ipv4"):
                    remove = ["-u", "JDK_JAVA_OPTIONS"] if mode == "default" else []
                    command = ["/usr/bin/env", *remove, str(java), "-cp", str(java_classes), "LoopbackProbe"]
                    label = "java-loopback-" + mode
                    output = run(sandbox_argv(environment, constants, android_spec, without_ci_signals(command)),
                                 cwd=app, env=android_env, evidence=evidence, label=label, separate_stderr=True,
                                 timeout=30, check=False)
                    record = json.loads((evidence / (label + ".json")).read_text())
                    try:
                        probe_result = json.loads(output.read_text())
                    except ValueError:
                        probe_result = None
                    java_probes[mode] = {"exit_code": record["exit_code"], "result": probe_result}
                restricted["java_loopback"] = java_probes
                require(java_probes["ipv4"] == {"exit_code": 0, "result": {"passed": True, "prefer_ipv4": True}},
                        "Frozen Java IPv4 loopback roundtrip failed")
                restricted["native_commands"] = verify_native(spec, environment, constants, contract,
                                                               child_env, evidence, label="restricted")
                gradle_fixture = app / ".pi/kanpilot/gradle-cache-diagnostic"
                gradle_fixture.mkdir(parents=True)
                (gradle_fixture / "settings.gradle.kts").write_text('rootProject.name = "cache-diagnostic"\n', encoding="utf-8")
                # Gradle exposes activation at info level. This is diagnostic cache
                # evidence only; it cannot replace any original verify command.
                gradle_log = run(sandbox_argv(environment, constants, android_spec, without_ci_signals(
                    ["/bin/sh", "-c", "cd " + shlex.quote(str(gradle_fixture))
                     + " && " + shlex.quote(str(app / "android/gradlew")) + " --offline --no-daemon --info help"])),
                    cwd=app / "android", env=android_env, evidence=evidence, label="restricted-gradle-cache-info", timeout=300)
                cache_text = gradle_log.read_text(encoding="utf-8")
                require(re.search(r"read.only dependency cache.*enabled", cache_text, re.IGNORECASE),
                        "Gradle did not report its read-only dependency cache enabled")
                restricted["gradle_ro_cache_enabled"] = True
                analyzer_uuid = private_analyzer / ".instrumentation/uuid.txt"
                require(analyzer_uuid.is_file() and analyzer_uuid.read_text().strip(), "Private analyzer identity missing")
                require(host_analyzer.read_text() == "unchanged\n", "Host analyzer sentinel changed")
                restricted["private_analyzer_state"] = {"uuid_created": True, "host_analyzer_write_denied": True}
                restricted["flutter_tool_state"] = snapshot_flutter_tool_state(private_config, evidence)
                # This explicit SDK getter diagnostic isolates the metadata
                # branch and default XDG state from the business verification.
                bot_source = evidence / "bot-detector-probe.dart"
                bot_source.write_text(BOT_DETECTOR_PROBE, encoding="utf-8")
                positive, _ = bot_detector_probe(flutter, bot_source,
                    private_config.parent / "bot-detector-positive-state", spec, environment, constants,
                    child_env, evidence, label="cold-with-bot", bot=True)
                restricted["bot_detector_positive"] = positive
                negative, denied = bot_detector_probe(flutter, bot_source,
                    private_config.parent / "bot-detector-without-bot-state", spec, environment, constants,
                    child_env, evidence, label="cold-without-bot", bot=False)
                restricted["bot_detector_without_bot"] = negative
                restricted["without_bot_metadata_denials"] = denied
                resumed = environment.preflight(spec, base)
                write_json(evidence / "restricted-resume-preflight.json", resumed)
                require(resumed["status"] == "passed", "Restricted analysis resume preflight failed")
                restricted["resume_native_commands"] = verify_native(spec, environment, constants, contract,
                    child_env, evidence, label="restricted-resume")
                restricted["resume_reprepared"] = False
                restricted["resume_rechecked"] = True
                android_resume = restricted_android_preflight(android_spec, environment, base,
                    requirement, sdk_probe, util, evidence, label="restricted-android-resume",
                    capability=lambda: android_capability("restricted-android-resume-sdk-write-probe"))
                require(android_resume["status"] == android_result["status"], "Android resume changed capability admission")
                android_result["resume_preflight"] = android_resume
                restricted["source_snapshot_after"] = git_delivery.source_state_sha256(app)
                require(restricted["source_snapshot_after"] == before, "Restricted verification changed source")
                restricted["status"] = "passed"
                report["checks"].append("restricted_two_frozen_commands_and_native_capability_admission_passed")

            with mode_attempt(report, "unrestricted", evidence):
                reset_generated(app)
                unrestricted = make_spec(app, flutter, plan, adapter, environment, contract, util,
                                         suffix="unrestricted", sandbox="danger-full-access")
                write_json(evidence / "spec.json", unrestricted)
                free = {"status": "failed", "spec": "spec.json", "execution_kind": "direct_unrestricted",
                        "spec_sha256": sha256(evidence / "spec.json"),
                        "model_calls": 0, "launchers": launcher_evidence(unrestricted, adapter, evidence),
                        "source_snapshot_before": git_delivery.source_state_sha256(app)}
                report["unrestricted"] = free
                free_host_analyzer = Path.home() / ".dartServer/kanpilot-unrestricted-fixture-sentinel"
                free_host_analyzer.parent.mkdir(parents=True, exist_ok=True)
                free_host_analyzer.write_text("unchanged\n", encoding="utf-8")
                frozen = unrestricted["tool_environment"]
                require(frozen["isolation"] == "none" and frozen["network"] == {"mode": "unrestricted"}
                        and frozen["writable_roots"] == [], "Explicit v3 permission contract differs")
                require("XCODE_USER_DEFAULTS_FILE" not in frozen["environment"]
                        and "JDK_JAVA_OPTIONS" not in frozen["environment"], "Unrestricted mode retained sandbox workarounds")
                require(not Path(frozen["environment"]["XDG_CONFIG_HOME"]).exists(), "Unrestricted tool state is not cold")
                free_child = environment.child_env(unrestricted, base)
                bot_log = run(native_argv(environment, constants, unrestricted, without_ci_signals(
                    [sys.executable, "-c", _BOT_ENV_PROBE, json.dumps(BOT_DETECTOR_SIGNALS)])),
                    cwd=app, env=free_child, evidence=evidence, label="unrestricted-bot-environment", separate_stderr=True)
                free["bot_environment"] = json.loads(bot_log.read_text(encoding="utf-8"))
                receipt = environment.preflight(unrestricted, base)
                write_json(evidence / "unrestricted-cold-preflight.json", receipt)
                require(receipt["status"] == "passed" and receipt["isolation"] == "none", "Unrestricted preparation failed")
                require((app / GENERATED_PACKAGE).is_file()
                        and "fixture_plugin" in (app / GENERATED_PACKAGE).read_text(), "Offline Swift package was not generated")
                free["native_commands"] = verify_native(unrestricted, environment, constants, contract,
                    free_child, evidence, label="unrestricted")
                free_analyzer = Path(frozen["environment"]["ANALYZER_STATE_LOCATION_OVERRIDE"]) / ".instrumentation/uuid.txt"
                require(free_analyzer.is_file() and free_analyzer.read_text().strip(), "Unrestricted private analyzer identity missing")
                require(free_host_analyzer.read_text() == "unchanged\n", "Unrestricted analysis changed host analyzer state")
                free["private_analyzer_state"] = {"uuid_created": True, "host_analyzer_unchanged": True}
                products = [app / "build/macos/Build/Products/Debug/fixture_app.app/Contents/MacOS/fixture_app",
                            app / "build/app/outputs/flutter-apk/app-debug.apk"]
                free["products"] = [{"path": str(path.relative_to(app)), "bytes": path.stat().st_size,
                                      "sha256": sha256(path)} for path in products]
                shutil.rmtree(app / "macos/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage")
                resumed = environment.preflight(unrestricted, base)
                write_json(evidence / "unrestricted-resume-preflight.json", resumed)
                require(resumed["status"] == "passed" and (app / GENERATED_PACKAGE).is_file(),
                        "Unrestricted resume did not restore its generated Swift package")
                free["resume_reprepared"] = True
                free["source_snapshot_after"] = git_delivery.source_state_sha256(app)
                require(free["source_snapshot_after"] == before, "Unrestricted verification changed source")
                free["status"] = "passed"
                report["checks"].append("unrestricted_four_frozen_native_commands_exit_zero")
            empty_gradle = root / "missing-gradle-cache"
            empty_gradle.mkdir()
            os.environ["GRADLE_USER_HOME"] = str(empty_gradle)
            missing_base = dict(base, GRADLE_USER_HOME=str(empty_gradle))
            report["missing_cache"] = {}
            for mode in ("workspace-write", "danger-full-access"):
                try:
                    missing = make_spec(app, flutter, plan, adapter, environment, contract, util,
                                        suffix="missing-" + mode, sandbox=mode, macos=False)
                    environment.preflight(missing, missing_base)
                except util.KanpilotError as exc:
                    require(exc.code == "codex_environment_unavailable" and "Gradle" in str(exc),
                            "Missing Gradle cache did not produce a specific environment failure")
                    report["missing_cache"][mode] = {"code": exc.code, "detail": util.redact(str(exc)), "model_calls": 0}
                else:
                    raise AssertionError("Missing Gradle cache was admitted in " + mode)
            report["checks"].append("both_modes_missing_cache_rejected_before_model")
            report["source_snapshot_before"] = before
            report["source_snapshot_after"] = git_delivery.source_state_sha256(app)
            require(report["source_snapshot_after"] == before and sha256(app / "pubspec.lock") == lock_before,
                    "Final fixture source/lock differs")
            require_modes_passed(report)
            report["status"] = "passed"
        finally:
            os.environ.clear()
            os.environ.update(previous)
    except Exception as exc:
        report["error"] = str(exc)
        traceback.print_exc()
        raise
    finally:
        write_json(evidence / "result.json", report)
        checksums = sorted((path.name, sha256(path)) for path in evidence.iterdir() if path.is_file())
        (evidence / "SHA256SUMS").write_text("".join(digest + "  " + name + "\n" for name, digest in checksums),
                                                encoding="utf-8")
    print(json.dumps({"status": "passed", "evidence": str(evidence), "model_calls": 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter", required=True, help="Pinned Flutter executable")
    parser.add_argument("--output", required=True, help="New disposable cloud fixture directory")
    validate(parser.parse_args())


if __name__ == "__main__":
    main()
