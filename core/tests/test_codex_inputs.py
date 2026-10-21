"""Codex input and retry gates: disposable Git/SQLite and local mailboxes only."""
import copy
import datetime
import os
import subprocess
from unittest import mock

from kanpilot import (cli, cmd_dispatch, cmd_exec, cmd_queue, codex_execution,
                      core, db, derive, execution_contract, git_delivery,
                      process_owner, round_store, runtime_client, runtime_operations, util)
from test_session import HeadlessCase, pi_event


class CodexInputsTests(HeadlessCase):
    def setUp(self):
        super().setUp()
        fixture=self.make_ready_task(task_id='t')
        self.fake.tasks['t']['status']='in-progress'
        self.ctx=cli.Context(self.db_path); self.addCleanup(self.ctx.close)
        self.repo=self.repos_root/'Feedback'
        self.worktree=self.root/'execution'
        self.git('worktree','add','--detach',str(self.worktree),'HEAD')
        (self.worktree/'partial.txt').write_text('preserve implementation\n')
        self.config={'backend':'codex',**codex_execution.DEFAULT_CONFIG,'sandbox':'workspace-write','execution_question_max_wait_seconds':0}
        self.scope=runtime_client.stable_scope_id('execution',task_id='t',run_id='r')
        self.directory=self.runs_root/'t'/'r'; self.directory.mkdir(parents=True)
        self.spec={'v':1,'task_id':'t','request_id':'request','run_id':'r','backend':'codex','scope_id':self.scope,
                   'config':self.config,'cwd':str(self.worktree),'repository':str(self.repo),
                   'plan_path':str(fixture['plan_path']),'plan_sha256':util.sha256_file(fixture['plan_path']),
                   'completion_policy':'command_verification','required_verification_commands':['python3 -V'],
                   'instruction':'Frozen task','runs_dir':str(self.directory),'db_path':str(self.db_path),'timeout_seconds':30}
        execution_contract.freeze_spec(self.spec,requirements_sha256=fixture['requirements_sha256'])
        (self.directory/'spec.json').write_text(util.jdump(self.spec))
        self.job=execution_contract.attach_to_job(self.spec,{
            'task_id':'t','run_id':'r','request_id':'request','backend':'codex','scope_id':self.scope,'config':self.config,
            'status':'running','session_id':'thread','turn_id':'codex:r:1','send_state':'sent',
            'plan_sha256':self.spec['plan_sha256'],'spec_sha256':util.sha256_file(self.directory/'spec.json'),
            'worktree_path':str(self.worktree),'pid':os.getpid(),'worker_birth':process_owner.process_birth(os.getpid()),
            'turns':[{'turn_id':'codex:r:1','status':'completed','pid':99999999,'birth':{'started_seconds':0,'started_microseconds':0},
                      'exit_code':0,'ended_at':util.now_iso(),'cli_terminal':True}],
            'started_at':util.now_iso(),'exit_code':None,'cli_terminal':False})
        now=self.ctx.now_iso
        db.update_task(self.ctx.conn,'t',now,status='running',stage='monitor',run_id='r',session_id='thread',sent=1,send_state='sent',sent_at=now,lock_retained=1,session_marker='t/r')
        db.set_extra(self.ctx.conn,self.task(),now,plan_execution_hold=False,delivery={'mode':'manual'})
        core.update_automation(self.ctx.conn,self.task(),now,manual_takeover=True)
        round_store.upsert_on_claim(self.ctx.conn,'t','r',self.config,now,backend='codex')
        round_store.mark_started(self.ctx.conn,'t','r',now,pid=os.getpid(),worktree_path=str(self.worktree))
        self.ctx.conn.execute("UPDATE execution_rounds SET session_id='thread' WHERE task_id='t' AND run_id='r'")
        self.save_job()
        self.make_run('t','r',job=self.job,events=[pi_event(1,'session',session_id='thread',turn_id='codex:r:1')])

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True,env=git_delivery.git_env()).stdout.decode().strip()

    def task(self): return db.require_task(self.ctx.conn,'t')

    def save_job(self): (self.directory/'job.json').write_text(util.jdump(self.job))

    def question(self):
        self.job.update(status='waiting_input',pending_source_sha256=git_delivery.source_state_sha256(self.worktree),
                        pending_input={'requestId':'ask','toolCallId':'tool','turnId':'codex:r:1','questions':[{'title':'Choose','options':['A','B']}]})
        self.save_job()
        self.report={'task_id':'t','run_id':'r','session_id':'thread','turn_id':'codex:r:1','request_id':'ask','tool_call_id':'tool','state':'open','questions':[{'title':'Choose','options':['A','B']}]}
        return self.call('report-execution-question',self.report)['execution_question']

    def answer(self,q,operation='answer'):
        return {'task_id':'t','run_id':'r','request_id':'ask','questions_sha256':q['questions_sha256'],'operation_id':operation,
                'answers':[{'question_id':'q0','option_ids':['o0'],'text':'','skipped':False}]}

    def consume(self,operation='answer'):
        path=self.directory/('answer-'+util.sha256_text('ask')+'.json')
        self.job['turns'][0].update(input_resolution='answered',answer_operation_id=operation,answer_sha256=util.sha256_file(path),answer_consumed_at=util.now_iso())
        self.job.update(pending_input=None,status='running'); self.save_job()

    def settle(self,status='failed'):
        self.job.update(status=status,exit_code=3,cli_terminal=True,ended_at=util.now_iso(),pid=99999998,
                        worker_birth={'started_seconds':0,'started_microseconds':0},pending_input=None)
        self.save_job()
        self.write_events('t','r',[pi_event(1,'session',session_id='thread',turn_id='codex:r:1'),pi_event(2,'terminal',outcome='failed',reason='fixture')])

    def test_mailbox_receipt_precedes_delivery_and_consumption_closes_original_question(self):
        q=self.question()
        with mock.patch('kanpilot.execution_questions.ensure_observer',return_value={'status':'observing'}):
            result=self.call('submit-execution-answers',self.answer(q))
        self.assertEqual(result['execution_question']['delivery'],'accepted')
        self.assertEqual(result['execution_question']['state'],'open')
        receipt=runtime_operations.get_operation(self.ctx.conn,self.scope,'answer')
        self.assertEqual((receipt['harness_id'],receipt['tool'],receipt['status']),('codex','codex_execution_input','accepted'))
        self.consume()
        result=self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
        self.assertEqual(result['execution_question']['state'],'answered')
        self.assertEqual(result['execution_question']['answered_by'],'kaneopilot')
        self.assertEqual(self.task()['status'],'running')

    def test_disappearance_wrong_operation_or_hash_never_proves_answer_received(self):
        q=self.question()
        with mock.patch('kanpilot.execution_questions.ensure_observer',return_value={}):
            self.call('submit-execution-answers',self.answer(q))
        self.job.update(pending_input=None,status='running'); self.save_job()
        self.assertEqual(self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})['execution_question']['state'],'open')
        self.consume('wrong-operation')
        self.assertEqual(self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})['execution_question']['state'],'open')
        self.job['turns'][0].update(answer_operation_id='answer',answer_sha256='0'*64); self.save_job()
        self.assertEqual(self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})['execution_question']['state'],'open')
        self.assertEqual(self.call_err('report-execution-question',{**self.report,'state':'closed'}).code,'conflict')

    def test_unknown_answer_is_frozen_and_never_replayed(self):
        q=self.question()
        with mock.patch.object(codex_execution.InputClient,'_mailbox',side_effect=util.KanpilotError('conflict','unknown')) as write, mock.patch('kanpilot.execution_questions.ensure_observer',return_value={}):
            self.assertEqual(self.call('submit-execution-answers',self.answer(q))['status'],'unknown')
            self.call('submit-execution-answers',self.answer(q))
            self.assertEqual(self.call_err('submit-execution-answers',self.answer(q,'new')).code,'conflict')
            self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
            self.assertEqual(write.call_count,1)

    def test_stop_without_question_requires_persistent_terminal_and_both_processes_dead(self):
        self.assertIn('stop_execution',derive.allowed_actions(self.ctx.conn,self.task(),self.ctx.now))
        payload={'task_id':'t','run_id':'r','operation_id':'stop'}
        result=self.call('stop-execution',payload)
        self.assertEqual(result['status'],'unknown')
        receipt=runtime_operations.get_operation(self.ctx.conn,self.scope,'stop')
        self.assertEqual(receipt['status'],'accepted')
        self.assertEqual(self.task()['status'],'running')
        self.assertEqual(self.call_err('stop-execution',{**payload,'operation_id':'other'}).code,'conflict')
        self.settle('aborted')
        self.job['turns'][0].update(pid=os.getpid(),birth=process_owner.process_birth(os.getpid())); self.save_job()
        self.assertNotEqual(self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})['status'],'stopped')
        self.job['turns'][0].update(pid=99999999,birth={'started_seconds':0,'started_microseconds':0}); self.save_job()
        result=self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(self.task()['failure_code'],'execution_failed')

    def test_wrong_scope_or_source_drift_reject_answer_but_allows_stop(self):
        q=self.question()
        (self.worktree/'partial.txt').write_text('changed while waiting')
        with mock.patch('kanpilot.execution_questions.ensure_observer',return_value={}):
            self.assertEqual(self.call('submit-execution-answers',self.answer(q))['status'],'unknown')
        self.assertFalse((self.directory/('answer-'+util.sha256_text('ask')+'.json')).exists())
        self.assertEqual(self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'})['status'],'unknown')
        self.job['scope_id']='wrong'; self.save_job()
        self.assertEqual(self.call_err('reconcile-execution-question',{'task_id':'t','run_id':'r'}).code,'conflict')

    def test_retry_requires_terminal_birth_evidence_and_preserves_worktree_in_new_run(self):
        self.settle()
        db.update_task(self.ctx.conn,'t',self.ctx.now_iso,status='needs_user',failure_code='execution_failed',failure_stage='running')
        payload={'task_id':'t','action':'resume','expected_execution_run_id':'r'}
        missing=copy.deepcopy(self.job); self.job.pop('worker_birth'); self.save_job()
        self.assertEqual(self.call_err('resolve',payload).code,'conflict')
        self.job=missing; self.save_job()
        result=cmd_queue.cmd_resolve(self.ctx,payload)
        claimed=cmd_exec.cmd_claim(self.ctx,{'stage':'dispatch','executor':'test','request_id':result['request']['request_id']})
        self.assertTrue(claimed['claimed']); self.assertNotEqual(claimed['run_id'],'r')
        path,mode=cmd_dispatch._ensure_worktree(self.task(),'t',claimed['run_id'],self.ctx.conn,require_isolated=True)
        self.assertEqual(path,self.worktree)
        self.assertEqual((path/'partial.txt').read_text(),'preserve implementation\n')
        self.assertTrue((self.directory/'job.json').exists())

    def test_abort_without_prior_stop_receipt_is_rejected(self):
        self.assertEqual(self.call_err('stop-execution',{'task_id':'t','run_id':'r','operation_id':'abort','abort':True}).code,'conflict')
        self.assertFalse(list(self.directory.glob('stop-*.json')))

    def test_question_payload_must_match_persistent_pending_input(self):
        self.question()
        self.assertEqual(self.call_err('report-execution-question',{**self.report,'request_id':'forged'}).code,'conflict')
        self.assertEqual(self.call_err('report-execution-question',{**self.report,'questions':[{'title':'changed','options':[]}]}).code,'conflict')

    def test_missing_stop_receipt_cannot_confirm_a_stop_and_terminal_is_not_new_activity(self):
        self.settle('aborted')
        self.assertEqual(self.call_err('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'}).code,'conflict')
        db.set_extra(self.ctx.conn,self.task(),self.ctx.now_iso,execution_stop={'run_id':'r','session_id':'thread','operation_id':'missing','reason':'user_stop'})
        self.assertNotEqual(self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})['status'],'stopped')
        self.assertEqual(self.task()['status'],'running')

    def test_explicit_abort_after_grace_can_settle_an_unknown_stop(self):
        self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'})
        stop=db.task_extra(self.task())['execution_stop']
        stop['requested_at']=util.to_iso(util.now_dt()-datetime.timedelta(seconds=11))
        db.set_extra(self.ctx.conn,self.task(),self.ctx.now_iso,execution_stop=stop)
        runtime_operations.mark_operation_unknown(self.ctx.conn,self.scope,'stop')
        result=self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'abort','abort':True})
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(runtime_operations.get_operation(self.ctx.conn,self.scope,'abort')['status'],'accepted')
        self.settle('aborted')
        self.assertEqual(self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})['status'],'stopped')

    def test_codex_cannot_manually_override_verification_in_legacy_or_shadow(self):
        self.settle()
        db.update_task(self.ctx.conn,'t',self.ctx.now_iso,status='needs_user',failure_code='verification_unconfirmed',failure_stage='monitor')
        for mode in ('legacy','shadow'):
            with self.subTest(mode=mode):
                self.call('set-project-engine-mode',{'project_id':self.task()['project_id'],'engine_mode':mode})
                before=dict(self.task())
                count=self.ctx.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
                self.assertNotIn('accept_verification',derive.allowed_actions(self.ctx.conn,self.task(),self.ctx.now))
                error=self.call_err('resolve',{'task_id':'t','action':'accept_verification','note':'skip'})
                self.assertEqual(error.code,'conflict')
                self.assertIn('Codex',error.message)
                self.assertEqual(dict(self.task()),before)
                self.assertNotIn('verification_override',db.task_extra(self.task()))
                self.assertEqual(self.ctx.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0],count)
