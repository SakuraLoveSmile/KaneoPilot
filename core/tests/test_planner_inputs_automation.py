from helpers import KanpilotTestCase
from kanpilot import automation_policy, core, db, planner_inputs, util
from types import SimpleNamespace


class TestAutomationSubmission(KanpilotTestCase):
    def prepare(self, task_id='automationtask000001'):
        pid = 'ryryo1reph1xfemqtwkjyiiz'
        remote = self.fake.add_task(task_id, pid)
        repo = self.make_repo('Feedback')
        prepared = self.call('prepare-plan-input', {'task_id':task_id})
        conn = db.connect(self.db_path)
        ctx = SimpleNamespace(conn=conn, now_iso=util.now_iso())
        policy = automation_policy.cmd_set_project_automation(ctx, {'project_id':pid,'automation':{'enabled':True}})['automation']
        delivery = {'mode':'pr','branch':'kaneo/'+task_id,'worktree_path':str(repo / 'branch'),
                    'base_branch':'main','base_sha':'a'*40,'head_sha':'a'*40}
        conn.close()
        item = {'input_id':prepared['input_id'],'input_sha256':prepared['input_sha256'],
                'automation':{'authorized_by':'automation','policy_revision':policy['policy_revision'],'delivery':delivery}}
        return task_id, item

    def test_adoption_atomic_and_manual_context(self):
        task_id, item = self.prepare()
        result = self.call('start-plans', {'items':[item]})['results'][0]
        self.assertIn('request_id', result)
        conn = db.connect(self.db_path)
        task = db.require_task(conn, task_id)
        request = db.get_request(conn, result['request_id'])
        payload = db.request_payload(request)
        self.assertEqual(request['requested_by'], 'automation')
        self.assertTrue(payload['manual_authorized'])
        self.assertEqual(payload['authorized_by'], 'automation')
        self.assertEqual(payload['plan_cwd'], item['automation']['delivery']['worktree_path'])
        self.assertEqual(core.automation_of(task)['plan_attempts'], 1)
        self.assertEqual(task['requirements_sha256'], payload['manifest']['requirements_sha256'])
        db.update_request(conn, request['request_id'], util.now_iso(), status='succeeded')
        conn.close()
        manual = dict(item); manual.pop('automation')
        result = self.call('start-plans', {'items':[manual]})['results'][0]
        conn = db.connect(self.db_path)
        request = db.get_request(conn, result['request_id'])
        self.assertEqual(request['requested_by'], 'user')
        self.assertEqual(db.request_payload(request)['delivery_mode'], 'pr')
        self.assertEqual(core.automation_of(db.require_task(conn, task_id))['plan_attempts'], 1)
        conn.close()

    def test_same_task_fix_payload(self):
        task_id, item = self.prepare()
        first = self.call('start-plans', {'items':[item]})['results'][0]
        conn = db.connect(self.db_path)
        db.update_request(conn, first['request_id'], util.now_iso(), status='succeeded')
        task = db.require_task(conn, task_id)
        report = self.reports_root / 'same-task.md'
        report.write_text('# Review\nFix timeout handling in core/request.py, line 42.\n',encoding='utf-8')
        fix = {'state':'open','fix_round':1,'report_path':str(report),'report_sha256':util.sha256_file(report),'review_key':'review-key'}
        db.set_extra(conn, task, util.now_iso(), fix_request=fix)
        core.update_automation(conn, task, util.now_iso(), fix_round=1, plan_wanted=True)
        conn.close()
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        item.update(input_id=prepared['input_id'],input_sha256=prepared['input_sha256'])
        result = self.call('start-plans', {'items':[item]})['results'][0]
        conn = db.connect(self.db_path)
        payload = db.request_payload(db.get_request(conn, result['request_id']))
        self.assertEqual(payload['fix_mode'], 'same_task')
        self.assertEqual(payload['review_source_task_id'], task_id)
        self.assertEqual(payload['fix_task_id'], task_id)
        self.assertEqual(payload['fix_round'], 1)
        self.assertEqual(payload['review_report_path'], fix['report_path'])
        self.assertEqual(payload['review_report_sha256'], fix['report_sha256'])
        conn.close()

    def test_invalid_adoption_rolls_back_import(self):
        task_id, item = self.prepare()
        item['automation']['policy_revision'] += 1
        result = self.call('start-plans', {'items':[item]})['results'][0]
        self.assertEqual(result['code'], 'conflict')
        conn = db.connect(self.db_path)
        self.assertIsNone(db.get_task(conn, task_id))
        self.assertIsNone(db.active_request_for_key(conn, 'plan:'+task_id))
        conn.close()

    def test_fake_kaneo_dry_preview_and_real_intake_commands(self):
        from unittest.mock import patch
        from kanpilot import auto_intake, git_delivery
        pid = 'ryryo1reph1xfemqtwkjyiiz'
        remote_task = self.fake.add_task('intakefaketask000001',pid)
        repo = self.make_repo('Feedback')
        conn = db.connect(self.db_path)
        ctx = SimpleNamespace(conn=conn,now_iso=util.now_iso())
        automation_policy.cmd_set_project_automation(ctx,{'project_id':pid,'automation':{'enabled':True}})
        before = list(conn.iterdump())
        delivery = {'branch':'kaneo/'+remote_task['id'],'worktree_path':str(repo/'branch'),
                    'base_branch':'main','base_sha':'a'*40,'head_sha':'a'*40}
        with patch.object(automation_policy,'automation_preflight',return_value=[]):
            preview = auto_intake.run_once(self.db_path,dry_run=True)
            self.assertEqual(preview['projects'][0]['candidates'],[remote_task['id']])
            self.assertEqual(list(conn.iterdump()),before)
            with patch.object(git_delivery,'prepare_branch_worktree',return_value=delivery):
                result = auto_intake.run_once(self.db_path)
        self.assertEqual(result['projects'][0]['adopted'],[remote_task['id']])
        request = db.active_request_for_key(conn,'plan:'+remote_task['id'])
        self.assertEqual(request['requested_by'],'automation')
        self.assertEqual(db.request_payload(request)['delivery_head_sha'],'a'*40)
        conn.close()

    def open_fix(self):
        task_id,item = self.prepare('fixmaterialtask000001')
        result = self.call('start-plans',{'items':[item]})['results'][0]
        report = self.reports_root / 'material-review.md'
        report.write_text('# Review\nFix the timeout and preserve the failed request ID.\n',encoding='utf-8')
        fix = {'state':'open','fix_round':1,'report_path':str(report),
               'report_sha256':util.sha256_file(report),'review_key':'specific-review'}
        conn = db.connect(self.db_path)
        db.update_request(conn,result['request_id'],util.now_iso(),status='succeeded')
        task = db.require_task(conn,task_id)
        db.set_extra(conn,task,util.now_iso(),fix_request=fix)
        core.update_automation(conn,task,util.now_iso(),fix_round=1,plan_wanted=True)
        conn.close()
        return task_id,item,report,fix

    def test_fix_report_copies_specific_content_and_reprepares(self):
        from pathlib import Path
        task_id,item,report,fix = self.open_fix()
        conn = db.connect(self.db_path)
        roots=[]
        for _ in range(2):
            prepared = self.call('prepare-plan-input',{'task_id':task_id})
            manifest = db.planner_input_manifest(db.get_planner_input(conn,prepared['input_id']))
            entries = [f for f in manifest['files'] if f['source']=='review_report']
            self.assertEqual(len(entries),1)
            self.assertEqual(entries[0]['sha256'],fix['report_sha256'])
            copy = Path(prepared['input_dir']) / entries[0]['path']
            self.assertEqual(copy.read_bytes(),report.read_bytes())
            self.assertEqual(manifest['task']['plan_kind'],'review_fix')
            roots.append(copy)
        self.assertNotEqual(roots[0],roots[1])
        self.assertEqual(db.task_extra(db.require_task(conn,task_id))['fix_request'],fix)
        conn.close()

    def test_fix_report_rejects_hash_missing_and_outside_root_before_input_writes(self):
        task_id,item,report,fix = self.open_fix()
        conn = db.connect(self.db_path)
        original_count = conn.execute('SELECT COUNT(*) FROM planner_inputs').fetchone()[0]
        for changes in ({'report_sha256':'0'*64}, {'report_path':str(self.reports_root/'missing.md')},
                        {'report_path':str(self.root/'outside.md')}):
            with self.subTest(changes=changes):
                task = db.require_task(conn,task_id)
                db.set_extra(conn,task,util.now_iso(),fix_request=dict(fix,**changes))
                error = self.call_err('prepare-plan-input',{'task_id':task_id})
                self.assertEqual(error.code,'validation_failed')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM planner_inputs').fetchone()[0],original_count)
        conn.close()

    def test_old_or_tampered_report_material_cannot_start_fix(self):
        from pathlib import Path
        task_id,item,report,fix = self.open_fix()
        conn = db.connect(self.db_path)
        old = self.call('start-plans',{'items':[item]})['results'][0]
        self.assertEqual(old['code'],'validation_failed')
        self.assertIsNone(db.active_request_for_key(conn,'plan:'+task_id))
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        item.update(input_id=prepared['input_id'],input_sha256=prepared['input_sha256'])
        manifest = db.planner_input_manifest(db.get_planner_input(conn,prepared['input_id']))
        entry = next(f for f in manifest['files'] if f['source']=='review_report')
        (Path(prepared['input_dir'])/entry['path']).write_text('tampered',encoding='utf-8')
        tampered = self.call('start-plans',{'items':[item]})['results'][0]
        self.assertEqual(tampered['code'],'validation_failed')
        self.assertIsNone(db.active_request_for_key(conn,'plan:'+task_id))
        self.assertEqual(core.automation_of(db.require_task(conn,task_id))['plan_attempts'],1)
        conn.close()

    def takeover_fix(self, state='open', send_state='confirmed'):
        from pathlib import Path
        task_id,item,report,fix = self.open_fix()
        conn = db.connect(self.db_path)
        task = db.require_task(conn,task_id)
        fix = dict(fix,state=state,report_path=str(self.reports_root/'historical-report-unavailable.md'))
        history = [{'review_key':'specific-review','conclusion':'changes_requested'}]
        db.set_extra(conn,task,util.now_iso(),fix_request=fix,review_history=history)
        db.update_task(conn,task_id,util.now_iso(),status='needs_user',plan_kind='review_fix',
            failure_code='planner_failed',sent=1,send_state=send_state,sent_at=util.now_iso())
        conn.close()
        self.call('resolve',{'task_id':task_id,'action':'dismiss'})
        Path(item['automation']['delivery']['worktree_path']).mkdir(parents=True,exist_ok=True)
        return task_id,item,fix,history

    def exercise_takeover_publish(self, state):
        import os
        from unittest.mock import patch
        from kanpilot import planner_runner
        task_id,old_item,fix,history = self.takeover_fix(state)
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        self.assertTrue(prepared['can_start'])
        conn = db.connect(self.db_path)
        manifest = db.planner_input_manifest(db.get_planner_input(conn,prepared['input_id']))
        self.assertEqual(manifest['task']['plan_kind'],'standard')
        self.assertFalse(any(f['source']=='review_report' for f in manifest['files']))
        conn.close()
        result = self.call('start-plans',{'items':[{'input_id':prepared['input_id'],'input_sha256':prepared['input_sha256']}]})['results'][0]
        self.assertIn('request_id',result)
        conn = db.connect(self.db_path)
        task = db.require_task(conn,task_id)
        payload = db.request_payload(db.get_request(conn,result['request_id']))
        self.assertEqual(task['plan_kind'],'standard')
        self.assertEqual(task['sent'],0)
        self.assertIsNone(task['send_state'])
        self.assertIsNone(task['sent_at'])
        self.assertEqual(payload['plan_kind'],'standard')
        self.assertNotIn('fix_mode',payload)
        self.assertNotIn('review_source_task_id',payload)
        self.assertEqual(payload['delivery_mode'],'manual')
        self.assertEqual(payload['plan_cwd'],old_item['automation']['delivery']['worktree_path'])
        self.assertEqual(payload['delivery_head_sha'],old_item['automation']['delivery']['head_sha'])
        self.assertEqual(db.task_extra(task)['fix_request'],fix)
        self.assertEqual(db.task_extra(task)['review_history'],history)
        conn.close()
        with patch.dict(os.environ,{'KANPILOT_CODEX_FAKE':'ready'}):
            self.assertEqual(planner_runner.run_loop(self.db_path,None,once=True),0)
        conn = db.connect(self.db_path)
        task = db.require_task(conn,task_id)
        self.assertEqual(task['status'],'ready')
        self.assertEqual(task['plan_kind'],'standard')
        self.assertEqual(db.get_request(conn,result['request_id'])['status'],'succeeded')
        self.assertTrue(db.task_extra(task)['plan_execution_hold'])
        self.assertEqual(db.task_extra(task)['fix_request'],fix)
        conn.close()
        enqueued = self.call('enqueue',{'task_id':task_id,'confirm_plan':True,'expected_plan_sha256':task['plan_sha256']})
        self.assertEqual(enqueued['request']['kind'],'dispatch')
        conn = db.connect(self.db_path)
        self.assertEqual(db.get_request(conn,enqueued['request']['request_id'])['status'],'queued')
        conn.close()

    def test_takeover_open_fix_prepares_starts_publishes_standard(self):
        self.exercise_takeover_publish('open')

    def test_takeover_held_fix_prepares_starts_publishes_standard(self):
        self.exercise_takeover_publish('held')

    def test_takeover_unknown_send_remains_blocked(self):
        task_id,item,fix,history = self.takeover_fix(send_state='unknown')
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        conn = db.connect(self.db_path)
        before = dict(db.require_task(conn,task_id))
        result = self.call('start-plans',{'items':[{'input_id':prepared['input_id'],'input_sha256':prepared['input_sha256']}]})['results'][0]
        self.assertEqual(result['code'],'conflict')
        self.assertIn('发送结果不明',result['error'])
        self.assertEqual(dict(db.require_task(conn,task_id)),before)
        self.assertEqual(before['send_state'],'unknown')
        self.assertIsNone(db.active_request_for_key(conn,'plan:'+task_id))
        conn.close()

    def test_normal_manual_submission_keeps_existing_send_fields(self):
        task_id,item = self.prepare('normalmanualtask00001')
        manual = dict(item); manual.pop('automation')
        initial = self.call('start-plans',{'items':[manual]})['results'][0]
        conn = db.connect(self.db_path)
        db.update_request(conn,initial['request_id'],util.now_iso(),status='succeeded')
        db.update_task(conn,task_id,util.now_iso(),sent=1,send_state='confirmed',sent_at='2026-10-05T01:00:00+00:00')
        conn.close()
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        result = self.call('start-plans',{'items':[{'input_id':prepared['input_id'],'input_sha256':prepared['input_sha256']}]})['results'][0]
        self.assertIn('request_id',result)
        conn = db.connect(self.db_path)
        task = db.require_task(conn,task_id)
        self.assertEqual(task['sent'],1)
        self.assertEqual(task['send_state'],'confirmed')
        self.assertEqual(task['sent_at'],'2026-10-05T01:00:00+00:00')
        self.assertFalse(core.automation_of(task))
        conn.close()


