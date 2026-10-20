#!/bin/bash
# Build KaneoPilot.app into dist/ — xcodebuild + bundle the python core into Resources.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$ROOT/macos/KaneoPilot.xcodeproj"
SCHEME="KaneoPilot"
CONFIG="${CONFIG:-Release}"
OUT="${APP_OUTPUT_DIR:-$ROOT/dist}"
DERIVED="$ROOT/build/DerivedData"

echo "== xcodebuild ($CONFIG) =="
xcodebuild -project "$PROJ" -scheme "$SCHEME" -configuration "$CONFIG" \
  -derivedDataPath "$DERIVED" CODE_SIGNING_ALLOWED=NO build

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
cp "$ROOT/docs/protocol.md" "$ROOT/docs/automation-contract.md" "$ROOT/docs/release-contract.md" "$APP_DST/Contents/Resources/docs/"
find "$RES" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
# fallback: single-file layout
[ -f "$ROOT/core/kanpilot.py" ] && cp "$ROOT/core/kanpilot.py" "$RES/"

echo "== verify executor_prompts / agents bundled =="
[ -d "$RES/executor_prompts" ] || { echo "ERROR: executor_prompts 未打入 Resources" >&2; exit 1; }
[ -d "$RES/agents" ] || { echo "ERROR: agents 未打入 Resources" >&2; exit 1; }

echo "== verify bundled desktop backend and source hashes =="
PYTHONDONTWRITEBYTECODE=1 python3 - "$ROOT/core" "$RES" <<'PY'
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
    "pi_desktop_controller", "pi_desktop_review", "plus_interaction",
    "exec_config", "planner_config", "review_config", "cmd_config", "project_config",
    "cmd_dispatch", "executor_runner", "planner_runner", "cli",
    "mcp_scopes", "mcp_server", "mcp_tools", "mcp_stdio", "mcp_client",
    "harness_plus_adapter", "runtime_operations", "runtime_client",
    "auto_dispatch", "auto_monitor", "verify_evidence", "automation_policy", "auto_intake",
    "git_delivery", "cmd_delivery", "gh_client", "cmd_delivery_pr", "pr_auto_merge", "delivery_runner",
    "parent_closeout", "parent_closeout_github",
    "release_delivery", "release_github",
    "claude_cli", "qoder_cli", "claude_planner", "review_settle", "review_repair", "repair_prompt", "task_ai_action", "cli_review",
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
for name in ("protocol.md", "automation-contract.md", "release-contract.md"):
    if (source.parent / "docs" / name).read_bytes() != (bundled.parent / "docs" / name).read_bytes():
        raise SystemExit("Bundled contract differs from source: " + name)
print("Desktop and automation backend imports and source hashes verified")
PY

echo "== sign local application bundle =="
codesign --force --deep --sign - "$APP_DST"
codesign --verify --deep --strict "$APP_DST"

echo "== done: $APP_DST =="
