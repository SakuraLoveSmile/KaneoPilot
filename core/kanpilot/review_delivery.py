"""User-authorized continuation of an existing manual Review pass into PR delivery.

Listing and worker authority are SQLite-only. The resolve command reuses the
formal Goal verifier, checks clean reviewed source and reads Kaneo before it
atomically records permission for this one immutable delivery identity.
"""
from . import core, db, git_delivery, project_config, util

_TASK_KEYS = ('task_id', 'project_id', 'repository', 'requirements_sha256',
              'run_id', 'plan_path', 'plan_sha256', 'review_key',
              'review_report_path', 'review_report_sha256')
_BRANCH_KEYS = ('workspace_version', 'branch', 'worktree_path', 'base_branch', 'base_sha', 'head_sha')
WAITING_REASON = '正式评审已通过；继续交付将复用已有报告，创建 PR 并推进合并和已配置的构建发布'
_DELIVERY_ENVIRONMENT_CODES = ('gh_missing', 'gh_not_logged_in', 'gh_timeout', 'gh_unavailable')


def manual_release_review(task):
    """Settlement's DTO gate; release.required is derived by the core itself."""
    return (not core.is_automation_managed(task)
            and core.delivery_of(task).get('workspace_version') == 1
            and core.delivery_of(task).get('mode') == 'manual'
            and (task.get('release') or {}).get('required') is True)


def _digest(value):
    return util.sha256_text(util.jdump(value))


def review_round_matches(task, review):
    """Old manual branch entries predate fix_round; managed rows stay strict."""
    receipt=db.task_extra(task).get('task_ai_action_delivery') or {}
    if (receipt.get('action') or {}).get('source_kind') in ('merge_conflict','build_failure'):
        from . import review_repair
        return review.get('fix_round')==review_repair._round_count(task)
    if core.is_automation_managed(task):
        return review.get('fix_round') == core.automation_of(task).get('fix_round', 0)
    from . import review_repair
    return review.get('fix_round', review_repair._round_count(task)) == review_repair._round_count(task)


def _pass(task):
    extra, delivery = db.task_extra(task), core.delivery_of(task)
    history = extra.get('review_history') or []
    review = history[-1] if history and isinstance(history[-1], dict) else {}
    identity = extra.get('registered_review_identity') or {}
    if (extra.get('review_conclusion') != 'pass' or review.get('conclusion') != 'pass'
            or any(not task[key] for key in _TASK_KEYS)
            or any(not delivery.get(key) for key in _BRANCH_KEYS)
            or review.get('execution_run_id') != task['run_id']
            or review.get('head_sha') != delivery['head_sha']
            or any(review.get(key) != task[column] for key, column in
                   (('review_key','review_key'), ('report_path','review_report_path'),
                    ('report_sha256','review_report_sha256')))
            or any(review.get(key) != delivery[key] for key in
                   ('branch','worktree_path','base_branch','base_sha'))
            or review.get('plan_sha256', task['plan_sha256']) != task['plan_sha256']
            or not review_round_matches(task, review)
            or not identity.get('request_id') or not identity.get('run_id')
            or any(identity.get(key) != task[column] for key, column in
                   (('execution_run_id','run_id'), ('plan_sha256','plan_sha256'),
                    ('report_sha256','review_report_sha256')))
            or not any(c.get('execution_run_id') == task['run_id'] and c.get('sha') == delivery['head_sha']
                       for c in delivery.get('commits') or [])):
        return None
    return identity


def _review_request(conn, task, identity):
    request = db.get_request(conn, identity['request_id'])
    latest = conn.execute("SELECT request_id FROM requests WHERE task_id=? AND kind='review' "
                          "ORDER BY created_at DESC,rowid DESC LIMIT 1", (task['task_id'],)).fetchone()
    if (request is None or request['task_id'] != task['task_id'] or request['kind'] != 'review'
            or request['stage'] != 'review' or request['run_id'] != identity['run_id']
            or not request['finished_at'] or latest is None or latest['request_id'] != request['request_id']):
        return None
    return request


