"""Audited, description-only repair of an owned published Release."""
import re

from . import db, runtime_operations, util
from . import release_delivery as delivery, release_github as github

TOOL = 'github_release_notes'


def operation_id(payload):
    return 'release-notes:' + payload['batch_id'] + ':' + payload['expected_body_sha256']


def owner_marker(batch):
    owner = dict(schema=1, batch_id=batch['batch_id'], repository=batch['repository'],
                 target_sha=batch['target_sha'], workflow_run_id=batch['workflow_run_id'],
                 task_manifest_sha256=batch['inputs']['task_manifest_sha256'])
    return 'kaneopilot-owner:' + util.jdump(owner)


def _local(ctx, payload):
    require = delivery._require
    require(payload.get('confirm') is True, 'release_notes_confirmation', '修正公开发布说明必须明确确认')
    batch = delivery._batch(ctx.conn, payload['batch_id'])
    require(batch is not None and batch['root_task_id'] == payload['task_id'],
            'release_batch_missing', '发布批次与任务归属不符')
    task = db.require_task(ctx.conn, payload['task_id'])
    require(task['project_id'] == batch['project_id'] and batch['context_sha256'] == payload['expected_context_sha256'],
            'release_context_changed', '发布任务或冻结上下文已变化')
    manifest = batch['task_manifest']
    require(util.jdump(manifest) == batch['inputs']['task_manifest_json']
            and util.sha256_text(util.jdump(manifest)) == batch['inputs']['task_manifest_sha256']
            and all(manifest.get(key) == batch[key] for key in ('batch_id', 'repository', 'target_sha', 'tag'))
            and type(batch.get('workflow_run_id')) is int and batch['workflow_run_id'] > 0,
            'release_notes_identity', '发布规范、标签或运行身份不完整')
    require(type(payload['release_id']) is int and payload['release_id'] > 0
            and re.fullmatch(r'[0-9a-f]{64}', payload['expected_body_sha256']) is not None,
            'release_notes_identity', '发布 ID 或原正文哈希无效')
    text = payload['notes']
    require(isinstance(text, str) and bool(text.strip()) and '\x00' not in text
            and 'kaneopilot-owner:' not in text and len(text.encode('utf-8')) <= 60 * 1024,
            'release_notes_invalid', '发布说明必须为非空用户文本，不含内部归属标记，且不超过 60 KiB')
    return batch


def _snapshot(batch, release_id):
    require = delivery._require
    remote = github.release(batch['repository'], batch['tag'])
    marker = owner_marker(batch)
    body = remote.get('body') if isinstance(remote, dict) else None
    require(isinstance(body, str) and body.split('\n', 1)[0] in (marker, '<!-- ' + marker + ' -->')
            and body.count('kaneopilot-owner:') == 1,
            'release_notes_owner', '发布说明缺少精确归属或存在冲突标记')
    require(remote.get('id') == release_id and remote.get('tag_name') == batch['tag']
            and remote.get('draft') is False and remote.get('prerelease') is (batch['policy']['channel'] == 'prerelease')
            and remote.get('html_url') == 'https://github.com/' + batch['repository'] + '/releases/tag/' + batch['tag'],
            'release_notes_identity', 'Release ID、标签、渠道或公开状态不符')
    tag = github.release_tag(batch['repository'], batch['tag'])
    require(tag.get('tag') == batch['tag'] and tag.get('message') == marker
            and tag.get('object', {}).get('type') == 'commit'
            and tag.get('object', {}).get('sha') == batch['target_sha'],
            'release_notes_tag', '不可变标签的原归属或源码目标不符')
    assets = remote.get('assets')
    require(isinstance(assets, list) and all(isinstance(a, dict) and isinstance(a.get('name'), str) for a in assets)
            and len({a['name'] for a in assets}) == len(assets)
            and {a['name'] for a in assets} == set(batch['policy']['required_assets']) | {github.MANIFEST_NAME},
            'release_notes_assets', '原版本的安装包和校验清单不完整或重复')
    fields = ('id', 'tag_name', 'name', 'draft', 'prerelease', 'target_commitish', 'html_url', 'created_at', 'published_at')
    asset_fields = ('id', 'name', 'size', 'digest', 'state', 'browser_download_url')
    identity = dict(release={key: remote.get(key) for key in fields}, tag=tag,
                    assets=[{key: a.get(key) for key in asset_fields} for a in sorted(assets, key=lambda a: a['name'])])
    return body, identity


