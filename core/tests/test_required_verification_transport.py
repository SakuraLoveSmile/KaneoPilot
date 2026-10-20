"""Frozen verification requirements cross isolated fake Plus MCP only."""
import hashlib
import json
from unittest import mock

import pytest
from kanpilot import harness_plus_adapter, pi_desktop_backend, runtime_client, util
import test_pi_desktop_backend as fixture


def test_runtime_forwards_frozen_commands_without_silent_kwargs_drop():
    facade = runtime_client.RuntimeClient(role='execution', connection_file='/fake',
        project_path='/fake', runtime_config={}, task_id='t', run_id='r')
    with mock.patch.object(facade, '_write', return_value={}) as write:
        facade.plans_resolve('p', 's', 't', 'call', 'approve', version=1,
                             required_verification_commands=['python3 -m pytest'])
    assert write.call_args.args[2]['required_verification_commands'] == ['python3 -m pytest']


def test_direct_helper_and_adapter_forward_camel_case():
    direct = pi_desktop_backend.MCPClient('http://fake', 'secret')
    with mock.patch.object(direct, 'supports_required_verification_commands', return_value=True), mock.patch.object(direct, 'call_tool', return_value={}) as call:
        direct.plans_resolve('p', 's', 't', 'call', 'approve', version=1,
                             required_verification_commands=['pytest'])
    assert call.call_args.args[1]['requiredVerificationCommands'] == ['pytest']


@pytest.fixture
def case():
    obj = fixture.TestRunReal('test_completed_flow_events_and_order')
    obj.setUp()
    try:
        yield obj
    finally:
        obj.tearDown()
        obj.doCleanups()


def test_old_plus_blocks_command_verification_before_model_send(case):
    spec, directory = case._base(body='```verify\npython3 -m pytest\n```\n')
    spec['completion_policy'] = 'command_verification'
    spec['required_verification_commands'] = ['python3 -m pytest']
    assert case._run(spec) == 1
    assert case.server.prompt_count == 0
    assert case.server.created is None
    assert 'requiredVerificationCommands' in case.read_job(directory)['detail']


def enable_requirements(case):
    original = case.server.tool_schema
    def schema(name):
        if name == 'pi_plans_resolve':
            return {'type': 'object', 'properties': {
                'requiredVerificationCommands': {'type': 'array', 'items': {'type': 'string'}}}}
        return original(name)
    case.server.tool_schema = schema


def frozen_scene(case):
    spec, directory = case._base(body='```verify\npython3 -m pytest\nprintf "a  b"\n```\n')
    spec['completion_policy'] = 'command_verification'
    spec['required_verification_commands'] = ['python3 -m pytest', 'printf "a  b"']
    canonical = case.runs_root / spec['task_id'] / spec['run_id']
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / 'spec.json').write_text(json.dumps(spec))
    case.server.turn_statuses = ['running', 'completed']
    case.server.turn_status = 'completed'
    case.server.plans = [case._goal_contract()]
    case.server.planning_state = 'awaiting_approval'
    case.server.execution_turn = 'turn-goal-exec'
    case.server.messages = case.default_messages()
    return spec, directory


def test_frozen_spec_survives_real_loopback_mcp_to_plus(case):
    enable_requirements(case)
    spec, directory = frozen_scene(case)
    assert case._run(spec) == 0
    assert case.server.prompt_count == 1
    assert len(case.server.plans_resolve_calls) == 1
    assert case.server.plans_resolve_calls[0]['requiredVerificationCommands'] == spec['required_verification_commands']
    from kanpilot import db
    conn = db.connect(case.db_path)
    try:
        row = conn.execute("SELECT request_sha256 FROM runtime_operations WHERE tool='kp_runtime_proposal_resolve'").fetchone()
        from kanpilot import runtime_operations
        job = case.read_job(directory)
        payload = {'scope_id': job['scope_id'], 'operation_id': 'proposal_prop-goal-1_accept',
            'session_id': fixture.SESSION_ID, 'turn_id': fixture.TURN_ID,
            'proposal_id': 'prop-goal-1', 'tool_call_id': 'tool-goal-1', 'version': 3,
            'action': 'accept', 'target_permission_mode': 'auto',
            'required_verification_commands': spec['required_verification_commands']}
        assert row['request_sha256'] == runtime_operations.compute_request_sha256(payload)
        payload.pop('required_verification_commands')
        assert row['request_sha256'] != runtime_operations.compute_request_sha256(payload)
    finally:
        conn.close()


