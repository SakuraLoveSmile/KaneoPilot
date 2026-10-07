"""Isolated execution-noop retry transitions and frozen model selection."""
from helpers import KanpilotTestCase
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kanpilot import (cli, cmd_config, cmd_dispatch, cmd_exec,
                      cmd_queue, core, db, derive, dispatch_validate, exec_config,
                      round_store, util)

NOW = '2026-10-07T04:00:00+00:00'
PRIMARY = {'model_source': 'custom', 'provider_id': 'primary-provider', 'model_id': 'primary-model',
           'thinking_level': 'high', 'permission_mode': 'auto', 'connection_file': '/tmp/isolated-plus.json'}
FALLBACK = {'provider_id': 'fallback-provider', 'model_id': 'fallback-model', 'thinking_level': 'medium'}


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / 'state.db')
    plan = tmp_path / 'plan.md'
    plan.write_text('confirmed plan')
    conn.execute("INSERT INTO tasks(task_id,project_id,project_name,status,stage,run_id,plan_path,plan_sha256,created_at,updated_at) "
                 "VALUES('task1','project1','P','running','monitor','execution1',?,?,?,?)",
                 (str(plan), util.sha256_file(plan), NOW, NOW))
    task = db.require_task(conn, 'task1')
    core.update_automation(conn, task, NOW, adopted_at=NOW, manual_takeover=False,
                           segment_started_at='2026-10-07T03:00:00+00:00', cost_usd=0, fix_round=1)
    db.set_extra(conn, db.require_task(conn, 'task1'), NOW,
                 verification_override={'execution_run_id': 'execution1'})
    db.update_task(conn, 'task1', NOW, sent=1, send_state='sent', sent_at=NOW, lock_retained=1)
    db.set_setting(conn, 'project_mappings', {'items': {'project1': {'automation': {'enabled': True}}}})
    db.set_setting(conn, 'exec_config', {'backend': 'pi_desktop', 'pi_desktop': dict(PRIMARY, fallback=FALLBACK)})
    yield SimpleNamespace(conn=conn, now=util.parse_ts(NOW), now_iso=NOW)
    conn.close()


def report_noop(ctx):
    request, _ = db.create_request(ctx.conn, NOW, 'monitor', 'monitor:task1', task_id='task1')
    db.update_request(ctx.conn, request['request_id'], NOW, status='claimed', run_id='monitor1', claimed_by='kaneo-monitor')
    return cmd_exec.cmd_report(ctx, {'request_id': request['request_id'], 'run_id': 'monitor1',
                                     'outcome': 'needs_user', 'failure_code': 'execution_noop',
                                     'failure_reason': '执行空转'})


def block(ctx):
    core.block_task(ctx.conn, db.require_task(ctx.conn, 'task1'), NOW, '执行空转', 'execution_noop', 'kaneo-monitor')


def test_first_noop_retries_once_without_consuming_review_round(ctx):
    before = db.require_task(ctx.conn, 'task1')['plan_sha256']
    assert report_noop(ctx)['task_status'] == 'ready'
    task = db.require_task(ctx.conn, 'task1')
    a = core.automation_of(task)
    assert a['exec_retries'] == 1 and a['fix_round'] == 1
    assert a['exec_retry'] == {'n': 1, 'reason': 'execution_noop', 'from_run_id': 'execution1',
                              'model': 'fallback', 'at': NOW, 'actor': 'automation'}
    assert a['segment_started_at'] == '2026-10-07T03:00:00+00:00'
    assert task['plan_sha256'] == before
    assert task['sent'] == 0 and task['send_state'] is None and task['sent_at'] is None and not task['lock_retained']
    assert 'verification_override' not in db.task_extra(task)
    assert ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='dispatch' AND status='queued'").fetchone()[0] == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind='exec_retry_started'").fetchone()[0] == 1
    db.update_task(ctx.conn, 'task1', NOW, status='running', stage='monitor', run_id='execution2', sent=1, send_state='sent')
    assert report_noop(ctx)['task_status'] == 'needs_user'
    assert db.require_task(ctx.conn, 'task1')['failure_code'] == 'execution_noop'
    assert core.automation_of(db.require_task(ctx.conn, 'task1'))['exec_retries'] == 1


