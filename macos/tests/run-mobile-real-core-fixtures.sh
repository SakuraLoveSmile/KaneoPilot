#!/bin/bash
# Actual core + fresh SQLite + owned loopback HTTPS + actual App-side TCP probe.
# No GUI, remote service, production data, model call, or retained credentials.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_ARCH="$(uname -m)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-mobile-real-core.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target "${TASK_ARCH}-apple-macos14" -g -Onone \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/MobileModels.swift" "$TASK_ROOT/macos/KaneoPilot/Core/MobileServiceManager.swift" \
  "$TASK_ROOT/macos/tests/mobile_service_main.swift" -o "$TASK_FIXTURE/fixture"
python3 - "$TASK_ROOT" "$TASK_FIXTURE" <<'PY'
import os, pathlib, shutil, socket, subprocess, sys
repo, root = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(repo / 'core'))
from kanpilot import db
path = root / 'state.db'
db.connect(path).close()
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
# Keep the desktop-selected interpreter and real CLI/HTTP path. Only the
# service process gets a one-shot stack dump before its 15s startup deadline.
stack_path = root / 'mobile-serve-stacks.log'
wrapper = root / 'diagnostic-core.py'
wrapper.write_text(f'''import faulthandler
import os
import runpy
import sys

trace = None
if len(sys.argv) > 1 and sys.argv[1] == 'mobile-serve':
    fd = os.open({str(stack_path)!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    trace = os.fdopen(fd, 'w')
    faulthandler.dump_traceback_later(10, repeat=False, file=trace)
try:
    runpy.run_path({str(repo/'core/kanpilot.py')!r}, run_name='__main__')
finally:
    if trace is not None:
        faulthandler.cancel_dump_traceback_later()
        trace.close()
''')
env = {**os.environ, 'KANPILOT_CORE': str(wrapper),
       'KANPILOT_DB': str(path), 'KANPILOT_MOBILE_TEST_PORT': str(port),
       'KANEO_CREDENTIALS_PATH': str(root/'absent-credentials.json')}
for key, directory in [('KANPILOT_APP_SUPPORT', 'support'), ('KANPILOT_LEGACY_ROOT', 'legacy'),
                       ('KANPILOT_PLAN_ROOT', 'plans'), ('KANPILOT_REPOS_ROOT', 'repos'),
                       ('KANPILOT_REPORTS_ROOT', 'reports')]:
    env[key] = str(root / directory)
env.pop('KANPILOT_FAKE_STATE', None)
# CoreClient deliberately prepares a desktop PATH; report that selected runtime
# separately from the interpreter used to initialize this isolated database.
desktop_path = str(pathlib.Path.home()/'.npm-global/bin') + ':/usr/local/bin:/opt/homebrew/bin:' + env.get('PATH', '/usr/bin:/bin')
print('mobile fixture bootstrap Python:', sys.executable, flush=True)
print('mobile fixture desktop Python:', shutil.which('python3', path=desktop_path), flush=True)
print('mobile fixture bind host: 127.0.0.1', flush=True)
try:
    subprocess.run([str(root/'fixture'), 'real-core'], env=env, check=True)
except subprocess.CalledProcessError:
    if stack_path.exists():
        # Python faulthandler records frames only, never locals, argv, or env.
        # Print before temporary-directory cleanup so CI retains the evidence.
        with stack_path.open('rb') as trace:
            stack = trace.read(64 * 1024)
        print('mobile fixture service stack (maximum 64 KiB):', flush=True)
        print(stack.decode('utf-8', errors='replace'), flush=True)
    raise
print('isolated real-core loopback HTTPS, probe deadline, pairing and shutdown: passed')
PY
