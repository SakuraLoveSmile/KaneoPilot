"""Bounded, deterministic GitHub merge with durable, single-send receipts.

No model calls and no network under the SQLite writer lock. Unknown outcomes are
only reconciled through the ordinary PR read, never sent again.
"""
from pathlib import Path

from . import automation_policy, core, db, gh_client, git_delivery, project_config, runtime_operations, util

TOOL = 'github_pr_merge'
HARNESS = 'github'


def snapshot(conn, task):
    policy = automation_policy.policy_for(conn, task['project_id']) or {}
    if not policy.get('auto_merge_pr', False):
        return None
    _, _, mappings, _ = project_config.load_raw_mappings(conn)
    mapping = mappings.get(task['project_id']) or {}
    value = {'policy_revision': policy['policy_revision'], 'repository': mapping.get('repository')}
    check_policy(conn, task, value)
    return value


def check_policy(conn, task, expected):
    policy = automation_policy.policy_for(conn, task['project_id']) or {}
    _, _, mappings, _ = project_config.load_raw_mappings(conn)
    repository = (mappings.get(task['project_id']) or {}).get('repository')
    util.require(policy.get('auto_merge_pr') is True
                 and policy.get('policy_revision') == expected['policy_revision']
                 and repository == expected['repository'],
                 '自动合并开关、策略或项目映射已变化', 'conflict')
    util.require(repository and task['repository']
                 and Path(repository).expanduser().resolve() == Path(task['repository']).expanduser().resolve(),
                 '项目映射仓库与任务仓库不一致，未提交自动合并', 'conflict')
    util.require(not db.paused(conn, 'delivery') and not db.task_auto_paused(task)
                 and db.task_is_active(task)
                 and not core.automation_of(task).get('manual_takeover', False),
                 '交付已暂停或任务已转为人工处理，未提交自动合并', 'conflict')


def eligibility(pr):
    """Explicit allow-list; missing/unknown GitHub fields always stop the write."""
    if pr.get('isDraft') is not False:
        return 'blocked', 'PR 为草稿或草稿状态未知，等待在 GitHub 处理'
    if pr.get('mergeable') != 'MERGEABLE':
        return 'blocked', ('PR 存在合并冲突，需在 GitHub 处理' if pr.get('mergeable') == 'CONFLICTING'
                           else 'GitHub 尚未确认 PR 可合并，继续观察')
    if pr.get('mergeStateStatus') != 'CLEAN':
        status = gh_client.redact(pr.get('mergeStateStatus') or '未知')[:100]
        return 'blocked', 'GitHub 合并状态未就绪（' + status + '），等待检查或保护规则放行'
    if pr.get('reviewDecision') not in ('', 'APPROVED'):
        return 'blocked', 'GitHub 评审要求尚未满足或状态未知，等待在 GitHub 处理'
    checks = pr.get('statusCheckRollup')
    if not isinstance(checks, list):
        return 'waiting_checks', 'GitHub 检查结果未知，继续观察'
    for check in checks:
        if not isinstance(check, dict):
            return 'waiting_checks', 'GitHub 检查结果未知，继续观察'
        name = gh_client.redact(check.get('name') or check.get('context') or '检查')[:120]
        if check.get('__typename') == 'CheckRun':
            if check.get('status') != 'COMPLETED':
                return 'waiting_checks', name + ' 尚未完成，继续观察'
            if check.get('conclusion') not in ('SUCCESS', 'NEUTRAL', 'SKIPPED'):
                return 'blocked', name + ' 未通过或结论未知，等待在 GitHub 处理'
        elif check.get('__typename') == 'StatusContext':
            if check.get('state') != 'SUCCESS':
                return ('waiting_checks' if check.get('state') in ('PENDING', 'EXPECTED') else 'blocked',
                        name + ' 尚未通过，继续观察')
        else:
            return 'waiting_checks', 'GitHub 检查类型未知，继续观察'
    return None


def _identity(task, delivery, repo):
    return {'task_id': task['task_id'], 'run_id': task['run_id'],
            'review_key': task['review_key'], 'report_sha256': task['review_report_sha256'],
            'head_sha': delivery['head_sha'], 'repo': repo, 'pr_number': delivery['pr_number'],
            'base': delivery['base_branch'], 'head': delivery['branch']}


def source_eligibility(task, live):
    item = live.get('task') or {}
    if (item.get('id') != task['task_id'] or item.get('projectId') != task['project_id']
            or item.get('status') != 'in-review' or not task['requirements_sha256']
            or live.get('requirements_sha256') != task['requirements_sha256']):
        return 'blocked', 'Kaneo 来源任务、项目、需求或评审状态已变化或不可确认，未提交自动合并；请重新核验'
    return None


def _unresolved(conn, scope):
    return conn.execute("SELECT * FROM runtime_operations WHERE scope_id=? "
                        "AND status IN ('prepared','unknown')", (scope,)).fetchall()


def _notice(state, reason, delivery, now):
    return {'state': state, 'reason': reason, 'head_sha': delivery['head_sha'], 'updated_at': now}


def _reject(ctx, scope, op_id, reason, boundary):
    with db.write_txn(ctx.conn):
        boundary()
        if db.get_runtime_operation(ctx.conn, scope, op_id)['status'] == 'accepted':
            return 'unknown', 'PR 合并已获观察确认，等待重新读取，不会重复提交'
        runtime_operations.complete_operation(ctx.conn, scope, op_id, status='rejected',
                                               result={'reason': reason}, now=ctx.now_iso)
    return 'blocked', reason


