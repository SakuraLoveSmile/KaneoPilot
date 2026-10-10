"""Shared Review settlement: fixed core commands only, no external services."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kanpilot import constants, pi_desktop_review as legacy, review_settle, util


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('KANPILOT_APP_SUPPORT', str(tmp_path / 'support'))
    monkeypatch.setenv('KANPILOT_REPORTS_ROOT', str(tmp_path / 'reports'))
    task = {'task_id': 't1', 'run_id': 'exec1', 'status': 'in_review', 'extra': {},
            'review_summary': None, 'review_report_sha256': None}
    request = {'request_id': 'req1', 'run_id': 'review1', 'stage': 'review',
               'status': 'claimed', 'lease_expires_at': util.to_iso(util.now_dt() + __import__('datetime').timedelta(minutes=10))}
    calls = []
    def core_call(_path, command, payload):
        calls.append((command, dict(payload)))
        if command == 'get-task':
            return {'task': task}
        if command == 'get-request':
            return {'request': request}
        if command == 'create-review-fix':
            return {'fix_task_id': 'fix1', 'source_sha256': 's' * 64}
        if command == 'register-review':
            task['review_report_sha256'] = util.sha256_file(Path(payload['report_path']))
            task['review_summary'] = dict(payload)
            if payload.get('review_key') and task['extra'].get('automation'):
                task['extra']['review_history'] = [{'review_key': payload['review_key']}]
            return {'ok': True}
        if command == 'advance-task':
            task['status'] = 'done'
            return {'ok': True}
        if command == 'report':
            request.update(status='succeeded' if payload['outcome'] == 'succeeded' else 'failed', result=payload)
            return {'ok': True}
        raise AssertionError(command)
    monkeypatch.setattr(legacy, 'core_call', core_call)
    return task, request, calls


@pytest.mark.parametrize('automated', [False, True])
@pytest.mark.parametrize('conclusion', ['pass', 'changes_requested', 'blocked'])
def test_all_settlement_paths_and_repeat(scene, automated, conclusion):
    task, request, calls = scene
    if automated:
        task['extra']['automation'] = {'fix_round': 0, 'manual_takeover': False}
    result = {'conclusion': conclusion, 'report_markdown': '# Review\n证据与结论',
              'fix': {'title': '修复', 'description': '补足证据', 'priority': 'high'} if conclusion == 'changes_requested' else None}
    kwargs = dict(db_path='isolated', task=task, request=request, run_id='review1', result=result,
                  backend='codex', cost_usd=None)
    settled = review_settle.settle(**kwargs)
    assert settled['exit_code'] == 0
    writes = [(cmd, p) for cmd, p in calls if cmd not in ('get-task', 'get-request')]
    assert writes[-1][0] == 'report'
    commands = [cmd for cmd, _ in writes]
    if conclusion == 'pass':
        assert ('advance-task' in commands) is not automated
    if conclusion == 'changes_requested':
        assert ('create-review-fix' in commands) is not automated
    payload = next(p for cmd, p in writes if cmd == 'register-review')
    assert payload['request_id'] == 'req1'
    assert payload['run_id'] == 'review1'
    assert payload['expected_execution_run_id'] == 'exec1'
    if automated:
        assert payload['review_key']
        if conclusion == 'changes_requested':
            assert payload['fix_mode'] == 'same_task' and payload['fix'] == result['fix']
            assert 'fix_task_id' not in payload
    assert settled['job']['report_sha256'] == util.sha256_file(Path(payload['report_path']))
    before = len(writes)
    # A persisted job records report_intent before sending. Re-running that job is read-only.
    log = legacy.pi_desktop_controller.EventLog(Path(payload['report_path']).parent / 'events.jsonl')
    assert review_settle.settle_job(driver=legacy, db_path='isolated', job=settled['job'],
                                   job_path=Path(payload['report_path']).parent / 'job.json',
                                   log=log, record_run=lambda **kwargs: None) == 0
    assert len([c for c, _ in calls if c not in ('get-task', 'get-request')]) == before


def test_manual_takeover_retains_legacy_pass(scene):
    task, request, calls = scene
    task['extra']['automation'] = {'fix_round': 1, 'manual_takeover': True}
    review_settle.settle(db_path='isolated', task=task, request=request, run_id='review1',
                         result={'conclusion': 'pass', 'report_markdown': '通过', 'fix': None},
                         backend='claude', cost_usd=0.2)
    assert any(c == 'advance-task' for c, _ in calls)


@pytest.mark.parametrize('location', ['fix_request', 'review_history', 'review_summary'])
def test_registered_key_recovery(location):
    job = {'review_key': 'same', 'report_sha256': 'sha', 'conclusion': 'changes_requested'}
    task = {'extra': {'automation': {'fix_round': 1}}, 'review_report_sha256': None}
    if location == 'fix_request':
        task['extra'][location] = {'review_key': 'same'}
    elif location == 'review_history':
        task[location] = [{'review_key': 'same'}]
    else:
        task['review_report_sha256'] = 'sha'
        task[location] = {'review_key': 'same', 'conclusion': 'changes_requested'}
    assert review_settle._registered_matches(task, job)
    assert not review_settle._registered_matches(task, dict(job, review_key='other'))


def test_schema_and_legacy_validation():
    schema = review_settle.RESULT_SCHEMA
    assert schema['required'] == ['conclusion', 'report_markdown', 'fix']
    assert schema['additionalProperties'] is False
    assert schema['properties']['fix']['type'] == ['object', 'null']
    good = {'conclusion': 'pass', 'report_markdown': '证据', 'fix': None}
    assert review_settle.validate_result(good) == good
    assert legacy.validate_result(good) == (good, None)
    invalid = [dict(good, extra=True), {k: v for k, v in good.items() if k != 'fix'},
               dict(good, conclusion='changes_requested'), dict(good, fix={}), dict(good, report_markdown='')]
    for value in invalid:
        with pytest.raises(util.KanpilotError):
            review_settle.validate_result(value)
        assert legacy.validate_result(value)[0] is None


def test_identity_lease_failure_never_registers(scene):
    task, request, calls = scene
    request['lease_expires_at'] = '2000-01-01T00:00:00Z'
    value = review_settle.settle(db_path='isolated', task=task, request=request, run_id='review1',
                                result={'conclusion': 'pass', 'report_markdown': '证据', 'fix': None},
                                backend='claude', cost_usd=0)
    assert value['exit_code'] == 1
    assert [cmd for cmd, _ in calls] == ['get-task', 'get-request']


@pytest.mark.parametrize('conclusion', ['pass', 'changes_requested', 'blocked'])
def test_real_core_automatic_settlement_and_recovery(factory, tmp_env, conclusion):
    from types import SimpleNamespace
    from kanpilot import automation_policy, cli, core, db
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            db.set_setting(conn, 'review_config', {'backend': 'codex'})
    finally:
        conn.close()
    review_scene = factory.make_in_review()
    task_id = review_scene.task.task_id
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            task = db.require_task(conn, task_id)
            db.set_extra(conn, task, util.now_iso(), automation={'fix_round': 0, 'manual_takeover': False},
                         delivery={**core.delivery_of(task), 'mode': 'pr'})
        ctx = SimpleNamespace(conn=conn, now=util.now_dt(), now_iso=util.now_iso())
        automation_policy.cmd_set_project_automation(ctx, {'project_id': task['project_id'],
                    'automation': {'enabled': True, 'max_fix_rounds': 2}, 'expected_policy_revision': 0})
    finally:
        conn.close()
    claim = cli.run('claim', {'stage': 'review', 'executor': 'kaneo-review'}, tmp_env.db)
    assert claim['claimed']
    task = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    request = cli.run('get-request', {'request_id': claim['request_id']}, tmp_env.db)['request']
    kwargs = dict(db_path=tmp_env.db, task=task, request=request, run_id=claim['run_id'], backend='codex', cost_usd=None,
                  result={'conclusion': conclusion, 'report_markdown': '实际核心结算证据',
                          'fix': {'title': '修复缺口', 'description': '回归覆盖', 'priority': 'high'} if conclusion == 'changes_requested' else None})
    assert review_settle.settle(**kwargs)['exit_code'] == 0
    updated = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    assert updated['status'] == {'pass': 'in_review', 'changes_requested': 'needs_plan', 'blocked': 'needs_user'}[conclusion]
    assert len(updated['review_history']) == 1
    if conclusion == 'changes_requested':
        assert updated['extra']['fix_request']['state'] == 'open'
        assert updated['automation']['fix_round'] == 1
        assert updated['review_report_sha256'] is None
    assert review_settle.settle(**kwargs)['exit_code'] == 0
    assert len(cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']['review_history']) == 1


def test_real_dismiss_takeover_restores_manual_pass(factory, tmp_env):
    from kanpilot import cli, core, db
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            db.set_setting(conn, 'review_config', {'backend': 'codex'})
    finally:
        conn.close()
    initial = factory.make_in_review()
    task_id = initial.task.task_id
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            task = db.require_task(conn, task_id)
            db.set_extra(conn, task, util.now_iso(), automation={'fix_round': 0},
                         delivery={**core.delivery_of(task), 'mode': 'pr'})
            core.block_task(conn, db.require_task(conn, task_id), util.now_iso(), '人工复核', 'review_blocked', 'test', stage='review')
    finally:
        conn.close()
    cli.run('resolve', {'task_id': task_id, 'action': 'dismiss'}, tmp_env.db)
    taken = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    assert taken['automation']['manual_takeover']
    assert taken['delivery']['mode'] == 'manual'
    assert taken['status'] == 'in_review'
    assert taken['stage'] == 'review'
    assert taken['auto_advance_paused'] == 1
    # Isolated fixture for the next execution's Review cycle; no process/model.
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            conn.execute("UPDATE requests SET status='cancelled' WHERE task_id=? AND status IN ('pending','claimed')", (task_id,))
            db.update_task(conn, task_id, util.now_iso(), status='in_review')
            db.create_request(conn, util.now_iso(), 'review', 'takeover-review:' + task_id, task_id=task_id, requested_by='test')
    finally:
        conn.close()
    # resume_auto is the reverse entry of ``resolve {action:"dismiss"}``: it
    # hands the task back to automation (clears manual_takeover) and, when the
    # PR identity is complete, flips delivery.mode back to "pr". That is the
    # point of the action — without it resume_auto would be a no-op for a
    # taken-over task (the one-way door D4 diagnoses), because a paused +
    # taken-over task can neither claim nor advance on its own.
    cli.run('restore-task', {'task_id': task_id, 'resume_auto': True}, tmp_env.db)
    resumed = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    assert resumed['automation']['manual_takeover'] is False, resumed['automation']
    assert resumed['delivery']['mode'] == 'pr', resumed['delivery']
    assert resumed['auto_advance_paused'] in (0, False), resumed
    claim = cli.run('claim', {'stage': 'review', 'executor': 'kaneo-review'}, tmp_env.db)
    assert claim['claimed']
    task = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    request = cli.run('get-request', {'request_id': claim['request_id']}, tmp_env.db)['request']
    assert review_settle.settle(db_path=tmp_env.db, task=task, request=request, run_id=claim['run_id'],
                                result={'conclusion': 'pass', 'report_markdown': '交回自动化后的评审通过', 'fix': None},
                                backend='codex', cost_usd=None)['exit_code'] == 0
    # Back under automation with PR delivery, a pass does not finish the task
    # directly: it enters the PR delivery cycle (commit → push → PR → merge →
    # done), which is the take_over → resume_auto → deliver loop of scenario 16.
    after = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    assert after['status'] == 'in_review', after
    assert after['delivery']['mode'] == 'pr', after['delivery']


def test_real_dismiss_takeover_manual_pass_still_finishes(factory, tmp_env):
    """A task that stays taken over still finishes directly on a review pass.

    Covers the ``conclusion == "pass" and not automated`` branch of
    ``review_settle.settle``: while ``manual_takeover`` is set the task is not
    automation-managed, so a pass advances straight to done instead of entering
    the PR delivery cycle. Set up directly (takeover + pause cleared) because a
    taken-over task cannot claim a review on its own — this is the path a user
    drives by hand.
    """
    from kanpilot import cli, core, db, util
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            db.set_setting(conn, 'review_config', {'backend': 'codex'})
    finally:
        conn.close()
    initial = factory.make_in_review()
    task_id = initial.task.task_id
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            task = db.require_task(conn, task_id)
            db.set_extra(conn, task, util.now_iso(), automation={'fix_round': 0},
                         delivery={**core.delivery_of(task), 'mode': 'manual'})
            core.block_task(conn, db.require_task(conn, task_id), util.now_iso(), '人工复核', 'review_blocked', 'test', stage='review')
    finally:
        conn.close()
    cli.run('resolve', {'task_id': task_id, 'action': 'dismiss'}, tmp_env.db)
    # Still taken over (no resume_auto), but the user cleared the pause by hand
    # so a review cycle can be claimed.
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            conn.execute("UPDATE requests SET status='cancelled' WHERE task_id=? AND status IN ('pending','claimed')", (task_id,))
            db.update_task(conn, task_id, util.now_iso(), status='in_review', auto_advance_paused=0)
            db.create_request(conn, util.now_iso(), 'review', 'manual-takeover-review:' + task_id, task_id=task_id, requested_by='test')
    finally:
        conn.close()
    taken = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    assert taken['automation']['manual_takeover']
    assert taken['delivery']['mode'] == 'manual'
    claim = cli.run('claim', {'stage': 'review', 'executor': 'kaneo-review'}, tmp_env.db)
    assert claim['claimed']
    task = cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']
    request = cli.run('get-request', {'request_id': claim['request_id']}, tmp_env.db)['request']
    assert review_settle.settle(db_path=tmp_env.db, task=task, request=request, run_id=claim['run_id'],
                                result={'conclusion': 'pass', 'report_markdown': '人工接管期间的评审通过', 'fix': None},
                                backend='codex', cost_usd=None)['exit_code'] == 0
    assert cli.run('get-task', {'task_id': task_id}, tmp_env.db)['task']['status'] == 'done'
