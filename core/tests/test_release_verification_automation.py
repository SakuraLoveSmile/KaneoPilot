"""Automatic release verification uses isolated Git/SQLite and fake Plus only."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import test_release_verification as fixtures
import test_release_verification_history as history_fixtures
import test_release_verification_runtime as runtime_fixtures
from kanpilot import cmd_read, core, db, release_verification as verification, runtime_operations, util


class AutomationTest(TestCase):
    def setUp(self):
        self.case = fixtures.ReleaseVerificationTest('test_cli_fixed_payload_round_trip')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx, self.plus = self.case.conn, self.case.ctx, self.case.plus
        self.first = self.case.start()
        self.vid = self.first['verification_id']

    def recover(self):
        return cmd_read.cmd_recover_execution_goals(self.ctx, {})

    def pending_in_history(self):
        original = self.plus.plans_pending
        def plans(sid):
            result = original(sid)
            if not self.plus.approved:
                result['history'] = [deepcopy(self.plus.proposal)]
            return result
        self.case.patch(patch.object(self.plus, 'plans_pending', side_effect=plans))

    def test_pending_goal_in_history_is_approved_once_and_automatically_verified(self):
        self.pending_in_history()
        result = self.recover()
        self.assertEqual(result['goal_recoveries'], [])
        self.assertEqual(result['release_verifications'][0]['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))
        self.assertEqual(self.recover()['release_verifications'], [])

    def test_report_delivery_delay_waits_without_new_attempt_or_approval(self):
        for state in ('pending', 'draft', 'not_found'):
            with self.subTest(state=state):
                with patch.object(self.plus, 'goal_report_get', return_value=dict(state=state)):
                    result = self.recover()['release_verifications'][0]
                self.assertIn(result['verification']['state'], ('running', 'unknown'))
                self.assertNotEqual(result['verification'].get('failure_code'), 'release_verification_report')
        self.assertEqual(self.recover()['release_verifications'][0]['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_second_pending_read_must_preserve_goal_body_and_artifact(self):
        self.plus.proposal.update(markdown='frozen', plan='frozen', artifact=dict(sha256='a'*64))
        original = self.plus.plans_pending
        reads = []
        def plans(sid):
            result = original(sid); reads.append(True)
            if len(reads) >= 3:
                result['plans'][0]['markdown'] = 'drifted'
            return result
        with patch.object(self.plus, 'plans_pending', side_effect=plans):
            result = self.case.sync(self.vid)
        self.assertEqual(self.plus.approvals, 0)
        self.assertEqual(result['verification']['state'], 'blocked')

    def test_nested_manual_sync_cannot_overwrite_or_repeat_background_approval(self):
        original = self.plus.plans_pending
        entered = []
        def plans(sid):
            result = original(sid)
            if not entered:
                entered.append(True)
                self.assertEqual(self.case.sync(self.vid)['verification']['state'], 'verified')
            return result
        with patch.object(self.plus, 'plans_pending', side_effect=plans):
            result = self.case.sync(self.vid)
        self.assertEqual(result['verification']['state'], 'verified')
        self.assertEqual(self.plus.approvals, 1)

    def test_lost_approval_response_is_only_observed_on_next_background_run(self):
        self.plus.lose = 'approval'
        original = self.plus.plans_resolve
        def reserved_then_lost(*args, **kwargs):
            value = verification._load(self.conn, 'parent', self.vid)
            runtime_operations.reserve_downstream(self.conn, *verification._approval_operation(value),
                'pi_desktop', 'kp_runtime_proposal_resolve', {})
            return original(*args, **kwargs)
        self.plus.plans_resolve = reserved_then_lost
        self.assertEqual(self.recover()['release_verifications'][0]['verification']['state'], 'unknown')
        self.plus.lose = None
        self.assertEqual(self.recover()['release_verifications'][0]['verification']['state'], 'verified')
        self.assertEqual(self.plus.approvals, 1)

    def test_background_selection_honors_pause_manual_permission_and_current_ref(self):
        value = verification._load(self.conn, 'parent', self.vid)
        for gate in ('monitor', 'dispatch', 'delivery', 'task', 'manual', 'permission', 'ref'):
            with self.subTest(gate=gate):
                db.set_setting(self.conn, 'paused_stages', [gate] if gate in ('monitor','dispatch','delivery') else [])
                db.update_task(self.conn, 'parent', self.ctx.now_iso, auto_advance_paused=1 if gate=='task' else 0)
                core.update_automation(self.conn, self.case.task(), self.ctx.now_iso, manual_takeover=gate=='manual')
                attempt = deepcopy(value)
                if gate == 'permission': attempt['context']['runtime_config']['permission_mode'] = 'prompt'
                verification._save(self.conn, attempt, self.ctx.now_iso)
                db.set_extra(self.conn, self.case.task(), self.ctx.now_iso, release_verification_ref=dict(
                    verification_id=self.vid, context_sha256='f'*64 if gate=='ref' else value['context']['context_sha256']))
                self.assertEqual(self.recover()['release_verifications'], [])
                self.assertEqual(self.plus.approvals, 0)

    def test_ineligible_unknown_prefix_does_not_starve_current_auto_attempt(self):
        task = dict(self.case.task())
        original = verification._load(self.conn, 'parent', self.vid)
        columns = list(task)
        for number in range(105):
            old_id = 'older-' + str(number)
            row = dict(task, task_id=old_id)
            attempt = deepcopy(original)
            attempt['context']['identity']['task_id'] = old_id
            attempt['context']['runtime_config']['permission_mode'] = 'prompt'
            if number == 103:
                attempt['context']['runtime_config']['permission_mode'] = 'auto'
                row['review_key'] = 'new-review-identity'
            if number == 104:
                attempt['context']['runtime_config']['permission_mode'] = 'auto'
                attempt['context']['commands'] = ['changed without new context hash']
            extra = util.jload(row['extra_json'])
            extra['release_verification_ref'] = dict(verification_id=attempt['verification_id'],
                context_sha256=attempt['context']['context_sha256'])
            row['extra_json'] = util.jdump(extra)
            self.conn.execute('INSERT INTO tasks (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                tuple(row[key] for key in columns))
            runtime_operations.begin_operation(self.conn, verification._scope(old_id), verification._op(self.vid),
                'core', verification.TOOL, attempt['context'])
            verification._save(self.conn, attempt, self.ctx.now_iso)
            self.conn.execute('UPDATE runtime_operations SET updated_at=? WHERE scope_id=?', ('2000-01-01T00:00:00Z',verification._scope(old_id)))
        result = verification.recover_current(self.ctx, limit=1)
        self.assertEqual([item['task_id'] for item in result], ['parent'])
        self.assertEqual(result[0]['verification']['state'], 'verified')

    def test_background_pause_during_final_remote_read_never_sends_approval(self):
        for stage in ('monitor', 'dispatch'):
            with self.subTest(stage=stage):
                db.set_setting(self.conn, 'paused_stages', [])
                original = self.plus.turn_get
                def pause(*args, **kwargs):
                    result = original(*args, **kwargs)
                    db.set_setting(self.conn, 'paused_stages', [stage])
                    return result
                with patch.object(self.plus, 'turn_get', side_effect=pause):
                    self.recover()
                self.assertEqual(self.plus.approvals, 0)
                self.assertFalse(verification._load(self.conn, 'parent', self.vid).get('approval_sent'))


class RealRuntimeAutomationTest(TestCase):
    def test_pause_at_atomic_runtime_reservation_prevents_background_approval(self):
        case = runtime_fixtures.ReleaseVerificationRuntimeTest('test_real_dispatch_accepts_core_receipt_and_forwards_frozen_commands')
        case.setUp()
        self.addCleanup(case.doCleanups)
        value = case.ready()
        value['automatic_recovery'] = True
        verification._save(case.conn, value, case.ctx.now_iso)
        reserve = runtime_operations.reserve_downstream
        for stage in ('monitor', 'dispatch'):
            with self.subTest(stage=stage):
                db.set_setting(case.conn, 'paused_stages', [])
                def pause_before_reservation(conn, *args, **kwargs):
                    db.set_setting(conn, 'paused_stages', [stage])
                    return reserve(conn, *args, **kwargs)
                with patch.object(runtime_operations, 'reserve_downstream', side_effect=pause_before_reservation):
                    with self.assertRaises(util.KanpilotError):
                        case.dispatch()
                case.assert_not_sent()
                self.assertNotIn('pi_plans_resolve', [name for name, _ in case.host_calls])

    def test_background_pending_history_uses_real_runtime_guard_and_unique_receipt(self):
        case = runtime_fixtures.ReleaseVerificationRuntimeTest('test_real_dispatch_accepts_core_receipt_and_forwards_frozen_commands')
        case.setUp()
        self.addCleanup(case.doCleanups)
        original = case.plus.plans_pending
        def plans(sid):
            result = original(sid)
            if not case.plus.approved:
                result['history'] = [deepcopy(case.plus.proposal)]
            return result
        with patch.object(case.plus, 'plans_pending', side_effect=plans):
            result = cmd_read.cmd_recover_execution_goals(case.ctx, {})
        self.assertEqual(result['release_verifications'][0]['verification']['state'], 'verified')
        row = db.get_runtime_operation(case.conn, case.scope.scope_id, 'proposal_proposal_accept')
        self.assertEqual(row['status'], 'accepted')
        self.assertEqual(case.plus.approvals, 1)
        self.assertEqual(case.plus.required, case.plus.commands)
        self.assertEqual(cmd_read.cmd_recover_execution_goals(case.ctx, {})['release_verifications'], [])


class HistoricalAutomationTest(TestCase):
    def test_legacy_history_cas_conflict_never_converts_pending_to_approval(self):
        case = history_fixtures.HistoryTest('test_planning_can_observe_manual_approval_after_history_version_growth')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.manual_approval(blocked=False)
        value = verification._load(case.conn, 'parent', case.vid)
        value.update(state='blocked', failure_code='release_verification_history', history_read_only=True,
            reason='历史观察写回前原尝试或下游写状态已变化')
        verification._save(case.conn, value, case.ctx.now_iso)
        case.plus.approved = False
        case.plus.current = 'planning'
        verification.recover_current(case.ctx)
        case.case.sync(case.vid)
        self.assertEqual((case.plus.created, case.plus.prompts, case.plus.approvals), (1, 1, 0))

    def test_legacy_read_only_lineage_survives_timeout_before_pending_reappears(self):
        case = history_fixtures.HistoryTest('test_planning_can_observe_manual_approval_after_history_version_growth')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.manual_approval(blocked=False)
        value = verification._load(case.conn, 'parent', case.vid)
        value.update(state='blocked', failure_code='release_verification_history', history_read_only=True,
            reason='历史观察写回前原尝试或下游写状态已变化')
        verification._save(case.conn, value, case.ctx.now_iso)
        with patch.object(verification, 'HistoricalReadClient', side_effect=util.KanpilotError('timeout', 'temporary read')):
            self.assertEqual(verification.recover_current(case.ctx)[0]['verification']['state'], 'unknown')
        case.plus.approved = False
        case.plus.current = 'planning'
        verification.recover_current(case.ctx)
        case.case.sync(case.vid)
        self.assertEqual((case.plus.created, case.plus.prompts, case.plus.approvals), (1, 1, 0))

    def test_history_local_snapshot_race_does_not_persist_terminal_failure(self):
        case = history_fixtures.HistoryTest('test_planning_can_observe_manual_approval_after_history_version_growth')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.manual_approval(blocked=False)
        observe = verification._observed_report
        def changed_snapshot(*args, **kwargs):
            result = observe(*args, **kwargs)
            db.set_setting(case.conn, 'paused_stages', ['review'])
            return result
        with patch.object(verification, '_observed_report', side_effect=changed_snapshot):
            self.assertEqual(case.case.sync(case.vid)['verification']['state'], 'unknown')
        value = verification._load(case.conn, 'parent', case.vid)
        self.assertEqual(value['failure_code'], 'release_verification_history_conflict')
        self.assertTrue(value['history_read_only'])
        self.assertTrue(value['history_approval_forbidden'])
        self.assertNotIn('approval_observation', value)
        self.assertNotIn('report_payload', value)
        case.plus.approved = False
        case.plus.current = 'planning'
        case.case.sync(case.vid)
        self.assertEqual(case.plus.approvals, 0)
        case.plus.approved = True
        case.plus.current = 'execution'
        self.assertEqual(case.case.sync(case.vid)['verification']['failure_code'], 'release_verification_report')
        self.assertEqual(case.plus.approvals, 0)

    def test_exact_legacy_history_cas_conflict_resumes_read_only_but_other_blocks_stay_terminal(self):
        case = history_fixtures.HistoryTest('test_planning_can_observe_manual_approval_after_history_version_growth')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.manual_approval(blocked=False)
        value = verification._load(case.conn, 'parent', case.vid)
        marker = '历史观察写回前原尝试或下游写状态已变化'
        for reason in ('真实来源漂移', marker):
            value.update(state='blocked', failure_code='release_verification_history',
                reason=reason, history_read_only=True)
            verification._save(case.conn, value, case.ctx.now_iso)
            result = verification.recover_current(case.ctx)
            if reason != marker:
                self.assertEqual(result, [])
            else:
                self.assertEqual(result[0]['verification']['state'], 'blocked')
                self.assertEqual(result[0]['verification']['failure_code'], 'release_verification_report')
        self.assertEqual((case.plus.created, case.plus.prompts, case.plus.approvals), (1, 1, 0))

    def test_external_approval_is_observed_until_report_without_new_side_effect(self):
        case = history_fixtures.HistoryTest('test_planning_can_observe_manual_approval_after_history_version_growth')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.manual_approval(blocked=False)
        case.plus.running = True
        case.plus.history_changes['executionState'] = 'running'
        first = verification.recover_current(case.ctx)
        self.assertEqual(first[0]['verification']['state'], 'running')
        observed = verification._load(case.conn, 'parent', case.vid)
        self.assertEqual(observed['approval_observation']['source'], 'plus_history')
        case.plus.running = False
        case.plus.history_changes['executionState'] = 'completed'
        case.plus.history_version += 1
        second = verification.recover_current(case.ctx)
        self.assertEqual(second[0]['verification']['state'], 'blocked')
        self.assertEqual(second[0]['verification']['failure_code'], 'release_verification_report')
        self.assertEqual(verification.recover_current(case.ctx), [])
        self.assertFalse(verification._load(case.conn, 'parent', case.vid).get('approval_sent'))
        self.assertIsNone(db.get_runtime_operation(case.conn, *verification._approval_operation(observed)))
        self.assertEqual((case.plus.created, case.plus.prompts, case.plus.approvals), (1, 1, 0))