def _misblocked(task, request):
    result = util.jload(request['result_json']) or {}
    # This exact failure comes from _registration_failed's advance-task catch.
    # A pass alone cannot override an unrelated Review/execution/user block.
    reason = task['failure_reason'] or ''
    note = result.get('note') or result.get('failure_reason') or ''
    return (task['status'] == 'needs_user' and task['stage'] == 'review'
            and task['failure_stage'] in ('review', 'in_review')
            and task['failure_code'] == 'review_blocked'
            and reason.startswith('advance-task 失败') and 'release_required:' in reason
            and note.startswith('advance-task 失败') and 'release_required:' in note
            and result.get('outcome') in ('needs_user', 'failed')
            and result.get('failure_code') == 'review_blocked' and request['status'] == 'failed')


def _mapping(conn, task):
    _, _, items, _ = project_config.load_raw_mappings(conn)
    mapping = items.get(task['project_id']) or {}
    return mapping if mapping.get('enabled', True) and mapping.get('repository') == task['repository'] else None


def _execution_active(conn, task):
    row = conn.execute('SELECT save_state,run_status,worktree_path FROM execution_rounds WHERE task_id=? AND run_id=?',
                       (task['task_id'], task['run_id'])).fetchone()
    return (row is not None and row['save_state'] == 'active' and row['run_status'] == 'finished'
            and row['worktree_path'] == core.delivery_of(task).get('worktree_path'))


def candidate(conn, task):
    """Cheap current pass gate, never reads files or external services."""
    from . import release_delivery
    if (not db.task_is_active(task) or core.is_automation_managed(task)
            or core.automation_of(task).get('manual_takeover') or db.task_auto_paused(task)
            or db.paused(conn, 'delivery') or task['sent'] != 1 or task['send_state'] != 'sent'
            or task['status'] not in ('in_review', 'needs_user')
            or core.review_repair_of(task) or _mapping(conn, task) is None or not _execution_active(conn, task)):
        return None
    if _failed_delivery(conn, task) is not None:
        return dict(execution_run_id=task['run_id'], review_key=task['review_key'])
    if task['stage'] != 'review':
        return None
    delivery = core.delivery_of(task)
    if (delivery.get('mode') != 'manual' or delivery.get('workspace_version') != 1
            or any(delivery.get(key) for key in ('pr_number', 'pushed_sha', 'merged_at', 'stage_contracts'))
            or not release_delivery._policy(conn, task)[0]['enabled']
            or conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
                            (task['task_id'],)).fetchone()):
        return None
    identity = _pass(task)
    if identity is None:
        return None
    request = _review_request(conn, task, identity)
    if request is None:
        return None
    passed = (task['status'] == 'in_review' and not task['failure_code']
              and request['status'] == 'succeeded'
              and (util.jload(request['result_json']) or {}).get('outcome') == 'succeeded')
    if not passed and not _misblocked(task, request):
        return None
    return dict(execution_run_id=task['run_id'], review_key=task['review_key'])


def attention_fields(conn, task):
    if candidate(conn, task) is None:
        return {}
    return dict(failure_code=None, failure_stage='delivery',
                failure_reason=WAITING_REASON, blocked_summary=WAITING_REASON)


def continuation(task, *, allow_paused=False, allow_delivery_block=False):
    """Metadata-only authority for the one explicitly continued reviewed result."""
    extra, delivery = db.task_extra(task), core.delivery_of(task)
    receipt = extra.get('manual_review_delivery') or {}
    retryable_block = (allow_delivery_block and task['status'] == 'needs_user'
                       and task['failure_stage'] == 'delivery'
                       and task['failure_code'] in _DELIVERY_ENVIRONMENT_CODES)
    state_allowed = (task['status'] == 'in_review' and not task['failure_code']) or retryable_block
    if (not receipt or receipt.get('authorized_by') != 'user' or not receipt.get('authorized_at')
            or not state_allowed or not db.task_is_active(task)
            or core.is_automation_managed(task) or core.automation_of(task).get('manual_takeover')
            or (not allow_paused and db.task_auto_paused(task))
            or task['sent'] != 1 or task['send_state'] != 'sent'
            or core.review_repair_of(task) or delivery.get('mode') != 'pr'
            or receipt.get('task') != {key: task[key] for key in _TASK_KEYS}
            or receipt.get('branch') != {key: delivery.get(key) for key in _BRANCH_KEYS}
            or receipt.get('review_identity') != _pass(task)
            or receipt.get('evidence_identity') != _digest({key: extra.get(key) for key in
                   ('engine_verdict', 'plan_verify', 'review_history')})
            or receipt.get('id') != _digest({key: value for key, value in receipt.items() if key != 'id'})):
        return None
    return receipt


