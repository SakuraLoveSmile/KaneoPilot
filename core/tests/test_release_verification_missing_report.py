"""Missing runtime report resolution: isolated Git/SQLite, fake Plus only."""
from unittest import TestCase

import test_release_verification as fixtures
from kanpilot import db, release_verification as verification, util


class MissingReportTest(TestCase):
    def setUp(self):
        self.case = fixtures.ReleaseVerificationTest('test_cli_fixed_payload_round_trip')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx, self.plus = self.case.conn, self.case.ctx, self.case.plus

    def task(self):
        return db.require_task(self.conn, 'parent')

    def draft_report(self):
        """Plus ends the execution turn with a draft (never-ready) report."""
        self.plus.goal_report_get = lambda sid, **kwargs: dict(self.plus.report, state='draft')
        start = self.case.start()
        result = self.case.sync(start['verification_id'])
        self.assertEqual(result['verification']['state'], 'unknown', result)
        value = verification._load(self.conn, 'parent', start['verification_id'])
        self.assertEqual(value['failure_code'], 'release_verification_report_pending')
        return value

    def payload(self, value, **overrides):
        request = dict(task_id='parent', verification_id=value['verification_id'],
                       expected_context_sha256=value['context']['context_sha256'], confirm=True)
        request.update(overrides)
        return request

    def resolve(self, value, **overrides):
        return verification.cmd_resolve_missing_release_verification(self.ctx, self.payload(value, **overrides))

    def test_resolution_enables_explicit_restart_without_a_report(self):
        value = self.draft_report()
        self.assertFalse(verification.attempt_summary(self.conn, self.task())['can_restart_verification'])
        result = self.resolve(value)
        self.assertEqual(result['verification']['state'], 'blocked')
        resolved = verification._load(self.conn, 'parent', value['verification_id'])
        self.assertTrue(resolved['report_missing'])
        self.assertEqual(resolved['failure_code'], 'release_verification_report')
        self.assertRegex(resolved['report_sha256'], '^[0-9a-f]{64}$')
        audit = db.get_runtime_operation(self.conn, verification._scope('parent'),
                                         verification._resolution_operation(value['verification_id']))
        self.assertEqual(audit['status'], 'accepted')
        self.assertEqual(audit['tool'], verification.RESOLUTION_TOOL)
        self.assertTrue(util.jload(audit['result_json'])['runtime_report']['found'])
        self.assertTrue(verification.attempt_summary(self.conn, self.task())['can_restart_verification'])
        prepared = verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertEqual(prepared['expected_verification_id'], value['verification_id'])
        self.assertEqual(prepared['expected_report_sha256'], resolved['report_sha256'])
        restarted = verification.cmd_restart_release_verification(self.ctx, dict(
            task_id='parent', expected_verification_id=prepared['expected_verification_id'],
            expected_context_sha256=prepared['expected_context_sha256'],
            expected_report_sha256=prepared['expected_report_sha256']))
        self.assertNotEqual(restarted['verification_id'], value['verification_id'])
        self.assertEqual(self.plus.created, 2)
        self.assertEqual(verification._load(self.conn, 'parent', value['verification_id'])['state'], 'blocked')

    def test_resolution_requires_confirmation_and_a_pending_report(self):
        value = self.draft_report()
        with self.assertRaises(util.KanpilotError) as caught:
            self.resolve(value, confirm=False)
        self.assertEqual(caught.exception.code, 'release_verification_resolution')
        self.assertIsNone(db.get_runtime_operation(self.conn, verification._scope('parent'),
                                                   verification._resolution_operation(value['verification_id'])))
        with self.assertRaises(util.KanpilotError) as caught:
            self.resolve(value, verification_id='0' * 32)
        self.assertEqual(caught.exception.code, 'release_verification_resolution')
        with self.assertRaises(util.KanpilotError) as caught:
            self.resolve(value, expected_context_sha256='0' * 64)
        self.assertEqual(caught.exception.code, 'release_verification_resolution')

    def test_resolution_refuses_when_the_report_became_readable(self):
        value = self.draft_report()
        self.plus.goal_report_get = lambda sid, **kwargs: dict(self.plus.report)
        with self.assertRaises(util.KanpilotError) as caught:
            self.resolve(value)
        self.assertEqual(caught.exception.code, 'release_verification_resolution')
        self.assertIn('正式报告', caught.exception.message)
        self.assertFalse(verification._load(self.conn, 'parent', value['verification_id']).get('report_missing'))

    def test_resolution_is_idempotent_and_keeps_the_audit(self):
        value = self.draft_report()
        first = self.resolve(value)
        second = self.resolve(value)
        self.assertEqual(first['verification']['state'], second['verification']['state'])
        rows = self.conn.execute('SELECT operation_id FROM runtime_operations WHERE operation_id=?',
                                 (verification._resolution_operation(value['verification_id']),)).fetchall()
        self.assertEqual(len(rows), 1)
        with self.assertRaises(util.KanpilotError) as caught:
            self.resolve(value, expected_context_sha256='0' * 64)
        self.assertEqual(caught.exception.code, 'release_verification_resolution')
