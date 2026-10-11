#!/bin/bash
# Standalone UI acceptance bundle + FakeCore + independent roots. No installation.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_APP="${1:?Usage: launch-engine-ui-check.sh /tmp/.../KaneoPilot.app [900x600|1180x760]}"
TASK_SIZE="${2:-1180x760}"
TASK_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$TASK_APP/Contents/Info.plist")"
if [ "$TASK_BUNDLE_ID" != local.kaneopilot.ui-engine-check ]; then
  echo 'Use a standalone bundle built with PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.ui-engine-check' >&2
  exit 2
fi
TASK_CASE="$(mktemp -d "${TMPDIR:-/tmp}/kp-engine-ui.XXXXXX")"
mkdir -p "$TASK_CASE/home" "$TASK_CASE/support" "$TASK_CASE/legacy" "$TASK_CASE/plans" "$TASK_CASE/repos" "$TASK_CASE/reports"
printf '{}\n' > "$TASK_CASE/credentials.json"
echo "Isolated UI fixture: $TASK_CASE"
exec env HOME="$TASK_CASE/home" \
  KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py" \
  KANPILOT_APP_SUPPORT="$TASK_CASE/support" KANPILOT_DB="$TASK_CASE/core.db" \
  KANPILOT_LEGACY_ROOT="$TASK_CASE/legacy" KANPILOT_PLAN_ROOT="$TASK_CASE/plans" \
  KANPILOT_REPOS_ROOT="$TASK_CASE/repos" KANPILOT_REPORTS_ROOT="$TASK_CASE/reports" \
  KANEO_CREDENTIALS_PATH="$TASK_CASE/credentials.json" \
  KANPILOT_FAKE_STATE="$TASK_CASE/state.json" KANPILOT_FAKE_COMMAND_LOG="$TASK_CASE/commands.jsonl" \
  KANPILOT_WINDOW_SIZE="$TASK_SIZE" \
  "$TASK_APP/Contents/MacOS/KaneoPilot"
