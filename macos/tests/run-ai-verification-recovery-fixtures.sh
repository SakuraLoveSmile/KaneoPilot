#!/bin/bash
# Actual resolve/perform methods and presentation with isolated FakeCore; no services/models.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-ai-verification-recovery.XXXXXX")"
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
resolve = source.split('    func resolve(taskID:', 1)[1].split('    func currentEngineFixRevert(', 1)[0]
perform = source.split('    private func perform(', 1)[1].split('    func showToast(', 1)[0]
error = source.split('extension Error {', 1)[1].split('// BEGIN EXECUTION ANSWER STORE', 1)[0]
presentation = (root / 'macos/KaneoPilot/Views/Components.swift').read_text().split('enum AutomationPresentation {', 1)[1].split('/// Highlight only', 1)[0]
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
    static func actionLabel(_ action: String) -> String { action }
    private func perform(''' + perform + '\n    func resolve(taskID:' + resolve
    + '\n}\nextension Error {' + error + '\nenum AutomationPresentation {' + presentation)
PY
swiftc -O -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
  "$TASK_FIXTURE/DesktopMethods.swift" "$TASK_ROOT/macos/tests/ai_verification_recovery_main.swift" \
  -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
