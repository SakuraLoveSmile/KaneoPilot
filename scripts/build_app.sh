#!/bin/bash
# Build KaneoPilot.app on GitHub Actions and bundle the Python core into Resources.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sh "$ROOT/scripts/require_actions.sh"
PROJ="$ROOT/macos/KaneoPilot.xcodeproj"
SCHEME="KaneoPilot"
CONFIG="${CONFIG:-Release}"
OUT="${APP_OUTPUT_DIR:-${RUNNER_TEMP:?RUNNER_TEMP is required}/macos-build}"
DERIVED="${DERIVED_DATA_DIR:-${RUNNER_TEMP:?RUNNER_TEMP is required}/KaneoPilotDerivedData}"
ARCHS="${ARCHS:-arm64}"
# KaneoPilot ships Apple Silicon only (docs/build-release.md): refuse any other request before building.
[ "$ARCHS" = "arm64" ] || { echo "ERROR: KaneoPilot builds arm64 only, got ARCHS=$ARCHS" >&2; exit 1; }
BUILD_SETTINGS=("ARCHS=$ARCHS" "ONLY_ACTIVE_ARCH=NO" "CODE_SIGNING_ALLOWED=NO")
if [ -n "${VERSION_NAME:-}" ]; then BUILD_SETTINGS+=("MARKETING_VERSION=$VERSION_NAME"); fi
if [ -n "${BUILD_NUMBER:-}" ]; then BUILD_SETTINGS+=("CURRENT_PROJECT_VERSION=$BUILD_NUMBER"); fi

echo "== xcodebuild ($CONFIG) =="
xcodebuild -project "$PROJ" -scheme "$SCHEME" -configuration "$CONFIG" \
  -derivedDataPath "$DERIVED" "${BUILD_SETTINGS[@]}" build

APP_SRC="$DERIVED/Build/Products/$CONFIG/KaneoPilot.app"
APP_DST="$OUT/KaneoPilot.app"
mkdir -p "$OUT"
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"

echo "== bundle core into Resources =="
RES="$APP_DST/Contents/Resources/core"
mkdir -p "$RES"
cp -R "$ROOT/core/kanpilot.py" "$ROOT/core/kanpilot" "$ROOT/core/_vendor" "$RES/"
# v1.6：pi worker 为纯 Python（无桥接包）；执行端 prompt 与 agent.md 内置预设
# 随 kanpilot 包同层打入（executor_runner/pi_worker 运行时按 core_root() 定位）。
cp -R "$ROOT/core/executor_prompts" "$ROOT/core/agents" "$RES/"
# The rendered controller preamble references these exact bundled paths.
mkdir -p "$APP_DST/Contents/Resources/docs"
cp "$ROOT/docs/protocol.md" "$ROOT/docs/automation-contract.md" "$ROOT/docs/release-contract.md" "$ROOT/docs/harness-contract.md" "$APP_DST/Contents/Resources/docs/"
find "$RES" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
# fallback: single-file layout
[ -f "$ROOT/core/kanpilot.py" ] && cp "$ROOT/core/kanpilot.py" "$RES/"

echo "== verify executor_prompts / agents bundled =="
[ -d "$RES/executor_prompts" ] || { echo "ERROR: executor_prompts 未打入 Resources" >&2; exit 1; }
[ -d "$RES/agents" ] || { echo "ERROR: agents 未打入 Resources" >&2; exit 1; }

echo "== bundle the pinned Python runtime into Resources =="
# The one native file in core/_vendor is a PyYAML extension built for another Python; the bundled interpreter uses the pure-Python fallback.
find "$RES/_vendor" -name "*.so" -type f -delete
RUNTIME_SRC="${PYTHON_RUNTIME_DIR:?PYTHON_RUNTIME_DIR is required (scripts/fetch_python_runtime.py sets it)}"
RUNTIME_DST="$APP_DST/Contents/Resources/python"
[ -x "$RUNTIME_SRC/bin/python3" ] || { echo "ERROR: $RUNTIME_SRC has no bin/python3" >&2; exit 1; }
rm -rf "$RUNTIME_DST"
ditto "$RUNTIME_SRC" "$RUNTIME_DST"

