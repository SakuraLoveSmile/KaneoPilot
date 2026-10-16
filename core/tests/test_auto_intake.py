from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from kanpilot import auto_intake, automation_policy, cli, core, db, git_delivery, planner_inputs, util

NOW = '2026-10-05T01:00:00+00:00'


@pytest.fixture
def intake(tmp_path, monkeypatch, tmp_env):
    path = tmp_path / 'intake.db'
    repo = tmp_path / 'repo'; repo.mkdir()
    conn = db.connect(path)
    db.set_setting(conn, 'project_mappings', {'version':1,'revision':1,'workspace_id':'test',
        'items':{'project1':{'project_name':'Test','repository':str(repo),'enabled':True}}})
    ctx = SimpleNamespace(conn=conn, now_iso=NOW)
    policy = automation_policy.cmd_set_project_automation(ctx, {'project_id':'project1','automation':{'enabled':True}})['automation']
    automation_policy.cmd_set_automation_global(ctx, {'max_active_tasks':5})
    monkeypatch.setattr(automation_policy, 'automation_preflight', lambda c, p: [])
    items, calls = [], []
    monkeypatch.setattr(planner_inputs, 'fetch_todos', lambda c: {'items':items})
    real_run = cli.run
    def run(command, payload, db_path):
        calls.append((command,payload))
        assert not conn.in_transaction
        if command == 'prepare-plan-input':
            task_id = payload['task_id']; input_id = 'input-'+task_id
            manifest = {'input_id':input_id,'task':{'task_id':task_id,'project_id':'project1','project_name':'Test',
                'repository':str(repo),'title':task_id,'plan_kind':'standard'},'requirements_sha256':'r'*64,'source_sha256':'s'*64,
                'files':[],'issues':[],'created_at':NOW}
            sha = planner_inputs._manifest_sha(manifest)
            db.insert_planner_input(conn,input_id,task_id,sha,'r'*64,'s'*64,manifest,NOW)
            return {'input_id':input_id,'input_sha256':sha,'can_start':True}
        return real_run(command,payload,db_path)
    monkeypatch.setattr(cli, 'run', run)
    def branch(repo_path, task_id, **kwargs):
        assert not conn.in_transaction
        return {'branch':'kaneo/'+task_id,'worktree_path':str(repo / task_id),'base_branch':'main','base_sha':'a'*40,'head_sha':'a'*40}
    monkeypatch.setattr(git_delivery, 'prepare_branch_worktree', branch)
    def candidate(n, **extra):
        item = {'task_id':f'task{n:08d}','project_id':'project1','requirements_sha256':'r'*64,'selectable':True,'number':n, **extra}
        items.append(item)
        return item
    def task(n, status='needs_plan', *, automation=None, delivery=None, extra=None, paused=False, save_state='active'):
        task_id = f'task{n:08d}'
        conn.execute('INSERT INTO tasks(task_id,project_id,project_name,repository,status,stage,requirements_sha256,created_at,updated_at,save_state,auto_advance_paused) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (task_id,'project1','Test',str(repo),status,'plan','r'*64,NOW,NOW,save_state,int(paused)))
        row = db.require_task(conn,task_id)
        if automation is not None:
            core.update_automation(conn,row,NOW,**dict({'adopted_at':NOW,'manual_takeover':False,'plan_wanted':False,'plan_attempts':0,'segment_started_at':NOW,'cost_usd':0},**automation))
        if delivery:
            core.update_delivery(conn,row,NOW,**delivery)
        if extra:
            db.set_extra(conn,row,NOW,**extra)
        return task_id
    yield SimpleNamespace(path=path,conn=conn,items=items,calls=calls,candidate=candidate,task=task,policy=policy,branch=branch)
    conn.close()


def test_intake_sorted_max_three_and_atomic_adoption(intake):
    for n in (4,1,3,2,5): intake.candidate(n)
    result = auto_intake.run_once(intake.path, now=NOW)
    assert result['errors'] == []
    assert result['projects'][0]['adopted'] == ['task00000001','task00000002','task00000003']
    assert intake.conn.execute("SELECT COUNT(*) FROM requests WHERE requested_by='automation'").fetchone()[0] == 3
    row = db.require_task(intake.conn, 'task00000001')
    assert core.automation_of(row)['plan_attempts'] == 1
    assert core.delivery_of(row)['mode'] == 'pr'


def test_capacity_waiting_merge_and_wanted_priority(intake):
    for n in range(1,5): intake.task(n,'running',automation={})
    intake.task(5,'in_review',automation={},delivery={'mode':'pr'},extra={'review_conclusion':'pass'})
    wanted = intake.task(6, automation={'plan_wanted':True})
    intake.candidate(7)
    result = auto_intake.run_once(intake.path, now=NOW)
    assert wanted in result['projects'][0]['adopted']
    assert 'task00000007' not in result['projects'][0]['adopted']
    assert [p for c,p in intake.calls if c=='prepare-plan-input'][0]['expected_requirements_sha256'] == 'r'*64


def test_excludes_saved_paused_previously_planned_and_manual(intake):
    for n in range(1,5): intake.candidate(n)
    intake.task(1, save_state='archived')
    intake.task(2, paused=True)
    intake.task(3, automation={'manual_takeover':True})
    tid = intake.task(4)
    req, _ = db.create_request(intake.conn,NOW,'plan','plan:'+tid,task_id=tid)
    db.update_request(intake.conn,req['request_id'],NOW,status='failed')
    assert auto_intake.run_once(intake.path, now=NOW)['projects'][0]['adopted'] == []


@pytest.mark.parametrize('code', ['branch_exists','delivery_base_unknown','git_transient'])
def test_branch_failures(intake, monkeypatch, code):
    intake.candidate(1)
    def fail(*args, **kwargs): raise git_delivery.GitDeliveryError(code, 'branch failed')
    monkeypatch.setattr(git_delivery,'prepare_branch_worktree',fail)
    result = auto_intake.run_once(intake.path, now=NOW)
    assert result['projects'][0]['skipped'][0]['reason'] == code
    row = db.require_task(intake.conn,'task00000001')
    assert row['status'] == ('needs_plan' if code=='git_transient' else 'needs_user')
    assert core.automation_of(row)['plan_wanted'] == (code=='git_transient')


@pytest.mark.parametrize('code', ['validation_failed','bad_request','conflict','remote_unavailable'])
def test_wanted_prepare_failures(intake, monkeypatch, code):
    tid = intake.task(1,automation={'plan_wanted':True})
    real = cli.run
    def fail(command,payload,path):
        if command=='prepare-plan-input': raise util.KanpilotError(code, '远端需求变化' if code=='conflict' else 'failed')
        return real(command,payload,path)
    monkeypatch.setattr(cli,'run',fail)
    result = auto_intake.run_once(intake.path, now=NOW)
    assert result['projects'][0]['skipped'][0]['reason'] == ('remote_conflict' if code=='conflict' else code)
    row = db.require_task(intake.conn,tid)
    assert row['status'] == ('needs_plan' if code=='remote_unavailable' else 'needs_user')
    assert core.automation_of(row)['plan_wanted'] == (code=='remote_unavailable')


def test_pause_and_singleflight(intake, monkeypatch):
    intake.candidate(1)
    db.set_setting(intake.conn,'paused_stages',['plan'])
    assert auto_intake.run_once(intake.path,now=NOW)['errors'][0]['code'] == 'paused'
    assert intake.calls == []
    db.set_setting(intake.conn,'paused_stages',[])
    db.set_setting(intake.conn,'executor_runs',{'intake':{'pid':os.getpid()}})
    before = intake.conn.total_changes
    assert auto_intake.run_once(intake.path,now=NOW)['errors'][0]['code'] == 'already_running'
    assert intake.conn.total_changes == before


def test_dry_run_reads_only(intake, monkeypatch):
    intake.candidate(1, labels=['BUG'])
    ctx = SimpleNamespace(conn=intake.conn,now_iso=NOW)
    automation_policy.cmd_set_project_automation(ctx,{'project_id':'project1','automation':{'enabled':True,'label_filter':['bug']}})
    before = list(intake.conn.iterdump())
    monkeypatch.setattr(cli,'run',lambda *args,**kwargs: pytest.fail('dry-run cannot issue a mutating command'))
    result = auto_intake.run_once(intake.path,dry_run=True,now=NOW)
    assert result['projects'][0]['candidates'] == ['task00000001']
    assert list(intake.conn.iterdump()) == before
    missing = intake.path.parent / 'missing.db'
    with pytest.raises(Exception): auto_intake.run_once(missing,dry_run=True,now=NOW)
    assert not missing.exists()


def test_label_query_limit_cache_and_inline(intake, monkeypatch):
    ctx = SimpleNamespace(conn=intake.conn,now_iso=NOW)
    automation_policy.cmd_set_project_automation(ctx,{'project_id':'project1','automation':{'enabled':True,'label_filter':['bug']}})
    for n in range(1,23): intake.candidate(n)
    queries=[]
    monkeypatch.setattr(auto_intake.remote,'get_client',lambda: object())
    monkeypatch.setattr(auto_intake.remote,'get_task_labels',lambda c,tid: queries.append(tid) or ['feature'])
    first = auto_intake.run_once(intake.path,now=NOW)
    assert len(queries) == 20
    assert sum(s['reason']=='label_query_limit' for s in first['projects'][0]['skipped']) == 2
    queries.clear()
    intake.candidate(23,labels=[{'name':'BUG'}])
    second = auto_intake.run_once(intake.path,now=NOW)
    assert len(queries) == 2
    assert second['projects'][0]['adopted'] == ['task00000023']
    cache = db.get_setting(intake.conn,'automation_intake_state')['label_checked']
    assert 'task00000023' not in cache


def test_new_deterministic_prepare_failure_imports_blocked_row(intake, monkeypatch):
    item = intake.candidate(1)
    real = cli.run
    def fail(command,payload,path):
        if command=='prepare-plan-input': raise util.KanpilotError('validation_failed','bad material')
        return real(command,payload,path)
    monkeypatch.setattr(cli,'run',fail)
    result = auto_intake.run_once(intake.path,now=NOW)
    assert result['projects'][0]['skipped'][0]['reason'] == 'validation_failed'
    task = db.require_task(intake.conn,item['task_id'])
    assert task['status'] == 'needs_user'
    assert not core.automation_of(task)['plan_wanted']
    assert core.automation_of(task)['last_skip']['reason'] == 'validation_failed'


def test_failed_branch_preparation_preserves_manual_delivery(intake, monkeypatch):
    task_id = intake.task(1, automation={'plan_wanted':True}, delivery={'mode':'manual'})
    def fail(*a, **kw):
        raise git_delivery.GitDeliveryError('branch_exists', 'unsafe branch')
    monkeypatch.setattr(git_delivery, 'prepare_branch_worktree', fail)
    result = auto_intake._submit(intake.conn, intake.path, task_id, intake.policy, NOW, 'r'*64, wanted=True)
    assert result['reason'] == 'branch_exists'
    assert core.delivery_of(db.require_task(intake.conn, task_id)) == {'mode':'manual'}


def test_adopted_branch_flag_is_transient_and_event_recorded_once(intake, monkeypatch):
    original = intake.branch
    monkeypatch.setattr(git_delivery, 'prepare_branch_worktree', lambda *a, **kw: dict(original(*a, **kw), adopted=True))
    intake.candidate(1)
    auto_intake.run_once(intake.path, now=NOW)
    task_id = 'task00000001'
    assert 'adopted' not in core.delivery_of(db.require_task(intake.conn, task_id))
    assert intake.conn.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind='branch_worktree_adopted'", (task_id,)).fetchone()[0] == 1
    auto_intake.run_once(intake.path, now=NOW)
    assert intake.conn.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind='branch_worktree_adopted'", (task_id,)).fetchone()[0] == 1