def allowed(conn, task, request=None, *, allow_paused=False, allow_delivery_block=False):
    receipt = continuation(task, allow_paused=allow_paused, allow_delivery_block=allow_delivery_block)
    if (receipt is None or _mapping(conn, task) is None or not _execution_active(conn, task)
            or (not allow_paused and db.paused(conn, 'delivery'))):
        return False
    identity = receipt['review_identity']
    review = _review_request(conn, task, identity)
    if (review is None or review['status'] != receipt['review_request_status']
            or _digest(util.jload(review['result_json'])) != receipt['review_result_sha256']):
        return False
    if request is not None:
        payload = db.request_payload(request)
        if (request['task_id'] != task['task_id'] or request['kind'] != 'delivery'
                or request['stage'] != 'delivery' or request['created_at'] < receipt['authorized_at']
                or any(payload.get(key) != value for key, value in request_payload(task, receipt).items())):
            return False
    return True


def _failed_delivery(conn, task):
    if not allowed(conn, task, allow_delivery_block=True):
        return None
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1",
                    (task['task_id'],)).fetchone():
        return None
    latest = conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='delivery' "
                          "ORDER BY created_at DESC,rowid DESC LIMIT 1", (task['task_id'],)).fetchone()
    if (latest is None or latest['status'] != 'failed' or not latest['finished_at']
            or not allowed(conn, task, latest, allow_delivery_block=True)):
        return None
    return continuation(task, allow_delivery_block=True)


def request_payload(task, receipt):
    return dict(manual_review_delivery_id=receipt['id'], expected_execution_run_id=task['run_id'],
                expected_head_sha=receipt['branch']['head_sha'], expected_plan_sha256=task['plan_sha256'])


def enqueue(conn, task, now, *, not_before=None, requested_by='scheduler'):
    receipt = continuation(task, allow_paused=True)
    util.require(receipt is not None, '手动交付授权身份已变化', 'conflict')
    return db.create_request(conn, now, 'delivery', 'delivery:' + task['task_id'], task_id=task['task_id'],
        requested_by=requested_by,
        not_before=not_before, payload=request_payload(task, receipt))[0]


def _snapshot(conn, task):
    from . import review_recovery
    return review_recovery._snapshot(conn, task['task_id'], task['run_id'])


def _exact_execution_coverage(conn, task, hashes):
    """Revalidate the registered backend without widening parent closeout support."""
    from pathlib import Path
    from . import codex_execution, parent_closeout, planfile
    from .engine import verdict
    row = conn.execute('SELECT backend FROM execution_rounds WHERE task_id=? AND run_id=?',
                       (task['task_id'], task['run_id'])).fetchone()
    if row is None or row['backend'] != 'codex':
        return parent_closeout._task_evidence(conn, task, hashes)['exact_coverage']
    util.require(task['source_path'] and task['source_sha256'] and task['requirements_sha256'],
                 '任务缺少冻结需求来源身份', 'conflict')
    source_file_sha = util.sha256_file(Path(task['source_path']))
    _, source_sha, requirements = planfile.check_source_snapshot(
        task['source_path'], task['task_id'], task['project_id'])
    util.require((source_sha, requirements) == (task['source_sha256'], task['requirements_sha256'])
                 and util.sha256_file(Path(task['source_path'])) == source_file_sha,
                 '任务冻结需求来源已变化', 'conflict')
    hashes[task['source_path']] = source_file_sha
    receipt = db.task_extra(task).get('engine_verdict') or {}
    util.require(receipt.get('evidence_source') == 'codex_report',
                 '正式 Codex 验证回执来源不符', 'conflict')
    evidence = codex_execution.read_evidence(conn, task)
    util.require(evidence['coverage']['ok'] and verdict.valid_committed_receipt(conn, task, receipt),
                 '正式 Codex 验证回执、报告或已验证源码快照身份不符', 'conflict')
    hashes.update(evidence['hashes'])
    checks = evidence['report']['report']['checks']
    return all(any(check.get('command') == command and type(check.get('exit_code')) is int
                   and check['exit_code'] == 0 for check in checks) for command in evidence['commands'])


