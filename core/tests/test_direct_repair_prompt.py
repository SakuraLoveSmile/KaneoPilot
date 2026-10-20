"""Direct Review repair transports use isolated MCP/SQLite/Git fixtures only."""
import copy
import hashlib
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import cmd_dispatch, pi_desktop_worker, util
from test_pi_desktop_backend import PiDesktopCase, _sync
import test_pi_desktop_backend as backend
from test_pi_desktop_worker_recovery import RecoveryCase


class DirectRepairTransport(PiDesktopCase):
    _base = backend.TestRunReal._base
    def frozen_spec(self, spec):
        from kanpilot import repair_prompt
        report = self.root / 'review.md'
        report.write_text('# Review\n修复 Range 忽略时下载无界的问题。', encoding='utf-8')
        fix = {'mode': 'direct', 'state': 'running', 'review_key': 'review-1',
               'task_id': spec['task_id'], 'repository': spec['repository'],
               'report_path': str(report), 'report_sha256': util.sha256_file(report),
               'plan_sha256': spec['plan_sha256'], 'head_sha': 'a' * 40,
               'branch': 'kaneo/repair', 'worktree_path': spec['cwd'],
               'source_execution_run_id': 'original-run', 'fix_round': 1,
               'repair_execution_run_id': spec['run_id'],
               'title': '修复下载', 'description': '处理当前报告'}
        with mock.patch('kanpilot.review_repair.verify_frozen', return_value=fix):
            fields = repair_prompt.freeze({'task_id': spec['task_id'],
                                          'plan_path': spec['plan_path']})
        spec.update(fields)
        spec['instruction'] = '仅修复本轮 Review 报告，执行原 verify。'
        return spec

    def expected_prompt(self, spec):
        from kanpilot import repair_prompt
        return spec['instruction'].rstrip() + '\n\n' + repair_prompt.execution_body(spec)

    def test_first_send_is_frozen_repair_body_not_full_plan(self):
        spec, run_dir = backend.TestRunReal._base(self, body='# WHOLE_PLAN_DO_NOT_REDO\n实现所有功能\n```verify\npython3 -m unittest\n```')
        self.frozen_spec(spec)
        _sync(self.server)
        self.server.turn_statuses = ['running', 'completed']
        self.server.messages = self.default_messages()
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec), 0, self.read_job(run_dir))
        self.assertEqual(self.server.prompt_content, self.expected_prompt(spec))
        self.assertNotIn('WHOLE_PLAN_DO_NOT_REDO', self.server.prompt_content)
        self.assertIn('python3 -m unittest', self.server.prompt_content)
        self.assertEqual(self.server.prompt_count, 1)

    def test_frozen_report_plan_and_identity_mutation_are_rejected(self):
        from kanpilot import repair_prompt
        spec, _ = backend.TestRunReal._base(self, body='```verify\npython3 -m unittest\n```')
        self.frozen_spec(spec)
        for change in ('report', 'plan', 'head', 'body', 'run'):
            candidate = copy.deepcopy(spec)
            if change in ('report', 'plan'):
                candidate['review_repair']['report_sha256' if change == 'report' else 'plan_sha256'] = '0' * 64
            elif change == 'head': candidate['review_repair']['head_sha'] = 'b' * 40
            elif change == 'run': candidate['run_id'] = 'foreign-run'
            else: candidate['repair_prompt']['body'] += '\nforeign task'
            with self.subTest(change=change), self.assertRaises(util.KanpilotError):
                repair_prompt.execution_body(candidate)

    def test_report_bytes_changed_blocks_before_session_create(self):
        spec, run_dir = backend.TestRunReal._base(self, body='```verify\npython3 -m unittest\n```')
        self.frozen_spec(spec)
        Path(spec['review_repair']['report_path']).write_text('changed', encoding='utf-8')
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec), 1)
        self.assertIsNone(self.server.created)
        self.assertEqual(self.server.prompt_count, 0)

    def test_recovery_uses_identical_frozen_body_without_append_or_send(self):
        original_expected = self.expected_prompt
        # Build the existing strict persistent-turn fixture before freezing repair.
        self.expected_prompt = lambda spec: spec['instruction'].rstrip() + '\n\n' + Path(spec['plan_path']).read_text().split('---', 2)[-1].lstrip('\n')
        spec, job, client, old, new, messages = RecoveryCase.recovery_scene(self)
        self.expected_prompt = original_expected
        plan = Path(spec['plan_path'])
        plan.write_text(plan.read_text() + '\n```verify\npython3 -m unittest\n```\n')
        spec['plan_sha256'] = util.sha256_file(plan)
        self.frozen_spec(spec)
        messages[0]['content'] = self.expected_prompt(spec)
        result = pi_desktop_worker.prepare_turn_recovery(spec, job, client)
        self.assertEqual(result['prompt_sha256'], hashlib.sha256(self.expected_prompt(spec).encode()).hexdigest())
        self.assertFalse(client.agent_prompt.called)
        messages[0]['content'] += '\n重新执行全部计划'
        with self.assertRaises(util.KanpilotError):
            pi_desktop_worker.prepare_turn_recovery(spec, job, client)

    def test_goal_continuation_uses_frozen_repair_prompt(self):
        import test_goal_continuation as continuation
        old_expected = self.expected_prompt
        self.expected_prompt = lambda spec: spec['instruction'].rstrip() + '\n\n' + Path(spec['plan_path']).read_text().split('---', 2)[-1].lstrip('\n')
        spec, run_dir, job, reader = continuation.TestGoalContinuation.scene(self)
        self.expected_prompt = old_expected
        plan = Path(spec['plan_path'])
        plan.write_text(plan.read_text() + '\n```verify\npython3 -m unittest\n```\n')
        spec['plan_sha256'] = util.sha256_file(plan)
        self.frozen_spec(spec)
        self.server.messages[0]['content'] = self.expected_prompt(spec)
        result = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        self.assertEqual(result['scope_id'], job['scope_id'])
        self.assertEqual(self.server.prompt_count, 0)
        self.server.messages[0]['content'] += '\nforeign user instructions'
        with self.assertRaises(util.KanpilotError):
            pi_desktop_worker.prepare_goal_continuation(spec, job, reader)


    def test_pi_launcher_gets_same_repair_body_before_any_process(self):
        from kanpilot import pi_worker
        spec, run_dir = backend.TestRunReal._base(self, body='# WHOLE_PLAN_DO_NOT_REDO\n```verify\npython3 -m unittest\n```')
        self.frozen_spec(spec)
        with mock.patch.object(pi_worker.subprocess, 'Popen', side_effect=OSError('isolated stop')) as launch:
            self.assertEqual(pi_worker._run_real(spec, mock.Mock()), 1)
        self.assertEqual(launch.call_args.args[0][-1], self.expected_prompt(spec))
        self.assertNotIn('WHOLE_PLAN_DO_NOT_REDO', launch.call_args.args[0][-1])


