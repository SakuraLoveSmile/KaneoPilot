"""Do not advertise an override that a live core verdict will never honor."""
from helpers import KanpilotTestCase
from kanpilot import db, engine_claim_scope, util


class TestLiveManualVerification(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        info = self.make_ready_task(task_id='manualverificationgate1')
        self.task_id = info['remote']['id']
        self.project_id = info['remote']['projectId']
        self.set_task_status(self.task_id, 'needs_user', failure_code='verification_unconfirmed',
                             failure_stage='monitor', run_id='frozen-run', send_state='sent', sent=1)

    def view(self):
        return self.call('get-task', {'task_id': self.task_id})['task']

    def mode(self, value):
        self.call('set-project-engine-mode', {'project_id': self.project_id, 'engine_mode': value})

    def test_live_hides_and_rejects_without_state_or_event_changes(self):
        self.mode('live')
        before = self.view()
        self.assertNotIn('accept_verification', before['allowed_actions'])
        conn = db.connect(self.db_path)
        try:
            count = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            error = self.call_err('resolve', {'task_id': self.task_id, 'action': 'accept_verification'})
            self.assertEqual(error.code, 'conflict')
            self.assertIn('Live', error.message)
            after = self.view()
            self.assertEqual(after['status'], before['status'])
            self.assertEqual(after['extra'], before['extra'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], count)
            self.assertIsNone(after['active_request'])
        finally:
            conn.close()

    def test_legacy_manual_acceptance_is_preserved(self):
        self.mode('legacy')
        self.assertIn('accept_verification', self.view()['allowed_actions'])
        self.call('resolve', {'task_id': self.task_id, 'action': 'accept_verification'})
        self.assertEqual(self.view()['status'], 'running')
        self.assertEqual(self.view()['extra']['verification_override']['execution_run_id'], 'frozen-run')

    def test_shadow_manual_acceptance_is_preserved(self):
        self.mode('shadow')
        self.assertIn('accept_verification', self.view()['allowed_actions'])
        self.call('resolve', {'task_id': self.task_id, 'action': 'accept_verification'})
        self.assertEqual(self.view()['status'], 'running')

    def test_forced_live_uses_the_same_effective_policy(self):
        self.mode('legacy')
        with engine_claim_scope.command_mode('live', True):
            self.assertNotIn('accept_verification', self.view()['allowed_actions'])
            self.assertEqual(self.call_err('resolve', {'task_id': self.task_id, 'action': 'accept_verification'}).code, 'conflict')
