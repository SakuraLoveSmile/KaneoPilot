"""v5 lifecycle tests: observable transitions, guards, and recovery boundaries."""
from datetime import timedelta
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from kanpilot import (automation_policy, cli, cmd_config, cmd_exec, cmd_queue,
                      cmd_read, cmd_remote, constants, core, db, derive, git_delivery,
                      remote, review_repair, round_store, util)

NOW = '2026-10-05T05:00:00+00:00'
TASK = 'automationtask00000000001'


def test_background_recovers_goal_outside_write_transaction(state, monkeypatch):
    from kanpilot import cmd_dispatch
    state.update(status='needs_user', failure_code='execution_failed')
    calls = []
    def observe(ctx, payload):
        assert not ctx.conn.in_transaction
        calls.append(payload)
        return {'status': 'deferred', 'detail': 'expired'}
    monkeypatch.setattr(cmd_dispatch, 'cmd_round_observe', observe)
    result = cmd_read.cmd_recover_execution_goals(state.ctx, {})
    assert calls == [{'task_id': TASK, 'run_id': 'execution1', 'executor': 'scheduler',
                      'continue_goal': True, 'continue_goal_only': True}]
    assert result['goal_recoveries'] == []
    assert state.task()['status'] == 'needs_user'


def test_tick_never_waits_for_plus_goal_recovery(state, monkeypatch):
    from kanpilot import cmd_dispatch
    state.update(status='needs_user', failure_code='execution_failed')
    monkeypatch.setattr(cmd_dispatch, 'cmd_round_observe', lambda *a: pytest.fail('tick must stay local'))
    assert cmd_read.cmd_tick(state.ctx, {})['created'] == []


@pytest.mark.parametrize('blocked', ['global', 'monitor', 'dispatch', 'task', 'manual', 'other_failure'])
def test_background_does_not_recover_stopped_or_unrelated_task(state, monkeypatch, blocked):
    from kanpilot import cmd_dispatch
    state.update(status='needs_user', failure_code='execution_failed')
    if blocked == 'global':
        db.set_setting(state.conn, 'paused_global', True)
    elif blocked in ('monitor', 'dispatch'):
        db.set_setting(state.conn, 'paused_stages', [blocked])
    elif blocked == 'task':
        state.update(auto_advance_paused=1)
    elif blocked == 'manual':
        core.update_automation(state.conn, state.task(), NOW, manual_takeover=True)
    else:
        state.update(failure_code='verification_unconfirmed')
    monkeypatch.setattr(cmd_dispatch, 'cmd_round_observe', lambda *a: pytest.fail('unexpected recovery'))
    cmd_read.cmd_recover_execution_goals(state.ctx, {})