def reconcile(ctx, task, delivery, repo, pr, policy, boundary, verify_pr, verify_repository, verify_source):
    """Return (observed PR, display receipt). Caller persists delivery atomically.

    boundary rechecks leased stage identity, report bytes and (when enabled) the
    exact current policy/mapping at every I/O boundary.
    """
    identity = _identity(task, delivery, repo)
    scope = 'pr-merge:' + task['task_id']
    op_id = runtime_operations.compute_request_sha256(identity)
    row = db.get_runtime_operation(ctx.conn, scope, op_id)
    if row:
        util.require(row['request_sha256'] == runtime_operations.compute_request_sha256(identity)
                     and row['tool'] == TOOL and row['harness_id'] == HARNESS,
                     '自动合并回执身份不符', 'conflict')
    if pr['state'] == 'MERGED':
        pending = _unresolved(ctx.conn, scope)
        if row or pending:
            with db.write_txn(ctx.conn):
                boundary()
                if row:
                    runtime_operations.complete_operation(ctx.conn, scope, op_id, result=identity,
                                                           now=ctx.now_iso)
                for previous in pending:
                    original = util.jload(previous['result_json']) or {}
                    # A new Review is not a new send permission while the prior
                    # request is uncertain. Reconcile its original PR/head only.
                    if (previous['tool'] == TOOL and previous['harness_id'] == HARNESS
                            and all(original.get(key) == identity[key] for key in
                                    ('task_id', 'repo', 'pr_number', 'head_sha', 'head', 'base'))):
                        runtime_operations.complete_operation(ctx.conn, scope, previous['operation_id'],
                                                               result=original, now=ctx.now_iso)
        return pr, (_notice('merged', 'PR 已合并，等待完成同步', delivery, ctx.now_iso)
                    if row or pending or policy or delivery.get('auto_merge') else None)
    if _unresolved(ctx.conn, scope):
        return pr, _notice('unknown', '已有合并请求结果待确认，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
    if row:
        if row['status'] == 'rejected':
            result = util.jload(row['result_json']) or {}
            return pr, _notice('blocked', result.get('reason') or 'GitHub 已拒绝此次合并，请到 GitHub 处理；不会自动重发', delivery, ctx.now_iso)
        return pr, _notice('unknown', '合并结果待确认，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
    if not policy:
        return pr, (_notice('disabled', '项目未开启自动合并，等待 PR 合并', delivery, ctx.now_iso)
                    if delivery.get('auto_merge') else None)
    blocked = eligibility(pr)
    if blocked:
        return pr, _notice(*blocked, delivery, ctx.now_iso)
    source = verify_source()
    if source:
        return pr, _notice(*source, delivery, ctx.now_iso)
    verify_repository()
    # Only a newly inserted intent can send. An abandoned prepared receipt is
    # conservative unknown too; policy revisions do not create a fresh identity.
    with db.write_txn(ctx.conn):
        boundary()
        if db.get_runtime_operation(ctx.conn, scope, op_id) or _unresolved(ctx.conn, scope):
            return pr, _notice('unknown', '合并请求已有回执，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
        runtime_operations.begin_operation(ctx.conn, scope, op_id, HARNESS, TOOL, identity,
                                           now=ctx.now_iso)
        db.update_runtime_operation(ctx.conn, scope, op_id, ctx.now_iso, result_json=util.jdump(identity))
    with db.write_txn(ctx.conn):
        boundary()
        if db.get_runtime_operation(ctx.conn, scope, op_id)['status'] != 'prepared':
            return pr, _notice('unknown', '合并回执已变化，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
        runtime_operations.mark_operation_unknown(ctx.conn, scope, op_id, now=ctx.now_iso)
    # Last check immediately before sending; GitHub itself atomically enforces sha.
    verify_repository()
    boundary()
    if db.get_runtime_operation(ctx.conn, scope, op_id)['status'] != 'unknown':
        return pr, _notice('unknown', '合并回执已变化，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
    try:
        result = gh_client.pr_merge(repo, delivery['pr_number'], sha=delivery['head_sha'])
    except gh_client.GhError as exc:
        boundary()
        if exc.kind in ('rejected', 'auth', 'not_found', 'missing'):
            reason = 'GitHub 拒绝自动合并：' + gh_client.redact(exc.message) + '；请到 GitHub 处理，不会自动重发'
            return pr, _notice(*_reject(ctx, scope, op_id, reason, boundary), delivery, ctx.now_iso)
        return pr, _notice('unknown', '合并结果待确认，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
    boundary()
    if isinstance(result, dict) and result.get('merged') is False:
        reason = 'GitHub 拒绝自动合并：' + gh_client.redact(result.get('message') or '未获准合并') + '；请到 GitHub 处理，不会自动重发'
        return pr, _notice(*_reject(ctx, scope, op_id, reason, boundary), delivery, ctx.now_iso)
    if not isinstance(result, dict) or result.get('merged') is not True:
        return pr, _notice('unknown', '合并结果待确认，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
    try:
        confirmed = gh_client.pr_view(repo, delivery['pr_number'], merge_checks=True)
        boundary()
        verify_pr(confirmed)
    except (gh_client.GhError, git_delivery.GitDeliveryError):
        boundary()
        return pr, _notice('unknown', '合并已提交，结果待确认，不会重复提交', delivery, ctx.now_iso)
    if confirmed['state'] == 'MERGED':
        with db.write_txn(ctx.conn):
            boundary()
            runtime_operations.complete_operation(ctx.conn, scope, op_id, result=identity,
                                                   now=ctx.now_iso)
        return confirmed, _notice('merged', 'PR 已合并，等待完成同步', delivery, ctx.now_iso)
    return confirmed, _notice('unknown', '合并已提交，结果待确认，不会重复提交', delivery, ctx.now_iso)
