"""Whole worker observation against real core/MCP and isolated fake Plus.

Only the worker clock and Plus boundary are simulated. Core commands, SQLite
transactions, scope authorization, and stop/abort receipts use the real paths.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

from test_pi_desktop_backend import PiDesktopCase, TASK_ID, RUN_ID, SESSION_ID, TURN_ID
from kanpilot import db, dsh_events, execution_questions, pi_desktop_worker as worker, runtime_client, util


class ExecutionInputWorkerTests(PiDesktopCase):
    def scene(self, *, historical=False, maximum=86400):
        self.seconds = 0.0
        self.base = util.now_dt()
        self.sleeps = []
        self.stop_times = []
        self.abort_times = []
        self.pending_reads = 0
        directory = self.runs_root / TASK_ID / RUN_ID
        cwd = self.root / 'worktree'
        cwd.mkdir()
        spec = self.make_spec(directory, cwd, self.write_spec_plan('Execute plan.'), timeout_seconds=60)
        spec['config']['execution_question_max_wait_seconds'] = maximum
        self.write_spec(spec)
        config = spec['config']
        client = runtime_client.RuntimeClient(role='execution', connection_file=config['connection_file'],
            project_path=str(cwd), runtime_config=config, task_id=TASK_ID, run_id=RUN_ID)
        client.connect()
        client.session_create(str(cwd), mode='goal')
        client.agent_prompt(SESSION_ID, 'Execute plan.')
        job = self.write_lost_observer_scene(spec, directory, status='running')
        if not historical:
            job['scope_id'] = client.scope_id
        job['completion_policy'] = 'command_verification'
        job['goal_approval'] = {'status':'approved', 'execution_turn_id':TURN_ID}
        (directory/'job.json').write_text(util.jdump(job))
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET run_id=?,status='running',sent=1,send_state='sent' WHERE task_id=?", (RUN_ID,TASK_ID))
            conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id=? AND run_id=?", (util.jdump(config),TASK_ID,RUN_ID))
        conn.close()
        self.server.turn_status = 'running'
        self.server.current_turn = TURN_ID
        self.server.is_running = True
        self.server.planning_state = 'inactive'
        self.server.messages = [{'id':'user','role':'user','content':'Execute plan.','turnId':TURN_ID}]
        original = self.server.tool_call
        def inputs(name,args):
            pending = self.server.turn_status == 'running'
            if name == 'pi_agent_status':
                return {'structuredContent':{'status':{'sessionId':SESSION_ID,'currentTurnId':TURN_ID,
                    'isRunning':pending,'planningState':'inactive',
                    'pendingInputs':{'count':int(pending),'requestIds':['ask'] if pending else []}}}}
            if name == 'pi_asks_pending':
                self.pending_reads += 1
                return {'structuredContent':{'sessionId':SESSION_ID,'asks':[{'requestId':'ask','toolCallId':'tool',
                    'turnId':TURN_ID,'questions':[{'question':'Continue?','options':['Yes']}]}] if pending else []}}
            if name == 'pi_agent_stop': self.stop_times.append(self.seconds)
            if name == 'pi_agent_abort': self.abort_times.append(self.seconds)
            return original(name,args)
        self.server.tool_call = inputs
        return spec,directory

    def finish(self):
        self.server.turn_status = 'completed'
        self.server.messages.append({'id':'answer','role':'toolResult','toolName':'AskUserQuestion',
            'toolCallId':'tool','turnId':TURN_ID,'toolStatus':'success','isError':False,
            'toolResult':{'details':{'resolutionKind':'answered'}}})

    def run_clock(self,spec,advance):
        def sleep(seconds):
            self.sleeps.append(seconds)
            advance()
            if len(self.sleeps)>20: raise AssertionError('worker did not settle')
        clock = SimpleNamespace(monotonic=lambda:self.seconds, sleep=sleep)
        with mock.patch.object(worker,'time',clock), mock.patch.object(util,'now_dt',
                side_effect=lambda:self.base+datetime.timedelta(seconds=self.seconds)), \
                mock.patch.object(runtime_client.RuntimeClient,'_expiry',side_effect=lambda lease:
                    util.to_iso(self.base+datetime.timedelta(seconds=self.seconds+lease+60))), \
                mock.patch.object(execution_questions,'ensure_observer',return_value={'status':'observing'}):
            return worker.run_real(spec,resume=True)

    def test_six_hour_input_wait_crosses_deadline_without_stop_or_replay(self):
        spec,directory = self.scene()
        def advance():
            if len(self.sleeps)==1:
                self.seconds += 21600
            else:
                self.seconds += 2
                self.finish()
        self.assertEqual(self.run_clock(spec,advance),0)
        job = self.read_job(directory)
        self.assertEqual(job['status'],'finished')
        self.assertGreaterEqual(job['input_wait_seconds'],21600)
        self.assertEqual(self.stop_times,[])
        self.assertEqual(self.abort_times,[])
        self.assertEqual(self.server.prompt_count,1)
        task = self.call('get-task',{'task_id':TASK_ID})['task']
        self.assertEqual(task['extra']['execution_questions'][0]['state'],'answered')
        self.assertEqual(len(task['extra']['execution_input_wait_intervals']),1)
        # One details fetch for the identity, then one independent terminal
        # reconciliation read; no details fetch at the six-hour pending poll.
        self.assertEqual(self.pending_reads,2)

    def test_resumed_observer_appends_answer_and_command_to_existing_log(self):
        spec,directory = self.scene()
        before = self.read_events(directory)
        def advance():
            self.seconds += 2
            self.finish()
            self.server.messages.append({'id':'verify','role':'toolResult','toolName':'Bash',
                'toolCallId':'verify-call','turnId':TURN_ID,'isError':False,
                'toolResult':{'content':[{'type':'text','text':'verified'}],
                              'details':{'exitCode':0}}})
        self.assertEqual(self.run_clock(spec,advance),0)
        events = self.read_events(directory)
        self.assertEqual(events[:len(before)],before)
        self.assertEqual(sum(event['type']=='session' for event in events),1)
        self.assertEqual([event['seq'] for event in events],list(range(1,len(events)+1)))
        answers = [event for event in events if event['type']=='tool_result'
                   and event.get('tool_call_id')=='tool']
        self.assertEqual(len(answers),1)
        self.assertEqual(answers[0]['turn_id'],TURN_ID)
        commands = [event for event in events if event['type']=='command_output'
                    and event.get('tool_call_id')=='verify-call']
        self.assertEqual(len(commands),1)
        self.assertEqual(commands[0]['turn_id'],TURN_ID)
        self.assertEqual(commands[0]['exit_code'],0)
        self.assertEqual(self.server.prompt_count,1)

    def test_resumed_message_revision_updates_actual_consumer(self):
        spec,directory = self.scene()
        path = directory / dsh_events.EVENT_LOG_NAME
        events = self.read_events(directory)
        for revision in (3,1):
            events.append({'v':1,'seq':len(events)+1,'ts':util.now_iso(),
                'type':'assistant_message','turn_id':TURN_ID,
                'source_message_id':'streamed','source_revision':revision,
                'content':'old partial'})
        path.write_text(''.join(util.jdump(event)+'\n' for event in events))
        before = path.read_text()
        self.server.messages.append({'id':'streamed','role':'assistant',
                                     'turnId':TURN_ID,'content':'new partial'})
        def advance():
            self.seconds += 2
            self.server.messages[-1]['content'] = 'new complete'
            self.finish()
        self.assertEqual(self.run_clock(spec,advance),0)
        self.assertTrue(path.read_text().startswith(before))
        observed = self.read_events(directory)
        source = [event for event in observed if event.get('source_message_id')=='streamed']
        messages = dsh_events.to_messages(observed)
        rendered = [message for message in messages if message['id']=='desktop:streamed:assistant_message']
        self.assertEqual(len(rendered),1)
        self.assertEqual(rendered[0]['content'],'new complete')
        self.assertEqual(rendered[0]['revision'],5)
        self.assertEqual([event['source_revision'] for event in source],[3,1,4,5])
        self.assertEqual(sum(event['type']=='session' for event in observed),1)
        self.assertEqual(self.server.prompt_count,1)

    def test_maxwait_core_stop_receipt_then_single_abort_after_ten_seconds(self):
        spec,directory = self.scene(maximum=3)
        spec['timeout_seconds'] = 1
        self.write_spec(spec)
        self.server.stop_aborts = False
        self.assertEqual(self.run_clock(spec,lambda:setattr(self,'seconds',self.seconds+2)),1)
        self.assertEqual(self.stop_times,[4])
        self.assertEqual(self.abort_times,[14])
        self.assertEqual(self.server.prompt_count,1)
        self.assertIn('execution_question_expired',self.read_job(directory)['detail'])
        task = self.call('get-task',{'task_id':TASK_ID})['task']
        self.assertEqual(task['failure_code'],'execution_failed')
        self.assertEqual(task['extra']['execution_questions'][0]['state'],'expired')
        conn = db.connect(self.db_path)
        receipts = conn.execute("SELECT tool,status FROM runtime_operations WHERE tool IN ('kp_runtime_stop','kp_runtime_abort') ORDER BY created_at").fetchall()
        conn.close()
        self.assertEqual([(r['tool'],r['status']) for r in receipts],
            [('kp_runtime_stop','accepted'),('kp_runtime_abort','accepted')])

    def test_no_scope_history_reads_pending_and_never_writes_or_replays(self):
        spec,directory = self.scene(historical=True)
        def advance():
            self.seconds += 2
            self.finish()
        self.assertEqual(self.run_clock(spec,advance),0)
        q = self.call('get-task',{'task_id':TASK_ID})['task']['extra']['execution_questions'][0]
        self.assertFalse(q['answerable'])
        self.assertIn('历史会话只读',q['rejection_reason'])
        self.assertEqual(q['state'],'answered')
        self.assertEqual(self.pending_reads,2)
        self.assertEqual(self.stop_times,[])
        self.assertEqual(self.abort_times,[])
        self.assertEqual(self.server.prompt_count,1)
        self.assertEqual(self.server.plans_resolve_calls,[])
        self.assertNotIn('scope_id',self.read_job(directory))

    def test_completed_without_durable_answer_fails_core_and_worker(self):
        spec,directory = self.scene()
        def advance():
            self.seconds += 2
            self.server.turn_status = 'completed'
        self.assertEqual(self.run_clock(spec,advance),1)
        task=self.call('get-task',{'task_id':TASK_ID})['task']
        self.assertEqual(task['failure_code'],'execution_failed')
        self.assertEqual(task['extra']['execution_questions'][0]['state'],'expired')
        self.assertEqual(self.read_job(directory)['status'],'failed')
        self.assertEqual(self.read_events(directory)[-1]['reason'],'execution_failed')
        self.assertEqual(self.server.prompt_count,1)

    def test_already_reconciled_expiry_survives_observer_restart(self):
        spec,directory=self.scene()
        self.call('report-execution-question',{'task_id':TASK_ID,'run_id':RUN_ID,'session_id':SESSION_ID,
            'turn_id':TURN_ID,'request_id':'ask','tool_call_id':'tool','state':'open',
            'questions':[{'question':'Continue?','options':['Yes']}]})
        self.server.turn_status='completed'
        self.call('reconcile-execution-question',{'task_id':TASK_ID,'run_id':RUN_ID})
        self.assertEqual(self.call('get-task',{'task_id':TASK_ID})['task']['failure_code'],'execution_failed')
        self.assertEqual(self.run_clock(spec,lambda:None),1)
        self.assertEqual(self.read_job(directory)['status'],'failed')
        self.assertEqual(self.read_events(directory)[-1]['reason'],'execution_failed')
