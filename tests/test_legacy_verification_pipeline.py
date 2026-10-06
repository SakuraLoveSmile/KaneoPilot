"""Step 15-G: manual legacy execution through real runner/core boundaries.

The conftest Kaneo service is loopback only. Pi execution and Codex Review use
isolated fake executables; no installed model or production database is used.
"""
import json
import sys
import subprocess
import time

import pytest

from kanpilot import auto_dispatch, auto_monitor, cli_review, db


def stage(module, name, env):
    conn = db.connect(env.db)
    records = []
    try:
        rc = module.run_stage(name, conn=conn, db_path=env.db, timeout=180,
                              log_path=env.root / (name + '.log'),
                              record_run=lambda **fields: records.append(fields))
    finally:
        conn.close()
    assert rc == 0, records
    assert records[-1]['exit_code'] == 0


def monitor(module, env):
    # Only advance the scheduler's clock boundary, never task or result state.
    conn = db.connect(env.db)
    try:
        conn.execute("UPDATE requests SET not_before=NULL WHERE kind='monitor' AND status='queued'")
    finally:
        conn.close()
    stage(module, 'monitor', env)


def assert_legacy(task):
    assert not task.get('automation')
    assert not task.get('delivery')
    assert not task['extra'].get('automation')
    assert not task['extra'].get('delivery')
    assert 'plan_verify' not in task['extra']


@pytest.fixture
def legacy_execution(factory, cli, tmp_env, monkeypatch):
    repo = tmp_env.repos_root / 'StudyTool'
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                 ('config', 'user.email', 'fixture@example.test')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    (repo / 'baseline.txt').write_text('legacy baseline\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'baseline'], check=True, capture_output=True)
    ready = factory.make_ready(title='Legacy manual execution without verify')
    tid = ready.task.task_id
    factory.enqueue_dispatch(tid)
    stage(auto_dispatch, 'dispatch', tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'running', task
    # Seed the pre-v5 persisted shape, rather than testing the new publisher's
    # empty/error declaration. This is fixture setup only; all later state
    # transitions must pass through the runner and core commands.
    extra = dict(task['extra'])
    extra.pop('plan_verify', None)
    conn = db.connect(tmp_env.db)
    try:
        conn.execute('UPDATE tasks SET extra_json=? WHERE task_id=?', (json.dumps(extra), tid))
    finally:
        conn.close()
    task = factory.get_task(tid)
    assert_legacy(task)
    # The real subprocess fake Pi worker records the round through core commands.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = cli('get-session', {'task_id': tid, 'run_id': task['run_id']}).get('round_detail') or {}
        if detail.get('terminal'):
            break
        time.sleep(.05)
    assert detail.get('terminal', {}).get('outcome') == 'completed', detail
    assert detail.get('exit_code') == 0, detail
    return tid, task['run_id']


def test_legacy_user_acceptance_reaches_real_codex_review(legacy_execution, factory, cli, tmp_env, monkeypatch):
    tid, execution_run = legacy_execution
    monitor(auto_monitor, tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'needs_user', task
    assert task['failure_code'] == 'verification_unconfirmed', task
    assert 'accept_verification' in task['allowed_actions']
    assert_legacy(task)
    cli('resolve', {'task_id': tid, 'action': 'accept_verification', 'note': 'User checked legacy result'})
    accepted = factory.get_task(tid)
    assert accepted['status'] == 'running'
    assert accepted['extra']['verification_override']['execution_run_id'] == execution_run
    monitor(auto_monitor, tmp_env)
    assert factory.get_task(tid)['status'] == 'in_review', factory.get_task(tid)
    codex = tmp_env.root / 'codex-review'
    codex.write_text('#!' + sys.executable + '\nimport json,pathlib,sys\n'
        'if "--version" in sys.argv: print("codex fake"); sys.exit(0)\n'
        'sys.stdin.read()\n'
        'out=sys.argv[sys.argv.index("-o")+1]\n'
        'pathlib.Path(out).write_text(json.dumps({"conclusion":"pass","report_markdown":"# Review\\nLegacy result accepted; code checked.","fix":None}))\n'
        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))\n')
    codex.chmod(0o755)
    monkeypatch.setenv('KANPILOT_CODEX_BIN', str(codex))
    # CLI fixture subprocesses retain their isolated env snapshot.
    cli.env['KANPILOT_CODEX_BIN'] = str(codex)
    cli('set-review-config', {'backend': 'codex', 'codex': {'model':'gpt-6.1-sol', 'reasoning_effort':'medium'}})
    stage(cli_review, 'review', tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'done', task
    assert task['extra']['review_conclusion'] == 'pass', task
    assert factory.kaneo.get_task(tid)['status'] == 'done'
    assert_legacy(task)
    conn = db.connect(tmp_env.db)
    try:
        assert conn.execute('SELECT version FROM schema_meta').fetchone()[0] == 5
    finally:
        conn.close()


@pytest.mark.parametrize('current_override,exit_code,expected', [
    (False, 0, 'verification_unconfirmed'), (True, 2, 'execution_failed'),
    (True, None, 'execution_failed'),
])
def test_legacy_override_identity_and_execution_gate(legacy_execution, factory, cli, tmp_env,
                                                       monkeypatch, current_override, exit_code, expected):
    tid, execution_run = legacy_execution
    monitor(auto_monitor, tmp_env)
    cli('resolve', {'task_id': tid, 'action': 'accept_verification', 'note': 'User checked result'})
    # Fake observation boundary only. All monitor state changes, remote recheck,
    # report/advance/claim and the persisted override stay actual core commands.
    real_run = auto_dispatch.cli.run
    observed = []
    def observation(command, payload, db_path):
        result = real_run(command, payload, db_path)
        if command == 'get-task' and payload.get('task_id') == tid and not current_override:
            result['task']['extra']['verification_override']['execution_run_id'] = 'previous-execution'
        if command == 'get-session':
            observed.append(payload['run_id'])
            result['round_detail']['exit_code'] = exit_code
        return result
    monkeypatch.setattr(auto_dispatch.cli, 'run', observation)
    monitor(auto_monitor, tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'needs_user', task
    assert task['failure_code'] == expected, task
    assert observed == [execution_run]
    assert factory.kaneo.get_task(tid)['status'] == 'in-progress'
    assert_legacy(task)