def test_requirement_transport_failure_never_retries_without_requirements(case):
    enable_requirements(case)
    spec, directory = frozen_scene(case)
    case.server.plans_resolve_mode = 'error'
    assert case._run(spec) == 1
    assert case.server.prompt_count == 1
    assert len(case.server.plans_resolve_calls) == 1
    assert case.server.plans_resolve_calls[0]['requiredVerificationCommands'] == spec['required_verification_commands']


@pytest.mark.parametrize('commands,action', [('pytest','approve'), ([1],'approve'), ([''],'approve'), (['pytest'],'reject')])
def test_invalid_requirements_rejected_before_any_write(commands, action):
    facade = runtime_client.RuntimeClient(role='execution', connection_file='/fake',
        project_path='/fake', runtime_config={}, task_id='t', run_id='r')
    with mock.patch.object(facade, '_write') as write:
        with pytest.raises(util.KanpilotError):
            facade.plans_resolve('p','s','t','c',action, version=1,
                                 required_verification_commands=commands)
    write.assert_not_called()


def test_goal_report_legacy_does_not_require_new_capability(case):
    case.server.turn_statuses = ['running', 'completed']
    case.server.messages = case.default_messages()
    spec, directory = case._base()
    assert case._run(spec) == 0
    assert case.server.prompt_count == 1


def test_frozen_command_mismatch_blocks_before_send(case):
    spec, directory = frozen_scene(case)
    spec['required_verification_commands'] = ['wrong']
    assert case._run(spec) == 1
    assert case.server.prompt_count == 0


@pytest.mark.parametrize('supplied', [None, [], ['injected command']])
def test_runtime_scope_rejects_dropped_or_injected_requirements(case, supplied):
    from kanpilot import db, mcp_tools
    enable_requirements(case)
    spec, directory = frozen_scene(case)
    assert case._run(spec) == 0
    scope = case.project_mcp.scope_manager.get_scope(case.read_job(directory)['scope_id'])
    args = {'action': 'accept'}
    if supplied is not None:
        args['required_verification_commands'] = supplied
    conn = db.connect(case.db_path)
    try:
        with pytest.raises(util.KanpilotError):
            mcp_tools._verification_requirements_guard(conn, scope, args)
    finally:
        conn.close()
    assert len(case.server.plans_resolve_calls) == 1


def test_legacy_command_spec_rebuilds_requirements_only_for_new_send(case):
    enable_requirements(case)
    spec, directory = frozen_scene(case)
    expected = spec.pop('required_verification_commands')
    canonical = case.runs_root / spec['task_id'] / spec['run_id'] / 'spec.json'
    canonical.write_text(json.dumps(spec))
    assert case._run(spec) == 0
    assert case.server.plans_resolve_calls[0]['requiredVerificationCommands'] == expected


@pytest.mark.parametrize('field', [None, {}, {'type':'string'}, {'type':'array','items':{'type':'number'}}])
def test_capability_requires_string_array_schema(field):
    client = pi_desktop_backend.MCPClient('http://fake', 'secret')
    properties = {} if field is None else {'requiredVerificationCommands': field}
    with mock.patch.object(client, 'tool_input_properties', return_value=properties):
        assert client.supports_required_verification_commands() is False


@pytest.mark.parametrize('commands', [[], ['pytest', 'pytest'], ['a\nb'], ['a\rb'], ['a\0b'],
    ['好' * 1366], [str(i).zfill(4) + 'x' * 4092 for i in range(17)], [str(i) for i in range(201)], ['\ud800']])
def test_required_commands_contract_bounds(commands):
    with pytest.raises(util.KanpilotError):
        pi_desktop_backend.validate_required_verification_commands(commands, 'approve')


def test_utf8_boundaries_accept_exact_limits():
    commands = [str(i).zfill(4) + 'x' * 4092 for i in range(16)]
    pi_desktop_backend.validate_required_verification_commands(commands, 'approve')
    pi_desktop_backend.validate_required_verification_commands(['好' * 1365 + 'x'], 'approve')


