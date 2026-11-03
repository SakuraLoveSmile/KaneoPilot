#!/bin/bash
# Build KaneoPilot.dmg on GitHub Actions using hdiutil.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sh "$ROOT/scripts/require_actions.sh"
OUT="${APP_OUTPUT_DIR:-${RUNNER_TEMP:?RUNNER_TEMP is required}/macos-build}"
APP="$OUT/KaneoPilot.app"
DMG="$OUT/${DMG_NAME:-KaneoPilot.dmg}"
VOLNAME="KaneoPilot"

# 1. Build .app if not present or rebuild
echo "== Building KaneoPilot.app =="
bash "$ROOT/scripts/build_app.sh"

if [ ! -d "$APP" ]; then
    echo "ERROR: $APP does not exist!" >&2
    exit 1
fi

echo "== Preparing DMG staging =="
STAGING=$(mktemp -d /tmp/kaneopilot_dmg.XXXXXX)
trap 'rm -rf "$STAGING"' EXIT

cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

echo "== Creating DMG =="
rm -f "$DMG"
# zlib-level=9: `man hdiutil` documents UDZO as defaulting to level 1 (fastest). The bundled
# Python runtime is ~81 MB of highly compressible bytecode and text, so the default level
# leaves most of the win on the table for every download of an online update.
hdiutil create \
    -volname "$VOLNAME" \
    -srcfolder "$STAGING" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG"

echo "== Sign local disk image =="
codesign --force --sign - "$DMG"
codesign --verify --strict "$DMG"

echo "== Verifying DMG =="
hdiutil imageinfo "$DMG" >/dev/null
hdiutil verify "$DMG" >/dev/null

echo "== Successfully generated: $DMG =="
ls -lh "$DMG"
