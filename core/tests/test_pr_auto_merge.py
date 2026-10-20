"""Auto merge uses isolated DB/git and fake GitHub/Kaneo only."""
from datetime import timedelta
from unittest.mock import patch

import pytest

import test_delivery_pr as fixture_pr
from kanpilot import automation_policy, core, db, gh_client, project_config, remote, util


@pytest.fixture
def delivery():
    case = fixture_pr.DeliveryPR('test_open_backoff_and_closed')
    case.setUp()
    try:
        case.update(pr_number=7, pushed_sha=case.delivery['head_sha'])
        task = case.task()
        db.set_setting(case.ctx.conn, project_config.SETTINGS_KEY, {
            'items': {task['project_id']: {'repository': str(case.repo), 'enabled': False,
                'automation': {'enabled': False, 'auto_merge_pr': True, 'policy_revision': 1}}}})
        source = {'task': {'id': task['task_id'], 'projectId': task['project_id'], 'status': 'in-review'},
                  'requirements_sha256': task['requirements_sha256']}
        with patch.object(remote, 'get_task', side_effect=case.outside('get_task', source)):
            yield case
    finally:
        case.doCleanups()
        case.tearDown()


def ready(case, state='OPEN', **changes):
    return case.pr(state, isDraft=False, mergeable='MERGEABLE', mergeStateStatus='CLEAN',
                   reviewDecision='', statusCheckRollup=[], **changes)


def policy(case, **changes):
    value = db.get_setting(case.ctx.conn, project_config.SETTINGS_KEY)
    value['items'][case.task()['project_id']]['automation'].update(changes)
    db.set_setting(case.ctx.conn, project_config.SETTINGS_KEY, value)


def receipt(case):
    return case.ctx.conn.execute("SELECT * FROM runtime_operations WHERE tool='github_pr_merge'").fetchone()


def test_clean_merges_once_then_observes_and_records_event(delivery):
    case = delivery
    calls = []
    def merge(repo, number, *, sha):
        assert not case.ctx.conn.in_transaction
        assert receipt(case)['status'] == 'unknown'
        calls.append((repo, number, sha))
        return {'merged': True}
    with patch.object(gh_client, 'pr_view', side_effect=[ready(case), ready(case, 'MERGED'), ready(case, 'MERGED')]), patch.object(
            gh_client, 'pr_merge', side_effect=merge, create=True):
        assert case.sync()['outcome'] == 'merged'
        assert case.sync()['outcome'] == 'merged'
    assert calls == [('owner/repo', 7, case.delivery['head_sha'])]
    assert receipt(case)['status'] == 'accepted'
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'merged'
    assert case.task()['status'] == 'in_review'  # runner, not sync, advances Kaneo.
    assert case.ctx.conn.execute("SELECT count(*) FROM events WHERE kind='pr_merged'").fetchone()[0] == 1


@pytest.mark.parametrize('changes', [
    {'isDraft': True}, {'isDraft': None}, {'mergeable': 'UNKNOWN'}, {'mergeable': 'CONFLICTING'},
    {'mergeStateStatus': 'BLOCKED'}, {'mergeStateStatus': None}, {'reviewDecision': 'CHANGES_REQUESTED'},
    {'reviewDecision': 'REVIEW_REQUIRED'}, {'reviewDecision': None}, {'statusCheckRollup': None},
    {'statusCheckRollup': [{'__typename': 'CheckRun', 'status': 'IN_PROGRESS', 'conclusion': ''}]},
    {'statusCheckRollup': [{'__typename': 'CheckRun', 'status': 'COMPLETED', 'conclusion': 'FAILURE'}]},
    {'statusCheckRollup': [{'__typename': 'StatusContext', 'state': 'PENDING'}]},
    {'statusCheckRollup': [{}]},
])
def test_unqualified_never_writes_or_fails_task(delivery, changes):
    case = delivery
    value = ready(case); value.update(changes)
    with patch.object(gh_client, 'pr_view', return_value=value), patch.object(gh_client, 'pr_merge', create=True) as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_not_called()
    assert receipt(case) is None
    assert case.task()['status'] == 'in_review'
    assert core.delivery_of(case.task())['auto_merge']['state'] in ('blocked', 'waiting_checks')


