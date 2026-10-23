"""Explicit manual pass continuation: isolated SQLite/Git/Goal and fake Kaneo."""
from copy import deepcopy
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import test_parent_closeout as fixtures
import test_codex_verdict as codex_fixtures
from test_git_delivery import git
from kanpilot import (cli, cmd_delivery_pr, cmd_exec, core, db, delivery_contract, derive,
                      engine_claim_scope, gh_client, process_owner, project_config, release_delivery, util)
from kanpilot.engine import commands as engine_commands, health, loop, verdict


class ReviewDeliveryTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ParentCloseoutTest('test_success_reuses_final_checkpoint_without_worktree_or_new_model')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn, self.ctx = self.fixture.conn, self.fixture.ctx
        self.conn.execute("DELETE FROM tasks WHERE task_id IN ('fix1','fix2')")
        task = self.task()
        delivery = core.delivery_of(task)
        path = self.fixture.root / 'manual-worktree'
        git(self.fixture.repo, 'worktree', 'add', '-b', delivery['branch'], str(path), delivery['head_sha'])
        delivery.update(worktree_path=str(path), workspace_version=1)
        report = self.fixture.reports_root / 'parent-pass.md'
        sha = util.sha256_file(report)
        review = dict(conclusion='pass', execution_run_id=task['run_id'], head_sha=delivery['head_sha'],
            review_key='pass-parent', report_path=str(report), report_sha256=sha,
            **{k: delivery[k] for k in ('branch','base_branch','base_sha','worktree_path')})
        db.update_task(self.conn, 'parent', self.ctx.now_iso, fix_task_id=None, review_key='pass-parent',
            review_report_path=str(report), review_report_sha256=sha)
        extra = db.task_extra(self.task())
        receipt = deepcopy(extra['engine_verdict'])
        receipt['binding']['worktree_path'] = str(path)
        directory = self.fixture.runs_root / 'parent' / 'run-parent'
        spec_path = directory / 'spec.json'
        spec = util.jload(spec_path.read_text()); spec['cwd'] = str(path)
        spec_path.write_text(util.jdump(spec))
        receipt['evidence_sha256'][str(spec_path)] = util.sha256_file(spec_path)
        self.conn.execute("UPDATE execution_rounds SET worktree_path=? WHERE task_id='parent'", (str(path),))
        request, _ = db.create_request(self.conn, self.ctx.now_iso, 'review', 'manual-pass-review', task_id='parent')
        db.update_request(self.conn, request['request_id'], self.ctx.now_iso, status='succeeded', run_id='review-run',
            finished_at=self.ctx.now_iso, result_json=dict(outcome='succeeded'))
        identity = dict(request_id=request['request_id'], run_id='review-run', execution_run_id=task['run_id'],
            report_sha256=sha, plan_sha256=task['plan_sha256'])
        db.set_extra(self.conn, self.task(), self.ctx.now_iso, delivery=delivery, engine_verdict=receipt,
            review_conclusion='pass', review_registered_at=self.ctx.now_iso,
            review_history=[review], registered_review_identity=identity)
        self.policy = dict(enabled=True, required_assets=['app.apk'])
        self.configure()
        self.payload = dict(task_id='parent', action='continue_delivery',
            expected_execution_run_id='run-parent', expected_review_key='pass-parent')

    def configure(self):
        db.set_setting(self.conn, project_config.SETTINGS_KEY, dict(version=1, revision=1, items={
            'project': dict(enabled=True, repository=str(self.fixture.repo), engine_mode='legacy',
                automation=dict(enabled=False, policy_revision=1, release=self.policy))}))

    def task(self):
        return db.require_task(self.conn, 'parent')

    def resolve(self, **overrides):
        return cli.run('resolve', dict(self.payload, **overrides), self.fixture.db_path)

    def block_old_pass(self):
        identity = db.task_extra(self.task())['registered_review_identity']
        request = db.get_request(self.conn, identity['request_id'])
        db.update_request(self.conn, request['request_id'], self.ctx.now_iso, status='failed',
            result_json=dict(outcome='needs_user', failure_code='review_blocked',
                note='advance-task 失败：release_required: 等待原始任务与全部修复具备合并和验证证据'))
        core.block_task(self.conn, self.task(), self.ctx.now_iso,
            'advance-task 失败：release_required: 等待原始任务与全部修复具备合并和验证证据',
            'review_blocked', 'kaneo-review', stage='review')

    def test_pass_enters_existing_pr_chain_preserving_every_execution_and_review_identity(self):
        before = dict(self.task()); extra = db.task_extra(self.task())
        report = Path(before['review_report_path']).read_bytes()
        result = self.resolve()
        self.assertEqual(result['task_status'], 'in_review')
        self.assertEqual(core.delivery_of(self.task())['mode'], 'pr')
        self.assertEqual(result['request']['kind'], 'delivery')
        for key in ('run_id','plan_sha256','plan_path','requirements_sha256','review_key','review_report_sha256','review_report_path'):
            self.assertEqual(self.task()[key], before[key])
        for key in ('registered_review_identity','review_history','engine_verdict','plan_verify'):
            self.assertEqual(db.task_extra(self.task())[key], extra[key])
        self.assertEqual(Path(before['review_report_path']).read_bytes(), report)
        self.assertFalse(core.is_automation_managed(self.task()))
        self.assertFalse(any(method != 'GET' for method, _, _ in self.fixture.fake.requests_log))
        again = self.resolve()
        self.assertTrue(again['deduplicated'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='delivery'").fetchone()[0], 1)

    def test_old_release_required_block_with_failed_request_recovers_pass_without_new_review(self):
        self.block_old_pass()
        actions = derive.allowed_actions(self.conn, self.task(), self.ctx.now)
        self.assertIn('continue_delivery', actions)
        self.assertEqual(actions[0], 'continue_delivery')
        self.assertEqual(derive.task_display_status(self.conn, self.task(), self.ctx.now), '待 PR 交付')
        self.assertIsNone(derive.task_exception(self.conn, self.task(), self.ctx.now))
        release = release_delivery.projection(self.conn, self.task())
        self.assertEqual(release['title'], '待 PR 交付')
        view = derive.task_summary(self.conn, self.task(), self.ctx.now)
        self.assertEqual(view['board_column'], 'in_review')
        self.assertEqual(view['board_substatus'], '待 PR 交付')
        self.assertEqual(view['phase'], 'delivery')
        self.assertEqual(view['attention']['reason_code'], 'delivery_blocked')
        self.assertEqual(view['attention']['recommended'], 'continue_delivery')
        self.assertNotIn('release_required', view['attention']['failure_reason'])
        inbox = engine_commands.attention_items(self.conn, self.ctx.now, self.ctx.now_iso)
        self.assertEqual(inbox[0]['reason_code'], 'delivery_blocked')
        self.assertEqual(inbox[0]['recommended'], 'continue_delivery')
        self.assertNotIn('release_required', inbox[0]['failure_reason'])
        self.assertFalse({'retry','enqueue','recover_review','resolve_review_blocker'} & set(actions))
        result = self.resolve()
        self.assertEqual(result['task_status'], 'in_review')
        self.assertIsNone(self.task()['failure_code'])
        self.assertEqual(db.task_extra(self.task())['review_conclusion'], 'pass')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='review'").fetchone()[0], 1)

    def test_single_pass_waits_for_current_pr_checks_without_claiming_associated_fixes(self):
        self.resolve()
        self.assertEqual(release_delivery.projection(self.conn, self.task())['title'], 'PR 交付中')
        head = core.delivery_of(self.task())['head_sha']
        for notice_head in (head, 'older-head'):
            core.update_delivery(self.conn, self.task(), self.ctx.now_iso, pr_number=8, pr_state='OPEN',
                auto_merge=dict(head_sha=notice_head, state='waiting_checks', reason='Test 尚未完成，继续观察'))
            release = release_delivery.projection(self.conn, self.task())
            self.assertEqual(release['title'], '等待 PR 合并')
            self.assertEqual(release['reason']=='Test 尚未完成，继续观察', notice_head==head)
            self.assertFalse(release['can_start'])

    def test_current_release_preflight_block_is_visible_and_stale_identity_is_not_reused(self):
        self.resolve()
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso, pr_number=8,
            pr_url='https://github.com/example/closeout/pull/8',
            pr_state='MERGED', merged_at=self.ctx.now_iso)
        snapshot = core.delivery_of(self.task())
        payload = dict(request_id='delivery-sync', run_id='delivery-sync-run')
        contract = delivery_contract.freeze(self.task(), payload, snapshot, 'sync')
        blocked = dict(state='blocked', title='发布受阻', reason='发布目标不是本轮已验证的完整源码',
            root_task_id='parent', member_task_ids=['parent'],
            blockers=[dict(code='release_target_unverified', message='发布目标不是本轮已验证的完整源码')])
        result = delivery_contract.verified_result(contract, self.task(), payload, snapshot,
            dict(outcome='waiting', release=blocked))
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso,
            stage_contracts=dict(sync=contract), stage_results=dict(sync=result))
        self.conn.execute('PRAGMA query_only=ON')
        try:
            release = release_delivery.projection(self.conn, self.task())
            self.assertEqual(release['state'], 'blocked')
            self.assertEqual(release['reason'], '上次发布核对：'+blocked['reason'])
            self.assertEqual(release['blockers'], blocked['blockers'])
            self.assertFalse(release['can_start'])
            self.assertIsNotNone(release_delivery.completion_gate(self.conn, self.task()))
        finally:
            self.conn.execute('PRAGMA query_only=OFF')
        from test_release_delivery import FakeReleaseAPI
        api = FakeReleaseAPI()
        self.fixture.tip = snapshot['head_sha']
        pr = dict(number=8, state='MERGED', url=snapshot['pr_url'], headRefOid=snapshot['head_sha'],
            baseRefName=snapshot['base_branch'], headRefName=snapshot['branch'], isCrossRepository=False)
        with patch('kanpilot.parent_closeout_github.pr', return_value=pr), patch.object(release_delivery,'github',api):
            prepared = release_delivery.cmd_prepare_task_release(self.ctx, dict(task_id='parent'))
        self.assertIsNotNone(prepared['context_sha256'], prepared)
        self.assertEqual(prepared['release']['state'], 'ready')
        self.assertEqual(prepared['release']['blockers'], [])
        self.assertTrue(prepared['release']['can_start'])
        self.assertEqual(api.sent, [])
        for key, value in (('run_id', 'new-run'), ('review_key', 'new-review')):
            with self.subTest(key=key):
                original = self.task()[key]
                db.update_task(self.conn, 'parent', self.ctx.now_iso, **{key:value})
                self.assertEqual(release_delivery.projection(self.conn, self.task())['state'], 'ready')
                db.update_task(self.conn, 'parent', self.ctx.now_iso, **{key:original})
        core.update_delivery(self.conn, self.task(), self.ctx.now_iso, head_sha='d'*40)
        self.assertEqual(release_delivery.projection(self.conn, self.task())['state'], 'ready')

    def test_narrow_legacy_delivery_receipt_claim_and_success_report_continue_observation(self):
        result = self.resolve()
        request = db.get_request(self.conn, result['request']['request_id'])
        self.assertTrue(engine_claim_scope.manual_review_repair(self.conn, request, self.task()))
        self.assertTrue(engine_claim_scope.action_delivery_allowed(self.conn, self.task(), request))
        health.acquire_or_renew_lease(self.conn, util.now_iso(), instance_id='manual-delivery-engine',
            stage=None, pid=os.getpid(), lease_seconds=1200)
        with patch.dict(os.environ, {engine_claim_scope.WORKER_ENV:'1', engine_claim_scope.MODE_ENV:'legacy',
                engine_claim_scope.FORCE_ENV:'0', process_owner.INSTANCE_ENV:'manual-delivery-engine'}):
            for mode in ('legacy', 'shadow'):
                self.assertEqual(loop.Reconciler(self.fixture.db_path, mode=mode)._queued_stages(self.conn, {}), {'delivery':1})
                self.assertTrue(engine_claim_scope.allows(self.conn, request, util.now_dt()))
            claim = cli.run('claim', dict(stage='delivery', executor='kaneo-delivery', request_id=request['request_id']), self.fixture.db_path)
            self.assertTrue(claim['claimed'])
            claimed = db.get_request(self.conn,request['request_id'])
            self.assertTrue(engine_claim_scope.action_delivery_allowed(self.conn,self.task(),claimed))
            cmd_delivery_pr._identity(self.ctx,dict(task_id='parent',request_id=claimed['request_id'],
                run_id=claim['run_id'],expected_execution_run_id='run-parent'))
            cli.run('report', dict(request_id=request['request_id'], run_id=claim['run_id'], outcome='succeeded'), self.fixture.db_path)
        queued = self.conn.execute("SELECT * FROM requests WHERE kind='delivery' AND status='queued'").fetchall()
        self.assertEqual(len(queued), 1)
        self.assertTrue(engine_claim_scope.action_delivery_allowed(self.conn, self.task(), queued[0]))
        forged = dict(queued[0], payload_json=util.jdump({'expected_execution_run_id':'run-parent'}))
        self.assertFalse(engine_claim_scope.action_delivery_allowed(self.conn, self.task(), forged))

    def test_only_explicit_release_required_misblock_can_restore(self):
        self.block_old_pass()
        for mutate in ('other_block','unknown','paused','archived','archived_run','active','stale_run','stale_key','automation'):
            with self.subTest(mutate=mutate):
                before = dict(self.task()); extra = db.task_extra(self.task())
                if mutate == 'other_block': db.update_task(self.conn,'parent',self.ctx.now_iso,failure_reason='another failure')
                elif mutate == 'unknown': db.update_task(self.conn,'parent',self.ctx.now_iso,send_state='unknown')
                elif mutate == 'paused': db.update_task(self.conn,'parent',self.ctx.now_iso,auto_advance_paused=1)
                elif mutate == 'archived': db.update_task(self.conn,'parent',self.ctx.now_iso,save_state='archived')
                elif mutate == 'archived_run': self.conn.execute("UPDATE execution_rounds SET save_state='archived' WHERE task_id='parent'")
                elif mutate == 'active': req,_=db.create_request(self.conn,self.ctx.now_iso,'review','active',task_id='parent')
                elif mutate == 'automation': db.set_extra(self.conn,self.task(),self.ctx.now_iso,automation=dict(fix_round=0))
                override = {'expected_execution_run_id':'stale'} if mutate=='stale_run' else {'expected_review_key':'stale'} if mutate=='stale_key' else {}
                with self.assertRaises(util.KanpilotError): self.resolve(**override)
                if mutate == 'active': self.conn.execute('DELETE FROM requests WHERE request_id=?',(req['request_id'],))
                if mutate == 'archived_run': self.conn.execute("UPDATE execution_rounds SET save_state='active' WHERE task_id='parent'")
                for key in ('failure_reason','send_state','auto_advance_paused','save_state','extra_json'):
                    db.update_task(self.conn,'parent',self.ctx.now_iso,**{key:before[key]})
        self.assertEqual(core.delivery_of(self.task())['mode'], 'manual')

    def test_evidence_or_remote_drift_does_not_enqueue(self):
        for mutate in ('report','source','history','registration','remote','dirty'):
            with self.subTest(mutate=mutate):
                task=self.task(); original=db.task_extra(task)
                if mutate in ('report','source'):
                    path=Path(task['review_report_path'] if mutate=='report' else task['plan_path']); raw=path.read_bytes();path.write_text('changed')
                elif mutate=='history':
                    history=deepcopy(original['review_history']);history[-1]['head_sha']='bad';db.set_extra(self.conn,task,self.ctx.now_iso,review_history=history)
                elif mutate=='registration': db.set_extra(self.conn,task,self.ctx.now_iso,registered_review_identity={})
                elif mutate=='remote': self.fixture.fake.tasks['parent']['status']='in-progress'
                else:
                    path=Path(core.delivery_of(task)['worktree_path'])/'unreviewed.py';path.write_text('new work')
                with self.assertRaises(util.KanpilotError): self.resolve()
                if mutate in ('report','source'): path.write_bytes(raw)
                elif mutate in ('history','registration'): db.update_task(self.conn,'parent',self.ctx.now_iso,extra_json=original)
                elif mutate=='remote': self.fixture.fake.tasks['parent']['status']='in-review'
                else: path.unlink()
        self.assertFalse(self.conn.execute("SELECT 1 FROM requests WHERE kind='delivery'").fetchone())

    def test_manual_missing_fix_round_uses_manual_round_but_automation_remains_strict(self):
        self.resolve()
        request = self.conn.execute("SELECT * FROM requests WHERE kind='delivery'").fetchone()
        db.update_request(self.conn,request['request_id'],self.ctx.now_iso,status='claimed',run_id='delivery-run',
            claimed_by='kaneo-delivery',lease_expires_at=util.plus_seconds(1200,self.ctx.now))
        payload=dict(task_id='parent',request_id=request['request_id'],run_id='delivery-run',expected_execution_run_id='run-parent')
        cmd_delivery_pr._identity(self.ctx,payload)
        db.set_extra(self.conn,self.task(),self.ctx.now_iso,automation=dict(fix_round=0))
        with self.assertRaises(util.KanpilotError): cmd_delivery_pr._identity(self.ctx,payload)

    def test_release_disabled_retains_old_manual_behavior(self):
        self.policy['enabled']=False; self.configure()
        self.assertNotIn('continue_delivery',derive.allowed_actions(self.conn,self.task(),self.ctx.now))
        with self.assertRaises(util.KanpilotError): self.resolve()

    def test_generated_goal_and_build_artifacts_are_preserved_and_excluded(self):
        path = Path(core.delivery_of(self.task())['worktree_path'])
        goal = path / '.pi' / 'goal' / 'current-goal.md'
        goal.parent.mkdir(parents=True); goal.write_text('# Goal\nOriginal evidence\n')
        artifact = path / 'build' / 'app.apk'
        artifact.parent.mkdir(); artifact.write_bytes(b'isolated build artifact')
        result = self.resolve()
        self.assertEqual(result['task_status'], 'in_review')
        self.assertEqual(goal.read_text(), '# Goal\nOriginal evidence\n')
        self.assertEqual(artifact.read_bytes(), b'isolated build artifact')

    def fail_delivery(self, *, environment):
        result = self.resolve()
        claim = cli.run('claim', dict(stage='delivery', executor='kaneo-delivery',
            request_id=result['request']['request_id']), self.fixture.db_path)
        payload = dict(task_id='parent',request_id=claim['request_id'],run_id=claim['run_id'],
                       expected_execution_run_id='run-parent')
        if environment:
            task = self.task()
            cmd_delivery_pr._failure(self.ctx,payload,task,core.delivery_of(task),gh_client.GhError('missing','fixture missing gh'))
        cli.run('report',dict(request_id=claim['request_id'],run_id=claim['run_id'],outcome='failed',note='fixture failure'),self.fixture.db_path)
        return payload

    def test_failed_delivery_explicitly_revalidates_and_resumes_the_same_pass(self):
        self.fail_delivery(environment=True)
        before = db.task_extra(self.task())
        self.assertIn('continue_delivery',derive.allowed_actions(self.conn,self.task(),self.ctx.now))
        report = Path(self.task()['review_report_path']); raw = report.read_bytes()
        report.write_text('changed report')
        with self.assertRaises(util.KanpilotError): self.resolve()
        report.write_bytes(raw)
        result = self.resolve()
        self.assertNotIn('deduplicated',result)
        self.assertEqual(result['task_status'],'in_review')
        self.assertIsNone(self.task()['failure_code'])
        after = db.task_extra(self.task())
        for key in ('manual_review_delivery','review_history','registered_review_identity','engine_verdict'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(self.task()['run_id'],'run-parent')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='review'").fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='delivery' AND status='queued'").fetchone()[0],1)
        self.assertTrue(self.resolve()['deduplicated'])

    def test_failed_runner_without_task_block_can_requeue_but_unrelated_block_cannot(self):
        self.fail_delivery(environment=False)
        self.assertEqual(self.task()['status'],'in_review')
        result = self.resolve()
        self.assertEqual(result['request']['kind'],'delivery')
        self.assertFalse(result.get('deduplicated',False))
        self.conn.execute("UPDATE requests SET status='failed',finished_at=? WHERE kind='delivery' AND status='queued'",(util.now_iso(),))
        core.block_task(self.conn,self.task(),util.now_iso(),'another review failure','review_blocked','user',stage='review')
        self.assertNotIn('continue_delivery',derive.allowed_actions(self.conn,self.task(),util.now_dt()))
        with self.assertRaises(util.KanpilotError): self.resolve()

    def test_lost_delivery_requeues_only_with_original_frozen_authority(self):
        result = self.resolve()
        claim = cli.run('claim',dict(stage='delivery',executor='kaneo-delivery',request_id=result['request']['request_id']),self.fixture.db_path)
        db.update_request(self.conn,claim['request_id'],util.now_iso(),lease_expires_at='2000-01-01T00:00:00Z')
        result = cli.run('resolve',dict(task_id='parent',action='release_lost'),self.fixture.db_path)
        queued = self.conn.execute("SELECT * FROM requests WHERE kind='delivery' AND status='queued'").fetchone()
        self.assertIsNotNone(queued)
        self.assertTrue(engine_claim_scope.action_delivery_allowed(self.conn,self.task(),queued))
        self.assertTrue(self.resolve()['deduplicated'])
        with self.assertRaises(util.KanpilotError):
            cli.run('enqueue',dict(task_id='parent',kind='delivery'),self.fixture.db_path)

    def test_current_manual_request_rejects_replaced_receipt(self):
        result = self.resolve()
        claimed = cli.run('claim',dict(stage='delivery',executor='kaneo-delivery',
            request_id=result['request']['request_id']),self.fixture.db_path)
        db.set_extra(self.conn,self.task(),util.now_iso(),manual_review_delivery=dict(id='old-replaced-receipt'))
        with self.assertRaises(util.KanpilotError):
            cmd_delivery_pr._identity(self.ctx,dict(task_id='parent',request_id=claimed['request_id'],
                run_id=claimed['run_id'],expected_execution_run_id='run-parent'))


