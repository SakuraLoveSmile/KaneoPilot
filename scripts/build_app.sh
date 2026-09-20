#!/bin/bash
# Build KaneoPilot.app into dist/ — xcodebuild + bundle the python core into Resources.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJ="$ROOT/macos/KaneoPilot.xcodeproj"
SCHEME="KaneoPilot"
CONFIG="${CONFIG:-Release}"
OUT="$ROOT/dist"
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
cp -R "$ROOT/core/kanpilot.py" "$ROOT/core/kanpilot" "$ROOT/core/_vendor" "$RES/" 2>/dev/null || true
# fallback: single-file layout
[ -f "$ROOT/core/kanpilot.py" ] && cp "$ROOT/core/kanpilot.py" "$RES/"

echo "== done: $APP_DST =="
