#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-page-refresh.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
export KANPILOT_PYTHON="${KANPILOT_PYTHON:-$(command -v python3)}"
export KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py"
export KANPILOT_FAKE_STATE="$TASK_FIXTURE/state.json"
export KANPILOT_FAKE_COMMAND_LOG="$TASK_FIXTURE/commands.jsonl"
export PYTHONDONTWRITEBYTECODE=1
export KANPILOT_FAKE_PAGE_READ_RULES='[
    {"command":"overview","payload":{},"gate":"overview"},
    {"command":"list-tasks","payload":{"save_state":"archived"},"gate":"archive"},
    {"command":"list-events","payload":{"limit":100,"query":"refresh-a"},"gate":"filter_a"},
    {"command":"list-events","payload":{"limit":100,"query":"refresh-b"},"gate":"filter_b"},
    {"command":"list-events","payload":{"limit":100,"query":"refresh-b","before_id":320},"gate":"more","fail":true},
    {"command":"list-events","payload":{"limit":100,"query":"refresh-failure"},"fail":true},
    {"command":"get-request","payload":{"request_id":"req-plan-002"},"gate":"old_request","fail":true},
    {"command":"desktop-status","payload":{},"gate":"pause"}
]'
python3 - "$TASK_ROOT" "$TASK_FIXTURE" <<'PY'
import json
from pathlib import Path
import sys

root, fixture = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/AppViewModel.swift').read_text()
def between(start, end):
    return start + source.split(start, 1)[1].split(end, 1)[0]

(fixture / 'DesktopReads.swift').write_text('''import SwiftUI
@MainActor final class AppRuntime {
    static let shared = AppRuntime()
    let scheduler = EngineSupervisor(client: CoreClient())
}
@MainActor final class AppViewModel {
    let client = CoreClient()
    var actionInFlight: String?
    var alertMessage: String?
    var toast: String?
    var lastRefreshAt: Date?
    var eventsNextBeforeID: Int?
    var eventsLoadingMore = false
    func showToast(_ text: String) { toast = text }
''' + between('    enum Section:', '    // MARK: - 反馈态')
    + between('    enum Banner:', '    @Published var doctor:')
    + between('    enum ReadDomain:', '    @Published var toast:')
    + between('    private func attempt<', '    /// get-task + list-events(task_id)')
    + between('    func fetchRequestDetail(', '    // MARK: - v2 §4.3 运行记录分页/搜索')
    + between('    func setEventFilters(', '    // MARK: - v2 §5.22 手动规划链路')
    + between('    private func perform(', '    func showToast(')
    + between('    func archiveTask(', '    /// §5.15.6 resume-auto')
    + '\n}\n' + between('extension Error {', '// BEGIN EXECUTION ANSWER STORE'))

events = [dict(id=250, kind='paused', actor='user', at='2026-11-22T00:00:00Z',
               detail={'fixture': 'refresh-a'})]
events.extend(dict(id=number, kind='paused', actor='user', at='2026-11-22T00:00:00Z',
                   detail={'fixture': 'refresh-b'}) for number in range(300, 420))
(fixture / 'state.json').write_text(json.dumps({'extra_events': events}))
PY
swiftc -target arm64-apple-macos14 -O -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/EngineSupervisor.swift" "$TASK_FIXTURE/DesktopReads.swift" \
  "$TASK_ROOT/macos/tests/page_refresh_main.swift" -o "$TASK_FIXTURE/fixture"
HOME="$TASK_FIXTURE" "$TASK_FIXTURE/fixture"
