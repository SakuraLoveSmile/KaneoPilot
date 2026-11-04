"""Ordinary release verification controls use isolated Git/SQLite/Plus fixtures."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import test_release_verification as fixtures
from kanpilot import (core, db, delivery_contract, derive, project_config,
                      release_delivery as release, release_verification as verification, util)


class ReleaseVerificationProjectionTest(TestCase):
    def setUp(self):
        self.case = fixtures.ReleaseVerificationTest(
            'test_complete_preserves_original_evidence_and_user_checkout_and_binds_release')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx = self.case.conn, self.case.ctx
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso, pr_state='MERGED')
        self.record_blocker()

    def task(self):
        return self.case.task()

    def record_blocker(self, code='release_target_unverified'):
        delivery = core.delivery_of(self.task())
        payload = dict(request_id='sync-current', run_id='sync-current-run')
        contract = delivery_contract.freeze(self.task(), payload, delivery, 'sync')
        blocked = dict(state='blocked', title='发布受阻', reason='发布目标不是本轮已验证的完整源码',
            root_task_id='parent', member_task_ids=['parent'],
            blockers=[dict(code=code, message='发布目标不是本轮已验证的完整源码')])
        result = delivery_contract.verified_result(contract, self.task(), payload, delivery,
            dict(outcome='waiting', release=blocked))
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso,
            stage_contracts=dict(sync=contract), stage_results=dict(sync=result))

    def view(self):
        return release.projection(self.conn, self.task())

    def actions(self):
        return derive.allowed_actions(self.conn, self.task(), self.ctx.now)

    def assert_no_verification_action(self):
        view = self.view() or {}
        self.assertFalse(view.get('can_prepare_verification'), view)
        self.assertFalse(view.get('can_sync_verification'), view)
        self.assertFalse(view.get('can_restart_verification'), view)
        self.assertFalse(set(self.actions()) & {'prepare_release_verification',
            'sync_release_verification', 'restart_release_verification'})

    def test_current_ordinary_blocker_exposes_readonly_preparation_and_footer_action(self):
        changes = self.conn.total_changes
        with patch.object(verification, '_context', side_effect=AssertionError('not a SQL projection')), \
                patch.object(release, '_prepare', side_effect=AssertionError('not a SQL projection')):
            self.conn.execute('PRAGMA query_only=ON')
            try:
                view = self.view()
                self.assertTrue(view['can_prepare_verification'])
                self.assertFalse(view['can_start'])
                self.assertIn('prepare_release_verification', self.actions())
            finally:
                self.conn.execute('PRAGMA query_only=OFF')
        prepared = self.case.prepare()
        self.assertEqual(prepared['verification']['state'], 'prepared')
        self.assertEqual(prepared['verification']['commands'], self.case.fixture.parent_commands)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual((self.case.plus.created, self.case.plus.prompts), (0, 0))
        self.assertIsNotNone(release.completion_gate(self.conn, self.task()))

    def test_planning_unknown_and_verified_replace_old_preflight_reason(self):
        first = self.case.start()
        for state in ('planning', 'running', 'unknown'):
            value = verification._load(self.conn, 'parent', first['verification_id'])
            value.update(state=state, reason='本轮验证阶段：' + state)
            verification._save(self.conn, value, self.ctx.now_iso)
            view = self.view()
            self.assertEqual(view['verification']['verification_id'], first['verification_id'])
            self.assertEqual(view['verification']['state'], state)
            self.assertEqual(view['reason'], value['reason'])
            self.assertTrue(view['can_sync_verification'])
            self.assertFalse(view['can_prepare_verification'])
            self.assertIn('sync_release_verification', self.actions())
            self.assertNotIn('prepare_release_verification', self.actions())
            self.assertFalse(view['can_start'])
        self.assertEqual(self.case.sync(first['verification_id'])['verification']['state'], 'verified')
        view = self.view()
        self.assertEqual(view['state'], 'ready')
        self.assertEqual(view['title'], '待构建发布')
        self.assertTrue(view['can_start'])
        self.assertFalse(view['can_sync_verification'])
        self.assertEqual(view['blockers'], [])
        self.assertIsNotNone(release.completion_gate(self.conn, self.task()))
        prepared = release.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.assertEqual(prepared['release']['state'], 'ready')
        self.assertEqual((self.case.plus.created, self.case.plus.prompts, self.case.plus.approvals), (1, 1, 1))

    def test_unknown_create_keeps_only_original_attempt_reconciliation(self):
        self.case.plus.lose = 'create'
        first = self.case.start()
        view = self.view()
        self.assertEqual(view['verification']['state'], 'unknown')
        self.assertTrue(view['can_sync_verification'])
        self.assertFalse(view['can_prepare_verification'])
        self.assertFalse(view['can_restart_verification'])
        self.assertEqual(self.case.sync(first['verification_id'])['verification']['state'], 'unknown')
        self.assertEqual((self.case.plus.created, self.case.plus.prompts), (1, 0))

    def test_completed_insufficient_report_can_only_explicitly_restart(self):
        old, payload = self.case.partial_report()
        view = self.view()
        self.assertEqual(view['title'], '验证受阻')
        self.assertEqual(view['reason'], old['reason'])
        self.assertEqual(view['verification']['failure_code'], 'release_verification_report')
        self.assertTrue(view['can_restart_verification'])
        self.assertFalse(view['can_sync_verification'])
        self.assertIn('restart_release_verification', self.actions())
        changes = self.conn.total_changes
        prepared = verification.cmd_prepare_restart_release_verification(self.ctx, dict(task_id='parent'))
        self.assertEqual(prepared['expected_report_sha256'], payload['expected_report_sha256'])
        self.assertEqual(self.conn.total_changes, changes)
        scope = verification._approval_operation(old)[0]
        self.conn.execute("INSERT INTO runtime_operations(scope_id,operation_id,request_sha256,harness_id,tool,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (scope, 'unknown-write', 'x', 'pi_desktop', 'unknown', 'unknown', self.ctx.now_iso, self.ctx.now_iso))
        self.assertFalse(self.view()['can_restart_verification'])

    def test_first_entry_requires_exact_current_target_blocker(self):
        self.record_blocker('workflow_unavailable')
        self.assert_no_verification_action()
        self.record_blocker()
        task = self.task()
        db.update_task(self.conn, 'parent', self.ctx.now_iso, requirements_sha256='changed-requirements')
        self.assert_no_verification_action()
        db.update_task(self.conn, 'parent', self.ctx.now_iso, requirements_sha256=task['requirements_sha256'])
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso, merged_at=None, pr_state='OPEN')
        self.assert_no_verification_action()

    def test_first_entry_rejects_unsupported_execution_pause_takeover_and_mapping_drift(self):
        fields = dict(self.task())
        original_extra = db.task_extra(self.task())
        original_settings = db.get_setting(self.conn, project_config.SETTINGS_KEY)
        round_row = dict(self.conn.execute("SELECT * FROM execution_rounds WHERE task_id='parent'").fetchone())
        variants = ('codex', 'round-archived', 'round-running', 'global-pause', 'delivery-pause',
            'task-pause', 'takeover', 'archived', 'mapping', 'policy', 'request', 'child', 'parent')
        for variant in variants:
            with self.subTest(variant=variant):
                self.conn.execute('SAVEPOINT eligibility')
                try:
                    if variant == 'codex':
                        self.conn.execute("UPDATE execution_rounds SET backend='codex' WHERE task_id='parent'")
                    elif variant == 'round-archived':
                        self.conn.execute("UPDATE execution_rounds SET save_state='archived' WHERE task_id='parent'")
                    elif variant == 'round-running':
                        self.conn.execute("UPDATE execution_rounds SET run_status='running' WHERE task_id='parent'")
                    elif variant == 'global-pause':
                        db.set_setting(self.conn, 'paused_global', True)
                    elif variant == 'delivery-pause':
                        db.set_setting(self.conn, 'paused_stages', ['delivery'])
                    elif variant == 'task-pause':
                        db.update_task(self.conn, 'parent', self.ctx.now_iso, auto_advance_paused=1)
                    elif variant == 'takeover':
                        extra = deepcopy(original_extra)
                        extra['automation'] = dict(extra.get('automation') or {}, manual_takeover=True)
                        db.update_task(self.conn, 'parent', self.ctx.now_iso, extra_json=extra)
                    elif variant == 'archived':
                        db.update_task(self.conn, 'parent', self.ctx.now_iso, save_state='archived')
                    elif variant in ('mapping', 'policy'):
                        setting = deepcopy(original_settings)
                        entry = setting['items']['project']
                        if variant == 'mapping':
                            entry['repository'] = '/different/repository'
                        else:
                            entry['automation']['release']['enabled'] = False
                        db.set_setting(self.conn, project_config.SETTINGS_KEY, setting)
                    elif variant == 'request':
                        db.create_request(self.conn, self.ctx.now_iso, 'review', 'unrelated-current-review', task_id='parent')
                    elif variant == 'child':
                        columns = {key: fields[key] for key in ('task_id', 'project_id', 'project_name', 'status', 'stage', 'created_at', 'updated_at')}
                        columns.update(task_id='child', review_source_task_id='parent')
                        self.conn.execute('INSERT INTO tasks (' + ','.join(columns) + ') VALUES (' +
                            ','.join('?' for _ in columns) + ')', tuple(columns.values()))
                    elif variant == 'parent':
                        db.update_task(self.conn, 'parent', self.ctx.now_iso, review_source_task_id='nonexistent')
                    self.assert_no_verification_action()
                finally:
                    self.conn.execute('ROLLBACK TO eligibility')
                    self.conn.execute('RELEASE eligibility')
        self.assertTrue(self.view()['can_prepare_verification'])
        self.assertEqual(dict(self.task()), fields)
        self.assertEqual(dict(self.conn.execute("SELECT * FROM execution_rounds WHERE task_id='parent'").fetchone()), round_row)

    def test_stale_or_untrusted_attempt_cannot_offer_verification_or_release(self):
        first = self.case.start()
        scope, operation = verification._scope('parent'), verification._op(first['verification_id'])
        for fields in ({'harness_id': 'other'}, {'request_sha256': 'changed'}, {'status': 'accepted'}):
            with self.subTest(fields=fields):
                self.conn.execute('SAVEPOINT receipt')
                try:
                    self.conn.execute('UPDATE runtime_operations SET ' + ','.join(key + '=?' for key in fields) +
                        ' WHERE scope_id=? AND operation_id=?', (*fields.values(), scope, operation))
                    self.assert_no_verification_action()
                    self.assertFalse(self.view()['can_start'])
                    self.assertNotIn('verification', self.view())
                finally:
                    self.conn.execute('ROLLBACK TO receipt')
                    self.conn.execute('RELEASE receipt')
        db.update_task(self.conn, 'parent', self.ctx.now_iso, requirements_sha256='new-requirements')
        self.assert_no_verification_action()
        self.assertNotIn('verification', self.view())

    def test_attempt_read_projection_performs_no_runtime_or_evidence_reads(self):
        first = self.case.start()
        changes = self.conn.total_changes
        with patch.object(verification, '_source', side_effect=AssertionError('filesystem verification')), \
                patch.object(verification, '_client', side_effect=AssertionError('runtime')), \
                patch.object(verification, '_context', side_effect=AssertionError('remote preflight')):
            self.conn.execute('PRAGMA query_only=ON')
            try:
                self.assertEqual(self.view()['verification']['verification_id'], first['verification_id'])
                self.assertIn('sync_release_verification', self.actions())
            finally:
                self.conn.execute('PRAGMA query_only=OFF')
        self.assertEqual(self.conn.total_changes, changes)