@pytest.mark.parametrize('checks', [[],
    [{'__typename': 'CheckRun', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}],
    [{'__typename': 'CheckRun', 'status': 'COMPLETED', 'conclusion': 'NEUTRAL'},
     {'__typename': 'CheckRun', 'status': 'COMPLETED', 'conclusion': 'SKIPPED'},
     {'__typename': 'StatusContext', 'state': 'SUCCESS'}]])
def test_all_success_forms(delivery, checks):
    case = delivery
    value = ready(case); value['statusCheckRollup'] = checks
    with patch.object(gh_client, 'pr_view', side_effect=[value, ready(case, 'MERGED')]), patch.object(
            gh_client, 'pr_merge', return_value={'merged': True}, create=True) as merge:
        assert case.sync()['outcome'] == 'merged'
    merge.assert_called_once_with('owner/repo', 7, sha=case.delivery['head_sha'])


def test_unknown_never_resends_even_after_policy_revision(delivery):
    case = delivery
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', side_effect=gh_client.GhError('timeout', 'timeout'), create=True) as merge:
        assert case.sync()['outcome'] == 'waiting'
        assert receipt(case)['status'] == 'unknown'
        policy(case, policy_revision=3)
        assert case.sync()['outcome'] == 'waiting'
        merge.assert_called_once()
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'unknown'
    with patch.object(gh_client, 'pr_view', return_value=ready(case, 'MERGED')):
        assert case.sync()['outcome'] == 'merged'
    assert receipt(case)['status'] == 'accepted'


def test_explicit_rejection_never_resends(delivery):
    case = delivery
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', return_value={'merged': False, 'message': 'branch protection'}, create=True) as merge:
        assert case.sync()['outcome'] == 'waiting'
        assert case.sync()['outcome'] == 'waiting'
        merge.assert_called_once()
    assert receipt(case)['status'] == 'rejected'
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'blocked'


@pytest.mark.parametrize('mutation', ['off', 'revision', 'mapping', 'global_pause', 'task_pause', 'lease', 'report', 'review', 'run'])
def test_mutation_during_read_prevents_write(delivery, mutation):
    case = delivery
    def read(*args, **kwargs):
        if mutation == 'off': policy(case, auto_merge_pr=False)
        elif mutation == 'revision': policy(case, policy_revision=2)
        elif mutation == 'mapping':
            value = db.get_setting(case.ctx.conn, project_config.SETTINGS_KEY)
            value['items'][case.task()['project_id']]['repository'] = str(case.root / 'other')
            db.set_setting(case.ctx.conn, project_config.SETTINGS_KEY, value)
        elif mutation == 'global_pause': db.set_setting(case.ctx.conn, 'paused_global', True)
        elif mutation == 'task_pause': db.update_task(case.ctx.conn, 'task1234', case.ctx.now_iso, auto_advance_paused=1)
        elif mutation == 'lease': case.clock += timedelta(hours=2)
        elif mutation == 'report':
            from pathlib import Path
            Path(case.task()['review_report_path']).write_text('changed')
        elif mutation == 'review': db.update_task(case.ctx.conn, 'task1234', case.ctx.now_iso, review_key='new')
        elif mutation == 'run': db.update_task(case.ctx.conn, 'task1234', case.ctx.now_iso, run_id='new')
        return ready(case)
    with patch.object(gh_client, 'pr_view', side_effect=read), patch.object(gh_client, 'pr_merge', create=True) as merge:
        with pytest.raises(util.KanpilotError): case.sync()
    merge.assert_not_called()
    assert receipt(case) is None


def test_disabled_default_observes_without_write(delivery):
    case = delivery
    policy(case, auto_merge_pr=False)
    with patch.object(gh_client, 'pr_view', return_value=case.pr()), patch.object(gh_client, 'pr_merge', create=True) as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_not_called()
    assert receipt(case) is None


