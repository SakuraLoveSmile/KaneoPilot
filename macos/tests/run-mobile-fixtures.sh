#!/bin/bash
# Real MobileServiceManager with FakeCore + owned non-network fixture child only.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_ARCH="$(uname -m)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-mobile-fixtures.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target "${TASK_ARCH}-apple-macos14" -O \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/MobileModels.swift" "$TASK_ROOT/macos/KaneoPilot/Core/MobileServiceManager.swift" \
  "$TASK_ROOT/macos/tests/mobile_service_main.swift" -o "$TASK_FIXTURE/fixture"
mkdir -p "$TASK_FIXTURE/simulation"
cat > "$TASK_FIXTURE/simulation/state.json" <<'JSON'
{"mobile_access":{"bind_host":"192.0.2.10","devices":[{"device_id":"fixture-android","name":"隔离安卓手机","created_at":"2026-10-21T00:00:00Z","revoked_at":null}]}}
JSON
KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py" \
  KANPILOT_FAKE_STATE="$TASK_FIXTURE/simulation/state.json" \
  KANPILOT_FAKE_COMMAND_LOG="$TASK_FIXTURE/simulation/commands.jsonl" \
  "$TASK_FIXTURE/fixture" simulation
for mode in ready failure loopback-host delayed-ready startup-timeout; do
  mkdir -p "$TASK_FIXTURE/$mode"
  env -u KANPILOT_FAKE_STATE \
    KANPILOT_CORE="$TASK_ROOT/macos/tests/mobile_fixture_core.py" \
    KANPILOT_MOBILE_FIXTURE_ROOT="$TASK_FIXTURE/$mode" \
    KANPILOT_MOBILE_FIXTURE_MODE="$mode" \
    KANPILOT_MOBILE_FIXTURE_LAN_FIRST="$([ "$mode" = loopback-host ] && echo 1 || echo 0)" \
    "$TASK_FIXTURE/fixture" "$mode"
done
python3 - "$TASK_FIXTURE" <<'PY'
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
commands = [json.loads(line)['command'] for line in (root/'simulation/commands.jsonl').read_text().splitlines()]
assert 'mobile-serve' not in commands, 'FakeCore must not start listener'
for mode in ('ready', 'loopback-host', 'delayed-ready', 'startup-timeout'):
    pid = int((root/mode/'service-started').read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f'owned child survived manager stop: {mode}')
print('FakeCore no-listener and owned-child shutdown: passed')
PY
