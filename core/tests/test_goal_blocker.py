"""Goal decisions against isolated SQLite/Git and a fake host; no model calls."""
import copy
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_engine_verdict as fixtures
from helpers import goal_report_read
from kanpilot import (auto_monitor, cli, cmd_delivery, cmd_exec, cmd_read, cmd_remote, core, cmd_request_continuation, db, goal_blocker as gb,
                      harness_plus_adapter as adapter, mcp_scopes, mcp_tools, runtime_client, runtime_operations, util)
from kanpilot.engine import commands as engine_commands


class GoalBlockerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TestEngineVerdictCommand('test_plus_success_never_runs_pilot_verification_in_live_or_shadow')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.tid, self.sha = self.f._running_task_with_repo('goal-blocked', commands=['true'])
        self.ctx = cli.Context(self.f.db_path)
        self.addCleanup(self.ctx.close)
        self.run = 'run-' + self.tid
        self.f.job['goal_approval'] = {'status': 'approved', 'proposal_id': 'proposal',
            'execution_turn_id': 'execution', 'planning_turn_id': 'planning'}
        self.f.write_evidence()
        db.update_task(self.ctx.conn, self.tid, self.ctx.now_iso, status='needs_user', stage='monitor',
            failure_code='verification_unconfirmed', failure_stage='monitor')
        self.ctx.conn.execute("UPDATE execution_rounds SET run_status='finished' WHERE task_id=?", (self.tid,))
        self.hold = {'executionId': 'execution', 'sessionId': 'session', 'state': 'blocked',
            'version': 1, 'firstTurnId': 'execution', 'resumeCount': 0,
            'segments': [{'turnId': 'execution', 'startedAt': 1, 'endedAt': 2}],
            'reasons': [{'code': 'verdict_partial'}],
            'draft': {'limitations': ['空歌词输入缺失'], 'nextSteps': ['补充可播放曲目'],
                'criteria': [{'id': 'c1', 'text': '已完成项', 'verdict': 'met'},
                             {'id': 'c2', 'text': '空歌词', 'verdict': 'partial', 'explanation': '没有输入'}]},
            'progress': {'items': [{'id': 'p1', 'label': '设备验证', 'status': 'blocked'}]}}
        self.supported, self.lose_response, self.source_on_resume = True, False, False
        self.writes = []
        owner = self
        class Client:
            def tools_list(self):
                return {'pi_goal_execution_get', 'pi_goal_resume', 'pi_goal_close'} if owner.supported else set()
            def goal_report_list(self, sid):
                return {'reports': [{'executionId': 'execution', 'proposalId': 'proposal',
                    'turnId': 'execution', 'sessionId': 'session', 'title': '歌词验收',
                    'hold': copy.deepcopy(owner.hold)}]}
            def goal_execution_get(self, sid, execution_id=None):
                return {'execution': {'id': 'execution', 'proposalId': 'proposal',
                    'sessionId': 'session', 'title': '歌词验收'}, 'hold': copy.deepcopy(owner.hold)}
            def agent_status(self, sid):
                return {'status': {'currentTurnId': owner.f.plus_state['current_turn_id'],
                    'isRunning': owner.f.plus_state['is_running']}}
            def goal_action(self, sid, **kwargs):
                owner.assertFalse(owner.ctx.conn.in_transaction)
                owner.writes.append(kwargs)
                owner.hold['version'] += 1
                if kwargs['action'] == 'accept':
                    owner.hold['state'] = 'accepted'
                    result = {}
                else:
                    owner.hold['state'] = 'resumed'
                    owner.hold['resumeCount'] += 1
                    owner.hold['segments'].append({'turnId': 'new-turn', 'startedAt': 3})
                    owner.f.plus_state.update(current_turn_id='new-turn', is_running=True)
                    if owner.source_on_resume:
                        (owner.f.worktree / 'source.py').write_text('legitimate continued work\n')
                    result = {'turnId': 'new-turn'}
                if owner.lose_response:
                    raise util.KanpilotError('remote_unavailable', 'response lost')
                return result
        self.client = Client()
        self.patch(gb, '_client', return_value=self.client)
        self.patch(cmd_read, 'read_goal_report', side_effect=self.report)
        self.patch(cmd_request_continuation.cmd_dispatch, 'cmd_round_observe', return_value={'status': 'observing'})

    def patch(self, obj, name, **kwargs):
        item = patch.object(obj, name, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def task(self):
        return db.require_task(self.ctx.conn, self.tid)

    def payload(self, action='accept', **fields):
        return dict(task_id=self.tid, run_id=self.run, execution_id='execution', expected_version=1,
                    action=action, confirm=True, **fields)

    def report(self, *args, **kwargs):
        report = goal_report_read([], turn_id='execution', structured=False, verdict='partial')
        report['report'].update(closure='user_accepted' if self.hold['state'] == 'accepted' else None,
            criteria=self.hold['draft']['criteria'], limitations=self.hold['draft']['limitations'])
        report['report_sha256'] = util.sha256_text(util.jdump(report['report']))
        return report

    def test_round_without_approved_goal_contract_is_not_applicable(self):
        """失败轮没有已批准契约时按普通失败处理，不冒充目标受阻、不接触 Pi。"""
        self.f.job.pop('goal_approval')
        self.f.write_evidence()
        with patch.object(gb, '_client', side_effect=AssertionError('contract gate must not contact Pi')):
            view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['state'], 'none')
        self.assertEqual(view['actions'], [])
        self.assertIn('不适用目标受阻处理', view['detail'])

    def test_malformed_goal_contract_is_not_applicable(self):
        self.f.job['goal_approval'].pop('proposal_id')
        self.f.write_evidence()
        view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['state'], 'none')
        self.assertIn('留痕不完整', view['detail'])

    def test_non_pi_desktop_round_has_no_goal_blocker(self):
        self.ctx.conn.execute("UPDATE execution_rounds SET backend='codex' WHERE task_id=?", (self.tid,))
        with patch.object(gb, '_client', side_effect=AssertionError('non Pi round must not contact Pi')):
            view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['state'], 'none')
        self.assertIn('不是 Pi Desktop', view['detail'])

    def test_unreadable_run_record_stays_unavailable(self):
        (self.f.directory / 'job.json').unlink()
        view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['state'], 'unavailable')
        self.assertIn('job.json', view['detail'])

    def test_old_host_displays_bound_hold_and_only_non_met_criteria(self):
        self.supported = False
        view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['state'], 'blocked')
        self.assertEqual(view['actions'], [])
        self.assertEqual(view['limitations'], ['空歌词输入缺失', '空歌词：没有输入'])
        self.assertEqual(view['progress'][0]['label'], '设备验证')
        self.assertIn('升级', view['detail'])
        self.assertEqual(self.writes, [])

    def test_accept_preserves_unverified_facts_queues_monitor_and_is_idempotent(self):
        result = gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        self.assertEqual(result['status'], 'accepted')
        saved = db.task_extra(self.task())[gb.KEY]
        self.assertIs(saved['verification_ok'], False)
        self.assertTrue(saved['review_authorized'])
        self.assertEqual(saved['accepted_report']['criteria'][1]['verdict'], 'partial')
        self.assertEqual(self.task()['status'], 'running')
        self.assertEqual(self.ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE task_id=? AND kind='monitor' AND status='queued'", (self.tid,)).fetchone()[0], 1)
        again = gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        self.assertTrue(again['deduplicated'])
        self.assertEqual(len(self.writes), 1)
        verdict = engine_commands.cmd_engine_verdict(self.ctx, dict(task_id=self.tid,
            expected_execution_run_id=self.run, expected_plan_sha256=self.sha))
        self.assertFalse(verdict['verification_ok'])
        self.assertTrue(verdict['review_authorized'])
        self.assertTrue(gb.review_admission_current(self.ctx.conn, self.task(), verdict))

    def test_acceptance_enters_real_commit_and_review_gate_without_a_verification_pass(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.f.worktree), *args], check=True,
                                  capture_output=True).stdout.decode().strip()
        git('switch', '-c', 'kaneo/goal-blocked')
        head = git('rev-parse', 'HEAD')
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, delivery=dict(mode='manual',
            workspace_version=1, branch='kaneo/goal-blocked', worktree_path=str(self.f.worktree),
            base_branch='main', base_sha=head, head_sha=head, commits=[]))
        gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        queued = self.ctx.conn.execute("SELECT request_id FROM requests WHERE task_id=? AND kind='monitor' AND status='queued'", (self.tid,)).fetchone()
        claim = cmd_exec.cmd_claim(self.ctx, dict(stage='monitor', executor='kaneo-monitor', request_id=queued['request_id']))
        self.assertTrue(claim['claimed'])
        identity = dict(task_id=self.tid, request_id=claim['request_id'], run_id=claim['run_id'],
                        expected_execution_run_id=self.run)
        receipt = engine_commands.cmd_engine_verdict(self.ctx, dict(identity, expected_plan_sha256=self.sha))
        self.assertFalse(receipt['verification_ok'])
        result = cmd_delivery.cmd_delivery_commit(self.ctx, identity)
        self.assertEqual(result['outcome'], 'committed', result)
        again = engine_commands.cmd_engine_verdict(self.ctx, dict(identity, expected_plan_sha256=self.sha))
        self.assertFalse(again['verification_ok'])
        self.assertTrue(again['review_authorized'])
        self.f.fake.tasks[self.tid]['status'] = 'in-progress'
        cmd_remote.cmd_advance_task(self.ctx, dict(task_id=self.tid, run_id=claim['run_id'],
                                   target='in-review', expected_current='in-progress'))
        self.assertEqual(self.task()['status'], 'in_review')
        # An execution acceptance never supplies the independent Review pass.
        with self.assertRaises(util.KanpilotError):
            gb.accepted_delivery_evidence(self.ctx.conn, self.task(), {})

    def test_stale_version_and_cross_run_refuse_without_side_effect(self):
        for fields in ({'expected_version': 2}, {'run_id': 'other'}, {'execution_id': 'other'}):
            payload = self.payload(); payload.update(fields)
            with self.assertRaises(util.KanpilotError):
                gb.cmd_resolve_goal_blocker(self.ctx, payload)
        self.assertEqual(self.writes, [])

    def test_source_report_and_new_turn_invalidate_acceptance(self):
        gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        self.assertTrue(gb.acceptance_current(self.ctx.conn, self.task()))
        self.f.plus_state.update(current_turn_id='unrelated', is_running=True)
        self.assertFalse(gb.acceptance_current(self.ctx.conn, self.task()))
        self.f.plus_state.update(current_turn_id='execution', is_running=False)
        (self.f.worktree / 'unreviewed.py').write_text('changed')
        self.assertFalse(gb.acceptance_current(self.ctx.conn, self.task()))
        (self.f.worktree / 'unreviewed.py').unlink()
        self.hold['draft']['limitations'].append('changed report')
        self.assertFalse(gb.acceptance_current(self.ctx.conn, self.task()))

    def test_user_can_accept_failed_goal_without_rewriting_its_terminal(self):
        self.f.job.update(status='failed', exit_code=1)
        self.f.events[-1].update(outcome='failed', reason='agent error')
        self.f.plus_turn['status'] = 'error'
        self.f.write_evidence()
        before = (self.f.directory / 'job.json').read_bytes()
        gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        receipt = engine_commands.cmd_engine_verdict(self.ctx, dict(task_id=self.tid,
            expected_execution_run_id=self.run, expected_plan_sha256=self.sha))
        self.assertFalse(receipt['verification_ok'])
        self.assertTrue(receipt['review_authorized'])
        self.assertEqual((self.f.directory / 'job.json').read_bytes(), before)
        task = dict(self.task(), extra=db.task_extra(self.task()))
        controller = SimpleNamespace(report=Mock())
        detail = dict(backend='pi_desktop', run_status='failed', exit_code=1,
                      terminal={'outcome': 'failed', 'reason': 'agent error'})
        with patch.object(auto_monitor, '_get_task', return_value=task), \
             patch.object(auto_monitor, '_recheck', return_value={'kaneo_status': 'in-progress'}), \
             patch.object(auto_monitor, '_get_session', return_value={'round_detail': detail}), \
             patch.object(auto_monitor, '_observe', return_value=None), \
             patch.object(auto_monitor, '_complete') as complete:
            auto_monitor._monitor(controller)
        complete.assert_called_once()
        controller.report.assert_not_called()

    def test_continue_adopts_started_turn_even_when_it_has_already_changed_source(self):
        self.source_on_resume = True
        result = gb.cmd_resolve_goal_blocker(self.ctx, self.payload('continue', message='补充曲目引用'))
        self.assertEqual(result['status'], 'continued')
        self.assertEqual(result['turn_id'], 'new-turn')
        self.assertEqual(self.writes[0]['message'], '补充曲目引用')
        self.assertEqual(self.task()['status'], 'running')
        self.assertEqual(util.jload((self.f.directory / 'job.json').read_text())['turn_id'], 'new-turn')

    def test_lost_accept_response_reconciles_without_repeating_close(self):
        self.lose_response = True
        with self.assertRaises(util.KanpilotError) as caught:
            gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        self.assertEqual(caught.exception.code, 'goal_decision_unconfirmed')
        self.assertNotIn(gb.KEY, db.task_extra(self.task()))
        view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['state'], 'accepted')
        self.assertEqual(view['actions'], ['reconcile'])
        result = gb.cmd_reconcile_goal_blocker(self.ctx, self.payload())
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(gb.acceptance_current(self.ctx.conn, self.task()))

    def test_host_acknowledgement_before_adoption_still_recovers_local_settlement(self):
        with patch.object(cmd_request_continuation, 'adopt_continuation', side_effect=OSError('before local adoption')):
            with self.assertRaises(util.KanpilotError):
                gb.cmd_resolve_goal_blocker(self.ctx, self.payload('continue'))
        pending = db.task_extra(self.task())[gb.PENDING_KEY]
        scope = runtime_client.stable_scope_id('execution', task_id=self.tid, run_id=self.run)
        operation = runtime_operations.get_operation(self.ctx.conn, scope, pending['operation_id'])
        self.assertEqual(operation['status'], 'accepted')
        self.assertEqual(self.task()['status'], 'needs_user')
        # Host completion may publish a new-turn summary before local adoption.
        original_listing = self.client.goal_report_list
        def completed_listing(sid):
            result = original_listing(sid)
            result['reports'][0]['turnId'] = 'new-turn'
            return result
        self.f.plus_state['is_running'] = False
        self.hold['segments'][-1]['endedAt'] = 4
        # Repeating the same user operation reconciles; it does not send again
        # or mistake the acknowledged host result for a finished local update.
        with patch.object(self.client, 'goal_report_list', side_effect=completed_listing):
            result = gb.cmd_resolve_goal_blocker(self.ctx, self.payload('continue'))
        self.assertEqual((result['status'], result['turn_id']), ('continued', 'new-turn'))
        self.assertEqual(self.task()['status'], 'running')
        self.assertNotIn(gb.PENDING_KEY, db.task_extra(self.task()))
        self.assertEqual(len(self.writes), 1)

    def test_continued_observation_keeps_original_goal_hold_identity(self):
        job_path = self.f.directory / 'job.json'
        job = util.jload(job_path.read_text())
        job['goal_approval']['observation_turn_id'] = 'new-turn'
        job['turn_adopted'] = [{'from': 'execution', 'to': 'new-turn', 'code': 'manual_continue'}]
        job['turn_id'] = 'new-turn'
        job_path.write_text(util.jdump(job))
        identity = cmd_read.goal_round_identity(self.ctx.conn, self.tid, self.run)
        self.assertIn('execution', identity['approved_turns'])
        execution, hold, supported = gb._candidate(identity, self.client)
        self.assertEqual(hold['firstTurnId'], 'execution')
        self.assertTrue(supported)

    def test_lost_resume_response_reconciles_changed_source_using_host_segment(self):
        self.lose_response, self.source_on_resume = True, True
        with self.assertRaises(util.KanpilotError):
            gb.cmd_resolve_goal_blocker(self.ctx, self.payload('continue'))
        result = gb.cmd_reconcile_goal_blocker(self.ctx, self.payload())
        self.assertEqual((result['status'], result['turn_id']), ('continued', 'new-turn'))
        self.assertEqual(len(self.writes), 1)

    def test_crash_between_job_adoption_and_db_settlement_recovers_without_resend(self):
        original = cmd_request_continuation.os.replace
        def replace_then_crash(source, target):
            original(source, target)
            raise OSError('injected interruption after job rename')
        with patch.object(cmd_request_continuation.os, 'replace', side_effect=replace_then_crash):
            with self.assertRaises(util.KanpilotError):
                gb.cmd_resolve_goal_blocker(self.ctx, self.payload('continue'))
        self.assertEqual(self.task()['status'], 'needs_user')
        self.assertEqual(util.jload((self.f.directory / 'job.json').read_text())['turn_id'], 'new-turn')
        result = gb.cmd_reconcile_goal_blocker(self.ctx, self.payload())
        self.assertEqual(result['status'], 'continued')
        self.assertEqual(self.task()['status'], 'running')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(util.jload((self.f.directory / 'job.json').read_text())['turn_adopted']), 1)

    def test_unknown_accept_cannot_authorize_source_changed_after_send(self):
        self.lose_response = True
        with self.assertRaises(util.KanpilotError):
            gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        (self.f.worktree / 'unexpected.py').write_text('new source')
        with self.assertRaises(util.KanpilotError):
            gb.cmd_reconcile_goal_blocker(self.ctx, self.payload())
        self.assertNotIn(gb.KEY, db.task_extra(self.task()))
        self.assertEqual(self.task()['status'], 'needs_user')
        self.assertEqual(len(self.writes), 1)

    def test_external_accept_without_pilot_receipt_does_not_claim_review_admission(self):
        self.hold.update(state='accepted', version=2)
        view = gb.cmd_get_goal_blocker(self.ctx, self.payload())
        self.assertEqual(view['actions'], [])
        self.assertIn('未据此进入评审', view['detail'])
        self.assertFalse(gb.acceptance_current(self.ctx.conn, self.task()))

    def test_runtime_dispatch_enforces_scope_reservation_and_maps_the_real_adapter(self):
        manager = mcp_scopes.ScopeManager(app_support_dir=self.f.root / 'isolated-mcp')
        scope_id = runtime_client.stable_scope_id('execution', task_id=self.tid, run_id=self.run)
        manager.register_scope(dict(scope_id=scope_id, invocation_id='goal-control-test', role='execution',
            expires_at=util.plus_seconds(3600, self.ctx.now), task_id=self.tid, run_id=self.run,
            project_path=str(self.f.worktree), connection_file=str(self.f.root / 'fake-plus.json'),
            runtime_config={}), server_url='http://127.0.0.1:1/mcp')
        scope = manager.get_scope(scope_id)
        runtime_operations.begin_operation(self.ctx.conn, scope_id, 'created-session', 'pi_desktop',
                                           'kp_runtime_session_create', {})
        runtime_operations.complete_operation(self.ctx.conn, scope_id, 'created-session',
                                             session_id='session', result={})
        host_action = self.client.goal_action
        calls = []
        owner = self
        class FakeMCP:
            def tools_list(self):
                return {'pi_goal_execution_get', 'pi_goal_resume', 'pi_goal_close'}
            def call_tool(self, name, args):
                calls.append((name, args))
                if name == 'pi_goal_execution_get':
                    return owner.client.goal_execution_get(args['sessionId'], execution_id=args.get('executionId'))
                return host_action(args['sessionId'], action='accept', execution_id=args['executionId'],
                    expected_version=args['expectedVersion'])
        def action(sid, **args):
            return mcp_tools.dispatch_tool_call(self.f.db_path, manager, 'runtime', scope,
                'kp_runtime_goal_action', dict(scope_id=scope_id, operation_id=args['operation_id'] + ':pi',
                    authorization_operation_id=args['operation_id'], session_id=sid,
                    execution_id=args['execution_id'], expected_version=args['expected_version'], action=args['action']))
        with patch.object(adapter, '_get_client', return_value=(FakeMCP(), {})):
            read = mcp_tools.dispatch_tool_call(self.f.db_path, manager, 'runtime', scope,
                'kp_runtime_goal_execution_get', dict(scope_id=scope_id, session_id='session', execution_id='execution'))
            self.assertEqual(read['hold']['state'], 'blocked')
            with self.assertRaises(util.KanpilotError):
                mcp_tools.dispatch_tool_call(self.f.db_path, manager, 'runtime', scope,
                    'kp_runtime_goal_execution_get', dict(scope_id=scope_id, session_id='foreign', execution_id='execution'))
            with self.assertRaises(util.KanpilotError):
                action('session', operation_id='unauthorized', execution_id='execution', expected_version=1, action='accept')
            with patch.object(self.client, 'goal_action', side_effect=action):
                result = gb.cmd_resolve_goal_blocker(self.ctx, self.payload())
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual([name for name, _ in calls], ['pi_goal_execution_get', 'pi_goal_close'])
        self.assertEqual(calls[-1][1], {'sessionId': 'session', 'executionId': 'execution',
            'expectedVersion': 1, 'confirm': True, 'action': 'accept'})
        self.assertEqual(len(self.writes), 1)

    def test_runtime_token_cannot_manufacture_user_acceptance(self):
        scope = SimpleNamespace(role='execution', task_id=self.tid, run_id=self.run,
            scope_id='execution_' + self.tid + '_' + self.run)
        args = dict(session_id='session', execution_id='execution', expected_version=1,
                    action='accept', authorization_operation_id='forged')
        with self.assertRaises(util.KanpilotError) as caught:
            gb.authorize_runtime_goal_action(self.ctx.conn, scope, args)
        self.assertEqual(caught.exception.code, 'forbidden')
        self.assertNotIn('kp_runtime_goal_action', mcp_scopes.RUNTIME_ROLE_TOOLS['review'])
        self.assertNotIn('kp_runtime_goal_action', mcp_scopes.RUNTIME_ROLE_TOOLS['planner'])