class CodexReviewDeliveryTest(TestCase):
    """Compose a real Codex verdict/commit with the manual Review pass gate."""
    def setUp(self):
        self.codex = codex_fixtures.CodexVerdictIntegration()
        self.codex.setUp()
        self.addCleanup(self.codex.doCleanups)
        self.task_id, sha = self.codex.prepare('manual-delivery')
        self.fixture = self.codex.case
        self.conn = db.connect(self.fixture.db_path)
        self.addCleanup(self.conn.close)
        self.now = util.now_iso()
        task = self.task()
        branch = 'kaneo/manual-delivery'
        git(self.fixture.worktree, 'switch', '-c', branch)
        base = git(self.fixture.worktree, 'rev-parse', 'HEAD')
        delivery = dict(mode='manual', workspace_version=1, branch=branch,
            worktree_path=str(self.fixture.worktree), base_branch='main', base_sha=base, head_sha=base)
        db.set_extra(self.conn, task, self.now, delivery=delivery)
        receipt = self.fixture.verdict(self.task_id, sha)
        self.assertTrue(receipt['verification_ok'], receipt)
        git(self.fixture.worktree, 'add', '.')
        git(self.fixture.worktree, 'commit', '-m', 'verified source\n\nKaneoPilot-Run: ' + task['run_id'])
        head = git(self.fixture.worktree, 'rev-parse', 'HEAD')
        delivery.update(head_sha=head, commits=[dict(execution_run_id=task['run_id'], sha=head)])
        report = self.fixture.reports_root / 'codex-manual-pass.md'
        report.write_text('Review passed for the registered Codex commit')
        report_sha = util.sha256_file(report)
        review_key = 'codex-manual-pass'
        review = dict(conclusion='pass', execution_run_id=task['run_id'], head_sha=head,
            review_key=review_key, report_path=str(report), report_sha256=report_sha,
            **{key: delivery[key] for key in ('branch','base_branch','base_sha','worktree_path')})
        request, _ = db.create_request(self.conn, self.now, 'review', review_key, task_id=self.task_id)
        db.update_request(self.conn, request['request_id'], self.now, status='succeeded',
            run_id='codex-review-run', finished_at=self.now, result_json=dict(outcome='succeeded'))
        identity = dict(request_id=request['request_id'], run_id='codex-review-run',
            execution_run_id=task['run_id'], report_sha256=report_sha, plan_sha256=sha)
        db.update_task(self.conn, self.task_id, self.now, status='in_review', stage='review',
            review_key=review_key, review_report_path=str(report), review_report_sha256=report_sha)
        self.conn.execute("UPDATE execution_rounds SET run_status='finished' WHERE task_id=?", (self.task_id,))
        db.set_extra(self.conn, self.task(), self.now, delivery=delivery, review_conclusion='pass',
            review_registered_at=self.now, review_history=[review], registered_review_identity=identity)
        db.set_setting(self.conn, project_config.SETTINGS_KEY, dict(version=1, revision=1, items={
            task['project_id']: dict(enabled=True, repository=task['repository'], engine_mode='legacy',
                automation=dict(enabled=False, policy_revision=1,
                    release=dict(enabled=True, required_assets=['app.apk'])))}))
        self.fixture.fake.tasks[self.task_id]['status'] = 'in-review'
        self.payload = dict(task_id=self.task_id, action='continue_delivery',
            expected_execution_run_id=task['run_id'], expected_review_key=review_key)
        self.assertTrue(verdict.valid_committed_receipt(self.conn, self.task(), receipt))

    def task(self):
        return db.require_task(self.conn, self.task_id)

    def resolve(self):
        return cli.run('resolve', self.payload, self.fixture.db_path)

    def test_codex_pass_continues_without_plus_or_local_verify_and_is_idempotent(self):
        before = db.task_extra(self.task())
        with patch('kanpilot.verification_recovery.read_goal_evidence', side_effect=AssertionError('Plus evidence read')), \
             patch.object(verdict, 'run_verify_commands', side_effect=AssertionError('local verify')):
            result = self.resolve()
            self.assertTrue(self.resolve()['deduplicated'])
        self.assertEqual(result['request']['kind'], 'delivery')
        self.assertEqual(core.delivery_of(self.task())['mode'], 'pr')
        for key in ('engine_verdict', 'plan_verify', 'review_history', 'registered_review_identity'):
            self.assertEqual(db.task_extra(self.task())[key], before[key])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requests WHERE kind='delivery'").fetchone()[0], 1)
        self.assertFalse(any(method != 'GET' for method, _, _ in self.fixture.fake.requests_log))

    def test_codex_changed_evidence_source_or_binding_rejects_without_enqueue(self):
        task = self.task()
        files = [self.fixture.directory / 'codex-report.json',
                 self.fixture.directory / 'codex-turn-1.jsonl',
                 Path(task['source_path']), Path(task['plan_path']),
                 Path(task['review_report_path']), self.fixture.worktree / ('change-' + self.task_id + '.py')]
        for path in files:
            with self.subTest(file=path.name):
                raw = path.read_bytes()
                try:
                    path.write_bytes(raw + b'\nchanged')
                    with self.assertRaises(util.KanpilotError):
                        self.resolve()
                    self.assertFalse(self.conn.execute("SELECT 1 FROM requests WHERE kind='delivery'").fetchone())
                    self.assertEqual(core.delivery_of(self.task())['mode'], 'manual')
                finally:
                    path.write_bytes(raw)
        original = db.task_extra(self.task())
        for key, value in (('evidence_source', 'goal_report'), ('execution_run_id', 'old-run'),
                           ('plan_sha256', 'old-plan'), ('index_source_sha256', 'old-tree')):
            with self.subTest(receipt=key):
                receipt = deepcopy(original['engine_verdict']); receipt[key] = value
                db.set_extra(self.conn, self.task(), self.now, engine_verdict=receipt)
                with self.assertRaises(util.KanpilotError):
                    self.resolve()
                self.assertFalse(self.conn.execute("SELECT 1 FROM requests WHERE kind='delivery'").fetchone())
                db.set_extra(self.conn, self.task(), self.now, engine_verdict=original['engine_verdict'])


class HistoricalManualReceiptTest(TestCase):
    def test_old_manual_receipt_does_not_block_new_authorized_ai_action_delivery(self):
        import test_task_ai_action_scheduler as action_fixtures
        fixture = action_fixtures.TaskAIActionScheduler('test_legacy_review_settles_to_delivery_and_engine_pushes_existing_pr')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        old = dict(id='historical-manual-receipt',authorized_by='user',authorized_at=fixture.now,
                   task=dict(run_id='older-execution'))
        db.set_extra(fixture.conn,fixture.task(),fixture.now,manual_review_delivery=old)
        fixture.pass_review(settle=True)
        with patch.dict(os.environ,fixture.engine_env()):
            claimed = fixture.call('claim',dict(stage='delivery',executor='kaneo-delivery'))
        self.assertTrue(claimed['claimed'])
        request = db.get_request(fixture.conn,claimed['request_id'])
        self.assertTrue(engine_claim_scope.action_delivery_allowed(fixture.conn,fixture.task(),request))
        self.assertNotIn('manual_review_delivery_id',db.request_payload(request))
        fixture.publish_original_pr(claimed)
        self.assertEqual(db.task_extra(fixture.task())['manual_review_delivery'],old)
