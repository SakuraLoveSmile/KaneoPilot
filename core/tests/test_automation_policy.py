from types import SimpleNamespace
import subprocess

import pytest

from kanpilot import automation_policy as policy, core, db, exec_config, project_config, review_config, util

NOW = '2026-10-05T01:00:00+00:00'


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / 'state.db')
    db.set_setting(conn, 'project_mappings', {'revision': 0, 'items': {
        'fixture-project-policy': {'project_name': 'Policy', 'repository': None, 'enabled': True},
    }})
    conn.commit()
    yield SimpleNamespace(conn=conn, now_iso=NOW, now=util.parse_ts(NOW))
    conn.close()


@pytest.mark.parametrize('key,value', [('enabled', 1), ('auto_confirm_plan', 'yes'),
    ('label_filter', ['a'] * 21), ('label_filter', [None]), ('label_filter', ['x'*51]),
    ('max_fix_rounds', -1), ('max_fix_rounds', 3), ('max_fix_rounds', True),
    ('max_task_minutes', 29), ('max_task_minutes', 1441), ('max_task_minutes', 30.5),
    ('max_task_cost_usd', .49), ('max_task_cost_usd', 101), ('max_task_cost_usd', float('nan')),
    ('max_task_cost_usd', True), ('delivery_base', '-main'), ('delivery_base', 'with space'), ('delivery_base', 'x'*101)])
def test_invalid_policy(key, value):
    with pytest.raises(util.KanpilotError) as error:
        policy.validate_automation({key: value})
    assert error.value.code == 'bad_request'


@pytest.mark.parametrize('raw', [{'max_fix_rounds':0,'max_task_minutes':30,'max_task_cost_usd':.5},
    {'max_fix_rounds':2,'max_task_minutes':1440,'max_task_cost_usd':100}, {'max_task_cost_usd':None}])
def test_policy_bounds(raw):
    result = policy.validate_automation(dict(raw, label_filter=[' Bug ', 'BUG', 'feature']))
    assert result['label_filter'] == ['bug', 'feature']
    assert all(result[key] == value for key, value in raw.items())


def test_revision_and_mapping_preservation(ctx, tmp_path):
    pid = next(iter(project_config.load_raw_mappings(ctx.conn)[2]))
    result = policy.cmd_set_project_automation(ctx, {'project_id':pid, 'automation':{'enabled':True}, 'expected_policy_revision':0})
    assert result['policy_revision'] == 1
    with pytest.raises(util.KanpilotError) as error:
        policy.cmd_set_project_automation(ctx, {'project_id':pid, 'automation':{}, 'expected_policy_revision':0})
    assert error.value.code == 'conflict'
    repo = tmp_path / 'repo'; repo.mkdir()
    project_config.set_project_mapping(ctx.conn, {'project_id':pid, 'repository':str(repo), 'enabled':True, 'expected_revision':0}, NOW)
    assert policy.policy_for(ctx.conn, pid) == result['automation']


@pytest.mark.parametrize('value', [0, 6, True, 1.5])
def test_global_bounds(ctx, value):
    with pytest.raises(util.KanpilotError):
        policy.cmd_set_automation_global(ctx, {'max_active_tasks':value})


def test_global_setting(ctx):
    assert policy.cmd_set_automation_global(ctx, {'max_active_tasks':5}) == {'max_active_tasks':5}
    assert policy.global_settings(ctx.conn)['max_active_tasks'] == 5


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', '0', '-1', 'invalid'])
def test_probe_timeout_rejects_unbounded_or_invalid_environment(monkeypatch, value):
    monkeypatch.setenv('KANPILOT_GIT_PROBE_TIMEOUT', value)
    assert policy.git_probe_timeout() == 5.0


@pytest.mark.parametrize('targets', [
    ['git@github.com:other/repo.git'],
    ['git@github.com:owner/repo.git', 'git@github.com:owner/repo.git'],
])
def test_preflight_rejects_unsafe_push_targets_before_adoption(ctx, tmp_path, monkeypatch, targets):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin',
                    'git@github.com:owner/repo.git'], check=True)
    for target in targets:
        subprocess.run(['git', '-C', str(repo), 'config', '--add', 'remote.origin.pushurl', target], check=True)
    pid = 'project00001'
    db.set_setting(ctx.conn, 'project_mappings', {'items': {pid: {'repository': str(repo)}}})
    monkeypatch.setattr(policy, '_planner_ready', lambda conn: True)
    monkeypatch.setattr(policy, '_executable', lambda binary: True)
    monkeypatch.setattr(review_config, 'load', lambda conn: {'backend': 'codex'})
    monkeypatch.setattr(exec_config, 'load', lambda conn: {'backend': 'pi_desktop',
                        'pi_desktop': {'permission_mode': 'auto'}})
    before = ctx.conn.total_changes
    errors = policy.automation_preflight(ctx.conn, pid)
    assert {item['code'] for item in errors} == {'branch_diverged'}
    assert ctx.conn.total_changes == before