@pytest.mark.parametrize('reason', ['disabled', 'budget', 'manual_takeover', 'unmanaged', 'paused', 'unconfirmed'])
def test_ineligible_automatic_retry_stays_blocked(ctx, reason):
    task = db.require_task(ctx.conn, 'task1')
    if reason == 'disabled':
        db.set_setting(ctx.conn, 'project_mappings', {'items': {'project1': {'automation': {'enabled': False}}}})
    elif reason == 'budget':
        core.update_automation(ctx.conn, task, NOW, segment_started_at='2026-10-06T00:00:00+00:00')
    elif reason == 'manual_takeover':
        core.update_automation(ctx.conn, task, NOW, manual_takeover=True)
    elif reason == 'unmanaged':
        db.set_extra(ctx.conn, task, NOW, automation=None)
    elif reason == 'paused':
        db.update_task(ctx.conn, 'task1', NOW, auto_advance_paused=1)
    else:
        db.set_extra(ctx.conn, task, NOW, plan_execution_hold=True)
    assert report_noop(ctx)['task_status'] == 'needs_user'
    task = db.require_task(ctx.conn, 'task1')
    assert task['failure_stage'] == 'monitor'
    assert ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 0


def test_missing_fallback_uses_primary(ctx):
    db.set_setting(ctx.conn, 'exec_config', {'backend': 'pi_desktop', 'pi_desktop': PRIMARY})
    report_noop(ctx)
    assert core.automation_of(db.require_task(ctx.conn, 'task1'))['exec_retry']['model'] == 'primary'


@pytest.mark.parametrize('model', ['primary', 'fallback', None])
def test_manual_resume_chooses_model_and_can_exceed_one_attempt(ctx, model):
    block(ctx)
    core.update_automation(ctx.conn, db.require_task(ctx.conn, 'task1'), NOW, exec_retries=4)
    payload = {'task_id': 'task1', 'action': 'resume'}
    if model is not None:
        payload['exec_model'] = model
    result = cmd_queue.cmd_resolve(ctx, payload)
    assert result['ok'] and result['task_status'] == 'ready' and result['request']['kind'] == 'dispatch'
    a = core.automation_of(db.require_task(ctx.conn, 'task1'))
    assert a['exec_retries'] == 5
    assert a['exec_retry']['model'] == (model or 'fallback') and a['exec_retry']['actor'] == 'user'
    assert a['segment_started_at'] == NOW


def test_manual_fallback_missing_is_not_silently_replaced(ctx):
    block(ctx)
    db.set_setting(ctx.conn, 'exec_config', {'backend': 'pi_desktop', 'pi_desktop': PRIMARY})
    with pytest.raises(util.KanpilotError) as error:
        cmd_queue.cmd_resolve(ctx, {'task_id': 'task1', 'action': 'resume', 'exec_model': 'fallback'})
    assert error.value.code == 'conflict'
    assert db.require_task(ctx.conn, 'task1')['status'] == 'needs_user'


@pytest.mark.parametrize('payload', [{'action': 'resume', 'exec_model': 'other'},
                                    {'action': 'replan', 'exec_model': 'primary'}])
def test_invalid_model_selection_rejected(ctx, payload):
    block(ctx)
    with pytest.raises(util.KanpilotError) as error:
        cmd_queue.cmd_resolve(ctx, dict(payload, task_id='task1'))
    assert error.value.code == 'bad_request'


def test_actions_are_specific_to_noop(ctx):
    block(ctx)
    actions = derive.allowed_actions(ctx.conn, db.require_task(ctx.conn, 'task1'), ctx.now)
    assert {'resume', 'replan', 'mark_done', 'dismiss'} <= set(actions)
    assert 'accept_verification' not in actions and 'retry' not in actions


