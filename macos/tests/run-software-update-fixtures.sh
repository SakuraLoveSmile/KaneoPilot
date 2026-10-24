#!/bin/bash
# Versioned release metadata, bounded package validation, preferences, and safe install handoff.
# The fixture only writes a temporary cache and uses injected services; no DB/model/network.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$TASK_ROOT/scripts/require_actions.sh"
TASK_FIXTURE_OUTPUT="${KANPILOT_UPDATE_FIXTURE_OUTPUT:-}"
if [ -n "$TASK_FIXTURE_OUTPUT" ]; then
  case "$TASK_FIXTURE_OUTPUT" in
    /*) ;;
    *) printf '%s\n' 'KANPILOT_UPDATE_FIXTURE_OUTPUT must be an absolute directory' >&2; exit 1 ;;
  esac
fi
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-software-update.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target "$(uname -m)-apple-macos14" -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdate.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdateManager.swift" \
  "$TASK_ROOT/macos/tests/software_update_main.swift" \
  -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
if [ -n "$TASK_FIXTURE_OUTPUT" ]; then
  mkdir -p "$TASK_FIXTURE_OUTPUT"
  cp -p "$TASK_FIXTURE/fixture" "$TASK_FIXTURE_OUTPUT/software-update-fixture"
fi