@pytest.fixture
def state(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / 'state.db')
    monkeypatch.setenv('KANPILOT_REPORTS_ROOT', str(tmp_path / 'reports'))
    (tmp_path / 'reports').mkdir()
    conn.execute('INSERT INTO tasks(task_id,project_id,project_name,repository,status,stage,requirements_sha256,plan_sha256,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 (TASK, 'project1', 'Test', str(tmp_path), 'ready', 'dispatch', 'requirements', 'plan', 'execution1', NOW, NOW))
    task = db.require_task(conn, TASK)
    core.update_automation(conn, task, NOW, adopted_at=NOW, manual_takeover=False, fix_round=0, plan_attempts=1, segment_started_at=NOW, cost_usd=0)
    core.update_delivery(conn, task, NOW, mode='pr', branch='kaneo/fixture', base_branch='main',
                         base_sha='base1', head_sha='head1', worktree_path=str(tmp_path / 'worktree'),
                         commits=[{'execution_run_id': 'execution1', 'sha': 'head1'}], next_sync_at=None)
    verify_branch_worktree = git_delivery.verify_branch_worktree
    monkeypatch.setattr(git_delivery, 'verify_branch_worktree', lambda *a, **k: None)
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
        if kind == 'review' and core.requires_branch_review(task()):
            db.merge_request_payload(conn, r['request_id'], NOW, review_branch_snapshot=core.branch_review_snapshot(task()))
        return {'task_id': TASK, 'request_id': r['request_id'], 'run_id': run, 'expected_execution_run_id': task()['run_id']}
    def review(conclusion='changes_requested', key='key1'):
        update(status='in_review', stage='review', sent=1, send_state='sent', sent_at=NOW, lock_retained=1)
        ident = claim()
        report = tmp_path / 'reports' / 'review.md'
        report.write_text('# Review\n')
        payload = dict(ident, report_path=str(report), conclusion=conclusion, review_key=key)
        if conclusion == 'changes_requested': payload.update(fix_mode='same_task', fix={'title':'Fix', 'description':'Repair this issue', 'priority':'high'})
        return payload
    def direct_repair():
        # These lifecycle cases exercise the real repair gates, including Git,
        # frozen files and persisted terminal evidence, without running a model.
        def git(*args):
            return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Isolated Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (tmp_path / 'feature.py').write_text('feature = True\n')
        git('add', 'feature.py')
        git('commit', '-q', '-m', 'isolated feature')
        head = git('rev-parse', 'HEAD')
        worktree = tmp_path / 'worktree'
        git('worktree', 'add', '-b', 'kaneo/fixture', str(worktree), 'HEAD')
        monkeypatch.setattr(git_delivery, 'verify_branch_worktree', verify_branch_worktree)
        plan = tmp_path / 'frozen-plan.md'
        plan.write_text('# Frozen plan\nPreserve the feature and its verification.\n')
        update(plan_path=str(plan), plan_sha256=util.sha256_file(plan))
        core.update_delivery(conn, task(), NOW, head_sha=head, base_sha=head,
                             commits=[{'execution_run_id': 'execution1', 'sha': head}])
        runs = tmp_path / 'session-runs'
        directory = runs / TASK / 'execution1'
        directory.mkdir(parents=True)
        (directory / 'job.json').write_text(util.jdump(dict(task_id=TASK, run_id='execution1',
            backend='pi', status='finished', exit_code=0, pid=None)))
        (directory / 'events.jsonl').write_text(util.jdump(dict(seq=1, type='terminal', outcome='completed', ts=NOW)) + '\n')
        db.set_setting(conn, 'session_runs_root', str(runs))
        round_store.upsert_on_claim(conn, TASK, 'execution1', {'backend': 'pi'}, NOW, backend='pi')
        conn.execute("UPDATE execution_rounds SET run_status='finished', worktree_path=? WHERE task_id=? AND run_id='execution1'", (str(worktree), TASK))
        return directory
    yield SimpleNamespace(ctx=ctx, conn=conn, task=task, update=update, extra=extra, policy=policy, claim=claim, review=review, live=live, direct_repair=direct_repair)
    conn.close()


def test_fix_round_waits_for_report_then_resets_send_without_replanning(state):
    state.direct_repair()
    payload = state.review()
    frozen_plan = state.task()['plan_sha256']
    cmd_exec.cmd_register_review(state.ctx, payload)
    t = state.task()
    assert t['status'] == 'in_review' and t['run_id'] == 'execution1'
    assert t['sent'] == 1 and t['send_state'] == 'sent' and t['lock_retained'] == 1
    assert core.automation_of(t)['fix_round'] == 0
    assert core.review_repair_of(t)['state'] == 'held'
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()
    identity = dict(task_id=TASK, **{k: core.review_repair_of(t)[v] for k, v in review_repair.IDENTITY.items()})
    with pytest.raises(util.KanpilotError, match='当前评审或其他请求尚未结束'):
        review_repair.cmd_start_review_repair(state.ctx, identity)
    result = cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    assert result['task_status'] == 'ready'
    t = state.task()
    assert core.automation_of(t)['fix_round'] == 1
    assert core.automation_of(t)['plan_wanted'] is False
    assert all(t[k] is None for k in ('send_state', 'sent_at', 'review_report_path', 'review_key'))
    assert t['sent'] == 0 and t['lock_retained'] == 0
    assert db.task_extra(t)['review_history'][0]['execution_run_id'] == 'execution1'
    assert core.review_repair_of(t)['state'] == 'queued'
    assert t['run_id'] == 'execution1' and t['plan_sha256'] == frozen_plan
    assert state.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    requests = state.conn.execute('SELECT kind,status FROM requests ORDER BY rowid').fetchall()
    assert [tuple(r) for r in requests] == [('review','succeeded'), ('dispatch','queued')]
    assert review_repair.cmd_start_review_repair(state.ctx, identity)['already_started']
    assert core.automation_of(state.task())['fix_round'] == 1


@pytest.mark.parametrize('policy,automation,paused,expected', [
    ({'max_fix_rounds':0}, {}, False, '本任务修复次数已用尽'),
    ({'max_task_cost_usd':1}, {'cost_usd':1}, False, '本任务时间或费用额度已用尽'),
    ({'enabled':False}, {}, False, '项目自动处理已关闭'),
    ({'max_task_minutes':30}, {'segment_started_at':'2026-10-05T04:00:00+00:00'}, False, '本任务时间或费用额度已用尽'),
    ({}, {}, True, '任务或执行阶段已暂停'),
])
def test_review_limit_holds_direct_fix_after_successful_settlement(state, policy, automation, paused, expected):
    state.direct_repair()
    state.policy(**policy)
    core.update_automation(state.conn, state.task(), NOW, **automation)
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    if paused:
        db.set_setting(state.conn, 'paused_stages', ['dispatch'])
    result = cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    assert result['request_status'] == 'succeeded'
    assert state.task()['status'] == 'in_review' and state.task()['run_id'] == 'execution1'
    fix = core.review_repair_of(state.task())
    assert fix['state'] == 'held' and fix['reason'] == expected
    assert core.automation_of(state.task())['fix_round'] == 0
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()
    identity = dict(task_id=TASK, **{k: fix[v] for k, v in review_repair.IDENTITY.items()})
    with pytest.raises(util.KanpilotError, match=expected):
        review_repair.cmd_start_review_repair(state.ctx, identity)


@pytest.mark.parametrize('outcome', ['failed', 'needs_user'])
def test_unsuccessful_review_settlement_never_starts_direct_fix(state, outcome):
    state.direct_repair()
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome=outcome))
    fix = core.review_repair_of(state.task())
    assert fix['state'] == 'held' and state.task()['run_id'] == 'execution1'
    assert core.automation_of(state.task())['fix_round'] == 0
    identity = dict(task_id=TASK, **{k: fix[v] for k, v in review_repair.IDENTITY.items()})
    with pytest.raises(util.KanpilotError, match='原评审请求尚未成功结算'):
        review_repair.cmd_start_review_repair(state.ctx, identity)
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()


