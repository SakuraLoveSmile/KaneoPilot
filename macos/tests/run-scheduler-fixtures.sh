#!/bin/bash
# 仅编译纯决策与真实 DTO；无核心调用、模型、远端服务或登录项写入。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-scheduler-fixtures.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT
swiftc -O "$ROOT/macos/KaneoPilot/Core/Models.swift" \
  "$ROOT/macos/KaneoPilot/Core/SchedulerDecision.swift" \
  "$HERE/LabelsStub.swift" "$HERE/scheduler_main.swift" -o "$BUILD/fixture"
"$BUILD/fixture"