echo "== trim the bundled runtime's unused and build-time files =="
# The App only ever runs `bin/python3` as a subprocess. Verified against the pinned
# `install_only_stripped` archive (docs/build-release.md): `otool -L bin/python3.13` does not
# reference libpython3.13.dylib, and across the runtime's nine Mach-O images the only one that
# names it is itself. C headers, the config-3.13-darwin build directory and _pyrepl are
# build-time or interactive-only. Every entry here was re-checked by importing the cloud smoke
# set and all 95 core modules with the trimmed runtime; removing them costs no capability the
# App or the core uses, and keeps every online update ~20 MB smaller. Anything that changes
# what the bundled interpreter can do belongs in a separate decision, not in this list.
TRIM_PATHS=(
  "lib/libpython3.13.dylib"
  "include"
  "lib/python3.13/config-3.13-darwin"
  "lib/python3.13/_pyrepl"
)
for relative in "${TRIM_PATHS[@]}"; do
  target="$RUNTIME_DST/$relative"
  [ -e "$target" ] || [ -L "$target" ] || { echo "ERROR: expected $relative in the bundled runtime" >&2; exit 1; }
  rm -rf "$target"
  echo "trimmed $relative"
done

python3 "$ROOT/scripts/prepare_python_runtime.py" "$RUNTIME_DST"
EXPECTED_PYTHON=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["python_version"])' "$ROOT/scripts/python-runtime.lock.json")
BUNDLED_PYTHON=$("$RUNTIME_DST/bin/python3" -I -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
[ "$BUNDLED_PYTHON" = "$EXPECTED_PYTHON" ] || { echo "ERROR: the bundled Python is $BUNDLED_PYTHON, the lock file says $EXPECTED_PYTHON" >&2; exit 1; }

echo "== bundle the Python runtime's license texts into Resources =="
# The runtime statically links OpenSSL, SQLite, libffi, expat, mpdecimal, bzip2, xz,
# ncurses, readline, libuuid and Tcl/Tk; a public release has to carry their notices
# (docs/build-release.md). The `install_only_stripped` archive itself only ships the PSF
# text, so the texts come from the same release's `full` archive and are checked in.
LICENSE_SRC="$ROOT/scripts/licenses/python"
LICENSE_DST="$APP_DST/Contents/Resources/licenses/python"
[ -d "$LICENSE_SRC" ] || { echo "ERROR: $LICENSE_SRC is missing (the Python runtime's license texts)" >&2; exit 1; }
rm -rf "$LICENSE_DST"
ditto "$LICENSE_SRC" "$LICENSE_DST"
LICENSE_COUNT=$(find "$LICENSE_DST" -type f | wc -l | tr -d ' ')
[ "$LICENSE_COUNT" -gt 0 ] || { echo "ERROR: no license text reached $LICENSE_DST" >&2; exit 1; }
echo "bundled $LICENSE_COUNT license texts into $LICENSE_DST"

echo "== verify bundled desktop backend and source hashes (with the bundled Python) =="
# -I keeps the runner's environment out; -B keeps bytecode out of the bundle, which is not signed yet but must stay as copied.
"$RUNTIME_DST/bin/python3" -I -B - "$ROOT/core" "$RES" <<'PY'
import hashlib
import importlib
import sys
from pathlib import Path

source, bundled = map(Path, sys.argv[1:])
sys.path[:0] = [str(bundled), str(bundled / "_vendor")]
# v4（T6）：计划/控制端/Review 三分支都是独立模块——缺任一个都必须让构建
# 失败，不能靠 `cp ... || true` 把核心打包缺失吞掉。
modules = (
    "pi_desktop_backend", "pi_desktop_worker", "pi_desktop_planner",
    "codex_planner", "codex_provider", "codex_execution", "codex_worker",
    "pi_desktop_controller", "pi_desktop_review", "plus_interaction",
    "exec_config", "planner_config", "review_config", "cmd_config", "project_config",
    "cmd_dispatch", "executor_runner", "planner_runner", "cli",
    "mcp_scopes", "mcp_server", "mcp_tools", "mcp_stdio", "mcp_client",
    "mobile_access", "mobile_server", "mobile_worker", "app_update_installer",
    "harness_plus_adapter", "runtime_operations", "runtime_client",
    "harnesses", "stage_contract", "stage_evidence", "execution_contract", "delivery_contract",
    "auto_dispatch", "auto_monitor", "verify_evidence", "automation_policy", "auto_intake",
    "git_delivery", "cmd_delivery", "gh_client", "cmd_delivery_pr", "pr_auto_merge", "delivery_runner",
    "parent_closeout", "parent_closeout_github",
    "release_delivery", "release_github", "release_dispatch_recovery", "release_verification", "release_verification_questions", "external_build_repair",
    "claude_cli", "qoder_cli", "claude_planner", "review_settle", "review_delivery", "review_repair", "repair_prompt", "task_ai_action", "task_ai_build_failure", "cli_review",
    "process_owner", "desktop_queue", "desktop_automation_control", "verification_recovery", "verification_checks", "verification_process", "review_snapshot", "review_resolution",
    "plan_questions", "plan_answers", "execution_questions",
    "task_workspace", "engine_claim_scope", "cmd_queue", "cmd_remote",
    "cmd_delivery", "derive", "planner_inputs", "constants",
    "engine.commands", "engine.verdict", "engine.loop", "engine.health",
    "engine.steps", "engine.decide", "engine.fallback", "engine.attention",
    "engine.ladder", "engine.rounds", "engine.runners",
)
for name in modules:
    relative = Path("kanpilot") / (name.replace(".", "/") + ".py")
    if not (source / relative).is_file():
        relative = Path("kanpilot") / name.replace(".", "/") / "__init__.py"
    expected = hashlib.sha256((source / relative).read_bytes()).digest()
    actual = hashlib.sha256((bundled / relative).read_bytes()).digest()
    if actual != expected:
        raise SystemExit("Bundled core differs from source: " + str(relative))
    module = importlib.import_module("kanpilot." + name)
    if not Path(module.__file__).resolve().is_relative_to(bundled.resolve()):
        raise SystemExit("Core imported outside app bundle: " + name)
for relative in (Path("executor_prompts/review_cli.md"), Path("executor_prompts/planner_prompt.md")):
    if hashlib.sha256((source / relative).read_bytes()).digest() != hashlib.sha256((bundled / relative).read_bytes()).digest():
        raise SystemExit("Bundled prompt differs from source: " + str(relative))
for name in ("protocol.md", "automation-contract.md", "release-contract.md", "harness-contract.md"):
    if (source.parent / "docs" / name).read_bytes() != (bundled.parent / "docs" / name).read_bytes():
        raise SystemExit("Bundled contract differs from source: " + name)
print("Desktop and automation backend imports and source hashes verified with Python", ".".join(map(str, sys.version_info[:3])))
PY

echo "== verify application architecture (arm64 only) =="
EXECUTABLE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP_DST/Contents/Info.plist")
lipo "$APP_DST/Contents/MacOS/$EXECUTABLE" -verify_arch arm64
[ "$(lipo -archs "$APP_DST/Contents/MacOS/$EXECUTABLE")" = "arm64" ] || { echo "ERROR: app executable is not arm64-only" >&2; exit 1; }

echo "== verify requested application version =="
if [ -n "${VERSION_NAME:-}" ]; then
    [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DST/Contents/Info.plist")" = "$VERSION_NAME" ] || { echo "ERROR: app version differs from VERSION_NAME" >&2; exit 1; }
fi
if [ -n "${BUILD_NUMBER:-}" ]; then
    [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP_DST/Contents/Info.plist")" = "$BUILD_NUMBER" ] || { echo "ERROR: app build differs from BUILD_NUMBER" >&2; exit 1; }
fi

echo "== sign local application bundle =="
codesign --force --deep --sign - "$APP_DST"
codesign --verify --deep --strict "$APP_DST"

echo "== done: $APP_DST =="
