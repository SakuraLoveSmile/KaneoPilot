"""Re-accept a source-drift block once the run finished and the tree is clean."""
import json
from unittest import TestCase

import test_release_verification as fixtures
from kanpilot import db, release_verification as verification, util


class RevalidateTest(TestCase):
    def setUp(self):
        self.case = fixtures.ReleaseVerificationTest('test_cli_fixed_payload_round_trip')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx, self.plus = self.case.conn, self.case.ctx, self.case.plus

    def blocked_by_source_drift(self):
        """A finished, passing run that a raced sync recorded as a source drift."""
        verification_id = self.case.complete()
        value = verification._load(self.conn, 'parent', verification_id)
        drifted = dict(value, state='blocked', failure_code='release_verification_source',
                       reason='独立工作树源码或索引与冻结目标不一致')
        drifted.pop('report_sha256', None)
        with db.write_txn(self.conn):
            db.update_runtime_operation(self.conn, verification._scope('parent'),
                                        verification._op(verification_id), self.ctx.now_iso,
                                        status='rejected', result_json=util.jdump(drifted))
        return verification._load(self.conn, 'parent', verification_id)

    def payload(self, value, **overrides):
        request = dict(task_id='parent', verification_id=value['verification_id'],
                       expected_context_sha256=value['context']['context_sha256'], confirm=True)
        request.update(overrides)
        return request

    def test_revalidation_accepts_a_finished_clean_run(self):
        value = self.blocked_by_source_drift()
        self.assertFalse(verification.attempt_summary(self.conn, self.case.task())['can_sync_verification'])
        result = verification.cmd_revalidate_release_verification(self.ctx, self.payload(value))
        self.assertEqual(result['verification']['state'], 'verified', result)
        resolved = verification._load(self.conn, 'parent', value['verification_id'])
        self.assertEqual(resolved['state'], 'verified')
        self.assertNotIn('failure_code', resolved)
        self.assertEqual(resolved['revalidated_from']['failure_code'], 'release_verification_source')
        self.assertRegex(resolved['report_sha256'], '^[0-9a-f]{64}$')
        audit = db.get_runtime_operation(self.conn, verification._scope('parent'),
                                         verification._revalidation_operation(value['verification_id']))
        self.assertEqual(audit['status'], 'accepted')
        self.assertEqual(audit['tool'], verification.REVALIDATION_TOOL)
        self.assertTrue(verification._load(self.conn, 'parent', value['verification_id'])['state'] == 'verified')
        self.assertTrue(verification.attempt_summary(self.conn, self.case.task())['can_prepare_verification'] is False)

    def test_revalidation_requires_confirmation_and_a_source_block(self):
        value = self.blocked_by_source_drift()
        with self.assertRaises(util.KanpilotError) as caught:
            verification.cmd_revalidate_release_verification(self.ctx, self.payload(value, confirm=False))
        self.assertEqual(caught.exception.code, 'release_verification_revalidation')
        self.assertIsNone(db.get_runtime_operation(self.conn, verification._scope('parent'),
                                                   verification._revalidation_operation(value['verification_id'])))
        with self.assertRaises(util.KanpilotError) as caught:
            verification.cmd_revalidate_release_verification(
                self.ctx, self.payload(value, expected_context_sha256='0' * 64))
        self.assertEqual(caught.exception.code, 'release_verification_revalidation')

    def test_revalidation_is_idempotent(self):
        value = self.blocked_by_source_drift()
        first = verification.cmd_revalidate_release_verification(self.ctx, self.payload(value))
        second = verification.cmd_revalidate_release_verification(self.ctx, self.payload(value))
        self.assertEqual(first['verification']['state'], second['verification']['state'])
        rows = self.conn.execute('SELECT operation_id FROM runtime_operations WHERE operation_id=?',
                                 (verification._revalidation_operation(value['verification_id']),)).fetchall()
        self.assertEqual(len(rows), 1)
