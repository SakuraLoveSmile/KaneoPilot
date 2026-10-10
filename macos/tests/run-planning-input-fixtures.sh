#!/bin/bash
# Compile the actual material preparation state without launching core or UI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-planning-fixtures.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT
python3 - "$ROOT" "$FIXTURE" <<'PY'
from pathlib import Path
import sys
root, fixture = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/Views/PlanningView.swift').read_text()
state = source.split('// BEGIN PLANNING PREPARATION STATE')[1].split('// END PLANNING PREPARATION STATE')[0]
(fixture / 'Preparation.swift').write_text('import Foundation\n' + state)
PY
swiftc -O "$ROOT/macos/KaneoPilot/Core/Models.swift" \
  "$ROOT/macos/tests/LabelsStub.swift" "$FIXTURE/Preparation.swift" \
  "$ROOT/macos/tests/planning_input_main.swift" -o "$FIXTURE/fixture"
"$FIXTURE/fixture"
