"""Explicit, one-use void for a prerelease dispatch that can never be claimed.

A batch is unclaimable when the workflow snapshot it froze no longer equals the
head of its ref: every dispatch on that ref now produces a different run
``head_sha``, so the frozen identity can never match again. Without an explicit
exit the project-wide pending receipt blocks every future batch forever.

Voiding never re-sends and never deletes audit: it records one accepted
``dispatch-void:<batch_id>`` receipt, retires the original dispatch receipt,
marks the batch abandoned and frees its members to prepare a new batch.
"""
from . import db, runtime_operations, util
from . import release_delivery as release

TOOL = 'github_release_dispatch_void'
HARNESS = 'core'


def operation_id(batch):
    return 'dispatch-void:' + batch['batch_id']


def receipt(conn, batch):
    return db.get_runtime_operation(conn, release._scope(batch['project_id']), operation_id(batch))


def _request(payload):
    # Stable across replays: later ref moves must not change this identity.
    return dict(batch_id=payload['batch_id'], task_id=payload['task_id'],
                context_sha256=payload['expected_context_sha256'],
                dispatch_request_sha256=payload['expected_dispatch_request_sha256'])


def _result(conn, batch):
    value = release._dto(batch, False, conn=conn)
    value.update(voided=True, void_operation_id=batch.get('void_operation_id'),
                 abandoned_at=batch.get('abandoned_at'))
    return dict(outcome='voided', release=value)


def _original(ctx, batch, payload):
    row = db.get_runtime_operation(ctx.conn, release._scope(batch['project_id']),
                                   'dispatch:' + batch['batch_id'])
    release._require(row is not None and row['tool'] == release.DISPATCH_TOOL
                     and row['harness_id'] == 'github'
                     and row['request_sha256'] == payload['expected_dispatch_request_sha256'],
                     'release_void_identity', '原调度回执与冻结输入不符')
    return row


def _eligible(ctx, batch, payload):
    require = release._require
    require(payload.get('confirm') is True, 'release_void_confirmation',
            '作废旧调度必须明确确认：原批次不会重发，成员将重新准备新批次')
    require(isinstance(batch, dict) and batch['root_task_id'] == payload['task_id'],
            'release_batch_missing', '原发布批次或任务归属不符')
    require(batch['context_sha256'] == payload['expected_context_sha256'],
            'release_context_changed', '原发布上下文已变化')
    require(batch['policy']['channel'] == 'prerelease', 'release_void_forbidden',
            '仅预发布批次的未知调度可作废')
    require(batch['state'] == 'unknown' and batch.get('dispatch_status') == 'sent_or_unknown'
            and not batch.get('workflow_run_id') and not batch.get('abandoned'),
            'release_void_forbidden', '仅结果未知且尚未认领运行的原批次可作废')
    require(not release._historical_build_source(ctx.conn, batch), 'release_build_repair_hold',
            '原批次已是构建修复来源，保留只读历史')
    original = _original(ctx, batch, payload)
    require(original['status'] in ('prepared', 'unknown'), 'release_void_forbidden',
            '原调度回执已不是未知结果')
    from . import release_dispatch_recovery as recovery
    require(recovery.receipt(ctx.conn, batch) is None, 'release_void_forbidden',
            '原批次已有恢复尝试，只能在恢复结果上核对')
    require(all(row['operation_id'] == 'dispatch:' + batch['batch_id']
                for row in release._pending(ctx.conn, batch['project_id'])),
            'release_dispatch_unknown', '同项目还有其他未知发布操作，先核对原批次')
    for member in batch['members']:
        task = db.require_task(ctx.conn, member['task_id'])
        require((db.task_extra(task).get('release_ref') or {}).get('batch_id') == batch['batch_id'],
                'release_ref_changed', '发布成员已解除或改关联其他批次')
    return original


