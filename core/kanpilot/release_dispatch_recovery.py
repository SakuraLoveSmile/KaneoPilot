"""Explicit one-use recovery for an ambiguous prerelease dispatch."""
from . import core, db, runtime_operations, util
from . import release_delivery as release

TOOL = 'github_release_dispatch_recovery'


def operation_id(batch):
    return 'dispatch-recovery:' + batch['batch_id']


def receipt(conn, batch):
    return db.get_runtime_operation(conn, release._scope(batch['project_id']), operation_id(batch))


def _result(conn, batch):
    return dict(outcome=batch['state'], release=release._dto(batch, True, conn=conn))


def _local(ctx, batch, payload):
    require = release._require
    require(payload.get('confirm') is True, 'release_recovery_confirmation', '恢复发送必须明确确认原请求可能延迟生效及重复触发')
    require(batch is not None and batch['root_task_id'] == payload['task_id'], 'release_batch_missing', '原发布批次或任务归属不符')
    require(batch['context_sha256'] == payload['expected_context_sha256'], 'release_context_changed', '原发布上下文已变化')
    original = db.get_runtime_operation(ctx.conn, release._scope(batch['project_id']), 'dispatch:' + batch['batch_id'])
    expected = runtime_operations.compute_request_sha256(dict(batch_id=batch['batch_id'], workflow=batch['workflow'], inputs=batch['inputs']))
    require(original is not None and original['tool'] == release.DISPATCH_TOOL and original['harness_id'] == 'github'
            and original['request_sha256'] == expected == payload['expected_dispatch_request_sha256'],
            'release_recovery_identity', '原调度回执与冻结输入不符')
    return original


def _eligible(ctx, batch):
    require = release._require
    require(batch['policy']['channel'] == 'prerelease' and batch['state'] == 'unknown'
            and batch.get('dispatch_status') == 'sent_or_unknown' and not batch.get('workflow_run_id')
            and not batch.get('abandoned') and not release._historical_build_source(ctx.conn, batch),
            'release_recovery_forbidden', '仅结果未知且尚未认领运行的原预发布批次允许显式恢复一次')
    require(release._current(ctx.conn, batch), 'release_members_changed', '原发布成员身份已变化')
    for member in batch['members']:
        task = db.require_task(ctx.conn, member['task_id'])
        require((db.task_extra(task).get('release_ref') or {}).get('batch_id') == batch['batch_id'],
                'release_ref_changed', '发布成员已改关联其他批次')
    root = db.require_task(ctx.conn, batch['root_task_id'])
    policy, revision = release._policy(ctx.conn, root)
    require(policy == batch['policy'] and revision == batch['policy_snapshot'] and not db.paused(ctx.conn, 'delivery')
            and not db.task_auto_paused(root) and not core.automation_of(root).get('manual_takeover'),
            'release_policy_changed', '原发布策略、暂停或人工接管状态已变化')
    require(all(row['operation_id'] == 'dispatch:' + batch['batch_id'] for row in release._pending(ctx.conn, batch['project_id'])),
            'release_dispatch_unknown', '同项目还有其他未知发布操作，不能恢复发送')
    manifest, inputs = release._inputs(batch)
    require(manifest == batch['task_manifest'] and inputs == batch['inputs'], 'release_recovery_identity', '原发布输入不完整或已变化')


def _existing_run(ctx, batch):
    runs = release.github.find_runs(batch['repository'], batch['workflow']['id'])
    marker = [row for row in runs if isinstance(row, dict) and row.get('display_title') == 'pilot-release-' + batch['batch_id']]
    release._require(len(marker) <= 1, 'release_run_ambiguous', '找到多个原批次运行，禁止恢复发送')
    if marker:
        release._require(release._run_matches(batch, marker[0]), 'release_run_mismatch', '同批次 marker 的运行身份不符，禁止恢复发送')
        return release.cmd_sync_release_batch(ctx, dict(batch_id=batch['batch_id']))
    return None


def observe(conn, batch, run_id, now):
    """Keep the original ambiguous receipt when a read resolves the batch."""
    row = receipt(conn, batch)
    if row is not None and row['status'] in ('prepared', 'unknown'):
        result = util.jload(row['result_json']) or {}
        runtime_operations.complete_operation(conn, release._scope(batch['project_id']), operation_id(batch),
            result=dict(result, workflow_run_id=run_id, resolution_source='read_only_observation'), now=now)