class GoalRuntimeContractTests(unittest.TestCase):
    def test_adapter_maps_only_declared_goal_tools_and_confirms_writes(self):
        class FakeMCP:
            def tools_list(self):
                return {'pi_goal_execution_get', 'pi_goal_resume', 'pi_goal_close'}
            def call_tool(self, name, payload):
                calls.append((name, payload))
                return {'ok': True}
        calls = []
        with patch.object(adapter, '_get_client', return_value=(FakeMCP(), {})):
            adapter.plus_goal_execution_get('connection', session_id='s', execution_id='e')
            adapter.plus_goal_action('connection', session_id='s', execution_id='e', expected_version=2,
                                    action='continue', message='material')
            adapter.plus_goal_action('connection', session_id='s', execution_id='e', expected_version=2, action='accept')
        self.assertEqual(calls, [('pi_goal_execution_get', {'sessionId': 's', 'executionId': 'e'}),
            ('pi_goal_resume', {'sessionId': 's', 'executionId': 'e', 'expectedVersion': 2, 'confirm': True, 'message': 'material'}),
            ('pi_goal_close', {'sessionId': 's', 'executionId': 'e', 'expectedVersion': 2, 'confirm': True, 'action': 'accept'})])

    def test_tool_schema_requires_execution_version_and_user_reservation(self):
        schema = mcp_tools.TOOLS_REGISTRY['kp_runtime_goal_action']['inputSchema']
        self.assertTrue({'session_id', 'execution_id', 'expected_version', 'authorization_operation_id'} <= set(schema['required']))
        self.assertFalse(schema['additionalProperties'])
        self.assertTrue(mcp_tools.TOOLS_REGISTRY['kp_runtime_goal_execution_get']['readOnlyHint'])
