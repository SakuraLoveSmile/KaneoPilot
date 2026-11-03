#!/bin/bash
# Isolated UI preview: FakeCore only, a separate bundle ID and storage roots.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sh "$ROOT/scripts/require_actions.sh" --allow-local-macos
if [ "${KANPILOT_LOCAL_BUILD:-}" = 1 ] && [ "${GITHUB_ACTIONS:-}" != true ]; then
    OUT="${LOCAL_BUILD_ROOT:-$ROOT/build/local-macos}/ui-preview"
else
    OUT="${RUNNER_TEMP:?RUNNER_TEMP is required}/ui-simplification"
fi
CONFIG="${CONFIG:-Release}"
DERIVED="${DERIVED_DATA_DIR:-$OUT/DerivedData}"
SDK_SETTINGS=()
if [ -n "${KANPILOT_MACOS_SDK:-}" ]; then SDK_SETTINGS+=("SDKROOT=$KANPILOT_MACOS_SDK"); fi
SIZE="${1:-1180x760}"
APPEARANCE="${2:-light}"
case "$SIZE" in 900x600|1180x760|1440x900) ;; *) echo '窗口尺寸须为 900x600、1180x760 或 1440x900' >&2; exit 2 ;; esac
case "$APPEARANCE" in light|dark) ;; *) echo '外观须为 light 或 dark' >&2; exit 2 ;; esac
if pgrep -f "$OUT/KaneoPilot UI Preview.app/Contents/MacOS/KaneoPilot" >/dev/null; then
    echo '请先退出 KaneoPilot UI Preview，再重新启动预览。' >&2
    exit 2
fi
mkdir -p "$OUT"
xcodebuild -project "$ROOT/macos/KaneoPilot.xcodeproj" -scheme KaneoPilot \
  -configuration "$CONFIG" -derivedDataPath "$DERIVED" ARCHS=arm64 ONLY_ACTIVE_ARCH=YES \
  CODE_SIGNING_ALLOWED=NO PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.ui-simplification "${SDK_SETTINGS[@]}" \
  build > "$OUT/build.log" 2>&1 || { tail -50 "$OUT/build.log"; exit 1; }
python3 - "$ROOT" "$OUT" "$SIZE" "$APPEARANCE" "$DERIVED" "$CONFIG" <<'PY'
from pathlib import Path
import json, os, plistlib, shutil, subprocess, sys
root, out = map(Path, sys.argv[1:3])
app = out / 'KaneoPilot UI Preview.app'
shutil.copytree(Path(sys.argv[5]) / 'Build/Products' / sys.argv[6] / 'KaneoPilot.app', app, dirs_exist_ok=True)
resources = app / 'Contents/Resources/core'
resources.mkdir(parents=True, exist_ok=True)
shutil.copy2(root / 'macos/FakeCore/kanpilot-fake.py', resources / 'kanpilot.py')
info_path = app / 'Contents/Info.plist'
info = plistlib.loads(info_path.read_bytes())
info['CFBundleName'] = info['CFBundleDisplayName'] = 'KaneoPilot UI Preview'
if os.environ.get('KANPILOT_COMPATIBILITY_UI') == '1':
    info['UIDesignRequiresCompatibility'] = True
info_path.write_bytes(plistlib.dumps(info))
subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True, capture_output=True)
fixture = out / 'fixture'
for name in ('support', 'legacy', 'plans', 'repos', 'reports'):
    (fixture / name).mkdir(parents=True, exist_ok=True)
(fixture / 'credentials.json').write_text('{}\n')
env = {
    'KANPILOT_CORE': str(resources / 'kanpilot.py'),
    'KANPILOT_APP_SUPPORT': str(fixture / 'support'),
    'KANPILOT_DB': str(fixture / 'fixture.db'),
    'KANPILOT_LEGACY_ROOT': str(fixture / 'legacy'),
    'KANPILOT_PLAN_ROOT': str(fixture / 'plans'),
    'KANPILOT_REPOS_ROOT': str(fixture / 'repos'),
    'KANPILOT_REPORTS_ROOT': str(fixture / 'reports'),
    'KANEO_CREDENTIALS_PATH': str(fixture / 'credentials.json'),
    'KANPILOT_FAKE_STATE': str(fixture / 'state.json'),
    'KANPILOT_FAKE_REQUEST_LOG': str(fixture / 'requests.jsonl'),
    'KANPILOT_WINDOW_SIZE': sys.argv[3], 'KANPILOT_APPEARANCE': sys.argv[4],
    'PYTHONDONTWRITEBYTECODE': '1',
    'KANPILOT_PYTHON': os.environ.get('KANPILOT_PYTHON', sys.executable),
}
(out / 'preview-environment.json').write_text(json.dumps(env, ensure_ascii=False, indent=2))
(out / 'launch-preview.py').write_text('''from pathlib import Path
import json, os, subprocess
root = Path(__file__).resolve().parent
env = {k: os.environ[k] for k in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'USER') if k in os.environ}
env.update(json.loads((root / 'preview-environment.json').read_text()))
with (root / 'preview.log').open('ab') as log:
    process = subprocess.Popen([str(root / 'KaneoPilot UI Preview.app/Contents/MacOS/KaneoPilot')],
        env=env, stdout=log, stderr=log, start_new_session=True)
(root / 'preview-pid.txt').write_text(str(process.pid))
print('Preview PID', process.pid)
''')
subprocess.run([sys.executable, str(out / 'launch-preview.py')], check=True)
print('隔离预览：', app)
PY