def test_policy_validation_and_enable_wakes_existing_delivery(delivery):
    case = delivery
    policy(case, auto_merge_pr=False)
    case.update(next_sync_at=util.plus_seconds(1800, case.ctx.now))
    assert automation_policy.validate_automation({})['auto_merge_pr'] is False
    with pytest.raises(util.KanpilotError): automation_policy.validate_automation({'auto_merge_pr': 'yes'})
    value = automation_policy.cmd_set_project_automation(case.ctx, {
        'project_id': case.task()['project_id'], 'expected_policy_revision': 1,
        'automation': {'enabled': False, 'auto_merge_pr': True}})
    assert value['automation']['auto_merge_pr'] is True
    assert core.delivery_of(case.task())['next_sync_at'] == case.ctx.now_iso


def test_concurrent_reentrant_sync_never_duplicates_send(delivery):
    case = delivery
    def sending(*args, **kwargs):
        assert receipt(case)['status'] == 'unknown'
        assert case.sync()['outcome'] == 'waiting'
        return {'merged': True}
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', side_effect=sending) as merge:
        # The competing observer updates the delivery snapshot. Its stale sender
        # may not overwrite that newer state, but the persisted receipt survives.
        with pytest.raises(util.KanpilotError): case.sync()
        assert case.sync()['outcome'] == 'waiting'
        merge.assert_called_once()
    assert receipt(case)['status'] == 'unknown'


def test_policy_switch_between_receipt_and_send_stops_before_side_effect(delivery):
    case = delivery
    from kanpilot import runtime_operations
    original = runtime_operations.mark_operation_unknown
    def change(*args, **kwargs):
        original(*args, **kwargs)
        policy(case, auto_merge_pr=False)
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            runtime_operations, 'mark_operation_unknown', side_effect=change), patch.object(gh_client, 'pr_merge') as merge:
        with pytest.raises(util.KanpilotError): case.sync()
    merge.assert_not_called()
    assert receipt(case)['status'] == 'unknown'
    # A disabled policy may still read and reconcile the already reserved intent.
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(gh_client, 'pr_merge') as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_not_called()


def test_crash_with_prepared_receipt_never_resends(delivery):
    case = delivery
    from kanpilot import runtime_operations
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            runtime_operations, 'mark_operation_unknown', side_effect=RuntimeError('crash')), patch.object(gh_client, 'pr_merge') as merge:
        with pytest.raises(RuntimeError): case.sync()
    assert receipt(case)['status'] == 'prepared'
    merge.assert_not_called()
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(gh_client, 'pr_merge') as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_not_called()
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'unknown'


@pytest.mark.parametrize('error', ['timeout', 'transient'])
def test_merge_success_followup_read_failure_keeps_unknown(delivery, error):
    case = delivery
    with patch.object(gh_client, 'pr_view', side_effect=[ready(case), gh_client.GhError(error, 'read unavailable')]), patch.object(
            gh_client, 'pr_merge', return_value={'merged': True}) as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_called_once()
    assert receipt(case)['status'] == 'unknown'
    assert 'merged_at' not in core.delivery_of(case.task())


def test_success_response_does_not_establish_merge_without_observation(delivery):
    case = delivery
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', return_value={'merged': True}) as merge:
        assert case.sync()['outcome'] == 'waiting'
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_called_once()
    assert receipt(case)['status'] == 'unknown'
    assert 'merged_at' not in core.delivery_of(case.task())


def test_origin_changed_during_read_stops_merge(delivery):
    case = delivery
    from kanpilot import git_delivery
    with patch.object(git_delivery, 'origin_github_repo', side_effect=['owner/repo', 'other/repo']), patch.object(
            gh_client, 'pr_view', return_value=ready(case)), patch.object(gh_client, 'pr_merge') as merge:
        with pytest.raises(util.KanpilotError): case.sync()
    merge.assert_not_called()
    assert receipt(case) is None


def test_success_recheck_with_wrong_identity_stays_unknown(delivery):
    case = delivery
    wrong = ready(case, 'MERGED'); wrong['headRefOid'] = 'b' * 40
    with patch.object(gh_client, 'pr_view', side_effect=[ready(case), wrong]), patch.object(
            gh_client, 'pr_merge', return_value={'merged': True}):
        assert case.sync()['outcome'] == 'waiting'
    assert receipt(case)['status'] == 'unknown'
    assert 'merged_at' not in core.delivery_of(case.task())


