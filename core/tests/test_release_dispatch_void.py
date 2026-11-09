"""Explicit void for an unclaimable prerelease dispatch. All release APIs fake."""
import json
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import test_parent_closeout as closeout_fixtures
from test_release_delivery import FakeReleaseAPI
from kanpilot import db, util, project_config, release_delivery as delivery
from kanpilot import release_dispatch_recovery as recovery, release_dispatch_void as void
from kanpilot import runtime_operations

PROJECT = 'project'
SCOPE = 'release-project:' + PROJECT


class VoidAPI(FakeReleaseAPI):
    """Adds the ref head knob and a dispatch that never reaches GitHub."""

    def __init__(self):
        super().__init__()
        self.workflow_head = 'c' * 40
        self.silent_drop = False

    def workflow_snapshot(self, repo, path, ref):
        value = super().workflow_snapshot(repo, path, ref)
        value['head_sha'] = self.workflow_head
        return value

    def dispatch(self, repo, workflow, ref, inputs):
        if not self.silent_drop:
            return super().dispatch(repo, workflow, ref, inputs)
        self.sent.append(deepcopy(inputs))
        self.input = inputs
        self.manifest = json.loads(inputs['task_manifest_json'])
        self.runs = []
        raise RuntimeError('dispatch never reached GitHub')


