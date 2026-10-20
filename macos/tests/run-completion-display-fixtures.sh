#!/bin/bash
# Compile the actual presentation slice with real DTO models in an isolated directory.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-completion-display.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
python3 - "$TASK_ROOT" "$TASK_FIXTURE" <<'PY'
from pathlib import Path
import sys
root, fixture = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/Views/Components.swift').read_text()
start = source.index('struct StatusStyle {')
end = source.index('// MARK: - 状态徽章', start)
(fixture / 'DisplayStatus.swift').write_text('import SwiftUI\n' + source[start:end])
board = (root / 'macos/KaneoPilot/Views/BoardView.swift').read_text()
start = board.index('struct TaskBoardGroup:')
end = board.index('struct TaskExceptionGroupHeader:', start)
(fixture / 'TaskGroups.swift').write_text('import Foundation\n' + board[start:end])
(fixture / 'main.swift').write_bytes((root / 'macos/tests/completion_display_main.swift').read_bytes())
PY
swiftc "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_FIXTURE/DisplayStatus.swift" "$TASK_FIXTURE/TaskGroups.swift" "$TASK_FIXTURE/main.swift" -o "$TASK_FIXTURE/checks"
"$TASK_FIXTURE/checks"
