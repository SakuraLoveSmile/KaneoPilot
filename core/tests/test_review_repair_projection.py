"""Review repairs stay in the review lane without hiding real exceptions."""
from helpers import KanpilotTestCase
from kanpilot import db, derive, util


class ReviewRepairProjectionTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.now = util.now_dt()

    def summary(self, state, status, **fields):
        self.conn.execute('DELETE FROM tasks')
        extra = dict(fix_request=dict(mode='direct', state=state, title='Repair',
            review_key='review', source_execution_run_id='run', report_path='/report',
            report_sha256='report-sha', plan_sha256='plan-sha', fix_round=1),
            delivery=dict(pr_number=2, pushed_sha='old-head'))
        values = dict(task_id='repair', project_id='project-a', project_name='Music',
            repository='/isolated/Music', title='Task', stage='monitor', status=status,
            created_at=util.to_iso(self.now), updated_at=util.to_iso(self.now),
            extra_json=util.jdump(extra))
        values.update(fields)
        self.conn.execute('INSERT INTO tasks (' + ','.join(values) + ') VALUES (' +
                          ','.join('?' for _ in values) + ')', tuple(values.values()))
        return derive.task_summary(self.conn, db.require_task(self.conn, 'repair'), self.now)

    def test_repair_states_stay_in_review_even_with_an_existing_pr(self):
        for state, status, label, condition in (
            ('queued', 'ready', '修复会话待启动', 'repairing'),
            ('running', 'running', '修复中', 'repairing'),
            ('awaiting_review', 'in_review', '修复后待复审', 'reviewing')):
            with self.subTest(state=state):
                view = self.summary(state, status)
                self.assertEqual(view['board_column'], 'in_review')
                self.assertEqual(view['lifecycle_column'], 'in_review')
                self.assertEqual(view['display_status'], label)
                self.assertEqual(view['board_substatus'], label)
                self.assertEqual((view['phase'], view['condition']), ('review', condition))
                self.assertEqual(view['review_repair']['state'], state)

    def test_repair_does_not_hide_needs_user(self):
        view = self.summary('running', 'needs_user', failure_code='execution_failed',
                            failure_stage='running')
        self.assertEqual(view['board_column'], 'failed')
        self.assertIsNotNone(view['exception'])
        self.assertEqual(view['display_status'], '需人工')

    def test_old_repair_state_does_not_override_completed_task(self):
        view = self.summary('awaiting_review', 'done')
        self.assertEqual(view['board_column'], 'done')
        self.assertEqual(view['display_status'], '已完成')

    def test_lost_repair_worker_remains_an_exception(self):
        self.summary('running', 'running')
        request, _ = db.create_request(self.conn, util.to_iso(self.now), 'monitor',
                                       'monitor:repair', task_id='repair')
        db.update_request(self.conn, request['request_id'], util.to_iso(self.now),
                          status='claimed', lease_expires_at='2020-01-01T00:00:00Z')
        view = derive.task_summary(self.conn, db.require_task(self.conn, 'repair'), self.now)
        self.assertEqual(view['display_status'], '执行端失联')
        self.assertEqual(view['board_column'], 'failed')
        self.assertEqual(view['exception']['kind'], 'executor_lost')

    def test_held_repair_offers_its_own_start_instead_of_another_review(self):
        view = self.summary('held', 'in_review', stage='review')
        task = db.require_task(self.conn, 'repair')
        self.assertEqual(view['display_status'], '待修复')
        self.assertEqual(view['phase'], 'review')
        self.assertNotIn('enqueue', derive.allowed_actions(self.conn, task, self.now))
