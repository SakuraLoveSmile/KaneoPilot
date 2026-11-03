#!/bin/bash
# Explicit local development entry. Release and DMG publishing stay in Packages.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:---build}"
case "$MODE" in
    --build|--check|--preview) ;;
    *) echo 'Usage: bash scripts/build_local_macos.sh [--check|--build|--preview [1180x760|900x600|1440x900] [light|dark]]' >&2; exit 2 ;;
esac
export KANPILOT_LOCAL_BUILD=1
sh "$ROOT/scripts/require_actions.sh" --allow-local-macos
for tool in xcodebuild xcrun codesign python3; do
    command -v "$tool" >/dev/null || { echo "Missing $tool. Install Xcode and Python 3.9+." >&2; exit 1; }
done
xcodebuild -version
xcrun --sdk "${KANPILOT_MACOS_SDK:-macosx}" --show-sdk-path
if ! xcodebuild -checkFirstLaunchStatus; then
    echo 'Finish Xcode setup with: xcodebuild -runFirstLaunch' >&2
    exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ is required"; print(sys.version)'
[ "$MODE" != --check ] || exit 0

export LOCAL_BUILD_ROOT="${LOCAL_BUILD_ROOT:-$ROOT/build/local-macos}"
RUNTIME_INPUT="$LOCAL_BUILD_ROOT/python-runtime"
if [ ! -d "$RUNTIME_INPUT" ]; then
    python3 "$ROOT/scripts/fetch_python_runtime.py" --output "$RUNTIME_INPUT"
fi
# A previous download is reusable only for the exact checked-in lock and archive.
python3 - "$ROOT" "$RUNTIME_INPUT" <<'PY'
import hashlib, json, sys
from pathlib import Path
root, runtime = map(Path, sys.argv[1:])
lock = json.loads((root / 'scripts/python-runtime.lock.json').read_text())
if json.loads((runtime / 'runtime.json').read_text()) != lock:
    raise SystemExit('Cached Python runtime differs from the lock. Choose a fresh LOCAL_BUILD_ROOT.')
archive = runtime / lock['asset']
if archive.stat().st_size != lock['size'] or hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']:
    raise SystemExit('Cached Python archive failed verification. Choose a fresh LOCAL_BUILD_ROOT.')
PY
export PYTHON_RUNTIME_DIR="$RUNTIME_INPUT/unpacked/python"
if [ "$MODE" = --preview ]; then
    export KANPILOT_PYTHON="$PYTHON_RUNTIME_DIR/bin/python3"
    bash "$ROOT/scripts/preview_automation_ui.sh" "${2:-1180x760}" "${3:-light}"
else
    bash "$ROOT/scripts/build_app.sh"
fi
