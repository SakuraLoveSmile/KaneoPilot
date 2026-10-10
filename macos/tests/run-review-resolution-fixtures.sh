#!/bin/bash
# Real model/store with isolated closures: no app, core, DB, network or model calls.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-review-resolution-fixtures.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
python3 - "$TASK_ROOT" "$TASK_FIXTURE" <<'PY'
from pathlib import Path
import sys
root, fixture = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/AppViewModel.swift').read_text()
state = source.split('// BEGIN REVIEW RESOLUTION STORE')[1].split('// END REVIEW RESOLUTION STORE')[0]
error_extension = source.split('extension Error {', 1)[1]
client = (root / 'macos/KaneoPilot/Core/CoreClient.swift').read_text()
errors = client.split('enum CoreError:')[1].split('/// `kanpilot` CLI 客户端')[0]
(fixture / 'ReviewState.swift').write_text('import Foundation\nimport Combine\n' + state
    + '\nenum CoreError:' + errors + '\nextension Error {' + error_extension)
PY
swiftc -O "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" \
  "$TASK_ROOT/macos/tests/LabelsStub.swift" "$TASK_FIXTURE/ReviewState.swift" \
  "$TASK_ROOT/macos/tests/review_resolution_main.swift" -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