class DirectRepairWorktree(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        from kanpilot import cli, core, db
        from test_git_delivery import make_repo, branch
        self.repo = make_repo(self.repos_root)
        self.repo = self.repo.rename(self.repos_root / 'Feedback')
        self.make_ready_task(task_id='task1234')
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.delivery = branch(self.repo, self.root / 'worktrees')
        with db.write_txn(self.ctx.conn):
            task = db.require_task(self.ctx.conn, 'task1234')
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso,
                                 **self.delivery, mode='manual', workspace_version=1)
        self.task = db.require_task(self.ctx.conn, 'task1234')
        self.fix = {'mode': 'direct', **self.delivery, 'review_key': 'r1',
                    'source_execution_run_id': 'source-run'}
        self.source_dir = self.runs_root / 'task1234' / 'source-run'
        self.source_dir.mkdir(parents=True)
        (self.source_dir / 'job.json').write_text(util.jdump({
            'backend': 'pi', 'task_id': 'task1234', 'run_id': 'source-run',
            'status': 'finished', 'ended_at': util.now_iso(), 'exit_code': 0, 'pid': None}))
        (self.source_dir / 'events.jsonl').write_text(util.jdump({
            'seq': 1, 'type': 'terminal', 'outcome': 'completed', 'ts': util.now_iso()}) + '\n')
        self.ctx.conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,run_status,worktree_path,config_json,created_at,updated_at) VALUES(?,?,'pi','finished',?,'{}',?,?)",
            ('task1234', 'source-run', self.delivery['worktree_path'], self.ctx.now_iso, self.ctx.now_iso))

    def test_manual_versioned_repair_reuses_original_worktree(self):
        with mock.patch('kanpilot.core.review_repair_of', return_value=self.fix), mock.patch(
                'kanpilot.review_repair.verify_frozen', return_value=self.fix):
            result = cmd_dispatch._ensure_worktree(self.task, 'task1234', 'new-repair-run', self.ctx.conn)
        self.assertEqual(result, (Path(self.delivery['worktree_path']), 'branch'))

    def test_dirty_or_changed_head_never_falls_back_to_main_checkout(self):
        from test_git_delivery import git
        wt = Path(self.delivery['worktree_path'])
        (wt / 'dirty.txt').write_text('dirty')
        with mock.patch('kanpilot.core.review_repair_of', return_value=self.fix), mock.patch(
                'kanpilot.review_repair.verify_frozen', return_value=self.fix):
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch._ensure_worktree(self.task, 'task1234', 'repair-run', self.ctx.conn)
            (wt / 'dirty.txt').unlink()
            self.fix['head_sha'] = 'b' * 40
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch._ensure_worktree(self.task, 'task1234', 'repair-run', self.ctx.conn)
        self.assertEqual(git(self.repo, 'symbolic-ref', '--short', 'HEAD'), 'main')

    def test_dispatch_accepts_review_status_with_same_frozen_plan(self):
        from kanpilot import dispatch_validate
        self.fake.tasks['task1234']['status'] = 'in-review'
        with mock.patch('kanpilot.core.review_repair_of', return_value=self.fix), mock.patch(
                'kanpilot.review_repair.verify_frozen', return_value=self.fix):
            checked = dispatch_validate.validate_dispatch(self.ctx.conn, self.task, self.ctx.now)
        self.assertEqual(checked['review_repair'], self.fix)
        self.assertEqual(checked['original_todo_slug'], 'in-review')
        self.assertEqual(checked['plan_sha256'], self.task['plan_sha256'])

    def test_source_execution_must_remain_completed_at_claim_and_start(self):
        from kanpilot import dispatch_validate
        (self.source_dir / 'job.json').write_text(util.jdump({
            'backend': 'pi', 'task_id': 'task1234', 'run_id': 'source-run',
            'status': 'running', 'pid': __import__('os').getpid()}))
        (self.source_dir / 'events.jsonl').write_text('')
        with mock.patch('kanpilot.core.review_repair_of', return_value=self.fix), mock.patch(
                'kanpilot.review_repair.verify_frozen', return_value=self.fix):
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch._ensure_worktree(self.task, 'task1234', 'repair-run', self.ctx.conn)
            with self.assertRaises(dispatch_validate.DispatchCheckFailed):
                dispatch_validate.validate_dispatch(self.ctx.conn, self.task, self.ctx.now)
