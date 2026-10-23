"""Real runtime dispatch/guards and Plus adapter, with an isolated fake Host."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import test_release_verification as fixtures
from kanpilot import (constants, db, harness_plus_adapter, mcp_scopes, mcp_tools,
                      release_verification as verification, runtime_operations, util)
from kanpilot.runtime_client import stable_scope_id


class ReleaseVerificationRuntimeTest(TestCase):
    def make_case(self):
        case=fixtures.ReleaseVerificationTest('test_cli_fixed_payload_round_trip')
        case.setUp()
        return case

    def setUp(self):
        self.case = self.make_case()
        self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx, self.plus = self.case.conn, self.case.ctx, self.case.plus
        # Original execution configs may carry backend/model_source metadata;
        # RuntimeClient intentionally keeps only EXEC_PI_DESKTOP_KEYS.
        original = self.conn.execute("SELECT config_json FROM execution_rounds WHERE task_id='parent'").fetchone()
        config = dict(util.jload(original['config_json']), backend='pi_desktop', model_source='custom')
        self.conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id='parent'", (util.jdump(config),))
        extra = db.task_extra(self.case.task())
        for name in ('job.json', 'spec.json'):
            path = self.case.fixture.runs_root / 'parent' / 'run-parent' / name
            content = util.jload(path.read_text()); content['config'] = config
            path.write_text(util.jdump(content))
            extra['engine_verdict']['evidence_sha256'][str(path)] = util.sha256_file(path)
        db.set_extra(self.conn, self.case.task(), self.ctx.now_iso, engine_verdict=extra['engine_verdict'])
        self.first = self.case.start()
        self.verification_id = self.first['verification_id']
        self.value = verification._load(self.conn, 'parent', self.verification_id)
        invocation = 'releaseverify_' + self.verification_id
        config = self.value['context']['runtime_config']
        self.scope = mcp_scopes.Scope(stable_scope_id('execution', invocation_id=invocation),
            invocation, 'execution', util.to_iso(util.now_dt() + timedelta(minutes=10)),
            project_path=self.value['worktree_path'], connection_file=config['connection_file'],
            runtime_config={key: config[key] for key in constants.EXEC_PI_DESKTOP_KEYS if key in config})
        self.manager = mcp_scopes.ScopeManager(self.case.fixture.root / 'isolated-mcp')
        self.manager.scopes[self.scope.scope_id] = self.scope
        with db.write_txn(self.conn):
            runtime_operations.begin_operation(self.conn, self.scope.scope_id, 'session_create',
                'pi_desktop', 'kp_runtime_session_create', {})
            runtime_operations.complete_operation(self.conn, self.scope.scope_id, 'session_create',
                session_id='supplement', result=dict(session_id='supplement'))
        self.host_calls = []
        original_resolve = self.plus.plans_resolve
        case = self
        class Host:
            def supports_required_verification_commands(self):
                return True
            def call_tool(self, name, args):
                case.host_calls.append((name, deepcopy(args)))
                if name == 'pi_plans_pending':
                    return case.plus.plans_pending(args['sessionId'])
                if name == 'pi_plans_resolve':
                    return original_resolve(args['proposalId'], args['sessionId'], args['turnId'],
                        args['toolCallId'], args['action'], required_verification_commands=args['requiredVerificationCommands'])
                raise AssertionError(name)
        patcher = patch.object(harness_plus_adapter, '_get_client', return_value=(Host(), {}))
        patcher.start(); self.addCleanup(patcher.stop)
        def resolve(proposal, sid, turn, tool, action, **kwargs):
            return self.dispatch(dict(self.args(), proposal_id=proposal, session_id=sid,
                turn_id=turn, tool_call_id=tool, version=kwargs['version'],
                target_permission_mode=kwargs['target_permission_mode'],
                required_verification_commands=kwargs['required_verification_commands']))
        self.plus.plans_resolve = resolve

    def args(self):
        return dict(scope_id=self.scope.scope_id, operation_id='proposal_proposal_accept',
            session_id=self.plus.session_id, turn_id='planning', proposal_id='proposal', tool_call_id='tool-call',
            version=1, action='accept', target_permission_mode='auto',
            required_verification_commands=list(self.plus.commands))

    def dispatch(self, args=None):
        return mcp_tools.dispatch_tool_call(self.case.fixture.db_path, self.manager, 'runtime', self.scope,
            'kp_runtime_proposal_resolve', args or self.args())

    def ready(self):
        value = verification._load(self.conn, 'parent', self.verification_id)
        value.update(state='unknown', approval_sent=True, approval_phase='pre_reservation', proposal=deepcopy(self.plus.proposal))
        with db.write_txn(self.conn):
            verification._save(self.conn, value, self.ctx.now_iso)
        return value

    def assert_not_sent(self):
        self.assertEqual(self.plus.approvals, 0)
        self.assertIsNone(db.get_runtime_operation(self.conn, self.scope.scope_id, 'proposal_proposal_accept'))

    def test_real_dispatch_accepts_core_receipt_and_forwards_frozen_commands(self):
        with patch.dict('os.environ',{'KANPILOT_RELEASE_WORKTREE_ROOT':str(self.case.fixture.root / 'changed-root')}):
            result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'verified', result)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))
        self.assertEqual(self.plus.required, self.plus.commands)
        row = db.get_runtime_operation(self.conn, self.scope.scope_id, 'proposal_proposal_accept')
        self.assertEqual(row['status'], 'accepted')
        self.assertFalse(runtime_operations.session_attached(self.conn, self.scope.scope_id, 'supplement'))
        self.assertEqual([name for name, _ in self.host_calls], ['pi_plans_pending', 'pi_plans_resolve'])

    def test_execution_refresh_version_uses_accepted_approval_without_changing_content(self):
        self.plus.proposal.update(artifact=dict(relativePath='.pi/goal/verify.md', sha256='a' * 64,
            sizeBytes=10, workspaceKind='project'), markdown='Frozen Goal', plan='Run frozen verification')
        self.plus.history_version = 3
        self.plus.history_changes = dict(executionState='running', updatedAt='2026-10-21T05:00:00Z')
        result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'verified', result)
        self.assertEqual(self.plus.approvals, 1)
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'verified')

        stored = verification._load(self.conn, 'parent', self.verification_id)
        self.assertEqual(stored['proposal']['version'], 1)
        self.assertEqual(stored['proposal']['artifact'], self.plus.proposal['artifact'])

        # A version increment cannot substitute for a trusted approval or conceal
        # edits to its artifact, content, commands, or execution identity.
        original_changes = deepcopy(self.plus.history_changes)
        for change in (dict(artifact=dict(self.plus.proposal['artifact'], sha256='b' * 64)),
                dict(markdown='changed'), dict(plan='changed'), dict(requiredVerificationCommands=['forged']),
                dict(executionId='other'), dict(version=0)):
            with self.subTest(change=change):
                self.plus.history_changes = dict(original_changes, **change)
                with self.assertRaises(util.KanpilotError):
                    verification.verified_proof(self.conn, self.case.task())
        self.plus.history_changes = original_changes
        scope_id, op_id = verification._approval_operation(stored)
        for state in ('unknown', 'rejected'):
            with self.subTest(receipt_state=state):
                self.conn.execute('UPDATE runtime_operations SET status=? WHERE scope_id=? AND operation_id=?',
                    (state, scope_id, op_id))
                with self.assertRaises(util.KanpilotError):
                    verification.verified_proof(self.conn, self.case.task())
        self.conn.execute('UPDATE runtime_operations SET status=? WHERE scope_id=? AND operation_id=?',
            ('accepted', scope_id, op_id))
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'verified')

    def restarted_attempt(self):
        self.plus.report['report']['criteria'][0]['verdict'] = 'partial'
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'blocked')
        old = verification._load(self.conn, 'parent', self.verification_id)
        report = self.plus.goal_report_get(old['session_id'])
        result = verification.cmd_restart_release_verification(self.ctx, dict(task_id='parent',
            expected_verification_id=old['verification_id'], expected_context_sha256=old['context']['context_sha256'],
            expected_report_sha256=report['report_sha256']))
        self.verification_id = result['verification_id']
        self.value = verification._load(self.conn, 'parent', self.verification_id)
        invocation = 'releaseverify_' + self.verification_id
        config = self.value['context']['runtime_config']
        self.scope = mcp_scopes.Scope(stable_scope_id('execution', invocation_id=invocation),
            invocation, 'execution', util.to_iso(util.now_dt() + timedelta(minutes=10)),
            project_path=self.value['worktree_path'], connection_file=config['connection_file'],
            runtime_config={key: config[key] for key in constants.EXEC_PI_DESKTOP_KEYS if key in config})
        self.manager.scopes[self.scope.scope_id] = self.scope
        with db.write_txn(self.conn):
            runtime_operations.begin_operation(self.conn, self.scope.scope_id, 'session_create',
                'pi_desktop', 'kp_runtime_session_create', {})
            runtime_operations.complete_operation(self.conn, self.scope.scope_id, 'session_create',
                session_id=self.value['session_id'], result=dict(session_id=self.value['session_id']))
        return old

    def test_restarted_attempt_uses_new_runtime_scope_and_exact_lineage_authorization(self):
        old = self.restarted_attempt()
        result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'verified', result)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (2, 2, 2))
        self.assertNotEqual(self.value['session_id'], old['session_id'])
        self.assertEqual(self.plus.required, old['context']['commands'])
        receipt = db.get_runtime_operation(self.conn, self.scope.scope_id, 'proposal_proposal_accept')
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(receipt['session_id'], self.value['session_id'])
        self.assertEqual(verification._load(self.conn, 'parent', old['verification_id']), old)

    def test_restarted_guard_rejects_missing_or_tampered_authorization_before_write(self):
        self.restarted_attempt(); self.ready()
        scope = verification._scope('parent')
        key = verification._restart_operation(self.value['context']['restart'])
        row = dict(db.get_runtime_operation(self.conn, scope, key))
        for column, value in (('status', 'unknown'), ('request_sha256', 'c' * 64), ('result_json', '{}')):
            with self.subTest(column=column):
                self.conn.execute('UPDATE runtime_operations SET ' + column + '=? WHERE scope_id=? AND operation_id=?',
                    (value, scope, key))
                with self.assertRaises(util.KanpilotError):
                    self.dispatch()
                self.conn.execute('UPDATE runtime_operations SET ' + column + '=? WHERE scope_id=? AND operation_id=?',
                    (row[column], scope, key))
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?', (scope, key))
        with self.assertRaises(util.KanpilotError):
            self.dispatch()
        self.assertEqual(self.plus.approvals, 1)
        self.assertIsNone(db.get_runtime_operation(self.conn, self.scope.scope_id, 'proposal_proposal_accept'))
    def test_missing_or_forged_root_receipt_is_rejected_before_downstream_reservation(self):
        value = self.ready()
        row = dict(db.get_runtime_operation(self.conn, verification._scope('parent'), verification._op(self.verification_id)))
        self.conn.execute('UPDATE runtime_operations SET result_json=? WHERE scope_id=? AND operation_id=?',
            (util.jdump(dict(value, context=dict(value['context'], commands=['forged command']))), row['scope_id'], row['operation_id']))
        with self.assertRaises(util.KanpilotError):
            self.dispatch()
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?',
            (row['scope_id'], row['operation_id']))
        with self.assertRaises(util.KanpilotError):
            self.dispatch()
        self.assert_not_sent()

    def test_caller_commands_and_scope_identity_cannot_authorize_verification(self):
        self.ready()
        variants = [dict(required_verification_commands=['forged command']), dict(session_id='other'),
            dict(turn_id='other'), dict(proposal_id='other'), dict(tool_call_id='other'), dict(version=2),
            dict(operation_id='other'), dict(target_permission_mode='ask'), dict(goal_continuation=True)]
        for change in variants:
            with self.subTest(change=change):
                with self.assertRaises(util.KanpilotError):
                    self.dispatch(dict(self.args(), **change))
        for key, altered in (('invocation_id', 'forged'), ('project_path', str(self.case.fixture.repo)),
                ('connection_file', '/tmp/foreign-connection'), ('runtime_config', {}),
                ('request_id', 'task-grant'), ('task_id', 'parent')):
            with self.subTest(field=key):
                old = getattr(self.scope, key)
                setattr(self.scope, key, altered)
                try:
                    with self.assertRaises(util.KanpilotError):
                        self.dispatch()
                finally:
                    setattr(self.scope, key, old)
        self.assert_not_sent()

    def test_task_identity_or_source_drift_cannot_authorize_verification(self):
        self.ready()
        original = self.case.task()['review_key']
        db.update_task(self.conn, 'parent', self.ctx.now_iso, review_key='changed')
        with self.assertRaises(util.KanpilotError):
            self.dispatch()
        db.update_task(self.conn, 'parent', self.ctx.now_iso, review_key=original)
        (Path(self.value['worktree_path']) / 'fix1.txt').write_text('changed')
        with self.assertRaises(util.KanpilotError):
            self.dispatch()
        self.assert_not_sent()

    def test_reservation_transaction_rechecks_identity_without_network_queries(self):
        self.ready()
        original_reserve = runtime_operations.reserve_downstream
        def changed_before_transaction(conn, *args, **kwargs):
            db.update_task(conn, 'parent', self.ctx.now_iso, review_key='changed-before-reserve')
            return original_reserve(conn, *args, **kwargs)
        with patch.object(verification, '_context', side_effect=AssertionError('network context used in guard')), \
                patch.object(verification, '_unchanged', side_effect=AssertionError('network context used in guard')), \
                patch.object(runtime_operations, 'reserve_downstream', side_effect=changed_before_transaction):
            with self.assertRaises(util.KanpilotError):
                self.dispatch()
        self.assert_not_sent()

    def test_source_and_evidence_preflight_never_hold_runtime_reservation_transaction(self):
        self.ready()
        active = []; observed = []
        trusted = verification.trusted_runtime_commands
        def commands(conn, *args, **kwargs):
            active.append(conn)
            try:
                return trusted(conn, *args, **kwargs)
            finally:
                active.pop()
        def unlocked(name, function):
            def invoke(*args, **kwargs):
                if active:
                    self.assertFalse(active[-1].in_transaction, name + ' ran under SQLite write transaction')
                    observed.append(name)
                return function(*args, **kwargs)
            return invoke
        with patch.object(verification, 'trusted_runtime_commands', side_effect=commands), \
                patch.object(verification, '_source', side_effect=unlocked('source', verification._source)), \
                patch.object(util, 'sha256_file', side_effect=unlocked('evidence', util.sha256_file)), \
                patch.object(Path, 'resolve', autospec=True, side_effect=unlocked('canonical_path', Path.resolve)), \
                patch.object(Path, 'is_file', autospec=True, side_effect=unlocked('file_stat', Path.is_file)):
            self.dispatch()
        self.assertIn('source', observed)
        self.assertIn('evidence', observed)
        self.assertIn('canonical_path', observed)
        self.assertIn('file_stat', observed)
        self.assertEqual(self.plus.approvals, 1)

    def test_reservation_transaction_rechecks_current_verification_reference(self):
        self.ready()
        reserve = runtime_operations.reserve_downstream
        def changed_before_transaction(conn, *args, **kwargs):
            db.set_extra(conn, db.require_task(conn, 'parent'), self.ctx.now_iso,
                release_verification_ref=dict(verification_id='f'*32, context_sha256='f'*64))
            return reserve(conn, *args, **kwargs)
        with patch.object(runtime_operations, 'reserve_downstream', side_effect=changed_before_transaction):
            with self.assertRaises(util.KanpilotError):
                self.dispatch()
        self.assert_not_sent()

    def test_reservation_rejects_scope_path_changed_after_full_preflight(self):
        self.ready()
        reserve = runtime_operations.reserve_downstream
        def changed_before_transaction(conn, *args, **kwargs):
            self.scope.project_path = '/tmp/foreign-after-preflight'
            return reserve(conn, *args, **kwargs)
        with patch.object(runtime_operations, 'reserve_downstream', side_effect=changed_before_transaction):
            with self.assertRaises(util.KanpilotError):
                self.dispatch()
        self.assert_not_sent()

    def test_preflight_rejects_scope_path_changed_during_source_hashing(self):
        self.ready()
        source = verification._source
        def changed_during_source(value):
            result = source(value)
            self.scope.project_path = '/tmp/foreign-during-preflight'
            return result
        with patch.object(verification, '_source', side_effect=changed_during_source):
            with self.assertRaises(util.KanpilotError):
                self.dispatch()
        self.assert_not_sent()

    def old_guard_block(self):
        with patch.object(mcp_tools, '_verification_requirements_guard',
                side_effect=util.KanpilotError('forbidden', '缺少验证要求所属执行轮次')):
            result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'blocked', result)
        self.assert_not_sent()

    def test_known_unsent_old_guard_block_recovers_same_session_once(self):
        self.old_guard_block()
        result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'verified', result)
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_pre_reservation_read_timeout_recovers_the_same_pending_goal(self):
        original = verification._approve_goal
        def read_timeout(ctx, task, value, client):
            # The durable intent exists, but no runtime reserve/write happened.
            stored = verification._load(ctx.conn, task['task_id'], value['verification_id'])
            self.assertEqual(stored['approval_phase'], 'pre_reservation')
            with patch.object(client, 'plans_pending', side_effect=util.KanpilotError('timeout', 'read timeout before reserve')):
                return original(ctx, task, value, client)
        with patch.object(verification, '_approve_goal', side_effect=read_timeout):
            result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'unknown', result)
        self.assert_not_sent()
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_crash_after_durable_approval_intent_before_runtime_reserve_recovers(self):
        with patch.object(verification, '_approve_goal', side_effect=SystemExit('crash after durable intent')):
            with self.assertRaises(SystemExit):
                self.case.sync(self.verification_id)
        stored = verification._load(self.conn, 'parent', self.verification_id)
        self.assertEqual(stored['state'], 'unknown')
        self.assertEqual(stored['approval_phase'], 'pre_reservation')
        self.assert_not_sent()
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_existing_unknown_accepted_or_rejected_approval_never_uses_unsent_recovery(self):
        self.old_guard_block()
        for status in ('unknown', 'accepted', 'rejected'):
            with self.subTest(status=status):
                with db.write_txn(self.conn):
                    runtime_operations.begin_operation(self.conn, self.scope.scope_id, 'proposal_proposal_accept',
                        'pi_desktop', 'kp_runtime_proposal_resolve', self.args())
                    runtime_operations.complete_operation(self.conn, self.scope.scope_id, 'proposal_proposal_accept',
                        status=status, result={})
                self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'blocked')
                self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?',
                    (self.scope.scope_id, 'proposal_proposal_accept'))
        self.assert_not_sent()

    def test_changed_pending_goal_cannot_recover_old_guard_block(self):
        self.old_guard_block()
        self.plus.proposal['version'] = 2
        result = self.case.sync(self.verification_id)
        self.assertEqual(result['verification']['state'], 'blocked', result)
        self.assert_not_sent()

    def test_expired_pending_goal_cannot_recover_old_guard_block(self):
        self.old_guard_block()
        self.plus.proposal['expiresAt'] = util.to_iso(util.now_dt() - timedelta(minutes=1))
        self.assertEqual(self.case.sync(self.verification_id)['verification']['state'], 'blocked')
        self.assert_not_sent()

    def test_dispatch_spec_path_remains_required_for_normal_task_scope(self):
        self.ready()
        self.scope.task_id, self.scope.run_id = 'parent', 'missing-run'
        with self.assertRaises(util.KanpilotError) as caught:
            self.dispatch()
        self.assertIn('冻结执行 spec', str(caught.exception))
        self.assert_not_sent()


class ExternalReleaseVerificationRuntimeTest(TestCase):
    args=ReleaseVerificationRuntimeTest.args
    dispatch=ReleaseVerificationRuntimeTest.dispatch
    ready=ReleaseVerificationRuntimeTest.ready
    assert_not_sent=ReleaseVerificationRuntimeTest.assert_not_sent

    def make_case(self):
        import test_external_build_repair as external_fixtures
        case=external_fixtures.ExternalBuildRepairTest('test_prepare_readonly_accept_archives_and_requires_new_goal')
        case.setUp()
        return case

    def setUp(self):
        import test_external_build_repair as external_fixtures
        from kanpilot import release_delivery as release
        def start(case):
            case.batch['members']=[release._identity(case.task())]
            release._save(case.conn,case.batch,case.ctx.now_iso)
            case.accept()
            return fixtures.ReleaseVerificationTest.start(case)
        with patch.object(external_fixtures.ExternalBuildRepairTest,'start',start):
            ReleaseVerificationRuntimeTest.setUp(self)

    def test_real_dispatch_accepts_external_path_and_frozen_commands(self):
        self.assertIn('/verify-worktrees/',self.value['worktree_path'])
        ReleaseVerificationRuntimeTest.test_real_dispatch_accepts_core_receipt_and_forwards_frozen_commands(self)

    def test_external_link_cannot_disappear_before_goal_authorization(self):
        self.ready()
        extra=db.task_extra(self.case.task());extra.pop('external_build_repair_ref')
        db.update_task(self.conn,'parent',self.ctx.now_iso,extra_json=extra)
        with self.assertRaises(util.KanpilotError):self.dispatch()
        self.assert_not_sent()
