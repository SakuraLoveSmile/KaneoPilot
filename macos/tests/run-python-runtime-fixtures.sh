#!/bin/bash
# The app's Python resolver and how CoreClient launches the core with it: resolution order, no PATH
# fallback, sanitized environment, the prepared interpreter started directly. No network, DB or real core.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$TASK_ROOT/scripts/require_actions.sh"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-python-runtime-fixtures.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target "$(uname -m)-apple-macos14" -O \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$TASK_ROOT/macos/tests/python_runtime_main.swift" -o "$TASK_FIXTURE/fixture"

mkdir -p "$TASK_FIXTURE/resolve" "$TASK_FIXTURE/launch/record" "$TASK_FIXTURE/missing/record" "$TASK_FIXTURE/missing/bin"
# Stands in for the interpreter: records how it was started, then answers like the core.
cat > "$TASK_FIXTURE/launch/fake-python" <<'SH'
#!/bin/sh
printf '%s\n' "$@" > "$KANPILOT_FIXTURE_RECORD/argv-$2"
env > "$KANPILOT_FIXTURE_RECORD/env-$2"
printf '{"ok":true}\n'
SH
chmod +x "$TASK_FIXTURE/launch/fake-python"
# A python3 that happens to be first on PATH: the app must never start it.
cat > "$TASK_FIXTURE/missing/bin/python3" <<'SH'
#!/bin/sh
: > "$KANPILOT_FIXTURE_RECORD/path-python-ran"
printf '{"ok":true}\n'
SH
cat > "$TASK_FIXTURE/missing/core-shim" <<'SH'
#!/bin/sh
printf '{"shim":true}\n'
SH
chmod +x "$TASK_FIXTURE/missing/bin/python3" "$TASK_FIXTURE/missing/core-shim"

"$TASK_FIXTURE/fixture" resolve "$TASK_FIXTURE/resolve"
env KANPILOT_PYTHON="$TASK_FIXTURE/launch/fake-python" KANPILOT_FIXTURE_RECORD="$TASK_FIXTURE/launch/record" \
  PYTHONPATH=/nonexistent/pythonpath PYTHONHOME=/nonexistent/pythonhome PYTHONSTARTUP=/nonexistent/startup.py \
  KANPILOT_SCHEDULER_PID=999999 \
  "$TASK_FIXTURE/fixture" launch "$TASK_FIXTURE/launch"
env KANPILOT_PYTHON="$TASK_FIXTURE/missing/does-not-exist" KANPILOT_FIXTURE_RECORD="$TASK_FIXTURE/missing/record" \
  PATH="$TASK_FIXTURE/missing/bin:$PATH" \
  "$TASK_FIXTURE/fixture" missing "$TASK_FIXTURE/missing"
