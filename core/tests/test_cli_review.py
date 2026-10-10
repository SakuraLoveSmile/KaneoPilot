"""Review backend argv, fake subprocesses and isolated deterministic runner."""
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kanpilot import (claude_cli, cli_review, codex_planner, db, git_delivery,
                      pi_desktop_review as review, review_config, review_settle, util)

RESULT = {'conclusion': 'pass', 'report_markdown': '通过；已核对证据', 'fix': None}


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_argv_freezes_readonly_nullable_schema(tmp_path, backend):
    config = {'model': 'test-model', 'effort': 'high', 'reasoning_effort': 'xhigh'}
    snapshot = tmp_path / 'review-123'
    argv = cli_review.launcher_argv(backend, config, tmp_path, snapshot, tmp_path / 'materials', budget=1.5)
    if backend == 'claude':
        assert argv[argv.index('--tools') + 1] == 'Read,Grep,Glob'
        assert '--bare' not in argv
        assert '--strict-mcp-config' in argv
        schema = json.loads(argv[argv.index('--json-schema') + 1])
        assert argv[argv.index('--max-budget-usd') + 1] == '1.5'
    else:
        assert argv[argv.index('-s') + 1] == 'read-only'
        assert argv[argv.index('-C') + 1] == str(snapshot)
        assert '--ignore-user-config' in argv
        assert 'model="test-model"' in argv
        assert 'model_reasoning_effort="xhigh"' in argv
        schema = json.loads(Path(argv[argv.index('--output-schema') + 1]).read_text())
    assert schema == review_settle.RESULT_SCHEMA
    assert schema['properties']['fix']['type'] == ['object', 'null']


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_fake_process_prompt_cwd_and_structured_result(tmp_path, backend):
    script = tmp_path / 'fake.py'
    output = tmp_path / 'last-message.txt'
    script.write_text("""import json, os, sys
from pathlib import Path
prompt=sys.stdin.read()
Path('observed.json').write_text(json.dumps({'cwd':os.getcwd(), 'prompt':prompt}))
result={'conclusion':'pass','report_markdown':'通过；已核对证据','fix':None}
if sys.argv[1]=='claude':
 print(json.dumps({'type':'system','subtype':'init','tools':['Read','Grep','Glob']}),flush=True)
 print(json.dumps({'type':'result','subtype':'success','structured_output':result,'total_cost_usd':.2}),flush=True)
else:
 Path(sys.argv[2]).write_text(json.dumps(result))
 print(json.dumps({'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':7}}),flush=True)
""")
    kwargs = dict(prompt='review input', cwd=tmp_path, env={}, timeout=5,
                  log_path=tmp_path / 'raw.jsonl')
    argv = [sys.executable, str(script), backend, str(output)]
    if backend == 'claude':
        result = claude_cli.run(argv, **kwargs)
    else:
        result = cli_review.run_codex(argv, out_path=output, **kwargs)
    assert result['is_error'] is False
    assert review_settle.validate_result(result['structured_output']) == RESULT
    observed = json.loads((tmp_path / 'observed.json').read_text())
    assert observed == {'cwd': str(tmp_path), 'prompt': 'review input'}
    assert result['total_cost_usd'] == (0.2 if backend == 'claude' else None)


def test_codex_idle_heartbeat_can_stop_without_resend(tmp_path):
    script = tmp_path / 'idle.py'
    script.write_text('import time\ntime.sleep(20)\n')
    calls = []
    def on_event(value):
        calls.append(value)
        raise util.KanpilotError('stale_run', 'lease lost')
    with pytest.raises(util.KanpilotError, match='lease lost'):
        cli_review.run_codex([sys.executable, str(script)], prompt='', cwd=tmp_path,
                             env={}, timeout=2, log_path=tmp_path / 'raw.jsonl',
                             out_path=tmp_path / 'missing.json', on_event=on_event)
    assert calls == [None]


