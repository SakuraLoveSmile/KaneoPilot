#!/bin/bash
# Versioned release metadata, bounded package validation, preferences, and safe install handoff.
# The fixture only writes a temporary cache and uses injected services; no DB/model/network.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$TASK_ROOT/scripts/require_actions.sh"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-software-update.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target "$(uname -m)-apple-macos14" -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdate.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdateManager.swift" \
  "$TASK_ROOT/macos/tests/software_update_main.swift" \
  -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
