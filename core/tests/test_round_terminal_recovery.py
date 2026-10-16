"""Terminal recovery regressions; fake Plus and isolated temporary SQLite only."""
import json
from types import SimpleNamespace

import pytest

from kanpilot import cmd_dispatch, db, round_store, util
from test_process_owner import plus_scene


def test_attach_does_not_report_running_for_settled_job(tmp_path):
    (tmp_path / 'job.json').write_text(json.dumps({
        'status': 'failed', 'detail': 'turn aborted: TURN_ABORTED',
        'observation_resumed_at': '2026-10-17T00:00:00+00:00',
    }))
    status, detail = cmd_dispatch._wait_observe_attach(
        tmp_path, SimpleNamespace(poll=lambda: 0), {})
    assert status == 'settled'
    assert detail == 'turn aborted: TURN_ABORTED'


def terminal_scene(plus_scene):
    case, owner, spec, path, fixtures = plus_scene
    job = case.write_lost_observer_scene(spec, path.parent)
    job.update(status='failed', detail='turn aborted: TURN_ABORTED', exit_code=1)
    (path.parent / 'job.json').write_text(json.dumps(job))
    with (path.parent / 'events.jsonl').open('a') as f:
        f.write(json.dumps({'type':'terminal', 'outcome':'failed',
                           'reason':'TURN_ABORTED', 'ts':util.now_iso(), 'seq':2, 'v':1})+'\n')
    case.server.turn_status = 'aborted'
    case.server.turn_error_code = 'TURN_ABORTED'
    fixtures._sync(case.server)
    return case, spec, path


def test_failed_terminal_round_does_not_spawn_again(plus_scene, monkeypatch):
    case, spec, path = terminal_scene(plus_scene)
    before = (path.parent / 'job.json').read_bytes()
    monkeypatch.setattr(cmd_dispatch.subprocess, 'Popen', lambda *a, **k: pytest.fail('terminal observer respawned'))
    for _ in range(2):
        result = case.call('round-observe', {'task_id':spec['task_id'], 'run_id':spec['run_id'], 'executor':'kaneo-monitor'})
        assert result['status'] == 'settled'
    assert (path.parent / 'job.json').read_bytes() == before
    assert case.server.prompt_count == 0
    with db.connect(case.db_path) as conn:
        assert round_store.require_round(conn, spec['task_id'], spec['run_id'])['run_status'] == 'failed'


@pytest.mark.parametrize('executor', ['scheduler', 'engine', 'kaneo-monitor'])
def test_automatic_caller_cannot_choose_a_user_continuation(plus_scene, executor):
    case, spec, _ = terminal_scene(plus_scene)
    error = case.call_err('round-observe', {'task_id':spec['task_id'], 'run_id':spec['run_id'],
                                           'executor':executor, 'recovery_turn_id':'another-turn'})
    assert error.code == 'forbidden'


def test_monitor_reports_the_settled_failure_instead_of_running(monkeypatch, tmp_path):
    from test_auto_monitor import invoke, detail, report
    value = dict(detail(), observer_alive=False, run_status='running',
                 terminal={'outcome':'failed','reason':'TURN_ABORTED'}, exit_code=1)
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=value,
                        responses={'round-observe':{'status':'settled','pid':None}})
    result = report(calls)
    assert result['outcome'] == 'needs_user'
    assert result['failure_code'] == 'execution_failed'
    assert not any(command == 'advance-task' for command, _ in calls)


def test_attach_waits_for_terminal_finalization_not_the_previous_failure(tmp_path, monkeypatch):
    job_path = tmp_path / 'job.json'
    job_path.write_text(json.dumps({'status':'failed', 'detail':'old failure',
                                   'observation_resumed_at':'new-resume'}))
    finalized = []
    def finish(_seconds):
        finalized.append(True)
        job_path.write_text(json.dumps({'status':'finished', 'detail':None,
                                       'observation_resumed_at':'new-resume'}))
    monkeypatch.setattr(cmd_dispatch._time, 'sleep', finish)
    status, detail = cmd_dispatch._wait_observe_attach(
        tmp_path, SimpleNamespace(poll=lambda: 0 if finalized else None), {})
    assert finalized, 'old failure must not be exposed while finalization is pending'
    assert (status, detail) == ('settled', None)


def test_monitor_waits_for_live_completed_finalizer(monkeypatch, tmp_path):
    from test_auto_monitor import invoke, detail, report
    value = dict(detail(), observer_alive=True, run_status='failed',
                 observation_finalizing=True,
                 terminal={'outcome':'failed','reason':'TURN_ABORTED'}, exit_code=1)
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=value)
    result = report(calls)
    assert result['outcome'] == 'succeeded'
    assert result['progress'] == '执行已完成，正在收尾'
    assert not any(command == 'advance-task' for command, _ in calls)


