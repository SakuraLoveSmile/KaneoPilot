#!/usr/bin/env python3
"""Cloud-only native Flutter verification in the real Codex sandbox, without a model.

The online preparation builds a disposable app to populate tool caches. A new
frozen execution then starts with no Flutter config or platform-generated files.
Only that second, restricted execution is acceptance evidence. No production DB,
installed KaneoPilot, provider credentials, or business worktree is used.
"""
from __future__ import annotations

import argparse
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
    "macos/Flutter/ephemeral", "macos/Pods", "android/.gradle",
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


def run(argv, *, cwd, env, evidence, label, timeout=900, check=True):
    log = evidence / (label + ".log")
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output,
                                stderr=subprocess.STDOUT, text=True, timeout=timeout)
    record = {"argv": [str(arg) for arg in argv], "exit_code": result.returncode,
              "log": log.name, "log_sha256": sha256(log)}
    write_json(evidence / (label + ".json"), record)
    if result.returncode:
        print(log.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
    if check:
        require(result.returncode == 0, label + " failed; see " + log.name)
    return log.read_text(encoding="utf-8")


def restricted_environment(base):
    """Keep cloud control signals out of the actual restricted tool process."""
    return {key: value for key, value in base.items() if key not in BOT_DETECTOR_SIGNALS}


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
    run([str(flutter), "build", "apk", "--debug", "--no-pub"], cwd=app, env=env,
        evidence=evidence, label="online-warm-android", timeout=1800)
    require((app / GENERATED_PACKAGE).is_file(), "Warm build did not generate the SwiftPM package")
    commit_fixture(app)
    reset_generated(app)
    require(not (app / GENERATED_PACKAGE).exists(), "Platform fixture was not reset")
    return app


def sandbox_argv(environment, constants, spec, command):
    return [constants.codex_bin(), "sandbox", "-P", environment.permission_profile(spec),
            "-C", spec["cwd"], *environment.launcher_config(spec), "--", *command]


def make_spec(app, flutter, plan, adapter, environment, contract, util, *, suffix):
    binary = shlex.quote(str(flutter))
    commands = [binary + " analyze --no-pub", binary + " test --no-pub",
                "LANG=en_US.UTF-8 " + binary + " build macos --debug --no-pub",
                binary + " build apk --debug --no-pub"]
    spec = {"v": 1, "backend": "codex", "cwd": str(app), "repository": str(app),
            "run_id": "fixture-" + suffix + "-" + uuid.uuid4().hex,
            "task_id": "fixture-codex-platforms", "request_id": "fixture-request",
            "config": {"backend": "codex", "model": "no-model-is-called"},
            "worktree_mode": "branch", "plan_path": str(plan), "plan_sha256": util.sha256_file(plan),
            "completion_policy": "command_verification", "instruction": "Isolated platform fixture; no model.",
            "required_verification_commands": commands}
    spec["sandbox_writable_roots"] = adapter.verification_writable_roots(commands, app)
    environment.freeze(spec)
    require(spec.get("tool_environment", {}).get("v") == 2, "Fixture requires the repaired v2 environment")
    require(spec["tool_environment"]["environment"].get("BOT") == "true", "Cold Flutter bot detection is not frozen")
    contract.freeze_spec(spec, requirements_sha256="a" * 64)
    contract.validate_spec(spec, starting=True)
    return spec


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
    report = {"v": 1, "status": "failed", "source_sha": git(REPOSITORY, "rev-parse", "HEAD"),
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
        version = json.loads(run([str(flutter), "--version", "--machine"], cwd=root, env=env,
                                 evidence=evidence, label="tool-flutter-version"))
        require(version["frameworkVersion"] == FLUTTER_VERSION
                and version["dartSdkVersion"] == DART_VERSION, "Flutter/Dart version differs")
        codex = shutil.which("codex")
        require(codex, "Codex CLI is unavailable")
        codex_version = run([codex, "--version"], cwd=root, env=env, evidence=evidence, label="tool-codex-version")
        require(codex_version.strip() == "codex-cli " + CODEX_VERSION, "Codex CLI version differs")
        report["tools"] = {"flutter": version, "codex": codex_version.strip(), "python": sys.version,
                           "macos": platform.mac_ver(), "machine": platform.machine()}
        run([str(flutter), "precache", "--android", "--macos"], cwd=root, env=env,
            evidence=evidence, label="online-precache", timeout=1800)
        app = build_fixture(flutter, root, evidence, env)
        # Import only after the cloud guard and isolated cache environment exist.
        sys.path.insert(0, str(REPOSITORY / "core"))
        from kanpilot import codex_environment as environment, codex_execution as adapter
        from kanpilot import constants, execution_contract as contract, git_delivery, util
        plan = evidence / "frozen-plan.md"
        plan.write_text("Disposable native platform verification fixture.\n", encoding="utf-8")
        previous = dict(os.environ)
        os.environ.update(env)
        try:
            # The script's guard and online warm phase retain normal Actions
            # environment. All restricted preflights and native commands use
            # this scrubbed base, with BOT supplied only by the frozen v2 spec.
            restricted_base = restricted_environment(env)
            spec = make_spec(app, flutter, plan, adapter, environment, contract, util, suffix="cold")
            write_json(evidence / "spec.json", spec)
            private_config = Path(spec["tool_environment"]["environment"]["XDG_CONFIG_HOME"])
            require(not private_config.exists(), "First Flutter invocation is not a cold configuration")
            before = git_delivery.source_state_sha256(app)
            lock_before = sha256(app / "pubspec.lock")
            child_env = environment.child_env(spec, restricted_base)
            bot_environment = run(sandbox_argv(environment, constants, spec,
                                              without_ci_signals([sys.executable, "-c", _BOT_ENV_PROBE,
                                                                  json.dumps(BOT_DETECTOR_SIGNALS)])),
                                  cwd=app, env=child_env, evidence=evidence, label="cold-bot-environment")
            report["bot_detector"]["actual_sandbox_environment"] = json.loads(bot_environment)
            require(not private_config.exists(), "Environment probe unexpectedly warmed Flutter configuration")
            receipt = environment.preflight(spec, restricted_base)
            write_json(evidence / "cold-preflight.json", receipt)
            require(receipt["status"] == "passed", "Cold environment preparation failed")
            require((app / GENERATED_PACKAGE).is_file(), "Offline preparation did not generate the SwiftPM package")
            require("fixture_plugin" in (app / GENERATED_PACKAGE).read_text(), "Generated package omitted the plugin")
            require(sha256(app / "pubspec.lock") == lock_before, "Offline preparation changed the locked dependencies")
            require(git_delivery.source_state_sha256(app) == before, "Preparation changed fixture source")
            private_pub = Path(spec["tool_environment"]["environment"]["PUB_CACHE"])
            hosted = private_pub / "hosted"
            cached_git = private_pub / "git"
            require(hosted.is_dir() and any(hosted.glob("*/collection-1.19.1")),
                    "Private cache did not seed the locked hosted package")
            require(cached_git.is_dir() and any(cached_git.glob("fixture_git-*")),
                    "Private cache did not seed the locked Git checkout")
            report["private_dependency_cache"] = {"hosted_collection": True, "git_checkout": True}
            report["checks"].append("cold_offline_preparation_and_generated_swift_package")
            child_env = environment.child_env(spec, restricted_base)
            sentinel = root / "readonly-sentinel"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            host_config = Path.home() / ".config/kanpilot-codex-platform-fixture"
            host_config.mkdir(parents=True, exist_ok=True)
            host_sentinel = host_config / "sentinel"
            host_sentinel.write_text("unchanged\n", encoding="utf-8")
            targets = {"sdk_source_denied": str(flutter.parent.parent / "packages/flutter_tools/lib/src/base/bot_detector.dart"),
                       "host_config_denied": str(host_sentinel), "other_directory_denied": str(sentinel)}
            output = run(sandbox_argv(environment, constants, spec,
                                     without_ci_signals([sys.executable, "-c", _PERMISSION_PROBE, json.dumps(targets)])),
                         cwd=app, env=child_env, evidence=evidence, label="sandbox-denials")
            report["permission_checks"] = json.loads(output)
            report["checks"].append("readonly_boundaries_and_preflight_network_denials")
            for index, command in enumerate(spec["required_verification_commands"], 1):
                contract.validate_spec(spec)
                run(sandbox_argv(environment, constants, spec, without_ci_signals(["/bin/sh", "-c", command])),
                    cwd=app, env=child_env, evidence=evidence, label="restricted-verify-" + str(index), timeout=1800)
            require(git_delivery.source_state_sha256(app) == before, "Verification changed fixture source")
            report["checks"].append("all_four_frozen_native_commands_exit_zero")
            products = [app / "build/macos/Build/Products/Debug/fixture_app.app/Contents/MacOS/fixture_app",
                        app / "build/app/outputs/flutter-apk/app-debug.apk"]
            report["products"] = [{"path": str(path.relative_to(app)), "bytes": path.stat().st_size,
                                    "sha256": sha256(path)} for path in products]
            # Counterexample: use the same permission profile and already valid
            # project, but a second empty config and no BotDetector signal. A
            # nonzero result alone is insufficient: keep the exact metadata
            # address plus its denial from the native sandbox log.
            no_bot_config = private_config.parent / "no-bot-config"
            require(not no_bot_config.exists(), "No-BOT counterexample configuration is already warm")
            no_bot_config.mkdir()
            no_bot_command = without_ci_signals(
                ["XDG_CONFIG_HOME=" + str(no_bot_config), str(flutter), "test", "--no-pub", "--verbose"],
                remove_bot=True)
            no_bot_argv = sandbox_argv(environment, constants, spec, no_bot_command)
            no_bot_argv.insert(no_bot_argv.index("--"), "--log-denials")
            no_bot_output = run(no_bot_argv, cwd=app, env=child_env, evidence=evidence,
                                label="cold-without-bot", timeout=900, check=False)
            denied = metadata_denials(no_bot_output)
            require(bool(denied), "No-BOT counterexample did not capture a specific 169.254.169.254 denial")
            report["bot_detector"]["without_bot_metadata_denials"] = denied
            report["checks"].append("no_bot_cold_counterexample_attempts_denied_metadata")
            # Reuse the exact frozen run, as worker resume does, and force the
            # platform preparation to repair missing generated files once more.
            shutil.rmtree(app / "macos/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage")
            resumed = environment.preflight(spec, restricted_base)
            write_json(evidence / "resume-preflight.json", resumed)
            require(resumed["status"] == "passed" and (app / GENERATED_PACKAGE).is_file(),
                    "Resume did not restore the platform-generated package")
            report["checks"].append("same_frozen_resume_rechecks_platform_preparation")
            empty_gradle = root / "missing-gradle-cache"
            empty_gradle.mkdir()
            os.environ["GRADLE_USER_HOME"] = str(empty_gradle)
            missing_env = dict(restricted_base, GRADLE_USER_HOME=str(empty_gradle))
            try:
                missing = make_spec(app, flutter, plan, adapter, environment, contract, util, suffix="missing-cache")
                environment.preflight(missing, missing_env)
            except util.KanpilotError as exc:
                require(exc.code == "codex_environment_unavailable" and "Gradle" in str(exc),
                        "Missing Gradle cache did not produce a specific environment failure")
                report["missing_cache"] = {"code": exc.code, "detail": str(exc), "model_calls": 0}
            else:
                raise AssertionError("Missing Gradle cache was admitted")
            report["checks"].append("missing_gradle_cache_rejected_before_model")
            report["source_snapshot_before"] = before
            report["source_snapshot_after"] = git_delivery.source_state_sha256(app)
            require(report["source_snapshot_after"] == before, "Final fixture source differs")
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
