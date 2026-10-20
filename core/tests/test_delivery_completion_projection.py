"""Final task state supersedes the merge receipt's earlier progress message."""
from helpers import KanpilotTestCase
from kanpilot import db, derive, util


class DeliveryCompletionProjectionTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.now = util.now_dt()

    def task(self, status='done', save_state='active', **delivery_fields):
        delivery = dict(mode='pr', head_sha='reviewed-head', pr_state='MERGED',
            merged_at='2026-10-18T16:25:42Z',
            auto_merge=dict(state='merged', head_sha='reviewed-head',
                            reason='PR 已合并，等待完成同步', updated_at='2026-10-18T16:25:34Z'))
        delivery.update(delivery_fields)
        values = dict(task_id='completed', project_id='music', project_name='Music',
            title='修复下载上限', repository='/isolated/Music', status=status, stage='review',
            save_state=save_state, created_at=util.to_iso(self.now), updated_at=util.to_iso(self.now),
            extra_json=util.jdump(dict(delivery=delivery)), last_progress='已合并并完成任务')
        self.conn.execute('INSERT INTO tasks (' + ','.join(values) + ') VALUES (' +
                          ','.join('?' for _ in values) + ')', tuple(values.values()))
        return db.require_task(self.conn, 'completed')

    def test_completed_merge_shows_final_result_in_list_and_detail_without_mutation(self):
        task = self.task()
        raw = task['extra_json']
        for render in (derive.task_summary, derive.task_detail):
            view = render(self.conn, task, self.now)
            self.assertEqual(view['display_status'], '已完成')
            self.assertEqual(view['delivery']['auto_merge']['reason'], 'PR 已合并，任务已完成')
            self.assertEqual(view['delivery']['auto_merge']['state'], 'merged')
        self.assertEqual(db.require_task(self.conn, 'completed')['extra_json'], raw)

    def test_merged_pr_still_waits_when_task_is_not_completed(self):
        view = derive.task_summary(self.conn, self.task(status='in_review'), self.now)
        self.assertEqual(view['delivery']['auto_merge']['reason'], 'PR 已合并，等待完成同步')

    def test_archived_task_keeps_its_save_state_label(self):
        view = derive.task_summary(self.conn, self.task(save_state='archived'), self.now)
        self.assertEqual(view['display_status'], '已归档')
        self.assertEqual(view['delivery']['auto_merge']['reason'], 'PR 已合并，任务已完成')

    def test_old_head_merge_is_not_relabelled_as_current_completion(self):
        task = self.task(head_sha='new-head')
        view = derive.task_summary(self.conn, task, self.now)
        self.assertEqual(view['delivery']['auto_merge']['head_sha'], 'reviewed-head')
        self.assertNotEqual(view['delivery']['auto_merge']['reason'], 'PR 已合并，任务已完成')
