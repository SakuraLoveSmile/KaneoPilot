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
modules = ("pi_desktop_backend", "pi_desktop_worker", "exec_config", "cmd_dispatch", "cli")
for name in modules:
    relative = Path("kanpilot") / (name + ".py")
    expected = hashlib.sha256((source / relative).read_bytes()).digest()
    actual = hashlib.sha256((bundled / relative).read_bytes()).digest()
    if actual != expected:
        raise SystemExit("Bundled core differs from source: " + str(relative))
    module = importlib.import_module("kanpilot." + name)
    if not Path(module.__file__).resolve().is_relative_to(bundled.resolve()):
        raise SystemExit("Core imported outside app bundle: " + name)
print("Desktop backend imports and source hashes verified")
PY

echo "== done: $APP_DST =="
