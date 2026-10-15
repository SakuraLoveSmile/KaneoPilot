"""New execution tools cross the real loopback project MCP receipt boundary."""
import datetime
import json
from unittest import mock
from test_pi_desktop_backend import PiDesktopCase, SESSION_ID
from kanpilot import db, runtime_client, util


class InputRuntimeTests(PiDesktopCase):
    def scene(self, *, read_only=False):
        self.seed_claimed_dispatch(task_id="t",run_id="r")
        config={'connection_file':str(self.connection_file),'provider_id':'test-provider','model_id':'test-model','thinking_level':'low','permission_mode':'auto'}
        client=runtime_client.RuntimeClient(role='execution',connection_file=str(self.connection_file),project_path=str(self.root),runtime_config=config,task_id='t',run_id='r',read_only=read_only,lease_seconds=1)
        client.connect()
        return client

    def asks(self):
        original=self.server.tool_call
        self.responds=[]
        def inputs(name,args):
            if name=='pi_asks_pending':return {'structuredContent':{'sessionId':args['sessionId'],'asks':[{'requestId':'ask','toolCallId':'tool','questions':[{'question':'Q','options':['A']}]}]}}
            if name=='pi_asks_resolve':
                self.responds.append(args)
                return {'structuredContent':{'status':'answered','requestId':args['requestId']}}
            return original(name,args)
        self.server.tool_call=inputs

    def test_tools_guard_receipts_and_read_only_worker(self):
        self.asks();client=self.scene()
        session=client.session_create(str(self.root),mode='goal')['session']['id']
        pending=client.input_pending(session)
        self.assertEqual(pending['sessionId'],session)
        self.assertEqual(pending['asks'][0]['requestId'],'ask')
        for _ in range(2):client.input_respond(session,'ask','tool',[['A']],operation_id='answer')
        self.assertEqual(len(self.responds),1)
        self.assertEqual(self.responds[0],{'sessionId':session,'requestId':'ask','expectedToolCallId':'tool','answers':[['A']],'confirm':True})
        with self.assertRaises(util.KanpilotError):client.input_pending('foreign')
        reader=self.scene(read_only=True)
        self.assertEqual(reader.input_pending(session)['sessionId'],session)
        with self.assertRaisesRegex(util.KanpilotError,'只读'):reader.input_respond(session,'ask','tool',[['A']],operation_id='readonly')
        conn=db.connect(self.db_path)
        row=conn.execute("SELECT * FROM runtime_operations WHERE scope_id=? AND operation_id='answer'",(client.scope_id,)).fetchone()
        self.assertEqual(row['status'],'accepted')
        self.assertNotIn(self.server.token, row['result_json'])
        conn.close()

    def test_scope_expiry_reissues_same_identity_keeps_create_receipt(self):
        self.asks();client=self.scene()
        session=client.session_create(str(self.root),mode='goal')['session']['id']
        scope=self.project_mcp.scope_manager.get_scope(client.scope_id)
        grant=self.app_support/'mcp'/'grants'/f'{client.scope_id}_runtime.json'
        info=json.loads(grant.read_text()); old_token=info['token']
        expired=util.to_iso(util.now_dt()-datetime.timedelta(seconds=1))
        scope.expires_at=expired;info['expires_at']=expired;grant.write_text(json.dumps(info))
        self.assertEqual(client.input_pending(session)['sessionId'],session)
        fresh=json.loads(grant.read_text())
        self.assertNotEqual(fresh['token'],old_token)
        self.assertIsNone(self.project_mcp.scope_manager.authenticate(old_token))
        with self.assertRaises(util.KanpilotError):client.input_pending('foreign')

    def test_planner_and_review_cannot_list_or_call_execution_input_tools(self):
        from kanpilot import mcp_scopes, mcp_tools
        self.asks(); self.scene()
        future=util.to_iso(util.now_dt()+datetime.timedelta(minutes=5))
        for role in ('planner','review'):
            with self.subTest(role=role):
                scope=mcp_scopes.Scope('input_'+role,'input_'+role,role,future)
                exposed={tool['name'] for tool in mcp_tools.list_tools(scope,'runtime')}
                self.assertNotIn('kp_runtime_input_pending',exposed)
                self.assertNotIn('kp_runtime_input_respond',exposed)
                for tool,args in [('kp_runtime_input_pending',{'session_id':SESSION_ID}),
                    ('kp_runtime_input_respond',{'session_id':SESSION_ID,'request_id':'ask',
                        'expected_tool_call_id':'tool','answers':[['A']],'operation_id':'forbidden'})]:
                    with self.assertRaises(util.KanpilotError) as caught:
                        mcp_tools.dispatch_tool_call(self.db_path,self.project_mcp.scope_manager,
                            'runtime',scope,tool,args)
                    self.assertEqual(caught.exception.code,'forbidden')
        self.assertEqual(self.responds,[])

    def relay_scene(self):
        from kanpilot import round_store,constants
        self.asks();client=self.scene()
        session=client.session_create(str(self.root),mode='goal')['session']['id']
        config={"backend":"pi_desktop",**client.runtime_config}
        directory=self.runs_root/'t'/'r';directory.mkdir(parents=True,exist_ok=True)
        job={'task_id':'t','run_id':'r','backend':'pi_desktop','status':'running','session_id':session,'turn_id':'turn-1','send_state':'sent','scope_id':client.scope_id,'config':config}
        (directory/'job.json').write_text(util.jdump(job))
        (directory/'spec.json').write_text(util.jdump({'task_id':'t','run_id':'r','cwd':str(self.root),'config':config,'timeout_seconds':60}))
        conn=db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET run_id='r',status='running',send_state='sent' WHERE task_id='t'")
            conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id='t' AND run_id='r'",(util.jdump(config),))
        conn.close()
        self.server.turn_status='running'
        self.call('report-execution-question',{'task_id':'t','run_id':'r','session_id':session,'turn_id':'turn-1','request_id':'ask','tool_call_id':'tool','state':'open','questions':[{'question':'Q','options':['A']}]})
        return client

    def test_stop_receipt_then_abort_only_after_core_ten_second_grace(self):
        from kanpilot import execution_questions as eq
        client=self.relay_scene();self.server.stop_aborts=False
        with mock.patch.object(eq,'ensure_observer',return_value={}):
            stopped=self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'})
            self.assertEqual(stopped['status'],'accepted')
            self.assertEqual(stopped['execution_question']['state'],'open')
            self.assertEqual(self.call_err('stop-execution',{'task_id':'t','run_id':'r','operation_id':'abort','abort':True}).code,'conflict')
            self.assertEqual(self.server.abort_calls,0)
            future=util.now_dt()+datetime.timedelta(seconds=11)
            with mock.patch.object(util,'now_dt',return_value=future):
                result=self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'abort','abort':True})
        self.assertEqual(self.server.stop_calls,1)
        self.assertEqual(self.server.abort_calls,1)
        self.assertEqual(result['execution_question']['state'],'stopped')
        self.assertEqual(self.call('get-task',{'task_id':'t'})['task']['failure_code'],'execution_failed')

    def test_stop_unknown_stays_open_and_does_not_repeat_operation(self):
        from kanpilot import execution_questions as eq
        self.relay_scene();self.server.stop_mode='http500'
        with mock.patch.object(eq,'ensure_observer',return_value={}):
            first=self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'})
            self.assertEqual(first['status'],'unknown')
            self.assertEqual(first['execution_question']['state'],'open')
            calls=self.server.stop_calls
            self.call('stop-execution',{'task_id':'t','run_id':'r','operation_id':'stop'})
            self.assertEqual(self.server.stop_calls,calls)
        self.assertEqual(self.call_err('stop-execution',{'task_id':'t','run_id':'r','operation_id':'other'}).code,'conflict')