def cmd_recover_release_dispatch(ctx, payload):
    batch = release._batch(ctx.conn, payload['batch_id'])
    original = _local(ctx, batch, payload)
    # Every existing slot is consumed, including a process lost before the POST.
    if receipt(ctx.conn, batch) is not None:
        return _result(ctx.conn, batch)
    _eligible(ctx, batch)
    release._require(original['status'] == 'unknown', 'release_recovery_forbidden', '原调度回执不是未知结果')
    release._recover_writer(ctx, batch)
    found = _existing_run(ctx, batch)
    if found is not None:
        return found
    context = release._prepare(ctx, payload['task_id'])
    release._require(context['context_sha256'] == batch['context_sha256'], 'release_context_changed', '恢复前原源码、验证、工作流或发布证据已变化')
    release._require(release.github.tag_commit(batch['repository'], batch['tag']) is None,
                     'release_tag_collision', '原 tag 已存在，禁止恢复发送或覆盖')
    found = _existing_run(ctx, batch)
    if found is not None:
        return found
    owner = 'release-recovery:' + util.new_uuid()
    process = release._process_identity()
    scope = release._scope(batch['project_id'])
    op_id = operation_id(batch)
    audit = None
    try:
        with db.write_txn(ctx.conn):
            current = release._batch(ctx.conn, batch['batch_id'])
            current_original = _local(ctx, current, payload)
            if receipt(ctx.conn, current) is not None:
                return _result(ctx.conn, current)
            _eligible(ctx, current)
            release._require(dict(current_original) == dict(original), 'release_recovery_changed', '核对期间原调度回执已变化，未发送')
            core.acquire_lifecycle(ctx.conn, owner)
            audit = dict(batch_id=batch['batch_id'], original_dispatch=dict(original), lifecycle_owner=owner,
                         process_identity=process, duplicate_risk_confirmed=True, authorized_context_sha256=batch['context_sha256'])
            runtime_operations.begin_operation(ctx.conn, scope, op_id, 'github', TOOL,
                dict(batch_id=batch['batch_id'], context_sha256=batch['context_sha256'],
                     original_dispatch_request_sha256=original['request_sha256'], workflow=batch['workflow'], inputs=batch['inputs']), now=ctx.now_iso)
            runtime_operations.complete_operation(ctx.conn, scope, op_id, status='unknown', result=audit, now=ctx.now_iso)
            batch = current
            batch.update(reason='已授权一次恢复调度，等待 GitHub 确认；不会再次发送', blockers=[])
            release._save(ctx.conn, batch, ctx.now_iso)
        response = release.github.dispatch(batch['repository'], batch['workflow']['id'], batch['workflow']['ref'], batch['inputs'])
        run_id = response.get('workflow_run_id') if isinstance(response, dict) else None
        release._require(type(run_id) is int and run_id > 0
            and response.get('run_url') == 'https://api.github.com/repos/' + batch['repository'] + '/actions/runs/' + str(run_id)
            and response.get('html_url') == 'https://github.com/' + batch['repository'] + '/actions/runs/' + str(run_id),
            'release_dispatch_unconfirmed', '恢复响应未返回精确运行身份；只允许核对，不再发送')
        run = release.github.run(batch['repository'], run_id)
        release._require(release._run_matches(batch, run), 'release_run_mismatch', '恢复运行与冻结发布身份不符')
        with db.write_txn(ctx.conn):
            release._require(release._current(ctx.conn, batch), 'release_members_changed', '保存恢复运行前成员身份已变化')
            current = release._batch(ctx.conn, batch['batch_id'])
            release._require(current.get('workflow_run_id') in (None, run_id), 'release_run_ambiguous', '恢复期间已认领其他运行，保留两个操作审计')
            runtime_operations.complete_operation(ctx.conn, scope, op_id,
                result=dict(audit, workflow_run_id=run_id, resolution_source='dispatch_response'), now=ctx.now_iso)
            runtime_operations.complete_operation(ctx.conn, scope, 'dispatch:' + batch['batch_id'],
                result=dict(batch_id=batch['batch_id'], workflow_run_id=run_id, recovery_operation_id=op_id), now=ctx.now_iso)
            batch.update(state='queued', workflow_run_id=run_id, workflow_url=run['html_url'], reason='恢复构建已排队', blockers=[])
            release._save(ctx.conn, batch, ctx.now_iso)
        return _result(ctx.conn, batch)
    except Exception as exc:
        if audit is None:
            raise
        with db.write_txn(ctx.conn):
            runtime_operations.complete_operation(ctx.conn, scope, op_id, status='unknown',
                result=dict(audit, error=util.redact(str(exc))), now=ctx.now_iso)
        return release._failure(ctx.conn, batch, ctx.now_iso, exc, state='unknown')
    finally:
        with db.write_txn(ctx.conn):
            core.release_lifecycle(ctx.conn, owner)
