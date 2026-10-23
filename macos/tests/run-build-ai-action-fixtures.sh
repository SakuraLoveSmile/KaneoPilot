#!/bin/bash
# Real desktop commands and decoding against isolated FakeCore; no model or network.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-build-ai-action.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
export PYTHONDONTWRITEBYTECODE=1
export KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py"
export KANPILOT_FAKE_STATE="$TASK_FIXTURE/state.json"
export KANPILOT_FAKE_COMMAND_LOG="$TASK_FIXTURE/commands.jsonl"
python3 - "$TASK_ROOT" "$TASK_FIXTURE" <<'PY'
from pathlib import Path
import sys
root, fixture = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/AppViewModel.swift').read_text()
methods = source.split('    func prepareTaskAIAction(', 1)[1].split('    func prepareTaskRelease(', 1)[0]
perform = source.split('    private func perform(', 1)[1].split('    func showToast(', 1)[0]
error = source.split('extension Error {', 1)[1].split('// BEGIN EXECUTION ANSWER STORE', 1)[0]
(fixture / 'DesktopMethods.swift').write_text('''import Foundation
@MainActor final class AppViewModel {
    let client = CoreClient()
    var alertMessage: String?
    var actionInFlight: String?
    var selectedTaskID: String?
    var toast: String?
    func refreshAll() async {}
    func loadSaved() async {}
    func loadDetail(taskID: String) async {}
    func showToast(_ text: String) { toast = text }
    private func perform(''' + perform + '\n    func prepareTaskAIAction(' + methods
    + '\n}\nextension Error {' + error)
PY
swiftc -O -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
  "$TASK_FIXTURE/DesktopMethods.swift" "$TASK_ROOT/macos/tests/build_ai_action_main.swift" \
  -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
