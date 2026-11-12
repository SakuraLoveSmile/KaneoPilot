"""Continuation regressions use disposable state and the local fake Plus host."""
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
import test_pi_desktop_backend as fixtures
from kanpilot import (cmd_request_continuation as continuation, cmd_session, db,
                     pi_desktop_worker as worker, round_store, runtime_client,
                     runtime_operations, util)


@pytest.fixture
def scene():
    case = fixtures.TestRunReal('test_completed_flow_events_and_order')
    case.setUp()
    try:
        spec, directory = case._base()
        job = case.write_lost_observer_scene(spec, directory, status='running')
        now = util.now_dt()
        job.update(started_at=util.to_iso(now - timedelta(hours=13)),
                   continuation_started_at=util.to_iso(now - timedelta(minutes=2)),
                   turn_id=fixtures.FOLLOW_UP_TURN_ID,
                   goal_approval={'status': 'approved', 'proposal_id': 'proposal',
                                  'execution_turn_id': fixtures.TURN_ID})
        adoption = dict(task_id=spec['task_id'], run_id=spec['run_id'],
                        session_id=fixtures.SESSION_ID, from_turn=fixtures.TURN_ID,
                        new_turn=fixtures.FOLLOW_UP_TURN_ID, operation_id='continue-op')
        job['continuation_adoption'] = adoption
        job['turn_adopted'] = [{'from': fixtures.TURN_ID, 'to': fixtures.FOLLOW_UP_TURN_ID,
                                'code': 'manual_continue', 'at': job['continuation_started_at']}]
        conn = db.connect(case.db_path)
        try:
            db.update_task(conn, spec['task_id'], util.now_iso(), session_id=fixtures.SESSION_ID, run_id=spec['run_id'])
            db.set_extra(conn, db.require_task(conn, spec['task_id']), util.now_iso(),
                         execution_continuation_request=dict(adoption, status='sent'))
            scope = runtime_client.stable_scope_id('execution', task_id=spec['task_id'], run_id=spec['run_id'])
            runtime_operations.begin_operation(conn, scope, 'continue-op', 'kaneo-request-continuation',
                'request-continuation', dict(task_id=spec['task_id'], run_id=spec['run_id'],
                    session_id=fixtures.SESSION_ID, turn_id=fixtures.TURN_ID))
            runtime_operations.complete_operation(conn, scope, 'continue-op',
                session_id=fixtures.SESSION_ID, turn_id=fixtures.FOLLOW_UP_TURN_ID)
        finally:
            conn.close()
        spec['timeout_seconds'] = 14400
        (directory / 'job.json').write_text(json.dumps(job))
        case.server.current_turn = fixtures.FOLLOW_UP_TURN_ID
        case.server.turn_status = 'completed'
        case.server.planning_state = 'inactive'
        case.server.messages = case.default_messages()
        fixtures._sync(case.server)
        yield case, spec, directory, job
    finally:
        case.tearDown()
        case.doCleanups()


def test_old_job_accepted_continuation_settles_without_resending(scene):
    case, spec, directory, job = scene
    report = dict(sessionId=fixtures.SESSION_ID, executionId='goal-execution',
                  proposalId='proposal', turnId=fixtures.FOLLOW_UP_TURN_ID,
                  integrity={'kind': 'structured'}, execution={'status': 'completed'}, verdict='met')
    case.server.goal_reports = [dict(executionId='goal-execution', reportId='continued-report',
        proposalId='proposal', turnId=fixtures.FOLLOW_UP_TURN_ID)]
    case.server.goal_report = dict(state='ready', report=report,
        report_sha256=util.sha256_text(util.jdump(report)))
    fixtures._sync(case.server)
    with case.fast_clock(), patch.object(worker, '_GOAL_REPORT_GRACE_SECONDS', 0):
        assert worker.run_real(spec, resume=True) == 0
    after = case.read_job(directory)
    assert after['status'] == 'finished' and after['exit_code'] == 0
    assert after['started_at'] == job['started_at']
    assert after['goal_approval']['observation_turn_id'] == fixtures.FOLLOW_UP_TURN_ID
    assert after['goal_approval']['execution_turn_id'] == fixtures.TURN_ID
    assert after['goal_report']['turn_id'] == fixtures.FOLLOW_UP_TURN_ID
    assert {name for name, args in case.server.goal_report_calls} == {'pi_goal_report_list', 'pi_goal_report_get'}
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []
    assert case.server.stop_calls == case.server.abort_calls == 0


@pytest.mark.parametrize('fault', ['missing_receipt', 'unknown_receipt', 'wrong_turn', 'wrong_request', 'wrong_from'])
def test_unacknowledged_or_foreign_continuation_does_not_inherit_goal(scene, fault):
    case, spec, directory, job = scene
    conn = db.connect(case.db_path)
    try:
        if fault == 'missing_receipt': conn.execute('DELETE FROM runtime_operations WHERE operation_id=?', ('continue-op',))
        elif fault == 'unknown_receipt': conn.execute("UPDATE runtime_operations SET status='unknown' WHERE operation_id=?", ('continue-op',))
        elif fault == 'wrong_turn': conn.execute("UPDATE runtime_operations SET turn_id='foreign' WHERE operation_id=?", ('continue-op',))
        elif fault == 'wrong_request': conn.execute("UPDATE runtime_operations SET request_sha256='foreign' WHERE operation_id=?", ('continue-op',))
        else:
            job['continuation_adoption']['from_turn'] = 'foreign'
            (directory / 'job.json').write_text(json.dumps(job))
    finally:
        conn.close()
    with case.fast_clock():
        assert worker.run_real(spec, resume=True) == 3
    after = case.read_job(directory)
    assert after['status'] == 'running' and 'goal_resume_unconfirmed' in after['resume_detail']
    assert 'observation_turn_id' not in after['goal_approval']
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []


def test_continuation_timeout_projection_preserves_original_start_and_deadline(scene):
    case, spec, directory, job = scene
    row = dict(task_id=spec['task_id'], run_id=spec['run_id'], run_status='running',
               backend='pi_desktop', started_at=job['started_at'])
    now = util.parse_ts(job['continuation_started_at'])
    assert round_store.derive_run_status(row, job, now + timedelta(hours=1), 14400) == 'running'
    assert round_store.derive_run_status(row, job, now + timedelta(hours=4, seconds=1), 14400) == 'failed'
    legacy = dict(job)
    legacy.pop('continuation_started_at')
    assert round_store.derive_run_status(row, legacy, now, 14400) == 'failed'


def test_old_segment_input_wait_cannot_extend_new_segment_budget(scene):
    case, spec, directory, job = scene
    start = util.parse_ts(job['continuation_started_at'])
    now = start + timedelta(hours=4, seconds=1)
    task = dict(task_id=spec['task_id'], run_id=spec['run_id'], extra_json=util.jdump({
        'execution_input_wait_intervals': [dict(run_id=spec['run_id'],
            start=util.to_iso(start - timedelta(hours=10)), end=util.to_iso(start - timedelta(hours=2)))]}))
    with patch.object(worker, '_core_input_state', return_value=task), \
            patch.object(util, 'now_dt', return_value=now), patch.object(util, 'now_iso', return_value=util.to_iso(now)):
        assert worker._round_remaining_seconds(job, 14400, spec) == -1
    row = dict(task_id=spec['task_id'], run_id=spec['run_id'], run_status='running',
               backend='pi_desktop', started_at=job['started_at'])
    with patch.object(db, 'get_task', return_value=task):
        assert cmd_session._round_run_status(None, row, job, now, 14400) == 'failed'
