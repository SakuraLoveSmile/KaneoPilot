"""Workspace setup and saved project identity stay local and explicit."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kanpilot import cmd_config, cmd_read, db, planner_inputs, project_config, remote, util


@pytest.fixture
def local_db(tmp_path):
    conn = db.connect(tmp_path / 'isolated.db')
    yield conn
    conn.close()


def context(conn):
    return SimpleNamespace(conn=conn, now_iso='2026-10-22T00:00:00+00:00')


def test_new_install_has_no_workspace_or_default_projects(local_db):
    assert project_config.load_raw_mappings(local_db) == (0, '', {}, False)


def test_workspace_setting_and_saved_mappings_precedence(local_db):
    cmd_config.cmd_set_config(context(local_db), {'workspace_id': 'fixture-workspace'})
    assert project_config.load_raw_mappings(local_db)[1] == 'fixture-workspace'
    saved = {'revision': 7, 'workspace_id': 'saved-workspace', 'items': {'fixture-project': {'project_name': 'Custom', 'repository': '/fixture/repo'}}}
    db.set_setting(local_db, 'project_mappings', saved)
    assert project_config.load_raw_mappings(local_db) == (7, 'saved-workspace', saved['items'], True)
    with pytest.raises(util.KanpilotError) as exc:
        cmd_config.cmd_set_config(context(local_db), {'workspace_id': 'other-workspace', 'worktree_pool_size': 2})
    assert exc.value.code == 'conflict'
    assert db.get_setting(local_db, 'project_mappings') == saved
    assert db.get_setting(local_db, 'worktree_pool_size') != 2
    cmd_config.cmd_set_config(context(local_db), {'workspace_id': 'saved-workspace'})
    assert db.get_setting(local_db, 'project_mappings') == saved


def test_unconfigured_remote_paths_never_create_client(local_db, monkeypatch):
    factory = Mock(side_effect=AssertionError('network must not be reached'))
    monkeypatch.setattr(remote, 'get_client', factory)
    result = project_config.get_project_mappings(local_db, refresh_remote=True)
    assert result['remote']['status'] == 'error'
    assert 'workspace_id' in result['remote']['error']
    with pytest.raises(util.KanpilotError):
        planner_inputs.fetch_todos(local_db)
    assert not cmd_read._doctor_kaneo_ping(1, local_db)['ok']
    factory.assert_not_called()


def test_name_fallback_is_unique_and_uses_only_saved_mappings(local_db):
    db.set_setting(local_db, 'project_mappings', {'revision': 3, 'items': {
        'fixture-one': {'project_name': 'Custom'},
        'fixture-two': {'project_name': 'Duplicate'},
        'fixture-three': {'project_name': 'Duplicate'},
    }})
    assert project_config.project_id_for_name(local_db, 'Custom') == 'fixture-one'
    assert project_config.project_id_for_name(local_db, 'Duplicate') is None
    assert project_config.project_id_for_name(local_db, 'Absent') is None
    assert project_config.project_id_from_items({'fixture-unnamed': {'project_name': ''}}, '') is None


def test_set_config_cli_retains_pool_shape_and_validates_workspace(local_db):
    from kanpilot import cli
    db_path = db._db_file(local_db)
    assert cli.run('set-config', {'worktree_pool_size': 2}, db_path) == {'worktree_pool_size': 2}
    assert cli.run('set-config', {'workspace_id': 'fixture-workspace'}, db_path) == {'workspace_id': 'fixture-workspace'}
    assert cli.run('get-project-mappings', {}, db_path)['workspace_id'] == 'fixture-workspace'
    for payload in ({}, {'workspace_id': ''}, {'workspace_id': ' x '}, {'workspace_id': False}):
        with pytest.raises(util.KanpilotError):
            cli.run('set-config', payload, db_path)


def test_configured_project_validation_keeps_exact_identity_and_repository(local_db, tmp_path):
    from kanpilot import constants, planfile
    repo = tmp_path / 'custom-repo'
    repo.mkdir()
    project = {'project_name': 'Custom', 'repository': str(repo)}
    meta = {key: 'fixture' for key in constants.PLAN_META_REQUIRED}
    meta.update(project_id='fixture-custom-project', project_name='Custom', repository=str(repo),
                implementation_status='未开始', planner_model=constants.STANDARD_PLANNER['planner_model'],
                reasoning_effort=constants.STANDARD_PLANNER['reasoning_effort'])
    mappings = {'fixture-custom-project': project}
    planfile.check_plan_meta(meta, 'standard', configured_mappings=mappings)
    for changes in ({'project_id': 'other-project'}, {'project_name': 'Other'}, {'repository': str(tmp_path)}):
        with pytest.raises(util.KanpilotError):
            planfile.check_plan_meta({**meta, **changes}, 'standard', configured_mappings=mappings)
    with pytest.raises(util.KanpilotError):
        planfile.check_plan_meta(meta, 'standard')


def test_name_only_planner_metadata_uses_unique_configured_project(tmp_path):
    from kanpilot import planner_runner
    context = {'task': {'project_name': 'Custom'}, 'configured_project_mappings': {
        'fixture-custom-project': {'project_name': 'Custom', 'repository': str(tmp_path)},
    }}
    assert planner_runner.build_plan_meta(context, {}, 'fixture-time')['project_id'] == 'fixture-custom-project'
    context['configured_project_mappings']['fixture-duplicate-project'] = {'project_name': 'Custom'}
    assert planner_runner.build_plan_meta(context, {}, 'fixture-time')['project_id'] is None
    context['payload'] = {'project_snapshot': {'project_id': 'frozen-project'}}
    assert planner_runner.build_plan_meta(context, {}, 'fixture-time')['project_id'] == 'frozen-project'


def test_doctor_encodes_configured_workspace_without_changing_query(local_db, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    db.set_setting(local_db, 'workspace_id', 'fixture-workspace&extra=value')
    client = Mock()
    monkeypatch.setattr(remote, 'get_client', lambda: client)
    assert cmd_read._doctor_kaneo_ping(1, local_db)['ok']
    query = parse_qs(urlsplit(client.request.call_args.args[0]).query)
    assert query == {'workspaceId': ['fixture-workspace&extra=value']}


@pytest.mark.parametrize('workspace_field', [{}, {'workspace_id': ''}])
def test_saved_mapping_inherited_workspace_cannot_be_rebound(local_db, workspace_field):
    db.set_setting(local_db, 'workspace_id', 'fixture-original-workspace')
    saved = {'revision': 8, 'items': {'fixture-project': {'project_name': 'Custom'}}, **workspace_field}
    db.set_setting(local_db, 'project_mappings', saved)
    with pytest.raises(util.KanpilotError) as exc:
        cmd_config.cmd_set_config(context(local_db), {'workspace_id': 'fixture-other-workspace', 'worktree_pool_size': 2})
    assert exc.value.code == 'conflict'
    assert db.get_setting(local_db, 'project_mappings') == saved
    assert db.get_setting(local_db, 'workspace_id') == 'fixture-original-workspace'
    assert db.get_setting(local_db, 'worktree_pool_size') != 2
