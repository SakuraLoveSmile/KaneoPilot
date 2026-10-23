#!/bin/bash
# Isolated local Release candidate; never install over the production app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
sh "$ROOT/scripts/require_actions.sh"
SHAPE=lines
COUNT=400
BYTES=4096
BUILD_ONLY=0
APPEARANCE=light
WINDOW_SIZE=1180x760
while [ "$#" -gt 0 ]; do
  case "$1" in
    --shape) SHAPE="$2"; shift 2 ;;
    --count) COUNT="$2"; shift 2 ;;
    --bytes) BYTES="$2"; shift 2 ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --appearance) APPEARANCE="$2"; shift 2 ;;
    --size) WINDOW_SIZE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$SHAPE" == lines || "$SHAPE" == wrapped_json ]] || exit 2
[[ "$APPEARANCE" == light || "$APPEARANCE" == dark ]] || exit 2
[[ "$WINDOW_SIZE" == 900x600 || "$WINDOW_SIZE" == 1180x760 || "$WINDOW_SIZE" == 1440x900 ]] || exit 2
[[ "$COUNT" =~ ^[1-9][0-9]*$ && "$BYTES" =~ ^[1-9][0-9]*$ ]] || exit 2
PREVIEW="$(mktemp -d "${TMPDIR:-/tmp}/kaneopilot-session-reader.XXXXXX")"
mkdir -p "$PREVIEW/support" "$PREVIEW/legacy" "$PREVIEW/plans" "$PREVIEW/repos" "$PREVIEW/reports"
printf '{}\n' > "$PREVIEW/credentials.json"
echo "Isolated preview: $PREVIEW"
xcodebuild -project "$ROOT/macos/KaneoPilot.xcodeproj" -scheme KaneoPilot \
  -configuration Release -derivedDataPath "$PREVIEW/DerivedData" \
  CODE_SIGNING_ALLOWED=NO PRODUCT_BUNDLE_IDENTIFIER=local.kaneopilot.session-reader-check \
  build > "$PREVIEW/build.log" 2>&1 || { tail -60 "$PREVIEW/build.log"; exit 1; }
cp -R "$PREVIEW/DerivedData/Build/Products/Release/KaneoPilot.app" "$PREVIEW/KaneoPilot.app"
codesign --force --deep --sign - "$PREVIEW/KaneoPilot.app" > "$PREVIEW/sign.log" 2>&1
# Save a repeatable launcher. All paths are passed as argv to Python, never shell interpolated.
python3 - "$ROOT" "$PREVIEW" "$SHAPE" "$COUNT" "$BYTES" "$BUILD_ONLY" "$APPEARANCE" "$WINDOW_SIZE" <<'PY'
import json, os, pathlib, subprocess, sys
root, preview, shape, count, size, build_only, appearance, window_size = sys.argv[1:]
p = pathlib.Path(preview)
env = {k:os.environ[k] for k in ('HOME','PATH','TMPDIR','LANG','USER') if k in os.environ}
env.update(KANPILOT_CORE=root+'/macos/FakeCore/kanpilot-fake.py',
           KANPILOT_FAKE_STATE=preview+'/fake-state.json', KANPILOT_APP_SUPPORT=preview+'/support',
           KANPILOT_DB=preview+'/fixture.db', KANPILOT_LEGACY_ROOT=preview+'/legacy',
           KANPILOT_PLAN_ROOT=preview+'/plans', KANPILOT_REPOS_ROOT=preview+'/repos',
           KANPILOT_REPORTS_ROOT=preview+'/reports', KANEO_CREDENTIALS_PATH=preview+'/credentials.json',
           KANPILOT_FAKE_LONG_SESSION='task-music-006', KANPILOT_FAKE_LONG_SESSION_SHAPE=shape,
           KANPILOT_FAKE_LONG_SESSION_COUNT=count, KANPILOT_FAKE_LONG_SESSION_BYTES=size,
           PYTHONDONTWRITEBYTECODE='1', KANPILOT_SESSION_DIAGNOSTICS='1',
           KANPILOT_SESSION_DIAGNOSTICS_PATH=preview+'/diagnostics.jsonl',
           KANPILOT_APPEARANCE=appearance, KANPILOT_WINDOW_SIZE=window_size)
# Persist only the selected non-secret overrides, for a repeatable local relaunch.
preview_keys = (
    'KANPILOT_CORE', 'KANPILOT_FAKE_STATE', 'KANPILOT_APP_SUPPORT', 'KANPILOT_DB',
    'KANPILOT_LEGACY_ROOT', 'KANPILOT_PLAN_ROOT', 'KANPILOT_REPOS_ROOT', 'KANPILOT_REPORTS_ROOT',
    'KANEO_CREDENTIALS_PATH', 'KANPILOT_FAKE_LONG_SESSION', 'KANPILOT_FAKE_LONG_SESSION_SHAPE',
    'KANPILOT_FAKE_LONG_SESSION_COUNT', 'KANPILOT_FAKE_LONG_SESSION_BYTES',
    'PYTHONDONTWRITEBYTECODE', 'KANPILOT_SESSION_DIAGNOSTICS',
    'KANPILOT_SESSION_DIAGNOSTICS_PATH', 'KANPILOT_APPEARANCE', 'KANPILOT_WINDOW_SIZE',
)
env.pop('KANPILOT_CODEX_PROVIDER_KEY', None)
selected = {k:env[k] for k in preview_keys}
(p/'environment.json').write_text(json.dumps(selected, indent=2))
subprocess.run([sys.executable, env['KANPILOT_CORE'], 'get-session', '--json',
                json.dumps({'task_id':'task-music-006','run_id':'run-music-006-b2'})],
               env=env, stdout=subprocess.DEVNULL, check=True)
seed=json.loads((p/'fake-state.json').read_text())['session_fixture_seed']['task-music-006/run-music-006-b2']['messages']
manifest={'shape':shape,'count':len(seed),'content_utf8_bytes':sum(len(m['content'].encode()) for m in seed),
          'serialized_message_bytes':len(json.dumps(seed,ensure_ascii=False).encode()),
          'app':preview+'/KaneoPilot.app','state':env['KANPILOT_FAKE_STATE']}
if build_only != '1':
    with (p/'app.log').open('wb') as log:
        proc=subprocess.Popen([manifest['app']+'/Contents/MacOS/KaneoPilot', '-executorSchedulingEnabled','NO'],
                              env=env, stdout=log, stderr=log, start_new_session=True)
    manifest['pid']=proc.pid
(p/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps(manifest,ensure_ascii=False,indent=2))
PY