def _result(status, op_id):
    return dict(outcome=status, operation_id=op_id)


def _reconcile(ctx, batch, payload, row):
    # Unknown includes a writer that may still be running. Never resend.
    require = delivery._require
    require(row['tool'] == TOOL and row['harness_id'] == 'github'
            and row['request_sha256'] == runtime_operations.compute_request_sha256(payload),
            'release_notes_conflict', '原正文修正操作已绑定不同参数')
    audit = util.jload(row['result_json'])
    try:
        body, identity = _snapshot(batch, payload['release_id'])
        require(body == audit['new_body'] and identity == audit['identity'],
                'release_notes_unconfirmed', '新说明、标签或资产尚未与原修正回执一致，只能继续核对')
    except Exception as exc:
        return dict(_result('unknown', row['operation_id']), reason=util.redact(str(exc)))
    with db.write_txn(ctx.conn):
        _local(ctx, payload)
        runtime_operations.complete_operation(ctx.conn, delivery._scope(batch['project_id']), row['operation_id'],
            result=dict(audit, resolution_source='read_only_observation'), now=ctx.now_iso)
    return _result('accepted', row['operation_id'])


def cmd_repair_release_notes(ctx, payload):
    batch = _local(ctx, payload)
    scope = delivery._scope(batch['project_id'])
    op_id = operation_id(payload)
    existing = db.get_runtime_operation(ctx.conn, scope, op_id)
    if existing is not None:
        return _reconcile(ctx, batch, payload, existing)
    old_body, identity = _snapshot(batch, payload['release_id'])
    delivery._require(util.sha256_text(old_body) == payload['expected_body_sha256'],
                      'release_notes_changed', '远端正文已变化，未覆盖新编辑')
    new_body = '<!-- ' + owner_marker(batch) + ' -->\n\n' + payload['notes']
    audit = dict(batch_id=batch['batch_id'], release_id=payload['release_id'],
                 old_body=old_body, new_body=new_body, identity=identity)
    with db.write_txn(ctx.conn):
        _local(ctx, payload)
        existing = db.get_runtime_operation(ctx.conn, scope, op_id)
        if existing is None:
            pending = ctx.conn.execute("SELECT result_json FROM runtime_operations WHERE scope_id=? AND tool=? AND status IN ('prepared','unknown')",
                                       (scope, TOOL)).fetchall()
            delivery._require(not any((util.jload(row['result_json']) or {}).get('batch_id') == batch['batch_id'] for row in pending),
                              'release_notes_pending', '此版本还有结果未知的说明操作，不能换参数重新发送')
            runtime_operations.begin_operation(ctx.conn, scope, op_id, 'github', TOOL, payload, now=ctx.now_iso)
            runtime_operations.complete_operation(ctx.conn, scope, op_id, status='unknown', result=audit, now=ctx.now_iso)
    if existing is not None:
        return _reconcile(ctx, batch, payload, existing)
    try:
        # Recheck after reserving the receipt; GitHub has no body compare-and-swap.
        latest_body, latest_identity = _snapshot(batch, payload['release_id'])
        delivery._require((latest_body, latest_identity) == (old_body, identity),
                          'release_notes_changed', '发送前发布说明或标签/资产已变化，未发送')
        github.patch_release_body(batch['repository'], payload['release_id'], new_body)
        row = db.get_runtime_operation(ctx.conn, scope, op_id)
        return _reconcile(ctx, batch, payload, row)
    except Exception as exc:
        with db.write_txn(ctx.conn):
            current = db.get_runtime_operation(ctx.conn, scope, op_id)
            # A concurrent read may already have proved the PATCH succeeded.
            # A delayed transport failure must not downgrade that observation.
            if current['status'] == 'accepted':
                return _result('accepted', op_id)
            runtime_operations.complete_operation(ctx.conn, scope, op_id, status='unknown',
                result=dict(audit, error=util.redact(str(exc))), now=ctx.now_iso)
        return dict(_result('unknown', op_id), reason=util.redact(str(exc)))
