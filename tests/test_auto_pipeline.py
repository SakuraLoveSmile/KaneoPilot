"""Full v5 pipeline with real local Git, fake Claude/gh and loopback Plus.

No model, GitHub or Kaneo production service is used. The fake Plus actually
writes in the task branch and runs the declared verification command; the real
worker records its exit status through the MCP event adapter.
"""
import json
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core' / 'tests'))
from test_pi_desktop_backend import PiDesktopCase, TURN_ID
from test_git_delivery import make_repo, git
from kanpilot import (auto_dispatch, auto_intake, auto_monitor, automation_policy, cli_review, constants, core,
                      db, delivery_runner, git_delivery, planner_runner, util)

TASK = 'pipelinefeedback000000001'
PROJECT = 'fixture-project-feedback'
VERIFY = 'python3 -m unittest discover -s tests'


def _fully_settled(detail: dict) -> bool:
    """get-session round_detail 的完整结算证据，与生产
    cmd_queue._require_execution_terminal 同判定方向：terminal 存在、
    exit_code 已落盘（is not None——0 不得当假）、run_status 为
    finished/failed、观察者已退出（observer_alive is False）。
    cmd_get_session 先读 job 再读 events，job-finalize→terminal 写入间隙
    会读到 terminal.completed + exit None + running + 观察者仍存活的
    混合视图，不得当作结算（Debug 证据 20261015）。"""
    return bool(detail.get('terminal')) and detail.get('exit_code') is not None \
        and detail.get('run_status') in ('finished', 'failed') \
        and detail.get('observer_alive') is False


def _wait_for(read, done, *, deadline, sleep=time.sleep, interval=.05, monotonic=time.monotonic):
    """有界轮询：read() 取视图，done(view) 为停止条件（达到目标或出现
    不可重试状态）；超时返回最后一次视图由调用方断言报错。
    deadline 固定，不无限延长、不空转。"""
    while True:
        view = read()
        if done(view) or monotonic() >= deadline:
            return view
        sleep(interval)


def _assert_settled(case, detail: dict) -> None:
    """有界轮询超时后的显式终态结算失败门：不满足 _fully_settled 一律
    AssertionError（消息带最后一次完整 detail），不得只靠 exit_code/outcome
    偶然相等收场——观察者永不退出、run_status 仍 running、terminal 缺失的
    视图都会被生产 cmd_queue._require_execution_terminal 拒绝（Debug 探针
    20261015 场景 A/C/D）。放在既有精确断言之前，因此 terminal 为 None/缺失
    时走本门的 AssertionError+detail，而不是 None.get(...) 的
    AttributeError（丢 detail）。"""
    case.assertTrue(_fully_settled(detail),
                    '固定 deadline 内未读到完整结算证据（terminal 存在、'
                    'exit_code 已落盘、run_status finished/failed、'
                    f'观察者已退出），最后一次 detail={detail}')


def _monitor_settled(expect: dict):
    """_monitor_until 的停止条件：仅「尚未达标且仍处在合法异步结算
    窗口」（观察者 --resume 会把已结算 run 瞬时重置为 running，下一轮
    读取即自愈）可继续有界重试；其余与目标不符的已结算状态立即停止，
    原样返回交调用点精确断言报错——不把任何失败当可重试吞掉。"""
    def done(value):
        return all(value.get(key) == want for key, want in expect.items()) \
            or value.get('status') != 'running'
    return done


