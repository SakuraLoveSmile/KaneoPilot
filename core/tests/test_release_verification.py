"""Release-target supplement: isolated Git/SQLite, fake Plus and GitHub only."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from helpers import goal_checks
from test_git_delivery import git
from test_goal_report_contract import formal_report_payload
from test_release_delivery import FakeReleaseAPI
import test_review_delivery as manual_fixtures
from kanpilot import cli, core, db, release_delivery as release, release_verification as verification, runtime_operations, util


class FakePlus:
    def __init__(self, commands):
        self.commands = commands
        self.created = self.prompts = self.approvals = 0
        self.clients = []
        self.current = 'planning'
        self.running = False
        self.approved = False
        self.lose = None
        self.bad = None
        self.history_version = 2
        self.history_changes = {}
        self.session_id = 'supplement'
        self.saved_reports = {}
        self.pending_asks = []
        self.extra_pending = []
        self.proposal = dict(id='proposal', kind='goal', status='pending', sessionId='supplement',
            turnId='planning', toolCallId='tool-call', version=1)
        self.report = formal_report_payload(sessionId='supplement', proposalId='proposal', executionId='execution-id',
            turnId='execution', criteria=[dict(id='c1', verdict='met')],
            steps=[dict(id='s1', status='completed')], checks=goal_checks(commands))
        # Synthetic Host success evidence for this isolated fake only. Real
        # reports are never amended to manufacture these native observations.
        body = self.report['report']
        body.update(evidences=[], checkObservations=[], evidenceResolution=[])
        for check in body['checks']:
            eid = 'e-' + check['id']
            check['evidenceRefs'] = [eid]
            body['evidences'].append(dict(id=eid, kind='tool_result', refId='call-' + check['id']))
            body['checkObservations'].append(dict(checkId=check['id'], command=check['command'],
                result='passed', exitCode=0, evidenceIds=[eid]))
            body['evidenceResolution'].append(dict(evidenceId=eid, state='recorded'))
        self.initial_report = deepcopy(self.report)

    def client(self, **kwargs):
        self.clients.append(kwargs)
        return self

    def connect(self, **kwargs):
        pass

    def supports_required_verification_commands(self):
        return True

    def session_create(self, path, **kwargs):
        self.created += 1
        self.create_config = kwargs
        if self.lose == 'create':
            raise util.KanpilotError('timeout', 'lost create response')
        if self.created > 1:
            self.saved_reports[self.session_id] = self.goal_report_get(self.session_id)
            self.session_id = 'supplement-' + str(self.created)
            self.current, self.running, self.approved = 'planning', False, False
            self.proposal = dict(self.proposal, sessionId=self.session_id)
            self.report = deepcopy(self.initial_report)
            self.report['report']['sessionId'] = self.session_id
        return dict(session=dict(id=self.session_id))

    def session_configure(self, sid, **kwargs):
        self.configure_config = kwargs

    def agent_prompt(self, sid, content, **kwargs):
        self.prompts += 1
        self.prompt = content
        if self.lose == 'prompt':
            raise util.KanpilotError('timeout', 'lost prompt response')
        return dict(turnId='planning')

    def agent_status(self, sid):
        return dict(currentTurnId=self.current, isRunning=self.running)

    def turn_get(self, sid, turn, **kwargs):
        return dict(sessionId=sid, turnId=turn, status='running' if self.running and turn != 'planning' else 'completed')

    def plans_pending(self, sid):
        return dict(plans=([] if self.approved else [deepcopy(self.proposal)]) + deepcopy(self.extra_pending),
            history=[dict(self.approved_proposal(self.history_version), **self.history_changes)] if self.approved else [])

    def input_pending(self, sid):
        return dict(sessionId=sid, asks=deepcopy(self.pending_asks))

    def approved_proposal(self, version):
        return dict(self.proposal, status='approved', action='approve', version=version,
            requiredVerificationCommands=self.required, targetPermissionMode='auto',
            executionId='execution-id', executionState='pending')

    def plans_resolve(self, proposal_id, sid, turn, tool, action, **kwargs):
        self.approvals += 1
        self.required = kwargs['required_verification_commands']
        self.approved = True
        self.current = 'execution'
        if self.lose == 'approval':
            raise util.KanpilotError('timeout', 'lost approval response')
        return dict(ok=True, action='approve', state='running', targetPermissionMode='auto',
            proposal=self.approved_proposal(2), execution=dict(id='execution-id', proposalId='proposal',
                sessionId=sid, requiredVerificationCommands=self.required,
                artifact=self.proposal.get('artifact'), targetPermissionMode='auto'))

    def goal_report_list(self, sid):
        return dict(reports=[dict(executionId='execution-id', proposalId='proposal', turnId='execution')])

    def goal_report_get(self, sid, **kwargs):
        if sid in self.saved_reports:
            return deepcopy(self.saved_reports[sid])
        result = deepcopy(self.report)
        if self.bad:
            self.bad(result)
        result['report_sha256'] = util.sha256_text(util.jdump(result['report']))
        return result


class ReleaseVerificationTest(TestCase):
    def setUp(self):
        self.manual = manual_fixtures.ReviewDeliveryTest('test_pass_enters_existing_pr_chain_preserving_every_execution_and_review_identity')
        self.manual.setUp()
        self.addCleanup(self.manual.doCleanups)
        self.fixture = self.manual.fixture
        self.patch(patch.dict('os.environ', {'KANPILOT_RELEASE_WORKTREE_ROOT':str(self.fixture.root / 'release-worktrees')}))
        self.conn, self.ctx = self.manual.conn, self.manual.ctx
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso, mode='pr', pr_number=8,
            pr_url='https://github.com/example/closeout/pull/8', merged_at=self.ctx.now_iso)
        d = core.delivery_of(self.task())
        pr = dict(number=8, state='MERGED', url=d['pr_url'], headRefOid=d['head_sha'],
            baseRefName=d['base_branch'], headRefName=d['branch'], isCrossRepository=False)
        self.patch(patch('kanpilot.parent_closeout_github.pr', return_value=pr))
        config = dict(connection_file=str(self.fixture.root / 'fake-plus.json'),
            permission_mode='auto', provider_id='isolated', model_id='isolated-model', thinking_level='high')
        self.conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id='parent'", (util.jdump(config),))
        extra = db.task_extra(self.task())
        directory = self.fixture.runs_root / 'parent' / 'run-parent'
        for name in ('job.json', 'spec.json'):
            path = directory / name
            content = util.jload(path.read_text())
            content['config'] = config
            path.write_text(util.jdump(content))
            extra['engine_verdict']['evidence_sha256'][str(path)] = util.sha256_file(path)
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, engine_verdict=extra['engine_verdict'])
        self.plus = FakePlus(self.fixture.parent_commands)
        self.patch(patch.object(verification, 'RuntimeClient', side_effect=self.plus.client))
        self.patch(patch.object(verification, 'HistoricalReadClient', return_value=self.plus))
        self.api = FakeReleaseAPI()
        self.patch(patch.object(release, 'github', self.api))

    def patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def task(self):
        return db.require_task(self.conn, 'parent')

    def prepare(self):
        return verification.cmd_prepare_release_verification(self.ctx, dict(task_id='parent'))

    def start(self):
        return verification.cmd_start_release_verification(self.ctx,
            dict(task_id='parent', expected_context_sha256=self.prepare()['context_sha256']))

    def sync(self, verification_id):
        return verification.cmd_sync_release_verification(self.ctx, dict(task_id='parent', verification_id=verification_id))

    def complete(self):
        start = self.start()
        result = self.sync(start['verification_id'])
        self.assertEqual(result['verification']['state'], 'verified', result)
        return start['verification_id']

    def partial_report(self):
        first = self.start()
        self.plus.report['report']['criteria'][0]['verdict'] = 'partial'
        result = self.sync(first['verification_id'])
        self.assertEqual(result['verification']['state'], 'blocked', result)
        value = verification._load(self.conn, 'parent', first['verification_id'])
        self.assertEqual(value['failure_code'], 'release_verification_report')
        report = self.plus.goal_report_get(value['session_id'])
        return value, dict(task_id='parent', expected_verification_id=value['verification_id'],
            expected_context_sha256=value['context']['context_sha256'],
            expected_report_sha256=report['report_sha256'])

    def restart(self, payload):
        return verification.cmd_restart_release_verification(self.ctx, payload)

    def test_complete_preserves_original_evidence_and_user_checkout_and_binds_release(self):
        task = dict(self.task()); extra = deepcopy(db.task_extra(self.task()))
        head = git(self.fixture.repo, 'rev-parse', 'HEAD')
        report = Path(task['review_report_path']).read_bytes()
        self.assertEqual(release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))['release']['state'], 'blocked')
        preview = self.prepare()
        self.assertEqual(self.plus.created, 0)
        self.assertEqual(preview['verification']['commands'], self.fixture.parent_commands)
        verification_id = self.complete()
        for key in ('run_id', 'plan_sha256', 'review_key', 'review_report_sha256', 'session_id'):
            self.assertEqual(self.task()[key], task[key])
        for key in ('engine_verdict', 'review_history', 'plan_verify', 'delivery'):
            self.assertEqual(db.task_extra(self.task())[key], extra[key])
        self.assertEqual(Path(task['review_report_path']).read_bytes(), report)
        self.assertEqual(git(self.fixture.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.fixture.repo, 'branch', '--show-current'), 'main')
        self.assertEqual(self.plus.required, self.fixture.parent_commands)
        self.assertEqual(self.plus.create_config['mode'], 'goal')
        self.assertEqual(self.plus.configure_config['permission_mode'], 'auto')
        for client in self.plus.clients:
            self.assertEqual(client['role'], 'execution')
            self.assertEqual(client['invocation_id'], 'releaseverify_' + verification_id)
            self.assertIsNone(client['task_id']); self.assertIsNone(client['run_id'])
            self.assertIsNone(client['request_id'])
            self.assertNotEqual(client['project_path'], str(self.fixture.repo))
        proof = verification.verified_proof(self.conn, self.task())
        self.assertEqual(proof['target_sha'], head)
        prepared = release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.assertEqual(prepared['release']['state'], 'ready', prepared)
        started = release.cmd_start_task_release(self.ctx, dict(task_id='parent', expected_context_sha256=prepared['context_sha256']))
        self.assertEqual(started['release']['state'], 'queued', started)
        self.assertEqual(self.api.manifest['target_sha'], head)

    def test_all_new_worktrees_use_unified_root_and_persisted_paths_survive_root_change(self):
        verification_id=self.complete()
        value=verification._load(self.conn,'parent',verification_id)
        expected=self.fixture.root / 'release-worktrees' / self.fixture.repo.name / ('release-verify-parent-'+verification_id)
        self.assertEqual(Path(value['worktree_path']),expected)
        with patch.dict('os.environ',{'KANPILOT_RELEASE_WORKTREE_ROOT':str(self.fixture.root / 'other-root')}):
            self.assertEqual(verification._worktree_path(self.conn,value),expected)
            self.assertEqual(verification.verified_proof(self.conn,self.task())['verification_id'],verification_id)
        legacy=dict(value,worktree_path=str(self.fixture.root / 'legacy-run-dir' / 'worktree'))
        self.assertEqual(verification._worktree_path(self.conn,legacy),Path(legacy['worktree_path']))

    def test_start_and_sync_are_idempotent_and_approved_commands_are_exact(self):
        verification_id = self.complete()
        self.assertEqual(self.start()['verification_id'], verification_id)
        self.assertEqual(self.sync(verification_id)['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_verified_proof_rejects_report_body_or_raw_hash_drift_without_mutating_accepted_audit(self):
        verification_id = self.complete()
        accepted = verification._load(self.conn, 'parent', verification_id)
        row = dict(db.get_runtime_operation(self.conn, verification._scope('parent'), verification._op(verification_id)))
        baseline = deepcopy(self.plus.report)
        for mutate in (
            lambda r: r['report'].update(summary='changed summary, same passing checks'),
            lambda r: r['report']['criteria'][0].update(text='changed criterion, still met'),
            lambda r: r.update(report_sha256='f'*64),
        ):
            with self.subTest(mutate=mutate):
                self.plus.report = deepcopy(baseline)
                mutate(self.plus.report)
                # The fake normally computes its hash, so raw-hash drift must
                # be applied after that read envelope has been constructed.
                payload = self.plus.goal_report_get(self.plus.session_id)
                if self.plus.report.get('report_sha256') == 'f'*64:
                    payload['report_sha256'] = 'f'*64
                with patch.object(self.plus, 'goal_report_get', return_value=payload):
                    with self.assertRaises(util.KanpilotError):
                        verification.verified_proof(self.conn, self.task())
                self.assertEqual(verification._load(self.conn, 'parent', verification_id), accepted)
                self.assertEqual(dict(db.get_runtime_operation(self.conn, verification._scope('parent'),
                    verification._op(verification_id))), row)

    def test_unknown_create_never_resends(self):
        self.plus.lose = 'create'
        first = self.start()
        self.assertEqual(first['verification']['state'], 'unknown')
        self.start(); self.sync(first['verification_id'])
        self.assertEqual((self.plus.created, self.plus.prompts), (1, 0))

    def test_unknown_prompt_never_resends(self):
        self.plus.lose = 'prompt'
        first = self.start()
        self.assertEqual(first['verification']['state'], 'unknown')
        self.start(); self.sync(first['verification_id'])
        self.assertEqual((self.plus.created, self.plus.prompts), (1, 1))

    def test_reservation_and_initial_context_are_atomic(self):
        with patch.object(verification, '_save', side_effect=RuntimeError('simulated crash before initial save')):
            with self.assertRaises(RuntimeError):
                self.start()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM runtime_operations WHERE tool=?',
            (verification.TOOL,)).fetchone()[0], 0)
        self.assertNotIn('release_verification_ref', db.task_extra(self.task()))
        self.assertEqual((self.plus.created, self.plus.prompts), (0, 0))
        self.assertEqual(self.start()['verification']['state'], 'planning')

    def test_crash_after_reservation_before_worktree_remains_readable_unknown(self):
        prepared = self.prepare()
        verification_id = prepared['context_sha256'][:32]
        with patch.object(verification, '_worktree', side_effect=SystemExit('crash after reservation')):
            with self.assertRaises(SystemExit):
                self.start()
        self.assertEqual(self.sync(verification_id)['verification']['state'], 'unknown')
        self.assertEqual(self.start()['verification']['state'], 'unknown')
        self.assertEqual((self.plus.created, self.plus.prompts), (0, 0))

    def test_transient_read_failures_before_approval_remain_observable_without_new_prompt(self):
        first = self.start()
        for code in ('timeout', 'remote_unavailable', 'runtime_unknown'):
            with self.subTest(code=code):
                with patch.object(verification, '_client', side_effect=util.KanpilotError(code, 'read unavailable')):
                    result = self.sync(first['verification_id'])
                self.assertEqual(result['verification']['state'], 'unknown', result)
                self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 0))
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_lost_approval_recovers_by_read_only_history_without_resend(self):
        self.plus.lose = 'approval'
        first = self.start()
        original = self.plus.plans_resolve
        def reserved_then_lost(*args, **kwargs):
            value = verification._load(self.conn, 'parent', first['verification_id'])
            scope_id, operation_id = verification._approval_operation(value)
            # A real RuntimeClient reserves durably before the Host write; the
            # fake must retain that unknown receipt when its response is lost.
            runtime_operations.reserve_downstream(self.conn, scope_id, operation_id,
                'pi_desktop', 'kp_runtime_proposal_resolve', {})
            return original(*args, **kwargs)
        self.plus.plans_resolve = reserved_then_lost
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'unknown')
        self.plus.lose = None
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'verified')
        self.assertEqual(self.plus.approvals, 1)

    def test_running_observation_then_completed_report(self):
        first = self.start()
        original = self.plus.plans_resolve
        def approve(*args, **kwargs):
            result = original(*args, **kwargs)
            self.plus.running = True
            return result
        self.plus.plans_resolve = approve
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'running')
        self.plus.running = False
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'verified')

    def test_missing_wrong_or_failed_report_never_proves_target(self):
        mutations = {
            'exit_nonzero': lambda r: r['report']['checks'][0].update(exitCode=1),
            'exit_bool': lambda r: r['report']['checks'][0].update(exitCode=False),
            'missing_command': lambda r: r['report'].update(checks=r['report']['checks'][1:]),
            'rewritten_command': lambda r: r['report']['checks'][0].update(command='flutter analyze '),
            'fallback': lambda r: r['report'].update(integrity=dict(kind='fallback')),
            'wrong_session': lambda r: r['report'].update(sessionId='other'),
            'wrong_proposal': lambda r: r['report'].update(proposalId='other'),
            'wrong_turn': lambda r: r['report'].update(turnId='other'),
            'not_met': lambda r: r['report'].update(verdict='unmet'),
            'step_failed': lambda r: r['report']['steps'][0].update(status='failed'),
            'later_exact_failure': lambda r: r['report']['checks'].extend([
                dict(r['report']['checks'][0], id='failed-retry', result='failed', exitCode=1),
                dict(r['report']['checks'][0], id='rewritten-pass', command=r['report']['checks'][0]['command'] + ' '),
            ]),
        }
        verification_id = self.complete()
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.plus.bad = mutate
                with self.assertRaises(util.KanpilotError):
                    verification.verified_proof(self.conn, self.task())
                result = release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
                self.assertEqual(result['release']['state'], 'blocked')
        self.plus.bad = None
        self.assertEqual(self.sync(verification_id)['verification']['state'], 'verified')

    def test_wrong_execution_identity_rejected_after_acceptance(self):
        self.complete()
        self.plus.bad = lambda r: r['report'].update(executionId='other')
        with self.assertRaises(util.KanpilotError):
            verification.verified_proof(self.conn, self.task())

    def test_source_and_index_drift_rejected_without_overwriting_accepted_audit(self):
        verification_id = self.complete()
        value = verification._load(self.conn, 'parent', verification_id)
        worktree = Path(value['worktree_path'])
        file = worktree / 'fix1.txt'; original = file.read_bytes()
        file.write_text('changed')
        with self.assertRaises(util.KanpilotError):
            verification.verified_proof(self.conn, self.task())
        self.assertEqual(self.sync(verification_id)['verification']['state'], 'blocked')
        self.assertEqual(verification._load(self.conn, 'parent', verification_id)['state'], 'verified')
        git(worktree, 'add', 'fix1.txt')
        file.write_bytes(original)
        with self.assertRaises(util.KanpilotError):
            verification.verified_proof(self.conn, self.task())

    def test_source_drift_before_approval_does_not_approve(self):
        first = self.start()
        value = verification._load(self.conn, 'parent', first['verification_id'])
        (Path(value['worktree_path']) / 'fix1.txt').write_text('changed')
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'blocked')
        self.assertEqual(self.plus.approvals, 0)

    def test_identity_drift_and_stale_prepare_rejected(self):
        prepared = self.prepare()
        self.fixture.tip = core.delivery_of(self.task())['head_sha']
        with self.assertRaises(util.KanpilotError):
            verification.cmd_start_release_verification(self.ctx,
                dict(task_id='parent', expected_context_sha256=prepared['context_sha256']))
        self.assertEqual(self.plus.created, 0)
        self.fixture.tip = git(self.fixture.repo, 'rev-parse', 'HEAD')
        self.complete()
        db.update_task(self.conn, 'parent', self.ctx.now_iso, review_key='different')
        with self.assertRaises(util.KanpilotError):
            verification.verified_proof(self.conn, self.task())

    def test_main_can_advance_while_release_keeps_verified_complete_checkpoint(self):
        self.complete()
        frozen = self.fixture.tip
        (self.fixture.repo / 'later.txt').write_text('later change')
        git(self.fixture.repo, 'add', '.')
        git(self.fixture.repo, 'commit', '-m', 'later independent change')
        self.fixture.tip = git(self.fixture.repo, 'rev-parse', 'HEAD')
        proof = verification.verified_proof(self.conn, self.task())
        self.assertEqual(proof['target_sha'], frozen)
        prepared = release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.assertEqual(prepared['release']['state'], 'ready', prepared)
        self.assertEqual(prepared['release']['target_sha'], frozen)

    def test_main_cannot_drop_the_verified_complete_checkpoint(self):
        self.complete()
        self.fixture.tip = core.delivery_of(self.task())['head_sha']
        prepared = release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.assertEqual(prepared['release']['state'], 'blocked', prepared)
        self.assertEqual(self.api.sent, [])

    def test_publish_rechecks_report_after_preview(self):
        self.complete()
        prepared = release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.plus.bad = lambda r: r['report']['checks'][0].update(exitCode=1)
        with self.assertRaises(util.KanpilotError):
            release.cmd_start_task_release(self.ctx, dict(task_id='parent', expected_context_sha256=prepared['context_sha256']))
        self.assertEqual(self.api.sent, [])

    def test_failed_first_report_blocks_and_does_not_restart_same_context(self):
        first = self.start()
        self.plus.bad = lambda r: r['report']['checks'][0].update(exitCode=1)
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'blocked')
        self.plus.bad = None
        self.assertEqual(self.start()['verification']['state'], 'blocked')
        with self.assertRaises(util.KanpilotError):
            verification.verified_proof(self.conn, self.task())
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_explicit_restart_preserves_old_report_and_freezes_original_identity(self):
        old, payload = self.partial_report()
        old_row = dict(db.get_runtime_operation(self.conn, verification._scope('parent'),
            verification._op(old['verification_id'])))
        old_report = deepcopy(self.plus.goal_report_get(old['session_id']))
        original = dict(self.task()); original_extra = deepcopy(db.task_extra(self.task()))
        result = self.restart(payload)
        new = verification._load(self.conn, 'parent', result['verification_id'])
        for key in ('verification_id', 'worktree_path', 'session_id'):
            self.assertNotEqual(new[key], old[key])
        self.assertNotEqual(new['context']['context_sha256'], old['context']['context_sha256'])
        self.assertEqual({key: value for key, value in new['context'].items()
            if key not in ('context_sha256', 'restart')},
            {key: value for key, value in old['context'].items() if key != 'context_sha256'})
        self.assertEqual(new['context']['restart']['report_sha256'], payload['expected_report_sha256'])
        self.assertEqual(dict(db.get_runtime_operation(self.conn, verification._scope('parent'),
            verification._op(old['verification_id']))), old_row)
        self.assertEqual(self.plus.goal_report_get(old['session_id']), old_report)
        for key in ('run_id', 'plan_sha256', 'review_key', 'review_report_sha256', 'session_id'):
            self.assertEqual(self.task()[key], original[key])
        for key in ('engine_verdict', 'review_history', 'plan_verify', 'delivery'):
            self.assertEqual(db.task_extra(self.task())[key], original_extra[key])
        self.assertIn('git status 全空', self.plus.prompt)
        self.assertIn('toolCallId', self.plus.prompt)
        self.assertIn('不是 Git 对象', self.plus.prompt)
        self.assertEqual(self.restart(payload), result)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (2, 2, 1))
        self.assertEqual(self.sync(new['verification_id'])['verification']['state'], 'verified')
        self.assertEqual(self.plus.required, old['context']['commands'])
        self.assertEqual(verification.verified_proof(self.conn, self.task())['target_sha'], old['context']['target_sha'])
        self.assertEqual(self.restart(payload)['verification']['state'], 'verified')
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (2, 2, 2))

    def test_restart_rejects_stale_report_context_and_other_current_attempt(self):
        old, payload = self.partial_report()
        for key in ('expected_report_sha256', 'expected_context_sha256', 'expected_verification_id'):
            with self.subTest(key=key), self.assertRaises(util.KanpilotError):
                self.restart(dict(payload, **{key: 'f' * len(payload[key])}))
        self.plus.report['report']['criteria'][0]['verdict'] = 'met'
        with self.assertRaises(util.KanpilotError):
            self.restart(payload)
        self.plus.report['report']['criteria'][0]['verdict'] = 'partial'
        db.set_extra(self.conn, self.task(), self.ctx.now_iso,
            release_verification_ref=dict(verification_id='a' * 32, context_sha256='a' * 64))
        with self.assertRaises(util.KanpilotError):
            self.restart(payload)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))

    def test_restart_requires_report_blocked_origin_and_fresh_completed_turn(self):
        old, payload = self.partial_report()
        for state, code in (('unknown', 'release_verification_report'), ('running', 'release_verification_report'),
                ('blocked', 'release_verification_source'), ('blocked', 'release_verification_checks')):
            with self.subTest(state=state, code=code):
                value = dict(old, state=state, failure_code=code)
                with db.write_txn(self.conn):
                    verification._save(self.conn, value, self.ctx.now_iso)
                with self.assertRaises(util.KanpilotError):
                    self.restart(payload)
        with db.write_txn(self.conn):
            verification._save(self.conn, old, self.ctx.now_iso)
        for running, current in ((True, 'execution'), (False, 'other')):
            with self.subTest(running=running, current=current):
                self.plus.running, self.plus.current = running, current
                with self.assertRaises(util.KanpilotError):
                    self.restart(payload)
        self.plus.running, self.plus.current = False, 'execution'
        with patch.object(self.plus, 'turn_get', return_value=dict(sessionId=old['session_id'],
                turnId='execution', status='failed')):
            with self.assertRaises(util.KanpilotError):
                self.restart(payload)
        self.assertEqual(self.plus.created, 1)

    def test_restart_rejects_pending_goals_asks_and_unknown_runtime_writes(self):
        old, payload = self.partial_report()
        for field, value in (('extra_pending', [dict(status='pending')]), ('pending_asks', [dict(id='ask')])):
            with self.subTest(field=field):
                setattr(self.plus, field, value)
                with self.assertRaises(util.KanpilotError):
                    self.restart(payload)
                setattr(self.plus, field, [])
        scope = verification._approval_operation(old)[0]
        for status in ('prepared', 'unknown'):
            with self.subTest(status=status):
                runtime_operations.begin_operation(self.conn, scope, 'pending-write', 'pi_desktop', 'unknown_write', {})
                self.conn.execute('UPDATE runtime_operations SET status=? WHERE scope_id=? AND operation_id=?',
                    (status, scope, 'pending-write'))
                with self.assertRaises(util.KanpilotError):
                    self.restart(payload)
                self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?', (scope, 'pending-write'))
        self.assertEqual(self.plus.created, 1)

    def test_restart_rechecks_source_report_identity_and_local_reservation(self):
        old, payload = self.partial_report()
        source = Path(old['worktree_path']) / 'fix1.txt'
        content = source.read_bytes(); source.write_text('changed')
        with self.assertRaises(util.KanpilotError):
            self.restart(payload)
        source.write_bytes(content)
        for change in (lambda r: r.update(state='pending'),
                lambda r: r['report'].update(sessionId='other'),
                lambda r: r['report']['execution'].update(status='running')):
            with self.subTest(change=change):
                self.plus.bad = change
                with self.assertRaises(util.KanpilotError):
                    self.restart(payload)
        self.plus.bad = None
        start = verification._start_context
        def change_before_reservation(ctx, task, context, **kwargs):
            changed = dict(old, failure_code='release_verification_source')
            with db.write_txn(self.conn):
                verification._save(self.conn, changed, self.ctx.now_iso)
            return start(ctx, task, context, **kwargs)
        with patch.object(verification, '_start_context', side_effect=change_before_reservation):
            with self.assertRaises(util.KanpilotError):
                self.restart(payload)
        self.assertEqual(self.plus.created, 1)

    def test_restart_lineage_authorization_cannot_be_missing_or_changed(self):
        old, payload = self.partial_report()
        result = self.restart(payload)
        new = verification._load(self.conn, 'parent', result['verification_id'])
        scope = verification._scope('parent'); key = verification._restart_operation(new['context']['restart'])
        authorization = dict(db.get_runtime_operation(self.conn, scope, key))
        for column, value in (('status', 'unknown'), ('request_sha256', 'b' * 64), ('result_json', '{}')):
            with self.subTest(column=column):
                self.conn.execute('UPDATE runtime_operations SET ' + column + '=? WHERE scope_id=? AND operation_id=?',
                    (value, scope, key))
                with self.assertRaises(util.KanpilotError):
                    verification._unchanged(self.conn, self.task(), new)
                self.conn.execute('UPDATE runtime_operations SET ' + column + '=? WHERE scope_id=? AND operation_id=?',
                    (authorization[column], scope, key))
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?', (scope, key))
        with self.assertRaises(util.KanpilotError):
            verification._unchanged(self.conn, self.task(), new)
        self.assertEqual(self.plus.created, 2)

    def test_new_attempt_partial_report_remains_blocked_without_promoting_original(self):
        old, payload = self.partial_report()
        new = self.restart(payload)
        self.plus.report['report']['criteria'][0]['verdict'] = 'partial'
        result = self.sync(new['verification_id'])
        self.assertEqual(result['verification']['state'], 'blocked')
        with self.assertRaises(util.KanpilotError):
            verification.verified_proof(self.conn, self.task())
        self.assertEqual(verification._load(self.conn, 'parent', old['verification_id']), old)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (2, 2, 2))

    def test_unapproved_or_wrong_proposal_never_completes(self):
        first = self.start()
        self.plus.proposal['sessionId'] = 'other'
        self.assertEqual(self.sync(first['verification_id'])['verification']['state'], 'blocked')
        self.assertEqual(self.plus.approvals, 0)

    def test_cli_fixed_payload_round_trip(self):
        preview = cli.run('prepare-release-verification', dict(task_id='parent'), self.fixture.db_path)
        first = cli.run('start-release-verification', dict(task_id='parent',
            expected_context_sha256=preview['context_sha256']), self.fixture.db_path)
        result = cli.run('sync-release-verification', dict(task_id='parent',
            verification_id=first['verification_id']), self.fixture.db_path)
        self.assertEqual(result['verification']['state'], 'verified', result)
        self.assertEqual((self.plus.created, self.plus.prompts, self.plus.approvals), (1, 1, 1))