class ReleaseDispatchVoidTest(TestCase):
    def setUp(self):
        self.fixture = closeout_fixtures.ParentCloseoutTest(
            'test_success_reuses_final_checkpoint_without_worktree_or_new_model')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.conn
        self.ctx = self.fixture.ctx
        self.api = VoidAPI()
        self.policy = dict(enabled=True, channel='prerelease', workflow_path='.github/workflows/pilot-release.yml',
                           workflow_ref='main', version_base='0.1.0', required_assets=['app.apk'],
                           required_jobs=['Build'])
        self.configure()
        patcher = patch.object(delivery, 'github', self.api, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self):
        db.set_setting(self.conn, project_config.SETTINGS_KEY, dict(version=1, revision=1, items={
            PROJECT: dict(repository=str(self.fixture.repo), automation=dict(policy_revision=1, release=self.policy))}))

    def row(self, name='parent'):
        return db.require_task(self.conn, name)

    def start(self, task_id='parent'):
        preview = delivery.cmd_prepare_task_release(self.ctx, dict(task_id=task_id))
        return delivery.cmd_start_task_release(
            self.ctx, dict(task_id=task_id, expected_context_sha256=preview['context_sha256']))

    def unclaimed_batch(self):
        """A batch whose dispatch intent was written but never produced a run."""
        self.api.silent_drop = True
        try:
            result = self.start()
        finally:
            self.api.silent_drop = False
        self.assertEqual(result['release']['state'], 'unknown', result)
        return result['release']

    def dispatch_request(self, batch_id):
        row = db.get_runtime_operation(self.conn, SCOPE, 'dispatch:' + batch_id)
        return row['request_sha256']

    def payload(self, batch, **overrides):
        value = dict(task_id='parent', batch_id=batch['batch_id'],
                     expected_context_sha256=batch['context_sha256'],
                     expected_dispatch_request_sha256=self.dispatch_request(batch['batch_id']),
                     confirm=True)
        value.update(overrides)
        return value

    def moved_ref(self):
        self.api.workflow_head = 'd' * 40

    def void(self, batch, **overrides):
        return void.cmd_void_stale_release_dispatch(self.ctx, self.payload(batch, **overrides))

    def test_void_requires_a_ref_that_moved_past_the_frozen_head(self):
        batch = self.unclaimed_batch()
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch)
        self.assertEqual(caught.exception.code, 'release_void_not_stale')
        self.assertEqual(len(delivery._pending(self.conn, PROJECT)), 1)

    def test_void_refuses_when_a_batch_run_was_observed(self):
        self.api.lose_reply = True
        result = self.start()
        self.api.lose_reply = False
        batch = result['release']
        self.assertEqual(batch['state'], 'unknown')
        self.moved_ref()
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch)
        self.assertEqual(caught.exception.code, 'release_run_observed')

    def test_void_requires_explicit_confirmation_and_frozen_identity(self):
        batch = self.unclaimed_batch()
        self.moved_ref()
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch, confirm=False)
        self.assertEqual(caught.exception.code, 'release_void_confirmation')
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch, expected_dispatch_request_sha256='0' * 64)
        self.assertEqual(caught.exception.code, 'release_void_identity')
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch, expected_context_sha256='0' * 64)
        self.assertEqual(caught.exception.code, 'release_context_changed')

    def test_void_refuses_another_unknown_dispatch_in_the_project(self):
        batch = self.unclaimed_batch()
        self.moved_ref()
        with db.write_txn(self.conn):
            runtime_operations.begin_operation(self.conn, SCOPE, 'dispatch:other', 'github',
                                               delivery.DISPATCH_TOOL, dict(batch_id='other'), now=self.ctx.now_iso)
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch)
        self.assertEqual(caught.exception.code, 'release_dispatch_unknown')

    def test_void_refuses_when_a_recovery_receipt_exists(self):
        batch = self.unclaimed_batch()
        self.moved_ref()
        with db.write_txn(self.conn):
            runtime_operations.begin_operation(self.conn, SCOPE, recovery.operation_id(batch), 'github',
                                               recovery.TOOL, dict(batch_id=batch['batch_id']), now=self.ctx.now_iso)
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(batch)
        self.assertEqual(caught.exception.code, 'release_void_forbidden')

    def test_void_refuses_a_stable_batch(self):
        batch = self.unclaimed_batch()
        self.moved_ref()
        raw = delivery._batch(self.conn, batch['batch_id'])
        patched = dict(raw, policy=dict(raw['policy'], channel='stable'))
        with db.write_txn(self.conn):
            db.update_runtime_operation(self.conn, SCOPE, 'batch:' + batch['batch_id'], self.ctx.now_iso,
                                        result_json=util.jdump(patched))
        with self.assertRaises(delivery.ReleaseBlocked) as caught:
            self.void(patched)
        self.assertEqual(caught.exception.code, 'release_void_forbidden')

    def test_void_abandons_the_batch_and_lets_members_prepare_again(self):
        batch = self.unclaimed_batch()
        self.moved_ref()
        result = self.void(batch)
        self.assertEqual(result['outcome'], 'voided')
        self.assertTrue(result['release']['voided'])
        self.assertEqual(result['release']['batch_id'], batch['batch_id'])
        self.assertIsNone(delivery._attached(self.conn, self.row()))
        self.assertNotIn('release_ref', db.task_extra(self.row()))
        self.assertEqual(delivery._pending(self.conn, PROJECT), [])
        original = db.get_runtime_operation(self.conn, SCOPE, 'dispatch:' + batch['batch_id'])
        self.assertEqual(original['status'], 'rejected')
        stored = json.loads(original['result_json'])
        self.assertEqual(stored['void_operation_id'], 'dispatch-void:' + batch['batch_id'])
        audit = db.get_runtime_operation(self.conn, SCOPE, 'dispatch-void:' + batch['batch_id'])
        self.assertEqual(audit['status'], 'accepted')
        recorded = json.loads(audit['result_json'])
        self.assertEqual(recorded['frozen_workflow']['head_sha'], 'c' * 40)
        self.assertEqual(recorded['current_workflow']['head_sha'], 'd' * 40)
        self.assertEqual(recorded['marker_runs'], 0)
        preview = delivery.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.assertTrue(preview['release']['can_start'], preview)
        again = delivery.cmd_start_task_release(
            self.ctx, dict(task_id='parent', expected_context_sha256=preview['context_sha256']))
        self.assertNotEqual(again['release']['batch_id'], batch['batch_id'])
        self.assertEqual(len(self.api.sent), 2)
        self.assertEqual(delivery._pending(self.conn, PROJECT)[0]['operation_id'],
                         'dispatch:' + again['release']['batch_id'])

    def test_void_replay_is_idempotent(self):
        batch = self.unclaimed_batch()
        self.moved_ref()
        first = self.void(batch)
        self.api.workflow_head = 'e' * 40
        second = self.void(batch)
        self.assertEqual(first['release']['batch_id'], second['release']['batch_id'])
        self.assertEqual(first['release']['void_operation_id'], second['release']['void_operation_id'])
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(delivery._pending(self.conn, PROJECT), [])
