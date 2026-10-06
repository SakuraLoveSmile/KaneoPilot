#!/bin/bash
# 编译真实 Scheduler，临时假 core 与独立 Python worker；不访问正式 DB/模型/网络。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-owner-build.XXXXXX")"
CASE=""
cleanup() {
  if [ -n "$CASE" ]; then
    if [ -f "$CASE/core.py" ]; then
      KANPILOT_OWNER_FIXTURE_ROOT="$CASE" python3 "$CASE/core.py" cleanup
    fi
    rm -rf "$CASE"
  fi
  rm -rf "$BUILD"
}
trap cleanup EXIT
swiftc -target arm64-apple-macos13 \
  "$ROOT/macos/KaneoPilot/Core/Models.swift" "$HERE/LabelsStub.swift" \
  "$ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$ROOT/macos/KaneoPilot/Core/SchedulerDecision.swift" "$ROOT/macos/KaneoPilot/Core/ExecutorScheduler.swift" \
  "$HERE/owner_lifecycle_main.swift" -o "$BUILD/fixture"
for mode in detached running start-stop start-failure start-recovery start-periodic-recovery close-failure status-failure disabled-maintenance; do
  CASE="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-owner-case.XXXXXX")"
  cp "$HERE/owner_fixture_core.py" "$CASE/core.py"
  mkdir "$CASE/kanpilot"
  touch "$CASE/kanpilot/__init__.py"
  cat > "$CASE/kanpilot/executor_runner.py" <<'PY'
import os, runpy, sys
sys.argv = [os.environ["KANPILOT_OWNER_FIXTURE_ROOT"] + "/core.py", "stage"]
runpy.run_path(sys.argv[0], run_name="__main__")
PY
  KANPILOT_OWNER_FIXTURE_ROOT="$CASE" "$BUILD/fixture" "$mode"
  KANPILOT_OWNER_FIXTURE_ROOT="$CASE" python3 "$CASE/core.py" cleanup
  rm -rf "$CASE"
  CASE=""
done
CASE="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-owner-real.XXXXXX")"
# 真实 CLI 的四次仅本地 settings/status 调用；空数据库，无 runner、模型或远端写。
KANPILOT_OWNER_FIXTURE_ROOT="$CASE" KANPILOT_OWNER_REAL_CORE="$ROOT/core/kanpilot.py" \
  KANPILOT_DB="$CASE/kanpilot.sqlite" KANPILOT_APP_SUPPORT="$CASE/app-support" \
  KANPILOT_PI_HOME="$CASE/pi" "$BUILD/fixture" real-core
rm -rf "$CASE"
CASE=""