def test_successful_review_report_cannot_replace_trusted_source_terminal(state):
    directory = state.direct_repair()
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    job_path = directory / 'job.json'
    job = util.jload(job_path.read_text())
    job.update(status='running', exit_code=None)
    job_path.write_text(util.jdump(job))
    result = cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    assert result['request_status'] == 'succeeded' and result['task_status'] == 'in_review'
    fix = core.review_repair_of(state.task())
    assert fix['state'] == 'held' and '终态' in fix['reason']
    assert core.automation_of(state.task())['fix_round'] == 0
    assert state.task()['run_id'] == 'execution1'
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()


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


def test_resume_cannot_bypass_direct_fix_budget_or_round_limit(state):
    state.direct_repair()
    state.policy(max_fix_rounds=0)
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    core.update_automation(state.conn,state.task(),NOW,cost_usd=50)
    state.update(status='needs_user', failure_code='fix_rounds_exhausted', failure_stage='review')
    before = dict(state.task())
    with pytest.raises(util.KanpilotError, match='不能跳过修复次数限制'):
        cmd_queue.cmd_resolve(state.ctx, {'task_id':TASK, 'action':'resume'})
    assert dict(state.task()) == before
    assert core.automation_of(state.task())['fix_round'] == 0
    assert 'segment_cost_base_usd' not in core.automation_of(state.task())
    assert core.review_repair_of(state.task())['state'] == 'held'
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()


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
    state.direct_repair()
    replan = dict(replan_requested_at=NOW, replan_remote_status='in-progress',
                  replan_worktree_sha256='source-worktree-sha')
    core.update_automation(state.conn,state.task(),NOW,**replan)
    assert 'in-progress' in core.expected_remote_slugs(state.task())
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    assert core.is_user_replan(state.task()), 'registration must not reset the active source execution'
    assert {key: core.automation_of(state.task())[key] for key in replan} == replan
    cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    assert core.review_repair_of(state.task())['state'] == 'queued'
    assert not core.is_user_replan(state.task())
    assert all(core.automation_of(state.task()).get(key) is None for key in replan)
    assert core.expected_remote_slugs(state.task()) == set(constants.REVIEW_SLUGS) | {constants.IN_PROGRESS_SLUG}


@pytest.mark.parametrize('exhausted', [False, True])
def test_detached_review_holds_fix_before_quota_without_advancing_round(state, exhausted):
    state.direct_repair()
    state.extra(delivery={'mode':'manual'})
    if exhausted:
        state.policy(max_fix_rounds=0)
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    assert core.review_repair_of(state.task())['state'] == 'held'
    result = cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    assert result['task_status'] == 'in_review'
    fix = core.review_repair_of(state.task())
    assert fix['state'] == 'held' and fix['reason'] == core.FIX_BRANCH_UNAVAILABLE_DETAIL
    assert state.task()['run_id'] == 'execution1'
    assert core.automation_of(state.task())['fix_round'] == 0
    assert not review_repair.view(state.conn, state.task(), state.ctx.now)['can_start']
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()


def test_registered_review_binding_mismatch_holds_direct_fix(state):
    state.direct_repair()
    payload = state.review()
    cmd_exec.cmd_register_review(state.ctx, payload)
    frozen = core.review_repair_of(state.task())
    core.update_delivery(state.conn, state.task(), NOW, worktree_path='/different/worktree')
    cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    fix = core.review_repair_of(state.task())
    assert fix['state'] == 'held' and fix['reason'] == '修复分支身份已变化'
    assert fix['worktree_path'] == frozen['worktree_path']
    assert core.automation_of(state.task())['fix_round'] == 0
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind IN ('dispatch','plan')").fetchone()


def test_historical_verdict_binding_does_not_block_current_review_branch(state):
    state.direct_repair()
    payload = state.review()
    state.extra(engine_verdict={'execution_run_id':'old-run', 'plan_sha256':'old-plan',
                                'binding':{'branch':'kaneo/old','worktree_path':'/old/worktree'}})
    cmd_exec.cmd_register_review(state.ctx, payload)
    result = cmd_exec.cmd_report(state.ctx, dict(request_id=payload['request_id'], run_id=payload['run_id'], outcome='succeeded'))
    assert result['task_status'] == 'ready'
    assert core.automation_of(state.task())['fix_round'] == 1
    assert core.review_repair_of(state.task())['state'] == 'queued'
    assert not state.conn.execute("SELECT 1 FROM requests WHERE kind='plan'").fetchone()
