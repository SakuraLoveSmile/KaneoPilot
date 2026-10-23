"""Only isolated Git/SQLite and a write-denying historical Plus reader."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import test_release_verification as fixtures
from kanpilot import (core, db, delivery_contract, external_build_repair as repair,
                      release_delivery as release, release_verification as verification, runtime_operations, util)


def host_evidence(body):
    body['evidences'] = []
    body['checkObservations'] = []
    body['evidenceResolution'] = []
    for check in body['checks']:
        eid = 'e-' + check['id']
        check['evidenceRefs'] = [eid]
        body['evidences'].append(dict(id=eid, kind='tool_result', refId='call-' + check['id']))
        body['checkObservations'].append(dict(checkId=check['id'], command=check['command'],
            result='passed', exitCode=0, evidenceIds=[eid]))
        # Legal Plus Host result: Bash callId is observed, but resolution only
        # searches message IDs. This must not erase the native observation.
        body['evidenceResolution'].append(dict(evidenceId=eid, state='unresolved'))


class HistoryTest(TestCase):
    def setUp(self):
        self.case = fixtures.ReleaseVerificationTest('test_cli_fixed_payload_round_trip')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx, self.plus = self.case.conn, self.case.ctx, self.case.plus

    def runtime_reset_fixture(self):
        markdown = '# Verification\n' + '\n'.join(self.plus.commands)
        artifact = dict(relativePath='.pi/goal/contract.md', workspaceKind='project',
            sizeBytes=len(markdown.encode()), sha256=util.sha256_text(markdown))
        self.plus.proposal.update(markdown=markdown, plan=markdown, artifact=artifact,
            title='Verification', question='Run?')
        self.plus.history_changes['executionState'] = 'completed'
        # The fake Plus normally bypasses the runtime ledger. Keep its actual
        # approval response as an isolated receipt for the reconnect contract.
        original_resolve = self.plus.plans_resolve
        def resolve(pid, sid, turn, tool, action, **kwargs):
            ref = db.task_extra(self.case.task())['release_verification_ref']
            value = verification._load(self.conn, 'parent', ref['verification_id'])
            scope, operation = verification._approval_operation(value)
            arguments = dict(scope_id=scope, operation_id=operation, session_id=sid,
                turn_id=turn, proposal_id=pid, tool_call_id=tool, version=kwargs['version'],
                action='accept', target_permission_mode=kwargs['target_permission_mode'],
                required_verification_commands=kwargs['required_verification_commands'])
            runtime_operations.begin_operation(self.conn, scope, operation,
                'pi_desktop', 'kp_runtime_proposal_resolve', arguments)
            response = original_resolve(pid, sid, turn, tool, action, **kwargs)
            runtime_operations.complete_operation(self.conn, scope, operation,
                session_id=sid, proposal_id=pid, result=dict(resolved=True, action='accept',
                    session_id=sid, proposal_id=pid, detail=deepcopy(response)))
            return response
        self.case.patch(patch.object(self.plus, 'plans_resolve', side_effect=resolve))
        old, payload = self.case.partial_report()
        self.reset_messages = [
            dict(id='prompt', role='user', turnId='planning', content=self.plus.prompt),
            dict(id='tool-call', role='tool', turnId='planning', toolName='SubmitGoal',
                 toolCallId='tool-call', toolStatus='success', status='complete',
                 toolArgs=dict(title='Verification', question='Run?', markdown=markdown),
                 toolResult=dict(details=dict(proposal=deepcopy(old['proposal'])), terminate=True)),
            dict(id='final', role='assistant', turnId='execution', content='Done')]
        self.reset_complete = True
        self.reset_status = dict(sessionId=old['session_id'], isRunning=False)
        self.reset_turn = dict(sessionId=old['session_id'], turnId='execution',
            status='completed', endedAt=self.plus.report['report']['execution']['endedAt'])
        def session_get(sid, **kwargs):
            return dict(session=dict(id=sid, messages=deepcopy(self.reset_messages),
                messageStart=0, messageEnd=len(self.reset_messages), hasMoreBefore=False,
                hasMoreAfter=not self.reset_complete))
        self.case.patch(patch.object(self.plus, 'session_get', side_effect=session_get, create=True))
        self.case.patch(patch.object(self.plus, 'agent_status', side_effect=lambda sid: deepcopy(self.reset_status)))
        self.case.patch(patch.object(self.plus, 'turn_get', side_effect=lambda *a, **k: deepcopy(self.reset_turn)))
        return old, payload

    def test_restart_preview_after_plus_reset_is_read_only_and_preserves_old_failure(self):
        old, payload = self.runtime_reset_fixture()
        before = self.conn.total_changes
        prepared = verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(prepared['expected_report_sha256'], payload['expected_report_sha256'])
        self.reset_status['currentTurnId'] = None
        self.assertEqual(verification.cmd_prepare_restart_release_verification(self.ctx,
            dict(task_id='parent')), prepared)
        self.assertEqual(verification._load(self.conn, 'parent', old['verification_id']), old)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))
        with self.assertRaises(util.KanpilotError):
            verification._completed_report(self.conn, self.plus, old)
        restarted = self.case.restart(payload)
        self.assertNotEqual(restarted['verification_id'], old['verification_id'])
        self.assertEqual(self.case.restart(payload)['verification_id'], restarted['verification_id'])
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (2, 2, 1))
        self.assertEqual(verification._load(self.conn, 'parent', old['verification_id']), old)

    def test_restart_after_plus_reset_rejects_changed_or_incomplete_transcript(self):
        self.runtime_reset_fixture()
        original = deepcopy(self.reset_messages)
        changes = [
            lambda rows: rows.append(dict(role='user', turnId='execution', content='Continue')),
            lambda rows: rows.append(dict(role='assistant', turnId='other', content='Later')),
            lambda rows: rows[-1].pop('turnId'),
            lambda rows: rows[0].update(content='Changed prompt'),
            lambda rows: rows.append(dict(role='tool', turnId='execution', toolName='SubmitGoal')),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.reset_messages = deepcopy(original); change(self.reset_messages)
                with self.assertRaises(util.KanpilotError):
                    verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.reset_messages = original; self.reset_complete = False
        with self.assertRaises(util.KanpilotError):
            verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_restart_after_plus_reset_rejects_live_drift_missing_terminal_and_report_changes(self):
        old, payload = self.runtime_reset_fixture()
        status = deepcopy(self.reset_status); turn = deepcopy(self.reset_turn)
        for status_change in (dict(isRunning=True), dict(currentTurnId='other'),
                dict(currentTurnId=''), dict(currentTurnId=1), dict(sessionId='other'),
                dict(pendingToolConfirmations=1), dict(pendingToolConfirmations=True),
                dict(pendingToolConfirmations='0'), dict(pendingPermission=True)):
            with self.subTest(status=status_change):
                self.reset_status = dict(status, **status_change)
                with self.assertRaises(util.KanpilotError):
                    self.case.restart(payload)
        self.reset_status = status
        for turn_change in (dict(status='failed'), dict(endedAt=None), dict(endedAt='invalid'),
                dict(turnId='other'), dict(sessionId='other')):
            with self.subTest(turn=turn_change):
                self.reset_turn = dict(turn, **turn_change)
                with self.assertRaises(util.KanpilotError):
                    self.case.restart(payload)
        self.reset_turn = turn
        for changed in (dict(status, isRunning=True), dict(status, currentTurnId='other')):
            with self.subTest(changed_during_read=changed), patch.object(self.plus, 'agent_status',
                    side_effect=[deepcopy(status), changed]):
                with self.assertRaises(util.KanpilotError):
                    self.case.restart(payload)
        for field in ('report_sha256', 'report_body_sha256', 'report_payload', 'report_id', 'execution_id'):
            with self.subTest(missing=field):
                damaged = deepcopy(old); damaged.pop(field, None)
                verification._save(self.conn, damaged, self.ctx.now_iso)
                with self.assertRaises(util.KanpilotError):
                    self.case.restart(payload)
        verification._save(self.conn, old, self.ctx.now_iso)
        self.plus.bad = lambda report: report['report'].update(summary='Changed after completion')
        with self.assertRaises(util.KanpilotError):
            self.case.restart(payload)
        # The report body and SHA remain identical; the raw envelope must also
        # remain the exact response saved with the old failed attempt.
        self.plus.bad = lambda report: report.update(extraAuditField='changed')
        with self.assertRaises(util.KanpilotError):
            self.case.restart(payload)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_restart_after_plus_reset_requires_original_accepted_approval(self):
        old, payload = self.runtime_reset_fixture()
        scope, operation = verification._approval_operation(old)
        row = dict(db.get_runtime_operation(self.conn, scope, operation))
        result = util.jload(row['result_json'])
        result['detail']['execution']['requiredVerificationCommands'] = ['different command']
        changes = [dict(status='unknown'), dict(request_sha256='0'*64), dict(session_id='other'),
            dict(result_json=util.jdump(result)), None]
        for change in changes:
            with self.subTest(change=change):
                self.conn.execute('SAVEPOINT original_approval')
                try:
                    if change is None:
                        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?',
                            (scope, operation))
                    else:
                        key, value = next(iter(change.items()))
                        self.conn.execute('UPDATE runtime_operations SET ' + key + '=? WHERE scope_id=? AND operation_id=?',
                            (value, scope, operation))
                    with self.assertRaises(util.KanpilotError):
                        self.case.restart(payload)
                finally:
                    self.conn.execute('ROLLBACK TO original_approval')
                    self.conn.execute('RELEASE original_approval')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_restart_after_plus_reset_preserves_outer_safety_guards(self):
        old, payload = self.runtime_reset_fixture()
        for field, value in (('extra_pending', [dict(status='pending')]), ('pending_asks', [dict(id='ask')])):
            with self.subTest(field=field):
                setattr(self.plus, field, value)
                try:
                    with self.assertRaises(util.KanpilotError):
                        verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
                finally:
                    setattr(self.plus, field, [])
        scope = verification._approval_operation(old)[0]
        runtime_operations.begin_operation(self.conn, scope, 'unknown-write', 'pi_desktop', 'unknown_write', {})
        with self.assertRaises(util.KanpilotError):
            self.case.restart(payload)
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?', (scope, 'unknown-write'))
        path = Path(old['worktree_path']) / 'fix1.txt'
        original = path.read_bytes()
        try:
            path.write_text('source changed')
            with self.assertRaises(util.KanpilotError):
                self.case.restart(payload)
        finally:
            path.write_bytes(original)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def manual_approval(self, blocked=True):
        first = self.case.start()
        self.vid = first['verification_id']
        self.value = verification._load(self.conn, 'parent', self.vid)
        markdown = '# Verification\n' + '\n'.join(self.plus.commands)
        path = Path(self.value['worktree_path']) / '.pi/goal/contract.md'
        path.parent.mkdir(parents=True)
        path.write_text(markdown)
        artifact = dict(relativePath='.pi/goal/contract.md', workspaceKind='project',
            sizeBytes=len(markdown.encode()), sha256=util.sha256_text(markdown))
        self.plus.proposal.update(markdown=markdown, plan=markdown, artifact=artifact,
            title='Verification', question='Run?')
        self.original = deepcopy(self.plus.proposal)
        self.plus.approved = True
        self.plus.required = None
        self.plus.history_version = 4
        self.plus.history_changes = dict(executionKind='goal', executionState='completed')
        self.plus.current = 'execution'
        self.plus.report['report']['goal'].update(markdown=markdown,
            contractHash=artifact['sha256'], contractPath=artifact['relativePath'])
        host_evidence(self.plus.report['report'])
        self.messages = [dict(id='user', role='user', turnId='planning', content=self.plus.prompt),
            dict(id='tool-call', role='tool', toolName='SubmitGoal', toolCallId='tool-call',
                turnId='planning', toolStatus='success', status='complete',
                toolArgs=dict(title='Verification', question='Run?', markdown=markdown),
                toolResult=dict(details=dict(proposal=deepcopy(self.original)), terminate=True))]
        self.complete_transcript = True
        test = self
        class ReadOnly:
            def __init__(self, *args, **kwargs):
                pass
            def connect(self, **kwargs):
                pass
            def session_get(self, sid, **kwargs):
                return dict(session=dict(id=sid, messages=deepcopy(test.messages), messageStart=0,
                    messageEnd=len(test.messages), hasMoreBefore=False,
                    hasMoreAfter=not test.complete_transcript))
            def __getattr__(self, name):
                if name in ('agent_prompt','plans_resolve','session_create','session_configure'):
                    raise AssertionError('historical read attempted write: ' + name)
                return getattr(test.plus, name)
        self.reader = ReadOnly
        self.case.patch(patch.object(verification, 'HistoricalReadClient', ReadOnly, create=True))
        if blocked:
            self.value.update(state='blocked', failure_code='release_verification_proposal',
                reason='缺少本轮唯一可批准 Goal')
            verification._save(self.conn, self.value, self.ctx.now_iso)
        return first

    def test_manual_approval_adopts_completed_report_without_forging_approval_or_pass(self):
        self.manual_approval()
        result = self.case.sync(self.vid)
        value = verification._load(self.conn, 'parent', self.vid)
        self.assertEqual(result['verification']['failure_code'], 'release_verification_report')
        self.assertEqual(value['approval_observation']['source'], 'plus_history')
        self.assertEqual(value['approval_observation']['previous_error']['reason'], '缺少本轮唯一可批准 Goal')
        self.assertFalse(value.get('approval_sent'))
        self.assertEqual(value['report_payload'], self.plus.goal_report_get('supplement'))
        self.assertEqual(value['report_sha256'], self.plus.goal_report_get('supplement')['report_sha256'])
        self.assertEqual(self.plus.approvals, 0)
        self.assertIsNone(db.get_runtime_operation(self.conn, *verification._approval_operation(value)))
        before = self.conn.total_changes
        prepared = verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.plus.created, 1)
        args = {key: prepared[key] for key in ('expected_verification_id','expected_context_sha256','expected_report_sha256')}
        restarted = self.case.restart(dict(task_id='parent', **args))
        self.assertNotEqual(restarted['verification_id'], self.vid)
        self.assertEqual(self.case.restart(dict(task_id='parent', **args))['verification_id'], restarted['verification_id'])
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (2, 2, 0))
        self.assertEqual(verification._load(self.conn, 'parent', self.vid), value)

    def test_restart_preview_after_plus_reset_preserves_historical_read_only_approval(self):
        self.manual_approval()
        self.messages.append(dict(id='final', role='assistant', turnId='execution', content='Done'))
        self.assertEqual(self.case.sync(self.vid)['verification']['failure_code'], 'release_verification_report')
        old = verification._load(self.conn, 'parent', self.vid)
        original_turn_get = self.plus.turn_get
        def turn_get(sid, turn, **kwargs):
            if turn == 'execution':
                return dict(sessionId=sid, turnId=turn, status='completed',
                    endedAt=self.plus.report['report']['execution']['endedAt'])
            return original_turn_get(sid, turn, **kwargs)
        with patch.object(self.plus, 'agent_status', return_value=dict(status=dict(
                sessionId=old['session_id'], isRunning=False))), patch.object(self.plus, 'turn_get', side_effect=turn_get):
            before = self.conn.total_changes
            prepared = verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
            self.assertEqual(self.conn.total_changes, before)
            self.assertEqual(prepared['expected_verification_id'], self.vid)
            with self.assertRaises(util.KanpilotError):
                verification._completed_report(self.conn, self.reader(), old)
        self.assertEqual(verification._load(self.conn, 'parent', self.vid), old)
        self.assertIsNone(db.get_runtime_operation(self.conn, *verification._approval_operation(old)))
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_planning_can_observe_manual_approval_after_history_version_growth(self):
        self.manual_approval(blocked=False)
        result = self.case.sync(self.vid)
        value = verification._load(self.conn, 'parent', self.vid)
        self.assertEqual(result['verification']['state'], 'blocked')
        self.assertEqual(value['approval_observation']['history']['version'], 4)
        self.assertEqual(self.plus.approvals, 0)

    def test_observed_pending_and_running_execution_can_finish_without_any_write(self):
        self.manual_approval()
        self.plus.current = 'planning'
        self.plus.history_changes['executionState'] = 'pending'
        self.assertEqual(self.case.sync(self.vid)['verification']['state'], 'unknown')
        self.plus.current = 'execution'; self.plus.running = True
        self.plus.history_changes['executionState'] = 'running'
        self.plus.history_version += 1
        self.messages.append(dict(role='assistant', turnId='execution', content='Checking frozen commands'))
        self.assertEqual(self.case.sync(self.vid)['verification']['state'], 'running')
        self.plus.running = False
        self.plus.history_changes['executionState'] = 'completed'
        self.plus.history_version += 1
        self.assertEqual(self.case.sync(self.vid)['verification']['failure_code'], 'release_verification_report')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_legacy_prompt_stays_exact_and_new_prompt_freezes_native_command_contract(self):
        self.manual_approval()
        self.assertEqual(self.value['prompt_contract_version'], 3)
        self.assertEqual(self.value['prompt_sha256'], util.sha256_text(self.plus.prompt))
        self.assertIn('Bash 工具调用的 command 必须与对应冻结命令完整逐字一致', self.plus.prompt)
        self.assertIn('每次 Bash 都是独立 shell', self.plus.prompt)
        self.assertIn('kind="tool_result"', self.plus.prompt)
        self.assertIn('commandShellId', self.plus.prompt)
        legacy = deepcopy(self.value)
        legacy.pop('prompt_contract_version'); legacy.pop('prompt_sha256')
        legacy_prompt = verification._prompt(legacy)
        self.assertNotIn('每次 Bash 都是独立 shell', legacy_prompt)
        self.assertTrue(legacy_prompt.endswith('\n'.join(self.plus.commands)))
        self.messages[0]['content'] = legacy_prompt
        verification._save(self.conn, legacy, self.ctx.now_iso)
        self.assertEqual(self.case.sync(self.vid)['verification']['failure_code'], 'release_verification_report')
        observed = verification._load(self.conn, 'parent', self.vid)
        self.assertEqual(observed['approval_observation']['prompt_sha256'], util.sha256_text(legacy_prompt))
        self.assertEqual(observed['context']['commands'], self.plus.commands)

    def test_v1_v2_prompt_bytes_and_v2_history_remain_compatible(self):
        frozen = dict(context=dict(target_sha='a'*40, context_sha256='b'*64,
            commands=['echo verify', 'python3 -m unittest']))
        for version, digest in (
            (1, 'cfec3e378a3eed73ad88c14060260e2769f238e79a182260bab0708710d54267'),
            (2, 'a6372aeb8c31e28adaacfbc8cf332219f5c57159ba2fc5c806a34c788523192b'),
        ):
            with self.subTest(version=version):
                frozen['prompt_contract_version'] = version
                self.assertEqual(util.sha256_text(verification._prompt(frozen)), digest)
        self.manual_approval()
        self.value['prompt_contract_version'] = 2
        original = verification._prompt(self.value)
        self.value['prompt_sha256'] = util.sha256_text(original)
        self.messages[0]['content'] = original
        verification._save(self.conn, self.value, self.ctx.now_iso)
        self.assertEqual(self.case.sync(self.vid)['verification']['failure_code'], 'release_verification_report')
        observed = verification._load(self.conn, 'parent', self.vid)
        self.assertEqual(observed['approval_observation']['prompt_sha256'], util.sha256_text(original))
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_unknown_write_and_local_scope_or_task_context_drift_are_rejected(self):
        self.manual_approval()
        scope = verification._approval_operation(self.value)[0]
        runtime_operations.begin_operation(self.conn, scope, 'unknown-write', 'pi_desktop', 'unknown_write', {})
        self.assertFalse(verification._history_recoverable(self.conn, self.value))
        self.case.sync(self.vid)
        self.assertNotIn('approval_observation', verification._load(self.conn, 'parent', self.vid))
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?', (scope, 'unknown-write'))
        for name, mutate in [
            ('task reference', lambda: db.set_extra(self.conn, self.case.task(), self.ctx.now_iso,
                release_verification_ref=dict(verification_id='f'*32, context_sha256='f'*64))),
            ('root scope', lambda: self.conn.execute('UPDATE runtime_operations SET harness_id=? WHERE scope_id=? AND operation_id=?',
                ('wrong', verification._scope('parent'), verification._op(self.vid)))),
        ]:
            with self.subTest(name=name):
                verification._save(self.conn, self.value, self.ctx.now_iso)
                mutate()
                self.case.sync(self.vid)
                self.assertNotIn('approval_observation', verification._load(self.conn, 'parent', self.vid))
                self.assertEqual(self.plus.approvals, 0)
                db.set_extra(self.conn, self.case.task(), self.ctx.now_iso, release_verification_ref=dict(
                    verification_id=self.vid, context_sha256=self.value['context']['context_sha256']))

    def test_report_identity_drift_and_restart_pending_asks_are_rejected(self):
        self.manual_approval()
        self.plus.report['report']['turnId'] = 'other'
        self.case.sync(self.vid)
        self.assertNotIn('approval_observation', verification._load(self.conn, 'parent', self.vid))
        self.plus.report['report']['turnId'] = 'execution'
        verification._save(self.conn, self.value, self.ctx.now_iso)
        self.case.sync(self.vid)
        self.plus.pending_asks = [dict(id='pending')]
        before = self.conn.total_changes
        with self.assertRaises(util.KanpilotError):
            verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.plus.created, 1)

    def test_transient_history_reads_keep_same_attempt_and_write_denying_reader(self):
        self.manual_approval()
        baseline = deepcopy(self.value)
        for stage in ('connect', 'transcript'):
            with self.subTest(stage=stage):
                verification._save(self.conn, baseline, self.ctx.now_iso)
                failure = util.KanpilotError('timeout', 'isolated transient historical read')
                target = patch.object(verification, 'HistoricalReadClient', side_effect=failure) if stage == 'connect' else patch.object(
                    self.reader, 'session_get', side_effect=failure)
                with patch.object(verification, 'RuntimeClient', side_effect=AssertionError('must remain historical read-only')):
                    with target:
                        result = self.case.sync(self.vid)
                    self.assertEqual(result['verification']['state'], 'unknown')
                    pending = verification._load(self.conn, 'parent', self.vid)
                    self.assertTrue(pending['history_read_only'])
                    self.assertTrue(verification._history_recoverable(self.conn, pending))
                    self.assertEqual(self.case.sync(self.vid)['verification']['failure_code'], 'release_verification_report')
                self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_delivery_poll_during_historical_io_does_not_discard_original_report(self):
        self.manual_approval()
        original = verification._observed_report
        refreshed_at = '2026-10-22T12:15:40+00:00'
        requests = []
        def delivery_poll(conn, client, task, value):
            result = original(conn, client, task, value)
            fresh = self.case.task()
            request, created = db.create_request(conn, refreshed_at, 'delivery', 'history-delivery-poll',
                task_id='parent', payload={}, requested_by='automation')
            self.assertTrue(created)
            identity = dict(task_id='parent', request_id=request['request_id'], run_id='delivery-poll')
            db.update_request(conn, request['request_id'], refreshed_at, status='claimed',
                run_id=identity['run_id'], claimed_at=refreshed_at)
            delivery = core.delivery_of(fresh)
            contract = delivery_contract.freeze(fresh, identity, delivery, 'sync')
            envelope = delivery_contract.verified_result(contract, fresh, identity, delivery,
                dict(outcome='waiting', message='PR 已合并，等待交付收尾'))
            core.update_delivery(conn, fresh, refreshed_at, last_sync_at=refreshed_at,
                next_sync_at='2026-10-22T12:20:40+00:00',
                auto_merge=dict(state='merged', head_sha=delivery['head_sha'], updated_at=refreshed_at),
                stage_contracts=dict(delivery.get('stage_contracts') or {}, sync=contract),
                stage_results=dict(delivery.get('stage_results') or {}, sync=envelope))
            db.update_request(conn, request['request_id'], refreshed_at, status='succeeded',
                finished_at=refreshed_at, result_json=dict(outcome='waiting'))
            requests.append(request['request_id'])
            return result
        with patch.object(verification, '_observed_report', side_effect=delivery_poll):
            result = self.case.sync(self.vid)
        value = verification._load(self.conn, 'parent', self.vid)
        self.assertEqual(result['verification']['failure_code'], 'release_verification_report')
        self.assertEqual(value['approval_observation']['source'], 'plus_history')
        self.assertEqual(value['report_payload'], self.plus.goal_report_get('supplement'))
        self.assertEqual(core.delivery_of(self.case.task())['last_sync_at'], refreshed_at)
        self.assertEqual(db.get_request(self.conn, requests[0])['status'], 'succeeded')
        self.assertFalse(value.get('approval_sent'))
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_legacy_snapshot_conflict_can_reconcile_same_attempt_without_resending(self):
        self.manual_approval()
        self.value.update(state='blocked', failure_code='release_verification_history',
            reason='历史观察写回前原尝试或下游写状态已变化', history_read_only=True)
        verification._save(self.conn, self.value, self.ctx.now_iso)
        summary_value = dict(context_sha256='e'*64, candidate=dict(pr_number=9,
            pr_url='https://github.com/example/closeout/pull/9', target_sha=self.value['context']['target_sha'],
            source_batch_id='failed', source_tag='v0.1.0-pilot.1'))
        self.assertTrue(verification._history_recoverable(self.conn, self.value))
        self.assertTrue(repair.summary(self.conn, self.case.task(), summary_value)['can_sync_verification'])
        with patch.object(verification, 'RuntimeClient', side_effect=AssertionError('history recovery must not write')):
            result = self.case.sync(self.vid)
        recovered = verification._load(self.conn, 'parent', self.vid)
        self.assertEqual(result['verification']['failure_code'], 'release_verification_report')
        self.assertEqual(recovered['verification_id'], self.vid)
        self.assertEqual(recovered['approval_observation']['source'], 'plus_history')
        self.assertEqual(recovered['report_payload'], self.plus.goal_report_get('supplement'))
        self.assertFalse(recovered.get('approval_sent'))
        self.assertIsNone(db.get_runtime_operation(self.conn, *verification._approval_operation(recovered)))
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_legacy_history_recovery_is_limited_to_exact_read_only_conflict(self):
        self.manual_approval()
        baseline = deepcopy(self.value)
        for reason, read_only in (('历史会话与原运行时作用域不符', True),
                ('历史观察写回前原尝试或下游写状态已变化', False)):
            with self.subTest(reason=reason, read_only=read_only):
                value = dict(baseline, state='blocked', failure_code='release_verification_history',
                    reason=reason, history_read_only=read_only)
                verification._save(self.conn, value, self.ctx.now_iso)
                self.assertFalse(verification._history_recoverable(self.conn, value))
                with patch.object(verification, 'HistoricalReadClient', side_effect=AssertionError('unsafe legacy recovery')):
                    self.case.sync(self.vid)
                self.assertEqual(verification._load(self.conn, 'parent', self.vid), value)
        conflict = dict(baseline, state='blocked', failure_code='release_verification_history',
            reason='历史观察写回前原尝试或下游写状态已变化', history_read_only=True)
        verification._save(self.conn, conflict, self.ctx.now_iso)
        runtime_operations.begin_operation(self.conn, verification._approval_operation(conflict)[0],
            'unknown-write', 'pi_desktop', 'unknown_write', {})
        self.assertFalse(verification._history_recoverable(self.conn, conflict))
        self.case.sync(self.vid)
        current = verification._load(self.conn, 'parent', self.vid)
        self.assertNotIn('approval_observation', current)
        self.assertNotIn('report_payload', current)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_late_pause_takeover_review_or_unknown_write_still_rejects_historical_observation(self):
        self.manual_approval()
        baseline = deepcopy(self.value)
        original_task = dict(self.case.task())
        original_paused = db.get_setting(self.conn, 'paused_stages', [])
        original_guard = verification._history_local_guard
        scope = verification._approval_operation(baseline)[0]
        def take_over():
            core.update_automation(self.conn, self.case.task(), self.ctx.now_iso, manual_takeover=True)
        mutations = (
            ('delivery pause', lambda: db.set_setting(self.conn, 'paused_stages', ['delivery'])),
            ('manual takeover', take_over),
            ('new review request', lambda: db.create_request(self.conn, self.ctx.now_iso, 'review',
                'late-review', task_id='parent')),
            ('unknown write', lambda: runtime_operations.begin_operation(self.conn, scope,
                'late-unknown', 'pi_desktop', 'unknown_write', {})),
        )
        for name, mutate in mutations:
            with self.subTest(change=name):
                verification._save(self.conn, baseline, self.ctx.now_iso)
                def change_after_guard(conn, task, value):
                    original_guard(conn, task, value)
                    if value.get('approval_observation'):
                        mutate()
                with patch.object(verification, '_history_local_guard', side_effect=change_after_guard):
                    self.case.sync(self.vid)
                value = verification._load(self.conn, 'parent', self.vid)
                self.assertNotIn('approval_observation', value)
                self.assertNotIn('report_payload', value)
                self.assertFalse(value.get('approval_sent'))
                self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?',
                    (scope, 'late-unknown'))
                self.conn.execute("DELETE FROM requests WHERE idempotency_key='late-review'")
                db.set_setting(self.conn, 'paused_stages', original_paused)
                db.update_task(self.conn, 'parent', self.ctx.now_iso, extra_json=original_task['extra_json'])
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_slow_history_success_or_failure_never_overwrites_concurrent_completed_observation(self):
        self.manual_approval()
        baseline = deepcopy(self.value)
        original = verification._observed_report
        for fail in (False, True):
            with self.subTest(read_failed=fail):
                verification._save(self.conn, baseline, self.ctx.now_iso)
                winners = []
                def finish_elsewhere(conn, client, task, value):
                    observation, turn, report = original(conn, client, task, value)
                    winner = deepcopy(value)
                    winner.update(approval_observation=observation, proposal=observation['proposal'],
                        execution_turn_id=turn, state='blocked', failure_code='release_verification_report',
                        reason='concurrent complete observation')
                    verification._record_report(winner, report)
                    verification._save(conn, winner, self.ctx.now_iso)
                    winners.append(deepcopy(winner))
                    if fail:
                        raise util.KanpilotError('timeout', 'slow reader lost response')
                    return observation, turn, report
                with patch.object(verification, '_observed_report', side_effect=finish_elsewhere):
                    result = self.case.sync(self.vid)
                self.assertEqual(verification._load(self.conn, 'parent', self.vid), winners[0])
                self.assertEqual(result['verification']['reason'], 'concurrent complete observation')

    def test_task_change_during_historical_io_prevents_stale_observation_save(self):
        self.manual_approval()
        original = verification._observed_report
        def change_after_read(*args):
            result = original(*args)
            self.conn.execute("UPDATE tasks SET plan_sha256=? WHERE task_id='parent'", ('f'*64,))
            return result
        with patch.object(verification, '_observed_report', side_effect=change_after_read):
            self.case.sync(self.vid)
        value = verification._load(self.conn, 'parent', self.vid)
        self.assertNotIn('approval_observation', value)
        self.assertEqual(value, self.value)

    def test_source_change_after_report_read_does_not_commit_provisional_observation(self):
        self.manual_approval()
        original = verification._observed_report
        def change_after_read(*args):
            result = original(*args)
            (Path(self.value['worktree_path']) / 'unexpected-source.txt').write_text('source changed')
            return result
        with patch.object(verification, '_observed_report', side_effect=change_after_read):
            self.case.sync(self.vid)
        value = verification._load(self.conn, 'parent', self.vid)
        self.assertNotIn('approval_observation', value)
        self.assertNotIn('report_payload', value)
        self.assertEqual(value['failure_code'], 'release_verification_source')

    def test_historical_and_github_reads_never_hold_sqlite_write_transaction(self):
        self.manual_approval()
        calls = []
        def unlocked(name, function):
            def invoke(*args, **kwargs):
                self.assertFalse(self.conn.in_transaction, name + ' ran under SQLite transaction')
                calls.append(name)
                return function(*args, **kwargs)
            return invoke
        for name in ('agent_status', 'turn_get', 'plans_pending', 'goal_report_get', 'input_pending'):
            self.case.patch(patch.object(self.plus, name, side_effect=unlocked('history.' + name, getattr(self.plus, name))))
        self.case.patch(patch.object(self.reader, 'session_get', side_effect=unlocked('history.session_get', self.reader().session_get)))
        for name in ('pr', 'commit', 'contains'):
            self.case.patch(patch.object(release.closeout.github, name, side_effect=unlocked('github.' + name, getattr(release.closeout.github, name))))
        self.assertEqual(self.case.sync(self.vid)['verification']['failure_code'], 'release_verification_report')
        verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertIn('github.pr', calls)
        self.assertIn('history.session_get', calls)

    def test_local_round_change_after_final_remote_guard_fails_sql_snapshot_commit(self):
        self.manual_approval()
        original = verification._history_local_guard
        config = self.conn.execute("SELECT config_json FROM execution_rounds WHERE task_id='parent'").fetchone()[0]
        calls = []
        def change_after_guard(conn, task, value):
            original(conn, task, value)
            calls.append(True)
            if value.get('approval_observation'):
                conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id='parent'", ('{}',))
        with patch.object(verification, '_history_local_guard', side_effect=change_after_guard):
            self.case.sync(self.vid)
        self.assertGreaterEqual(len(calls), 2)
        value = verification._load(self.conn, 'parent', self.vid)
        self.assertNotIn('approval_observation', value)
        self.assertNotIn('report_payload', value)
        self.assertEqual(value['state'], 'unknown')
        self.assertEqual(value['failure_code'], 'release_verification_history_conflict')
        self.assertTrue(verification._history_recoverable(self.conn, value))
        self.conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id='parent'", (config,))
        with patch.object(verification, 'RuntimeClient', side_effect=AssertionError('conflict recovery must not write')):
            result = self.case.sync(self.vid)
        self.assertEqual(result['verification']['failure_code'], 'release_verification_report')
        self.assertEqual(verification._load(self.conn, 'parent', self.vid)['approval_observation']['source'], 'plus_history')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))

    def test_transcript_goal_artifact_identity_and_unknown_write_drift_are_rejected(self):
        self.manual_approval()
        cases = [
            ('extra user', lambda: self.messages.append(dict(role='user', content='also change things', turnId='execution'))),
            ('prompt', lambda: self.messages[0].update(content=self.plus.prompt + '!')),
            ('planning turn', lambda: self.messages[1].update(turnId='other')),
            ('tool identity', lambda: self.plus.history_changes.update(toolCallId='other')),
            ('scope identity', lambda: self.plus.history_changes.update(sessionId='other')),
            ('goal body', lambda: self.plus.history_changes.update(markdown='different')),
            ('artifact', lambda: (Path(self.value['worktree_path']) / '.pi/goal/contract.md').write_text('drift')),
            ('pagination', lambda: setattr(self, 'complete_transcript', False)),
        ]
        original_messages = deepcopy(self.messages)
        original_body = self.original['markdown']
        for name, mutate in cases:
            with self.subTest(name=name):
                self.messages = deepcopy(original_messages)
                self.plus.history_changes = dict(executionKind='goal', executionState='completed')
                self.complete_transcript = True
                (Path(self.value['worktree_path']) / '.pi/goal/contract.md').write_text(original_body)
                verification._save(self.conn, self.value, self.ctx.now_iso)
                mutate()
                self.case.sync(self.vid)
                value = verification._load(self.conn, 'parent', self.vid)
                self.assertNotIn('approval_observation', value)
                self.assertEqual(value['state'], 'blocked')
                self.assertEqual(self.plus.approvals, 0)

    def test_host_inconclusive_cannot_be_overridden_by_top_level_met(self):
        host_evidence(self.plus.report['report'])
        self.plus.report['report']['checkObservations'][0]['result'] = 'inconclusive'
        first = self.case.start()
        result = self.case.sync(first['verification_id'])
        self.assertEqual(result['verification']['state'], 'blocked')
        self.assertEqual(result['verification']['failure_code'], 'release_verification_report')

    def test_native_host_pass_with_unresolved_call_id_resolution_remains_valid(self):
        host_evidence(self.plus.report['report'])
        self.case.complete()

    def test_shell_name_evidence_cannot_promote_met_or_start_another_execution(self):
        host_evidence(self.plus.report['report'])
        body = self.plus.report['report']
        for evidence in body['evidences']:
            evidence.update(kind='tool_call', refId='bash')
        for observation in body['checkObservations']:
            observation.update(result='inconclusive')
            observation.pop('exitCode')
        first = self.case.start()
        result = self.case.sync(first['verification_id'])
        self.assertEqual(result['verification']['failure_code'], 'release_verification_report')
        saved = verification._load(self.conn, 'parent', first['verification_id'])
        self.assertEqual(saved['report_payload'], self.plus.goal_report_get('supplement'))
        self.assertEqual(saved['state'], 'blocked')
        self.case.sync(first['verification_id'])
        self.assertEqual(verification._load(self.conn, 'parent', first['verification_id']), saved)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_missing_or_ambiguous_host_evidence_is_blocked(self):
        host_evidence(self.plus.report['report'])
        baseline = deepcopy(self.plus.report)
        for name, mutate in [
            ('missing observations', lambda b: b.pop('checkObservations')),
            ('missing exit', lambda b: b['checkObservations'][0].pop('exitCode')),
            ('bool exit', lambda b: b['checkObservations'][0].update(exitCode=False)),
            ('duplicate observation', lambda b: b['checkObservations'].append(deepcopy(b['checkObservations'][0]))),
            ('wrong command', lambda b: b['checkObservations'][0].update(command='echo changed')),
            ('wrong references', lambda b: b['checkObservations'][0].update(evidenceIds=['missing'])),
            ('tool call without result', lambda b: b['evidences'][0].update(kind='tool_call')),
        ]:
            with self.subTest(name=name):
                self.plus.report = deepcopy(baseline)
                mutate(self.plus.report['report'])
                first = self.case.start()
                result = self.case.sync(first['verification_id'])
                self.assertEqual(result['verification']['state'], 'blocked')
                # Reuse immutable attempt only after resetting fixture receipt;
                # each case is testing the report predicate itself below.
                value = verification._load(self.conn, 'parent', first['verification_id'])
                with self.assertRaises(util.KanpilotError):
                    verification._report(self.conn, self.plus, value)