class TestUserReplanMaterial(KanpilotTestCase):
    prepare = TestAutomationSubmission.prepare

    def dirty_replan(self):
        import os
        import subprocess
        from pathlib import Path
        task_id,item = self.prepare('userreplantask000001')
        first = self.call('start-plans',{'items':[item]})['results'][0]
        repo = self.repos_root / 'Feedback'
        env = dict(os.environ,GIT_AUTHOR_NAME='Isolated Test',GIT_AUTHOR_EMAIL='test@example.invalid',
                   GIT_COMMITTER_NAME='Isolated Test',GIT_COMMITTER_EMAIL='test@example.invalid',GIT_CONFIG_NOSYSTEM='1')
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],env=env,capture_output=True,text=True,check=True).stdout.strip()
        git('init','-b','main')
        (repo/'partial.py').write_text('original = 1\n',encoding='utf-8')
        (repo/'deleted.py').write_text('old = True\n',encoding='utf-8')
        (repo/'rename.py').write_text('rename = True\n',encoding='utf-8')
        git('add','.')
        git('-c','commit.gpgsign=false','commit','-m','fixture baseline')
        sha = git('rev-parse','HEAD')
        worktree = Path(item['automation']['delivery']['worktree_path'])
        git('worktree','add','-b','kaneo/'+task_id,str(worktree),sha)
        (worktree/'partial.py').write_text('partial_result = 42\n',encoding='utf-8')
        (worktree/'deleted.py').unlink()
        subprocess.run(['git','-C',str(worktree),'mv','rename.py','renamed.py'],check=True,capture_output=True)
        (worktree/'.env').write_text('DO_NOT_COPY_SECRET=private-value\n',encoding='utf-8')
        (worktree/'id_rsa').write_text('DO_NOT_COPY_PRIVATE_KEY\n',encoding='utf-8')
        (worktree/'client.p12').write_text('DO_NOT_COPY_CERTIFICATE\n',encoding='utf-8')
        outside = self.root/'outside-secret.txt'; outside.write_text('EXTERNAL_CONTENT_NEVER_COPY\n',encoding='utf-8')
        (worktree/'external-link.txt').symlink_to(outside)
        (worktree/'oversized.txt').write_bytes(b'x'*(20*1024*1024+1))
        conn = db.connect(self.db_path)
        db.update_request(conn,first['request_id'],util.now_iso(),status='succeeded')
        task = db.require_task(conn,task_id)
        core.update_delivery(conn,task,util.now_iso(),head_sha=sha,base_sha=sha)
        db.update_task(conn,task_id,util.now_iso(),status='needs_user',failure_code='execution_failed',failure_stage='monitor',sent=0,run_id=None)
        conn.close()
        self.fake.tasks[task_id]['status'] = 'in-progress'
        self.call('resolve',{'task_id':task_id,'action':'replan'})
        item['automation']['delivery'].update(head_sha=sha,base_sha=sha)
        return task_id,item,worktree,sha

    def test_partial_changes_material_and_real_publish_from_in_progress(self):
        import os
        from pathlib import Path
        from unittest.mock import patch
        from kanpilot import planner_runner
        task_id,item,worktree,sha = self.dirty_replan()
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        self.assertTrue(prepared['can_start'])
        conn = db.connect(self.db_path)
        manifest = db.planner_input_manifest(db.get_planner_input(conn,prepared['input_id']))
        frozen = manifest['execution_worktree']
        self.assertEqual(frozen['head_sha'],sha)
        changes = {entry['path']:entry for entry in frozen['changes']}
        self.assertEqual(changes['deleted.py']['reason'],'deleted')
        self.assertIn('renamed.py',changes)
        self.assertEqual(changes['rename.py']['reason'],'deleted')
        self.assertEqual(changes['.env']['reason'],'sensitive_file')
        self.assertEqual(changes['id_rsa']['reason'],'sensitive_file')
        self.assertEqual(changes['client.p12']['reason'],'sensitive_file')
        self.assertEqual(changes['external-link.txt']['reason'],'symlink')
        self.assertEqual(changes['oversized.txt']['reason'],'file_too_large')
        files = [entry for entry in manifest['files'] if entry['source']=='execution_worktree']
        partial = next(entry for entry in files if entry['repository_path']=='partial.py')
        self.assertEqual((Path(prepared['input_dir'])/partial['path']).read_text(encoding='utf-8'),'partial_result = 42\n')
        all_material = b''.join(path.read_bytes() for path in Path(prepared['input_dir']).rglob('*') if path.is_file())
        self.assertNotIn(b'DO_NOT_COPY_SECRET',all_material)
        self.assertNotIn(b'DO_NOT_COPY_PRIVATE_KEY',all_material)
        self.assertNotIn(b'DO_NOT_COPY_CERTIFICATE',all_material)
        self.assertNotIn(b'EXTERNAL_CONTENT_NEVER_COPY',all_material)
        item.update(input_id=prepared['input_id'],input_sha256=prepared['input_sha256'])
        result = self.call('start-plans',{'items':[item]})['results'][0]
        self.assertIn('request_id',result)
        self.assertEqual(core.automation_of(db.require_task(conn,task_id))['replan_worktree_sha256'],frozen['state_sha256'])
        conn.close()
        with patch.dict(os.environ,{'KANPILOT_CODEX_FAKE':'ready'}):
            self.assertEqual(planner_runner.run_loop(self.db_path,None,once=True),0)
        conn = db.connect(self.db_path)
        task = db.require_task(conn,task_id)
        self.assertEqual(task['status'],'ready')
        self.assertEqual(task['kaneo_status'],'in-progress')
        self.assertEqual(db.get_request(conn,result['request_id'])['status'],'succeeded')
        self.assertEqual((worktree/'partial.py').read_text(encoding='utf-8'),'partial_result = 42\n')
        conn.close()

    def test_changed_worktree_rejected_before_request_or_automation_write(self):
        task_id,item,worktree,sha = self.dirty_replan()
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        item.update(input_id=prepared['input_id'],input_sha256=prepared['input_sha256'])
        conn = db.connect(self.db_path)
        before = dict(db.require_task(conn,task_id))
        (worktree/'partial.py').write_text('changed after confirmation\n',encoding='utf-8')
        result = self.call('start-plans',{'items':[item]})['results'][0]
        self.assertEqual(result['code'],'validation_failed')
        self.assertIsNone(db.active_request_for_key(conn,'plan:'+task_id))
        self.assertEqual(dict(db.require_task(conn,task_id)),before)
        conn.close()

    def test_changed_worktree_during_preparation_rejected(self):
        from unittest.mock import patch
        from kanpilot import git_delivery
        task_id,item,worktree,sha = self.dirty_replan()
        actual = git_delivery.worktree_state_sha256
        calls = []
        def state(delivery):
            result = actual(delivery)
            calls.append(result)
            if len(calls)==1:
                (worktree/'partial.py').write_text('changed during preparation\n',encoding='utf-8')
            return result
        conn = db.connect(self.db_path)
        count = conn.execute('SELECT COUNT(*) FROM planner_inputs').fetchone()[0]
        with patch.object(git_delivery,'worktree_state_sha256',side_effect=state):
            error = self.call_err('prepare-plan-input',{'task_id':task_id})
        self.assertEqual(error.code,'conflict')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM planner_inputs').fetchone()[0],count)
        conn.close()

    def test_changed_material_copy_rejected_even_when_branch_unchanged(self):
        from pathlib import Path
        task_id,item,worktree,sha = self.dirty_replan()
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        item.update(input_id=prepared['input_id'],input_sha256=prepared['input_sha256'])
        conn = db.connect(self.db_path)
        manifest = db.planner_input_manifest(db.get_planner_input(conn,prepared['input_id']))
        partial = next(entry for entry in manifest['files'] if entry.get('repository_path')=='partial.py')
        (Path(prepared['input_dir'])/partial['path']).write_text('tampered material\n',encoding='utf-8')
        result = self.call('start-plans',{'items':[item]})['results'][0]
        self.assertEqual(result['code'],'validation_failed')
        self.assertIsNone(db.active_request_for_key(conn,'plan:'+task_id))
        conn.close()

    def test_replan_git_io_outside_write_transactions(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from kanpilot import git_delivery
        task_id,item,worktree,sha = self.dirty_replan()
        actual_state = git_delivery.worktree_state_sha256
        actual_txn = db.write_txn
        active = []
        @contextmanager
        def write_txn(conn):
            with actual_txn(conn):
                active.append(True)
                try: yield
                finally: active.pop()
        def state(delivery):
            self.assertFalse(active,'Git state hashing cannot hold a write transaction')
            return actual_state(delivery)
        with patch.object(db,'write_txn',side_effect=write_txn), patch.object(git_delivery,'worktree_state_sha256',side_effect=state):
            prepared = self.call('prepare-plan-input',{'task_id':task_id})
            item.update(input_id=prepared['input_id'],input_sha256=prepared['input_sha256'])
            result = self.call('start-plans',{'items':[item]})['results'][0]
        self.assertIn('request_id',result)

    def test_user_replan_manual_submission_updates_only_this_recovery_snapshot(self):
        import os
        from unittest.mock import patch
        from kanpilot import planner_runner
        task_id,item,worktree,sha = self.dirty_replan()
        prepared = self.call('prepare-plan-input',{'task_id':task_id})
        manual = {'input_id':prepared['input_id'],'input_sha256':prepared['input_sha256']}
        result = self.call('start-plans',{'items':[manual]})['results'][0]
        self.assertIn('request_id',result)
        conn = db.connect(self.db_path)
        request = db.get_request(conn,result['request_id'])
        self.assertEqual(request['requested_by'],'user')
        payload = db.request_payload(request)
        task = db.require_task(conn,task_id)
        self.assertEqual(task['source_sha256'],prepared['source_sha256'])
        self.assertEqual(task['requirements_sha256'],prepared['requirements_sha256'])
        self.assertEqual(core.automation_of(task)['replan_worktree_sha256'],payload['manifest']['execution_worktree']['state_sha256'])
        conn.close()
        with patch.dict(os.environ,{'KANPILOT_CODEX_FAKE':'ready'}):
            self.assertEqual(planner_runner.run_loop(self.db_path,None,once=True),0)
        conn = db.connect(self.db_path)
        self.assertEqual(db.require_task(conn,task_id)['status'],'ready')
        self.assertEqual(db.get_request(conn,result['request_id'])['status'],'succeeded')
        conn.close()