@pytest.fixture
def runner_scene(tmp_path, monkeypatch):
    monkeypatch.setenv('KANPILOT_APP_SUPPORT', str(tmp_path / 'support'))
    monkeypatch.setenv('KANPILOT_REPORTS_ROOT', str(tmp_path / 'reports'))
    conn = db.connect(tmp_path / 'isolated.db')
    repository = tmp_path / 'repo'
    repository.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.name', 'Isolated Review Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repository / 'app.py').write_text('print("baseline")\n')
    git('add', 'app.py')
    git('commit', '-qm', 'isolated baseline')
    worktree = tmp_path / 'worktree'
    git('worktree', 'add', '--detach', str(worktree), 'HEAD')
    (worktree / 'app.py').write_text('print("reviewed change")\n')
    plan = tmp_path / 'plan.md'
    plan.write_text('# Plan\n')
    task = {'task_id': 't1', 'project_id': 'p1', 'run_id': 'exec1', 'status': 'in_review', 'extra': {},
            'plan_path': str(plan), 'plan_sha256': util.sha256_file(plan), 'repository': str(repository),
            'execution_round': {'backend': 'pi_desktop', 'worktree_path': str(worktree)}}
    request = {'request_id': 'req1', 'run_id': 'review1', 'stage': 'review', 'status': 'claimed',
               'lease_expires_at': util.to_iso(util.now_dt() + timedelta(minutes=10))}
    calls = []
    def core_call(_db, command, payload):
        calls.append((command, dict(payload)))
        if command == 'claim':
            return {'claimed': True, 'request_id': 'req1', 'run_id': 'review1', 'context': {'task_id': 't1'}}
        if command == 'get-task':
            return {'task': task}
        if command == 'get-request':
            return {'request': request}
        if command == 'register-review':
            task['review_summary'] = payload
            return {'ok': True}
        if command == 'report':
            request.update(status='succeeded' if payload['outcome'] == 'succeeded' else 'failed', result=payload)
        return {'ok': True}
    monkeypatch.setattr(review, 'core_call', core_call)
    monkeypatch.setattr(codex_planner.codex_provider, 'resolve_provider',
                        lambda _home: SimpleNamespace(auth_ready=True, auth_detail='', config_args=[], env={}))
    yield conn, task, calls
    conn.close()


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('failed', [False, True])
def test_run_stage_records_cost_before_settlement(runner_scene, tmp_path, monkeypatch, backend, failed):
    conn, task, calls = runner_scene
    monkeypatch.setattr(review_config, 'snapshot', lambda _conn: {'backend': backend, 'model': 'm', 'effort': 'high', 'reasoning_effort': 'high'})
    seen = []
    def process(argv, **kwargs):
        seen.append((argv, kwargs['cwd'], kwargs['prompt']))
        return {'is_error': failed, 'error': 'fake failure' if failed else None,
                'structured_output': RESULT, 'total_cost_usd': .4}
    monkeypatch.setattr(claude_cli, 'run', process)
    monkeypatch.setattr(cli_review, 'run_codex', process)
    code = cli_review.run_stage('review', conn=conn, db_path='isolated', timeout=3,
                                log_path=tmp_path / 'review.log', record_run=lambda **kwargs: None)
    assert code == (1 if failed else 0)
    assert len(seen) == 1
    assert seen[0][1].name.startswith('review-')
    assert 'kp_task' not in seen[0][2]
    commands = [cmd for cmd, _ in calls]
    assert commands.index('record-cost') < commands.index('report')
    cost = next(p for c, p in calls if c == 'record-cost')
    assert cost['cost_usd'] == (.4 if backend == 'claude' else None)
    assert cost['source'] == backend
    assert ('register-review' in commands) is not failed


def test_delivery_materials_full_diff_increment_and_large_diff(tmp_path, monkeypatch):
    old = tmp_path / 'old.md'
    old.write_text('上一轮报告')
    task = {'task_id': 't1', 'run_id': 'exec', 'repository': str(tmp_path),
            'delivery': {'mode': 'pr', 'branch': 'fixture', 'base_branch': 'main',
                         'base_sha': 'base', 'head_sha': 'head', 'worktree_path': str(tmp_path),
                         'commits': [{'execution_run_id': 'exec', 'sha': 'head'}]},
            'review_history': [{'head_sha': 'previous', 'report_path': str(old)}]}
    git_calls = []
    def git(args, **kwargs):
        git_calls.append(args)
        if '--stat' in args:
            return ' app.py | 1 +\n', None
        if 'diff' in args and 'base' in args:
            return 'x' * (400 * 1024 + 1), None
        return 'increment or commit evidence\n', None
    monkeypatch.setattr(review, '_git', git)
    monkeypatch.setattr(git_delivery, 'verify_branch_worktree', lambda *a, **k: None)
    monkeypatch.setattr(review.headless_adapter, 'resolve_source', Mock(side_effect=ValueError('fixture missing evidence')))
    materials = review.collect_materials(db_path='isolated', conn=None, materials=tmp_path / 'inputs',
                                         task=task, execution_worktree=tmp_path, execution_run_id='exec')
    assert '请在只读工作树中查看具体文件' in Path(materials['files']['changes.diff']).read_text()
    assert Path(materials['files']['previous-review-report.md']).read_text() == '上一轮报告'
    assert 'fix-increment.diff' in materials['files'] and 'commits.log' in materials['files']
    assert ['-C', str(tmp_path), 'diff', '--no-color', 'base', 'head'] in git_calls
    assert ['-C', str(tmp_path), 'diff', '--no-color', 'previous', 'head'] in git_calls
    assert not any('HEAD' in args for args in git_calls)