def _observe(ctx, batch):
    """Read-only proof: the frozen run cannot appear and no marker run exists."""
    require = release._require
    try:
        current = release.github.workflow_snapshot(batch['repository'], batch['workflow']['path'],
                                                   batch['workflow']['ref'])
        runs = release.github.find_runs(batch['repository'], batch['workflow']['id'])
    except release.gh_client.GhError as exc:
        # No receipt is written for an unreadable observation; retrying is safe.
        raise release.ReleaseBlocked('release_void_observe_failed',
                                     'GitHub 只读核对失败，未作废：' + util.redact(exc.message)) from exc
    require(isinstance(current, dict) and current['id'] == batch['workflow']['id'],
            'release_void_changed', '发布工作流身份已变化')
    require(current.get('head_sha') != batch['workflow']['head_sha'], 'release_void_not_stale',
            '工作流 ref 仍指向原冻结提交，原批次仍可能被认领；继续只读核对')
    marker = [row for row in runs if isinstance(row, dict)
              and row.get('display_title') == 'pilot-release-' + batch['batch_id']]
    require(not marker, 'release_run_observed', '已出现同批次运行，请先核对原批次')
    return dict(current_workflow=dict(id=current['id'], path=current['path'], ref=current['ref'],
                                      head_sha=current['head_sha']),
                marker_runs=0)


def cmd_void_stale_release_dispatch(ctx, payload):
    batch = release._batch(ctx.conn, payload['batch_id'])
    release._require(batch is not None, 'release_batch_missing', '发布批次不存在')
    request = _request(payload)
    existing = receipt(ctx.conn, batch)
    if existing is not None:
        stored = util.jload(existing['result_json']) or {}
        release._require(existing['status'] == 'accepted' and stored.get('request') == request,
                         'release_void_pending', '本次作废结果未知或参数不同，不会重复执行')
        return _result(ctx.conn, batch)
    original = _eligible(ctx, batch, payload)
    release._recover_writer(ctx, batch)
    observation = _observe(ctx, batch)
    scope = release._scope(batch['project_id'])
    op_id = operation_id(batch)
    with db.write_txn(ctx.conn):
        current = release._batch(ctx.conn, batch['batch_id'])
        release._require(receipt(ctx.conn, current) is None, 'release_void_pending',
                         '本次作废结果待确认，不会重复执行')
        current_original = _eligible(ctx, current, payload)
        release._require(dict(current_original) == dict(original), 'release_void_changed',
                         '核对期间原调度回执已变化，未作废')
        audit = dict(request=request, batch_id=current['batch_id'], root_task_id=current['root_task_id'],
                     context_sha256=current['context_sha256'], tag=current['tag'],
                     target_sha=current['target_sha'], frozen_workflow=current['workflow'],
                     reason='冻结的工作流提交已不是该 ref 的当前提交，原调度运行不可能再与冻结身份匹配')
        audit.update(observation)
        runtime_operations.begin_operation(ctx.conn, scope, op_id, HARNESS, TOOL, request, now=ctx.now_iso)
        runtime_operations.complete_operation(ctx.conn, scope, op_id, result=audit, now=ctx.now_iso)
        runtime_operations.complete_operation(
            ctx.conn, scope, 'dispatch:' + current['batch_id'], status='rejected',
            result=dict(util.jload(original['result_json']) or {}, void_operation_id=op_id,
                        voided_at=ctx.now_iso,
                        reason='已显式作废不可复现的未知预发布调度；原请求不会重发'),
            now=ctx.now_iso)
        current.update(abandoned=True, abandoned_at=ctx.now_iso, void_operation_id=op_id,
                       state='blocked', blockers=[],
                       reason='已作废不可复现的未知预发布调度；成员可重新准备新批次')
        release._save(ctx.conn, current, ctx.now_iso, status='rejected')
        for member in current['members']:
            task = db.get_task(ctx.conn, member['task_id'])
            if task is not None and (db.task_extra(task).get('release_ref') or {}).get('batch_id') == current['batch_id']:
                extra = db.task_extra(task)
                extra.pop('release_ref', None)
                db.update_task(ctx.conn, task['task_id'], ctx.now_iso, extra_json=extra)
    return _result(ctx.conn, release._batch(ctx.conn, batch['batch_id']))
