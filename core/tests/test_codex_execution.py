"""Codex runtime checks use isolated HOME, CODEX_HOME, repo and CLI processes."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kanpilot import codex_execution as adapter, codex_worker as worker
from kanpilot import execution_contract, git_delivery, process_owner, runtime_client, util
from helpers import make_codex_fixture


class CodexExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = make_codex_fixture(self.root, with_provider=False)
        self.env.update(HOME=str(self.root), CODEX_HOME=self.env['KANPILOT_CODEX_HOME'], KANPILOT_DB=str(self.root/'test.sqlite'), KANPILOT_HOME=str(self.root/'pilot'))
        self.env_patch = mock.patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.bin = Path(self.env['KANPILOT_CODEX_BIN'])
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'config','user.name','Test'],check=True)
        subprocess.run(['git','-C',str(self.repo),'config','user.email','test@example.invalid'],check=True)
        (self.repo/'main.txt').write_text('before\n')
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.repo),'commit','-qm','initial'],check=True)
        self.plan = self.root/'plan.md'; self.plan.write_text('# Task\n```verify\npython3 -V\n```\n')
        self.run_dir = self.root/'runs'; self.run_dir.mkdir()
        self.spec = {'v':1,'task_id':'t','request_id':'request','run_id':'r','backend':'codex',
            'scope_id':runtime_client.stable_scope_id('execution',task_id='t',run_id='r'),
            'config':{'backend':'codex',**adapter.DEFAULT_CONFIG,'sandbox':'workspace-write','execution_question_max_wait_seconds':0},
            'cwd':str(self.repo),'repository':str(self.repo),'plan_path':str(self.plan),'plan_sha256':util.sha256_file(self.plan),
            'completion_policy':'command_verification','required_verification_commands':['python3 -V'],
            'instruction':'Do the frozen task','runs_dir':str(self.run_dir),'db_path':str(self.root/'test.sqlite'),'timeout_seconds':10}
        execution_contract.freeze_spec(self.spec,requirements_sha256='a'*64)
        (self.run_dir/'spec.json').write_text(util.jdump(self.spec))
        self.owner = mock.Mock(instance_id=None)
        self.owner.__enter__ = mock.Mock(return_value=self.owner)
        self.owner.__exit__ = mock.Mock(return_value=False)

    def tearDown(self):
        self.env_patch.stop(); self.temp.cleanup()

    def stub(self, mode='completed'):
        # The executable is a real subprocess, with protocol-shaped synthetic model events.
        self.bin.write_text('#!'+sys.executable+'\n'+'''import json,os,sys,time,subprocess
from pathlib import Path
args=sys.argv[1:]
if '--version' in args: print('codex-cli test'); sys.exit(0)
prompt=sys.stdin.read()
out=Path(args[args.index('-o')+1])
Path(str(out)+'.argv').write_text(json.dumps(args))
mode='''+repr(mode)+'''
def emit(e): print(json.dumps(e),flush=True)
emit({'type':'thread.started','thread_id':'thread-1'})
emit({'type':'turn.started'})
if mode=='hang':
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
    Path(str(out)+'.child').write_text(str(child.pid))
    time.sleep(120)
if mode=='partial':
    sys.stdout.write('{"type":'); sys.stdout.flush(); sys.exit(0)
needs=mode=='questions' and 'resume' not in args
report={'status':'needs_input' if needs else 'completed','summary':'Fixture result','checks':[{'command':'python3 -V','exit_code':0}], 'questions':[{'title':'Choose','options':['A','B']}] if needs else []}
if mode!='no_command' and not needs:
    emit({'type':'item.started','item':{'id':'command-1','type':'command_execution','status':'in_progress','command':"/bin/zsh -lc 'python3 -V'"}})
    emit({'type':'item.completed','item':{'id':'command-1','type':'command_execution','status':'completed','command':"/bin/zsh -lc 'python3 -V'",'exit_code':0,'aggregated_output':'Python fixture'}})
if mode=='change_after_check':
    emit({'type':'item.started','item':{'id':'write-1','type':'command_execution','status':'in_progress','command':"python3 -c 'write_source()'"}})
    Path('main.txt').write_text('after verification')
    emit({'type':'item.completed','item':{'id':'write-1','type':'command_execution','status':'completed','command':"python3 -c 'write_source()'",'exit_code':0}})
emit({'type':'item.completed','item':{'id':'message-1','type':'agent_message','text':json.dumps(report)}})
if mode!='no_report': out.write_text(json.dumps(report))
emit({'type':'turn.completed','usage':{}})
''')
        self.bin.chmod(0o755)

    @contextlib.contextmanager
    def guards(self, callback=None):
        with mock.patch.object(worker,'_start_authorized'), mock.patch.object(worker,'_mailbox_authorized',return_value=True), mock.patch.object(worker,'_heartbeat') as heartbeat, mock.patch.object(worker.process_owner,'WorkerOwner',return_value=self.owner), mock.patch.object(worker.cli,'run',side_effect=callback):
            yield heartbeat

    def job(self):
        return json.loads((self.run_dir/'job.json').read_text())

    def test_capabilities_read_only_and_explicit_config(self):
        self.assertFalse(adapter.capabilities()['available'])
        self.assertIsNone(adapter.validate_config(adapter.DEFAULT_CONFIG))
        self.assertTrue(adapter.capabilities(adapter.DEFAULT_CONFIG)['available'])
        self.assertEqual(list(self.run_dir.iterdir()),[self.run_dir/'spec.json'])
        with mock.patch.object(adapter,'_catalog',return_value=[{'id':'known','reasoning_efforts':['low']}]):
            self.assertIn('目录',adapter.validate_config(adapter.DEFAULT_CONFIG))
            self.assertIn('不支持',adapter.validate_config({'model':'known','reasoning_effort':'high'}))

    def test_launcher_preserves_provider_permissions_and_resume(self):
        provider=mock.Mock(config_args=['model_provider="local"'])
        argv=adapter.launcher_argv(self.spec,'schema','final',provider,'thread')
        self.assertIn('resume',argv); self.assertNotIn('--ephemeral',argv)
        self.assertIn('--ignore-user-config',argv)
        self.assertIn('approval_policy="never"',argv)
        self.assertIn('sandbox_mode="workspace-write"',argv)
        self.assertEqual(argv[-2:],['thread','-'])
        self.assertIsNone(adapter.exact_command(None))
        self.assertEqual(adapter.exact_command('/bin/zsh -lc "python3 -V"'),'python3 -V')
        self.assertEqual(adapter.exact_command('cd x && python3 -V'),'cd x && python3 -V')

    def test_worker_records_native_commands_and_real_thread_after_heartbeat(self):
        self.stub()
        with self.guards() as heartbeat:
            self.assertEqual(worker.run_real(self.spec),0)
            heartbeat.assert_called_once()
        job=self.job(); self.assertEqual(job['status'],'finished'); self.assertEqual(job['verification_evidence_version'],1)
        self.assertEqual(job['session_id'],'thread-1')
        self.assertEqual(job['turn_id'],'codex:r:1')
        native=adapter._native_checks(self.run_dir,job['turns'][-1],{})
        self.assertEqual(native[0]['command'],'python3 -V')
        self.assertEqual(native[0]['source_sha256'],job['verification_source_sha256'])
        events=[json.loads(line) for line in (self.run_dir/'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[0]['type'],'session'); self.assertEqual(events[0]['turn_id'],job['turn_id']); self.assertEqual(events[-1]['type'],'terminal')
        self.assertEqual(worker.run_real(self.spec),3)

    def test_heartbeat_failure_has_no_session_and_kills_child(self):
        self.stub('hang')
        with self.guards(), mock.patch.object(worker,'_heartbeat',side_effect=util.KanpilotError('conflict','heartbeat failed')):
            self.assertEqual(worker.run_real(self.spec),3)
        job=self.job(); self.assertIsNone(job['session_id'])
        self.assertIsNone(process_owner.process_birth(job['codex_cli_pid']))
        events=[json.loads(line) for line in (self.run_dir/'events.jsonl').read_text().splitlines()]
        self.assertFalse(any(e['type']=='session' for e in events))

    def test_missing_report_and_partial_native_stream_fail_closed(self):
        for mode in ('no_report','partial'):
            with self.subTest(mode=mode):
                if (self.run_dir/'codex-started.json').exists():
                    for path in self.run_dir.iterdir():
                        if path.name!='spec.json': path.unlink()
                self.stub(mode)
                with self.guards(): self.assertEqual(worker.run_real(self.spec),3)
                self.assertEqual(self.job()['status'],'failed')
                self.assertFalse((self.run_dir/adapter.REPORT_NAME).exists())

    def test_boolean_and_duplicate_command_evidence_are_not_passed(self):
        from kanpilot import stage_evidence
        self.stub()
        with self.guards(): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job(); turn=job['turns'][-1]; path=self.run_dir/turn['events_file']
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        command=next(row for row in rows if row['event']['type']=='item.completed' and (row['event'].get('item') or {}).get('type')=='command_execution')
        command['event']['item']['exit_code']=True
        path.write_text(''.join(util.jdump(row)+'\n' for row in rows))
        checks=adapter._native_checks(self.run_dir,turn,{})
        self.assertFalse(stage_evidence.match_checks(['python3 -V'],checks)['ok'])
        command['event']['item']['exit_code']=0
        rows.insert(-1,dict(command))
        for number,row in enumerate(rows,1): row['seq']=number
        path.write_text(''.join(util.jdump(row)+'\n' for row in rows))
        checks=adapter._native_checks(self.run_dir,turn,{})
        self.assertFalse(stage_evidence.match_checks(['python3 -V'],checks)['ok'])
        report={'status':'completed','summary':'x','checks':[{'command':'python3 -V','exit_code':True}],'questions':[]}
        with self.assertRaises(util.KanpilotError): adapter.validate_report(report)

    def test_question_answer_resumes_same_thread_once_with_checkpoint(self):
        self.stub('questions')
        def callback(command,payload,*args):
            if command=='report-execution-question':
                job=self.job(); pending=job['pending_input']
                self.assertEqual(job['status'],'waiting_input')
                answer={'scope_id':self.spec['scope_id'],'session_id':job['session_id'],'request_id':pending['requestId'],'tool_call_id':pending['toolCallId'],'answers':[['A']],'operation_id':'answer-op'}
                (self.run_dir/('answer-'+util.sha256_text(pending['requestId'])+'.json')).write_text(util.jdump(answer))
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job(); self.assertEqual(len(job['turns']),2)
        self.assertEqual(job['turns'][0]['input_resolution'],'answered')
        argv=json.loads((self.run_dir/'codex-final-2.json.argv').read_text())
        self.assertIn('resume',argv); self.assertEqual(argv[-2: ],['thread-1','-'])
        self.assertEqual(job['turns'][0]['answer_operation_id'],'answer-op')


    def test_fast_question_waits_for_dispatch_confirm_without_new_model_call(self):
        self.stub('questions')
        reports=[]
        def callback(command,payload,*args):
            if command=='report-execution-question':
                reports.append(payload.copy())
                self.assertEqual(self.job()['status'],'waiting_input')
                if len(reports)==1:
                    raise util.KanpilotError('conflict','任务当前不能接收执行问题')
                self.assertEqual(reports[0],reports[1])
                pending=self.job()['pending_input']
                answer={'scope_id':self.spec['scope_id'],'session_id':'thread-1','request_id':pending['requestId'],'tool_call_id':pending['toolCallId'],'answers':[['A']],'operation_id':'answer-op'}
                (self.run_dir/('answer-'+util.sha256_text(pending['requestId'])+'.json')).write_text(util.jdump(answer))
            if command=='get-task':
                self.assertEqual(len(self.job()['turns']),1)
                return {'task':{'task_id':'t','run_id':'r','status':'submitting'}}
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),0)
        self.assertEqual(len(reports),2)
        self.assertEqual(len(self.job()['turns']),2)
        self.assertEqual(self.job()['verification_evidence_version'],1)


    def test_confirm_commits_between_question_conflict_and_status_read(self):
        self.stub('questions')
        reports=[]
        def callback(command,payload,*args):
            if command=='report-execution-question':
                reports.append(payload.copy())
                if len(reports)==1:
                    raise util.KanpilotError('conflict','任务当前不能接收执行问题')
                self.assertEqual(reports[0],reports[1])
                pending=self.job()['pending_input']
                answer={'scope_id':self.spec['scope_id'],'session_id':'thread-1','request_id':pending['requestId'],'tool_call_id':pending['toolCallId'],'answers':[['A']],'operation_id':'answer-op'}
                (self.run_dir/('answer-'+util.sha256_text(pending['requestId'])+'.json')).write_text(util.jdump(answer))
            if command=='get-task':
                return {'task':{'task_id':'t','run_id':'r','status':'running'}}
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),0)
        self.assertEqual(len(reports),2)
        self.assertEqual(len(self.job()['turns']),2)

    def test_running_question_conflict_is_bounded_and_never_reinvokes_model(self):
        self.stub('questions')
        reports=[]
        def callback(command,payload,*args):
            if command=='report-execution-question':
                reports.append(payload.copy())
                raise util.KanpilotError('conflict','unrelated persistent identity conflict')
            if command=='get-task':
                return {'task':{'task_id':'t','run_id':'r','status':'running'}}
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),3)
        self.assertEqual(len(reports),2)
        self.assertEqual(reports[0],reports[1])
        self.assertEqual(len(self.job()['turns']),1)
        self.assertEqual(self.job()['status'],'failed')

    def test_stop_waiting_worker_is_terminal_without_resume(self):
        self.stub('questions')
        def callback(command,payload,*args):
            if command=='report-execution-question':
                job=self.job()
                (self.run_dir/'stop-op.json').write_text(util.jdump({'scope_id':self.spec['scope_id'],'session_id':job['session_id']}))
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),3)
        job=self.job(); self.assertEqual(job['status'],'aborted'); self.assertEqual(len(job['turns']),1)
        self.assertFalse((self.run_dir/'codex-final-2.json').exists())

    def test_owner_closed_cleans_cli_process_group(self):
        self.stub('hang')
        def check():
            child=self.run_dir/'codex-final-1.json.child'
            if child.exists(): raise process_owner.OwnerClosed('test owner closed')
        self.owner.check.side_effect=check
        with self.guards(): self.assertEqual(worker.run_real(self.spec),3)
        job=self.job(); child=int((self.run_dir/'codex-final-1.json.child').read_text())
        self.assertIsNone(process_owner.process_birth(job['codex_cli_pid']))
        for _ in range(30):
            if process_owner.process_birth(child) is None: break
            time.sleep(.02)
        self.assertIsNone(process_owner.process_birth(child)); self.assertEqual(job['status'],'aborted')

    def test_source_drift_after_command_cannot_be_verified(self):
        self.stub('change_after_check')
        with self.guards(): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job(); native=adapter._native_checks(self.run_dir,job['turns'][-1],{},commands=['python3 -V'])
        from kanpilot import stage_evidence
        coverage=stage_evidence.match_checks(['python3 -V'],native,expected_source_sha256=job['verification_source_sha256'])
        self.assertFalse(coverage['ok']); self.assertEqual(coverage['rows'][0]['unconfirmed_reason'],'source_mismatch')

    def test_require_terminal_never_calls_a_model(self):
        job={'status':'finished','exit_code':0,'ended_at':'now','cli_terminal':True,'pid':4321,'worker_birth':{'started_seconds':1,'started_microseconds':0},'turns':[{'status':'completed','exit_code':0,'cli_terminal':True,'ended_at':'now','pid':4322,'birth':{'started_seconds':1,'started_microseconds':0}}]}
        with mock.patch.object(process_owner,'process_birth',return_value=None),mock.patch.object(util,'pid_alive',return_value=False):
            adapter.require_terminal(job)
        with mock.patch.object(process_owner,'process_birth',return_value={'started_seconds':1,'started_microseconds':0}):
            with self.assertRaises(util.KanpilotError): adapter.require_terminal(job)



    def test_native_verification_window_rejects_changes_and_later_write_tools(self):
        from kanpilot import stage_evidence
        self.stub()
        with self.guards(): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job(); turn=job['turns'][-1]; path=self.run_dir/turn['events_file']
        original=json.loads(json.dumps([json.loads(line) for line in path.read_text().splitlines()]))
        for mode in ('missing_start','source_change','file_change','mcp_tool_call'):
            with self.subTest(mode=mode):
                rows=json.loads(json.dumps(original))
                started=next(row for row in rows if row['event']['type']=='item.started')
                if mode=='missing_start': rows.remove(started)
                elif mode=='source_change': started['source_sha256']='f'*64
                else:
                    rows.insert(-1,{'turn_id':turn['turn_id'],'event':{'type':'item.completed','item':{'id':'later-write','type':mode,'status':'completed'}}})
                for number,row in enumerate(rows,1): row['seq']=number
                path.write_text(''.join(util.jdump(row)+'\n' for row in rows))
                checks=adapter._native_checks(self.run_dir,turn,{},commands=['python3 -V'])
                self.assertFalse(stage_evidence.match_checks(['python3 -V'],checks,expected_source_sha256=job['verification_source_sha256'])['ok'])

    def test_resumed_evidence_binds_prior_turns_and_answer_receipt(self):
        from kanpilot import headless_adapter, verification_recovery, runtime_operations
        self.stub('questions')
        answer={}
        def callback(command,payload,*args):
            if command=='report-execution-question':
                job=self.job(); pending=job['pending_input']
                answer.update(scope_id=self.spec['scope_id'],session_id=job['session_id'],request_id=pending['requestId'],tool_call_id=pending['toolCallId'],answers=[['A']],operation_id='answer-op')
                (self.run_dir/('answer-'+util.sha256_text(pending['requestId'])+'.json')).write_text(util.jdump(answer))
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job(); source=mock.Mock(); source.run_dir.return_value=self.run_dir
        hashes={str(self.run_dir/'spec.json'):util.sha256_file(self.run_dir/'spec.json')}
        receipt={'harness_id':'codex','tool':'codex_execution_input','status':'accepted','session_id':job['session_id'],'request_sha256':runtime_operations.compute_request_sha256(answer)}
        with mock.patch.object(verification_recovery,'_read_run_evidence',return_value=(hashes,job,[],{'commands':['python3 -V']})), mock.patch.object(headless_adapter,'resolve_source',return_value=source), mock.patch.object(adapter,'require_terminal'), mock.patch.object(runtime_operations,'get_operation',return_value=receipt):
            self.assertTrue(adapter.read_evidence(None,{'task_id':'t','run_id':'r'})['coverage']['ok'])
            receipt['status']='unknown'
            with self.assertRaises(util.KanpilotError): adapter.read_evidence(None,{'task_id':'t','run_id':'r'})

    def test_read_evidence_cross_checks_source_report_and_native_stream(self):
        from kanpilot import headless_adapter, verification_recovery
        self.stub()
        with self.guards(): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job()
        hashes={str(self.run_dir/'spec.json'):util.sha256_file(self.run_dir/'spec.json')}
        source=mock.Mock(); source.run_dir.return_value=self.run_dir
        with mock.patch.object(verification_recovery,'_read_run_evidence',return_value=(hashes,job,[],{'commands':['python3 -V']})), mock.patch.object(headless_adapter,'resolve_source',return_value=source), mock.patch.object(adapter,'require_terminal'):
            evidence=adapter.read_evidence(None,{'task_id':'t','run_id':'r'})
            self.assertTrue(evidence['coverage']['ok'])
            self.assertEqual(evidence['report']['state'],'ready')
            native=self.run_dir/job['turns'][-1]['events_file']
            original=native.read_bytes(); native.write_bytes(original+b'{')
            with self.assertRaises(util.KanpilotError): adapter.read_evidence(None,{'task_id':'t','run_id':'r'})
            native.write_bytes(original)
            report_path=self.run_dir/adapter.REPORT_NAME
            report=json.loads(report_path.read_text()); report['run_id']='other'
            report_path.write_text(util.jdump(report))
            with self.assertRaises(util.KanpilotError): adapter.read_evidence(None,{'task_id':'t','run_id':'r'})

    def test_read_evidence_missing_command_is_unconfirmed(self):
        from kanpilot import headless_adapter, verification_recovery
        self.stub('no_command')
        with self.guards(): self.assertEqual(worker.run_real(self.spec),0)
        job=self.job(); source=mock.Mock(); source.run_dir.return_value=self.run_dir
        hashes={str(self.run_dir/'spec.json'):util.sha256_file(self.run_dir/'spec.json')}
        with mock.patch.object(verification_recovery,'_read_run_evidence',return_value=(hashes,job,[],{'commands':['python3 -V']})), mock.patch.object(headless_adapter,'resolve_source',return_value=source), mock.patch.object(adapter,'require_terminal'):
            evidence=adapter.read_evidence(None,{'task_id':'t','run_id':'r'})
            self.assertFalse(evidence['coverage']['ok'])
            self.assertEqual(evidence['coverage']['rows'][0]['unconfirmed_reason'],'missing_record')

    def test_mailbox_receipt_and_unknown_delivery_never_resend(self):
        from kanpilot import cli, db, runtime_operations
        cli.run('migrate',{},Path(self.spec['db_path']))
        conn=db.connect(Path(self.spec['db_path']))
        self.addCleanup(conn.close)
        client=adapter.InputClient.__new__(adapter.InputClient)
        client.conn=conn; client.directory=self.run_dir; client.scope_id=self.spec['scope_id']; client.read_only=False
        payload={'scope_id':client.scope_id,'session_id':'thread-1','request_id':'request-1','tool_call_id':'tool-1','answers':[['A']],'operation_id':'op-1'}
        result=client._mailbox('codex_execution_input','op-1',payload,'answer-one.json',{'status':'answered'})
        self.assertEqual(result,{'status':'answered'})
        self.assertTrue(worker._mailbox_authorized(self.spec,payload,'codex_execution_input'))
        original=(self.run_dir/'answer-one.json').read_bytes()
        self.assertEqual(client._mailbox('codex_execution_input','op-1',payload,'answer-one.json',{'status':'answered'}),result)
        self.assertEqual((self.run_dir/'answer-one.json').read_bytes(),original)
        payload['operation_id']='op-2'
        with mock.patch.object(runtime_operations,'complete_operation',side_effect=OSError('lost acknowledgement')):
            with self.assertRaises(OSError): client._mailbox('codex_execution_input','op-2',payload,'answer-two.json',{'status':'answered'})
        self.assertFalse(worker._mailbox_authorized(self.spec,payload,'codex_execution_input'))
        with self.assertRaises(util.KanpilotError): client._mailbox('codex_execution_input','op-2',payload,'answer-two.json',{'status':'answered'})
        self.assertEqual(len(list(self.run_dir.glob('answer-*.json'))),2)
        self.assertFalse(list(self.run_dir.glob('*.tmp-*')))

    def test_read_only_input_client_has_no_write_or_resume_effect(self):
        self.stub('questions')
        def callback(command,payload,*args):
            if command=='report-execution-question':
                job=self.job(); pending=job['pending_input']
                client=adapter.InputClient.__new__(adapter.InputClient)
                client.directory=self.run_dir; client.spec=self.spec; client.read_only=True
                status=client.agent_status(job['session_id'])
                self.assertTrue(status['isRunning'])
                self.assertEqual(client.input_pending(job['session_id'])['asks'][0]['requestId'],pending['requestId'])
                with self.assertRaises(util.KanpilotError): client.input_respond(job['session_id'],pending['requestId'],pending['toolCallId'],[['A']],operation_id='op')
                with self.assertRaises(util.KanpilotError): client._write('kp_runtime_stop','op',{'session_id':job['session_id']})
                self.assertFalse(list(self.run_dir.glob('answer-*.json')))
                raise process_owner.OwnerClosed('end read-only fixture')
        with self.guards(callback): self.assertEqual(worker.run_real(self.spec),3)
        self.assertEqual(len(self.job()['turns']),1)

    def test_sigterm_cleans_cli_and_descendants(self):
        self.stub('hang')
        sent=[]
        def check():
            if not sent and (self.run_dir/'codex-final-1.json.child').exists():
                sent.append(True)
                os.kill(os.getpid(),signal.SIGTERM)
        self.owner.check.side_effect=check
        with self.guards(): self.assertEqual(worker.run_real(self.spec),3)
        self.assertEqual(self.job()['status'],'aborted')
        self.assertIsNone(process_owner.process_birth(self.job()['codex_cli_pid']))
        child=int((self.run_dir/'codex-final-1.json.child').read_text())
        for _ in range(30):
            if process_owner.process_birth(child) is None: break
            time.sleep(.02)
        self.assertIsNone(process_owner.process_birth(child))


if __name__=='__main__': unittest.main()