def test_fix_round_and_replan_clear_execution_retry(ctx, monkeypatch):
    report_noop(ctx)
    core.start_fix_round_inline(ctx.conn, db.require_task(ctx.conn, 'task1'), NOW, fix_request={'review_key': 'review1'})
    a = core.automation_of(db.require_task(ctx.conn, 'task1'))
    assert a['exec_retries'] == 0 and a['exec_retry'] is None and a['fix_round'] == 2
    block(ctx)
    core.update_automation(ctx.conn, db.require_task(ctx.conn, 'task1'), NOW, exec_retries=1, exec_retry={'model': 'fallback'})
    monkeypatch.setattr(cmd_queue, '_accept_remote_for_resolution', lambda c, p: None)
    cmd_queue.cmd_resolve(ctx, {'task_id': 'task1', 'action': 'replan'})
    a = core.automation_of(db.require_task(ctx.conn, 'task1'))
    assert a['exec_retries'] == 0 and a['exec_retry'] is None


def test_report_retry_is_one_transaction(ctx, monkeypatch):
    original = db.create_request
    def fail_dispatch(*args, **kwargs):
        if args[2] == 'dispatch':
            raise RuntimeError('isolated creation failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(db, 'create_request', fail_dispatch)
    with pytest.raises(RuntimeError):
        report_noop(ctx)
    task = db.require_task(ctx.conn, 'task1')
    assert task['status'] == 'running' and 'exec_retries' not in core.automation_of(task)
    assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind='exec_retry_started'").fetchone()[0] == 0


@pytest.mark.parametrize('fallback', [None, {}, FALLBACK])
def test_config_fallback_roundtrip_and_clear(ctx, fallback):
    result = cmd_config.cmd_set_exec_config(ctx, {'pi_desktop': {'fallback': fallback}})
    assert result['config']['pi_desktop'].get('fallback') == (fallback or None)
    assert exec_config.load(ctx.conn)['pi_desktop'].get('fallback') == (fallback or None)
    assert cmd_config.cmd_get_exec_config(ctx, {})['config']['pi_desktop'].get('fallback') == (fallback or None)


@pytest.mark.parametrize('fallback', ['bad', {'provider_id': 'a'}, dict(FALLBACK, thinking_level='bad')])
def test_invalid_fallback_save_preserves_config(ctx, fallback):
    original = exec_config.load(ctx.conn)
    with pytest.raises(util.KanpilotError) as error:
        cmd_config.cmd_set_exec_config(ctx, {'pi_desktop': {'fallback': fallback}})
    assert error.value.code == 'bad_request' and exec_config.load(ctx.conn) == original


def test_retry_snapshot_overrides_only_model_and_drops_extra_fields(ctx):
    block(ctx)
    core.start_exec_retry_inline(ctx.conn, db.require_task(ctx.conn, 'task1'), NOW)
    config = exec_config.retry_config(ctx.conn, db.require_task(ctx.conn, 'task1'))
    snapshot = exec_config.snapshot(ctx.conn, config_override=config)
    assert snapshot == {'backend': 'pi_desktop', 'connection_file': PRIMARY['connection_file'],
                        'permission_mode': 'auto', **FALLBACK}
    assert exec_config.load(ctx.conn)['pi_desktop']['model_id'] == 'primary-model'
    db.set_setting(ctx.conn, 'exec_config', {'backend': 'pi_desktop', 'pi_desktop': PRIMARY})
    with pytest.raises(util.KanpilotError) as error:
        exec_config.retry_config(ctx.conn, db.require_task(ctx.conn, 'task1'))
    assert error.value.code == 'validation_failed'


@pytest.mark.parametrize('status,conflict', [('in-progress', False), ('done', True)])
def test_begin_send_retry_reads_and_verifies_without_remote_write(ctx, monkeypatch, status, conflict):
    block(ctx)
    core.start_exec_retry_inline(ctx.conn, db.require_task(ctx.conn, 'task1'), NOW)
    task = db.require_task(ctx.conn, 'task1')
    assert 'in-progress' in core.expected_remote_slugs(task)
    request = ctx.conn.execute("SELECT * FROM requests WHERE kind='dispatch'").fetchone()
    db.update_request(ctx.conn, request['request_id'], NOW, status='claimed', run_id='execution2', claimed_by='kaneo-dispatch')
    db.update_task(ctx.conn, 'task1', NOW, status='prepared', run_id='execution2', original_todo_slug='in-progress')
    client = object()
    monkeypatch.setattr(cmd_dispatch.remote, 'get_client', lambda: client)
    reads = Mock(side_effect=[{'task': {'projectId': 'project1', 'status': 'in-progress'}},
                             {'task': {'projectId': 'project1', 'status': status}}])
    writes = Mock(side_effect=AssertionError('retry must not write remote status'))
    monkeypatch.setattr(cmd_dispatch.remote, 'get_task', reads)
    monkeypatch.setattr(cmd_dispatch.remote, 'update_status', writes)
    if conflict:
        with pytest.raises(util.KanpilotError) as error:
            cmd_dispatch.cmd_dispatch_begin_send(ctx, {'task_id': 'task1', 'run_id': 'execution2'})
        assert error.value.code == 'remote_conflict'
        assert db.require_task(ctx.conn, 'task1')['failure_code'] == 'remote_conflict'
    else:
        assert cmd_dispatch.cmd_dispatch_begin_send(ctx, {'task_id': 'task1', 'run_id': 'execution2'})['status'] == 'submitting'
    assert reads.call_count == 2 and writes.call_count == 0


def test_unmarked_task_does_not_accept_in_progress(ctx):
    assert 'in-progress' not in core.expected_remote_slugs(db.require_task(ctx.conn, 'task1'))


class RetryDispatchClaim(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = self.make_ready_task(task_id="retry-task1")
        self.enqueue_dispatch("retry-task1")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.fake.tasks["retry-task1"]["status"] = "in-progress"
        conn, now = self.ctx.conn, self.ctx.now_iso
        task = db.require_task(conn, "retry-task1")
        core.update_automation(conn, task, now, adopted_at=now, manual_takeover=False,
                               segment_started_at=now, cost_usd=0)
        db.set_setting(conn, "exec_config", {"backend": "pi_desktop", "pi_desktop": dict(PRIMARY, fallback=FALLBACK)})
        db.update_task(conn, "retry-task1", now, status="running", run_id="previous-execution", sent=1, send_state="sent")
        core.block_task(conn, db.require_task(conn, "retry-task1"), now, "idle", "execution_noop", "kaneo-monitor")
        core.start_exec_retry_inline(conn, db.require_task(conn, "retry-task1"), now)
        self.client = Mock()
        self.client.initialize.return_value = {"serverInfo": {"name": "kaneopilot"}}

    def test_claim_freezes_fallback_and_records_actual_model(self):
        with patch.object(dispatch_validate.mcp_client.MCPClient, "from_server_file", return_value=self.client), \
             patch.object(exec_config, "check_available", return_value=(True, None)) as probe:
            result = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(result["claimed"])
        snapshot = result["context"]["exec_config"]
        self.assertEqual(snapshot["model_id"], "fallback-model")
        self.assertEqual(result["context"]["original_todo_slug"], "in-progress")
        self.assertEqual(probe.call_args.kwargs["config_snapshot"], snapshot)
        round_row = round_store.get_round(self.ctx.conn, "retry-task1", result["run_id"])
        self.assertEqual(util.jload(round_row["config_json"]), snapshot)
        event = self.ctx.conn.execute("SELECT * FROM events WHERE kind='dispatch_configured'").fetchone()
        self.assertEqual(util.jload(event["detail_json"])["exec_config"], snapshot)
        self.assertEqual(util.jload(event["detail_json"])["exec_model"], "fallback")
        db.set_setting(self.ctx.conn, "exec_config", {"backend": "pi_desktop", "pi_desktop": PRIMARY})
        frozen = round_store.get_round(self.ctx.conn, "retry-task1", result["run_id"])
        self.assertEqual(util.jload(frozen["config_json"]), snapshot)

    def test_unavailable_fallback_blocks_without_switching(self):
        with patch.object(dispatch_validate.mcp_client.MCPClient, "from_server_file", return_value=self.client), \
             patch.object(exec_config, "check_available", return_value=(False, "fallback model unavailable")) as probe:
            result = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertFalse(result["claimed"])
        self.assertEqual(result["code"], "validation_failed")
        self.assertEqual(db.require_task(self.ctx.conn, "retry-task1")["status"], "needs_user")
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.kwargs["config_snapshot"]["model_id"], "fallback-model")

    def test_retry_remote_status_mismatch_stays_blocked(self):
        self.fake.tasks["retry-task1"]["status"] = "done"
        result = self.call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertFalse(result["claimed"])
        self.assertEqual(db.require_task(self.ctx.conn, "retry-task1")["status"], "needs_user")


def test_fallback_never_persists_credentials_or_extra_fields(ctx):
    cmd_config.cmd_set_exec_config(ctx, {'pi_desktop': {'fallback': dict(FALLBACK, token='isolated-secret', connection_file='/other-file')}})
    stored = db.get_setting(ctx.conn, 'exec_config')
    assert stored['pi_desktop']['fallback'] == FALLBACK
    assert 'isolated-secret' not in util.jdump(stored)
    event = ctx.conn.execute("SELECT detail_json FROM events WHERE kind='exec_config_updated' ORDER BY id DESC LIMIT 1").fetchone()
    assert 'isolated-secret' not in event['detail_json']


class RetryMonitorInterval(KanpilotTestCase):
    def test_legacy_900_first_monitor_uses_120_without_changing_setting(self):
        fixture = self.make_ready_task(task_id='interval-task1')
        self.enqueue_dispatch('interval-task1')
        context = cli.Context(self.db_path)
        self.addCleanup(context.close)
        db.set_setting(context.conn, 'monitor_interval_seconds', 900)
        claim = self.call('claim', {'stage': 'dispatch', 'executor': 'kaneo-dispatch'})
        evidence = self.valid_evidence(self.task('interval-task1'), fixture['plan_path'])
        payload = {'task_id': 'interval-task1', 'run_id': claim['run_id'], 'evidence': evidence}
        self.call('dispatch-prepare', payload)
        self.call('dispatch-begin-send', {'task_id': 'interval-task1', 'run_id': claim['run_id']})
        evidence['message_visible'] = True
        self.call('dispatch-confirm', payload)
        task = db.require_task(context.conn, 'interval-task1')
        request = context.conn.execute("SELECT * FROM requests WHERE task_id='interval-task1' AND kind='monitor'").fetchone()
        self.assertEqual((util.parse_ts(request['not_before']) - util.parse_ts(task['sent_at'])).total_seconds(), 120)
        self.assertEqual(db.get_setting(context.conn, 'monitor_interval_seconds'), 900)


def test_noop_event_keeps_only_anonymous_activity(ctx):
    request, _ = db.create_request(ctx.conn, NOW, 'monitor', 'monitor:task1', task_id='task1')
    db.update_request(ctx.conn, request['request_id'], NOW, status='claimed', run_id='monitor1', claimed_by='kaneo-monitor')
    cmd_exec.cmd_report(ctx, {'request_id': request['request_id'], 'run_id': 'monitor1', 'outcome': 'needs_user',
                             'failure_code': 'execution_noop', 'evidence': {
                                 'signals': ['short_reply_read_only', 'secret-path', {}],
                                 'evidence_window': 'after_goal_approved', 'window_basis': 'sequence',
                                 'reply_length': 1, 'tool_event_count': 8, 'action_tool_event_count': 0,
                                 'effective_change_count': -1, 'reply': 'isolated-secret', 'path': '/secret/file'}})
    event = ctx.conn.execute("SELECT detail_json FROM events WHERE kind='execution_noop'").fetchone()
    assert util.jload(event['detail_json']) == {'signals': ['short_reply_read_only'],
                                             'evidence_window': 'after_goal_approved', 'window_basis': 'sequence',
                                             'reply_length': 1, 'tool_event_count': 8, 'action_tool_event_count': 0}


class ReplanNoopDispatch(KanpilotTestCase):
    def setUp(self):
        from pathlib import Path
        from test_git_delivery import make_repo, branch
        super().setUp()
        self.repo = make_repo(self.root / 'gitfixture').rename(self.repos_root / 'Feedback')
        self.fixture = self.make_ready_task(task_id='replannooptask')
        self.enqueue_dispatch('replannooptask')
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.delivery = branch(self.repo, self.root / 'worktrees', task_id='replannooptask')
        self.worktree = Path(self.delivery['worktree_path'])
        from kanpilot import git_delivery
        (self.worktree / 'confirmed-dirty.txt').write_text('user-confirmed baseline')
        previous_fingerprint = git_delivery.worktree_state_sha256(self.delivery, effective_only=True)
        now, conn = self.ctx.now_iso, self.ctx.conn
        task = db.require_task(conn, 'replannooptask')
        core.update_delivery(conn, task, now, **self.delivery, mode='pr', commits=[])
        task = db.require_task(conn, 'replannooptask')
        core.update_automation(conn, task, now, adopted_at=now, manual_takeover=False,
                               replan_requested_at=now, replan_remote_status='in-progress',
                               replan_worktree_sha256=previous_fingerprint)
        self.fake.tasks['replannooptask']['status'] = 'in-progress'
        core.block_task(conn, db.require_task(conn, 'replannooptask'), now, 'idle', 'execution_noop', 'kaneo-monitor')
        core.start_exec_retry_inline(conn, db.require_task(conn, 'replannooptask'), now)
        goal = self.worktree / '.pi' / 'goal' / 'contract.md'
        goal.parent.mkdir(parents=True)
        goal.write_text('goal-only metadata')

    def test_retry_keeps_confirmed_dirty_baseline_and_ignores_new_goal(self):
        task = db.require_task(self.ctx.conn, 'replannooptask')
        self.assertTrue(core.is_user_replan(task))
        dispatch_validate.validate_dispatch(self.ctx.conn, task, self.ctx.now)
        self.assertEqual(cmd_dispatch._ensure_worktree(task, task['task_id'], 'retry1', self.ctx.conn),
                         (self.worktree, 'branch'))
        result = self.call('claim', {'stage': 'dispatch', 'executor': 'kaneo-dispatch'})
        self.assertTrue(result['claimed'])
        self.assertEqual((self.worktree / 'confirmed-dirty.txt').read_text(), 'user-confirmed baseline')

    def test_retry_still_rejects_substantive_uncommitted_changes(self):
        (self.worktree / 'actual-change.txt').write_text('preserve and require a decision')
        task = db.require_task(self.ctx.conn, 'replannooptask')
        with self.assertRaises(util.KanpilotError):
            dispatch_validate.validate_dispatch(self.ctx.conn, task, self.ctx.now)
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(task, task['task_id'], 'retry1', self.ctx.conn)
        self.assertTrue((self.worktree / 'actual-change.txt').is_file())

    def test_retry_rejects_changed_contents_in_confirmed_dirty_baseline(self):
        (self.worktree / 'confirmed-dirty.txt').write_text('unconfirmed changed content')
        task = db.require_task(self.ctx.conn, 'replannooptask')
        with self.assertRaises(util.KanpilotError):
            dispatch_validate.validate_dispatch(self.ctx.conn, task, self.ctx.now)
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(task, task['task_id'], 'retry1', self.ctx.conn)

    def test_unchanged_legacy_raw_fingerprint_with_goal_is_accepted(self):
        from kanpilot import git_delivery
        task = db.require_task(self.ctx.conn, 'replannooptask')
        old_raw = git_delivery.worktree_state_sha256(self.delivery)
        core.update_automation(self.ctx.conn, task, self.ctx.now_iso, replan_worktree_sha256=old_raw)
        task = db.require_task(self.ctx.conn, 'replannooptask')
        dispatch_validate.validate_dispatch(self.ctx.conn, task, self.ctx.now)
        cmd_dispatch._ensure_worktree(task, task['task_id'], 'retry1', self.ctx.conn)

    def test_changed_legacy_raw_goal_cannot_be_silently_refrozen(self):
        from kanpilot import git_delivery
        task = db.require_task(self.ctx.conn, 'replannooptask')
        old_raw = git_delivery.worktree_state_sha256(self.delivery)
        core.update_automation(self.ctx.conn, task, self.ctx.now_iso, replan_worktree_sha256=old_raw)
        (self.worktree / '.pi' / 'goal' / 'contract.md').write_text('goal changed since the legacy freeze')
        task = db.require_task(self.ctx.conn, 'replannooptask')
        with self.assertRaises(util.KanpilotError):
            dispatch_validate.validate_dispatch(self.ctx.conn, task, self.ctx.now)
        with self.assertRaises(util.KanpilotError):
            cmd_dispatch._ensure_worktree(task, task['task_id'], 'retry1', self.ctx.conn)
        self.assertEqual(core.automation_of(task)['replan_worktree_sha256'], old_raw)
