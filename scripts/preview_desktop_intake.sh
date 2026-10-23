#!/bin/bash
# Build and launch an isolated acceptance app; preserve the installed scheduler.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sh "$ROOT/scripts/require_actions.sh"
PREVIEW="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-desktop-intake.XXXXXX")"
mkdir -p "$PREVIEW/support" "$PREVIEW/legacy" "$PREVIEW/plans" "$PREVIEW/repos" "$PREVIEW/reports"
printf '{}\n' > "$PREVIEW/credentials.json"
echo "Isolated preview: $PREVIEW"
xcodebuild -project "$ROOT/macos/KaneoPilot.xcodeproj" -scheme KaneoPilot \
  -configuration Release -derivedDataPath "$PREVIEW/DerivedData" \
  CODE_SIGNING_ALLOWED=NO PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.desktop-intake-check \
  build > "$PREVIEW/build.log" 2>&1 || { tail -60 "$PREVIEW/build.log"; exit 1; }
cp -R "$PREVIEW/DerivedData/Build/Products/Release/KaneoPilot.app" "$PREVIEW/KaneoPilot.app"
RES="$PREVIEW/KaneoPilot.app/Contents/Resources/core"
mkdir -p "$RES"
cp -R "$ROOT/core/kanpilot.py" "$ROOT/core/kanpilot" "$ROOT/core/_vendor" \
  "$ROOT/core/executor_prompts" "$ROOT/core/agents" "$RES/"
codesign --force --deep --sign - "$PREVIEW/KaneoPilot.app" > "$PREVIEW/sign.log" 2>&1
python3 - "$PREVIEW" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys

preview = Path(sys.argv[1])
app = preview / 'KaneoPilot.app'
validation = Path.home() / 'Library/Application Support/KaneoPilot-Desktop-Validation'
validation.mkdir(mode=0o700, parents=True, exist_ok=True)
validation.chmod(0o700)
env = {k: os.environ[k] for k in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'USER') if k in os.environ}
env.update({
    'KANPILOT_CORE': str(app / 'Contents/Resources/core/kanpilot.py'),
    'KANPILOT_APP_SUPPORT': str(preview / 'support'),
    'KANPILOT_DB': str(validation / 'fixture.db'),
    'KANPILOT_LEGACY_ROOT': str(preview / 'legacy'),
    'KANPILOT_PLAN_ROOT': str(preview / 'plans'),
    'KANPILOT_REPOS_ROOT': str(preview / 'repos'),
    'KANPILOT_REPORTS_ROOT': str(preview / 'reports'),
    'KANEO_CREDENTIALS_PATH': str(preview / 'credentials.json'),
    'KANPILOT_WINDOW_SIZE': '1180x760',
    'PYTHONDONTWRITEBYTECODE': '1',
})
(preview / 'environment.json').write_text(json.dumps(
    {k: v for k, v in env.items() if k.startswith('KAN') or k == 'PYTHONDONTWRITEBYTECODE'},
    ensure_ascii=False, indent=2) + '\n')
(preview / 'launch.py').write_text('''import json, os, subprocess
from pathlib import Path
root = Path(__file__).resolve().parent
env = {k: os.environ[k] for k in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'USER') if k in os.environ}
env.update(json.loads((root / 'environment.json').read_text()))
with (root / 'app.log').open('ab') as log:
    subprocess.Popen([str(root / 'KaneoPilot.app/Contents/MacOS/KaneoPilot'),
                      '-executorSchedulingEnabled', 'NO'],
                     env=env, stdout=log, stderr=log, start_new_session=True)
''')
subprocess.run([sys.executable, env['KANPILOT_CORE'], 'overview'], env=env,
               stdout=subprocess.DEVNULL, check=True)
with (preview / 'app.log').open('wb') as log:
    proc = subprocess.Popen([str(app / 'Contents/MacOS/KaneoPilot'),
                             '-executorSchedulingEnabled', 'NO'],
                            env=env, stdout=log, stderr=log, start_new_session=True)
manifest = {'app': str(app), 'pid': proc.pid, 'db': env['KANPILOT_DB'],
            'core': env['KANPILOT_CORE'], 'support': env['KANPILOT_APP_SUPPORT']}
(preview / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(manifest, ensure_ascii=False, indent=2))
PY
