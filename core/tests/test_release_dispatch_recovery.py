"""Isolated explicit recovery; all GitHub calls use the lifecycle fake."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from kanpilot import cli, core, db, gh_client, runtime_operations, util
from kanpilot import release_delivery as release, release_dispatch_recovery as recovery
import test_release_delivery as lifecycle


@pytest.fixture
def case():
    value = lifecycle.ReleaseLifecycleTest('test_unknown_recovers_unique_marker_and_never_resends')
    value.setUp()
    try:
        value.api.lose_reply = True
        result = value.start()
        assert result['release']['state'] == 'unknown'
        value.batch = release._batch(value.conn, result['release']['batch_id'])
        value.original_runs = deepcopy(value.api.runs)
        value.api.runs = []
        value.api.lose_reply = False
        value.original = dict(db.get_runtime_operation(value.conn, release._scope('project'), 'dispatch:' + value.batch['batch_id']))
        value.payload = dict(task_id='parent', batch_id=value.batch['batch_id'], expected_context_sha256=value.batch['context_sha256'],
                             expected_dispatch_request_sha256=value.original['request_sha256'], confirm=True)
        yield value
    finally:
        value.doCleanups()


def test_recovery_sends_same_frozen_batch_once_and_preserves_original_audit(case):
    result = recovery.cmd_recover_release_dispatch(case.ctx, case.payload)
    assert result['release']['state'] == 'queued'
    assert result['release']['tag'] == case.batch['tag']
    assert result['release']['batch_id'] == case.batch['batch_id']
    assert len(case.api.sent) == 2 and case.api.sent[0] == case.api.sent[1]
    row = recovery.receipt(case.conn, case.batch)
    audit = util.jload(row['result_json'])
    assert row['status'] == 'accepted' and audit['original_dispatch'] == case.original
    assert audit['resolution_source'] == 'dispatch_response' and audit['duplicate_risk_confirmed'] is True
    assert recovery.cmd_recover_release_dispatch(case.ctx, case.payload)['release']['state'] == 'queued'
    assert len(case.api.sent) == 2
    assert not db.get_setting(case.conn, 'lifecycle_owner')


def test_lost_recovery_reply_never_resends_and_read_only_sync_resolves_receipt(case):
    case.api.lose_reply = True
    assert recovery.cmd_recover_release_dispatch(case.ctx, case.payload)['release']['state'] == 'unknown'
    assert recovery.cmd_recover_release_dispatch(case.ctx, case.payload)['release']['state'] == 'unknown'
    assert len(case.api.sent) == 2
    result = case.sync(case.batch['batch_id'])
    assert result['release']['state'] == 'queued'
    row = recovery.receipt(case.conn, case.batch)
    assert row['status'] == 'accepted'
    audit = util.jload(row['result_json'])
    assert audit['original_dispatch'] == case.original and audit['resolution_source'] == 'read_only_observation'
    assert len(case.api.sent) == 2


def test_existing_exact_run_is_only_reconciled_without_consuming_recovery(case):
    case.api.runs = case.original_runs
    assert recovery.cmd_recover_release_dispatch(case.ctx, case.payload)['release']['state'] == 'queued'
    assert len(case.api.sent) == 1
    assert recovery.receipt(case.conn, case.batch) is None


@pytest.mark.parametrize('change', ['wrong_head', 'duplicate', 'incomplete'])
def test_ambiguous_or_incomplete_remote_evidence_never_sends(case, change):
    case.api.runs = deepcopy(case.original_runs)
    if change == 'wrong_head':
        case.api.runs[0]['head_sha'] = '0' * 40
    elif change == 'duplicate':
        case.api.runs *= 2
    else:
        case.api.find_runs = lambda *args: (_ for _ in ()).throw(gh_client.GhError('transient', 'incomplete pages'))
    with pytest.raises((release.ReleaseBlocked, gh_client.GhError)):
        recovery.cmd_recover_release_dispatch(case.ctx, case.payload)
    assert len(case.api.sent) == 1 and recovery.receipt(case.conn, case.batch) is None


@pytest.mark.parametrize('field,value', [('confirm', False), ('expected_context_sha256', '0' * 64),
                                        ('expected_dispatch_request_sha256', '0' * 64), ('task_id', 'fix1')])
def test_confirmation_and_frozen_identity_are_required(case, field, value):
    with pytest.raises(release.ReleaseBlocked):
        recovery.cmd_recover_release_dispatch(case.ctx, dict(case.payload, **{field: value}))
    assert len(case.api.sent) == 1 and recovery.receipt(case.conn, case.batch) is None


def test_pause_at_final_remote_read_prevents_reservation_and_send(case):
    calls = []
    def find(*args):
        assert not case.conn.in_transaction
        calls.append(args)
        if len(calls) == 2:
            db.set_setting(case.conn, 'paused_global', True)
        return []
    case.api.find_runs = find
    with pytest.raises(release.ReleaseBlocked):
        recovery.cmd_recover_release_dispatch(case.ctx, case.payload)
    assert len(case.api.sent) == 1 and recovery.receipt(case.conn, case.batch) is None


@pytest.mark.parametrize('matches', [False, True])
def test_stale_sync_cannot_overwrite_a_new_recovery_writer(case, matches):
    def find(*args):
        with db.write_txn(case.conn):
            runtime_operations.begin_operation(case.conn, release._scope('project'), recovery.operation_id(case.batch),
                                               'github', recovery.TOOL, {}, now=case.ctx.now_iso)
            runtime_operations.complete_operation(case.conn, release._scope('project'), recovery.operation_id(case.batch),
                status='unknown', result=dict(lifecycle_owner='new-writer', process_identity=dict(pid=123, started_seconds=1, started_microseconds=2)), now=case.ctx.now_iso)
            core.acquire_lifecycle(case.conn, 'new-writer')
        case.api.runs = deepcopy(case.original_runs)
        return case.api.runs if matches else []
    case.api.find_runs = find
    result = case.sync(case.batch['batch_id'])
    assert result['release']['state'] == 'unknown' and result['release'].get('workflow_run_id') is None
    assert dict(db.get_runtime_operation(case.conn, release._scope('project'), 'dispatch:' + case.batch['batch_id'])) == case.original
    assert util.jload(recovery.receipt(case.conn, case.batch)['result_json'])['lifecycle_owner'] == 'new-writer'
    assert db.get_setting(case.conn, 'lifecycle_owner') == 'new-writer'
    assert len(case.api.sent) == 1


def test_recovery_writer_is_recoverable_even_if_old_sync_overwrote_original_receipt(case):
    case.api.lose_reply = True
    recovery.cmd_recover_release_dispatch(case.ctx, case.payload)
    audit = util.jload(recovery.receipt(case.conn, case.batch)['result_json'])
    db.set_setting(case.conn, 'lifecycle_owner', audit['lifecycle_owner'])
    runtime_operations.complete_operation(case.conn, release._scope('project'), 'dispatch:' + case.batch['batch_id'],
        status='unknown', result=dict(batch_id=case.batch['batch_id']), now=case.ctx.now_iso)
    birth = {key: audit['process_identity'][key] for key in ('started_seconds', 'started_microseconds')}
    with patch.object(release.process_owner, 'process_birth', return_value=birth):
        assert case.sync(case.batch['batch_id'])['release']['state'] == 'unknown'
        assert db.get_setting(case.conn, 'lifecycle_owner') == audit['lifecycle_owner']
    with patch.object(release.process_owner, 'process_birth', return_value=dict(started_seconds=0, started_microseconds=0)):
        assert case.sync(case.batch['batch_id'])['release']['state'] == 'queued'
    assert not db.get_setting(case.conn, 'lifecycle_owner')
    assert util.jload(recovery.receipt(case.conn, case.batch)['result_json'])['original_dispatch'] == case.original
    assert len(case.api.sent) == 2


def test_recovery_command_has_fixed_cli_schema():
    payload = dict(task_id='parent', batch_id='batch', expected_context_sha256='a' * 64,
                   expected_dispatch_request_sha256='b' * 64, confirm=True)
    cli.validate_payload('recover-release-dispatch', payload)
    with pytest.raises(util.KanpilotError):
        cli.validate_payload('recover-release-dispatch', dict(payload, auto_retry=True))
