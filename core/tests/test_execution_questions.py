"""Execution ask relay: isolated DB, fake runtime boundary; no production calls."""
from unittest import mock
import datetime
from helpers import KanpilotTestCase
from kanpilot import cli, db, util


class ExecutionQuestionsTests(KanpilotTestCase):
    def scene(self):
        from kanpilot import round_store, runtime_client
        self.call('migrate')
        self.make_repo('test')
        self.fake.add_project('p', 'test')
        task = self.fake.add_task('t', 'p')
        self.sync_task(task, project_name='test')
        self.run_dir = self.runs_root / 't' / 'r'
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.config = {'backend':'pi_desktop','connection_file':str(self.root/'control.json'), 'provider_id':'p','model_id':'m','thinking_level':'low','permission_mode':'auto'}
        self.job = {'scope_id':runtime_client.stable_scope_id('execution',task_id='t',run_id='r'),'task_id':'t','run_id':'r','backend':'pi_desktop','status':'running','session_id':'s','turn_id':'turn','send_state':'sent','config':self.config}
        (self.run_dir/'job.json').write_text(util.jdump(self.job))
        (self.run_dir/'spec.json').write_text(util.jdump({'task_id':'t','run_id':'r','cwd':str(self.repos_root/'test'),'config':self.config}))
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET run_id='r',status='running',sent=1,send_state='sent',extra_json=? WHERE task_id='t'", (util.jdump({'automation':{'segment_started_at':'2026-10-14T00:00:00Z'}}),))
            round_store.upsert_discovered(conn, 't','r',util.now_iso(),backend='pi_desktop',run_status='running')
            conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id='t' AND run_id='r'", (util.jdump(self.config),))
            from kanpilot import runtime_operations
            runtime_operations.begin_operation(conn,self.job['scope_id'],'create','pi_desktop','kp_runtime_session_create',{},util.now_iso())
            runtime_operations.complete_operation(conn,self.job['scope_id'],'create',result={'session_id':'s'},session_id='s')
        conn.close()
        self.payload = {'task_id':'t','run_id':'r','session_id':'s','turn_id':'turn','request_id':'ask','tool_call_id':'tool','state':'open','questions':[{'title':'Choose','options':[{'label':'A'},{'label':'B'}],'allowCustom':True}]}
        return self.call('report-execution-question', self.payload)['execution_question']

    def answer(self, q, **extra):
        return {'task_id':'t','run_id':'r','request_id':'ask','questions_sha256':q['questions_sha256'],'answers':[{'question_id':'q0','option_ids':['o0'],'text':'','skipped':False}],'operation_id':'op',**extra}

    def test_open_draft_and_single_select_mutual_exclusion(self):
        q = self.scene()
        self.assertEqual(q['state'],'open')
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['allowed_actions'], ['answer_execution_questions','skip_execution_questions','stop_execution'])
        inbox=self.call('attention-list')['items'][0]
        self.assertEqual(inbox['reason_code'],'execution_question')
        self.assertEqual(inbox['question_preview'],{'request_id':'ask','count':1,'first_title':'Choose'})
        self.assertEqual(inbox['recommended'],'answer_execution_questions')
        payload = self.answer(q); payload.pop('run_id'); payload.pop('operation_id')
        self.call('save-execution-answers',payload)
        payload['answers'][0]['text']='custom'
        self.assertEqual(self.call_err('save-execution-answers',payload).code,'bad_request')
        for command, values in [('resolve',{'task_id':'t','action':'replan'}), ('archive-task',{'task_id':'t'}), ('trash-task',{'task_id':'t'}), ('enqueue',{'task_id':'t','kind':'dispatch'})]:
            with self.subTest(command=command):
                self.assertEqual(self.call_err(command,values).code,'conflict')

    def test_overlapping_wait_intervals_and_segment_intersection(self):
        from kanpilot import execution_questions as eq
        q = self.scene()
        ctx = cli.Context(self.db_path)
        base = util.parse_ts('2026-10-14T00:00:00Z')
        with db.write_txn(ctx.conn):
            task=db.require_task(ctx.conn,'t'); extra=db.task_extra(task)
            extra['execution_input_wait_intervals']=[{'run_id':'r','start':util.to_iso(base),'end':util.to_iso(base+datetime.timedelta(hours=6))}]
            db.set_extra(ctx.conn,task,ctx.now_iso,**extra)
        task=db.require_task(ctx.conn,'t')
        self.assertEqual(eq.wait_seconds(task,base+datetime.timedelta(hours=8),run_id='r'),21600)
        self.assertEqual(eq.wait_seconds(task,base+datetime.timedelta(hours=8),since=base+datetime.timedelta(hours=3)),10800)
        ctx.close()

    def fake_client(self, *args, **kwargs):
        client=mock.Mock()
        client.harness_pid=100
        client.agent_status.return_value={'currentTurnId':'turn','isRunning':True}
        client.turn_get.return_value={'sessionId':'s','turnId':'turn','status':'running'}
        client.input_pending.return_value={'sessionId':'s','asks':[{'requestId':'ask'}]}
        client.input_respond.return_value={'session_id':'s','request_id':'ask','status':'answered'}
        return client,self.job

    def test_unknown_pending_freezes_answer_and_only_same_explicit_retry(self):
        from kanpilot import execution_questions as eq
        q=self.scene(); client,_=self.fake_client()
        client.input_respond.side_effect=util.KanpilotError('remote_unavailable','lost')
        with mock.patch.object(eq,'_client',return_value=(client,self.job)), mock.patch.object(eq,'ensure_observer',return_value={'status':'observing'}):
            result=self.call('submit-execution-answers',self.answer(q))
            self.assertEqual(result['execution_question']['delivery'],'unknown')
            self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
            self.assertEqual(client.input_respond.call_count,1)
            self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['execution_question']['delivery'],'unknown')
            changed=self.answer(q,operation_id='different');changed['answers'][0]['option_ids']=['o1']
            self.assertEqual(self.call_err('submit-execution-answers',changed).code,'conflict')
            self.call('submit-execution-answers',self.answer(q))
            self.assertEqual(client.input_respond.call_count,1)
            client.input_respond.side_effect=None
            result=self.call('submit-execution-answers',self.answer(q,operation_id='retry'))
            self.assertEqual(result['execution_question']['state'],'answered')
            self.assertEqual(result['execution_question']['answered_by'],'kaneopilot')
            self.assertEqual(client.input_respond.call_count,2)
            self.call('submit-execution-answers',self.answer(q,operation_id='retry'))
            self.assertEqual(client.input_respond.call_count,2)

    def test_unknown_then_plus_disappearance_is_unknown_source(self):
        from kanpilot import execution_questions as eq
        q=self.scene();client,_=self.fake_client()
        client.input_respond.side_effect=util.KanpilotError('remote_unavailable','lost')
        with mock.patch.object(eq,'_client',return_value=(client,self.job)),mock.patch.object(eq,'ensure_observer',return_value={}):
            self.call('submit-execution-answers',self.answer(q))
            client.input_pending.return_value={'sessionId':'s','asks':[]}
            result=self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
            self.assertEqual(result['execution_question']['answered_by'],'unknown')
            self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['status'],'running')

    def test_terminal_evidence_precedes_disappearance_and_keeps_draft(self):
        from kanpilot import execution_questions as eq
        q=self.scene();client,_=self.fake_client()
        payload=self.answer(q);payload.pop('operation_id');payload.pop('run_id')
        self.call('save-execution-answers',payload)
        client.input_pending.return_value={'sessionId':'s','asks':[]}
        client.turn_get.return_value={'sessionId':'s','turnId':'turn','status':'completed'}
        with mock.patch.object(eq,'_client',return_value=(client,self.job)):
            result=self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
        self.assertEqual(result['execution_question']['state'],'expired')
        self.assertEqual(result['execution_question']['draft'],payload['answers'])
        task=self.call('get-task',{'task_id':'t'})['task']
        self.assertEqual(task['failure_code'],'execution_failed')
        self.assertNotEqual(task['allowed_actions'],['answer_execution_questions','skip_execution_questions','stop_execution'])

    def test_two_questions_fifo_union_and_one_close_does_not_unblock(self):
        from kanpilot import execution_questions as eq
        q=self.scene()
        self.call('report-execution-question',{**self.payload,'request_id':'z','tool_call_id':'tool2'})
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['execution_question']['queued'],1)
        self.call('report-execution-question',{**{k:v for k,v in self.payload.items() if k!='questions'},'state':'closed'})
        task=self.call('get-task',{'task_id':'t'})['task']
        self.assertEqual(task['status'],'needs_user')
        self.assertEqual(task['execution_question']['request_id'],'z')
        self.assertEqual(len(task['extra']['execution_input_wait_intervals']),1)
        self.assertIsNone(task['extra']['execution_input_wait_intervals'][0]['end'])
        self.call('report-execution-question',{**{k:v for k,v in self.payload.items() if k!='questions'},'request_id':'z','tool_call_id':'tool2','state':'closed'})
        task=self.call('get-task',{'task_id':'t'})['task']
        self.assertEqual(task['status'],'running')
        self.assertIsNotNone(task['extra']['execution_input_wait_intervals'][0]['end'])
        self.assertEqual(self.call('report-execution-question',self.payload)['execution_question']['state'],'answered')

    def test_incomplete_draft_allowed_submit_rejects_and_oversize_blocked(self):
        from kanpilot import execution_questions as eq
        q=self.scene();draft=[{'question_id':'q0','option_ids':[],'text':'','skipped':False}]
        self.call('save-execution-answers',{'task_id':'t','request_id':'ask','questions_sha256':q['questions_sha256'],'answers':draft})
        with mock.patch.object(eq,'_client',side_effect=self.fake_client):
            self.assertEqual(self.call_err('submit-execution-answers',self.answer(q,answers=draft)).code,'bad_request')
        result=self.call('report-execution-question',{**self.payload,'request_id':'large','questions':[{'question':'x'*65536}]})
        task=self.call('get-task',{'task_id':'t'})['task']
        large=next(q for q in task['extra']['execution_questions'] if q['request_id']=='large')
        self.assertFalse(large['answerable'])
        self.assertEqual(task['status'],'needs_user')

    def test_late_receipt_cannot_overwrite_new_operation_or_closed_request(self):
        from kanpilot import execution_questions as eq
        q=self.scene();client,_=self.fake_client()
        def late(*args,**kwargs):
            # Another read-only observer settles Plus UI's answer first.
            self.call('report-execution-question',{**{k:v for k,v in self.payload.items() if k!='questions'},'state':'closed'})
            return {'status':'answered'}
        client.input_respond.side_effect=late
        with mock.patch.object(eq,'_client',return_value=(client,self.job)),mock.patch.object(eq,'ensure_observer',return_value={}):
            result=self.call('submit-execution-answers',self.answer(q))
        self.assertEqual(result['execution_question']['state'],'answered')
        self.assertEqual(result['execution_question']['delivery'],'unknown')
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['status'],'running')

    def test_size_limits_and_label_order_preserved(self):
        from kanpilot import execution_questions as eq
        q=self.scene()
        questions=eq.normalize_questions([{'question':'many','multiSelect':True,'options':[' A ','B']}])
        q['questions']=questions
        draft=[{'question_id':'q0','option_ids':['o1','o0'],'text':'custom','skipped':False}]
        answers,wire=eq.normalize_answers(q,draft)
        self.assertEqual(answers[0]['option_ids'],['o0','o1'])
        self.assertEqual(wire,[[' A ','B','custom']])
        with self.assertRaisesRegex(util.KanpilotError,'1 MiB'):
            eq.validate_wire_answers([['x'*(1024*1024)]],'s','r','t')
        # A near 1MiB canonical answer can still exceed the 2MiB full envelope
        # through non-canonical encoding overhead; full envelope is checked too.
        with self.assertRaises(util.KanpilotError):
            eq.validate_wire_answers([['x']*101],'s','r','t')

    def test_observer_launch_reservation_atomic(self):
        from kanpilot import cmd_dispatch
        self.scene()
        calls=[]
        def launch(ctx,payload):
            calls.append(payload)
            other=cli.Context(self.db_path)
            try:
                result=cmd_dispatch.cmd_round_observe(other,payload)
                self.assertEqual(result['status'],'starting')
                self.assertTrue(result['deduplicated'])
            finally:other.close()
            return {'status':'observing','pid':123}
        with mock.patch.object(cmd_dispatch,'_round_observe_reserved',launch):
            result=self.call('round-observe',{'task_id':'t','run_id':'r'})
        self.assertEqual(result['status'],'observing')
        self.assertEqual(len(calls),1)
        self.assertIsNone(self.call('get-task',{'task_id':'t'})['task']['extra']['observer_launch'])

    def test_concurrent_observer_ensure_launches_one_pid(self):
        import threading
        from kanpilot import cmd_dispatch,execution_questions as eq
        self.scene();entered=threading.Event();release=threading.Event();calls=[];results=[]
        def launch(ctx,payload):
            calls.append(payload)
            entered.set()
            self.assertTrue(release.wait(5))
            return {'status':'observing','pid':123}
        def ensure():
            ctx=cli.Context(self.db_path)
            try:results.append(eq.ensure_observer(ctx,'t','r'))
            finally:ctx.close()
        with mock.patch.object(cmd_dispatch,'_round_observe_reserved',side_effect=launch):
            thread=threading.Thread(target=ensure);thread.start()
            self.assertTrue(entered.wait(5))
            try:ensure()
            finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls),1)
        self.assertEqual(sorted(r['status'] for r in results),['observing','starting'])
        self.assertEqual([r['pid'] for r in results if r['status']=='observing'],[123])

    def test_direct_core_queue_commands_cannot_bypass_active_question_guard(self):
        from kanpilot import cmd_queue
        self.scene();ctx=cli.Context(self.db_path)
        try:
            for command,payload in [(cmd_queue.cmd_resolve,{'task_id':'t','action':'replan'}),
                                    (cmd_queue.cmd_enqueue,{'task_id':'t','kind':'dispatch'})]:
                with self.assertRaises(util.KanpilotError) as caught:command(ctx,payload)
                self.assertEqual(caught.exception.code,'conflict')
        finally:ctx.close()

    def test_background_recovery_respects_global_stages_and_task_pause(self):
        from kanpilot import cmd_dispatch
        self.scene()
        with mock.patch.object(cmd_dispatch,'_round_observe_reserved',return_value={'status':'observing','pid':123}) as launch:
            initial=self.call('recover-execution-goals')
            self.assertEqual(len(initial['goal_recoveries']),1)
            self.assertFalse(launch.call_args.args[1]['continue_goal'])
            self.assertFalse(launch.call_args.args[1]['continue_goal_only'])
            launch.reset_mock()
            for settings in ({'paused_global':True,'paused_stages':[]},
                             {'paused_global':False,'paused_stages':['dispatch']},
                             {'paused_global':False,'paused_stages':['monitor']}):
                with self.subTest(settings=settings):
                    conn=db.connect(self.db_path)
                    with db.write_txn(conn):
                        for key,value in settings.items():db.set_setting(conn,key,value)
                    conn.close()
                    self.assertEqual(self.call('recover-execution-goals')['goal_recoveries'],[])
            conn=db.connect(self.db_path)
            with db.write_txn(conn):
                db.set_setting(conn,'paused_global',False)
                db.set_setting(conn,'paused_stages',[])
                conn.execute("UPDATE tasks SET auto_advance_paused=1 WHERE task_id='t'")
            conn.close()
            self.assertEqual(self.call('recover-execution-goals')['goal_recoveries'],[])
            self.assertEqual(launch.call_count,0)

    def test_engine_wait_blocks_stale_retry_and_only_recovers_observer(self):
        from kanpilot.engine import loop,decide
        self.scene()
        conn=db.connect(self.db_path)
        with db.write_txn(conn):
            request,_=db.create_request(conn,util.now_iso(),'monitor','old-monitor',task_id='t',requested_by='test')
            conn.execute("UPDATE requests SET status='failed',result_json=? WHERE request_id=?",
                (util.jdump({'failure_code':'remote_unavailable'}),request['request_id']))
        conn.close()
        commands=[]
        def execute(name,payload):
            commands.append((name,payload))
            return {'status':'observing','pid':123}
        result=loop.Reconciler(self.db_path,mode='live',force_mode=True,run_command=execute).reconcile_once()
        self.assertEqual([name for name,_ in commands],['round-observe'])
        self.assertEqual(result['actions'][0]['action'],decide.BLOCK)
        self.assertEqual(result['actions'][0]['reason'],'execution_input_required')
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['status'],'needs_user')

    def test_stop_invalid_frozen_identity_rejects_before_flags(self):
        from kanpilot import execution_questions as eq
        q=self.scene();before=self.call('get-task',{'task_id':'t'})['task']['extra']
        spec=self.run_dir/'spec.json';original=spec.read_text()
        for damaged in (None,'{"task_id":"other","run_id":"r","config":{}}'):
            with self.subTest(spec=damaged):
                if damaged is None: spec.unlink()
                else:spec.write_text(damaged)
                error=self.call_err('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'})
                self.assertIn(error.code,('validation_failed','conflict'))
                self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['extra'],before)
        spec.write_text(original)
        conn=db.connect(self.db_path)
        with db.write_txn(conn):conn.execute("DELETE FROM runtime_operations WHERE tool='kp_runtime_session_create'")
        conn.close()
        self.assertEqual(self.call_err('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'}).code,'forbidden')
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['extra'],before)

    def test_concurrent_submit_one_reservation_without_network_write_lock(self):
        import threading
        from kanpilot import execution_questions as eq
        q=self.scene();client,_=self.fake_client(); entered=threading.Event();release=threading.Event();results=[]
        def downstream(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(5));return {'status':'answered'}
        client.input_respond.side_effect=downstream
        def first():
            try:results.append(self.call('submit-execution-answers',self.answer(q)))
            except Exception as exc:results.append(exc)
        with mock.patch.object(eq,'_client',return_value=(client,self.job)),mock.patch.object(eq,'ensure_observer',return_value={}):
            thread=threading.Thread(target=first);thread.start()
            self.assertTrue(entered.wait(5))
            try:
                # This second caller can enter a transaction while Plus waits.
                error=self.call_err('submit-execution-answers',self.answer(q,operation_id='op2'))
                self.assertEqual(error.code,'conflict')
                self.assertEqual(client.input_respond.call_count,1)
            finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(results[0]['status'],'accepted')

    def test_fast_completed_requires_exact_durable_ask_resolution(self):
        from kanpilot import execution_questions as eq
        q=self.scene();client,_=self.fake_client();client.input_pending.return_value={'sessionId':'s','asks':[]}
        client.turn_get.return_value={'sessionId':'s','turnId':'turn','status':'completed'}
        message={'id':'result','role':'tool_result','turnId':'turn','toolCallId':'tool','toolName':'AskUserQuestion','toolResult':{'details':{'resolutionKind':'answered','answers':[['A']]}}}
        client.session_get.return_value={'session':{'id':'s','messages':[message],'messageStart':0,'messageEnd':1,'hasMoreBefore':False}}
        with mock.patch.object(eq,'_client',return_value=(client,self.job)):
            result=self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
        self.assertEqual(result['execution_question']['state'],'answered')
        self.assertEqual(result['execution_question']['answered_by'],'plus')
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['status'],'running')

    def test_running_waiter_disappearance_without_current_turn_keeps_block(self):
        from kanpilot import execution_questions as eq,pi_desktop_worker
        self.scene();client,_=self.fake_client();client.input_pending.return_value={'sessionId':'s','asks':[]}
        client.agent_status.return_value={'isRunning':False,'currentTurnId':None,'pendingInputs':{'count':0,'requestIds':[]}}
        with mock.patch.object(eq,'_client',return_value=(client,self.job)):
            result=self.call('reconcile-execution-question',{'task_id':'t','run_id':'r'})
        self.assertEqual(result['execution_question']['state'],'open')
        spec={'task_id':'t','run_id':'r','db_path':str(self.db_path),'config':self.config}
        observer=pi_desktop_worker._ExecutionInputObserver(spec,self.job,mock.Mock())
        waiting=observer.observe(client,client.agent_status.return_value,client.turn_get.return_value)
        self.assertTrue(waiting)
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['failure_code'],'execution_input_required')

    def test_six_hours_wait_rebuilt_from_core_not_job_cache_and_new_segment(self):
        from kanpilot import execution_questions as eq,pi_desktop_worker,core
        t0=util.now_dt()
        with mock.patch.object(util,'now_dt',return_value=t0):self.scene()
        spec={'task_id':'t','run_id':'r','db_path':str(self.db_path),'config':self.config}
        job={**self.job,'started_at':util.to_iso(t0),'input_wait_seconds':999999}
        later=t0+datetime.timedelta(hours=6)
        with mock.patch.object(util,'now_dt',return_value=later):
            self.assertAlmostEqual(pi_desktop_worker._round_remaining_seconds(job,60,spec),60,delta=1)
            task=self.call('get-task',{'task_id':'t'})['task']
            value={'run_id':'r','extra_json':task['extra']}
            value['extra_json']['automation']['segment_started_at']=util.to_iso(t0+datetime.timedelta(hours=3))
            value['extra_json']=util.jdump(value['extra_json'])
            budget=core.automation_budget(value,{'max_task_minutes':240},later)
            self.assertEqual(budget['minutes'],0)
            observer=pi_desktop_worker._ExecutionInputObserver(spec,job,mock.Mock())
            self.assertFalse(observer.expired())
        with mock.patch.object(util,'now_dt',return_value=t0+datetime.timedelta(hours=24)):
            self.assertTrue(observer.expired())
        spec['config']['execution_question_max_wait_seconds']=0
        self.assertFalse(observer.expired())
