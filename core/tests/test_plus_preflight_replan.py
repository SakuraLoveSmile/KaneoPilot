"""Exact pre-runtime Plus limit failure; isolated HOME/SQLite/Git only."""
import copy
import uuid
from pathlib import Path
from unittest import mock
from kanpilot import (cli, cmd_queue, constants, core, db, execution_contract, git_delivery,
                      plus_preflight_replan, process_owner, remote, round_store,
                      runtime_client, runtime_operations, util)
from test_session import HeadlessCase, pi_event, write_json, write_jsonl


class PlusPreflightReplanTests(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.make_ready_task(task_id='t')
        self.ctx = cli.Context(self.db_path); self.addCleanup(self.ctx.close)
        self.conn, self.stamp = self.ctx.conn, self.ctx.now_iso
        self.fake.tasks['t']['status'] = 'in-progress'
        self.repo, self.worktree = self.repos_root / 'Feedback', self.root / 'execution'
        git_delivery._git(['worktree', 'add', '-b', 'kaneo/t', str(self.worktree), 'HEAD'], self.repo, 30)
        head = git_delivery._value(['rev-parse', 'HEAD'], self.repo)
        plan = Path(self.task()['plan_path']); command = 'python3 -c ' + 'x' * 4100
        plan.write_text('# Frozen oversized command\n\n```verify\n' + command + '\n```\n')
        db.update_task(self.conn, 't', self.stamp, status='needs_user', stage='dispatch', failure_stage='submitting',
            failure_code='dispatch_failed', send_state='unknown', sent=None, run_id='r', session_id=None,
            plan_sha256=util.sha256_file(plan))
        db.set_extra(self.conn, self.task(), self.stamp, plan_execution_hold=False,
            automation={'plan_wanted': False, 'manual_takeover': False}, delivery={'mode': 'pr', 'branch': 'kaneo/t',
                'worktree_path': str(self.worktree), 'head_sha': head, 'base_sha': head, 'base_branch': 'main'})
        request, _ = db.create_request(self.conn, self.stamp, 'dispatch', 'failed-dispatch', task_id='t')
        self.request_id = request['request_id']
        db.update_request(self.conn, self.request_id, self.stamp, status='failed', run_id='r', finished_at=self.stamp)
        self.directory = self.runs_root / 't' / 'r'; self.directory.mkdir(parents=True)
        self.instance = str(uuid.uuid4()); self.pid = 99999998
        config = {'backend': 'pi_desktop', 'connection_file': str(self.root / 'plus.json')}
        self.spec = dict(v=1, backend='pi_desktop', task_id='t', run_id='r', request_id=self.request_id,
            config=config, repository=str(self.repo), cwd=str(self.worktree), runs_dir=str(self.directory),
            plan_path=str(plan), plan_sha256=self.task()['plan_sha256'], instruction='Frozen instruction',
            completion_policy='command_verification', required_verification_commands=[command], executor_instance_id=self.instance,
            db_path=str(self.db_path))
        execution_contract.freeze_spec(self.spec, requirements_sha256=self.task()['requirements_sha256'])
        self.job = execution_contract.attach_to_job(self.spec, dict(backend='pi_desktop', task_id='t', run_id='r',
            request_id=self.request_id, config=config, pid=self.pid, status='failed', exit_code=1, send_state='not_sent',
            session_id=None, turn_id=None, pi_desktop_pid=None, completion_policy='command_verification',
            plan_sha256=self.task()['plan_sha256'], detail='verification_requirements_invalid: command too large',
            started_at=self.stamp, ended_at=self.stamp))
        write_json(self.directory / 'spec.json', self.spec); self.save_job()
        write_jsonl(self.directory / 'events.jsonl', [pi_event(1, 'terminal', outcome='failed', reason='verification_requirements_invalid')])
        round_store.upsert_on_claim(self.conn, 't', 'r', config, self.stamp, backend='pi_desktop')
        round_store.mark_started(self.conn, 't', 'r', self.stamp, pid=self.pid, worktree_path=str(self.worktree))
        self.conn.execute("UPDATE execution_rounds SET run_status='failed' WHERE task_id='t' AND run_id='r'")
        self.worker = dict(pid=self.pid, task_id='t', run_id='r', spec_path=str(self.directory / 'spec.json'),
                           started_seconds=100, started_microseconds=20)
        self.register()
        self.payload = dict(task_id='t', action='replan', expected_execution_run_id='r')
        self.scope = runtime_client.stable_scope_id('execution', task_id='t', run_id='r')

    def register(self):
        db.set_setting(self.conn, process_owner.SETTING, {self.instance: {'workers': [self.worker]}})

    def task(self):
        return db.require_task(self.conn, 't')

    def save_job(self):
        write_json(self.directory / 'job.json', self.job)

    def reject(self, payload=None):
        before = dict(self.task()); requests = [dict(r) for r in self.conn.execute('SELECT * FROM requests')]
        with self.assertRaises(util.KanpilotError):
            cmd_queue.cmd_resolve(self.ctx, payload or self.payload)
        self.assertEqual(dict(self.task()), before)
        self.assertEqual([dict(r) for r in self.conn.execute('SELECT * FROM requests')], requests)

    def test_exact_proof_replans_without_rewriting_failed_records_or_starting_anything(self):
        writes = self.conn.total_changes
        self.assertEqual(plus_preflight_replan.prove(self.conn, self.task(), 'r')['request_id'], self.request_id)
        self.assertEqual(self.conn.total_changes, writes)
        files = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        request = dict(db.get_request(self.conn, self.request_id)); old_round = dict(round_store.get_round(self.conn, 't', 'r'))
        result = cmd_queue.cmd_resolve(self.ctx, self.payload)
        self.assertEqual(result['task_status'], 'needs_plan'); self.assertIsNone(result['request'])
        self.assertIsNone(self.task()['send_state']); self.assertEqual(self.task()['sent'], 0)
        self.assertEqual(core.delivery_of(self.task())['mode'], 'pr')
        self.assertEqual(core.automation_of(self.task())['replan_remote_status'], 'in-progress')
        self.assertEqual(dict(db.get_request(self.conn, self.request_id)), request)
        self.assertEqual(dict(round_store.get_round(self.conn, 't', 'r')), old_round)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, files)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='plus_preflight_replan_confirmed'").fetchone()[0], 1)

    def test_explicit_current_user_replan_only(self):
        for change in ({'expected_execution_run_id': 'old'}, {'expected_execution_run_id': None}, {'actor': 'automation'},
                       {'action': 'resume'}, {'action': 'retry'}):
            with self.subTest(change=change): self.reject(dict(self.payload, **change))

    def test_tampered_identity_or_any_send_activity_rejects(self):
        original = copy.deepcopy(self.job)
        for change in ({'request_id': 'other'}, {'plan_sha256': '0'*64}, {'config': {}}, {'status': 'running'},
            {'exit_code': 0}, {'send_state': 'unknown'}, {'session_id': 's'}, {'turn_id': 'turn'}, {'scope_id': self.scope},
            {'turns': [{}]}, {'pending_input': True}, {'ended_at': None}, {'detail': 'other_failure'}):
            with self.subTest(change=change):
                self.job = dict(copy.deepcopy(original), **change); self.save_job(); self.reject()

    def test_missing_live_or_unreadable_birth_rejects_and_reused_pid_is_distinct(self):
        db.set_setting(self.conn, process_owner.SETTING, {}); self.reject(); self.register()
        birth = {k: self.worker[k] for k in ('started_seconds', 'started_microseconds')}
        with mock.patch.object(process_owner, 'process_birth', return_value=birth): self.reject()
        with mock.patch.object(process_owner, 'process_birth', return_value=None), mock.patch.object(util, 'pid_alive', return_value=True): self.reject()
        with mock.patch.object(process_owner, 'process_birth', return_value=dict(birth, started_seconds=101)):
            self.assertEqual(plus_preflight_replan.prove(self.conn, self.task(), 'r')['run_id'], 'r')

    def test_incomplete_or_additional_event_activity_rejects(self):
        path = self.directory / 'events.jsonl'; original = path.read_bytes()
        for data in (original.rstrip(), b'{}\n', original+original, original+b'{"seq":2,"type":"session"}\n'):
            with self.subTest(data=data[:25]): path.write_bytes(data); self.reject()

    def test_any_runtime_operation_or_grant_rejects(self):
        for status in ('prepared', 'unknown', 'accepted'):
            with self.subTest(status=status):
                runtime_operations.begin_operation(self.conn, self.scope, status, 'plus', 'session_create', {}, now=self.stamp)
                self.conn.execute('UPDATE runtime_operations SET status=? WHERE scope_id=?', (status, self.scope)); self.reject()
                self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=?', (self.scope,))
        grant = constants.app_support() / 'mcp' / 'grants' / (self.scope + '_runtime.json')
        grant.parent.mkdir(parents=True, exist_ok=True); grant.write_text('{}'); self.reject()

    def test_plan_source_remote_and_active_owner_drift_rejects(self):
        plan = Path(self.task()['plan_path']); raw = plan.read_bytes()
        plan.write_bytes(raw+b'\nchanged'); self.reject(); plan.write_bytes(raw)
        changed = self.worktree / 'changed.py'; changed.write_text('changed'); self.reject(); changed.unlink()
        old = self.fake.tasks['t']['description']; self.fake.tasks['t']['description']='changed'; self.reject(); self.fake.tasks['t']['description']=old
        self.fake.tasks['t']['status']='done'; self.reject(); self.fake.tasks['t']['status']='in-progress'
        db.create_request(self.conn, self.stamp, 'monitor', 'active', task_id='t'); self.reject()

    def test_remote_io_evidence_drift_is_rechecked_before_writes(self):
        original = remote.get_task
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs); self.job['detail'] += ' changed'; self.save_job(); return result
        with mock.patch.object(remote, 'get_task', side_effect=mutate): self.reject()

    def test_spec_database_identity_is_not_inferred_from_the_worker_record(self):
        self.spec['db_path'] = str(self.root / 'another.db')
        write_json(self.directory / 'spec.json', self.spec)
        self.reject()
