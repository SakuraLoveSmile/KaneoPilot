"""Regression cases for MCP send receipts and resource authorization."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import timedelta
import sqlite3
import pytest

from kanpilot import db, runtime_operations, util, mcp_scopes, mcp_tools


def test_send_slot_is_unique_and_unknown_is_not_replayed(tmp_path):
    path = tmp_path / 'core.db'
    db.connect(path).close()
    def reserve(_):
        conn = db.connect(path)
        try:
            return runtime_operations.reserve_downstream(conn, 'scope', 'create', 'pi_desktop', 'kp_runtime_session_create', {'project': 'same'})['send']
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert results.count(True) == 1
    assert reserve(None) is False
    conn = db.connect(path)
    try:
        with pytest.raises(util.KanpilotError, match='不同参数'):
            runtime_operations.reserve_downstream(conn, 'scope', 'create', 'pi_desktop', 'kp_runtime_prompt', {'project': 'same'})
    finally:
        conn.close()


def test_malformed_existing_database_is_not_initialized(tmp_path):
    path = tmp_path / 'broken.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_meta(version INTEGER)')
    conn.commit()
    conn.close()
    with pytest.raises(util.KanpilotError, match='missing'):
        db.connect(path)
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT count(*) FROM schema_meta').fetchone()[0] == 0
    conn.close()


def test_runtime_resource_and_config_scope_guards(tmp_path):
    conn = db.connect(tmp_path / 'test.db')
    cfg = {'connection_file': '/tmp/plus.json', 'provider_id': 'p', 'model_id': 'm', 'thinking_level': 'off', 'permission_mode': 'ask'}
    scope = mcp_scopes.Scope('scope', 'invocation', 'planner', util.to_iso(util.now_dt() + timedelta(minutes=10)), project_path=str(tmp_path), runtime_config=cfg)
    try:
        with pytest.raises(util.KanpilotError, match='does not belong'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_status', {'scope_id': 'scope', 'session_id': 'foreign'})
        with pytest.raises(util.KanpilotError, match='snapshot'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_session_create', {'scope_id': 'scope', 'project_path': str(tmp_path), 'mode': 'plan', 'runtime_config': {**cfg, 'permission_mode': 'auto'}})
        mcp_tools._runtime_guard(conn, scope, 'kp_runtime_session_create', {'scope_id': 'scope', 'project_path': str(tmp_path), 'mode': 'plan', 'runtime_config': cfg})
        with pytest.raises(util.KanpilotError, match='unknown fields'):
            mcp_tools._validate_arguments(mcp_tools.TOOLS_REGISTRY['kp_task_heartbeat']['inputSchema'], {'request_id': 'r', 'run_id': 'x', 'token': 'secret'})
    finally:
        conn.close()


def test_runtime_prompt_uses_session_ownership_without_attachment(tmp_path):
    conn = db.connect(tmp_path / 'test.db')
    scope = mcp_scopes.Scope(
        'scope', 'invocation', 'planner',
        util.to_iso(util.now_dt() + timedelta(minutes=10)), project_path=str(tmp_path),
    )
    try:
        with db.write_txn(conn):
            runtime_operations.begin_operation(
                conn, scope.scope_id, 'create', 'pi_desktop',
                'kp_runtime_session_create', {},
            )
            runtime_operations.complete_operation(
                conn, scope.scope_id, 'create', session_id='owned-session',
            )
        assert not runtime_operations.session_attached(conn, scope.scope_id, 'owned-session')
        args = {'scope_id': scope.scope_id, 'session_id': 'owned-session', 'content': 'plan'}
        mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', args)
        with pytest.raises(util.KanpilotError, match='does not belong'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', {
                **args, 'session_id': 'foreign-session',
            })
        with pytest.raises(util.KanpilotError, match='outside the authorized project'):
            mcp_tools._runtime_guard(conn, scope, 'kp_runtime_prompt', {
                **args, 'attachments': [{'path': str(tmp_path.parent / 'outside.png')}],
            })
    finally:
        conn.close()


def test_authorization_is_checked_under_write_lock(tmp_path):
    conn = db.connect(tmp_path / 'test.db')
    def denied(_):
        raise util.KanpilotError('forbidden', 'revoked')
    conn.authorization_guard = denied
    try:
        with pytest.raises(util.KanpilotError, match='revoked'):
            with db.write_txn(conn):
                db.set_setting(conn, 'must_not_write', True)
        assert db.get_setting(conn, 'must_not_write') is None
    finally:
        conn.close()


def test_offline_plus_probe_returns_reason(tmp_path):
    from kanpilot import harness_plus_adapter
    result = harness_plus_adapter.plus_capabilities(str(tmp_path / 'missing.json'))
    assert result['connected'] is False
    assert result['attachment_supported'] is False
    assert result['detail']


def test_atomic_claim_receipt_prevents_concurrent_and_second_scope_claim(factory, tmp_env):
    from kanpilot import cli
    first_task = factory.make_running(title='first monitor').task
    second_task = factory.make_running(title='second monitor').task
    first_request = factory.active_request(first_task.task_id, 'monitor')['request_id']
    second_request = factory.active_request(second_task.task_id, 'monitor')['request_id']
    payload = {'stage':'monitor', 'executor':'kaneo-monitor', 'scope_id':'monitor_atomic', 'operation_id':'claim-1', 'request_id':first_request}
    def claim(_):
        return cli.run('mcp-claim', payload, tmp_env.db)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert all(r['claimed'] for r in results)
    assert len({r['run_id'] for r in results}) == 1
    with pytest.raises(util.KanpilotError, match='active claim'):
        cli.run('mcp-claim', {**payload, 'operation_id':'claim-2', 'request_id':second_request}, tmp_env.db)
    conn = db.connect(tmp_env.db)
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE stage='monitor' AND status='claimed'").fetchone()[0] == 1
    conn.close()


def test_report_committed_before_unbind_allows_next_claim_and_result_read(factory, tmp_env):
    from kanpilot import cli
    first_task = factory.make_running(title='before crash').task
    second_task = factory.make_running(title='after crash').task
    first_request = factory.active_request(first_task.task_id, 'monitor')['request_id']
    second_request = factory.active_request(second_task.task_id, 'monitor')['request_id']
    manager = mcp_scopes.ScopeManager(tmp_env.app_support / 'mcp')
    scope = mcp_scopes.Scope('monitor_crash', 'inv', 'monitor', util.to_iso(util.now_dt()+timedelta(minutes=10)))
    manager.scopes[scope.scope_id] = scope
    def call(name, payload):
        return mcp_tools.dispatch_tool_call(tmp_env.db, manager, 'task', scope, name, payload)
    first = call('kp_task_claim', {'operation_id':'claim-1', 'request_id':first_request})
    assert first['claimed']
    cli.run('report', {'request_id':first['request_id'], 'run_id':first['run_id'], 'outcome':'succeeded'}, tmp_env.db)
    # The core commit happened; the old in-memory binding deliberately remains.
    assert scope.bound_claim
    result = call('kp_task_request', {'request_id':first['request_id']})
    assert result['request']['status'] == 'succeeded'
    second = call('kp_task_claim', {'operation_id':'claim-2', 'request_id':second_request})
    assert second['claimed']
    assert second['request_id'] != first['request_id']
    assert scope.bound_claim['request_id'] == second['request_id']


def test_plan_resolution_has_no_write_lock_and_existing_authorization_is_offline_safe(factory, tmp_env, monkeypatch):
    from kanpilot import cli, planner_inputs, planner_config
    task = factory.sync_task(title='snapshot lock boundary')
    prepared = factory.cli('prepare-plan-input', {'task_id':task.task_id})
    factory.cli('set-planner-config', {'backend':'pi_desktop', 'pi_desktop':{'model_source':'inherit', 'connection_file':str(tmp_env.root/'plus.json'), 'permission_mode':'ask'}})
    runtime = {'connection_file':str(tmp_env.root/'plus.json'), 'provider_id':'p', 'model_id':'m', 'thinking_level':'off', 'permission_mode':'ask'}
    calls = []
    def resolve(conn, block=None, **kwargs):
        assert not conn.in_transaction
        second = db.connect(tmp_env.db)
        with db.write_txn(second):
            db.set_setting(second,'network_callback_succeeded',True)
        second.close()
        calls.append(True)
        return runtime, None
    monkeypatch.setattr(planner_config,'resolve_runtime_config',resolve)
    payload = {'items':[{'input_id':prepared['input_id'], 'input_sha256':prepared['input_sha256'], 'planner_backend':'pi_desktop'}]}
    ctx=cli.Context(tmp_env.db)
    try:
        first=planner_inputs.start_plans(ctx,payload)
        assert first['started']==1, first
        assert len(calls)==1
        def offline(*args, **kwargs):
            raise AssertionError('authorized request must not resolve again')
        monkeypatch.setattr(planner_config,'resolve_runtime_config',offline)
        duplicate=planner_inputs.start_plans(ctx,payload)
        assert duplicate['results'][0]['deduplicated'] is True
        assert duplicate['results'][0]['request_id']==first['results'][0]['request_id']
    finally:
        ctx.close()


def test_mcp_server_rejects_old_schema_without_upgrade(tmp_path):
    from kanpilot import mcp_server
    path=tmp_path/'old.db'
    conn=db.connect(path)
    conn.execute('UPDATE schema_meta SET version=4')
    conn.close()
    server=mcp_server.MCPServer(db_path=path,app_support_dir=tmp_path/'profile')
    with pytest.raises(util.KanpilotError, match='schema version is 4'):
        server.start()
    conn=sqlite3.connect(path)
    assert conn.execute('SELECT version FROM schema_meta').fetchone()[0]==4
    conn.close()
    assert server.http_server is None
