"""Mixed project queues stay scoped at the shared claim boundary."""
import os
from unittest import mock
from helpers import KanpilotTestCase
from kanpilot import cmd_exec, db, engine_claim_scope, process_owner, util
from kanpilot.engine import health, loop, runners


class TestEngineClaimScope(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.instance = 'scope-engine'
        conn = db.connect(self.db_path)
        try:
            health.acquire_or_renew_lease(conn, util.now_iso(), instance_id=self.instance,
                stage=None, pid=os.getpid(), lease_seconds=1200)
        finally:
            conn.close()
        patch = mock.patch.dict(os.environ, {
            engine_claim_scope.WORKER_ENV: '1', engine_claim_scope.MODE_ENV: 'legacy',
            engine_claim_scope.FORCE_ENV: '0', process_owner.INSTANCE_ENV: self.instance})
        patch.start()
        self.addCleanup(patch.stop)

    def _requests(self, conn, stage):
        result = []
        for index, mode in enumerate(('legacy', 'shadow', 'live')):
            info = self.make_ready_task(task_id='enginescope%s%s' % (stage, index))
            task_id = info['remote']['id']
            task = db.require_task(conn, task_id)
            db.set_extra(conn, task, util.now_iso(), plan_execution_hold=False)
            project_id = 'scope-project-' + str(index)
            conn.execute('UPDATE tasks SET project_id=? WHERE task_id=?', (project_id, task_id))
            self.call('set-project-mapping', {'project_id': project_id,
                'repository': task['repository'], 'enabled': True,
                'expected_revision': self.call('get-project-mappings', {})['revision']})
            self.call('set-project-engine-mode', {'project_id': project_id, 'engine_mode': mode})
            request, _ = db.create_request(conn, util.now_iso(), stage,
                'scope:' + stage + ':' + mode, task_id=task_id,
                payload={'manual_authorized': True}, requested_by='automation')
            result.append(db.get_request(conn, request['request_id']))
        return result

    def test_all_five_stages_select_only_live_and_pin_obeys_scope(self):
        conn = db.connect(self.db_path)
        try:
            for stage in ('plan', 'dispatch', 'monitor', 'review', 'delivery'):
                with self.subTest(stage=stage):
                    requests = self._requests(conn, stage)
                    for request in requests[:2]:
                        self.assertIsNone(cmd_exec._select_candidate(conn, stage, request['request_id'], util.now_dt()))
                    self.assertEqual(cmd_exec._select_candidate(conn, stage, None, util.now_dt())['request_id'], requests[2]['request_id'])
        finally:
            conn.close()

    def test_manual_planning_remains_explicit_and_can_run_in_legacy(self):
        conn = db.connect(self.db_path)
        try:
            request = self._requests(conn, 'plan')[0]
            self.assertFalse(engine_claim_scope.allows(conn, request, util.now_dt()))
            conn.execute("UPDATE requests SET requested_by='user' WHERE request_id=?", (request['request_id'],))
            request = db.get_request(conn, request['request_id'])
            self.assertTrue(engine_claim_scope.allows(conn, request, util.now_dt()))
            counts = loop.Reconciler(self.db_path, mode='legacy')._queued_stages(conn, {})
            self.assertEqual(counts, {'plan': 1})
            db.merge_request_payload(conn, request['request_id'], util.now_iso(), manual_authorized=False)
            self.assertFalse(engine_claim_scope.allows(conn, db.get_request(conn, request['request_id']), util.now_dt()))
        finally:
            conn.close()

    def test_mapping_change_and_dead_owner_stop_new_claims(self):
        conn = db.connect(self.db_path)
        try:
            request = self._requests(conn, 'monitor')[2]
            self.assertTrue(engine_claim_scope.allows(conn, request, util.now_dt()))
            self.call('set-project-engine-mode', {'project_id': 'scope-project-2', 'engine_mode': 'shadow'})
            self.assertFalse(engine_claim_scope.allows(conn, request, util.now_dt()))
            with mock.patch.dict(os.environ, {engine_claim_scope.FORCE_ENV: '1', engine_claim_scope.MODE_ENV: 'live'}):
                self.assertTrue(engine_claim_scope.allows(conn, request, util.now_dt()))
            health.release_lease(conn, util.now_iso(), instance_id=self.instance)
            self.assertFalse(engine_claim_scope.allows(conn, request, util.now_dt()))
        finally:
            conn.close()

    def test_legacy_runner_keeps_original_claim_scope(self):
        conn = db.connect(self.db_path)
        try:
            request = self._requests(conn, 'monitor')[0]
            with mock.patch.dict(os.environ, {engine_claim_scope.WORKER_ENV: '0'}):
                self.assertTrue(engine_claim_scope.allows(conn, request, util.now_dt()))
        finally:
            conn.close()

    def test_in_process_default_force_does_not_mutate_environment(self):
        conn = db.connect(self.db_path)
        try:
            self._requests(conn, 'monitor')
            previous = dict(os.environ)
            with engine_claim_scope.command_mode('live', True):
                self.assertEqual(engine_claim_scope.effective_mode(conn, 'scope-project-0'), 'live')
            self.assertEqual(engine_claim_scope.effective_mode(conn, 'scope-project-0'), 'legacy')
            self.assertEqual(dict(os.environ), previous)
        finally:
            conn.close()

    def test_spawn_uses_explicit_database_and_propagates_mode(self):
        with mock.patch.dict(os.environ, {'KANPILOT_DB': '/wrong.db'}):
            child = runners._child_env('/correct.db', self.instance, 'shadow', True)
        self.assertEqual(child['KANPILOT_DB'], '/correct.db')
        self.assertEqual(child[engine_claim_scope.MODE_ENV], 'shadow')
        self.assertEqual(child[engine_claim_scope.FORCE_ENV], '1')
