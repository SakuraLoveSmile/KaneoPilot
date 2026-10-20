"""Explicit parent closeout; all services and source repositories are isolated."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import KanpilotTestCase, goal_checks, goal_report_read
from kanpilot import (db, util, round_store, git_delivery, harness_plus_adapter,
                      cmd_read, verification_recovery, parent_closeout)
from test_git_delivery import git


class ParentCloseoutTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.now = util.now_iso()
        self.ctx = SimpleNamespace(conn=self.conn, now_iso=self.now, now=util.now_dt())
        self.repo = self.make_repo('Closeout')
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/example/closeout.git')
        (self.repo / 'test').mkdir()
        for n in ('a', 'b'):
            (self.repo / 'test' / (n + '_test.dart')).write_text('// isolated\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'parent feature')
        self.parent_commands = ['flutter analyze', 'flutter test test/a_test.dart']
        self.commands = ['flutter analyze', 'flutter test test/b_test.dart test/a_test.dart']
        self.goals = {}
        self.add_task('parent', commands=self.parent_commands)
        for name in ('fix1', 'fix2'):
            (self.repo / (name + '.txt')).write_text(name)
            git(self.repo, 'add', '.')
            git(self.repo, 'commit', '-m', name)
            self.add_task(name, commands=self.commands, parent='parent')
        parent = self.row('parent')
        entries = []
        for n in (1, 2):
            path = self.reports_root / ('source-review-' + str(n) + '.md')
            path.write_text('blocking finding ' + str(n))
            entries.append(dict(review_key='source-key-' + str(n), execution_run_id='run-parent',
                conclusion='changes_requested', fix_task_id='fix' + str(n),
                report_path=str(path), report_sha256=util.sha256_file(path),
                head_sha=self.delivery('parent')['head_sha']))
        db.update_task(self.conn, 'parent', self.now, fix_task_id='fix2', review_key='source-key-2',
                       review_report_path=entries[-1]['report_path'], review_report_sha256=entries[-1]['report_sha256'])
        db.set_extra(self.conn, self.row('parent'), self.now,
                     review_history=entries, review_conclusion='changes_requested')
        self.tip = git(self.repo, 'rev-parse', 'HEAD')
        self.github_patches = [
            patch('kanpilot.parent_closeout_github.pr', side_effect=self.pr),
            patch('kanpilot.parent_closeout_github.commit', side_effect=self.commit),
            patch('kanpilot.parent_closeout_github.contains', side_effect=self.contains),
            patch.object(harness_plus_adapter, 'plus_status', side_effect=lambda c, **kw:
                dict(session_id=kw['session_id'], current_turn_id='turn', is_running=False)),
            patch.object(harness_plus_adapter, 'plus_turn_get', side_effect=lambda c, **kw:
                dict(session_id=kw['session_id'], turn_id='turn', status='completed')),
            patch.object(cmd_read, 'read_goal_report', side_effect=lambda c,t,r,**kw: self.goals[t])]
        for p in self.github_patches:
            p.start(); self.addCleanup(p.stop)

    def row(self, name):
        return db.require_task(self.conn, name)

    def delivery(self, name):
        return db.task_extra(self.row(name))['delivery']

    def add_task(self, name, commands, parent=None):
        remote = self.fake.add_task(name, 'project', status='in-review' if not parent else 'done')
        source_sha, req_sha, source_path = self.make_source_snapshot(remote)
        plan = self.plan_root / (name + '.md')
        plan.write_text('---\n' + '\n'.join(k + ': ' + str(v) for k,v in dict(
            task_id=name, project_id='project', repository=str(self.repo), requirements_sha256=req_sha).items())
            + '\n---\n\n```verify\n' + '\n'.join(commands) + '\n```\n\n真机体验：NOT RUN\n')
        sha = util.sha256_file(plan)
        head = git(self.repo, 'rev-parse', 'HEAD')
        report = self.reports_root / (name + '-pass.md'); report.write_text('pass '+name)
        review = dict(conclusion='pass', execution_run_id='run-' + name, head_sha=head,
                      review_key='pass-' + name, report_path=str(report), report_sha256=util.sha256_file(report))
        config = {'connection_file': str(self.root / 'fake-plus.json')}
        worktree = str(self.root / ('removed-worktree-' + name))
        directory = self.runs_root / name / ('run-' + name); directory.mkdir(parents=True)
        job = dict(task_id=name, run_id='run-' + name, backend='pi_desktop', config=config,
            plan_sha256=sha, completion_policy='command_verification', session_id='session-'+name,
            turn_id='turn', send_state='sent', status='finished', exit_code=0,
            verification_evidence_version=1, verification_source_sha256='source-'+name)
        spec = dict(job, plan_path=str(plan), repository=str(self.repo), cwd=worktree, runs_dir=str(directory))
        for f,v in [('job.json',job),('spec.json',spec)]: (directory/f).write_text(util.jdump(v))
        (directory/'events.jsonl').write_text(util.jdump(dict(seq=1,type='session',session_id='session-'+name))+'\n'+
            util.jdump(dict(seq=2,type='terminal',outcome='completed'))+'\n')
        goal = goal_report_read(goal_checks(commands), turn_id='turn'); self.goals[name]=goal
        binding = dict(task_id=name, execution_run_id='run-'+name, plan_sha256=sha,
            commands_sha256=util.sha256_text(util.jdump(commands)), worktree_path=worktree,
            head_sha=head, branch='kaneo/'+name, source_sha256='source-'+name)
        receipt = dict(evidence_source='goal_report', verification_ok=True, execution_run_id='run-'+name,
            plan_sha256=sha, report_sha256=goal['report_sha256'], execution_turn_id='turn',
            binding=binding, index_source_sha256=git_delivery.tree_source_sha256(self.repo,head),
            evidence_sha256={str(p):util.sha256_file(p) for p in [plan,directory/'job.json',directory/'spec.json',directory/'events.jsonl']})
        delivery = dict(mode='pr' if parent else 'manual',head_sha=head,base_sha=head,base_branch='main',
            branch='kaneo/'+name,worktree_path=worktree,
            commits=[dict(execution_run_id='run-'+name,sha=head)],
            pr_number=int(name[-1]) if parent else None,
            pr_url='https://github.com/example/closeout/pull/'+name[-1] if parent else None,
            merged_at='2026-10-19T00:00:00Z' if parent else None)
        vals = dict(task_id=name,project_id='project',project_name='Closeout',title=name,repository=str(self.repo),
            status='done' if parent else 'in_review',stage='done' if parent else 'review',
            kaneo_status=remote['status'],run_id='run-'+name,session_id='session-'+name,sent=1,send_state='sent',
            plan_path=str(plan),plan_sha256=sha,source_path=source_path,source_sha256=source_sha,
            requirements_sha256=req_sha,review_source_task_id=parent,review_key='pass-'+name,
            review_report_path=str(report),review_report_sha256=review['report_sha256'],
            created_at=self.now,updated_at=self.now,
            extra_json=util.jdump(dict(plan_verify=dict(plan_sha256=sha,commands=commands),engine_verdict=receipt,
                                      delivery=delivery,review_conclusion='pass',review_history=[review])))
        self.conn.execute('INSERT INTO tasks ('+','.join(vals)+') VALUES ('+','.join('?' for _ in vals)+')',tuple(vals.values()))
        round_store.upsert_on_claim(self.conn,name,'run-'+name,config,self.now)
        self.conn.execute("UPDATE execution_rounds SET backend='pi_desktop',session_id=?,worktree_path=?,run_status='finished' WHERE task_id=?",('session-'+name,worktree,name))

    def pr(self, repo, number):
        d=self.delivery('fix'+str(number))
        return dict(number=number,url=d['pr_url'],state='MERGED',headRefOid=d['head_sha'],
                    baseRefName='main',headRefName=d['branch'],isCrossRepository=False,mergedAt=d['merged_at'])

    def commit(self, repo, ref):
        sha=self.tip if ref=='main' else ref
        return dict(sha=sha,tree_sha=git(self.repo,'rev-parse',sha+'^{tree}'),
                    source_sha256=git_delivery.tree_source_sha256(self.repo,sha),
                    paths=['test/a_test.dart','test/b_test.dart'])

    def contains(self,repo,base,head):
        import subprocess
        return subprocess.run(['git','-C',str(self.repo),'merge-base','--is-ancestor',base,head],capture_output=True).returncode==0

    def run_closeout(self, expected=None):
        return parent_closeout.cmd_closeout_parent_task(self.ctx,dict(task_id='parent',
            expected_context_sha256=expected or parent_closeout.projection(self.conn,self.row('parent'))['context_sha256']))

    def test_success_reuses_final_checkpoint_without_worktree_or_new_model(self):
        result=self.run_closeout()
        self.assertEqual(result['outcome'],'completed',result)
        self.assertEqual(self.row('parent')['status'],'done')
        self.assertEqual(self.fake.tasks['parent']['status'],'done')
        self.assertEqual(db.task_extra(self.row('parent'))['review_conclusion'],'changes_requested')
        receipt=db.task_extra(self.row('parent'))['parent_closeout']
        self.assertEqual(receipt['evidence_task_id'],'fix2')
        self.assertIn('未核验',receipt['acceptance_note'])
        self.assertEqual(len(receipt['coverage_mapping']),2)
        self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.fake.requests_log),1)

    def test_not_done_and_unmerged_block_without_remote_write(self):
        db.update_task(self.conn,'fix1',self.now,status='in_review')
        self.assertEqual(self.run_closeout()['outcome'],'blocked')
        db.update_task(self.conn,'fix1',self.now,status='done')
        with patch('kanpilot.parent_closeout_github.pr',return_value=dict(self.pr('example/closeout',1),state='OPEN')):
            self.assertEqual(self.run_closeout()['outcome'],'blocked')
        self.assertFalse(any(m=='PUT' for m,_,_ in self.fake.requests_log))

    def test_unbound_direct_child_and_stale_context_fail_closed(self):
        expected=parent_closeout.projection(self.conn,self.row('parent'))['context_sha256']
        db.update_task(self.conn,'fix1',self.now,requirements_sha256='changed')
        self.assertEqual(self.run_closeout(expected)['outcome'],'blocked')
        self.add_task('fix3',commands=self.commands,parent='parent')
        self.assertEqual(self.run_closeout()['outcome'],'blocked')

    def test_report_plan_source_and_old_review_drift_block(self):
        for mutate in ('report','plan','source','review'):
            with self.subTest(mutate=mutate):
                task=self.row('fix2'); extra=db.task_extra(task); old=extra['engine_verdict'].copy()
                if mutate=='report':
                    p=Path(task['review_report_path']); raw=p.read_bytes();p.write_text('tampered')
                elif mutate=='plan':
                    p=Path(task['plan_path']);raw=p.read_bytes();p.write_text('changed')
                elif mutate=='source':
                    p=Path(task['source_path']);raw=p.read_bytes();p.write_text('{}')
                else:
                    db.update_task(self.conn,'fix2',self.now,run_id='new-run')
                self.assertEqual(self.run_closeout()['outcome'],'blocked')
                if mutate=='review':db.update_task(self.conn,'fix2',self.now,run_id='run-fix2')
                else:p.write_bytes(raw)

    def test_no_single_integrated_checkpoint_and_missing_parent_command(self):
        with patch('kanpilot.parent_closeout_github.contains',return_value=False):
            self.assertEqual(self.run_closeout()['outcome'],'blocked')
        goal=self.goals['fix2']; original=goal['report']['checks']
        goal['report']['checks']=goal_checks(['flutter analyze','flutter test test/b_test.dart'])
        self.assertEqual(self.run_closeout()['outcome'],'blocked')
        goal['report']['checks']=original

    def test_unknown_never_resends_even_when_context_changes(self):
        from kanpilot import remote, bridge
        with patch.object(remote,'update_status',side_effect=bridge.BridgeError('lost response')):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        db.update_task(self.conn,'fix1',self.now,title='changed identity')
        self.assertEqual(self.run_closeout()['outcome'],'unknown')
        self.assertEqual(self.fake.tasks['parent']['status'],'in-review')

    def test_context_excludes_own_result_and_updated_at(self):
        initial=parent_closeout.projection(self.conn,self.row('parent'))['context_sha256']
        db.set_extra(self.conn,self.row('parent'),self.now,parent_closeout=dict(state='blocked'))
        db.update_task(self.conn,'parent','2099-01-01T00:00:00Z')
        self.assertEqual(parent_closeout.projection(self.conn,self.row('parent'))['context_sha256'],initial)

    def test_unknown_after_applied_write_reconciles_without_a_second_send(self):
        from kanpilot import remote, bridge
        original=remote.update_status
        def lose_response(*args,**kwargs):
            original(*args,**kwargs)
            raise bridge.BridgeError('reply lost after application')
        with patch.object(remote,'update_status',side_effect=lose_response):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        db.update_task(self.conn,'parent',self.now,kaneo_status='done',kaneo_checked_at='2099-01-01T00:00:00Z')
        self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.fake.requests_log),1)

    def test_remote_already_done_is_fully_verified_and_read_only(self):
        self.fake.tasks['parent']['status']='done'
        self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertFalse(any(method=='PUT' for method,_,_ in self.fake.requests_log))

    def test_projection_is_sql_only_and_old_result_does_not_capture_new_run(self):
        before=list(self.conn.iterdump())
        self.conn.execute('PRAGMA query_only=ON')
        with patch.object(Path,'read_bytes',side_effect=AssertionError('projection read files')), \
                patch('kanpilot.parent_closeout_github.commit',side_effect=AssertionError('projection called network')):
            self.assertEqual(parent_closeout.projection(self.conn,self.row('parent'))['state'],'ready')
        self.assertEqual(before,list(self.conn.iterdump()))
        self.conn.execute('PRAGMA query_only=OFF')
        db.set_extra(self.conn,self.row('parent'),self.now,parent_closeout=dict(state='blocked'))
        db.update_task(self.conn,'parent',self.now,run_id='new-run')
        self.assertIsNone(parent_closeout.projection(self.conn,self.row('parent')))

    def test_old_completed_result_does_not_capture_replanning(self):
        self.assertEqual(self.run_closeout()['outcome'],'completed')
        db.update_task(self.conn,'parent',self.now,status='needs_plan',stage='plan',run_id='new-run')
        self.assertIsNone(parent_closeout.projection(self.conn,self.row('parent')))

    def test_concurrent_mutation_before_reservation_prevents_send(self):
        original=parent_closeout._collect_evidence
        count=[0]
        def collect(*args,**kwargs):
            proof=original(*args,**kwargs); count[0]+=1
            if count[0]==2:
                db.update_task(self.conn,'fix2',self.now,run_id='new-run')
            return proof
        with patch.object(parent_closeout,'_collect_evidence',side_effect=collect):
            self.assertEqual(self.run_closeout()['outcome'],'blocked')
        self.assertFalse(any(method=='PUT' for method,_,_ in self.fake.requests_log))
        self.assertEqual(self.row('parent')['status'],'in_review')

    def test_reservation_precedes_send_and_nested_click_cannot_send_again(self):
        from kanpilot import remote
        original=remote.update_status
        def inspect(*args,**kwargs):
            receipt=self.conn.execute("SELECT * FROM runtime_operations WHERE scope_id='parent-closeout:parent'").fetchone()
            self.assertEqual(receipt['status'],'unknown')
            self.assertTrue(json.loads(receipt['result_json'])['proof']['target_sha'])
            with patch.object(remote,'update_status',side_effect=AssertionError('duplicate write')):
                self.assertEqual(self.run_closeout()['outcome'],'unknown')
            return original(*args,**kwargs)
        with patch.object(remote,'update_status',side_effect=inspect):
            self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.fake.requests_log),1)

    def test_true_integer_exit_and_structured_criteria_cannot_be_overridden(self):
        for field,value in [('criteria',[dict(verdict='unmet')]),('steps',[dict(status='skipped')])]:
            with self.subTest(field=field):
                self.goals['fix2']['report'][field]=value
                result=self.run_closeout()
                self.assertEqual(result['outcome'],'blocked')
                self.assertEqual(result['parent_closeout']['blockers'][0]['code'],'report_incomplete')
                self.goals['fix2']['report'].pop(field)
        self.goals['fix2']['report']['checks'][0]['exitCode']=False
        self.assertEqual(self.run_closeout()['outcome'],'blocked')

    def test_cycle_cross_scope_and_removed_child_fail_closed(self):
        for fields in [dict(review_source_task_id='fix1'),dict(project_id='wrong'),dict(save_state='purged')]:
            with self.subTest(fields=fields):
                previous={k:self.row('fix1')[k] for k in fields}
                db.update_task(self.conn,'fix1',self.now,**fields)
                self.assertEqual(self.run_closeout()['outcome'],'blocked')
                db.update_task(self.conn,'fix1',self.now,**previous)

    def test_historical_path_prefixed_report_requires_final_exact_all_task_coverage(self):
        commands=['PATH="/isolated/flutter/bin:$PATH" '+command for command in self.commands]
        self.goals['fix1']=goal_report_read(goal_checks(commands),turn_id='turn')
        extra=db.task_extra(self.row('fix1'));receipt=extra['engine_verdict']
        receipt['report_sha256']=self.goals['fix1']['report_sha256']
        db.set_extra(self.conn,self.row('fix1'),self.now,engine_verdict=receipt)
        result=self.run_closeout()
        self.assertEqual(result['outcome'],'completed',result)
        saved=db.task_extra(self.row('parent'))['parent_closeout']
        self.assertFalse(next(row for row in saved['reused_evidence'] if row['task_id']=='fix1')['exact_coverage'])
        self.assertEqual(set(saved['all_task_coverage']),{'parent','fix1','fix2'})

    def test_unknown_dead_writer_recovers_only_its_own_retained_lock(self):
        from kanpilot import remote,bridge,core,process_owner
        original=remote.update_status
        def lose_response(*args,**kwargs):
            original(*args,**kwargs);raise bridge.BridgeError('applied, process crashed')
        # Retain the exact original lifecycle lock to simulate an interrupted
        # process whose finally block never ran.
        with patch.object(remote,'update_status',side_effect=lose_response), \
                patch.object(core,'release_lifecycle'):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        held=db.get_setting(self.conn,'lifecycle_owner')
        self.assertTrue(held)
        # The original kernel birth identity is still alive: do not unlock.
        self.assertEqual(self.run_closeout()['outcome'],'unknown')
        self.assertEqual(db.get_setting(self.conn,'lifecycle_owner'),held)
        birth=process_owner.process_birth(__import__('os').getpid())
        count=[0]
        def recovering_birth(pid):
            count[0]+=1
            return None if count[0]==1 else birth
        with patch.object(process_owner,'process_birth',side_effect=recovering_birth),patch.object(util,'pid_alive',return_value=False):
            self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertEqual(db.get_setting(self.conn,'lifecycle_owner'),'')
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.fake.requests_log),1)

    def test_unknown_reconciliation_does_not_take_another_operations_lock(self):
        from kanpilot import remote,bridge
        original=remote.update_status
        def lose_response(*args,**kwargs):
            original(*args,**kwargs);raise bridge.BridgeError('lost')
        with patch.object(remote,'update_status',side_effect=lose_response):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        db.set_setting(self.conn,'lifecycle_owner','unrelated-owner')
        self.assertEqual(self.run_closeout()['outcome'],'unknown')
        self.assertEqual(self.row('parent')['status'],'in_review')
        self.assertEqual(db.get_setting(self.conn,'lifecycle_owner'),'unrelated-owner')

    def test_unknown_can_reconcile_frozen_tip_after_normal_branch_progress(self):
        from kanpilot import remote,bridge
        original=remote.update_status
        def lose_response(*args,**kwargs):
            original(*args,**kwargs);raise bridge.BridgeError('lost')
        with patch.object(remote,'update_status',side_effect=lose_response):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        frozen=self.tip
        (self.repo/'later.txt').write_text('later unrelated work')
        git(self.repo,'add','.');git(self.repo,'commit','-m','later work')
        self.tip=git(self.repo,'rev-parse','HEAD')
        self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertEqual(db.task_extra(self.row('parent'))['parent_closeout']['target_sha'],frozen)
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.fake.requests_log),1)

    def test_unknown_rejects_forcepush_which_removed_frozen_tip(self):
        from kanpilot import remote,bridge
        original=remote.update_status
        def lose_response(*args,**kwargs):
            original(*args,**kwargs);raise bridge.BridgeError('lost')
        with patch.object(remote,'update_status',side_effect=lose_response):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        self.tip=self.delivery('parent')['head_sha']
        self.assertEqual(self.run_closeout()['outcome'],'unknown')
        self.assertEqual(self.row('parent')['status'],'in_review')

    def test_remote_children_reopen_or_change_identity_prevents_parent_write(self):
        for fields in [dict(status='in-progress'),dict(projectId='different'),dict(description='new requirements')]:
            with self.subTest(fields=fields):
                old=dict(self.fake.tasks['fix1']);self.fake.tasks['fix1'].update(fields)
                result=self.run_closeout()
                self.assertEqual(result['outcome'],'blocked')
                self.assertEqual(result['parent_closeout']['blockers'][0]['code'],'child_remote_changed')
                self.fake.tasks['fix1']=old
        self.assertFalse(any(method=='PUT' for method,_,_ in self.fake.requests_log))

    def test_reconciliation_owner_is_durable_across_a_second_crash(self):
        import os
        from kanpilot import remote,bridge,core,process_owner
        original=remote.update_status
        def lose_response(*args,**kwargs):
            original(*args,**kwargs);raise bridge.BridgeError('lost')
        with patch.object(remote,'update_status',side_effect=lose_response),patch.object(core,'release_lifecycle'):
            self.assertEqual(self.run_closeout()['outcome'],'unknown')
        first=db.get_setting(self.conn,'lifecycle_owner')
        birth=process_owner.process_birth(os.getpid())
        def dead_then_current():
            count=[0]
            def read(pid):
                count[0]+=1
                return None if count[0]==1 else birth
            return read
        pending=parent_closeout._pending(self.conn,'parent')
        frozen=json.loads(pending['result_json'])
        # Recovery acquires a new lock, then crashes before any remote read or
        # local acceptance. Its owner must already be in the same receipt.
        with patch.object(process_owner,'process_birth',side_effect=dead_then_current()),patch.object(util,'pid_alive',return_value=False):
            owner=parent_closeout._reconcile_owner(self.conn,'parent',pending,frozen)
        self.assertNotEqual(first,owner)
        self.assertEqual(json.loads(parent_closeout._pending(self.conn,'parent')['result_json'])['lifecycle_owner'],owner)
        with patch.object(process_owner,'process_birth',side_effect=dead_then_current()),patch.object(util,'pid_alive',return_value=False):
            self.assertEqual(self.run_closeout()['outcome'],'completed')
        self.assertEqual(db.get_setting(self.conn,'lifecycle_owner'),'')
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.fake.requests_log),1)

    def test_no_combining_independent_checkpoints_even_if_target_contains_both(self):
        # Model a normal merge tip with unchanged bytes, while the individual
        # check point's graph is reported not to contain the earlier fix.
        git(self.repo,'commit','--allow-empty','-m','integration tip')
        self.tip=git(self.repo,'rev-parse','HEAD')
        first=self.delivery('fix1')['head_sha'];last=self.delivery('fix2')['head_sha']
        real_contains=self.contains
        def contains(repo,base,head):
            return False if base==first and head==last else real_contains(repo,base,head)
        with patch('kanpilot.parent_closeout_github.contains',side_effect=contains):
            result=self.run_closeout()
        self.assertEqual(result['outcome'],'blocked')
        self.assertEqual(result['parent_closeout']['blockers'][0]['code'],'integrated_checkpoint_missing')

    def test_final_checkpoint_requires_exact_coverage_not_a_legacy_prefix(self):
        self.goals['fix2']=goal_report_read(goal_checks(['PATH="/isolated/bin:$PATH" '+c for c in self.commands]),turn_id='turn')
        receipt=db.task_extra(self.row('fix2'))['engine_verdict'];receipt['report_sha256']=self.goals['fix2']['report_sha256']
        db.set_extra(self.conn,self.row('fix2'),self.now,engine_verdict=receipt)
        result=self.run_closeout()
        self.assertEqual(result['outcome'],'blocked')
        self.assertEqual(result['parent_closeout']['blockers'][0]['code'],'integrated_checkpoint_missing')

    def test_committed_tree_must_match_saved_verified_index(self):
        receipt=db.task_extra(self.row('fix2'))['engine_verdict'];receipt['index_source_sha256']='bad-tree'
        db.set_extra(self.conn,self.row('fix2'),self.now,engine_verdict=receipt)
        result=self.run_closeout()
        self.assertEqual(result['outcome'],'blocked')
        self.assertEqual(result['parent_closeout']['blockers'][0]['code'],'source_tree_changed')

    def test_new_repair_of_child_is_detected_before_a_request_is_queued(self):
        expected=parent_closeout.projection(self.conn,self.row('parent'))['context_sha256']
        self.add_task('fix3',commands=self.commands,parent='fix2')
        self.assertNotEqual(parent_closeout.projection(self.conn,self.row('parent'))['context_sha256'],expected)
        self.assertEqual(self.run_closeout()['outcome'],'blocked')
        self.assertFalse(any(method=='PUT' for method,_,_ in self.fake.requests_log))

    def test_final_checkpoint_cannot_hide_later_exact_parent_failure_with_superset_pass(self):
        checks=goal_checks(self.commands)+[dict(id='failed-parent-check',
            command=self.parent_commands[-1],exitCode=1,result='failed')]
        self.goals['fix2']=goal_report_read(checks,turn_id='turn')
        receipt=db.task_extra(self.row('fix2'))['engine_verdict']
        receipt['report_sha256']=self.goals['fix2']['report_sha256']
        db.set_extra(self.conn,self.row('fix2'),self.now,engine_verdict=receipt)
        result=self.run_closeout()
        self.assertEqual(result['outcome'],'blocked',result)
        self.assertEqual(result['parent_closeout']['blockers'][0]['code'],'integrated_checkpoint_missing')
        self.assertEqual(self.row('parent')['status'],'in_review')
        self.assertFalse(any(method=='PUT' for method,_,_ in self.fake.requests_log))


class FlutterCoverageTest(KanpilotTestCase):
    def test_last_exact_record_is_authoritative_and_never_falls_back_to_superset(self):
        required='flutter test test/a_test.dart'
        superset='flutter test test/a_test.dart test/b_test.dart'
        paths={'test/a_test.dart','test/b_test.dart'}
        for row in [dict(command=required,exitCode=1,result='failed'),
                    dict(command=required,exitCode=None,result='inconclusive'),
                    dict(command=required,exitCode=False,result='passed'),
                    dict(command=required,exitCode=0,result='failed'),
                    dict(command=required,exitCode=0,result='passed',disposition='not_run')]:
            with self.subTest(row=row):
                self.assertIsNone(parent_closeout.coverage_mapping([required],goal_checks([superset])+[row],paths))
        checks=goal_checks([required,superset])+[dict(command=required,exitCode=1,result='failed')]
        self.assertIsNone(parent_closeout.coverage_mapping([required],checks,paths))

    def test_last_structurally_covering_record_decides_without_exact(self):
        required='flutter test test/a_test.dart'
        superset='flutter test test/a_test.dart test/b_test.dart'
        paths={'test/a_test.dart','test/b_test.dart','test/c_test.dart'}
        for failed in [dict(command=superset,exitCode=1,result='failed'),
                       dict(command='flutter test test/a_test.dart test/c_test.dart',exitCode=None,result='inconclusive')]:
            with self.subTest(failed=failed):
                self.assertIsNone(parent_closeout.coverage_mapping([required],goal_checks([superset])+[failed],paths))
        checks=[dict(command=superset,exitCode=1,result='failed')]+goal_checks([superset])
        self.assertEqual(parent_closeout.coverage_mapping([required],checks,paths)[0]['check_id'],'check-1')

    def test_only_explicit_safe_file_supersets_are_accepted(self):
        paths={'test/a_test.dart','test/b_test.dart'}
        mapping=parent_closeout.coverage_mapping(['flutter test test/a_test.dart'],
            goal_checks(['flutter test test/b_test.dart test/a_test.dart']),paths)
        self.assertEqual(mapping[0]['basis'],'flutter_test_file_superset')
        self.assertIsNone(parent_closeout.coverage_mapping(['flutter test test/a_test.dart'],
            goal_checks(['flutter test test/a_test.dart'])+[dict(command='flutter test test/a_test.dart',exitCode=1,result='failed')],paths))
        self.assertIsNone(parent_closeout.coverage_mapping(['flutter test test/a_test.dart'],
            goal_checks(['flutter test test/b_test.dart test/a_test.dart']),{'test/a_test.dart'}))
        for bad in ['flutter test --coverage test/a_test.dart','flutter test test/a_test.dart; echo ok',
                    'X=1 flutter test test/a_test.dart','flutter test ../test/a_test.dart',
                    'flutter test test/*_test.dart','flutter test /test/a_test.dart']:
            self.assertIsNone(parent_closeout.coverage_mapping(['flutter test test/a_test.dart'],goal_checks([bad]),paths))