def test_preflight_codes_and_local_only(ctx, tmp_path, monkeypatch):
    pid = 'project00001'
    def mapping(repo):
        db.set_setting(ctx.conn, 'project_mappings', {'items':{pid:{'repository':repo}}})
    mapping(None)
    monkeypatch.setattr(policy, '_planner_ready', lambda conn: False)
    monkeypatch.setattr(policy, '_executable', lambda binary: False)
    codes = {e['code'] for e in policy.automation_preflight(ctx.conn, pid)}
    assert codes == {'repository_missing','gh_missing','planner_unconfigured','review_backend_unsupported','exec_backend_not_plus','exec_permission_not_auto'}
    repo = tmp_path / 'repo'; repo.mkdir(); mapping(str(repo))
    assert 'not_git_repo' in {e['code'] for e in policy.automation_preflight(ctx.conn, pid)}
    subprocess.run(['git','init',str(repo)], check=True, capture_output=True)
    subprocess.run(['git','-C',str(repo),'remote','add','origin','https://example.com/secret/token.git'], check=True)
    errors = policy.automation_preflight(ctx.conn, pid)
    assert 'origin_not_github' in {e['code'] for e in errors}
    assert 'secret' not in str(errors)
    subprocess.run(['git','-C',str(repo),'remote','set-url','origin','git@github.com:owner/repo.git'], check=True)
    monkeypatch.setattr(policy, '_planner_ready', lambda conn: True)
    monkeypatch.setattr(policy, '_executable', lambda binary: True)
    monkeypatch.setattr(review_config, 'load', lambda conn: {'backend':'codex'})
    monkeypatch.setattr(exec_config, 'load', lambda conn: {'backend':'pi_desktop','pi_desktop':{'permission_mode':'auto'}})
    assert policy.automation_preflight(ctx.conn, pid) == []
    for scheme in ("http", "git"):
        subprocess.run(['git', '-C', str(repo), 'remote', 'set-url', 'origin',
                        scheme + '://github.com/owner/repo.git'], check=True)
        assert {e['code'] for e in policy.automation_preflight(ctx.conn, pid)} == {'origin_not_github'}