@pytest.mark.parametrize('kind', ['rejected', 'auth', 'not_found', 'missing'])
def test_definitive_cli_refusal_not_retried(delivery, kind):
    case = delivery
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', side_effect=gh_client.GhError(kind, 'token=secret123456 refused')) as merge:
        assert case.sync()['outcome'] == 'waiting'
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_called_once()
    assert receipt(case)['status'] == 'rejected'
    assert 'secret123456' not in receipt(case)['result_json']
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'blocked'


@pytest.mark.parametrize('mutation', ['mapping', 'global_pause', 'stage_pause', 'task_pause', 'takeover', 'lease', 'report'])
def test_initial_gate_blocks_before_any_external_read_or_write(delivery, mutation):
    case = delivery
    if mutation == 'mapping':
        value = db.get_setting(case.ctx.conn, project_config.SETTINGS_KEY)
        value['items'][case.task()['project_id']]['repository'] = str(case.root / 'other')
        db.set_setting(case.ctx.conn, project_config.SETTINGS_KEY, value)
    elif mutation == 'global_pause': db.set_setting(case.ctx.conn, 'paused_global', True)
    elif mutation == 'stage_pause': db.set_setting(case.ctx.conn, 'paused_stages', ['delivery'])
    elif mutation == 'task_pause': db.update_task(case.ctx.conn, 'task1234', case.ctx.now_iso, auto_advance_paused=1)
    elif mutation == 'takeover': core.update_automation(case.ctx.conn, case.task(), case.ctx.now_iso, manual_takeover=True)
    elif mutation == 'lease': case.clock += timedelta(hours=2)
    elif mutation == 'report':
        from pathlib import Path
        Path(case.task()['review_report_path']).write_text('changed')
    with patch.object(gh_client, 'pr_merge') as merge:
        with pytest.raises(util.KanpilotError): case.sync()
    merge.assert_not_called()
    assert case.order == []
    assert receipt(case) is None


@pytest.mark.parametrize('value', [{}, {'merged': None}, {'merged': 'true'}, None])
def test_untrusted_merge_response_never_resends(delivery, value):
    case = delivery
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', return_value=value) as merge:
        assert case.sync()['outcome'] == 'waiting'
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_called_once()
    assert receipt(case)['status'] == 'unknown'


def test_policy_change_plus_failed_read_does_not_block_or_mutate_task(delivery):
    case = delivery
    def fails(*args, **kwargs):
        policy(case, auto_merge_pr=False)
        raise gh_client.GhError('auth', 'denied')
    before = core.delivery_of(case.task())
    with patch.object(gh_client, 'pr_view', side_effect=fails), patch.object(gh_client, 'pr_merge') as merge:
        with pytest.raises(util.KanpilotError) as error: case.sync()
    assert error.value.code == 'conflict'
    merge.assert_not_called()
    assert case.task()['status'] == 'in_review'
    assert core.delivery_of(case.task()) == before


@pytest.mark.parametrize('mutation', ['task', 'project', 'status', 'requirements', 'missing_requirements'])
def test_changed_kaneo_source_stops_new_merge(delivery, mutation):
    case = delivery
    source = {'task': {'id': case.task()['task_id'], 'projectId': case.task()['project_id'], 'status': 'in-review'},
              'requirements_sha256': case.task()['requirements_sha256']}
    if mutation == 'task': source['task']['id'] = 'other'
    elif mutation == 'project': source['task']['projectId'] = 'other'
    elif mutation == 'status': source['task']['status'] = 'in-progress'
    elif mutation == 'requirements': source['requirements_sha256'] = 'changed'
    else: source.pop('requirements_sha256')
    with patch.object(remote, 'get_task', return_value=source), patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(gh_client, 'pr_merge') as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_not_called()
    assert receipt(case) is None
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'blocked'
    assert case.task()['status'] == 'in_review'


