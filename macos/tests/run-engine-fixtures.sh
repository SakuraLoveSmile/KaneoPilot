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
        assert engines and all(item['payload']['mode'] == 'live' for item in engines)
    if mode == 'external':
        assert len([item for item in commands if item['command'] == 'engine-run']) == 1
print('engine ownership, live default, and no legacy spawning: passed')
import copy, importlib.util, hashlib, os
os.environ['KANPILOT_FAKE_STATE'] = str(root / 'expiry-state.json')
spec = importlib.util.spec_from_file_location('engine_contract_fake', sys.argv[2])
fake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake)
mode_state = {'project_mappings': {'revision': 4, 'items': {
    'fixture-mode': {'engine_mode': 'legacy', 'automation': {
        'engine_mode': 'shadow', 'enabled': False, 'max_cost_usd': 3.5}}}}}
original = copy.deepcopy(mode_state)
for old_mode in (None, '', 'legacy', 'shadow', 'live'):
    for field in ('entry', 'nested'):
        candidate = copy.deepcopy(original)
        entry = candidate['project_mappings']['items']['fixture-mode']
        target = entry if field == 'entry' else entry['automation']
        target['engine_mode'] = old_mode
        projected = fake.cmd_get_project_mappings(candidate, {})
        assert projected['items'][0]['engine_mode'] == projected['items'][0]['effective_engine_mode'] == 'live'
        assert target['engine_mode'] == old_mode, 'mode projection must remain read-only'
for field in ('entry', 'nested'):
    candidate = copy.deepcopy(original)
    entry = candidate['project_mappings']['items']['fixture-mode']
    target = entry if field == 'entry' else entry['automation']
    target['engine_mode'] = 'invalid-mode'
    rejected = fake.cmd_get_project_mappings(candidate, {})
    assert rejected['__code__'] == 'validation_failed'
    assert target['engine_mode'] == 'invalid-mode', 'invalid mode must not be silently activated'
saved = fake.cmd_engine_project_setting(mode_state, {'project_id': 'fixture-mode', 'expected_revision': 4}, 'engine_mode')
assert saved['engine_mode'] == 'live' and saved['revision'] == 5
entry = mode_state['project_mappings']['items']['fixture-mode']
assert entry['engine_mode'] == 'live'
assert entry['automation'] == {'enabled': False, 'max_cost_usd': 3.5}, 'only the nested mode is removed'
print('project mode projection, invalid-mode refusal, and nested-mode cleanup: passed')
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
