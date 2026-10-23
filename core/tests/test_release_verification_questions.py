"""Independent verification input relay; isolated SQLite/Git and fake Plus only."""
from copy import deepcopy
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

import test_release_verification as fixtures
import test_release_verification_history as history_fixtures
from kanpilot import (cli, constants, db, derive, harness_plus_adapter, mcp_scopes, mcp_tools,
    release_verification as verification, runtime_operations, util)
from kanpilot import release_verification_questions as questions
from kanpilot.engine.commands import cmd_attention_list
from kanpilot.runtime_client import stable_scope_id


class VerificationQuestionsTest(TestCase):
    def setUp(self):
        self.case = fixtures.ReleaseVerificationTest('test_cli_fixed_payload_round_trip')
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.conn, self.ctx, self.plus = self.case.conn, self.case.ctx, self.case.plus
        first = self.case.start(); self.vid = first['verification_id']
        self.value = verification._load(self.conn, 'parent', self.vid)
        self.scope = stable_scope_id('execution', invocation_id='releaseverify_' + self.vid)
        runtime_operations.begin_operation(self.conn, self.scope, 'session_create',
            'pi_desktop', 'kp_runtime_session_create', {})
        runtime_operations.complete_operation(self.conn, self.scope, 'session_create',
            session_id='supplement', result=dict(session_id='supplement'))
        self.ask = dict(requestId='ask-1', toolCallId='ask-tool', turnId='planning',
            createdAt=self.ctx.now_iso, questions=[dict(question='How to verify?', options=[dict(label='Strict'),dict(label='Offline')])])
        self.plus.pending_asks = [deepcopy(self.ask)]
        self.plus.running = True
        def status(sid):
            return dict(sessionId=sid, currentTurnId=self.plus.current, isRunning=self.plus.running,
                pendingInputs=dict(count=len(self.plus.pending_asks),requestIds=[a['requestId'] for a in self.plus.pending_asks]))
        self.case.patch(patch.object(self.plus, 'agent_status', side_effect=status))
        self.case.patch(patch.object(self.plus, 'turn_get', side_effect=lambda sid,turn,**kw:
            dict(sessionId=sid,turnId=turn,status='running' if self.plus.running else 'completed')))
        self.calls=[]
        def respond(sid,request,tool,answers,*,operation_id):
            self.calls.append((sid,request,tool,deepcopy(answers),operation_id))
            if self.plus.lose == 'answer': raise util.KanpilotError('timeout','lost response')
            self.plus.pending_asks=[]; self.plus.running=False
            return dict(session_id=sid,request_id=request,status='answered')
        self.case.patch(patch.object(self.plus,'input_respond',side_effect=respond,create=True))

    def sync(self): return self.case.sync(self.vid)

    def question(self): return questions.view(self.conn,self.case.task(),self.ctx.now_iso)

    def payload(self, **extra):
        q=self.question()
        return dict(task_id='parent',verification_id=self.vid,
            expected_context_sha256=self.value['context']['context_sha256'],
            request_id=q['request_id'],questions_sha256=q['questions_sha256'],**extra)

    def answer(self):
        return [dict(question_id='q0',option_ids=['o0'],text='',skipped=False)]

    def test_planning_question_projects_without_changing_original_task_or_review(self):
        before=dict(self.case.task()); self.sync()
        q=self.question(); self.assertEqual(q['phase'],'planning')
        self.assertEqual(q['run_id'],before['run_id']); self.assertTrue(q['answerable'])
        self.assertEqual(dict(self.case.task()),before)
        detail=derive.task_detail(self.conn,self.case.task(),self.ctx.now)['release_verification_question']
        self.assertEqual(detail['request_id'],q['request_id'])
        self.assertEqual(detail['questions'],q['questions'])
        actions=derive.allowed_actions(self.conn,self.case.task(),self.ctx.now)
        self.assertIn('answer_release_verification_questions',actions)
        self.assertNotIn('stop_execution',actions)
        items=cmd_attention_list(self.ctx,{})['items']
        self.assertEqual(items[0]['reason_code'],'release_verification_question')
        self.assertEqual(items[0]['question_preview']['first_title'],'How to verify?')
        self.assertIsNotNone(derive.archive_blocked_reason(self.conn,self.case.task(),self.ctx.now))
        self.assertEqual(self.plus.approvals,0)

    def test_save_is_local_submit_answers_exact_ask_once_and_original_attempt_continues(self):
        self.sync(); payload=self.payload(answers=self.answer())
        questions.cmd_save_release_verification_answers(self.ctx,payload)
        self.assertEqual(self.calls,[])
        submitted=dict(payload,operation_id='answer-operation')
        result=questions.cmd_submit_release_verification_answers(self.ctx,submitted)
        self.assertEqual(result['release_verification_question']['state'],'answered')
        self.assertEqual(self.calls,[('supplement','ask-1','ask-tool',[['Strict']],'answer-operation')])
        questions.cmd_submit_release_verification_answers(self.ctx,submitted)
        self.assertEqual(len(self.calls),1)
        self.sync()
        self.assertEqual((self.plus.created,self.plus.prompts,self.plus.approvals),(1,1,1))

    def test_context_attempt_content_and_tool_drift_are_rejected_without_send(self):
        self.sync(); payload=self.payload(answers=self.answer(),operation_id='op')
        for key,value in [('verification_id','f'*32),('expected_context_sha256','e'*64),('request_id','other'),('questions_sha256','a'*64)]:
            with self.subTest(key=key),self.assertRaises(util.KanpilotError):
                questions.cmd_submit_release_verification_answers(self.ctx,dict(payload,**{key:value}))
        for change in (dict(toolCallId='other'),dict(questions=[dict(question='Different')])):
            self.plus.pending_asks=[dict(self.ask,**change)]
            with self.assertRaises(util.KanpilotError):
                questions.cmd_submit_release_verification_answers(self.ctx,payload)
        self.assertEqual(self.calls,[])

    def test_unknown_freezes_answers_and_sync_never_resends_or_accepts_missing_ask(self):
        self.sync(); self.plus.lose='answer'
        result=questions.cmd_submit_release_verification_answers(self.ctx,self.payload(answers=self.answer(),operation_id='op'))
        self.assertEqual(result['release_verification_question']['delivery'],'unknown')
        self.sync();self.assertEqual(len(self.calls),1)
        changed=[dict(question_id='q0',option_ids=['o1'],text='',skipped=False)]
        with self.assertRaises(util.KanpilotError):
            questions.cmd_save_release_verification_answers(self.ctx,self.payload(answers=changed))
        self.plus.pending_asks=[]
        with patch.object(questions,'_resolutions',return_value={}): self.sync()
        self.assertEqual(self.question()['state'],'open')
        self.assertEqual(self.question()['delivery'],'unknown')
        self.assertEqual(self.plus.approvals,0)

    def test_external_answer_requires_durable_tool_resolution(self):
        self.sync(); self.plus.pending_asks=[]; self.plus.running=False
        message=dict(id='ask-result',role='tool',turnId='planning',toolName='asktool',toolCallId='ask-tool',
            toolStatus='success',toolResult=dict(details=dict(resolutionKind='answered')))
        transcript=dict(session=dict(id='supplement',messages=[message],messageStart=0,messageEnd=1,
            hasMoreBefore=False,hasMoreAfter=False))
        with patch.object(self.plus,'session_get',return_value=transcript,create=True):
            questions.cmd_reconcile_release_verification_answers(self.ctx,self.payload())
        self.assertEqual(self.question()['state'],'answered')
        self.assertEqual(self.question()['answered_by'],'plus')
        self.assertEqual(self.calls,[])

    def test_accepted_runtime_receipt_recovers_lost_response_without_resending(self):
        self.sync();self.plus.lose='answer'
        questions.cmd_submit_release_verification_answers(self.ctx,self.payload(answers=self.answer(),operation_id='lost-op'))
        self.plus.pending_asks=[]
        arguments=dict(scope_id=self.scope,operation_id='lost-op',session_id='supplement',request_id='ask-1',
            expected_tool_call_id='ask-tool',answers=[['Strict']])
        runtime_operations.begin_operation(self.conn,self.scope,'lost-op','pi_desktop','kp_runtime_input_respond',arguments)
        runtime_operations.complete_operation(self.conn,self.scope,'lost-op',session_id='supplement',
            result=dict(session_id='supplement',request_id='ask-1',status='answered'))
        with patch.object(questions,'_resolutions',return_value={}):
            questions.cmd_reconcile_release_verification_answers(self.ctx,self.payload())
        self.assertEqual(self.question()['delivery'],'accepted')
        self.assertEqual(self.question()['state'],'answered')
        self.assertEqual(self.question()['answered_by'],'kaneopilot')
        self.assertEqual(len(self.calls),1)

    def test_dead_submitter_becomes_unknown_while_original_ask_is_still_pending(self):
        self.sync();value=verification._load(self.conn,'parent',self.vid);q=value['input_questions'][0]
        q.update(delivery='submitting',submitted=dict(answers=self.answer(),operation_id='dead-op',at=self.ctx.now_iso,by='kaneopilot'))
        q['operations']=[dict(operation_id='dead-op',answers=self.answer(),delivery='submitting',pid=999999999,at=self.ctx.now_iso)]
        verification._save(self.conn,value,self.ctx.now_iso)
        self.sync()
        self.assertEqual(self.question()['delivery'],'unknown')
        self.assertEqual(self.calls,[])

    def test_concurrent_saved_draft_survives_sync_compare_and_swap(self):
        self.sync(); original=self.plus.input_pending; entered=[]
        def nested(sid):
            pending=original(sid)
            if not entered:
                entered.append(True)
                questions.cmd_save_release_verification_answers(self.ctx,self.payload(answers=self.answer()))
            return pending
        with patch.object(self.plus,'input_pending',side_effect=nested): self.sync()
        self.assertEqual(self.question()['draft'],self.answer())

    def test_unowned_session_is_displayed_but_cannot_answer(self):
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=?',(self.scope,))
        self.sync();self.assertFalse(self.question()['answerable'])
        with self.assertRaises(util.KanpilotError):
            questions.cmd_submit_release_verification_answers(self.ctx,self.payload(answers=self.answer(),operation_id='op'))
        self.assertEqual(self.calls,[])

    def test_execution_phase_binds_approved_execution_and_never_the_original_run(self):
        self.plus.pending_asks=[];self.plus.running=False
        original=self.plus.plans_resolve
        def approve(*args,**kwargs):
            result=original(*args,**kwargs)
            self.plus.running=True
            self.plus.pending_asks=[dict(self.ask,turnId='execution')]
            return result
        with patch.object(self.plus,'plans_resolve',side_effect=approve): self.sync()
        self.sync();q=self.question()
        self.assertEqual((q['phase'],q['turn_id'],q['run_id']),('execution','execution','run-parent'))
        self.assertEqual(self.plus.approvals,1)
        questions.cmd_submit_release_verification_answers(self.ctx,self.payload(answers=self.answer(),operation_id='op'))
        self.assertEqual(self.calls[0][1:3],('ask-1','ask-tool'))

    def test_changed_current_reference_cannot_save_or_send_old_answers(self):
        self.sync();payload=self.payload(answers=self.answer(),operation_id='op')
        db.set_extra(self.conn,self.case.task(),self.ctx.now_iso,release_verification_ref=dict(
            verification_id='f'*32,context_sha256='f'*64))
        with self.assertRaises(util.KanpilotError):
            questions.cmd_submit_release_verification_answers(self.ctx,payload)
        self.assertEqual(self.calls,[])

    def test_cli_contract_saves_exact_verification_without_runtime_write(self):
        self.sync()
        result=cli.run('save-release-verification-answers',self.payload(answers=self.answer()),self.case.fixture.db_path)
        self.assertEqual(result['run_id'],'run-parent')
        self.assertEqual(result['verification_id'],self.vid)
        self.assertEqual(result['release_verification_question']['draft'],self.answer())
        with self.assertRaises(util.KanpilotError):
            cli.run('save-release-verification-answers',self.payload(answers=self.answer(),session_id='arbitrary'),self.case.fixture.db_path)
        self.assertEqual(self.calls,[])

    def runtime_scope(self):
        value=verification._load(self.conn,'parent',self.vid);config=value['context']['runtime_config']
        invocation='releaseverify_'+self.vid
        scope=mcp_scopes.Scope(self.scope,invocation,'execution',util.to_iso(util.now_dt()+timedelta(minutes=10)),
            project_path=value['worktree_path'],connection_file=config['connection_file'],
            runtime_config={k:config[k] for k in constants.EXEC_PI_DESKTOP_KEYS if k in config})
        manager=mcp_scopes.ScopeManager(self.case.fixture.root/'isolated-mcp-input')
        manager.scopes[scope.scope_id]=scope
        return scope,manager

    def test_real_runtime_reservation_delivers_only_the_frozen_user_answer(self):
        self.sync();scope,manager=self.runtime_scope();host_calls=[]
        case=self
        class Host:
            def call_tool(self,name,args):
                host_calls.append((name,deepcopy(args)))
                case.plus.pending_asks=[];case.plus.running=False
                return dict(requestId=args['requestId'],status='answered')
        def respond(sid,request,tool,answers,*,operation_id):
            args=dict(scope_id=scope.scope_id,session_id=sid,request_id=request,
                expected_tool_call_id=tool,answers=answers,operation_id=operation_id)
            return mcp_tools.dispatch_tool_call(self.case.fixture.db_path,manager,'runtime',scope,'kp_runtime_input_respond',args)
        with patch.object(harness_plus_adapter,'_get_client',return_value=(Host(),{})),patch.object(self.plus,'input_respond',side_effect=respond):
            result=questions.cmd_submit_release_verification_answers(self.ctx,self.payload(answers=self.answer(),operation_id='runtime-op'))
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(host_calls,[('pi_asks_resolve',dict(sessionId='supplement',requestId='ask-1',
            expectedToolCallId='ask-tool',answers=[['Strict']],confirm=True))])
        self.assertEqual(db.get_runtime_operation(self.conn,self.scope,'runtime-op')['status'],'accepted')

    def test_runtime_reservation_rechecks_current_attempt_before_any_external_call(self):
        self.sync();scope,manager=self.runtime_scope();original=runtime_operations.reserve_downstream
        def change_before_reserve(conn,*args,**kwargs):
            task=db.require_task(conn,'parent')
            db.set_extra(conn,task,self.ctx.now_iso,release_verification_ref=dict(verification_id='e'*32,context_sha256='e'*64))
            return original(conn,*args,**kwargs)
        def respond(sid,request,tool,answers,*,operation_id):
            args=dict(scope_id=scope.scope_id,session_id=sid,request_id=request,
                expected_tool_call_id=tool,answers=answers,operation_id=operation_id)
            return mcp_tools.dispatch_tool_call(self.case.fixture.db_path,manager,'runtime',scope,'kp_runtime_input_respond',args)
        with patch.object(self.plus,'input_respond',side_effect=respond),patch.object(runtime_operations,'reserve_downstream',side_effect=change_before_reserve):
            # The current ref may change after the user send was reserved. Its
            # old receipt remains recoverable, but no downstream send is made.
            with self.assertRaises(util.KanpilotError):
                questions.cmd_submit_release_verification_answers(self.ctx,self.payload(answers=self.answer(),operation_id='stale-op'))
        self.assertIsNone(db.get_runtime_operation(self.conn,self.scope,'stale-op'))


