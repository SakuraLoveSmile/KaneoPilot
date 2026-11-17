#!/bin/bash
# Actual Goal blocker store with isolated command closures; no app, DB or network.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
sh "$TASK_ROOT/scripts/require_actions.sh"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-goal-blocker.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -O -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Views/GoalBlockerStore.swift" \
  "$TASK_ROOT/macos/tests/goal_blocker_main.swift" -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
