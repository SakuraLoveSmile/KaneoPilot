"""Automatic retries keep immutable user inputs; publication failures keep drafts."""
import json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from helpers import KanpilotTestCase
from kanpilot import (auto_intake, automation_policy, cli, constants, core, db,
                     derive, git_delivery, planner_inputs, planner_runner, util)


class TestPlannerRetryInputs(KanpilotTestCase):
    def failed_plan(self, suffix='one', *, completed=False):
        task_id = 'retryinput' + suffix
        remote_task = self.fake.add_task(task_id, 'fixture-project-feedback')
        repo = self.make_repo('Feedback')
        original = self.root / ('attachment-' + suffix + '.md')
        original.write_text('Keep this exact local attachment.\n', encoding='utf-8')
        supplement = '用户确认：保留完整材料；额度 128/512 MiB。\n第二行不省略。\n'
        prepared = self.call('prepare-plan-input', dict(task_id=task_id, supplement=supplement,
            local_paths=[str(original)], excluded_remote_ids=['excluded-attachment']))
        self.assertTrue(prepared['can_start'])
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        stamp = util.now_iso()
        policy = automation_policy.cmd_set_project_automation(SimpleNamespace(conn=conn, now_iso=stamp),
            dict(project_id=remote_task['projectId'], automation=dict(enabled=True)))['automation']
        branch, worktree = 'kaneo/' + task_id, self.root / ('worktree-' + suffix)
        git_delivery._git(['worktree', 'add', '-b', branch, str(worktree), 'HEAD'], repo, 30)
        head = git_delivery._value(['rev-parse', 'HEAD'], repo)
        delivery = dict(mode='pr', branch=branch, worktree_path=str(worktree),
                        base_branch='main', base_sha=head, head_sha=head)
        request_id = self.call('start-plans', dict(items=[dict(input_id=prepared['input_id'],
            input_sha256=prepared['input_sha256'], automation=dict(authorized_by='automation',
                policy_revision=policy['policy_revision'], delivery=delivery))]))['results'][0]['request_id']
        claim = self.call('claim', dict(stage='plan', executor=constants.PLANNER_EXECUTOR_ID, request_id=request_id))
        self.assertTrue(claim['claimed'])
        directory = self.runs_root / task_id / claim['run_id']
        directory.mkdir(parents=True, exist_ok=True)
        payload = db.request_payload(db.get_request(conn, request_id))
        job = dict(task_id=task_id, request_id=request_id, run_id=claim['run_id'], backend='codex',
            status='failed', exit_code=0 if completed else 1, owner_stopped=True,
            detail='publish failed: 远端任务已不在 To Do（in-progress），拒绝发布' if completed else 'isolated process failure')
        if completed:
            planner_runner._freeze_plan_contract(job, planner_runner._prepare_stage_inputs(
                claim['context'], task_id, request_id, claim['run_id'], 'codex'), delivery['worktree_path'])
            output = dict(status='ready', plan_markdown='# 原完整规划稿\n\n```verify\npython3 -m unittest\n```\n',
                          questions=[], evidence=['manifest.json'])
            planner_runner._plan_stage_result(job, output, evidence=dict(mechanism='cli_process', exit_code=0,
                output_path=str(directory / 'last-message.txt')))
            (directory / 'last-message.txt').write_text(util.jdump(output), encoding='utf-8')
        (directory / 'job.json').write_text(util.jdump(job), encoding='utf-8')
        (directory / 'events.jsonl').write_text(util.jdump(dict(seq=1, type='terminal', outcome='failed',
            reason='publish_failed' if completed else 'process_exit')) + '\n', encoding='utf-8')
        self.call('report', dict(request_id=request_id, run_id=claim['run_id'], outcome='failed',
            failure_code='planner_failed', note=job['detail']))
        original.unlink()  # Reuse the frozen copy even when its selected source is gone.
        return SimpleNamespace(conn=conn, task_id=task_id, request_id=request_id, directory=directory,
            prepared=prepared, payload=payload, policy=policy, remote_task=remote_task, supplement=supplement)

    def retry(self, case, *, cli_run=None):
        actual = cli.run
        actual_start = planner_inputs.start_plans
        def run(command, payload, path):
            self.assertNotEqual(command, 'prepare-plan-input', 'retry must keep the entire original input')
            if cli_run is not None:
                return cli_run(command, payload, path)
            return actual(command, payload, path)
        def start(ctx, payload, **kwargs):
            self.assertFalse(ctx.conn.in_transaction, 'submission preparation must not hold a write transaction')
            if cli_run is not None:
                return cli_run('start-plans', payload, self.db_path)
            return actual_start(ctx, payload, **kwargs)
        with patch.object(auto_intake.cli, 'run', side_effect=run), \
             patch.object(planner_inputs, 'start_plans', side_effect=start):
            return auto_intake._submit(case.conn, self.db_path, case.task_id, case.policy,
                util.now_iso(), case.payload['manifest']['requirements_sha256'], wanted=True)

    def new_successful_owner(self, case):
        # Exercise the actual start/claim/publish command path without a model.
        item = dict(input_id=case.prepared['input_id'], input_sha256=case.prepared['input_sha256'],
            automation=dict(authorized_by='automation', policy_revision=case.policy['policy_revision'],
                            delivery=core.delivery_of(db.require_task(case.conn, case.task_id))))
        request_id = self.call('start-plans', dict(items=[item]))['results'][0]['request_id']
        claim = self.call('claim', dict(stage='plan', executor=constants.PLANNER_EXECUTOR_ID, request_id=request_id))
        generated = util.now_iso()
        path = planner_runner.write_plan_file(claim['context'], case.payload['manifest'],
            '# New owner plan\n\n```verify\npython3 -m unittest\n```\n', generated)
        model = db.request_payload(db.get_request(case.conn, request_id))['model_config']
        self.call('publish-plan', dict(task_id=case.task_id, request_id=request_id, run_id=claim['run_id'],
            plan_path=str(path), plan_kind='standard', source_sha256=case.payload['manifest']['source_sha256'],
            requirements_sha256=case.payload['manifest']['requirements_sha256'], generated_at=generated,
            planner_model=model['planner_model'], reasoning_effort=model['reasoning_effort'],
            planner_input_sha256=case.prepared['input_sha256']))
        self.assertEqual(db.get_request(case.conn, request_id)['status'], 'succeeded')
        self.assertEqual(db.require_task(case.conn, case.task_id)['status'], 'ready')
        return request_id

    def explicit_replan(self, case):
        with db.write_txn(case.conn):
            core.block_task(case.conn, db.require_task(case.conn, case.task_id), util.now_iso(),
                            'explicit replan fixture', 'planner_failed', 'user', stage='plan')
        self.call('resolve', dict(task_id=case.task_id, action='replan'))
        self.assertIsNone(db.active_request_for_key(case.conn, 'plan:' + case.task_id))

    def assert_stopped(self, case):
        task = db.require_task(case.conn, case.task_id)
        self.assertEqual(task['status'], 'needs_user')
        self.assertFalse(core.automation_of(task)['plan_wanted'])
        self.assertEqual(db.get_request(case.conn, case.request_id)['status'], 'failed')
        self.assertIsNone(db.active_request_for_key(case.conn, 'plan:' + case.task_id))

    def test_retry_reuses_whole_frozen_input_and_local_copy_without_preparing_again(self):
        case = self.failed_plan()
        count = case.conn.execute('SELECT COUNT(*) FROM planner_inputs').fetchone()[0]
        original = dict(db.get_request(case.conn, case.request_id))
        root = Path(case.prepared['input_dir'])
        before = {entry['path']: (root / entry['path']).read_bytes() for entry in case.payload['manifest']['files']}
        response = self.retry(case)
        self.assertIn('request_id', response)
        self.assertNotEqual(response['request_id'], case.request_id)
        request = db.get_request(case.conn, response['request_id'])
        payload = db.request_payload(request)
        self.assertEqual(payload['input_id'], case.prepared['input_id'])
        self.assertEqual(payload['planner_input_sha256'], case.prepared['input_sha256'])
        self.assertEqual(payload['manifest'], case.payload['manifest'])
        self.assertEqual(payload['manifest']['supplement'], case.supplement)
        self.assertEqual(payload['manifest']['excluded_remote_ids'], ['excluded-attachment'])
        self.assertTrue(any(entry['source'] == 'local' for entry in payload['manifest']['files']))
        self.assertEqual({name: (root / name).read_bytes() for name in before}, before)
        self.assertEqual(case.conn.execute('SELECT COUNT(*) FROM planner_inputs').fetchone()[0], count)
        self.assertEqual(dict(db.get_request(case.conn, case.request_id)), original)
        self.assertEqual(request['requested_by'], 'automation')

    def test_completed_publish_failure_stops_model_retry_and_preserves_draft_and_original_terminal(self):
        case = self.failed_plan(completed=True)
        original = dict(db.get_request(case.conn, case.request_id))
        saved = {path.name: path.read_bytes() for path in case.directory.iterdir() if path.is_file()}
        with patch.object(git_delivery, 'prepare_branch_worktree', side_effect=AssertionError('no new worktree')):
            response = self.retry(case, cli_run=lambda *args: self.fail('completed draft must not start a new request'))
        self.assertEqual(response['reason'], 'planner_publish_failed')
        self.assert_stopped(case)
        task = db.require_task(case.conn, case.task_id)
        self.assertIn('规划稿已完成', task['failure_reason'])
        self.assertIn('in-progress', task['failure_reason'])
        self.assertEqual(dict(db.get_request(case.conn, case.request_id)), original)
        self.assertEqual({path.name: path.read_bytes() for path in case.directory.iterdir() if path.is_file()}, saved)
        self.assertIn('replan', derive.allowed_actions(case.conn, task, util.now_dt()))
        resolved = self.call('resolve', dict(task_id=case.task_id, action='replan'))
        self.assertEqual(resolved['task_status'], 'needs_plan')
        self.assertEqual(db.get_request(case.conn, case.request_id)['status'], 'failed')

    def test_non_native_retry_keeps_frozen_input_without_requiring_native_job_format(self):
        for backend in ('pi_desktop', 'claude', 'codex_desktop', 'qoder'):
            with self.subTest(backend=backend):
                case = self.failed_plan(backend.replace('_', ''))
                db.merge_request_payload(case.conn, case.request_id, util.now_iso(), planner_backend=backend)
                (case.directory / 'job.json').unlink()
                submitted = []
                def start(command, payload, path):
                    self.assertEqual(command, 'start-plans')
                    submitted.append(payload['items'][0])
                    return dict(results=[dict(request_id='isolated-request')])
                response = self.retry(case, cli_run=start)
                self.assertEqual(response['request_id'], 'isolated-request')
                self.assertEqual(submitted[0]['input_id'], case.prepared['input_id'])
                self.assertEqual(submitted[0]['input_sha256'], case.prepared['input_sha256'])
                self.assertEqual(submitted[0]['planner_backend'], backend)

    def test_fenced_completed_draft_keeps_real_publish_failure_classification(self):
        case = self.failed_plan(completed=True)
        path = case.directory / 'last-message.txt'
        path.write_text('```json\n' + path.read_text() + '\n```\n')
        response = self.retry(case, cli_run=lambda *args: self.fail('completed draft must not rerun'))
        self.assertEqual(response['reason'], 'planner_publish_failed')
        self.assert_stopped(case)
        self.assertIn('in-progress', db.require_task(case.conn, case.task_id)['failure_reason'])

    def test_retry_rejects_changed_or_missing_frozen_inputs_without_thinner_fallback(self):
        for index, fault in enumerate(('missing_input', 'manifest', 'local_copy', 'source', 'job_identity',
                                       'task_requirements', 'task_project', 'remote_requirements', 'remote_status')):
            with self.subTest(fault=fault):
                case = self.failed_plan(str(index))
                root = Path(case.prepared['input_dir'])
                if fault == 'missing_input':
                    case.conn.execute('DELETE FROM planner_inputs WHERE input_id=?', (case.prepared['input_id'],))
                elif fault == 'manifest':
                    (root / 'manifest.json').write_text('{}')
                elif fault in ('local_copy', 'source'):
                    entry = next(entry for entry in case.payload['manifest']['files']
                        if entry['source'] == ('local' if fault == 'local_copy' else 'source_snapshot'))
                    (root / entry['path']).write_text('tampered frozen material')
                elif fault == 'job_identity':
                    path = case.directory / 'job.json'
                    job = json.loads(path.read_bytes()); job['request_id'] = 'other-request'
                    path.write_text(util.jdump(job))
                elif fault == 'task_requirements':
                    db.update_task(case.conn, case.task_id, util.now_iso(), requirements_sha256='f' * 64)
                elif fault == 'task_project':
                    db.update_task(case.conn, case.task_id, util.now_iso(), project_id='other-project')
                elif fault == 'remote_requirements':
                    case.remote_task['description'] = 'Requirements changed after failure.'
                elif fault == 'remote_status':
                    case.remote_task['status'] = 'in-progress'
                response = self.retry(case, cli_run=lambda *args: self.fail('changed input must not submit'))
                self.assertIn('reason', response)
                self.assert_stopped(case)

    def test_active_request_is_left_with_its_current_owner(self):
        case = self.failed_plan()
        new, _ = db.create_request(case.conn, util.now_iso(), 'plan', 'plan:' + case.task_id,
                                  task_id=case.task_id, payload={'manual_authorized': True})
        before = dict(db.require_task(case.conn, case.task_id))
        response = self.retry(case, cli_run=lambda *args: self.fail('active owner must not be replaced'))
        self.assertEqual(response['reason'], 'active_request')
        self.assertEqual(dict(db.require_task(case.conn, case.task_id)), before)
        self.assertEqual(db.get_request(case.conn, new['request_id'])['status'], 'queued')

    def test_new_owner_during_retry_preflight_is_not_blocked_by_the_old_failure(self):
        case = self.failed_plan()
        actual = auto_intake._retry_file
        new_request = []
        def read(root, name):
            if name == 'job.json':
                request, _ = db.create_request(case.conn, util.now_iso(), 'plan', 'plan:' + case.task_id,
                    task_id=case.task_id, payload={'manual_authorized': True})
                new_request.append(request['request_id'])
                raise util.KanpilotError('conflict', 'isolated evidence race')
            return actual(root, name)
        before = dict(db.require_task(case.conn, case.task_id))
        with patch.object(auto_intake, '_retry_file', side_effect=read):
            response = self.retry(case, cli_run=lambda *args: self.fail('new owner must not be replaced'))
        self.assertEqual(response['reason'], 'conflict')
        self.assertEqual(dict(db.require_task(case.conn, case.task_id)), before)
        self.assertEqual(db.get_request(case.conn, new_request[0])['status'], 'queued')

    def test_failure_transaction_preserves_finished_new_owner_and_explicit_replan(self):
        for action in ('finished_owner', 'explicit_replan'):
            with self.subTest(action=action):
                case = self.failed_plan(action.replace('_', ''))
                actual = auto_intake._retry_file
                newer = []
                def read(root, name):
                    if name == 'job.json':
                        if action == 'finished_owner': self.new_successful_owner(case)
                        else: self.explicit_replan(case)
                        newer.append(dict(db.require_task(case.conn, case.task_id)))
                        raise util.KanpilotError('conflict', 'old retry lost its identity')
                    return actual(root, name)
                with patch.object(auto_intake, '_retry_file', side_effect=read):
                    response = self.retry(case)
                self.assertEqual(response['reason'], 'conflict')
                self.assertEqual(dict(db.require_task(case.conn, case.task_id)), newer[0])
                self.assertIsNone(db.active_request_for_key(case.conn, 'plan:' + case.task_id))

    def test_submission_transaction_rejects_finished_owner_and_explicit_replan_after_external_io(self):
        for action in ('finished_owner', 'explicit_replan'):
            with self.subTest(action=action):
                case = self.failed_plan(action.replace('_', ''))
                actual = planner_inputs._prepare_submission
                newer, fired = [], []
                def prepare(conn, item):
                    prepared = actual(conn, item)
                    if not fired:
                        self.assertFalse(conn.in_transaction)
                        fired.append(True)
                        if action == 'finished_owner': self.new_successful_owner(case)
                        else: self.explicit_replan(case)
                        newer.append(dict(db.require_task(case.conn, case.task_id)))
                    return prepared
                count = case.conn.execute('SELECT COUNT(*) FROM requests WHERE task_id=?', (case.task_id,)).fetchone()[0]
                with patch.object(planner_inputs, '_prepare_submission', side_effect=prepare):
                    response = self.retry(case)
                self.assertEqual(response['reason'], 'conflict')
                self.assertEqual(dict(db.require_task(case.conn, case.task_id)), newer[0])
                expected_count = count + (1 if action == 'finished_owner' else 0)
                self.assertEqual(case.conn.execute('SELECT COUNT(*) FROM requests WHERE task_id=?',
                    (case.task_id,)).fetchone()[0], expected_count)
                self.assertIsNone(db.active_request_for_key(case.conn, 'plan:' + case.task_id))

    def test_retry_snapshot_capture_cannot_adopt_a_new_owner_or_explicit_replan(self):
        for action in ('finished_owner', 'explicit_replan'):
            with self.subTest(action=action):
                case = self.failed_plan(action.replace('_', ''))
                actual = planner_inputs._retry_snapshot
                newer, fired = [], []
                def snapshot(conn, task_id):
                    if not fired:
                        fired.append(True)
                        if action == 'finished_owner': self.new_successful_owner(case)
                        else: self.explicit_replan(case)
                        newer.append(dict(db.require_task(case.conn, case.task_id)))
                    return actual(conn, task_id)
                with patch.object(planner_inputs, '_retry_snapshot', side_effect=snapshot):
                    response = self.retry(case)
                self.assertEqual(response['reason'], 'planning_state_changed')
                self.assertEqual(dict(db.require_task(case.conn, case.task_id)), newer[0])
                self.assertIsNone(db.active_request_for_key(case.conn, 'plan:' + case.task_id))

    def test_wanted_candidate_that_became_ready_before_submit_is_not_prepared_again(self):
        case = self.failed_plan()
        self.new_successful_owner(case)
        before = dict(db.require_task(case.conn, case.task_id))
        response = self.retry(case, cli_run=lambda *args: self.fail('ready candidate must not prepare or submit'))
        self.assertEqual(response['reason'], 'planning_state_changed')
        self.assertEqual(dict(db.require_task(case.conn, case.task_id)), before)
