#!/bin/bash
# Real AppKit `.terminateLater` quit hand-off compiled with the production update manager.
# "direct" documents the shipped main-queue-callout deadlock; "deferred" proves the fix quits.
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
SOURCE_SHA_VALUE="${SOURCE_SHA:-}"
if ! printf '%s' "$SOURCE_SHA_VALUE" | grep -Eq '^[0-9a-f]{40}$'; then
  printf '%s\n' 'SOURCE_SHA must name the full commit under test' >&2
  exit 1
fi
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-update-appkit.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target "$(uname -m)-apple-macos14" -parse-as-library \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdate.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/SoftwareUpdateManager.swift" \
  "$TASK_ROOT/macos/tests/software_update_appkit_termination_main.swift" \
  -o "$TASK_FIXTURE/fixture"
python3 - "$TASK_FIXTURE" <<'PY'
import json
import os
import pathlib
import subprocess
import sys

fixture_dir = pathlib.Path(sys.argv[1])
fixture = fixture_dir / 'fixture'


def run(mode, expected_exit, expected_result):
    path = fixture_dir / (mode + '.json')
    if path.exists():
        path.unlink()
    try:
        completed = subprocess.run([str(fixture), mode, str(path)], timeout=45,
                                   capture_output=True, text=True)
        code, stderr = completed.returncode, (completed.stderr or '')[-2000:]
    except subprocess.TimeoutExpired:
        code, stderr = 'harness_timeout', 'the fixture harness killed the process'
    result = json.loads(path.read_text()) if path.exists() else None
    problems = []
    if code != expected_exit:
        problems.append('exit %r != expected %r' % (code, expected_exit))
    if not isinstance(result, dict):
        problems.append('no result file')
    else:
        if result.get('result') != expected_result:
            problems.append('result %r != expected %r' % (result.get('result'), expected_result))
        if result.get('mode') != mode:
            problems.append('mode %r != %r' % (result.get('mode'), mode))
    return {'mode': mode, 'exit_code': code, 'result': result, 'stderr': stderr}, problems


scenarios, problems = [], []
for mode, expected_exit, expected_result in (('direct', 2, 'timed_out'), ('deferred', 0, 'passed')):
    outcome, issues = run(mode, expected_exit, expected_result)
    scenarios.append(outcome)
    problems.extend('%s: %s' % (mode, issue) for issue in issues)
evidence = {
    'kind': 'appkit-terminate-later-handoff',
    'source_sha': os.environ['SOURCE_SHA'],
    'github_run_id': os.environ.get('GITHUB_RUN_ID'),
    'github_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
    'runner_environment': os.environ.get('RUNNER_ENVIRONMENT'),
    'production_manager': 'macos/KaneoPilot/Core/SoftwareUpdateManager.swift',
    'direct_expected': 'the main-queue-callout request deadlocks the nested termination loop',
    'deferred_expected': 'the production scheduling primitive quits the App',
    'scenarios': scenarios,
    'passed': not problems,
    'problems': problems,
}
print(json.dumps(evidence, sort_keys=True))
output = os.environ.get('KANPILOT_UPDATE_FIXTURE_OUTPUT', '')
if output:
    directory = pathlib.Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'appkit-termination-results.json').write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + '\n')
if problems:
    raise SystemExit('AppKit quit hand-off fixture failed: ' + '; '.join(problems))
PY
