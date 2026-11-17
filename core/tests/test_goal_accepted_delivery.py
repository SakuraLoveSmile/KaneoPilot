"""Goal acceptance admission at delivery boundaries; all Git/SQLite/APIs are fixtures."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import test_parent_closeout as parent_fixtures
import test_review_delivery as review_fixtures
from test_release_delivery import FakeReleaseAPI
from kanpilot import (core, db, goal_blocker, parent_closeout, project_config,
                      release_delivery, review_delivery, util)
from kanpilot.engine import verdict


class AcceptedReviewDeliveryTest(TestCase):
    def setUp(self):
        self.fixture = review_fixtures.ReviewDeliveryTest(
            'test_pass_enters_existing_pr_chain_preserving_every_execution_and_review_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn, self.ctx = self.fixture.conn, self.fixture.ctx
        self.hashes = {}
        self.evidence = parent_closeout._task_evidence(self.conn, self.task(), self.hashes)
        self.evidence.update(user_accepted=True, exact_coverage=False, verification_ok=False,
            acceptance_sha256='accepted-result', acceptance_snapshot=dict(
                limitations=['Device criterion not verified'], verification_ok=False))

    def task(self):
        return self.fixture.task()

    def accept(self):
        receipt = deepcopy(db.task_extra(self.task())['engine_verdict'])
        receipt.update(verification_ok=False, review_authorized=True, admission_source='user_acceptance')
        saved = dict(acceptance_sha256='accepted-result', verification_ok=False,
            evidence_sha256=self.hashes, index_source_sha256=self.evidence['source_sha256'],
            report_sha256=self.evidence['report_sha256'], report_id=self.evidence.get('report_id'),
            accepted_at=self.ctx.now_iso, actor='user',
            blocked_snapshot=dict(limitations=['Device criterion not verified']),
            accepted_report=deepcopy(self.fixture.fixture.goals['parent']['report']))
        saved['accepted_report']['verdict'] = 'partial'
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, engine_verdict=receipt, goal_acceptance=saved)
        self.fixture.fixture.goals['parent']['verdict'] = 'partial'
        self.fixture.fixture.goals['parent']['report']['verdict'] = 'partial'
        # Pi/source admission is covered by its owner tests; exercise the real
        # delivery helper, independent Review and file identities here.
        p = patch.object(goal_blocker, 'review_admission_current', return_value=True)
        self.accepted = p.start(); self.addCleanup(p.stop)
        p = patch.object(verdict, 'valid_committed_receipt', return_value=True)
        p.start(); self.addCleanup(p.stop)

    def test_current_user_acceptance_and_independent_pass_continue_without_claiming_verification(self):
        self.accept()
        result = self.fixture.resolve()
        self.assertEqual(result['task_status'], 'in_review')
        self.assertEqual(result['request']['kind'], 'delivery')
        extra = db.task_extra(self.task())
        self.assertFalse(extra['engine_verdict']['verification_ok'])
        self.assertEqual(extra['manual_review_delivery']['goal_acceptance_sha256'], 'accepted-result')
        self.assertFalse(self.evidence['exact_coverage'])
        self.assertIsNotNone(review_delivery.continuation(self.task()))
        db.set_extra(self.conn, self.task(), self.ctx.now_iso,
            goal_acceptance=dict(acceptance_sha256='changed-result', verification_ok=False))
        self.assertIsNone(review_delivery.continuation(self.task()))

    def test_acceptance_cannot_replace_an_independent_review_pass(self):
        self.accept()
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, review_conclusion='changes_requested')
        with self.assertRaises(util.KanpilotError):
            self.fixture.resolve()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='delivery'").fetchone()[0], 0)
        self.accepted.assert_not_called()

    def test_non_met_without_current_acceptance_keeps_the_strict_gate(self):
        self.fixture.fixture.goals['parent']['verdict'] = 'partial'
        self.fixture.fixture.goals['parent']['report']['verdict'] = 'partial'
        with self.assertRaises(util.KanpilotError):
            self.fixture.resolve()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='delivery'").fetchone()[0], 0)

    def merged(self):
        delivery = core.delivery_of(self.task())
        delivery.update(mode='pr', pr_number=8, pr_url='https://github.com/example/closeout/pull/8',
                        pr_state='MERGED', merged_at=self.ctx.now_iso)
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso, **delivery)
        self.fixture.fixture.tip = delivery['head_sha']
        pr = dict(number=8, state='MERGED', url=delivery['pr_url'], headRefOid=delivery['head_sha'],
                  headRefName=delivery['branch'], baseRefName=delivery['base_branch'], isCrossRepository=False)
        p = patch.object(parent_closeout.github, 'pr', return_value=pr)
        p.start(); self.addCleanup(p.stop)
        return delivery

    def test_accepted_result_remains_unverified_in_full_release_proof_and_lifecycle(self):
        self.accept()
        self.merged()
        api = FakeReleaseAPI()
        policy = dict(enabled=True, channel='prerelease', workflow_path='.github/workflows/pilot-release.yml',
            workflow_ref='main', version_base='0.1.0', required_assets=['app.apk'], required_jobs=['Build'])
        db.set_setting(self.conn, project_config.SETTINGS_KEY, dict(version=1, revision=1, items={
            'project':dict(enabled=True, repository=str(self.fixture.fixture.repo),
                           automation=dict(policy_revision=1, release=policy))}))
        with patch.object(release_delivery, 'github', api):
            prepared = release_delivery.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
            self.assertIsNotNone(prepared['context_sha256'], prepared)
            result = release_delivery.cmd_start_task_release(self.ctx, dict(task_id='parent',
                expected_context_sha256=prepared['context_sha256']))
            self.assertEqual(result['release']['state'], 'queued', result)
            self.assertEqual(len(api.sent), 1)
            batch_id = result['release']['batch_id']
            batch = release_delivery._batch(self.conn, batch_id)
            evidence = batch['proof']['reused_evidence'][0]
            self.assertFalse(evidence['verification_ok'])
            self.assertFalse(evidence['exact_coverage'])
            self.assertEqual(evidence['acceptance_snapshot']['blocked_snapshot']['limitations'], ['Device criterion not verified'])
            self.assertIsNotNone(release_delivery.completion_gate(self.conn, self.task()))
            api.published = True
            published = release_delivery.cmd_sync_release_batch(self.ctx, dict(batch_id=batch_id))
            self.assertEqual(published['release']['state'], 'published', published)
            self.assertIsNone(release_delivery.completion_gate(self.conn, self.task()))
            self.assertFalse(db.task_extra(self.task())['engine_verdict']['verification_ok'])

    def test_failed_review_or_stale_acceptance_blocks_release_before_dispatch(self):
        self.accept()
        self.merged()
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, review_conclusion='changes_requested')
        with self.assertRaises(util.KanpilotError):
            release_delivery._single_proof(self.conn, self.task())


class AcceptedParentCheckpointTest(TestCase):
    def setUp(self):
        self.fixture = parent_fixtures.ParentCloseoutTest(
            'test_success_reuses_final_checkpoint_without_worktree_or_new_model')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.conn, self.ctx = self.fixture.conn, self.fixture.ctx
        hashes = {}
        self.accepted = parent_closeout._task_evidence(self.conn, self.fixture.row('fix2'), hashes)
        self.accepted.update(user_accepted=True, exact_coverage=False, verification_ok=False,
                             acceptance_sha256='fix2-acceptance')
        self.hashes = hashes
        self.original_helper = goal_blocker.accepted_delivery_evidence
        self.admission_calls = []
        db.set_extra(self.conn, self.fixture.row('fix2'), self.ctx.now_iso,
            goal_acceptance=dict(acceptance_sha256='fix2-acceptance', verification_ok=False))
        def accepted(conn, task, hashes, **kwargs):
            self.admission_calls.append((task['task_id'], kwargs.get('require_review_pass', True)))
            if task['task_id'] != 'fix2':
                return self.original_helper(conn, task, hashes, **kwargs)
            hashes.update(self.hashes)
            return deepcopy(self.accepted)
        p = patch.object(goal_blocker, 'accepted_delivery_evidence', side_effect=accepted, create=True)
        p.start(); self.addCleanup(p.stop)

    def test_parent_uses_each_tasks_own_admission_and_preserves_unverified_checkpoint(self):
        result = self.fixture.run_closeout()
        self.assertEqual(result['outcome'], 'completed', result)
        receipt = db.task_extra(self.fixture.row('parent'))['parent_closeout']
        checkpoint = next(row for row in receipt['reused_evidence'] if row['task_id'] == 'fix2')
        self.assertFalse(checkpoint['verification_ok'])
        self.assertFalse(checkpoint['exact_coverage'])
        self.assertEqual(receipt['all_task_coverage']['fix2'], [dict(basis='user_accepted',
            acceptance_sha256='fix2-acceptance', verification_ok=False)])
        self.assertTrue(all(row['basis'] != 'user_accepted' for row in receipt['coverage_mapping']))

    def test_accepted_child_cannot_replace_unaccepted_parent_verification(self):
        self.accepted['checks'] = [row for row in self.accepted['checks'] if row.get('command') != 'flutter analyze']
        result = self.fixture.run_closeout()
        self.assertEqual(result['outcome'], 'blocked', result)
        self.assertTrue(any(row['code'] == 'integrated_checkpoint_missing'
                            for row in result['parent_closeout']['blockers']))
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.fixture.fake.requests_log))

    def accept_parent(self):
        hashes = {}
        parent = self.fixture.row('parent')
        evidence = parent_closeout._task_evidence(self.conn, parent, hashes)
        saved = dict(acceptance_sha256='parent-acceptance', verification_ok=False,
            evidence_sha256=hashes, index_source_sha256=evidence['source_sha256'],
            report_sha256=evidence['report_sha256'], report_id=evidence.get('report_id'),
            actor='user', accepted_at=self.ctx.now_iso,
            blocked_snapshot=dict(limitations=['Original device evidence remains unverified']),
            accepted_report=deepcopy(self.fixture.goals['parent']['report']))
        saved['accepted_report']['verdict'] = 'partial'
        receipt = deepcopy(db.task_extra(parent)['engine_verdict'])
        receipt.update(verification_ok=False, admission_source='user_acceptance', review_authorized=True)
        db.set_extra(self.conn, parent, self.ctx.now_iso, engine_verdict=receipt, goal_acceptance=saved)
        p = patch.object(goal_blocker, 'review_admission_current', return_value=True)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(verdict, 'valid_committed_receipt', return_value=True)
        p.start(); self.addCleanup(p.stop)

    def test_accepted_parent_can_close_only_after_the_independent_repair_chain_passes(self):
        self.accept_parent()
        result = self.fixture.run_closeout()
        self.assertEqual(result['outcome'], 'completed', result)
        receipt = db.task_extra(self.fixture.row('parent'))['parent_closeout']
        self.assertEqual(db.task_extra(self.fixture.row('parent'))['review_conclusion'], 'changes_requested')
        self.assertFalse(receipt['original_evidence']['verification_ok'])
        self.assertIn(('parent', False), self.admission_calls)
        self.assertIn(('fix2', True), self.admission_calls)
        self.assertNotIn(('fix2', False), self.admission_calls)
        self.assertEqual(receipt['all_task_coverage']['parent'], [dict(basis='user_accepted',
            acceptance_sha256='parent-acceptance', verification_ok=False)])

    def test_accepted_parent_cannot_override_a_child_review_failure(self):
        self.accept_parent()
        child = self.fixture.row('fix1')
        db.set_extra(self.conn, child, self.ctx.now_iso, review_conclusion='changes_requested')
        result = self.fixture.run_closeout()
        self.assertEqual(result['outcome'], 'blocked', result)
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.fixture.fake.requests_log))
