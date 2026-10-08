"""v5 lifecycle tests: observable transitions, guards, and recovery boundaries."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanpilot import (automation_policy, cli, cmd_config, cmd_exec, cmd_queue,
                      cmd_read, cmd_remote, constants, core, db, derive, remote, util)

NOW = '2026-10-05T05:00:00+00:00'
TASK = 'automationtask00000000001'


@pytest.fixture
def state(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / 'state.db')
    monkeypatch.setenv('KANPILOT_REPORTS_ROOT', str(tmp_path / 'reports'))
    (tmp_path / 'reports').mkdir()
    conn.execute('INSERT INTO tasks(task_id,project_id,project_name,repository,status,stage,requirements_sha256,plan_sha256,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 (TASK, 'project1', 'Test', str(tmp_path), 'ready', 'dispatch', 'requirements', 'plan', 'execution1', NOW, NOW))
    task = db.require_task(conn, TASK)
    core.update_automation(conn, task, NOW, adopted_at=NOW, manual_takeover=False, fix_round=0, plan_attempts=1, segment_started_at=NOW, cost_usd=0)
    core.update_delivery(conn, task, NOW, mode='pr', head_sha='head1', commits=[], next_sync_at=None)
    db.set_setting(conn, 'project_mappings', {'version': 1, 'revision': 1, 'items': {'project1': {'repository': str(tmp_path), 'enabled': True, 'automation': dict(constants.AUTOMATION_DEFAULTS, enabled=True)}}})
    monkeypatch.setattr(automation_policy, 'automation_preflight', lambda c, p: [] if not c.in_transaction else pytest.fail('preflight in write txn'))
    monkeypatch.setattr(remote, 'get_client', lambda: object())
    live = {'task': {'id': TASK, 'projectId': 'project1', 'status': 'in-review'}, 'requirements_sha256': 'requirements'}
    monkeypatch.setattr(remote, 'get_task', lambda *a: live if not conn.in_transaction else pytest.fail('remote in write txn'))
    ctx = SimpleNamespace(conn=conn, now=util.parse_ts(NOW), now_iso=NOW, db_path=tmp_path / 'state.db')
    def task(): return db.require_task(conn, TASK)
    def update(**fields): db.update_task(conn, TASK, NOW, **fields)
    def extra(**fields): db.set_extra(conn, task(), NOW, **fields)
    def policy(**fields):
        v = db.get_setting(conn, 'project_mappings')
        v['items']['project1']['automation'].update(fields)
        db.set_setting(conn, 'project_mappings', v)
    def claim(kind='review', run='review1'):
        r, _ = db.create_request(conn, NOW, kind, kind + ':' + TASK, task_id=TASK)
        db.update_request(conn, r['request_id'], NOW, status='claimed', run_id=run, lease_expires_at=util.plus_seconds(600, ctx.now), claimed_by='test')
        return {'task_id': TASK, 'request_id': r['request_id'], 'run_id': run, 'expected_execution_run_id': task()['run_id']}
    def review(conclusion='changes_requested', key='key1'):
        update(status='in_review', stage='review', sent=1, send_state='sent', sent_at=NOW, lock_retained=1)
        ident = claim()
        report = tmp_path / 'reports' / 'review.md'
        report.write_text('# Review\n')
        payload = dict(ident, report_path=str(report), conclusion=conclusion, review_key=key)
        if conclusion == 'changes_requested': payload.update(fix_mode='same_task', fix={'title':'Fix', 'description':'Repair this issue', 'priority':'high'})
        return payload
    yield SimpleNamespace(ctx=ctx, conn=conn, task=task, update=update, extra=extra, policy=policy, claim=claim, review=review, live=live)
    conn.close()


def test_fix_round_resets_send_and_preserves_history(state):
    result = cmd_exec.cmd_register_review(state.ctx, state.review())
    assert result['task_status'] == 'needs_plan'
    t = state.task()
    assert core.automation_of(t)['fix_round'] == 1
    assert core.automation_of(t)['plan_wanted'] is True
    assert all(t[k] is None for k in ('send_state', 'sent_at', 'review_report_path', 'review_key'))
    assert t['sent'] == 0 and t['lock_retained'] == 0
    assert db.task_extra(t)['review_history'][0]['execution_run_id'] == 'execution1'
    assert db.task_extra(t)['fix_request']['state'] == 'open'


@pytest.mark.parametrize('policy,automation,expected', [
    ({'max_fix_rounds':0}, {}, 'fix_rounds_exhausted'),
    ({'max_task_cost_usd':1}, {'cost_usd':1}, 'auto_budget_exceeded'),
    ({'enabled':False}, {}, 'automation_stopped'),
    ({'max_task_minutes':30}, {'segment_started_at':'2026-10-05T04:00:00+00:00'}, 'auto_budget_exceeded'),
])
def test_review_limit_holds_same_task_fix(state, policy, automation, expected):
    state.policy(**policy)
    core.update_automation(state.conn, state.task(), NOW, **automation)
    cmd_exec.cmd_register_review(state.ctx, state.review())
    assert state.task()['failure_code'] == expected
    assert state.task()['status'] == 'needs_user'
    assert db.task_extra(state.task())['fix_request']['state'] == 'held'
    assert core.automation_of(state.task())['fix_round'] == 0


def test_review_same_key_is_idempotent_and_stale_cycle_rejected(state):
    p = state.review()
    cmd_exec.cmd_register_review(state.ctx, p)
    assert cmd_exec.cmd_register_review(state.ctx, p)['already_registered']
    assert len(db.task_extra(state.task())['review_history']) == 1
    state.update(run_id='execution2')
    with pytest.raises(util.KanpilotError) as err: cmd_exec.cmd_register_review(state.ctx, p)
    assert err.value.code == 'stale_run'


def test_automatic_review_refuses_cross_task_path(state):
    p = state.review()
    p.pop('fix_mode'); p.pop('fix'); p['fix_task_id'] = 'other'
    with pytest.raises(util.KanpilotError) as err: cmd_exec.cmd_register_review(state.ctx, p)
    assert err.value.code == 'conflict'


@pytest.mark.parametrize('target', ['in-review', 'done'])
def test_passive_remote_transition_does_not_write_remote(state, monkeypatch, target):
    state.update(status='running', stage='monitor')
    state.live['task']['status'] = target
    monkeypatch.setattr(remote, 'update_status', lambda *a: pytest.fail('passive pair must not write remote'))
    result = cmd_remote.cmd_advance_task(state.ctx, {'task_id':TASK, 'target':target, 'expected_current':target})
    assert result['task_status'] == ('done' if target == 'done' else 'in_review')


def test_unmerged_pr_refuses_done_before_remote_io(state, monkeypatch):
    state.update(status='in_review', stage='review')
    monkeypatch.setattr(remote, 'get_task', lambda *a: pytest.fail('unmerged must reject before IO'))
    with pytest.raises(util.KanpilotError) as err:
        cmd_remote.cmd_advance_task(state.ctx, {'task_id':TASK, 'target':'done', 'expected_current':'in-review'})
    assert err.value.code == 'conflict'


def test_delivery_identity_accepts_merged_cycle_and_rejects_wrong_stage(state, monkeypatch):
    state.update(status='in_review', stage='review')
    core.update_delivery(state.conn, state.task(), NOW, merged_at=NOW)
    p = dict(state.claim('delivery', 'delivery1'), target='done', expected_current='in-review')
    def write(client, tid, pid, target, expected):
        assert not state.conn.in_transaction
        state.live['task']['status'] = target
    monkeypatch.setattr(remote,'update_status',write)
    assert cmd_remote.cmd_advance_task(state.ctx,p)['task_status'] == 'done'
    with pytest.raises(util.KanpilotError):
        core.require_stage_identity(state.conn,p,state.task(),state.ctx.now,stages=('review',))


@pytest.mark.parametrize('reason', ['disabled','auto_confirm_off','preflight','verify_missing','verify_mismatch','budget','paused'])
def test_auto_confirm_all_skip_reasons_once(state, monkeypatch, reason):
    state.extra(plan_execution_hold=True, plan_verify={'plan_sha256':'plan','commands':['python -m unittest']})
    if reason == 'disabled': state.policy(enabled=False)
    elif reason == 'auto_confirm_off': state.policy(auto_confirm_plan=False)
    elif reason == 'preflight': monkeypatch.setattr(automation_policy,'automation_preflight',lambda *a:[{'code':'bad','message':'not ready'}])
    elif reason == 'verify_missing': state.extra(plan_verify={'plan_sha256':'plan','error':'missing'})
    elif reason == 'verify_mismatch': state.extra(plan_verify={'plan_sha256':'other','commands':['test']})
    elif reason == 'budget': core.update_automation(state.conn,state.task(),NOW,cost_usd=10)
    elif reason == 'paused': db.set_setting(state.conn,'paused_global',True)
    for _ in range(2): cmd_read.cmd_tick(state.ctx,{})
    assert state.task()['status'] == 'ready'
    assert core.automation_of(state.task())['last_skip']['reason'] == reason
    events=state.conn.execute("SELECT * FROM events WHERE kind='auto_confirm_skipped'").fetchall()
    assert len(events) == 1
    assert state.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='dispatch'").fetchone()[0] == 0


def test_auto_confirm_and_delivery_tick_cycle(state):
    plan = Path(state.task()['repository']) / 'current-plan.md'
    plan.write_text('# Current authorized plan\n')
    plan_sha = util.sha256_file(plan)
    state.update(plan_path=str(plan), plan_sha256=plan_sha)
    state.extra(plan_execution_hold=True,plan_verify={'plan_sha256':plan_sha,'commands':['test']})
    assert cmd_read.cmd_tick(state.ctx,{})['created'][0]['kind']=='dispatch'
    assert not db.task_extra(state.task()).get('plan_execution_hold')
    state.update(status='in_review',stage='review')
    state.extra(review_conclusion='pass',review_history=[{'execution_run_id':'execution1'}])
    cmd_read.cmd_tick(state.ctx,{})
    assert state.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='delivery'").fetchone()[0] == 1
    cmd_read.cmd_tick(state.ctx,{})
    assert state.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='delivery'").fetchone()[0] == 1


@pytest.mark.parametrize('change', ['takeover','saved','paused','stale_review','backoff'])
def test_delivery_tick_skips_ineligible(state,change):
    state.update(status='in_review',stage='review')
    state.extra(review_conclusion='pass',review_history=[{'execution_run_id':'execution1'}])
    if change=='takeover': core.update_automation(state.conn,state.task(),NOW,manual_takeover=True)
    elif change=='saved': state.update(save_state='archived')
    elif change=='paused': state.update(auto_advance_paused=1)
    elif change=='stale_review': state.extra(review_history=[{'execution_run_id':'other'}])
    elif change=='backoff': core.update_delivery(state.conn,state.task(),NOW,next_sync_at='2026-10-06T00:00:00+00:00')
    cmd_read.cmd_tick(state.ctx,{})
    assert state.conn.execute("SELECT COUNT(*) FROM requests WHERE kind='delivery'").fetchone()[0]==0


@pytest.mark.parametrize('code,target,kind', [(c,t,k) for c,(t,k) in cmd_queue._AUTO_RESUME_TARGETS.items()])
def test_resume_restores_correct_step(state, code,target,kind):
    state.update(status='needs_user',failure_code=code,failure_stage=kind or 'plan')
    if code=='review_backend_unsupported': db.set_setting(state.conn,'review_config',{'backend':'codex'})
    assert 'resume' in derive.allowed_actions(state.conn,state.task(),state.ctx.now)
    assert cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'resume'})['task_status']==target
    if kind:
        assert state.conn.execute("SELECT COUNT(*) FROM requests WHERE kind=?",(kind,)).fetchone()[0]==1
    else: assert core.automation_of(state.task())['plan_wanted']


def test_accept_verification_requires_correct_reason_and_round(state):
    state.update(status='needs_user',failure_code='verification_unconfirmed',failure_stage='monitor')
    cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'accept_verification','note':'checked'})
    assert state.task()['status']=='running'
    assert db.task_extra(state.task())['verification_override']['execution_run_id']=='execution1'
    state.update(status='needs_user',failure_code='execution_failed')
    with pytest.raises(util.KanpilotError): cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'accept_verification'})


def test_resume_fix_opens_new_segment_with_explicit_extra_round(state):
    state.policy(max_fix_rounds=0)
    cmd_exec.cmd_register_review(state.ctx,state.review())
    core.update_automation(state.conn,state.task(),NOW,cost_usd=50)
    cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'resume'})
    a=core.automation_of(state.task())
    assert a['fix_round']==1 and a['segment_cost_base_usd']==50
    assert db.task_extra(state.task())['fix_request']['state']=='open'


def test_remote_resume_rejects_wrong_status_then_accepts_current_fingerprint(state):
    state.update(status='needs_user',failure_code='remote_conflict',failure_stage='monitor')
    state.live['task']['status']='to-do'
    with pytest.raises(util.KanpilotError): cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'resume'})
    state.live['task']['status']='in-progress';state.live['requirements_sha256']='updated'
    assert cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'resume'})['task_status']=='running'
    assert state.task()['requirements_sha256']=='updated'


def test_dismiss_permanently_exits_auto_and_preserves_branch(state):
    state.update(status='needs_user',failure_code='review_blocked')
    core.update_delivery(state.conn,state.task(),NOW,branch='kaneo/'+TASK)
    cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'dismiss'})
    assert not core.is_automation_managed(state.task())
    assert core.delivery_of(state.task())['mode']=='manual'
    assert core.delivery_of(state.task())['branch']=='kaneo/'+TASK
    state.extra(plan_execution_hold=True,plan_verify={'plan_sha256':'plan','commands':['test']})
    cmd_read.cmd_tick(state.ctx,{})
    assert db.task_extra(state.task())['plan_execution_hold']


@pytest.mark.parametrize('kind', ['monitor','delivery','plan','dispatch','review'])
def test_tick_only_releases_lost_monitor_delivery(state,kind):
    state.update(status='running' if kind=='monitor' else 'in_review')
    ident=state.claim(kind)
    db.update_request(state.conn,ident['request_id'],NOW,lease_expires_at='2026-10-04T00:00:00+00:00')
    cmd_read.cmd_tick(state.ctx,{})
    assert db.get_request(state.conn,ident['request_id'])['status']==('cancelled' if kind in ('monitor','delivery') else 'claimed')


@pytest.mark.parametrize('attempts,status,wanted', [(1,'needs_plan',True),(2,'needs_user',False)])
def test_plan_failure_retries_only_once(state,attempts,status,wanted):
    state.update(status='planning',stage='plan')
    core.update_automation(state.conn,state.task(),NOW,plan_attempts=attempts,plan_wanted=False)
    ident=state.claim('plan','plan1')
    cmd_exec.cmd_report(state.ctx,{**ident,'outcome':'failed','note':'failed'})
    assert state.task()['status']==status
    assert core.automation_of(state.task()).get('plan_wanted',False)==wanted


def test_claimable_respects_pause_save_hold_authorization_and_not_before(state):
    for n,kind in enumerate(('plan','dispatch','monitor','delivery')):
        r,_=db.create_request(state.conn,NOW,kind,kind+':'+TASK,task_id=TASK)
    state.extra(plan_execution_hold=True)
    result=cmd_config.cmd_executor_status(state.ctx,{})
    assert result['stages']['plan']['claimable']==0
    assert result['stages']['dispatch']['claimable']==0
    assert result['stages']['monitor']['claimable']==1
    state.update(auto_advance_paused=1)
    assert all(v['claimable']==0 for v in cmd_config.cmd_executor_status(state.ctx,{})['stages'].values())


def test_cli_new_commands_are_registered(state):
    for cmd in ('delivery-commit','delivery-publish','delivery-sync','set-project-automation','set-automation-global','record-cost'):
        assert cmd in cli.COMMANDS


def test_resume_review_clears_current_report_but_keeps_history(state):
    state.update(status='needs_user',failure_code='review_blocked',review_report_path='/old/report.md',review_report_sha256='old',review_key='old-key')
    state.extra(review_conclusion='blocked',review_registered_at=NOW,review_history=[{'review_key':'old-key'}])
    cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'resume'})
    assert state.task()['review_report_path'] is None
    assert 'review_conclusion' not in db.task_extra(state.task())
    assert db.task_extra(state.task())['review_history']==[{'review_key':'old-key'}]


def test_tick_recovers_done_advance_after_merged_checkpoint(state):
    state.update(status='in_review',stage='review')
    state.extra(review_conclusion='pass',review_history=[{'execution_run_id':'execution1'}])
    core.update_delivery(state.conn,state.task(),NOW,merged_at=NOW)
    assert any(r['kind']=='delivery' for r in cmd_read.cmd_tick(state.ctx,{})['created'])


def test_delivery_remote_conflict_preserves_resume_stage(state):
    state.update(status='in_review',stage='review')
    core.update_delivery(state.conn,state.task(),NOW,merged_at=NOW)
    p=dict(state.claim('delivery','delivery1'),target='done',expected_current='in-review')
    state.live['task']['status']='to-do'
    with pytest.raises(util.KanpilotError): cmd_remote.cmd_advance_task(state.ctx,p)
    assert state.task()['failure_stage']=='delivery'
    state.live['task']['status']='in-review'
    assert cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'resume'})['task_status']=='in_review'


def test_sync_managed_task_only_observes_metadata(state):
    state.update(status='running',stage='monitor')
    cmd_queue.cmd_sync_tasks(state.ctx,{'executor':'sync','projects':[{'project_id':'project1','project_name':'Test','tasks':[{'task_id':TASK,'title':'new title','kaneo_status':'done','requirements_sha256':'new-fingerprint'}]}]})
    assert state.task()['title']=='new title'
    assert state.task()['kaneo_status']=='done'
    assert state.task()['status']=='running'
    assert state.task()['requirements_sha256']=='requirements'


def test_old_delivery_cannot_finish_new_execution_after_io(state,monkeypatch):
    state.update(status='in_review',stage='review')
    core.update_delivery(state.conn,state.task(),NOW,merged_at=NOW)
    p=dict(state.claim('delivery','delivery1'),target='done',expected_current='in-review')
    def changed(*args):
        state.update(run_id='new-execution')
        db.update_request(state.conn,p['request_id'],NOW,status='cancelled')
        state.live['task']['status']='done'
        return state.live
    monkeypatch.setattr(remote,'get_task',changed)
    monkeypatch.setattr(remote,'update_status',lambda *a:None)
    with pytest.raises(util.KanpilotError) as err: cmd_remote.cmd_advance_task(state.ctx,p)
    assert err.value.code in ('stale_run','remote_conflict')
    assert state.task()['status']=='in_review'
    assert not db.get_setting(state.conn,'lifecycle_serial')


@pytest.mark.parametrize('identity', ['delivery', 'monitor', 'legacy_monitor'])
@pytest.mark.parametrize('change', ['elapsed', 'expired', 'released', 'execution', 'requirements', 'project'])
def test_remote_read_invalidation_prevents_status_write(state, monkeypatch, identity, change):
    """远端 get 期间失效的 claim 必须在远端 update 前被拒绝。"""
    if identity == 'delivery':
        state.update(status='in_review', stage='delivery')
        core.update_delivery(state.conn, state.task(), NOW, merged_at=NOW)
        claim = state.claim('delivery', 'delivery1')
        payload = dict(claim, target='done', expected_current='in-review')
        original_status = 'in_review'
    else:
        state.update(status='running', stage='monitor')
        state.live['task']['status'] = 'in-progress'
        claim = state.claim('monitor', 'monitor1')
        payload = {'task_id': TASK, 'run_id': claim['run_id'],
                   'target': 'in-review', 'expected_current': 'in-progress'}
        if identity == 'legacy_monitor':
            db.update_request(state.conn, claim['request_id'], NOW, claimed_by='kaneo-monitor')
            payload.pop('run_id')
            payload.update(monitor_request_id=claim['request_id'], monitor_run_id=claim['run_id'])
        original_status = 'running'
    clock = [0.0]
    monkeypatch.setattr(cmd_remote.time, 'monotonic', lambda: clock[0])
    reads, writes = [], []

    def get(*args):
        assert not state.conn.in_transaction, 'remote get in write transaction'
        reads.append(args)
        if change == 'elapsed':
            clock[0] = 601.0
        elif change == 'expired':
            db.update_request(state.conn, claim['request_id'], NOW,
                              lease_expires_at=util.to_iso(state.ctx.now - timedelta(seconds=1)))
        elif change == 'released':
            db.update_request(state.conn, claim['request_id'], NOW, status='cancelled')
        elif change == 'execution':
            state.update(run_id='replacement-execution')
        elif change == 'requirements':
            state.update(requirements_sha256='replacement-requirements')
        else:
            state.update(project_id='replacement-project')
        return state.live

    def write(*args):
        assert not state.conn.in_transaction, 'remote update in write transaction'
        writes.append(args)

    monkeypatch.setattr(remote, 'get_task', get)
    monkeypatch.setattr(remote, 'update_status', write)
    with pytest.raises(util.KanpilotError) as err:
        cmd_remote.cmd_advance_task(state.ctx, payload)
    assert err.value.code == 'stale_run'
    assert len(reads) == 1
    assert writes == []
    assert state.task()['status'] == original_status
    assert state.task()['kaneo_status'] is None
    assert state.conn.execute("SELECT COUNT(*) FROM events WHERE kind='kaneo_transition'").fetchone()[0] == 0
    assert not db.get_setting(state.conn, 'lifecycle_owner')


def test_replan_reads_new_remote_requirements_after_execution_failure(state):
    state.update(status='needs_user',failure_code='execution_failed',failure_stage='monitor')
    state.live['requirements_sha256']='new-demand'
    cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'replan'})
    assert state.task()['requirements_sha256']=='new-demand'
    assert state.task()['status']=='needs_plan'
    assert core.automation_of(state.task())['plan_wanted']


def test_archived_review_is_not_failed_by_backend_guard(state):
    state.update(status='in_review',stage='review',save_state='archived')
    r,_=db.create_request(state.conn,NOW,'review','review:'+TASK,task_id=TASK)
    result=cmd_exec.cmd_claim(state.ctx,{'stage':'review','executor':'test','request_id':r['request_id']})
    assert result['reason']=='task_archived'
    assert state.task()['status']=='in_review'
    assert db.get_request(state.conn,r['request_id'])['status']=='queued'


def test_automation_replan_cannot_erase_unknown_send(state,monkeypatch):
    state.update(status='needs_user',failure_code='dispatch_failed',failure_stage='dispatch',send_state='unknown',sent=0,lock_retained=1)
    monkeypatch.setattr(remote,'get_task',lambda *a:pytest.fail('unknown send must reject before remote IO'))
    before=dict(state.task())
    with pytest.raises(util.KanpilotError) as err:
        cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'replan'})
    assert err.value.code=='conflict'
    assert dict(state.task())==before
    assert 'replan' not in derive.allowed_actions(state.conn,state.task(),state.ctx.now)


def test_replan_rechecks_send_evidence_after_remote_io(state,monkeypatch):
    state.update(status='needs_user',failure_code='execution_failed',failure_stage='monitor',send_state='not_sent',sent=0)
    def changed(*args):
        state.update(send_state='unknown',lock_retained=1)
        return state.live
    monkeypatch.setattr(remote,'get_task',changed)
    with pytest.raises(util.KanpilotError) as err:
        cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'replan'})
    assert err.value.code=='conflict'
    assert state.task()['send_state']=='unknown'
    assert state.task()['status']=='needs_user'


def test_replan_sent_without_round_rejects_before_remote_io(state,monkeypatch):
    state.update(status='needs_user',failure_code='execution_failed',sent=1,send_state='sent')
    monkeypatch.setattr(remote,'get_task',lambda *a:pytest.fail('unindexed send must reject before IO'))
    with pytest.raises(util.KanpilotError) as err:
        cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'replan'})
    assert err.value.code=='conflict'
    assert state.task()['sent']==1


def test_replan_requires_previous_execution_terminal(state,monkeypatch):
    from kanpilot import cmd_session
    state.update(status='needs_user',failure_code='remote_conflict',failure_stage='monitor',sent=1,send_state='sent')
    state.conn.execute('INSERT INTO execution_rounds(task_id,run_id,backend,run_status,created_at,updated_at) VALUES(?,?,?,?,?,?)',(TASK,'execution1','pi_desktop','running',NOW,NOW))
    monkeypatch.setattr(cmd_session,'cmd_get_session',lambda *a:{'round_detail':{'run_status':'running','terminal':None}})
    before=dict(state.task())
    with pytest.raises(util.KanpilotError) as err: cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'replan'})
    assert err.value.code=='conflict' and '终态' in err.value.message
    assert dict(state.task())==before
    monkeypatch.setattr(cmd_session,'cmd_get_session',lambda *a:{'round_detail':{'run_status':'failed','terminal':{'outcome':'failed'}}})
    state.live['task']['status']='in-progress'
    cmd_queue.cmd_resolve(state.ctx,{'task_id':TASK,'action':'replan'})
    assert core.is_user_replan(state.task())
    assert 'in-progress' in core.expected_remote_slugs(state.task())


def test_automatic_fix_does_not_inherit_user_replan_status_authorization(state):
    core.update_automation(state.conn,state.task(),NOW,replan_requested_at=NOW,replan_remote_status='in-progress')
    assert 'in-progress' in core.expected_remote_slugs(state.task())
    cmd_exec.cmd_register_review(state.ctx,state.review())
    assert not core.is_user_replan(state.task())
    assert 'in-progress' not in core.expected_remote_slugs(state.task())
