#!/bin/bash
# Launch only the dedicated acceptance bundle. All core/state roots are temporary.
# Build example:
# xcodebuild -project macos/KaneoPilot.xcodeproj -scheme KaneoPilot -configuration Debug \
#   -derivedDataPath /tmp/kp-mobile-ui CODE_SIGNING_ALLOWED=NO \
#   PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.mobile-ui-check build
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_APP="${1:?Usage: run-mobile-ui-preview.sh /tmp/.../KaneoPilot.app [900x600|1180x760]}"
TASK_SIZE="${2:-1180x760}"
TASK_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$TASK_APP/Contents/Info.plist")"
if [ "$TASK_BUNDLE_ID" != local.kaneopilot.mobile-ui-check ]; then
  echo 'Only PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.mobile-ui-check is allowed.' >&2
  exit 2
fi
TASK_CASE="$(mktemp -d "${TMPDIR:-/tmp}/kp-mobile-ui.XXXXXX")"
mkdir -p "$TASK_CASE/home" "$TASK_CASE/support" "$TASK_CASE/legacy" "$TASK_CASE/plans" "$TASK_CASE/repos" "$TASK_CASE/reports"
printf '{}\n' > "$TASK_CASE/credentials.json"
cat > "$TASK_CASE/state.json" <<'JSON'
{"mobile_access":{"enabled":false,"bind_host":"192.0.2.10","port":47831,"devices":[{"device_id":"fixture-android","name":"隔离安卓手机","created_at":"2026-10-21T00:00:00Z","revoked_at":null}]}}
JSON
echo "Isolated mobile UI fixture: $TASK_CASE"
echo 'Navigate 设置 → 连接 → 手机连接. FakeCore does not listen on any network port.'
exec env HOME="$TASK_CASE/home" \
  KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py" \
  KANPILOT_APP_SUPPORT="$TASK_CASE/support" KANPILOT_DB="$TASK_CASE/core.db" \
  KANPILOT_LEGACY_ROOT="$TASK_CASE/legacy" KANPILOT_PLAN_ROOT="$TASK_CASE/plans" \
  KANPILOT_REPOS_ROOT="$TASK_CASE/repos" KANPILOT_REPORTS_ROOT="$TASK_CASE/reports" \
  KANEO_CREDENTIALS_PATH="$TASK_CASE/credentials.json" \
  KANPILOT_FAKE_STATE="$TASK_CASE/state.json" KANPILOT_FAKE_COMMAND_LOG="$TASK_CASE/commands.jsonl" \
  KANPILOT_WINDOW_SIZE="$TASK_SIZE" \
  "$TASK_APP/Contents/MacOS/KaneoPilot"