def continue_delivery(ctx, payload):
    util.require(payload.get('actor', 'user') == 'user', '继续交付必须由用户明确发起', 'bad_request')
    expected = dict(execution_run_id=payload.get('expected_execution_run_id'), review_key=payload.get('expected_review_key'))
    util.require(all(isinstance(v, str) and v.strip() for v in expected.values()),
                 '继续交付必须指定当前执行轮次和 Review key', 'bad_request')
    task = db.require_task(ctx.conn, payload['task_id'])
    util.require((task['run_id'], task['review_key']) == (expected['execution_run_id'], expected['review_key']),
                 '执行轮次或 Review 已变化，请刷新后继续交付', 'conflict')
    if allowed(ctx.conn, task):
        active = ctx.conn.execute("SELECT * FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                                  (task['task_id'],)).fetchall()
        if len(active) == 1 and allowed(ctx.conn, task, active[0]):
            return dict(ok=True, deduplicated=True, task_status=task['status'], request=None)
    util.require(candidate(ctx.conn, task) == expected, '当前任务不符合继续交付条件或仍有活动请求', 'conflict')
    snapshot = _snapshot(ctx.conn, task)
    from . import parent_closeout, review_resolution, verification_recovery
    hashes = {}
    try:
        util.require(_exact_execution_coverage(ctx.conn, task, hashes),
                     '冻结验证命令缺少逐条通过证据', 'conflict')
        parent_closeout._report_bytes(task['review_report_path'], task['review_report_sha256'], hashes)
    except parent_closeout._Blocked as exc:
        raise util.KanpilotError('conflict', str(exc)) from exc
    core.require_committed_review(task)
    # The checked commit must also describe the currently clean working source.
    worktree = dict(core.delivery_of(task), repository=task['repository'])
    source_state = git_delivery.worktree_state_sha256(worktree)
    review_resolution._remote(task)
    verification_recovery._require_unchanged(hashes)
    core.require_committed_review(task)
    util.require(git_delivery.worktree_state_sha256(worktree) == source_state,
                 '核对期间已验证工作树发生变化', 'conflict')
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task['task_id'])
        util.require(_snapshot(ctx.conn, fresh) == snapshot and candidate(ctx.conn, fresh) == expected,
                     '核对期间任务、评审或交付身份已变化', 'conflict')
        extra, identity = db.task_extra(fresh), _pass(fresh)
        review = _review_request(ctx.conn, fresh, identity)
        receipt = _failed_delivery(ctx.conn, fresh)
        if receipt is None:
            receipt = dict(authorized_by='user', authorized_at=ctx.now_iso,
            task={key: fresh[key] for key in _TASK_KEYS},
            branch={key: core.delivery_of(fresh).get(key) for key in _BRANCH_KEYS},
            review_identity=identity, review_request_status=review['status'],
            review_result_sha256=_digest(util.jload(review['result_json'])),
            evidence_identity=_digest({key: extra.get(key) for key in ('engine_verdict','plan_verify','review_history')}))
            receipt['id'] = _digest(receipt)
        if fresh['status'] == 'needs_user':
            core.transition_task(ctx.conn, fresh, 'in_review', ctx.now_iso, 'user',
                detail=dict(via='continue_delivery', review_key=fresh['review_key']), **core.clear_block_fields())
        fresh = db.require_task(ctx.conn, task['task_id'])
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, mode='pr', next_sync_at=ctx.now_iso)
        db.set_extra(ctx.conn, db.require_task(ctx.conn, task['task_id']), ctx.now_iso, manual_review_delivery=receipt)
        request = enqueue(ctx.conn, db.require_task(ctx.conn, task['task_id']), ctx.now_iso, requested_by='user')
        db.add_event(ctx.conn, ctx.now_iso, 'resolved', task_id=task['task_id'], actor='user',
            detail=dict(action='continue_delivery', execution_run_id=task['run_id'], review_key=task['review_key']))
    return dict(ok=True, task_status='in_review', request=dict(request))
