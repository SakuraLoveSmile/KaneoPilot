"""Manual budget segments and recovery use disposable DBs and fake Plus only."""
from datetime import timedelta
from unittest.mock import patch

import pytest
import test_request_continuation as continuation_fixture
import test_verification_recovery as recovery_fixture
from kanpilot import (cli, cmd_request_continuation as continuation,
                     core, db, util, verification_recovery)
from kanpilot.engine import verdict


OLD = '2026-01-01T00:00:00+00:00'


@pytest.fixture
def continued():
    case = continuation_fixture.RequestContinuationTest('runTest')
    case.setUp()
    f = case.f
    core.update_automation(f.ctx.conn, f.task(), f.ctx.now_iso, manual_takeover=False,
        segment_started_at=OLD, segment_cost_base_usd=2, cost_usd=12.5)
    try:
        yield case, f, dict(task_id=f.task_id, run_id=f.run_id, confirm=True)
    finally:
        case.doCleanups()


def test_acknowledged_manual_continuation_starts_one_new_automation_segment(continued):
    case, f, payload = continued
    result = continuation.cmd_request_continuation(f.ctx, payload)
    assert result['status'] == 'continued'
    budget = core.automation_of(f.task())
    assert budget['segment_started_at'] == f.ctx.now_iso
    assert budget['segment_cost_base_usd'] == 12.5
    assert core.automation_budget(f.task(), {'max_task_minutes': 240}, f.ctx.now)['ok']
    f.ctx.now += timedelta(hours=1)
    f.ctx.now_iso = util.to_iso(f.ctx.now)
    repeated = continuation.cmd_request_continuation(f.ctx, payload)
    assert repeated['deduplicated'] is True
    assert core.automation_of(f.task()) == budget
    assert len(case.calls) == 1


def test_unknown_continuation_does_not_open_a_new_budget_segment(continued):
    case, f, payload = continued
    before = core.automation_of(f.task())
    with patch.object(continuation, '_client', return_value=continuation_fixture.FakeClient(case.calls, fail=True)):
        with pytest.raises(util.KanpilotError):
            continuation.cmd_request_continuation(f.ctx, payload)
    assert core.automation_of(f.task()) == before
    assert f.task()['status'] == 'needs_user'


@pytest.fixture
def recovery():
    case = recovery_fixture.TestVerificationRecovery('runTest')
    case.setUp()
    ctx = cli.Context(case.db_path)
    row = verification_recovery.round_store.get_round(ctx.conn, case.task_id, case.run_id)
    binding = dict(task_id=case.task_id, execution_run_id=case.run_id,
        plan_sha256=case.plan_sha, worktree_path=row['worktree_path'], source_sha256='source')
    receipt = dict(engine_mode='live', kind='empty_diff', empty_diff=True, verification_ok=False,
        evidence_source='goal_report', execution_run_id=case.run_id, plan_sha256=case.plan_sha,
        binding=binding, execution_turn_id='execution-turn', report_sha256='report',
        evidence_sha256={'job.json': 'hash'}, completed_at=OLD)
    core.update_automation(ctx.conn, db.require_task(ctx.conn, case.task_id), ctx.now_iso,
        manual_takeover=False, segment_started_at=OLD, segment_cost_base_usd=2, cost_usd=12.5)
    db.set_extra(ctx.conn, db.require_task(ctx.conn, case.task_id), ctx.now_iso, engine_verdict=receipt)
    db.update_task(ctx.conn, case.task_id, ctx.now_iso, failure_code='auto_budget_exceeded')
    try:
        yield case, ctx, binding, receipt
    finally:
        ctx.close()
        case.doCleanups()


def test_reverify_budget_block_requires_current_receipt_then_queues_only_monitor(recovery):
    case, ctx, binding, receipt = recovery
    original = dict(db.require_task(ctx.conn, case.task_id))
    with patch.object(verdict, 'binding', return_value=binding), \
            patch.object(verdict, 'receipt_evidence_current', return_value=True) as current:
        result = verification_recovery.recover(ctx, case.payload)
    assert current.call_count == 2
    assert all(call.args[2] == receipt for call in current.call_args_list)
    assert result['request']['kind'] == 'monitor'
    task = db.require_task(ctx.conn, case.task_id)
    assert task['status'] == 'running' and task['failure_code'] is None
    for key in ('run_id', 'plan_sha256', 'session_id', 'plan_path'):
        assert task[key] == original[key]
    assert core.automation_of(task)['segment_started_at'] == ctx.now_iso
    assert core.automation_of(task)['segment_cost_base_usd'] == 12.5
    assert not ctx.conn.execute("SELECT 1 FROM requests WHERE task_id=? AND kind='dispatch'",
                                (case.task_id,)).fetchone()
    budget = core.automation_of(task)
    ctx.now += timedelta(hours=1)
    ctx.now_iso = util.to_iso(ctx.now)
    assert verification_recovery.recover(ctx, case.payload)['deduplicated']
    assert core.automation_of(db.require_task(ctx.conn, case.task_id)) == budget
    assert case.fake.requests_log == []


@pytest.mark.parametrize('field,value', [('execution_run_id', 'other'), ('plan_sha256', 'other'),
    ('kind', 'verify_failed'), ('evidence_source', 'legacy'), ('verification_ok', True)])
def test_unrelated_budget_block_has_no_reverify_entry(recovery, field, value):
    case, ctx, binding, receipt = recovery
    changed = dict(receipt, **{field: value})
    db.set_extra(ctx.conn, db.require_task(ctx.conn, case.task_id), ctx.now_iso, engine_verdict=changed)
    assert not verification_recovery.candidate(ctx.conn, db.require_task(ctx.conn, case.task_id))
    with pytest.raises(util.KanpilotError):
        verification_recovery.recover(ctx, case.payload)


@pytest.mark.parametrize('changed_binding,current_receipt', [(True, True), (False, False)])
def test_stale_source_or_receipt_keeps_budget_and_block_intact(recovery, changed_binding, current_receipt):
    case, ctx, binding, receipt = recovery
    before = verification_recovery._snapshot(ctx.conn, case.task_id, case.run_id)
    current_binding = dict(binding, source_sha256='changed') if changed_binding else binding
    with patch.object(verdict, 'binding', return_value=current_binding), \
            patch.object(verdict, 'receipt_evidence_current', return_value=current_receipt):
        with pytest.raises(util.KanpilotError):
            verification_recovery.recover(ctx, case.payload)
    assert verification_recovery._snapshot(ctx.conn, case.task_id, case.run_id) == before


def test_failed_full_verification_cannot_reopen_budget_or_enqueue(recovery):
    case, ctx, binding, receipt = recovery
    case.goal['checks'] = []
    before = verification_recovery._snapshot(ctx.conn, case.task_id, case.run_id)
    with patch.object(verdict, 'binding', return_value=binding), \
            patch.object(verdict, 'receipt_evidence_current', return_value=True):
        with pytest.raises(util.KanpilotError):
            verification_recovery.recover(ctx, case.payload)
    assert verification_recovery._snapshot(ctx.conn, case.task_id, case.run_id) == before
