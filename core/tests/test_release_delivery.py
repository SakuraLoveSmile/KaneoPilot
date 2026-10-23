"""Isolated release lifecycle. Plus/Git/Kaneo fixture is local, all release APIs fake."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import test_parent_closeout as closeout_fixtures
from kanpilot import db, util, project_config, release_delivery as delivery


class FakeReleaseAPI:
    def __init__(self):
        self.sent=[];self.published=False;self.lose_reply=False;self.failed=False
        self.runs=[];self.stable=[];self.bad={};self.asset_sha='1'*64
    def workflow_snapshot(self,repo,path,ref):
        return dict(id=44,path=path,ref=ref,head_sha='c'*40,state='active',repository=repo)
    def list_releases(self,repo):return deepcopy(self.stable)
    def dispatch(self,repo,workflow,ref,inputs):
        self.sent.append(deepcopy(inputs));self.input=inputs;self.manifest=json.loads(inputs['task_manifest_json'])
        self.runs=[dict(id=100,workflow_id=44,event='workflow_dispatch',display_title='pilot-release-'+self.manifest['batch_id'],
            head_branch=ref,head_sha='c'*40,repository=dict(full_name=repo),status='queued',conclusion=None,run_attempt=1,
            html_url='https://github.com/'+repo+'/actions/runs/100')]
        if self.lose_reply:raise RuntimeError('lost response')
        return dict(workflow_run_id=100,run_url='https://api.github.com/repos/'+repo+'/actions/runs/100',html_url=self.runs[0]['html_url'])
    def find_runs(self,repo,workflow):return deepcopy(self.runs)
    def run(self,repo,run_id):
        result=deepcopy(self.runs[0]);result.update(self.bad.get('run',{}))
        if self.published or self.failed:result.update(status='completed',conclusion='failure' if self.failed else 'success')
        return result
    def jobs(self,repo,run_id,attempt):return deepcopy(self.bad.get('jobs',[dict(name='Build',status='completed',conclusion='success')]))
    def manifest_bytes(self):
        value=dict(schema=1,batch_id=self.manifest['batch_id'],repository=self.manifest['repository'],target_sha=self.manifest['target_sha'],
            task_manifest_sha256=self.input['task_manifest_sha256'],task_manifest=self.manifest,workflow_run_id=100,run_attempt=1,
            channel=self.manifest['channel'],version=self.manifest['version'],tag=self.manifest['tag'],build_number=self.manifest['build_number'],
            assets=[dict(name='app.apk',size=10,sha256=self.asset_sha)])
        value.update(self.bad.get('manifest',{}));return util.jdump(value).encode()
    def provenance(self,repo,run_id):return self.bad.get('artifact',self.manifest_bytes())
    def release(self,repo,tag):
        if not self.published:return None
        value=dict(id=10,tag_name=tag,draft=False,prerelease=True,html_url='https://github.com/'+repo+'/releases/tag/'+tag,
            assets=[dict(id=1,name='app.apk',size=10,digest='sha256:'+self.asset_sha,
                browser_download_url='https://github.com/'+repo+'/releases/download/'+tag+'/app.apk'),
                dict(id=2,name='kaneopilot-release.json',size=len(self.manifest_bytes()),
                browser_download_url='https://github.com/'+repo+'/releases/download/'+tag+'/kaneopilot-release.json')])
        value.update(self.bad.get('release',{}));return value
    def tag_commit(self,repo,tag):return self.bad.get('tag',self.manifest['target_sha']) if self.published else None
    def asset_bytes(self,repo,asset):return self.bad.get('release_manifest',self.manifest_bytes())
    def merge_commit_sha(self,repo,number,head):return head


class ReleaseLifecycleTest(TestCase):
    def setUp(self):
        self.fixture=closeout_fixtures.ParentCloseoutTest('test_success_reuses_final_checkpoint_without_worktree_or_new_model')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.conn=self.fixture.conn;self.ctx=self.fixture.ctx;self.api=FakeReleaseAPI()
        self.policy=dict(enabled=True,channel='prerelease',workflow_path='.github/workflows/pilot-release.yml',
            workflow_ref='main',version_base='0.1.0',required_assets=['app.apk'],required_jobs=['Build'])
        self.configure()
        p=patch.object(delivery,'github',self.api,create=True);p.start();self.addCleanup(p.stop)
    def configure(self):
        db.set_setting(self.conn,project_config.SETTINGS_KEY,dict(version=1,revision=1,items={
            'project':dict(repository=str(self.fixture.repo),automation=dict(policy_revision=1,release=self.policy))}))
    def row(self,name='parent'):return db.require_task(self.conn,name)
    def prepare(self,name='parent'):return delivery.cmd_prepare_task_release(self.ctx,dict(task_id=name))
    def start(self,name='parent'):
        preview=self.prepare(name)
        return delivery.cmd_start_task_release(self.ctx,dict(task_id=name,expected_context_sha256=preview['context_sha256']))
    def sync(self,batch_id):return delivery.cmd_sync_release_batch(self.ctx,dict(batch_id=batch_id))
    def test_software_ready_children_need_not_be_done_and_share_one_release(self):
        db.update_task(self.conn,'fix1',self.ctx.now_iso,status='in_review')
        self.fixture.fake.tasks['fix1']['status']='in-review'
        self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))
        result=self.start();self.assertEqual(len(self.api.sent),1)
        batch_id=result['release']['batch_id'];self.assertEqual(result['release']['state'],'queued')
        self.assertEqual(self.start('fix1')['release']['batch_id'],batch_id)
        self.assertEqual(len(self.api.sent),1)
        self.assertEqual(set(result['release']['member_task_ids']),{'parent','fix1','fix2'})
        self.api.published=True;result=self.sync(batch_id)
        self.assertEqual(result['release']['state'],'published',result)
        self.assertIsNone(delivery.completion_gate(self.conn,self.row()))
        self.assertIsNone(delivery.completion_gate(self.conn,self.row('fix1')))
        self.assertEqual(self.row()['status'],'in_review')
        db.update_task(self.conn,'fix1',self.ctx.now_iso,status='done');self.fixture.fake.tasks['fix1']['status']='done'
        self.assertEqual(self.fixture.run_closeout()['outcome'],'completed')
    def test_unknown_recovers_unique_marker_and_never_resends(self):
        self.api.lose_reply=True;result=self.start();self.assertEqual(result['release']['state'],'unknown')
        batch_id=result['release']['batch_id']
        self.start();self.assertEqual(len(self.api.sent),1)
        self.assertEqual(self.sync(batch_id)['release']['state'],'queued')
        self.api.published=True;self.assertEqual(self.sync(batch_id)['release']['state'],'published')
    def test_scheduler_reconciles_unknown_dispatch_when_due_without_resending(self):
        self.api.lose_reply=True;result=self.start();batch_id=result['release']['batch_id']
        later=SimpleNamespace(conn=self.conn,now_iso=util.plus_seconds(61,self.ctx.now),now=self.ctx.now)
        with patch.object(self.api,'find_runs',wraps=self.api.find_runs) as find:
            self.assertEqual(delivery.sync_existing(self.ctx)['results'],[])
            self.assertEqual(delivery.sync_existing(later,project_ids=[])['results'],[])
            recovered=delivery.sync_existing(later,project_ids=['project'])['results']
            self.assertEqual(len(recovered),1)
            self.assertEqual(recovered[0]['batch_id'],batch_id)
            self.assertEqual(recovered[0]['release']['state'],'queued')
            self.assertEqual(recovered[0]['release']['workflow_run_id'],100)
            self.assertEqual(recovered[0]['release']['blockers'],[])
            self.assertEqual(find.call_count,1)
        receipt=db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+batch_id)
        self.assertEqual(receipt['status'],'accepted')
        self.assertEqual(len(self.api.sent),1)
        self.assertEqual(delivery.sync_existing(later)['results'],[])

    def test_unknown_zero_matches_replaces_old_transport_blocker_with_current_observation(self):
        self.api.lose_reply=True;result=self.start();batch_id=result['release']['batch_id']
        self.assertTrue(result['release']['blockers'])
        original=dict(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+batch_id))
        self.api.runs=[];observed=self.sync(batch_id)['release']
        self.assertEqual(observed['state'],'unknown')
        self.assertEqual(observed['reason'],'GitHub 暂未查到本批次运行；将自动核对，暂不重新发送')
        self.assertEqual(observed['blockers'],[dict(code='release_run_not_observed',message=observed['reason'])])
        self.assertFalse(observed['can_start']);self.assertFalse(observed['can_abandon'])
        self.assertTrue(observed['can_reconcile'])
        self.assertEqual(dict(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+batch_id)),original)
        self.assertEqual(len(self.api.sent),1)

    def test_unknown_recovered_failed_run_clears_old_transport_blocker(self):
        self.api.lose_reply=True;result=self.start();batch_id=result['release']['batch_id']
        self.assertTrue(result['release']['blockers'])
        self.api.failed=True;observed=self.sync(batch_id)['release']
        self.assertEqual(observed['state'],'failed')
        self.assertEqual(observed['workflow_run_id'],100)
        self.assertIn('构建运行未成功',observed['reason'])
        self.assertEqual(observed['blockers'],[])
        self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))
        self.assertEqual(len(self.api.sent),1)
    def test_missing_or_wrong_provenance_never_opens_completion_gate(self):
        result=self.start();batch=result['release']['batch_id'];self.api.published=True
        for bad in [dict(tag='0'*40),dict(manifest=dict(target_sha='0'*40)),
                    dict(manifest=dict(run_attempt=2)),dict(release_manifest=b'changed'),
                    dict(artifact=b'{}'),dict(jobs=[dict(name='Build',status='completed',conclusion='skipped')])]:
            with self.subTest(bad=bad):
                self.api.bad=bad;result=self.sync(batch)
                self.assertEqual(result['release']['state'],'blocked',result)
                self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))
        self.api.bad={};self.assertEqual(self.sync(batch)['release']['state'],'published')
    def test_failed_build_is_not_automatically_retried(self):
        result=self.start();self.api.failed=True
        self.assertEqual(self.sync(result['release']['batch_id'])['release']['state'],'failed')
        self.start();self.assertEqual(len(self.api.sent),1)
    def test_policy_off_keeps_old_flow_and_does_not_dispatch_historical_done(self):
        self.policy['enabled']=False;self.configure()
        self.assertIsNone(delivery.completion_gate(self.conn,self.row()))
        self.assertEqual(delivery.after_merge(self.ctx,self.row())['outcome'],'disabled')
        self.assertEqual(self.api.sent,[])
    def test_disabled_after_dispatch_does_not_remove_completion_gate(self):
        result=self.start();self.policy['enabled']=False;self.configure()
        self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))
        self.api.published=True;self.assertEqual(self.sync(result['release']['batch_id'])['release']['state'],'published')
    def test_projection_and_gate_are_sql_only(self):
        self.conn.execute('PRAGMA query_only=ON');before=list(self.conn.iterdump())
        with patch.object(self.api,'workflow_snapshot',side_effect=AssertionError('network')):
            self.assertEqual(delivery.projection(self.conn,self.row())['state'],'ready')
            self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))
        self.assertEqual(before,list(self.conn.iterdump()))
    def test_manifest_has_only_five_string_inputs_and_current_members(self):
        result=self.start();sent=self.api.sent[0]
        self.assertEqual(set(sent),{'target_sha','release_batch_id','task_manifest_json','task_manifest_sha256','tag_name'})
        self.assertTrue(all(isinstance(value,str) for value in sent.values()))
        self.assertEqual(util.sha256_bytes(sent['task_manifest_json'].encode()),sent['task_manifest_sha256'])
        self.assertEqual(json.loads(sent['task_manifest_json'])['members'][0]['task_id'],'fix1')

    def test_nested_manifest_bool_cannot_replace_frozen_integer(self):
        result=self.start();self.api.published=True
        altered=deepcopy(self.api.manifest);altered['schema']=True
        self.api.bad={'manifest':{'task_manifest':altered}}
        self.assertEqual(self.sync(result['release']['batch_id'])['release']['state'],'blocked')

    def test_stale_sync_cannot_erase_accepted_publication(self):
        result=self.start();batch_id=result['release']['batch_id'];stale=deepcopy(delivery._batch(self.conn,batch_id))
        stale_unsent=deepcopy(stale);stale_unsent.pop('workflow_run_id');stale_unsent['dispatch_status']='not_sent'
        self.assertEqual(delivery._failure(self.conn,stale_unsent,self.ctx.now_iso,RuntimeError('stale preflight'))['release']['state'],'queued')
        self.api.published=True;self.assertEqual(self.sync(batch_id)['release']['state'],'published')
        stale.update(state='building')
        with db.write_txn(self.conn):delivery._save(self.conn,stale,self.ctx.now_iso)
        self.assertEqual(stale['state'],'published')
        result=delivery._failure(self.conn,stale,self.ctx.now_iso,RuntimeError('stale failure'))
        self.assertEqual(result['outcome'],'published')
        self.assertIsNone(delivery.completion_gate(self.conn,self.row()))

    def test_report_or_workflow_drift_after_reservation_never_dispatches(self):
        prepared=self.prepare();path=self.row('fix2')['review_report_path']
        from pathlib import Path
        with patch.object(self.api,'tag_commit',side_effect=lambda *args:Path(path).write_text('changed') and None):
            result=delivery.cmd_start_task_release(self.ctx,dict(task_id='parent',expected_context_sha256=prepared['context_sha256']))
        self.assertEqual(result['release']['state'],'blocked');self.assertEqual(self.api.sent,[])

    def test_workflow_ref_moves_at_last_read_never_dispatches(self):
        prepared=self.prepare();original=self.api.workflow_snapshot;calls=[]
        def snapshot(*args):
            calls.append(args);value=original(*args)
            if len(calls)==3:value['head_sha']='d'*40
            return value
        with patch.object(self.api,'workflow_snapshot',side_effect=snapshot):
            result=delivery.cmd_start_task_release(self.ctx,dict(task_id='parent',expected_context_sha256=prepared['context_sha256']))
        self.assertEqual(result['release']['state'],'blocked');self.assertEqual(self.api.sent,[])

    def test_run_identity_and_asset_contract_fail_closed(self):
        result=self.start();batch=result['release']['batch_id'];self.api.published=True
        for bad in [dict(run=dict(repository=dict(full_name='other/repo'))),dict(run=dict(head_sha='d'*40)),
                    dict(run=dict(head_branch='other')),dict(run=dict(workflow_id=55)),dict(run=dict(run_attempt=2)),
                    dict(release=dict(assets=[])),dict(release=dict(prerelease=False)),dict(release=dict(draft=True))]:
            with self.subTest(bad=bad):
                self.api.bad=bad;self.assertEqual(self.sync(batch)['release']['state'],'blocked')
        self.api.bad={};release=self.api.release('example/closeout',self.api.manifest['tag'])
        for mutation in [dict(digest=None),dict(digest='sha256:'+'0'*64),dict(size=0),
                         dict(browser_download_url='https://github.com/other/repo/releases/download/v1/app.apk')]:
            with self.subTest(mutation=mutation):
                modified=deepcopy(release);modified['assets'][0].update(mutation);self.api.bad={'release':modified}
                self.assertEqual(self.sync(batch)['release']['state'],'blocked')
        self.api.bad={};self.assertEqual(self.sync(batch)['release']['state'],'published')
        db.set_extra(self.conn,self.row(),self.ctx.now_iso,review_conclusion='pass')
        self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))

    def test_unknown_zero_and_multiple_matches_do_not_resend(self):
        self.api.lose_reply=True;result=self.start();batch=result['release']['batch_id'];runs=deepcopy(self.api.runs)
        self.api.runs=[];self.assertEqual(self.sync(batch)['release']['state'],'unknown')
        self.api.runs=runs*2;result=self.sync(batch)
        self.assertEqual(result['release']['blockers'][0]['code'],'release_run_ambiguous')
        self.assertEqual(len(self.api.sent),1)

    def test_scheduler_only_polls_due_existing_batches_in_project_scope(self):
        result=self.start();batch=result['release']['batch_id']
        with patch.object(self.api,'run',wraps=self.api.run) as observed:
            self.assertEqual(delivery.sync_existing(self.ctx)['results'],[])
            later=SimpleNamespace(conn=self.conn,now_iso=util.plus_seconds(61,self.ctx.now),now=self.ctx.now)
            self.assertEqual(delivery.sync_existing(later,project_ids=[])['results'],[])
            self.assertEqual(len(delivery.sync_existing(later,project_ids=['project'])['results']),1)
            self.assertEqual(observed.call_count,1)
            delivery._failure(self.conn,delivery._batch(self.conn,batch),self.ctx.now_iso,RuntimeError('blocked'))
            self.assertEqual(delivery.sync_existing(later)['results'],[])
            delivery.after_merge(later,self.row());self.assertEqual(observed.call_count,1)
        self.assertEqual(len(self.api.sent),1)

    def test_transient_release_reads_retry_same_batch_when_due_without_dispatch(self):
        from kanpilot import gh_client
        batch_id=self.start()['release']['batch_id']
        original=delivery._batch(self.conn,batch_id)
        receipt=dict(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+batch_id))
        with patch.object(self.api,'run',side_effect=gh_client.GhError('transient','读取连接 EOF')) as read:
            first=self.sync(batch_id)
            self.assertEqual(first['release']['state'],'queued')
            self.assertIn('自动核对',first['release']['reason'])
            self.assertEqual(delivery.sync_existing(self.ctx)['results'],[])
            later=SimpleNamespace(conn=self.conn,now_iso=util.plus_seconds(61,self.ctx.now),now=self.ctx.now)
            second=delivery.sync_existing(later)['results']
            self.assertEqual(len(second),1)
            self.assertEqual(second[0]['release']['batch_id'],batch_id)
            self.assertEqual(read.call_count,2)
        self.api.published=True
        recovered=self.sync(batch_id)
        self.assertEqual(recovered['release']['state'],'published')
        current=delivery._batch(self.conn,batch_id)
        self.assertEqual(current.get('read_failures'),0)
        for key in ('batch_id','target_sha','workflow_run_id','inputs'):
            self.assertEqual(current[key],original[key])
        self.assertEqual(len(self.api.sent),1)
        self.assertEqual(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+batch_id)['operation_id'],receipt['operation_id'])

    def test_unknown_dispatch_matched_before_transient_read_keeps_reconciling(self):
        from kanpilot import gh_client
        self.api.lose_reply=True
        batch_id=self.start()['release']['batch_id']
        with patch.object(self.api,'run',side_effect=gh_client.GhError('transient','读取连接 EOF')):
            result=self.sync(batch_id)
        self.assertEqual(result['release']['state'],'unknown')
        self.assertEqual(result['release']['workflow_run_id'],100)
        later=SimpleNamespace(conn=self.conn,now_iso=util.plus_seconds(61,self.ctx.now),now=self.ctx.now)
        observed=delivery.sync_existing(later)['results']
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0]['release']['state'],'queued')
        self.assertEqual(len(self.api.sent),1)
        self.assertEqual(delivery._batch(self.conn,batch_id)['read_failures'],0)

    def test_existing_blocked_batch_does_not_claim_automatic_read_recovery(self):
        from kanpilot import gh_client
        batch_id=self.start()['release']['batch_id']
        delivery._failure(self.conn,delivery._batch(self.conn,batch_id),self.ctx.now_iso,
                          RuntimeError('previous evidence blocker'))
        with patch.object(self.api,'run',side_effect=gh_client.GhError('transient','读取连接 EOF')):
            result=self.sync(batch_id)
        self.assertEqual(result['release']['state'],'blocked')
        self.assertNotIn('自动核对',result['release']['reason'])
        self.assertEqual(len(self.api.sent),1)

    def test_release_read_retries_are_bounded_and_auth_is_not_retried(self):
        from kanpilot import gh_client
        for error_kind in ('timeout','transient','auth','not_found'):
            with self.subTest(error_kind=error_kind):
                batch_id=self.start()['release']['batch_id']
                batch=delivery._batch(self.conn,batch_id)
                batch.update(state='queued',read_failures=0)
                delivery._save(self.conn,batch,self.ctx.now_iso)
                with patch.object(self.api,'run',side_effect=gh_client.GhError(error_kind,'read failed')):
                    limit=3 if error_kind in ('timeout','transient') else 1
                    for attempt in range(limit):
                        result=self.sync(batch_id)
                        self.assertEqual(result['release']['state'],'blocked' if attempt==limit-1 else 'queued')
                    later=SimpleNamespace(conn=self.conn,now_iso=util.plus_seconds(301,self.ctx.now),now=self.ctx.now)
                    self.assertEqual(delivery.sync_existing(later)['results'],[])
                self.assertEqual(len(self.api.sent),1)

    def test_reserved_group_cannot_be_reserved_twice(self):
        context=delivery._prepare(self.ctx,'parent');delivery._reserve(self.ctx,context)
        with self.assertRaises(delivery.ReleaseBlocked):delivery._reserve(self.ctx,context)
        self.assertEqual(len(delivery._batches(self.conn,'project')),1);self.assertEqual(self.api.sent,[])

    def test_projection_waits_for_software_ready_not_planning_or_execution(self):
        for status in ('needs_plan','running','failed','blocked'):
            with self.subTest(status=status):
                db.update_task(self.conn,'fix1',self.ctx.now_iso,status=status)
                value=delivery.projection(self.conn,self.row())
                self.assertFalse(value['can_start']);self.assertEqual(value['state'],'waiting_group')
        db.update_task(self.conn,'fix1',self.ctx.now_iso,status='in_review')
        self.assertTrue(delivery.projection(self.conn,self.row())['can_start'])

    def test_standalone_current_pass_can_release_and_old_done_is_explicit_only(self):
        db.update_task(self.conn,'fix2',self.ctx.now_iso,review_source_task_id=None)
        self.assertEqual(delivery.after_merge(self.ctx,self.row('fix2'))['outcome'],'disabled')
        self.assertEqual(self.api.sent,[])
        result=self.start('fix2');self.assertEqual(result['release']['member_task_ids'],['fix2'])
        self.api.published=True;self.assertEqual(self.sync(result['release']['batch_id'])['release']['state'],'published')

    def test_historical_parent_uses_saved_checkpoint_after_later_workflow_commit(self):
        self.policy['enabled']=False;self.configure();self.assertEqual(self.fixture.run_closeout()['outcome'],'completed')
        saved=db.task_extra(self.row())['parent_closeout']['target_sha']
        from test_git_delivery import git
        (self.fixture.repo/'later-workflow.txt').write_text('not part of verified source')
        git(self.fixture.repo,'add','.');git(self.fixture.repo,'commit','-m','later workflow')
        self.fixture.tip=git(self.fixture.repo,'rev-parse','HEAD')
        self.policy['enabled']=True;self.configure()
        self.assertEqual(delivery.after_merge(self.ctx,self.row())['outcome'],'disabled')
        result=self.start();self.assertEqual(result['release']['target_sha'],saved)
        self.assertNotEqual(saved,self.fixture.tip)

    def test_stable_inventory_includes_full_remote_and_frozen_local_versions(self):
        self.policy['channel']='stable';self.configure()
        self.api.stable=[dict(draft=False,prerelease=False,tag_name='v0.9.9'),
                         dict(draft=False,prerelease=True,tag_name='v9.9.9-pilot.1'),
                         dict(draft=False,prerelease=False,tag_name='v0.2.0')]
        context=delivery._prepare(self.ctx,'parent');self.assertEqual(context['version'],'0.9.10')
        batch=delivery._reserve(self.ctx,context)
        self.assertEqual(batch['tag'],'v0.9.10')
        versions,_=delivery._version_inventory(self.conn,'project',self.api.stable)
        self.assertEqual(versions[-1],(0,9,10))
        with self.assertRaises(delivery.ReleaseBlocked):delivery._stable_available(self.conn,'project',self.policy)

    def test_not_sent_reservation_recovers_same_batch_once_and_unknown_never_resends(self):
        context=delivery._prepare(self.ctx,'parent');batch=delivery._reserve(self.ctx,context)
        self.assertTrue(delivery.projection(self.conn,self.row())['can_start'])
        self.assertEqual(self.sync(batch['batch_id'])['release']['state'],'blocked')
        self.assertEqual(self.api.sent,[])
        with patch.object(self.api,'workflow_snapshot',side_effect=OSError('temporary workflow read failure')):
            self.assertEqual(self.start()['release']['state'],'blocked')
        self.assertEqual(self.api.sent,[])
        result=self.start();self.assertEqual(result['release']['batch_id'],batch['batch_id'])
        self.assertEqual(result['release']['state'],'queued');self.start()
        self.assertEqual(len(self.api.sent),1)

    def test_abandon_only_proven_unsent_batch_retains_audit_and_allows_new_batch(self):
        self.policy['channel']='stable';self.configure()
        context=delivery._prepare(self.ctx,'parent');old=delivery._reserve(self.ctx,context)
        db.update_task(self.conn,'fix1',self.ctx.now_iso,requirements_sha256='drift')
        value=delivery.projection(self.conn,self.row())
        self.assertTrue(value['can_abandon']);self.assertFalse(value['can_start'])
        self.assertEqual(delivery.after_merge(self.ctx,self.row())['outcome'],'blocked')
        with self.assertRaises(delivery.ReleaseBlocked):self.start()
        result=delivery.cmd_abandon_unsent_release(self.ctx,dict(batch_id=old['batch_id'],expected_context_sha256=old['context_sha256']))
        self.assertEqual(result['outcome'],'abandoned');self.assertFalse(result['release']['required'])
        self.assertFalse(result['release']['can_abandon']);self.assertFalse(result['release']['can_start'])
        self.assertTrue(delivery._batch(self.conn,old['batch_id'])['abandoned'])
        self.assertNotIn('release_ref',db.task_extra(self.row()))
        self.assertEqual(self.row()['status'],'in_review')
        db.update_task(self.conn,'fix1',self.ctx.now_iso,requirements_sha256=next(m for m in old['members'] if m['task_id']=='fix1')['requirements_sha256'])
        new=self.start();self.assertNotEqual(new['release']['batch_id'],old['batch_id'])
        self.assertEqual(len(self.api.sent),1);self.assertEqual(len(delivery._batches(self.conn,'project')),2)

    def test_abandon_rejects_unknown_or_any_existing_dispatch_receipt(self):
        self.api.lose_reply=True;result=self.start();batch=delivery._batch(self.conn,result['release']['batch_id'])
        with self.assertRaises(delivery.ReleaseBlocked):
            delivery.cmd_abandon_unsent_release(self.ctx,dict(batch_id=batch['batch_id'],expected_context_sha256=batch['context_sha256']))
        self.assertFalse(delivery.projection(self.conn,self.row())['can_abandon'])
        # Even a stale/corrupt flag cannot override durable send intent.
        batch.update(state='blocked',dispatch_status='not_sent')
        with db.write_txn(self.conn):delivery._save(self.conn,batch,self.ctx.now_iso)
        with self.assertRaises(delivery.ReleaseBlocked):
            delivery.cmd_abandon_unsent_release(self.ctx,dict(batch_id=batch['batch_id'],expected_context_sha256=batch['context_sha256']))
        self.assertEqual(len(self.api.sent),1)

    def test_abandon_during_preflight_prevents_old_dispatch(self):
        context=delivery._prepare(self.ctx,'parent');old=delivery._reserve(self.ctx,context)
        other=db.connect(self.fixture.db_path);self.addCleanup(other.close)
        peer=SimpleNamespace(conn=other,now_iso=self.ctx.now_iso,now=self.ctx.now)
        def abandon(*args):
            delivery.cmd_abandon_unsent_release(peer,dict(batch_id=old['batch_id'],expected_context_sha256=old['context_sha256']))
            return None
        with patch.object(self.api,'tag_commit',side_effect=abandon):
            result=delivery._dispatch_reserved(self.ctx,old,self.row())
        self.assertEqual(self.api.sent,[])
        self.assertIsNone(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+old['batch_id']))
        self.assertTrue(delivery._batch(self.conn,old['batch_id'])['abandoned'])
        self.assertFalse(result['release']['can_start'])

    def test_abandon_then_new_batch_during_preflight_preserves_new_refs_without_old_dispatch(self):
        context=delivery._prepare(self.ctx,'parent');old=delivery._reserve(self.ctx,context);new=[]
        other=db.connect(self.fixture.db_path);self.addCleanup(other.close)
        peer=SimpleNamespace(conn=other,now_iso=self.ctx.now_iso,now=self.ctx.now)
        def replace(*args):
            delivery.cmd_abandon_unsent_release(peer,dict(batch_id=old['batch_id'],expected_context_sha256=old['context_sha256']))
            new.append(delivery._reserve(peer,delivery._prepare(peer,'parent')))
            return None
        with patch.object(self.api,'tag_commit',side_effect=replace):
            delivery._dispatch_reserved(self.ctx,old,self.row())
        self.assertEqual(self.api.sent,[])
        self.assertIsNone(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+old['batch_id']))
        self.assertTrue(delivery._batch(self.conn,old['batch_id'])['abandoned'])
        self.assertEqual(delivery._batch(self.conn,new[0]['batch_id'])['dispatch_status'],'not_sent')
        for member in old['members']:
            self.assertEqual(db.task_extra(self.row(member['task_id']))['release_ref']['batch_id'],new[0]['batch_id'])

    def test_preflight_failure_after_abandon_cannot_revive_old_batch(self):
        context=delivery._prepare(self.ctx,'parent');old=delivery._reserve(self.ctx,context)
        other=db.connect(self.fixture.db_path);self.addCleanup(other.close)
        peer=SimpleNamespace(conn=other,now_iso=self.ctx.now_iso,now=self.ctx.now)
        def fail_after_abandon(*args):
            delivery.cmd_abandon_unsent_release(peer,dict(batch_id=old['batch_id'],expected_context_sha256=old['context_sha256']))
            raise OSError('preflight read failed after another writer abandoned the batch')
        with patch.object(self.api,'tag_commit',side_effect=fail_after_abandon):
            result=delivery._dispatch_reserved(self.ctx,old,self.row())
        self.assertEqual(self.api.sent,[])
        self.assertIsNone(db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+old['batch_id']))
        self.assertTrue(delivery._batch(self.conn,old['batch_id'])['abandoned'])
        self.assertFalse(result['release']['can_start'])
        self.assertFalse(result['release']['can_abandon'])

    def test_attempt_rerun_during_evidence_read_cannot_accept_old_success(self):
        result=self.start();batch=result['release']['batch_id'];self.api.published=True
        original=self.api.asset_bytes
        def rerun(*args):
            raw=original(*args);self.api.bad['run']=dict(run_attempt=2,status='in_progress',conclusion=None)
            return raw
        with patch.object(self.api,'asset_bytes',side_effect=rerun):
            result=self.sync(batch)
        self.assertEqual(result['release']['state'],'blocked')
        self.assertEqual(result['release']['blockers'][0]['code'],'release_run_changed')
        self.assertIsNotNone(delivery.completion_gate(self.conn,self.row()))

    def test_unknown_crash_recovery_checks_pid_birth_and_releases_only_original_lock(self):
        self.api.lose_reply=True;result=self.start();batch=delivery._batch(self.conn,result['release']['batch_id'])
        operation=db.get_runtime_operation(self.conn,delivery._scope('project'),'dispatch:'+batch['batch_id'])
        owner=util.jload(operation['result_json'])['lifecycle_owner']
        db.set_setting(self.conn,'lifecycle_owner',owner)
        process=util.jload(operation['result_json'])['process_identity']
        with patch.object(delivery.process_owner,'process_birth',return_value={k:process[k] for k in ('started_seconds','started_microseconds')}):
            value=self.sync(batch['batch_id']);self.assertEqual(value['release']['state'],'unknown')
            self.assertEqual(db.get_setting(self.conn,'lifecycle_owner'),owner)
        with patch.object(delivery.process_owner,'process_birth',return_value=dict(started_seconds=0,started_microseconds=0)):
            value=self.sync(batch['batch_id']);self.assertEqual(value['release']['state'],'queued')
            self.assertFalse(db.get_setting(self.conn,'lifecycle_owner'))


class ReleasePolicyTest(TestCase):
    def test_policy_is_explicit_and_requires_safe_assets_and_workflow(self):
        self.assertFalse(delivery.validate_policy({})['enabled'])
        for raw in [dict(enabled=True),dict(workflow_path='../bad.yml'),dict(workflow_ref='a'*40),dict(workflow_ref='refs/heads/main'),
                    dict(required_assets=['../app.apk']),dict(required_assets=['*.apk']),dict(channel='latest'),
                    dict(version_base='1.0-beta')]:
            with self.subTest(raw=raw),self.assertRaises(util.KanpilotError):delivery.validate_policy(raw)
