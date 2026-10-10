#!/bin/bash
# Compile the actual Store and presentation; exercise CoreClient -> isolated FakeCore.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-session-store.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT
export PYTHONDONTWRITEBYTECODE=1
export KANPILOT_APP_SUPPORT="$FIXTURE/support" KANPILOT_DB="$FIXTURE/core.db"
export KANPILOT_LEGACY_ROOT="$FIXTURE/legacy" KANPILOT_PLAN_ROOT="$FIXTURE/plans"
export KANPILOT_REPOS_ROOT="$FIXTURE/repos" KANPILOT_REPORTS_ROOT="$FIXTURE/reports"
export KANEO_CREDENTIALS_PATH="$FIXTURE/credentials.json"
export KANPILOT_CORE="$ROOT/macos/FakeCore/kanpilot-fake.py"
export KANPILOT_FAKE_STATE="$FIXTURE/state.json" KANPILOT_FAKE_REQUEST_LOG="$FIXTURE/requests.jsonl"
export KANPILOT_FAKE_LONG_SESSION=task-music-006 KANPILOT_FAKE_LONG_SESSION_SHAPE=wrapped_json
export KANPILOT_FAKE_LONG_SESSION_COUNT="${KANPILOT_TEST_SESSION_COUNT:-400}"
export KANPILOT_FAKE_LONG_SESSION_BYTES="${KANPILOT_TEST_SESSION_BYTES:-4096}"
mkdir -p "$KANPILOT_APP_SUPPORT" "$KANPILOT_LEGACY_ROOT" "$KANPILOT_PLAN_ROOT" "$KANPILOT_REPOS_ROOT" "$KANPILOT_REPORTS_ROOT"
printf '{}\n' > "$KANEO_CREDENTIALS_PATH"
swiftc -O -parse-as-library \
 "$ROOT/macos/KaneoPilot/Core/Models.swift" "$HERE/LabelsStub.swift" \
 "$ROOT/macos/KaneoPilot/Core/CoreClient.swift" "$ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
 "$ROOT/macos/KaneoPilot/Views/SessionReaderStore.swift" \
 "$ROOT/macos/KaneoPilot/Views/SessionMessagePresentation.swift" \
 "$HERE/session_reader_main.swift" -o "$FIXTURE/fixture"
"$FIXTURE/fixture"
