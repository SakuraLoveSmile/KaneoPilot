#!/bin/bash
# Build/sign an isolated native shell around the real update section. UI execution is separate.
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
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-update-ui-build.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
TASK_APP="$TASK_FIXTURE/KaneoPilot-SoftwareUpdate-UI.app"
mkdir -p "$TASK_APP/Contents/MacOS" "$TASK_APP/Contents/Resources"
python3 - "$TASK_ROOT" "$TASK_FIXTURE" "$TASK_APP" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

root, fixture, app = map(Path, sys.argv[1:])
source = (root / 'macos/KaneoPilot/Views/Components.swift').read_text()
def declaration(marker):
    start = source.index(marker)
    first = source.index('{', start)
    depth = 0
    for position in range(first, len(source)):
        depth += (source[position] == '{') - (source[position] == '}')
        if depth == 0:
            return source[start:position + 1]
    raise SystemExit('Cannot extract actual design component: ' + marker)
components = 'import SwiftUI\nimport AppKit\n\n' + '\n\n'.join(declaration(marker) for marker in (
    'extension Font {', 'enum Spacing {', 'struct FieldRow: View {')) + '\n'
(fixture / 'FixtureComponents.swift').write_text(components)
identity_variable = 'SOURCE_SHA' if os.environ.get('SOURCE_SHA') else 'GITHUB_SHA'
sha = os.environ[identity_variable]
if not re.fullmatch(r'[0-9a-f]{40}', sha):
    raise SystemExit('Fixture source identity is not a full commit SHA')
checkout = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
if sha != checkout:
    raise SystemExit('UI fixture checkout does not match the source identity')
inputs = ['macos/KaneoPilot/Core/SoftwareUpdate.swift', 'macos/KaneoPilot/Core/SoftwareUpdateManager.swift',
          'macos/KaneoPilot/Views/SoftwareUpdateSection.swift', 'macos/KaneoPilot/Views/Components.swift',
          'macos/tests/software_update_ui_main.swift', 'macos/tests/run-software-update-ui-fixtures.sh']
identity = {'kind': 'isolated-native-update-ui', 'source_sha': sha, 'checkout_sha': checkout,
            'source_identity_variable': identity_variable,
            'run_id': os.environ.get('GITHUB_RUN_ID'), 'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
            'bundle_id': 'local.kaneopilot.software-update-ui-check',
            'inputs': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in inputs},
            'extracted_components_sha256': hashlib.sha256(components.encode()).hexdigest(),
            'real_network': False, 'production_core': False, 'actual_installation': False,
            'ui_execution': 'NOT RUN by build script'}
(app / 'Contents/Resources/fixture-source.json').write_text(json.dumps(identity, indent=2, sort_keys=True) + '\n')
with (app / 'Contents/Info.plist').open('wb') as stream:
    plistlib.dump({'CFBundleIdentifier': identity['bundle_id'], 'CFBundleName': 'KaneoPilot 更新隔离核验',
                  'CFBundleExecutable': 'KaneoPilotSoftwareUpdateUI', 'CFBundlePackageType': 'APPL',
                  'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': '1',
                  'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
                  'NSPrincipalClass': 'NSApplication'}, stream)
PY
swiftc -target "$(uname -m)-apple-macos14" -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdate.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdateManager.swift" \
  "$TASK_FIXTURE/FixtureComponents.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Views/SoftwareUpdateSection.swift" \
  "$TASK_ROOT/macos/tests/software_update_ui_main.swift" \
  -o "$TASK_APP/Contents/MacOS/KaneoPilotSoftwareUpdateUI"
codesign --force --sign - --timestamp=none "$TASK_APP"
codesign --verify --deep --strict "$TASK_APP"
if [ -n "$TASK_FIXTURE_OUTPUT" ]; then
  mkdir -p "$TASK_FIXTURE_OUTPUT"
  ditto -c -k --sequesterRsrc --keepParent "$TASK_APP" "$TASK_FIXTURE_OUTPUT/software-update-ui-fixture.zip"
  python3 - "$TASK_APP" "$TASK_FIXTURE_OUTPUT" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
app, output = map(Path, sys.argv[1:])
identity = json.loads((app / 'Contents/Resources/fixture-source.json').read_text())
identity['archive_sha256'] = hashlib.sha256((output / 'software-update-ui-fixture.zip').read_bytes()).hexdigest()
identity['executable_sha256'] = hashlib.sha256((app / 'Contents/MacOS/KaneoPilotSoftwareUpdateUI').read_bytes()).hexdigest()
(output / 'software-update-ui-fixture-source.json').write_text(json.dumps(identity, indent=2, sort_keys=True) + '\n')
PY
fi
printf '%s\n' 'Isolated native update UI fixture compiled and ad-hoc signed; UI interaction NOT RUN.'