def test_dirty_or_mismatched_execution_blocks_before_snapshot(tmp_path, monkeypatch):
    task = {'task_id': 't1', 'repository': '/repo', 'run_id': 'exec',
            'delivery': {'mode': 'pr', 'worktree_path': '/worktree', 'head_sha': 'a' * 40,
                         'base_sha': 'base', 'base_branch': 'main', 'branch': 'fixture',
                         'commits': [{'execution_run_id': 'exec', 'sha': 'a' * 40}]}}
    verify = Mock(side_effect=git_delivery.GitDeliveryError('worktree_invalid', 'dirty'))
    snapshot = Mock()
    monkeypatch.setattr(git_delivery, 'verify_branch_worktree', verify)
    monkeypatch.setattr(git_delivery, 'prepare_snapshot_worktree', snapshot)
    assert '执行工作树与已提交版本不一致' in review.prepare_worktree(task, Path('/worktree'), tmp_path / 't1' / ('review-' + 'a' * 12))
    assert verify.call_args.kwargs == {'require_clean': True, 'expected_head': 'a' * 40}
    assert verify.call_args.args[0]['repository'] == '/repo'
    snapshot.assert_not_called()


def test_plus_configures_and_never_attaches(tmp_path):
    client = Mock()
    client.session_create.return_value = {'sessionId': 's1'}
    client.attach_mcp.side_effect = AssertionError('must not attach')
    assert review._create_plan_session(client, {'permission_mode': 'auto'}, tmp_path) == ('s1', None)
    client.attach_mcp.assert_not_called()
    assert client.session_configure.call_args.kwargs['permission_mode'] == 'auto'


def test_review_cli_config_save_switch_snapshot_and_invalid(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / 'config.db')
    monkeypatch.setattr(review_config.exec_config, 'capabilities', Mock(side_effect=AssertionError('CLI saving must not probe PI')))
    try:
        claude = review_config.save(conn, {'backend': 'claude', 'claude': {'model': 'opus', 'effort': 'xhigh'}}, 'now')
        assert review_config.snapshot(conn) == {'backend': 'claude', 'model': 'opus', 'effort': 'xhigh'}
        codex = review_config.save(conn, {'backend': 'codex', 'codex': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}}, 'later')
        assert codex['claude'] == claude['claude']
        assert review_config.snapshot(conn) == {'backend': 'codex', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}
        assert review_config.load(conn)['claude'] == {'model': 'opus', 'effort': 'xhigh'}
        for fields in ({'claude': {'model': 'bad\nmodel'}}, {'claude': {'effort': 'invalid'}}, {'codex': {'model': ''}}, {'codex': {'token': 'secret'}}):
            with pytest.raises(util.KanpilotError):
                review_config.save(conn, fields, 'bad')
        assert review_config.snapshot(conn) == {'backend': 'codex', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}
    finally:
        conn.close()


@pytest.mark.parametrize('event', [{'type': 'error', 'message': 'error'},
    {'type': 'turn.failed', 'error': {'message': 'error'}},
    {'type': 'item.completed', 'item': {'type': 'error', 'text': 'error'}}])
def test_codex_error_events_never_accept_success_json(tmp_path, event):
    output = tmp_path / 'out.json'
    script = tmp_path / 'error.py'
    script.write_text('import json\nfrom pathlib import Path\n'
                      + f'Path({str(output)!r}).write_text({json.dumps(RESULT)!r})\n'
                      + f'print({json.dumps(event)!r},flush=True)\n')
    result = cli_review.run_codex([sys.executable, str(script)], prompt='', cwd=tmp_path, env={},
                                  timeout=2, log_path=tmp_path / 'raw.jsonl', out_path=output)
    assert result['is_error']
    assert result['structured_output'] is None
