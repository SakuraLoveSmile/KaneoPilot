#!/bin/bash
# Execute the production AppDelegate class with boundary stubs and an AppKit reply interceptor.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$TASK_ROOT/scripts/require_actions.sh"
TASK_FIXTURE_OUTPUT="${KANPILOT_UPDATE_FIXTURE_OUTPUT:-}"
if [ -n "$TASK_FIXTURE_OUTPUT" ]; then
  case "$TASK_FIXTURE_OUTPUT" in
    /*) ;;
    *) printf '%s\n' 'KANPILOT_UPDATE_FIXTURE_OUTPUT must be an absolute directory' >&2; exit 1 ;;
  esac
fi
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-update-termination.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
python3 - "$TASK_ROOT" "$TASK_FIXTURE" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
root, fixture = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/KaneoPilotApp.swift').read_text()
marker = '@MainActor\nprivate final class KaneoPilotAppDelegate:'
start = source.index(marker)
end = source.index('\n@main\nstruct KaneoPilotApp:', start)
delegate = source[start:end]
if delegate.count('private final class KaneoPilotAppDelegate:') != 1:
    raise SystemExit('Unexpected AppDelegate declaration')
delegate = delegate.replace('private final class KaneoPilotAppDelegate:', 'final class KaneoPilotAppDelegate:', 1)
extracted = 'import SwiftUI\nimport AppKit\n@preconcurrency import UserNotifications\n\n' + delegate + '\n'
(fixture / 'ProductionAppDelegate.swift').write_text(extracted)
identity_variable = 'SOURCE_SHA' if os.environ.get('SOURCE_SHA') else 'GITHUB_SHA'
sha = os.environ[identity_variable]
if not re.fullmatch(r'[0-9a-f]{40}', sha):
    raise SystemExit('Fixture source identity is not a full commit SHA')
checkout = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
if sha != checkout:
    raise SystemExit('Termination fixture checkout does not match the source identity')
identity = {'kind': 'production-appdelegate-isolated-sequencing', 'source_sha': sha, 'checkout_sha': checkout,
            'source_identity_variable': identity_variable,
            'run_id': os.environ.get('GITHUB_RUN_ID'), 'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
            'app_source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'extracted_class_sha256': hashlib.sha256(delegate.encode()).hexdigest(),
            'compiled_extraction_sha256': hashlib.sha256(extracted.encode()).hexdigest(),
            'fixture_source_sha256': hashlib.sha256((root / 'macos/tests/software_update_termination_main.swift').read_bytes()).hexdigest(),
            'extraction_change': 'remove only class-level private access modifier',
            'actual_app_exit': False, 'actual_installation': False, 'production_core': False}
(fixture / 'source.json').write_text(json.dumps(identity, indent=2, sort_keys=True) + '\n')
PY
swiftc -target "$(uname -m)-apple-macos14" -parse-as-library \
  "$TASK_FIXTURE/ProductionAppDelegate.swift" \
  "$TASK_ROOT/macos/tests/software_update_termination_main.swift" \
  -o "$TASK_FIXTURE/fixture"
"$TASK_FIXTURE/fixture" > "$TASK_FIXTURE/results.json"
python3 - "$TASK_FIXTURE" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
results = json.loads((root / 'results.json').read_text())
expected = {'success', 'drafts-failure', 'stop-failure', 'helper-failure', 'duplicate-exit'}
if results.get('passed') is not True or {item['scenario'] for item in results.get('scenarios', [])} != expected:
    raise SystemExit('Production AppDelegate sequencing evidence is incomplete')
print(json.dumps(results, sort_keys=True))
PY
if [ -n "$TASK_FIXTURE_OUTPUT" ]; then
  mkdir -p "$TASK_FIXTURE_OUTPUT"
  cp -p "$TASK_FIXTURE/results.json" "$TASK_FIXTURE_OUTPUT/software-update-termination-results.json"
  cp -p "$TASK_FIXTURE/source.json" "$TASK_FIXTURE_OUTPUT/software-update-termination-source.json"
  cp -p "$TASK_FIXTURE/ProductionAppDelegate.swift" "$TASK_FIXTURE_OUTPUT/software-update-termination-class.swift"
fi
