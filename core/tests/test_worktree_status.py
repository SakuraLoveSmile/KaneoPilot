"""User-facing pool health from actual isolated leases and core commands."""
from pathlib import Path

from helpers import KanpilotTestCase
from kanpilot import db, worktree_pool as pool


class WorktreeHealthTests(KanpilotTestCase):
    def test_unknown_changes_appear_in_attention_and_offline_doctor(self):
        repo = self.make_repo('Pool Health')
        conn = db.connect(self.db_path)
        try:
            lease = pool.acquire(conn, repo, stage='review', holder_id='health-review',
                                 sha='HEAD', worktrees_root=self.root / 'worktrees')
            unknown = Path(lease['path']) / 'user-notes.txt'
            unknown.write_text('preserve this work\n')
            result = pool.release(conn, lease['slot_id'], lease['holder_id'],
                                  lease['lease_token'], owner_stopped=True)
            self.assertEqual(result['state'], 'quarantined')
        finally:
            conn.close()
        attention = self.call('attention-list')['worktree_pool']
        self.assertEqual(attention['quarantined'], 1)
        self.assertEqual(attention['slots'][0]['path'], str(unknown.parent))
        self.assertEqual(self.call('overview')['attention']['worktree_pool'], attention)
        self.assertEqual(self.call('list-tasks')['attention']['worktree_pool'], attention)
        checks = self.call('doctor')['checks']
        health = next(item for item in checks if item['name'] == 'worktree_pool')
        self.assertFalse(health['ok'])
        self.assertIn('quarantined: 1', health['detail'])
        self.assertEqual(unknown.read_text(), 'preserve this work\n')

    def test_capacity_configuration_is_persisted_and_rejects_invalid_sizes(self):
        self.assertEqual(self.call('set-config', {'worktree_pool_size': 2})['worktree_pool_size'], 2)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.get_setting(conn, 'worktree_pool_size'), 2)
        finally:
            conn.close()
        for size in (0, -1, 33, True):
            self.call_err('set-config', {'worktree_pool_size': size})

    def test_unconfirmed_remote_owner_is_visible_and_is_not_released(self):
        repo = self.make_repo('Unconfirmed Owner')
        conn = db.connect(self.db_path)
        try:
            lease = pool.acquire(conn, repo, stage='review', holder_id='unknown-review',
                                 sha='HEAD', worktrees_root=self.root / 'worktrees')
            pool.mark_owner(conn, lease['slot_id'], lease['holder_id'], lease['lease_token'], state='unknown')
        finally:
            conn.close()
        state = self.call('attention-list')['worktree_pool']
        self.assertEqual(state['unconfirmed'], 1)
        self.assertEqual(state['leased'], 1)
        self.assertEqual(state['slots'][0]['slot_id'], lease['slot_id'])
        self.call_err('worktree-slot-release', {
            'slot_id': lease['slot_id'], 'holder_id': lease['holder_id'],
            'lease_token': lease['lease_token'], 'confirm': True})
        self.assertEqual(self.call('attention-list')['worktree_pool']['leased'], 1)

    def test_unknown_git_receipt_blocks_release_and_makes_doctor_unhealthy(self):
        from kanpilot import runtime_operations
        repo = self.make_repo('Unconfirmed Git')
        conn = db.connect(self.db_path)
        try:
            lease = pool.acquire(conn, repo, stage='plan', holder_id='interrupted-plan',
                                 sha='HEAD', worktrees_root=self.root / 'worktrees')
            scope = 'worktree-slot:' + lease['slot_id']
            runtime_operations.begin_operation(conn, scope, 'lost-result', 'worktree_pool',
                                               'worktree_checkout', {})
            runtime_operations.mark_operation_unknown(conn, scope, 'lost-result')
            self.assertEqual(pool.release(conn, lease['slot_id'], lease['holder_id'],
                                           lease['lease_token'], owner_stopped=True)['state'], 'leased')
        finally:
            conn.close()
        state = self.call('attention-list')['worktree_pool']
        self.assertEqual(state['unconfirmed'], 1)
        self.assertEqual(state['slots'][0]['reason'], 'operation_unconfirmed')
        health = next(item for item in self.call('doctor')['checks'] if item['name'] == 'worktree_pool')
        self.assertFalse(health['ok'])
