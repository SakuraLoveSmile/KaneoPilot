#!/bin/bash
# Actual EngineSupervisor, explicit FakeCore-only recovery injection; no real DB or services.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-recovery-supervisor-fixtures.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target arm64-apple-macos14 -O \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/EngineSupervisor.swift" "$TASK_ROOT/macos/tests/recovery_supervisor_main.swift" \
  -o "$TASK_FIXTURE/fixture"
for mode in active global dispatch monitor; do
  mkdir -p "$TASK_FIXTURE/$mode"
  KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py" \
  KANPILOT_FAKE_STATE="$TASK_FIXTURE/$mode/state.json" \
  KANPILOT_FAKE_COMMAND_LOG="$TASK_FIXTURE/$mode/commands.jsonl" \
  "$TASK_FIXTURE/fixture" "$mode" -executorSchedulingEnabled NO
done
