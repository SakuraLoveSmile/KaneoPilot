"""Full-live execution rejects retired pi before sending or starting workers."""
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, cmd_dispatch, db, round_store, util


class LiveDispatchGate(KanpilotTestCase):
    def test_new_pi_claim_stops_before_availability_or_worker_side_effects(self):
        info = self.make_ready_task(task_id='live-pi-claim')
        tid = info['remote']['id']
        self.enqueue_dispatch(tid)
        ctx = cli.Context(self.db_path)
        self.addCleanup(ctx.close)
        rounds_before = [tuple(row) for row in ctx.conn.execute('SELECT * FROM execution_rounds WHERE task_id=?', (tid,))]
        with patch('kanpilot.exec_config.check_available', side_effect=AssertionError('availability')), \
             patch.object(cmd_dispatch, '_ensure_worktree', side_effect=AssertionError('worktree')), \
             patch.object(cmd_dispatch.subprocess, 'Popen', side_effect=AssertionError('worker')):
            result = self.call('claim', {'stage': 'dispatch', 'executor': 'fixture'})
        self.assertFalse(result['claimed'])
        self.assertEqual(result['code'], 'exec_backend_not_plus')
        self.assertEqual(self.fake.tasks[tid]['status'], 'to-do')
        self.assertEqual([tuple(row) for row in ctx.conn.execute('SELECT * FROM execution_rounds WHERE task_id=?', (tid,))], rounds_before)

    def historical_claim(self, *, status='submitting', started=False):
        tid, run = 'live-pi-historical', 'historical-run'
        self.make_ready_task(task_id=tid)
        ctx = cli.Context(self.db_path)
        self.addCleanup(ctx.close)
        with db.write_txn(ctx.conn):
            db.update_task(ctx.conn, tid, ctx.now_iso, status=status, run_id=run,
                           session_marker=tid + '/' + run)
            request, _ = db.create_request(ctx.conn, ctx.now_iso, 'dispatch', 'dispatch:' + tid, task_id=tid)
            db.update_request(ctx.conn, request['request_id'], ctx.now_iso, status='claimed', run_id=run,
                              lease_expires_at=util.plus_seconds(600, ctx.now))
            round_store.upsert_on_claim(ctx.conn, tid, run, {'backend': 'pi'}, ctx.now_iso)
            if started:
                round_store.mark_intent(ctx.conn, tid, run, ctx.now_iso)
                round_store.mark_started(ctx.conn, tid, run, ctx.now_iso, pid=9999999, worktree_path='/historical/worktree')
        return ctx, {'task_id': tid, 'run_id': run}

    def test_claimed_unstarted_pi_cannot_start_or_send_after_upgrade(self):
        ctx, payload = self.historical_claim()
        before = dict(round_store.require_round(ctx.conn, payload['task_id'], payload['run_id']))
        with patch.object(cmd_dispatch, '_ensure_worktree', side_effect=AssertionError('worktree')), \
             patch.object(cmd_dispatch.subprocess, 'Popen', side_effect=AssertionError('worker')):
            error = self.call_err('dispatch-start', payload)
            self.assertEqual(error.code, 'exec_backend_not_plus')
            db.update_task(ctx.conn, payload['task_id'], ctx.now_iso, status='prepared')
            error = self.call_err('dispatch-begin-send', payload)
            self.assertEqual(error.code, 'exec_backend_not_plus')
        self.assertEqual(dict(round_store.require_round(ctx.conn, payload['task_id'], payload['run_id'])), before)
        self.assertEqual(self.fake.tasks[payload['task_id']]['status'], 'to-do')

    def test_started_pi_returns_existing_identity_without_resending(self):
        ctx, payload = self.historical_claim(started=True)
        with patch.object(cmd_dispatch, '_ensure_worktree', side_effect=AssertionError('worktree')), \
             patch.object(cmd_dispatch.subprocess, 'Popen', side_effect=AssertionError('worker')), \
             patch.object(cmd_dispatch, '_pi_session_view', return_value=('old-session', 'Old')):
            result = self.call('dispatch-start', payload)
        self.assertTrue(result['deduplicated'])
        self.assertEqual((result['pid'], result['session_id'], result['worktree_path']),
                         (9999999, 'old-session', '/historical/worktree'))