class TestAutoPipeline(PiDesktopCase):
    def setUp(self):
        super().setUp()
        # Retain the exact child handles: a resumed observer can outlive the
        # assertion and otherwise consume the next fixture's reused Plus port.
        self.worker_processes = []
        real_popen = subprocess.Popen
        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            argv = args[0] if args else kwargs.get('args', [])
            if isinstance(argv, (list, tuple)) and 'kanpilot.pi_desktop_worker' in argv:
                self.worker_processes.append(process)
            return process
        worker_spawn = mock.patch('kanpilot.cmd_dispatch.subprocess.Popen', side_effect=popen)
        worker_spawn.start()
        self.addCleanup(worker_spawn.stop)
        self.addCleanup(self._stop_workers)
        self.repo = make_repo(self.root / 'git')
        tests = self.repo / 'tests'; tests.mkdir()
        (tests / 'test_feedback.py').write_text("import unittest\nfrom pathlib import Path\nclass Feedback(unittest.TestCase):\n def test_feedback(self):\n  self.assertTrue(Path('feedback.txt').read_text().startswith('round '))\n")
        git(self.repo,'add','tests');git(self.repo,'commit','-m','verification baseline');git(self.repo,'push','origin','main')
        (self.repo / 'keep-local.txt').write_text('existing primary checkout data')
        self.primary = (git(self.repo,'rev-parse','HEAD'),git(self.repo,'status','--porcelain'))
        self.comments = []
        request = self.fake.request
        def kaneo(path, method='GET', body=None):
            if path == '/api/comment/' + TASK:
                if method == 'POST':
                    self.comments.append({'id':str(len(self.comments)+1),'content':body['content'],'createdAt':util.now_iso()})
                    return {'data':self.comments[-1]}
                return {'data':self.comments}
            if path == '/api/label/task/' + TASK: return {'data':[]}
            return request(path,method,body)
        self.fake.request = kaneo
        self.fake.add_task(TASK, PROJECT, status='to-do', title='Repair feedback', description='Implement and verify feedback')
        self.gh_state = self.root / 'gh-state.json'
        self.gh_state.write_text(json.dumps({'state':'OPEN','mergedAt':None}))
        claude = self.root / 'fake-claude'
        review_count = self.root / 'review-count'
        plan = '# Plan\n\nWrite feedback.txt and fix issues from the supplied Review report.\n\n```verify\n' + VERIFY + '\n```\n'
        claude.write_text('#!' + sys.executable + '\nimport json,sys,pathlib\n'
            'if "--version" in sys.argv: print("fake-claude 1"); sys.exit(0)\n'
            'prompt=sys.stdin.read()\nschema=json.loads(sys.argv[sys.argv.index("--json-schema")+1])\n'
            'print(json.dumps({"type":"system","subtype":"init","tools":["Read","Grep","Glob"],"session_id":"fake-claude"}),flush=True)\n'
            'if "conclusion" in schema["properties"]:\n'
            ' if pathlib.Path(' + repr(str(self.root / 'fail-review')) + ').exists(): sys.stderr.write("isolated Review process failure\\n"); sys.exit(1)\n'
            ' p=pathlib.Path(' + repr(str(review_count)) + '); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))\n'
            ' result={"conclusion":"changes_requested" if n==1 else "pass","report_markdown":"# Review\\nRepair requested issue" if n==1 else "# Review\\nAll checks passed","fix":{"title":"Fix feedback","description":"Please append a second verified round","priority":"high"} if n==1 else None}\n'
            'else: result={"status":"ready","plan_markdown":' + repr(plan) + ',"questions":[],"evidence":[]}\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,"structured_output":result,"total_cost_usd":0.1}),flush=True)\n')
        claude.chmod(0o755)
        gh = self.root / 'fake-gh'
        gh.write_text('#!' + sys.executable + '\nimport json,os,sys,pathlib,subprocess\n'
            'p=pathlib.Path(os.environ["PIPELINE_GH_STATE"]); state=json.loads(p.read_text()); a=sys.argv[1:]\n'
            'if "--version" in a: print("gh fake"); sys.exit(0)\n'
            'branch=os.environ.get("PIPELINE_BRANCH")\n'
            'sha=subprocess.check_output(["git","-C",branch,"rev-parse","HEAD"]).decode().strip() if branch else ""\n'
            'row={"number":7,"url":"https://github.com/owner/repo/pull/7","state":state["state"],"mergedAt":state["mergedAt"],"headRefOid":sha,"baseRefName":"main","headRefName":' + repr('kaneo/' + TASK) + ',"isCrossRepository":False}\n'
            'if a[:2]==["pr","list"]: print(json.dumps([row] if state.get("created") else []))\n'
            'elif a[:2]==["pr","create"]: state["created"]=True;p.write_text(json.dumps(state));print(row["url"])\n'
            'elif a[:2]==["pr","view"]: print(json.dumps(row))\n'
            'else: sys.exit(2)\n')
        gh.chmod(0o755)
        self.patch_env = mock.patch.dict(os.environ, {'KANPILOT_CLAUDE_BIN':str(claude),'KANPILOT_GH_BIN':str(gh),
            'CLAUDE_CONFIG_DIR':str(self.root / 'claude-config'),
            'KANPILOT_WORKTREES_ROOT':str(self.root / 'trees'),'KANPILOT_DB':str(self.db_path),
            'PIPELINE_GH_STATE':str(self.gh_state),'KANPILOT_PI_DESKTOP_START_WAIT':'5'})
        self.patch_env.start();self.addCleanup(self.patch_env.stop)
        os.environ.pop('KANPILOT_PI_DESKTOP_FAKE',None)
        github = mock.patch.object(git_delivery,'origin_github_repo',return_value='owner/repo')
        github.start();self.addCleanup(github.stop)
        # Only this disposable bare repository stands in for the approved GitHub target.
        bare_target = git(self.repo, 'remote', 'get-url', '--push', 'origin')
        real_repo_parser = git_delivery.github_repository
        push_identity = mock.patch.object(git_delivery, 'github_repository', side_effect=lambda url, **kwargs:
            'owner/repo' if url == bare_target else real_repo_parser(url, **kwargs))
        push_identity.start();self.addCleanup(push_identity.stop)
        github_policy=mock.patch.object(automation_policy,'github_repository',return_value='owner/repo')
        github_policy.start();self.addCleanup(github_policy.stop)
        self.call('set-project-mapping', {'project_id':PROJECT,'repository':str(self.repo),'enabled':True,'expected_revision':0})
        self.call('set-planner-config', {'backend':'claude','claude':{'model':'test-sonnet','effort':'high'}})
        self.call('set-review-config', {'backend':'claude','claude':{'model':'test-sonnet','effort':'high'}})
        self.call('set-exec-config', {'backend':'pi_desktop','pi_desktop':{'model_source':'custom','connection_file':str(self.connection_file),
            'provider_id':'test-provider','model_id':'test-model','thinking_level':'medium','permission_mode':'auto'}})
        self.call('set-project-automation', {'project_id':PROJECT,'automation':{'enabled':True}})
        self.round = 0
        self.omit_exit = False
        self.round_contents = {}
        self.completed_turns = {}
        # This actor implements the current Plus command-binding/report
        # contract; the shared backend unit fixture deliberately defaults to
        # an older host so its compatibility tests remain meaningful.
        self.server.required_verification_commands = True
        self.server.tools.extend(name for name in constants.PI_DESKTOP_GOAL_REPORT_TOOLS
                                 if name not in self.server.tools)
        original = self.server.tool_call
        def plus(name,args):
            identity = (args.get('sessionId'), args.get('turnId'))
            if name == 'pi_agent_status' and args.get('sessionId') != self.server.session_id:
                previous = [turn for session, turn in self.completed_turns
                            if session == args.get('sessionId')]
                if previous:
                    return {'structuredContent': {'status': {'sessionId':args['sessionId'],
                        'currentTurnId':previous[-1], 'isRunning':False,
                        'planningState':'inactive', 'pendingToolConfirmations':[]}}}
            if name == 'pi_turn_get' and identity in self.completed_turns:
                return {'structuredContent': {'sessionId':identity[0], 'turnId':identity[1],
                                              'status':self.completed_turns[identity]}}
            response = original(name,args)
            if name == 'pi_plans_resolve' and args.get('action')=='approve':
                execution_id = 'exec-' + str(self.round)
                response['structuredContent']['execution']['id'] = execution_id
                self._plus_execute_approved_goal(args, execution_id)
            return response
        self.server.tool_call = plus

    def _plus_execute_approved_goal(self, approval, execution_id):
        """The fake Plus actor owns writes and exact frozen verify execution."""
        commands = approval.get('requiredVerificationCommands')
        self.assertEqual(commands, [VERIFY], approval)
        self.assertEqual(approval.get('proposalId'), 'goal-' + str(self.round), approval)
        worktree = Path(self.server.created['projectPath'])
        content = self.round_contents.get(self.round, 'round ' + str(self.round))
        if content is not None:
            (worktree / 'feedback.txt').write_text(content + '\n', encoding='utf-8')
        messages, checks = [], []
        for index, command in enumerate(commands, 1):
            result = subprocess.run(command, shell=True, executable='/bin/sh', cwd=worktree,
                                    capture_output=True, text=True, timeout=120)
            code = None if self.omit_exit else result.returncode
            output = result.stdout + result.stderr
            messages.append({'id':'verify-%s-%s' % (self.round, index), 'role':'toolResult',
                'toolName':'exec', 'toolArgs':{'command':command, 'cwd':str(worktree)},
                'toolResult':{'output':output, 'exit_code':code}, 'exitCode':code,
                'turnId':self.server.execution_turn})
            checks.append({'id':'C' + str(index), 'command':command, 'exitCode':code,
                'result':None if code is None else ('passed' if code == 0 else 'failed'),
                'detail':output})
        # Generated artifacts exercise delivery filtering without changing
        # source identity, independent of PYTHONDONTWRITEBYTECODE.
        cache = worktree / 'tests' / '__pycache__'
        cache.mkdir(exist_ok=True)
        (cache / 'fixture.pyc').write_bytes(b'rehearsal-build-artifact')
        body = {'sessionId':self.server.session_id, 'proposalId':approval['proposalId'],
            'executionId':execution_id, 'turnId':self.server.execution_turn,
            'reportId':'report-' + execution_id, 'integrity':{'kind':'structured', 'issues':[]},
            'execution':{'status':'completed'},
            'verdict':'met' if all(row['exitCode'] == 0 for row in checks) else 'partial',
            'checks':checks}
        self.server.messages = messages
        self.server.goal_reports = [{key:body[key] for key in
                                     ('reportId', 'executionId', 'proposalId', 'turnId', 'sessionId')}]
        self.server.goal_report = {'state':'ready', 'report':body,
                                  'report_sha256':util.sha256_bytes(util.jdump(body).encode())}
        self.server.is_running = False
        self.completed_turns[(self.server.session_id, self.server.execution_turn)] = self.server.turn_status

    def _stop_workers(self):
        for process in self.worker_processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def _stage(self, module, stage, *, expected_exit=0):
        conn = db.connect(self.db_path)
        try:
            rc = module.run_stage(stage,conn=conn,db_path=self.db_path,timeout=180,
                log_path=self.root / (stage + '-' + str(self.round) + '.log'),record_run=lambda **fields:None)
            self.assertEqual(rc,expected_exit)
        finally: conn.close()

    def _adopt_and_plan(self):
        summary=auto_intake.run_once(self.db_path)
        self.assertEqual(summary['errors'],[],summary)
        self.assertTrue(any(TASK in p['adopted'] for p in summary['projects']),summary)
        value=self.task(TASK)
        os.environ['PIPELINE_BRANCH']=value['delivery']['worktree_path']
        self.assertTrue(planner_runner.run_one(self.db_path))
        value=self.task(TASK)
        self.assertEqual(value['status'],'ready',value)
        self.assertEqual(value['extra']['plan_verify']['commands'],[VERIFY])
        self.call('tick')
        self.assertFalse(self.task(TASK)['plan_execution_hold'])

    def _execute(self, should_fail=False):
        self.round += 1
        self.server.session_id='pipeline-session-'+str(self.round)
        self.server.turn_statuses=['running','completed']
        self.server.turn_status='error' if should_fail else 'completed'
        self.server.execution_turn='pipeline-execution-'+str(self.round)
        self.server.current_turn=None
        self.server.is_running=True
        self.server.messages=[]
        self.server.planning_state='awaiting_approval'
        self.server.plans=[{'id':'goal-'+str(self.round),'sessionId':self.server.session_id,'turnId':TURN_ID,
            'toolCallId':'goal-tool','kind':'goal','status':'pending','version':1,'markdown':'Execute the verified plan',
            'createdAt':util.now_iso(),'updatedAt':util.now_iso()}]
        self._stage(auto_dispatch,'dispatch')
        value=self.task(TASK)
        self.assertEqual(value['status'],'running',value)
        # 有界轮询完整结算证据：仅 terminal 到达而 job 未定稿
        # （exit None / run_status running / 观察者仍在）的混合视图不算结算；
        # 超时先由结算门带最后一次视图失败，再进行既有精确结果断言。
        detail=_wait_for(lambda: self.call('get-session',{'task_id':TASK,'run_id':value['run_id']}).get('round_detail') or {},
                         _fully_settled,deadline=time.monotonic()+15)
        _assert_settled(self,detail)
        self.assertEqual(detail.get('exit_code'),1 if should_fail else 0,detail)
        self.assertEqual(detail.get('terminal',{}).get('outcome'),'failed' if should_fail else 'completed',detail)
        return value['run_id']

    def _monitor(self):
        # Simulate the next scheduled cycle without a two-minute test sleep.
        conn=db.connect(self.db_path)
        try:
            conn.execute("UPDATE requests SET not_before=NULL WHERE kind='monitor' AND status='queued'")
            # A prior observation may finish while its resumed worker is still
            # settling. Advance the finished-request interval as well, so the
            # bounded retry represents another scheduler cycle.
            due = (util.now_dt() - timedelta(seconds=util.monitor_interval(conn) + 1)).isoformat()
            conn.execute("UPDATE requests SET finished_at=? WHERE task_id=? AND kind='monitor' "
                         "AND status IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL",
                         (due, TASK))
        finally: conn.close()
        self.call('tick')
        self._stage(auto_monitor,'monitor')

    def _monitor_until(self, **expect):
        """已知合法异步结算窗口（--resume 瞬时 running）内有界重 tick/读取
        直至目标状态；不符状态立即停止；超时返回最后视图供调用点断言。"""
        def tick_and_read():
            self._monitor()
            return self.task(TASK)
        return _wait_for(tick_and_read, _monitor_settled(expect),
                         deadline=time.monotonic()+15)

    def _review(self, *, should_fail=False):
        self._stage(cli_review,'review',expected_exit=1 if should_fail else 0)

    def _assert_review_repair_held_at_limit(self):
        value = self.task(TASK)
        self.assertEqual(value['status'], 'in_review', value)
        repair = value['review_repair']
        self.assertEqual(repair['state'], 'held', value)
        self.assertFalse(repair['can_start'], repair)
        self.assertIn('修复次数已用尽', repair['reason'])
        self.assertEqual(repair['round'], repair['max_rounds'])
        before = self.call('list-requests', {})['requests']
        error = self.call_err('start-review-repair', {'task_id':TASK,
            'expected_execution_run_id':value['run_id'], 'expected_review_key':repair['review_key'],
            'expected_report_sha256':repair['report_sha256'], 'expected_plan_sha256':repair['plan_sha256']})
        self.assertIn('修复次数已用尽', error.message)
        self.call_err('resolve', {'task_id':TASK, 'action':'resume'})
        self.assertEqual(before, self.call('list-requests', {})['requests'])
        self.assertIsNone(self.task(TASK)['active_request'])
        return value

    def test_failed_execution_replan_preserves_partial_work(self):
        self._adopt_and_plan();self._execute(should_fail=True)
        self._monitor_until(failure_code='execution_failed')
        value=self.task(TASK)
        self.assertEqual(value['failure_code'],'execution_failed',value)
        self.assertEqual(self.fake.tasks[TASK]['status'],'in-progress')
        branch=Path(value['delivery']['worktree_path'])
        self.assertEqual((branch/'feedback.txt').read_text(),'round 1\n')
        self.assertFalse(value['delivery'].get('commits'))
        self.call('resolve',{'task_id':TASK,'action':'replan'})
        self._adopt_and_plan()
        self.assertTrue(self.task(TASK)['automation'].get('replan_worktree_sha256'))
        self.assertEqual((branch/'feedback.txt').read_text(),'round 1\n')
        self._execute();self._monitor()
        value=self.task(TASK)
        self.assertEqual(value['status'],'in_review',value)
        self.assertEqual(len(value['delivery']['commits']),1)
        self.assertEqual((branch/'feedback.txt').read_text(),'round 2\n')
        self.assertEqual(self.primary,(git(self.repo,'rev-parse','HEAD'),git(self.repo,'status','--porcelain')))

    def test_full_feedback_two_rounds_to_pr_merge(self):
        self._adopt_and_plan();first=self._execute();self._monitor()
        self.assertEqual(self.task(TASK)['status'],'in_review',self.task(TASK))
        plan_identity = (self.task(TASK)['plan_path'], self.task(TASK)['plan_sha256'])
        self._review()
        self.assertEqual(self.task(TASK)['status'],'ready',self.task(TASK))
        self.assertEqual(self.task(TASK)['review_repair']['state'],'queued')
        self.assertEqual(self.task(TASK)['automation']['fix_round'],1)
        second=self._execute();self._monitor();self._review()
        value=self.task(TASK)
        self.assertEqual(value['status'],'in_review',value)
        self.assertEqual((value['plan_path'], value['plan_sha256']), plan_identity)
        self.assertEqual(sum(a['stage'] == 'plan' for a in value['attempts']), 1)
        self.assertNotEqual(first, second)
        self.assertEqual([c['execution_run_id'] for c in value['delivery']['commits']],[first,second])
        self.call('tick');self._stage(delivery_runner,'delivery')
        value=self.task(TASK)
        self.assertEqual(value['delivery'].get('pr_number'),7,{'status':value['status'],'failure':value['failure_code'],'events':self.call('list-events',{'task_id':TASK,'limit':12}), 'gh_log':self.gh_state.read_text()})
        self.assertEqual(value['status'],'in_review')
        self.assertEqual(len(self.comments),1)
        self.assertEqual(self.comments[0]['content'],'[kaneopilot:delivery:'+TASK+'] 已创建 PR：https://github.com/owner/repo/pull/7')
        self.gh_state.write_text(json.dumps({'created':True,'state':'MERGED','mergedAt':util.now_iso()}))
        conn=db.connect(self.db_path)
        try: core.update_delivery(conn,db.require_task(conn,TASK),util.now_iso(),next_sync_at=None)
        finally: conn.close()
        self.call('tick');self._stage(delivery_runner,'delivery')
        self.assertEqual(self.task(TASK)['status'],'done',self.task(TASK))
        self.assertEqual(self.fake.tasks[TASK]['status'],'done')
        self.assertEqual(self.server.prompt_count,2)
        self.assertEqual(self.primary,(git(self.repo,'rev-parse','HEAD'),git(self.repo,'status','--porcelain')))
        self.assertTrue(Path(value['delivery']['worktree_path']).exists())

    def test_missing_verification_user_accepts_then_review(self):
        self._adopt_and_plan();self.omit_exit=True;self._execute()
        self._monitor_until(failure_code='verification_unconfirmed')
        value=self.task(TASK)
        self.assertEqual(value['failure_code'],'verification_unconfirmed')
        self.assertIn('accept_verification',value['allowed_actions'])
        self.call('resolve',{'task_id':TASK,'action':'accept_verification','note':'checked separately'})
        self._monitor()
        self.assertEqual(self.task(TASK)['status'],'in_review',self.task(TASK))
        self.assertEqual(len(self.task(TASK)['delivery']['commits']),1)

    def test_fix_round_limit_and_manual_takeover(self):
        self.call('set-project-automation',{'project_id':PROJECT,'automation':{'enabled':True,'max_fix_rounds':0}})
        self._adopt_and_plan();self._execute();self._monitor();self._review()
        value=self._assert_review_repair_held_at_limit()
        self.assertEqual(self.server.prompt_count, 1)
        self.assertTrue(Path(value['delivery']['worktree_path']).exists())

    def test_review_failure_manual_takeover_preserves_branch(self):
        self._adopt_and_plan();self._execute();self._monitor()
        (self.root / 'fail-review').touch()
        self._review(should_fail=True)
        self.assertEqual(self.task(TASK)['status'], 'needs_user', self.task(TASK))
        self.call('resolve',{'task_id':TASK,'action':'dismiss'})
        value=self.task(TASK)
        self.assertTrue(value['automation']['manual_takeover'])
        self.assertEqual(value['delivery']['mode'],'manual')
        self.assertFalse(any(TASK in p['adopted'] for p in auto_intake.run_once(self.db_path)['projects']))
        self.assertTrue(Path(value['delivery']['worktree_path']).exists())

    def test_closed_pr_enters_pending_without_completing_kaneo(self):
        (self.root / 'review-count').write_text('1')
        self._adopt_and_plan();self._execute();self._monitor();self._review()
        self.call('tick');self._stage(delivery_runner,'delivery')
        self.assertEqual(self.task(TASK)['delivery'].get('pr_number'),7,self.task(TASK))
        self.gh_state.write_text(json.dumps({'created':True,'state':'CLOSED','mergedAt':None}))
        conn=db.connect(self.db_path)
        try: core.update_delivery(conn,db.require_task(conn,TASK),util.now_iso(),next_sync_at=None)
        finally: conn.close()
        self.call('tick');self._stage(delivery_runner,'delivery')
        value=self.task(TASK)
        self.assertEqual(value['failure_code'],'pr_closed',value)
        self.assertEqual(value['status'],'needs_user')
        self.assertEqual(self.fake.tasks[TASK]['status'],'in-review')
        self.assertIn('resume',value['allowed_actions'])


