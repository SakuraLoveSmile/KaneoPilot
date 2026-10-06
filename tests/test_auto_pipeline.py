"""Full v5 pipeline with real local Git, fake Claude/gh and loopback Plus.

No model, GitHub or Kaneo production service is used. The fake Plus actually
writes in the task branch and runs the declared verification command; the real
worker records its exit status through the MCP event adapter.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core' / 'tests'))
from test_pi_desktop_backend import PiDesktopCase, TURN_ID
from test_git_delivery import make_repo, git
from kanpilot import (auto_dispatch, auto_intake, auto_monitor, automation_policy, cli_review, core,
                      db, delivery_runner, git_delivery, planner_runner, util)

TASK = 'pipelinefeedback000000001'
PROJECT = 'ryryo1reph1xfemqtwkjyiiz'
VERIFY = 'python3 -m unittest discover -s tests'


class TestAutoPipeline(PiDesktopCase):
    def setUp(self):
        super().setUp()
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
            'if "conclusion" in schema["properties"]:\n'
            ' p=pathlib.Path(' + repr(str(review_count)) + '); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))\n'
            ' result={"conclusion":"changes_requested" if n==1 else "pass","report_markdown":"# Review\\nRepair requested issue" if n==1 else "# Review\\nAll checks passed","fix":{"title":"Fix feedback","description":"Please append a second verified round","priority":"high"} if n==1 else None}\n'
            'else: result={"status":"ready","plan_markdown":' + repr(plan) + ',"questions":[],"evidence":[]}\n'
            'print(json.dumps({"type":"system","subtype":"init","tools":["Read","Grep","Glob"],"session_id":"fake-claude"}),flush=True)\n'
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
        original = self.server.tool_call
        def plus(name,args):
            response = original(name,args)
            if name == 'pi_plans_resolve' and args.get('action')=='approve':
                worktree = Path(self.server.created['projectPath'])
                (worktree / 'feedback.txt').write_text('round ' + str(self.round) + '\n')
                result = subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],cwd=worktree,capture_output=True,text=True)
                self.server.messages = [{'id':'verify-'+str(self.round),'role':'toolResult','toolName':'exec',
                    'toolArgs':{'command':VERIFY},'toolResult':{'output':result.stdout+result.stderr,'exit_code':None if self.omit_exit else result.returncode},
                    'exitCode':None if self.omit_exit else result.returncode,'turnId':self.server.execution_turn}]
            return response
        self.server.tool_call = plus

    def _stage(self, module, stage):
        conn = db.connect(self.db_path)
        try:
            rc = module.run_stage(stage,conn=conn,db_path=self.db_path,timeout=180,
                log_path=self.root / (stage + '-' + str(self.round) + '.log'),record_run=lambda **fields:None)
            self.assertEqual(rc,0)
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
        self.server.messages=[]
        self.server.planning_state='awaiting_approval'
        self.server.plans=[{'id':'goal-'+str(self.round),'sessionId':self.server.session_id,'turnId':TURN_ID,
            'toolCallId':'goal-tool','kind':'goal','status':'pending','version':1,'markdown':'Execute the verified plan',
            'createdAt':util.now_iso(),'updatedAt':util.now_iso()}]
        self._stage(auto_dispatch,'dispatch')
        value=self.task(TASK)
        self.assertEqual(value['status'],'running',value)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            detail=self.call('get-session',{'task_id':TASK,'run_id':value['run_id']}).get('round_detail') or {}
            if detail.get('terminal'): break
            time.sleep(.05)
        self.assertEqual(detail.get('exit_code'),1 if should_fail else 0,detail)
        self.assertEqual(detail.get('terminal',{}).get('outcome'),'failed' if should_fail else 'completed',detail)
        return value['run_id']

    def _monitor(self):
        # Simulate the next scheduled cycle without a two-minute test sleep.
        conn=db.connect(self.db_path)
        try:
            conn.execute("UPDATE requests SET not_before=NULL WHERE kind='monitor' AND status='queued'")
        finally: conn.close()
        self.call('tick')
        self._stage(auto_monitor,'monitor')

    def _review(self):
        self._stage(cli_review,'review')

    def test_failed_execution_replan_preserves_partial_work(self):
        self._adopt_and_plan();self._execute(should_fail=True);self._monitor()
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
        self._review()
        self.assertEqual(self.task(TASK)['status'],'needs_plan',self.task(TASK))
        self.assertEqual(self.task(TASK)['automation']['fix_round'],1)
        self._adopt_and_plan();second=self._execute();self._monitor();self._review()
        value=self.task(TASK)
        self.assertEqual(value['status'],'in_review',value)
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
        self._adopt_and_plan();self.omit_exit=True;self._execute();self._monitor()
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
        value=self.task(TASK)
        self.assertEqual(value['failure_code'],'fix_rounds_exhausted',value)
        self.assertIn('resume',value['allowed_actions'])
        self.assertEqual(value['extra']['fix_request']['state'],'held')
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
