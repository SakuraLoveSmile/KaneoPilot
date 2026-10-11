#!/bin/bash
# Actual supervisor + isolated FakeCore: no real DB, services, notifications or login item.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/kp-engine-fixtures.XXXXXX")"
trap 'rm -rf "$TASK_FIXTURE"' EXIT
swiftc -target arm64-apple-macos14 -O \
  "$TASK_ROOT/macos/KaneoPilot/Core/Models.swift" "$TASK_ROOT/macos/tests/LabelsStub.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/CoreLocator.swift" "$TASK_ROOT/macos/KaneoPilot/Core/CoreClient.swift" \
  "$TASK_ROOT/macos/KaneoPilot/Core/EngineSupervisor.swift" "$TASK_ROOT/macos/tests/engine_supervisor_main.swift" \
  -o "$TASK_FIXTURE/fixture"
for mode in healthy external failure; do
  mkdir -p "$TASK_FIXTURE/$mode/home"
  HOME="$TASK_FIXTURE/$mode/home" \
  KANPILOT_CORE="$TASK_ROOT/macos/FakeCore/kanpilot-fake.py" \
  KANPILOT_FAKE_STATE="$TASK_FIXTURE/$mode/state.json" \
  KANPILOT_FAKE_COMMAND_LOG="$TASK_FIXTURE/$mode/commands.jsonl" \
  KANPILOT_FAKE_ENGINE_FAIL="$([ "$mode" = failure ] && echo 1 || echo 0)" \
  "$TASK_FIXTURE/fixture" "$mode"
done
python3 - "$TASK_FIXTURE" "$TASK_ROOT/macos/FakeCore/kanpilot-fake.py" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for mode in ('healthy', 'external', 'failure'):
    commands = [json.loads(line) for line in (root / mode / 'commands.jsonl').read_text().splitlines()]
    forbidden = {'tick', 'executor-instance', 'enqueue', 'recover-execution-goals'}
    assert not forbidden.intersection(item['command'] for item in commands)
    if mode != 'external':
        engines = [item for item in commands if item['command'] == 'engine-run']
        assert engines and all(item['payload']['mode'] == 'legacy' for item in engines)
    if mode == 'external':
        assert len([item for item in commands if item['command'] == 'engine-run']) == 1
print('engine ownership, default mode, and no legacy spawning: passed')
import importlib.util, hashlib, os
os.environ['KANPILOT_FAKE_STATE'] = str(root / 'expiry-state.json')
spec = importlib.util.spec_from_file_location('engine_contract_fake', sys.argv[2])
fake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake)
task = fake.cmd_get_task({}, {'task_id': 'auto-verification_unconfirmed'})['task']
plan = fake.cmd_get_plan({}, {'task_id': task['task_id'], 'expected_plan_sha256': task['plan_sha256']})
assert hashlib.sha256(plan['markdown'].encode()).hexdigest() == task['plan_sha256']
assert task['plan_verify']['plan_sha256'] == task['plan_sha256']
assert task['plan_verify']['commands'] == fake.VERIFICATION_FIXTURE_COMMANDS
entry = {'instance_id': 'fake-expired', 'pid': os.getpid(), 'state': 'active',
         'lease_expires_at': fake.past(minutes=1), 'owned_workers': []}
pathlib.Path(fake.STATE_PATH + '.engine.json').write_text(json.dumps(entry))
expired = fake.cmd_engine_status({}, {})['instances'][0]
assert expired['holder_alive'] is True and expired['active'] is False
print('verification identity and real SHA declaration, healthy renewal, expired lease: passed')
PY