def test_unknown_still_reconciles_after_kaneo_source_changes(delivery):
    case = delivery
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', side_effect=gh_client.GhError('timeout', 'timeout')):
        assert case.sync()['outcome'] == 'waiting'
    with patch.object(remote, 'get_task', return_value={'task': {'status': 'in-progress'}}), patch.object(
            gh_client, 'pr_view', return_value=ready(case, 'MERGED')), patch.object(gh_client, 'pr_merge') as merge:
        assert case.sync()['outcome'] == 'merged'
    merge.assert_not_called()
    assert receipt(case)['status'] == 'accepted'


def test_new_review_identity_cannot_bypass_unresolved_merge(delivery):
    case = delivery
    from pathlib import Path
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            gh_client, 'pr_merge', side_effect=gh_client.GhError('timeout', 'timeout')) as merge:
        assert case.sync()['outcome'] == 'waiting'
        original = receipt(case)['operation_id']
        report = Path(case.task()['review_report_path']).with_name('review2.md')
        report.write_text('new Review pass')
        report_hash = util.sha256_file(report)
        with db.write_txn(case.ctx.conn):
            history = db.task_extra(case.task())['review_history']
            history.append(dict(history[-1], review_key='review2', report_path=str(report), report_sha256=report_hash))
            db.set_extra(case.ctx.conn, case.task(), case.ctx.now_iso, review_history=history)
            db.update_task(case.ctx.conn, 'task1234', case.ctx.now_iso, review_key='review2',
                           review_report_path=str(report), review_report_sha256=report_hash)
        assert case.sync()['outcome'] == 'waiting'
        merge.assert_called_once()
    assert receipt(case)['operation_id'] == original
    assert case.ctx.conn.execute("SELECT count(*) FROM runtime_operations WHERE tool='github_pr_merge'").fetchone()[0] == 1
    assert core.delivery_of(case.task())['auto_merge']['state'] == 'unknown'
    with patch.object(gh_client, 'pr_view', return_value=ready(case, 'MERGED')):
        assert case.sync()['outcome'] == 'merged'
    assert receipt(case)['status'] == 'accepted'


def test_enabled_auto_merge_keeps_fast_poll_after_old_review(delivery):
    case = delivery
    db.set_extra(case.ctx.conn, case.task(), case.ctx.now_iso,
                 review_registered_at=util.to_iso(case.ctx.now - timedelta(hours=2)))
    pr = ready(case); pr['isDraft'] = True
    with patch.object(gh_client, 'pr_view', return_value=pr), patch.object(gh_client, 'pr_merge') as merge:
        result = case.sync()
    assert util.parse_ts(result['next_sync_at']) - case.ctx.now == timedelta(seconds=300)
    merge.assert_not_called()


def test_concurrent_observer_acceptance_cannot_be_downgraded_and_sent(delivery):
    case = delivery
    from kanpilot import runtime_operations
    original = runtime_operations.begin_operation
    def observed(*args, **kwargs):
        value = original(*args, **kwargs)
        # Simulates a concurrent confirmed observation in the gap after the
        # prepared commit and before the sender's next transaction.
        db.update_runtime_operation(case.ctx.conn, args[1], args[2], case.ctx.now_iso, status='accepted')
        return value
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(
            runtime_operations, 'begin_operation', side_effect=observed), patch.object(gh_client, 'pr_merge') as merge:
        assert case.sync()['outcome'] == 'waiting'
    merge.assert_not_called()
    assert receipt(case)['status'] == 'accepted'


@pytest.mark.parametrize('exception', [False, True])
def test_late_refusal_cannot_overwrite_confirmed_acceptance(delivery, exception):
    case = delivery
    def merge(*args, **kwargs):
        row = receipt(case)
        with db.write_txn(case.ctx.conn):
            db.update_runtime_operation(case.ctx.conn, row['scope_id'], row['operation_id'], case.ctx.now_iso, status='accepted')
        if exception: raise gh_client.GhError('rejected', 'already merged')
        return {'merged': False, 'message': 'already merged'}
    with patch.object(gh_client, 'pr_view', return_value=ready(case)), patch.object(gh_client, 'pr_merge', side_effect=merge) as sender:
        assert case.sync()['outcome'] == 'waiting'
        assert case.sync()['outcome'] == 'waiting'
    sender.assert_called_once()
    assert receipt(case)['status'] == 'accepted'
