#!/bin/bash
# Actual input State/Store publishers; no UI snapshots, production roots or remote calls.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-search-input.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT
export KANPILOT_SESSION_DIAGNOSTICS=0
export KANPILOT_APP_SUPPORT="$FIXTURE/support" KANPILOT_DB="$FIXTURE/core.db"
export KANPILOT_LEGACY_ROOT="$FIXTURE/legacy" KANPILOT_PLAN_ROOT="$FIXTURE/plans"
export KANPILOT_REPOS_ROOT="$FIXTURE/repos" KANPILOT_REPORTS_ROOT="$FIXTURE/reports"
export KANEO_CREDENTIALS_PATH="$FIXTURE/credentials.json"
export KANPILOT_CORE="$ROOT/macos/FakeCore/kanpilot-fake.py" KANPILOT_FAKE_STATE="$FIXTURE/state.json"
printf '{}\n' > "$KANEO_CREDENTIALS_PATH"
swiftc -O -whole-module-optimization -parse-as-library \
 "$ROOT/macos/KaneoPilot/Core/Models.swift" "$HERE/LabelsStub.swift" \
 "$ROOT/macos/KaneoPilot/Core/CoreClient.swift" "$ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
 "$ROOT/macos/KaneoPilot/Views/SessionReaderStore.swift" \
 "$ROOT/macos/KaneoPilot/Views/SessionMessagePresentation.swift" \
 "$HERE/session_search_input_main.swift" -o "$FIXTURE/fixture"
"$FIXTURE/fixture"
