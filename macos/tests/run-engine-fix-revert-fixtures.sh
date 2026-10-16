#!/bin/bash
# Actual desktop resolve methods -> isolated FakeCore; no App, DB, network or model.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-engine-fix-revert.XXXXXX")"
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
methods = source.split('    func resolve(taskID:', 1)[1].split('    func recoverReview(', 1)[0]
policy = source.split('    @discardableResult\n    func setProjectAutomation(', 1)[1].split('    func setAutomationGlobal(', 1)[0]
error = source.split('extension Error {', 1)[1].split('// BEGIN EXECUTION ANSWER STORE', 1)[0]
presentation = (root / 'macos/KaneoPilot/Views/Components.swift').read_text().split('enum AutomationPresentation {', 1)[1].split('/// Highlight only', 1)[0]
(fixture / 'DesktopMethods.swift').write_text('''import Foundation
@MainActor final class AppViewModel {
    let client = CoreClient()
    var alertMessage: String?
    var actionInFlight: String?
    var refreshCount = 0
    func refreshAll() async { refreshCount += 1 }
    func showToast(_ text: String) {}
    static func actionLabel(_ action: String) -> String { action }
    func perform(_ title: String, successToast: String? = nil,
                 onError: ((String) -> Void)? = nil, operation: () async throws -> String?) async {
        do { _ = try await operation() }
        catch { alertMessage = error.coreMessage; onError?(error.coreMessage) }
    }
    func resolve(taskID:''' + methods + '\n    @discardableResult\n    func setProjectAutomation(' + policy
    + '\n}\nextension Error {' + error + '\nenum AutomationPresentation {' + presentation)
PY
swiftc -O -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
  "$TASK_FIXTURE/DesktopMethods.swift" "$TASK_ROOT/macos/tests/engine_fix_revert_main.swift" \
  -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture"