def test_preflight_resolves_ssh_alias_in_repository(ctx, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin',
                    'git@github-alt:owner/repo.git'], check=True)
    pid = 'project00001'
    db.set_setting(ctx.conn, 'project_mappings', {'items': {pid: {'repository': str(repo)}}})
    monkeypatch.setattr(policy, '_planner_ready', lambda conn: True)
    monkeypatch.setattr(policy, '_executable', lambda binary: True)
    monkeypatch.setattr(review_config, 'load', lambda conn: {'backend': 'codex'})
    monkeypatch.setattr(exec_config, 'load', lambda conn: {'backend': 'pi_desktop',
                        'pi_desktop': {'permission_mode': 'auto'}})
    config = tmp_path / 'ssh_config'
    include = tmp_path / 'account.conf'
    include.write_text('Host github-alt\n  HostName ssh.github.com\n  Port 443\n')
    config.write_text(f'Include "{include}"\n')
    git_config = tmp_path / 'git_config'
    git_config.touch()
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(git_config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.delenv('GIT_SSH', raising=False)
    monkeypatch.delenv('GIT_SSH_COMMAND', raising=False)
    monkeypatch.delenv('GIT_SSH_VARIANT', raising=False)
    real_run, calls = subprocess.run, []
    def run(args, **kwargs):
        if args[0] == 'ssh':
            assert args[1:3] == ['-G', '-oCanonicalizeHostname=no']
            assert kwargs['cwd'] == str(repo)
            calls.append(args)
            args = [args[0], '-F', str(config), *args[1:]]
        return real_run(args, **kwargs)
    monkeypatch.setattr(policy.subprocess, 'run', run)
    assert policy.automation_preflight(ctx.conn, pid) == []
    assert len(calls) == 2  # Fetch identity and the effective push target are both checked.
    include.write_text('Host github-alt\n  HostName example.invalid\n  Port 443\n')
    assert {e['code'] for e in policy.automation_preflight(ctx.conn, pid)} == {'origin_not_github'}
    assert len(calls) == 3  # No cross-check cache hides an SSH configuration change.


def _task_request(ctx):
    ctx.conn.execute("INSERT INTO tasks(task_id,project_id,project_name,status,stage,created_at,updated_at) VALUES('task00001','project0001','P','planning','plan',?,?)", (NOW,NOW))
    task = db.require_task(ctx.conn, 'task00001')
    core.update_automation(ctx.conn, task, NOW, adopted_at=NOW, manual_takeover=False)
    request, _ = db.create_request(ctx.conn, NOW, 'plan', 'plan:task00001', task_id='task00001')
    db.update_request(ctx.conn, request['request_id'], NOW, status='succeeded', run_id='run1')
    return {'task_id':'task00001','request_id':request['request_id'],'run_id':'run1','stage':'plan','cost_usd':.42,'source':'claude'}


def test_cost_identity_dedup_and_unknown(ctx):
    payload = _task_request(ctx)
    assert policy.cmd_record_cost(ctx, payload) == {'recorded':True,'cost_usd':.42,'cost_unknown_steps':0}
    assert not policy.cmd_record_cost(ctx, payload)['recorded']
    db.update_request(ctx.conn, payload['request_id'], NOW, run_id='run2')
    unknown = policy.cmd_record_cost(ctx, dict(payload, run_id='run2', cost_usd=None))
    assert unknown['cost_unknown_steps'] == 1
    for changes, code in [({'run_id':'wrong'},'stale_run'), ({'stage':'review'},'conflict')]:
        with pytest.raises(util.KanpilotError) as error:
            policy.cmd_record_cost(ctx, dict(payload, **changes))
        assert error.value.code == code
    task = db.require_task(ctx.conn, payload['task_id'])
    core.update_automation(ctx.conn, task, NOW, cost_log=[])
    assert not policy.cmd_record_cost(ctx, dict(payload, run_id='run2', cost_usd=None))['recorded']
    core.update_automation(ctx.conn, task, NOW, manual_takeover=True)
    before = ctx.conn.total_changes
    assert not policy.cmd_record_cost(ctx, dict(payload, run_id='run2'))['recorded']
    assert ctx.conn.total_changes == before


def test_cost_log_rollover_keeps_permanent_dedup(ctx):
    payload = _task_request(ctx)
    for n in range(51):
        run = 'run'+str(n)
        db.update_request(ctx.conn,payload['request_id'],NOW,run_id=run)
        policy.cmd_record_cost(ctx,dict(payload,run_id=run,cost_usd=1))
    task = db.require_task(ctx.conn,payload['task_id'])
    assert len(core.automation_of(task)['cost_log']) == 50
    assert core.automation_of(task)['cost_usd'] == 51
    db.update_request(ctx.conn,payload['request_id'],NOW,run_id='run0')
    assert not policy.cmd_record_cost(ctx,dict(payload,run_id='run0',cost_usd=1))['recorded']


def test_unknown_cost_on_manual_task_does_not_adopt(ctx):
    payload = _task_request(ctx)
    task = db.require_task(ctx.conn,payload['task_id'])
    db.set_extra(ctx.conn,task,NOW,automation=None)
    assert not policy.cmd_record_cost(ctx,dict(payload,cost_usd=None))['recorded']
    assert not core.is_automation_managed(db.require_task(ctx.conn,payload['task_id']))


def test_base_save_three_states_and_local_read_fields(ctx, tmp_path, monkeypatch):
    from kanpilot import git_delivery
    from test_git_delivery import make_repo
    repo = make_repo(tmp_path / 'base-check')
    pid = next(iter(project_config.load_raw_mappings(ctx.conn)[2]))
    revision = project_config.load_raw_mappings(ctx.conn)[0]
    project_config.set_project_mapping(ctx.conn, {'project_id':pid, 'repository':str(repo), 'enabled':True, 'expected_revision':revision}, NOW)
    original = git_delivery._git
    calls = []
    def outside(args, *a, **kw):
        assert not ctx.conn.in_transaction
        calls.append(args)
        return original(args, *a, **kw)
    with monkeypatch.context() as scoped:
        scoped.setattr(git_delivery, '_git', outside)
        result = policy.cmd_set_project_automation(ctx, {'project_id':pid,'automation':{'delivery_base':'main'}})
        assert result['delivery_base_check'] == 'verified'
        assert not any(args[0] == 'fetch' for args in calls)
        with pytest.raises(util.KanpilotError, match='origin 上不存在分支 missing（默认分支 main）'):
            policy.cmd_set_project_automation(ctx, {'project_id':pid,'automation':{'delivery_base':'missing'}})
    def offline(args, *a, **kw):
        assert not ctx.conn.in_transaction
        if args[0] == 'fetch':
            assert a[1] == policy.git_probe_timeout()
            raise git_delivery.GitDeliveryError('git_transient', 'offline')
        return original(args, *a, **kw)
    with monkeypatch.context() as scoped:
        scoped.setattr(git_delivery, '_git', offline)
        result = policy.cmd_set_project_automation(ctx, {'project_id':pid,'automation':{'delivery_base':'missing'}})
        assert result['delivery_base_check'] == 'unverified'
    # Read side never refreshes remote refs.
    calls.clear()
    with monkeypatch.context() as scoped:
        scoped.setattr(git_delivery, '_git', outside)
        view = project_config.cmd_get_project_mappings(ctx, {})
    entry = next(item for item in view['items'] if item['project_id'] == pid)
    assert entry['origin_default_branch'] == 'main'
    assert entry['delivery_base_exists'] is False
    assert not any(args[0] == 'fetch' for args in calls)