def test_adapter_rejects_requirements_on_ordinary_plan_before_resolve():
    client = mock.MagicMock()
    client.call_tool.return_value = {'plans': [{'id':'p','sessionId':'s','turnId':'t',
        'toolCallId':'c','kind':'plan','status':'pending','version':1}]}
    with mock.patch.object(harness_plus_adapter, '_get_client', return_value=(client, {})):
        with pytest.raises(util.KanpilotError, match='Goal'):
            harness_plus_adapter.plus_proposal_resolve('/fake', session_id='s', turn_id='t',
                proposal_id='p',tool_call_id='c',version=1,action='accept',
                required_verification_commands=['pytest'])
    assert client.call_tool.call_count == 1  # read pending only; no resolve


@pytest.mark.parametrize('kind', ['missing', 'directory', 'symlink', 'invalid_json', 'unknown_policy'])
def test_execution_accept_requires_trusted_spec_even_when_caller_omits_commands(case, kind):
    from types import SimpleNamespace
    from kanpilot import db, mcp_tools
    scope = SimpleNamespace(role='execution', task_id='task', run_id='run')
    directory = case.runs_root / 'task' / 'run'
    directory.mkdir(parents=True)
    path = directory / 'spec.json'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'symlink':
        target = directory / 'target.json'
        target.write_text(json.dumps({'task_id':'task','run_id':'run','completion_policy':'goal_report'}))
        path.symlink_to(target)
    elif kind == 'invalid_json':
        path.write_text('{')
    elif kind == 'unknown_policy':
        path.write_text(json.dumps({'task_id':'task','run_id':'run','completion_policy':'typo'}))
    conn = db.connect(case.db_path)
    try:
        with pytest.raises(util.KanpilotError):
            mcp_tools._verification_requirements_guard(conn, scope, {'action':'accept'})
    finally:
        conn.close()
    assert case.server.plans_resolve_calls == []


@pytest.mark.parametrize('policy', ['goal_report', 'historical_absent'])
def test_trusted_legacy_goal_report_spec_accepts_without_new_requirements(case, policy):
    from types import SimpleNamespace
    from kanpilot import db, mcp_tools
    scope = SimpleNamespace(role='execution', task_id='task', run_id='run')
    directory = case.runs_root / 'task' / 'run'
    directory.mkdir(parents=True)
    spec = {'task_id':'task','run_id':'run'}
    if policy == 'goal_report':
        spec['completion_policy'] = policy
    (directory / 'spec.json').write_text(json.dumps(spec))
    conn = db.connect(case.db_path)
    try:
        mcp_tools._verification_requirements_guard(conn, scope, {'action':'accept'})
    finally:
        conn.close()


def test_unreadable_execution_spec_cannot_approve(case):
    from types import SimpleNamespace
    from kanpilot import db, mcp_tools
    scope = SimpleNamespace(role='execution', task_id='task', run_id='run')
    directory = case.runs_root / 'task' / 'run'
    directory.mkdir(parents=True)
    path = directory / 'spec.json'
    path.write_text('{}')
    conn = db.connect(case.db_path)
    try:
        with mock.patch.object(type(path), 'read_text', side_effect=PermissionError('unreadable')):
            with pytest.raises(util.KanpilotError, match='无法读取'):
                mcp_tools._verification_requirements_guard(conn, scope, {'action':'accept'})
    finally:
        conn.close()


def test_missing_canonical_spec_never_reaches_plus_approval_even_without_commands(case):
    spec, directory = case._base()
    (case.runs_root / spec['task_id'] / spec['run_id'] / 'spec.json').unlink()
    case.server.turn_statuses = ['running', 'completed']
    case.server.turn_status = 'completed'
    case.server.plans = [case._goal_contract()]
    case.server.planning_state = 'awaiting_approval'
    case.server.execution_turn = 'turn-goal-exec'
    case.server.messages = case.default_messages()
    assert case._run(spec) == 1
    assert case.server.plans_resolve_calls == []
    assert 'goal_approval_failed' in case.read_job(directory)['detail']