class HistoricalVerificationQuestionsTest(TestCase):
    def test_first_external_approval_execution_ask_is_adopted_read_only_then_reported(self):
        history=history_fixtures.HistoryTest('test_manual_approval_adopts_completed_report_without_forging_approval_or_pass')
        history.setUp();self.addCleanup(history.doCleanups)
        history.manual_approval(blocked=False)
        case,conn,plus=history.case,history.conn,history.plus
        scope=stable_scope_id('execution',invocation_id='releaseverify_'+history.vid)
        runtime_operations.begin_operation(conn,scope,'session_create','pi_desktop','kp_runtime_session_create',{})
        runtime_operations.complete_operation(conn,scope,'session_create',session_id='supplement',result=dict(session_id='supplement'))
        plus.running=True;plus.history_changes['executionState']='running'
        plus.pending_asks=[dict(requestId='execution-ask',toolCallId='execution-tool',turnId='execution',
            questions=[dict(question='Continue verification?',options=[dict(label='Continue')])])]
        def status(sid):
            return dict(sessionId=sid,currentTurnId=plus.current,isRunning=plus.running,
                pendingInputs=dict(count=len(plus.pending_asks),requestIds=[a['requestId'] for a in plus.pending_asks]))
        case.patch(patch.object(plus,'agent_status',side_effect=status))
        before=verification._load(conn,'parent',history.vid)
        self.assertFalse(before.get('approval_observation'))
        self.assertFalse(before.get('execution_turn_id'))
        result=case.sync(history.vid)
        self.assertEqual(result['verification']['state'],'running',result)
        q=questions.view(conn,case.task())
        self.assertEqual((q['phase'],q['turn_id']),('execution','execution'))
        self.assertFalse(q['answerable'])
        with self.assertRaises(util.KanpilotError):
            questions.cmd_submit_release_verification_answers(case.ctx,dict(task_id='parent',verification_id=history.vid,
                expected_context_sha256=before['context']['context_sha256'],request_id=q['request_id'],
                questions_sha256=q['questions_sha256'],operation_id='not-authorized',
                answers=[dict(question_id='q0',option_ids=['o0'],text='',skipped=False)]))
        self.assertEqual(plus.approvals,0)
        self.assertIsNone(db.get_runtime_operation(conn,*verification._approval_operation(before)))

        # Plus answers its own waiter. Only its durable tool result closes the
        # read-only card; no Pilot submit or synthetic observation is injected.
        plus.pending_asks=[]
        history.messages.append(dict(id='execution-answer',role='tool',turnId='execution',toolName='asktool',
            toolCallId='execution-tool',toolStatus='success',
            toolResult=dict(details=dict(resolutionKind='answered'))))
        case.sync(history.vid)
        self.assertEqual(questions.view(conn,case.task())['state'],'answered')
        self.assertEqual(questions.view(conn,case.task())['answered_by'],'plus')
        plus.running=False;plus.history_changes['executionState']='completed';plus.history_version+=1
        finished=case.sync(history.vid)
        value=verification._load(conn,'parent',history.vid)
        self.assertEqual(finished['verification']['failure_code'],'release_verification_report')
        self.assertEqual(value['report_payload'],plus.goal_report_get('supplement'))
        self.assertEqual(value['verification_id'],history.vid)
        self.assertEqual((plus.created,plus.prompts,plus.approvals),(1,1,0))