def test_settled_reopening_continues_to_verification(monkeypatch, tmp_path):
    from test_auto_monitor import invoke, detail, report
    value = dict(detail(), observer_alive=False)
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=value,
                        responses={'round-observe':{'status':'settled','reopened':True}})
    assert report(calls)['outcome'] == 'succeeded'
    assert any(command == 'advance-task' for command, _ in calls)


def test_finalizing_marker_survives_status_only_cache(monkeypatch):
    from kanpilot import dsh_events, cmd_session
    monkeypatch.setattr(round_store, 'round_view', lambda row: {'backend':'pi_desktop','run_status':'failed'})
    monkeypatch.setattr(util, 'pid_alive', lambda pid: True)
    marker = {'turn_id':'completed-follow-up', 'observed_status':'completed'}
    job = {'status':'failed','turn_id':'completed-follow-up','observe_pid':123,
           'observation_finalizing':marker}
    summary = cmd_session._status_summary(job, [])
    value = dsh_events.round_detail({}, summary['round_job'], [])
    assert value['observation_finalizing'] is True
    for changed in [{'turn_id':'other'}, {'observed_status':'running'}]:
        bad = dict(job, observation_finalizing=dict(marker, **changed))
        assert dsh_events.round_detail({}, bad, [])['observation_finalizing'] is False


def test_completed_observation_reopens_only_its_execution_failure(plus_scene, monkeypatch):
    from test_process_owner import write_owned_execution_scene
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.delenv('KANPILOT_EXECUTOR_INSTANCE_ID', raising=False)
    write_owned_execution_scene(case, spec, path)
    case.call('executor-instance', {'instance_id':owner, 'action':'stop'})
    case.mark_needs_user(spec['task_id'], spec['run_id'])
    case.server.turn_status = 'completed'
    case.server.messages = case.default_messages()
    fixtures._sync(case.server)
    with case.env(KANPILOT_PI_DESKTOP_FAKE=None):
        result = case.call('round-observe', {'task_id':spec['task_id'], 'run_id':spec['run_id'], 'executor':'user'})
    assert result['status'] == 'settled'
    assert result['reopened'] is True
    assert case.task(spec['task_id'])['status'] == 'running'
    assert case.task(spec['task_id'])['failure_code'] is None
    with db.connect(case.db_path) as conn:
        assert round_store.require_round(conn, spec['task_id'], spec['run_id'])['run_status'] == 'finished'
    assert case.server.prompt_count == 0


def test_slow_finalizer_reopens_failed_task_and_keeps_monitoring(plus_scene, monkeypatch):
    from test_process_owner import write_owned_execution_scene
    case, owner, spec, path, _ = plus_scene
    monkeypatch.delenv('KANPILOT_EXECUTOR_INSTANCE_ID', raising=False)
    job = write_owned_execution_scene(case, spec, path)
    job.update(status='failed', detail='heartbeat_failed: previous observer', exit_code=1)
    (path.parent/'job.json').write_text(json.dumps(job))
    case.call('executor-instance', {'instance_id':owner, 'action':'stop'})
    case.mark_needs_user(spec['task_id'], spec['run_id'])
    proc = SimpleNamespace(pid=123, poll=lambda:None)
    monkeypatch.setattr(cmd_dispatch.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(cmd_dispatch, '_register_started_worker', lambda *a: None)
    monkeypatch.setattr(cmd_dispatch, '_observer_pid_alive',
                        lambda conn, tid, rid, pid, directory, data: bool(data.get('observation_finalizing')))
    def pending(directory, process, previous):
        pending_job = dict(job, observe_pid=123, observation_finalizing={
            'turn_id':job['turn_id'], 'observed_status':'completed'})
        (directory/'job.json').write_text(json.dumps(pending_job))
        return 'starting', 'awaiting finalizer'
    monkeypatch.setattr(cmd_dispatch, '_wait_observe_attach', pending)
    result = case.call('round-observe', {'task_id':spec['task_id'], 'run_id':spec['run_id'], 'executor':'user'})
    assert result['status'] == 'finalizing'
    assert result['reopened'] is True
    assert case.task(spec['task_id'])['status'] == 'running'
    assert case.task(spec['task_id'])['failure_code'] is None
    assert case.read_job(path.parent)['status'] == 'failed', 'job terminal must not be faked as running'