class TestSettlementWait(unittest.TestCase):
    """结算等待条件的确定性回归：序列 stub 替换真实 worker/MCP，
    时钟与 sleep 均注入，不含真实等待。覆盖 Debug 混合结算窗口、
    永久不结算超时、合法 running 窗口重试、意外终态不得误吞、
    超时后结算门不得因 exit_code 偶然相等放行、terminal 缺失必须
    AssertionError（而非 AttributeError 丢 detail）六类行为。"""

    TRAP = {'terminal': {'outcome': 'completed'}, 'exit_code': None,
            'run_status': 'running', 'observer_alive': True}
    SETTLED = {'terminal': {'outcome': 'completed'}, 'exit_code': 0,
               'run_status': 'finished', 'observer_alive': False}
    # 只差一项结算证据但 exit_code 已是 0、terminal 已 completed 的视图：
    # 旧写法（仅断言这两项）全部偶然 PASS，生产终态守卫却全部拒绝。
    ZERO_EXIT_VIEWS = (
        {'terminal': {'outcome': 'completed'}, 'exit_code': 0,
         'run_status': 'finished', 'observer_alive': True},
        {'terminal': {'outcome': 'completed'}, 'exit_code': 0,
         'run_status': 'running', 'observer_alive': False},
    )

    def run_wait(self, views, done, deadline=3):
        remaining = list(views)
        reads = []
        def read():
            reads.append(None)
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]
        result = _wait_for(read, done, deadline=deadline,
                           sleep=lambda _seconds: None,
                           monotonic=iter(range(1000)).__next__)
        return result, len(reads)

    def test_mixed_settle_view_is_repollled_until_full_evidence(self):
        # Debug 证据窗口：terminal.completed 但 exit None/running/观察者存活
        # 的视图不算结算；下一轮读到完整证据才返回。exit_code 0 不判假。
        self.assertFalse(_fully_settled(self.TRAP))
        self.assertTrue(_fully_settled(self.SETTLED))
        result, reads = self.run_wait([self.TRAP, self.SETTLED], _fully_settled)
        self.assertEqual(result, self.SETTLED)
        self.assertEqual(reads, 2)

    def test_never_settled_view_times_out_with_last_view(self):
        # 永久混合视图必须在固定 deadline 后停止并交出最后视图（由调用方
        # 断言报错），轮询次数有界、不无限 tick。
        result, reads = self.run_wait([self.TRAP], _fully_settled)
        self.assertEqual(result, self.TRAP)
        self.assertEqual(reads, 4)  # monotonic 0/1/2/3，首次 >= deadline 即停

    def test_monitor_retries_running_then_target_state(self):
        # 合法异步窗口：任务仍 running 时有界重 tick，目标 failure_code
        # 出现即停；既有精确断言仍由调用点执行。
        running = {'status': 'running'}
        target = {'status': 'needs_user', 'failure_code': 'execution_failed'}
        result, reads = self.run_wait([running, running, target],
                                      _monitor_settled({'failure_code': 'execution_failed'}))
        self.assertEqual(result, target)
        self.assertEqual(reads, 3)

    def test_monitor_unexpected_terminal_state_never_falsely_passes(self):
        # 与目标不符的已结算状态不是可重试的 running 窗口：立即停止并
        # 原样返回该视图，由调用点精确断言报错，不吞不符结果。
        wrong = {'status': 'needs_user', 'failure_code': 'other_code'}
        done = _monitor_settled({'failure_code': 'execution_failed'})
        self.assertTrue(done(wrong))
        result, reads = self.run_wait([{'status': 'running'}, wrong], done)
        self.assertEqual(result, wrong)
        self.assertEqual(reads, 2)

    def test_stuck_observer_with_zero_exit_fails_after_timeout(self):
        # 缺口实证：观察者一直存活（或 run_status 仍 running）时，超时后
        # 即使 exit_code 0 + terminal completed 也必须 AssertionError，
        # 不得只靠 exit_code 偶然失败。
        for view in self.ZERO_EXIT_VIEWS:
            with self.subTest(view=view):
                result, reads = self.run_wait([view], _fully_settled)
                self.assertEqual(result, view)  # 超时交出最后视图
                self.assertEqual(reads, 4)      # 固定 deadline，有界轮询
                self.assertFalse(_fully_settled(view))
                # 旧写法的两条精确断言在这里都会 PASS——缺口所在
                self.assertEqual(view.get('exit_code'), 0)
                self.assertEqual(view['terminal'].get('outcome'), 'completed')
                with self.assertRaises(AssertionError) as caught:
                    _assert_settled(self, view)
                message = str(caught.exception)
                self.assertIn('observer_alive', message)
                self.assertIn('run_status', message)  # 带完整最后 detail

    def test_absent_terminal_fails_with_detail_not_attribute_error(self):
        # terminal 缺失/为 None：必须是带 detail 的 AssertionError，
        # 而不是 None.get('outcome') 抛 AttributeError 丢掉诊断信息。
        for view in ({'exit_code': 0, 'run_status': 'finished', 'observer_alive': False},
                     {'terminal': None, 'exit_code': 0, 'run_status': 'finished',
                      'observer_alive': False}):
            with self.subTest(view=view):
                self.assertFalse(_fully_settled(view))
                result, reads = self.run_wait([view], _fully_settled)
                self.assertEqual(result, view)
                self.assertEqual(reads, 4)
                with self.assertRaises(AssertionError) as caught:
                    _assert_settled(self, view)
                self.assertIn('run_status', str(caught.exception))
