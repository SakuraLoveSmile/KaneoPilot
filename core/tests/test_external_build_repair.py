"""External merged repair: isolated receipts, GitHub and Plus only."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

import test_release_verification as fixtures
from kanpilot import core, db, external_build_repair as repair, release_delivery as release, runtime_operations, util


class ExternalBuildRepairTest(TestCase):
    patch = fixtures.ReleaseVerificationTest.patch
    task = fixtures.ReleaseVerificationTest.task
    prepare = fixtures.ReleaseVerificationTest.prepare
    start = fixtures.ReleaseVerificationTest.start
    sync = fixtures.ReleaseVerificationTest.sync
    complete = fixtures.ReleaseVerificationTest.complete
    partial_report = fixtures.ReleaseVerificationTest.partial_report
    def setUp(self):
        fixtures.ReleaseVerificationTest.setUp(self)
        self.patch(patch.dict('os.environ', {'KANPILOT_RELEASE_WORKTREE_ROOT':str(self.fixture.root / 'verify-worktrees')}))
        self.original_pr = deepcopy(release.closeout.github.pr('example/closeout', 8))
        self.target = self.fixture.tip
        self.batch = dict(batch_id='failed-external', project_id='project', repository='example/closeout',
            root_task_id='parent', state='failed', dispatch_status='sent_or_unknown', target_sha=self.target,
            reason='构建运行未成功；可在Actions手动重跑后同步结果',
            members=[release._identity(self.task())], context_sha256='b'*64, policy={'channel':'prerelease'},
            version='0.1.0', tag='v0.1.0-pilot.1', build_number=1,
            workflow=dict(id=44, ref='main', head_sha=self.target), workflow_run_id=100)
        runtime_operations.begin_operation(self.conn, release._scope('project'), 'batch:failed-external',
            'github', release.BATCH_TOOL, {})
        release._save(self.conn, self.batch, self.ctx.now_iso)
        db.set_extra(self.conn, self.task(), self.ctx.now_iso,
            release_ref=dict(batch_id='failed-external', required=True))
        self.run = dict(id=100, workflow_id=44, event='workflow_dispatch', display_title='pilot-release-failed-external',
            head_branch='main', head_sha=self.target, repository=dict(full_name='example/closeout'), run_attempt=1,
            html_url='https://github.com/example/closeout/actions/runs/100', status='completed', conclusion='failure')
        self.patch(patch.object(self.api, 'run', return_value=self.run))
        self.patch(patch.object(self.api, 'merge_commit_sha', return_value=self.target))
        self.patch(patch('kanpilot.parent_closeout_github.pr', side_effect=lambda repo,n:
            deepcopy(self.original_pr if n==8 else dict(self.original_pr, number=n,
                url='https://github.com/example/closeout/pull/'+str(n)))))

    def candidate(self):
        return repair.cmd_prepare_external_build_repair(self.ctx, dict(task_id='parent', pr_number=9))

    def accept(self):
        c=self.candidate()
        return repair.cmd_accept_external_build_repair(self.ctx, dict(task_id='parent', pr_number=9,
            expected_context_sha256=c['context_sha256'], confirm=True))

    def block_delivery_environment(self,code='gh_unavailable',stage='delivery',**fields):
        core.block_task(self.conn,self.task(),self.ctx.now_iso,'origin不是有效GitHub仓库',code,
            'isolated-old-background',stage=stage,**fields)

    def test_delivery_environment_prepare_readonly_confirm_restores_only_local_block(self):
        self.block_delivery_environment()
        before=dict(self.task());extra=deepcopy(db.task_extra(self.task()))
        changes=self.conn.total_changes
        candidate=self.candidate()
        self.assertEqual(self.conn.total_changes,changes)
        self.assertEqual(dict(self.task()),before)
        with self.assertRaises(util.KanpilotError):
            release._single_proof(self.conn,self.task(),allow_unverified_target=True)
        self.assertEqual(self.plus.created,0)
        self.assertTrue(release.projection(self.conn,self.task())['can_link_external_repair'])
        repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
            expected_context_sha256=candidate['context_sha256'],confirm=True))
        current=self.task()
        self.assertEqual(current['status'],'in_review')
        for key,value in core.clear_block_fields().items():self.assertEqual(current[key],value)
        for key in ('run_id','review_key','review_report_sha256','plan_sha256','requirements_sha256'):
            self.assertEqual(current[key],before[key])
        for key in ('delivery','review_history','engine_verdict','plan_verify'):
            self.assertEqual(db.task_extra(current)[key],extra[key])
        source=repair.accepted(self.conn,current)['source_gate']
        self.assertEqual(source['status'],'needs_user')
        self.assertEqual(source['failure_code'],'gh_unavailable')
        self.assertEqual(source['failure_reason'],before['failure_reason'])
        self.assertEqual(self.prepare()['verification']['state'],'prepared')
        self.assertEqual(self.plus.created,0)

    def test_other_blocks_takeover_or_pause_cannot_use_environment_recovery(self):
        for fields in (dict(failure_code='verification_failed'),dict(failure_stage='review'),
                dict(manual_recovery_required=1),dict(auto_advance_paused=1),dict(manual_takeover=True),dict(delivery_paused=True)):
            with self.subTest(fields=fields):
                self.block_delivery_environment()
                db.set_setting(self.conn,'paused_stages',[])
                db.update_task(self.conn,'parent',self.ctx.now_iso,auto_advance_paused=0)
                extra=db.task_extra(self.task());automation=core.automation_of(self.task())
                automation.pop('manual_takeover',None);extra['automation']=automation
                db.update_task(self.conn,'parent',self.ctx.now_iso,extra_json=extra)
                if fields.get('delivery_paused'):
                    db.set_setting(self.conn,'paused_stages',['delivery'])
                elif fields.get('manual_takeover'):
                    extra=db.task_extra(self.task());extra['automation']=dict(core.automation_of(self.task()),manual_takeover=True)
                    db.update_task(self.conn,'parent',self.ctx.now_iso,extra_json=extra)
                else:db.update_task(self.conn,'parent',self.ctx.now_iso,**fields)
                with self.assertRaises(util.KanpilotError):self.candidate()
                self.assertEqual(self.task()['status'],'needs_user')
                self.assertFalse(release.projection(self.conn,self.task())['can_link_external_repair'])
                self.assertNotIn('external_build_repair_ref',db.task_extra(self.task()))

    def test_environment_failure_drift_before_and_during_confirm_is_rejected(self):
        self.block_delivery_environment()
        prepared=self.candidate()
        db.update_task(self.conn,'parent',self.ctx.now_iso,failure_reason='new local failure')
        with self.assertRaises(util.KanpilotError):
            repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                expected_context_sha256=prepared['context_sha256'],confirm=True))
        original=repair._candidate
        for fields in (dict(failure_reason='failure changed during readonly check'),dict(status='in_review')):
            with self.subTest(fields=fields):
                self.block_delivery_environment();prepared=self.candidate()
                def race(*args):
                    value=original(*args)
                    db.update_task(self.conn,'parent',self.ctx.now_iso,**fields)
                    return value
                with patch.object(repair,'_candidate',side_effect=race):
                    with self.assertRaises(util.KanpilotError):
                        repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                            expected_context_sha256=prepared['context_sha256'],confirm=True))
                for key,value in fields.items():self.assertEqual(self.task()[key],value)
                self.assertNotIn('external_build_repair_ref',db.task_extra(self.task()))

    def test_replayed_environment_accept_does_not_clear_a_later_block(self):
        self.block_delivery_environment();prepared=self.candidate()
        payload=dict(task_id='parent',pr_number=9,expected_context_sha256=prepared['context_sha256'],confirm=True)
        repair.cmd_accept_external_build_repair(self.ctx,payload)
        repair.cmd_accept_external_build_repair(self.ctx,payload)
        self.block_delivery_environment()
        later_environment=dict(self.task())
        repair.cmd_accept_external_build_repair(self.ctx,payload)
        self.assertEqual(dict(self.task()),later_environment)
        self.block_delivery_environment(code='verification_failed',stage='monitor')
        later=dict(self.task())
        with self.assertRaises(util.KanpilotError):repair.cmd_accept_external_build_repair(self.ctx,payload)
        self.assertEqual(dict(self.task()),later)

    def test_prepare_readonly_accept_archives_and_requires_new_goal(self):
        before=self.conn.total_changes
        c=self.candidate()
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(c['candidate']['source_workflow_attempt'],1)
        original=deepcopy(db.task_extra(self.task()))
        self.accept(); self.accept()
        extra=db.task_extra(self.task())
        self.assertNotIn('release_ref',extra)
        for key in ('delivery','engine_verdict','review_history','plan_verify'):
            self.assertEqual(extra[key],original[key])
        self.assertEqual(release._batch(self.conn,'failed-external')['state'],'failed')
        self.assertEqual(release.after_merge(self.ctx,self.task())['outcome'],'blocked')
        self.assertEqual(self.plus.created,0)
        self.assertEqual(release.cmd_prepare_task_release(self.ctx,dict(task_id='parent'))['release']['state'],'blocked')
        self.complete()
        self.assertIn('原需求',self.plus.prompt)
        self.assertIn('冻结计划',self.plus.prompt)
        self.assertEqual(release.cmd_prepare_task_release(self.ctx,dict(task_id='parent'))['release']['state'],'ready')

    def test_run_drift_and_unconfirmed_accept_block(self):
        c=self.candidate(); self.run['run_attempt']=2
        with self.assertRaises(util.KanpilotError):
            repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                expected_context_sha256=c['context_sha256'],confirm=True))
        self.assertIn('release_ref',db.task_extra(self.task()))
        with self.assertRaises(util.KanpilotError):
            repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                expected_context_sha256=c['context_sha256']))

    def test_old_verified_goal_is_archived_and_new_goal_cannot_be_reused(self):
        old_id=self.complete()
        old_ref=deepcopy(db.task_extra(self.task())['release_verification_ref'])
        self.accept()
        extra=db.task_extra(self.task())
        self.assertNotIn('release_verification_ref',extra)
        self.assertEqual(extra['build_failure_sources'][-1]['release_verification_ref'],old_ref)
        new_id=self.complete()
        self.assertNotEqual(new_id,old_id)
        self.assertEqual(self.plus.created,2)
        current_ref=deepcopy(db.task_extra(self.task())['release_verification_ref'])
        self.accept()
        self.assertEqual(db.task_extra(self.task())['release_verification_ref'],current_ref)
        with self.assertRaises(util.KanpilotError):
            release.cmd_sync_release_batch(self.ctx,dict(batch_id='failed-external'))
        self.assertEqual(release.cmd_sync_release_batches(self.ctx,{})['results'],[])
        self.assertEqual(release._batch(self.conn,'failed-external')['state'],'failed')

    def test_old_reference_drift_rejects_and_unknown_verification_is_not_overwritten(self):
        c=self.candidate()
        db.set_extra(self.conn,self.task(),self.ctx.now_iso,
            release_verification_ref=dict(verification_id='unknown-old',context_sha256='a'*64))
        with self.assertRaises(util.KanpilotError):
            repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                expected_context_sha256=c['context_sha256'],confirm=True))
        c=self.candidate()
        with self.assertRaises(util.KanpilotError):
            repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                expected_context_sha256=c['context_sha256'],confirm=True))
        self.assertIn('release_ref',db.task_extra(self.task()))

    def test_files_preservation_is_scoped_to_association_and_goal(self):
        original_contains=self.fixture.contains
        original_head=core.delivery_of(self.task())['head_sha']
        def contains(repo,head,target):
            if head==original_head and target==self.target:return False
            return original_contains(repo,head,target)
        original_commit=self.fixture.commit
        def commit(repo,ref):
            return dict(original_commit(repo,ref),records=[dict(path='feature',mode='100644',blob='c'*40)])
        with patch.object(release.closeout.github,'contains',side_effect=contains), patch.object(
            release.closeout.github,'commit',side_effect=commit), patch.object(repair,'_files',return_value=['feature']):
            with self.assertRaises(util.KanpilotError):
                release._single_proof(self.conn,self.task(),allow_unverified_target=True)
            self.assertEqual(self.candidate()['candidate']['preservation_kind'],'files')
            self.accept()
            self.complete()
            prepared=release.cmd_prepare_task_release(self.ctx,dict(task_id='parent'))
            self.assertEqual(prepared['release']['state'],'ready',prepared)
            context=release._prepare(self.ctx,'parent')
            self.assertEqual(context['proof']['external_build_repair']['preservation_kind'],'files')
            self.assertIn('release_verification',context['proof'])

    def test_unchanged_failed_sync_does_not_expire_confirmation(self):
        from types import SimpleNamespace
        c=self.candidate()
        later=SimpleNamespace(conn=self.conn,now_iso=util.plus_seconds(60,self.ctx.now),now=self.ctx.now)
        release.cmd_sync_release_batch(later,dict(batch_id='failed-external'))
        self.assertNotEqual(release._batch(self.conn,'failed-external')['checked_at'],self.batch['checked_at'])
        self.assertEqual(self.candidate()['context_sha256'],c['context_sha256'])
        repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
            expected_context_sha256=c['context_sha256'],confirm=True))

    def test_failed_batch_diagnostic_drift_expires_confirmation(self):
        for changes in (dict(read_failures=1),
                dict(blockers=[dict(code='release_read_failed',message='new failure')])):
            with self.subTest(changes=changes):
                release._save(self.conn,deepcopy(self.batch),self.ctx.now_iso)
                c=self.candidate()
                batch=release._batch(self.conn,'failed-external')
                batch.update(changes)
                release._save(self.conn,batch,self.ctx.now_iso)
                with self.assertRaises(util.KanpilotError) as error:
                    repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                        expected_context_sha256=c['context_sha256'],confirm=True))
                self.assertEqual(error.exception.code,'external_repair_changed')
                self.assertNotIn('external_build_repair_ref',db.task_extra(self.task()))

    def test_inflight_sync_cannot_write_archived_source_even_on_error(self):
        for error in (False,True):
            with self.subTest(error=error):
                # Reset only the isolated association from the prior subcase.
                if repair.accepted(self.conn,self.task()):
                    extra=db.task_extra(self.task());extra.pop('external_build_repair_ref');extra.pop('build_failure_sources')
                    extra['release_ref']=dict(batch_id='failed-external',required=True)
                    db.update_task(self.conn,'parent',self.ctx.now_iso,extra_json=extra)
                c=self.candidate();before=deepcopy(release._batch(self.conn,'failed-external'))
                original=self.api.run
                entered=False
                def response(*args):
                    nonlocal entered
                    if not entered:
                        entered=True
                        with patch.object(self.api,'run',original):
                            repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                                expected_context_sha256=c['context_sha256'],confirm=True))
                        if error:raise repair.gh_client.GhError('transient','network lost after association')
                    return deepcopy(self.run)
                with patch.object(self.api,'run',side_effect=response):
                    result=release.cmd_sync_release_batch(self.ctx,dict(batch_id='failed-external'))
                self.assertTrue(result['release']['is_historical'])
                self.assertFalse(result['release']['can_reconcile'])
                self.assertEqual(release._batch(self.conn,'failed-external'),before)

    def test_policy_drift_blocks_verification_release_and_projection(self):
        self.accept()
        policy,mapping=release._policy(self.conn,self.task())
        for changes in (dict(channel='stable'),dict(enabled=False),dict(version_base='9.9.9')):
            with self.subTest(changes=changes),patch.object(release,'_policy',return_value=(dict(policy,**changes),mapping)):
                with self.assertRaises(util.KanpilotError):self.prepare()
                self.assertEqual(release.cmd_prepare_task_release(self.ctx,dict(task_id='parent'))['release']['state'],'blocked')
                dto=release.projection(self.conn,self.task())
                self.assertFalse(dto['can_start'])
                self.assertTrue(dto['required'])
                self.assertFalse(dto['external_repair']['can_prepare_verification'])

    def test_source_batch_and_policy_are_rechecked_in_accept_transaction(self):
        c=self.candidate()
        original=repair._candidate
        def race(conn,task,number):
            value=original(conn,task,number)
            batch=release._batch(conn,'failed-external');batch['state']='published'
            release._save(conn,batch,self.ctx.now_iso)
            return value
        with patch.object(repair,'_candidate',side_effect=race):
            with self.assertRaises(util.KanpilotError):
                repair.cmd_accept_external_build_repair(self.ctx,dict(task_id='parent',pr_number=9,
                    expected_context_sha256=c['context_sha256'],confirm=True))
        self.assertNotIn('external_build_repair_ref',db.task_extra(self.task()))

    def test_failed_report_can_prepare_restart_readonly_then_explicitly_restart(self):
        from kanpilot import derive, release_verification as verification
        self.accept()
        old,payload=self.partial_report()
        projection=release.projection(self.conn,self.task())
        shown=projection['external_repair']
        self.assertTrue(shown['can_restart_verification'])
        self.assertEqual(projection['title'],'验证受阻')
        self.assertEqual(projection['reason'],old['reason'])
        self.assertEqual(shown['verification']['failure_code'],'release_verification_report')
        self.assertIn('restart_release_verification',derive.allowed_actions(self.conn,self.task(),self.ctx.now_iso))
        self.assertNotIn('sync_release_verification',derive.allowed_actions(self.conn,self.task(),self.ctx.now_iso))
        changes=self.conn.total_changes
        prepared=verification.cmd_prepare_restart_release_verification(self.ctx,dict(task_id='parent'))
        self.assertEqual(self.conn.total_changes,changes)
        self.assertEqual(self.plus.created,1)
        self.assertEqual(prepared['expected_report_sha256'],payload['expected_report_sha256'])
        self.conn.execute("UPDATE runtime_operations SET status='unknown' WHERE scope_id=? AND operation_id=?",
            (verification._scope('parent'),verification._op(old['verification_id'])))
        with self.assertRaises(util.KanpilotError):
            verification.cmd_prepare_restart_release_verification(self.ctx,dict(task_id='parent'))
        self.conn.execute("UPDATE runtime_operations SET status='rejected' WHERE scope_id=? AND operation_id=?",
            (verification._scope('parent'),verification._op(old['verification_id'])))
        restarted=verification.cmd_restart_release_verification(self.ctx,dict(task_id='parent',**{
            k:v for k,v in prepared.items() if k.startswith('expected_')}))
        self.assertNotEqual(restarted['verification_id'],old['verification_id'])
        self.assertEqual(self.plus.created,2)

    def test_history_sync_and_restart_actions_come_only_from_bound_sql_receipts(self):
        from kanpilot import derive, release_verification as verification
        self.accept()
        first=self.start()
        value=verification._load(self.conn,'parent',first['verification_id'])
        value.update(state='blocked',failure_code='release_verification_proposal',reason='缺少本轮唯一可批准 Goal')
        verification._save(self.conn,value,self.ctx.now_iso)
        self.assertIn('sync_release_verification',derive.allowed_actions(self.conn,self.task(),self.ctx.now_iso))
        self.assertNotIn('restart_release_verification',derive.allowed_actions(self.conn,self.task(),self.ctx.now_iso))
        scope=verification._approval_operation(value)[0]
        runtime_operations.begin_operation(self.conn,scope,'unknown-write','pi_desktop','unknown_write',{})
        self.assertFalse(repair.summary(self.conn,self.task())['can_sync_verification'])
        self.assertNotIn('sync_release_verification',derive.allowed_actions(self.conn,self.task(),self.ctx.now_iso))
        self.conn.execute('DELETE FROM runtime_operations WHERE scope_id=? AND operation_id=?',(scope,'unknown-write'))
        self.conn.execute('UPDATE runtime_operations SET harness_id=? WHERE scope_id=? AND operation_id=?',
            ('other',verification._scope('parent'),verification._op(value['verification_id'])))
        self.assertFalse(repair.summary(self.conn,self.task())['can_sync_verification'])

    def test_projection_is_sql_only(self):
        self.accept()
        with patch.object(repair,'_candidate',side_effect=AssertionError('network')):
            self.conn.execute('PRAGMA query_only=ON')
            try:
                value=release.projection(self.conn,self.task())
                self.assertTrue(value['external_repair']['can_prepare_verification'])
            finally:
                self.conn.execute('PRAGMA query_only=OFF')


class PreservationTest(TestCase):
    def test_full_paginated_files_include_rename_and_deletion(self):
        paths=[dict(filename='file'+str(i),status='modified') for i in range(100)]
        paths.append(dict(filename='new',previous_filename='old',status='renamed'))
        paths.append(dict(filename='deleted',status='removed'))
        records=[dict(path=row['filename'],mode='100644',blob='a'*40) for row in paths if row['filename']!='deleted']
        calls=[]
        def api(args,**kwargs):
            calls.append(args[1])
            if '/files?' not in args[1]:return dict(changed_files=102)
            return paths[:100] if args[1].endswith('page=1') else paths[100:]
        with patch.object(repair.release.closeout.github,'contains',return_value=False), patch.object(
            repair.release.closeout.github,'commit',return_value=dict(records=records)), patch.object(repair.gh_client,'_run',side_effect=api):
            self.assertEqual(repair.preservation('a/b',8,'a'*40,'b'*40),'files')
        self.assertEqual(len(calls),3)
        with patch.object(repair.gh_client,'_run',side_effect=[dict(changed_files=102),paths[:100],[]]):
            with self.assertRaises(util.KanpilotError):repair._files('a/b',8)

    def test_changed_blob_mode_deleted_rename_and_unknown_network_rejected(self):
        original=[dict(path='new',mode='100644',blob='a'*40)]
        paths=[dict(filename='new',previous_filename='old',status='renamed'),dict(filename='deleted',status='removed')]
        for records in ([dict(path='new',mode='100755',blob='a'*40)],
                        [dict(path='new',mode='100644',blob='b'*40)],
                        original+[dict(path='old',mode='100644',blob='a'*40)],
                        original+[dict(path='deleted',mode='100644',blob='a'*40)]):
            with self.subTest(records=records), patch.object(repair.release.closeout.github,'contains',return_value=False), patch.object(
                repair.release.closeout.github,'commit',side_effect=[dict(records=original),dict(records=records)]), patch.object(
                repair.gh_client,'_run',side_effect=[dict(changed_files=2),paths]):
                with self.assertRaises(util.KanpilotError):repair.preservation('a/b',8,'a'*40,'b'*40)
        for kind,message in [('auth','HTTP 403'),('not_found','HTTP 404 not found'),('transient','network timeout')]:
            with patch.object(repair.release.closeout.github,'contains',side_effect=repair.gh_client.GhError(kind,message)), patch.object(
                repair.gh_client,'_run',side_effect=AssertionError('must not fall back')):
                with self.assertRaises(repair.gh_client.GhError):repair.preservation('a/b',8,'a'*40,'b'*40)
