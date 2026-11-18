"""Bounded, deterministic GitHub merge with durable, single-send receipts.

No model calls and no network under the SQLite writer lock. Unknown outcomes are
only reconciled through the ordinary PR read, never sent again.
"""
from pathlib import Path

from . import (automation_policy, constants, core, db, gh_client, git_delivery,
               project_config, runtime_operations, util)

TOOL = 'github_pr_merge'
HARNESS = 'github'


def snapshot(conn, task):
    if db.task_extra(task).get('build_failure_hold'):
        return None
    policy = automation_policy.policy_for(conn, task['project_id']) or {}
    if not policy.get('auto_merge_pr', False):
        return None
    _, _, mappings, _ = project_config.load_raw_mappings(conn)
    mapping = mappings.get(task['project_id']) or {}
    value = {'policy_revision': policy['policy_revision'], 'repository': mapping.get('repository')}
    check_policy(conn, task, value)
    return value


def check_policy(conn, task, expected):
    util.require(not db.task_extra(task).get('build_failure_hold'),
                 '构建修复仅授权创建新 PR，合并须用户另行确认', 'conflict')
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


_PASSING_CONCLUSIONS = ('SUCCESS', 'NEUTRAL', 'SKIPPED')
_MAX_CHECK_NAME = 120
_MAX_CHECK_WORKFLOW = 80
_MAX_REASON_LABELS = 2


def _check_label(check):
    name = gh_client.redact(check.get('name') or check.get('context') or '未命名检查')[:_MAX_CHECK_NAME]
    workflow = gh_client.redact(check.get('workflowName') or '')[:_MAX_CHECK_WORKFLOW]
    return workflow + '/' + name if workflow else name


def _label_text(labels):
    if len(labels) <= _MAX_REASON_LABELS:
        return '、'.join(labels)
    return '、'.join(labels[:_MAX_REASON_LABELS]) + ' 等 ' + str(len(labels)) + ' 项'


def _classify_checks(pr):
    """Point at the check that stops the merge instead of echoing a status code.

    Pending checks are normal progress and stay 'waiting_checks': GitHub also
    reports UNSTABLE while they run, so judging the status code first would start
    the long-blocked streak during a normal build. A failed check is a real
    'blocked' observation and names itself. The reason stays deterministic
    (sorted, deduplicated, no timestamps or observation counts) because the
    stall streak is keyed by the exact string.
    """
    checks = pr.get('statusCheckRollup')
    if not isinstance(checks, list):
        return 'waiting_checks', 'GitHub 检查结果未知，继续观察'
    failed, pending = [], []
    for check in checks:
        if not isinstance(check, dict):
            return 'waiting_checks', 'GitHub 检查结果未知，继续观察'
        typename = check.get('__typename')
        if typename == 'CheckRun':
            if check.get('status') != 'COMPLETED':
                pending.append(_check_label(check))
            elif check.get('conclusion') not in _PASSING_CONCLUSIONS:
                failed.append(_check_label(check))
        elif typename == 'StatusContext':
            if check.get('state') in ('PENDING', 'EXPECTED'):
                pending.append(_check_label(check))
            elif check.get('state') != 'SUCCESS':
                failed.append(_check_label(check))
        else:
            return 'waiting_checks', 'GitHub 检查类型未知，继续观察'
    failed, pending = sorted(set(failed)), sorted(set(pending))
    if failed:
        return 'blocked', '检查未通过：' + _label_text(failed) + '，修复或重跑后会自动合并'
    if pending:
        return 'waiting_checks', '检查仍在运行：' + _label_text(pending) + '，等待结果'
    return None


def eligibility(pr):
    """Explicit allow-list; missing/unknown GitHub fields always stop the write."""
    if pr.get('isDraft') is not False:
        return 'blocked', 'PR 为草稿或草稿状态未知，等待在 GitHub 处理'
    if pr.get('mergeable') != 'MERGEABLE':
        return 'blocked', ('PR 存在合并冲突，需在 GitHub 处理' if pr.get('mergeable') == 'CONFLICTING'
                           else 'GitHub 尚未确认 PR 可合并，继续观察')
    checks = _classify_checks(pr)
    if checks:
        return checks
    if pr.get('reviewDecision') not in ('', 'APPROVED'):
        decision = gh_client.redact(pr.get('reviewDecision') or '')[:60]
        return 'blocked', ('GitHub 评审尚未通过（' + decision + '），等待在 GitHub 处理'
                           if decision else 'GitHub 评审要求尚未满足或状态未知，等待在 GitHub 处理')
    if pr.get('mergeStateStatus') != 'CLEAN':
        status = gh_client.redact(pr.get('mergeStateStatus') or '未知')[:100]
        return 'blocked', ('检查已通过，GitHub 合并状态仍未就绪（' + status + '），等待保护规则或评审放行'
                           if pr.get('statusCheckRollup') else
                           'GitHub 合并状态未就绪（' + status + '），暂无可读的检查结果，等待保护规则或重新读取')
    return None


def refresh_notice(conn, task, now, *, reader=None):
    """Read-only refresh of the merge-gate line for a parked delivery task.

    A task that stopped polling keeps its last notice forever, so its line would
    keep quoting an old status instead of the check that blocks it now. Recompute
    the same allow-list from the current PR and write only the read-side receipt:
    no GitHub write, no task status change, no stall reset.
    """
    delivery = core.delivery_of(task)
    number = delivery.get('pr_number')
    head = delivery.get('head_sha')
    if (not core.is_pr_delivery(task) or delivery.get('pr_state') != 'OPEN'
            or not isinstance(number, int) or number < 1
            or not isinstance(head, str) or not head):
        return None
    if (delivery.get('auto_merge') or {}).get('state') in ('unknown', 'merging', 'merged'):
        return None  # an unconfirmed merge receipt is never rewritten by a read
    repo = git_delivery.require_github_origin(task['repository'])
    pr = (reader or gh_client.pr_view)(repo, number, merge_checks=True)
    if pr.get('headRefOid') and pr['headRefOid'] != head:
        return None  # the reviewed head moved; the delivery head is still frozen
    blocked = eligibility(pr)
    state, reason = blocked if blocked else (
        'waiting_checks', '检查与保护规则已满足，等待下一轮交付同步合并')
    notice = _notice(state, reason, delivery, now)
    with db.write_txn(conn):
        core.update_delivery(conn, task, now, auto_merge=notice)
    return notice


def _identity(task, delivery, repo):
    return {'task_id': task['task_id'], 'run_id': task['run_id'],
            'review_key': task['review_key'], 'report_sha256': task['review_report_sha256'],
            'head_sha': delivery['head_sha'], 'merge_head_sha': core.delivery_merge_head(delivery),
            'repo': repo, 'pr_number': delivery['pr_number'],
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


# 长期未就绪的合并门：连续观测 N 次且持续 M 秒后升级为待处理，避免每五分钟空转。
STALL_MIN_OBSERVATIONS = 3


def stall(delivery, notice, now_dt, now_iso):
    """Return (evidence|None, escalate) for repeated not-ready merge observations.

    Only *blocked* notices count (waiting_checks is normal progress). The streak
    resets as soon as the reason, the head or the state changes, so a transient
    blip never escalates.
    """
    if not notice or notice.get('state') != 'blocked':
        return None, False
    previous = (delivery or {}).get('auto_merge_stall') or {}
    same = (previous.get('head_sha') == notice.get('head_sha')
            and previous.get('reason') == notice.get('reason'))
    if same and previous.get('since'):
        since = previous['since']
        count = int(previous.get('count') or 0) + 1
    else:
        since = now_iso
        count = 1
    value = {'head_sha': notice.get('head_sha'), 'reason': notice.get('reason'),
             'since': since, 'count': count}
    started = util.parse_ts(since)
    elapsed = (now_dt - started).total_seconds() if started else 0.0
    escalate = count >= STALL_MIN_OBSERVATIONS and elapsed >= constants.MERGE_STALL_SECONDS
    return value, escalate


def _not_sent(row, identity):
    if (row is None or row['status'] != 'rejected' or row['tool'] != TOOL
            or row['harness_id'] != HARNESS
            or row['request_sha256'] != runtime_operations.compute_request_sha256(identity)):
        return False
    result = util.jload(row['result_json']) or {}
    return result.get('not_sent') is True and result.get('intent') == identity


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
    if row and not _not_sent(row, identity):
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
    # Only a new intent or an exactly matching, proven-not-sent local rejection
    # can send. Abandoned prepared/unknown receipts remain read-only forever.
    with db.write_txn(ctx.conn):
        boundary()
        current = db.get_runtime_operation(ctx.conn, scope, op_id)
        if _unresolved(ctx.conn, scope) or current and not _not_sent(current, identity):
            return pr, _notice('unknown', '合并请求已有回执，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
        if current is None:
            runtime_operations.begin_operation(ctx.conn, scope, op_id, HARNESS, TOOL, identity,
                                               now=ctx.now_iso)
            db.update_runtime_operation(ctx.conn, scope, op_id, ctx.now_iso, result_json=util.jdump(identity))
        else:
            runtime_operations.complete_operation(ctx.conn, scope, op_id, status='prepared',
                                                   result=identity, now=ctx.now_iso)
    # Keep the last repository I/O before reserving an uncertain downstream
    # send. A local guard failure proves that this call never invoked pr_merge.
    try:
        verify_repository()
        boundary()
    except git_delivery.GitDeliveryError as exc:
        with db.write_txn(ctx.conn):
            boundary()
            if db.get_runtime_operation(ctx.conn, scope, op_id)['status'] == 'prepared':
                runtime_operations.complete_operation(ctx.conn, scope, op_id, status='rejected',
                    result={'intent': identity, 'not_sent': True, 'reason': exc.message}, now=ctx.now_iso)
        raise
    with db.write_txn(ctx.conn):
        boundary()
        if db.get_runtime_operation(ctx.conn, scope, op_id)['status'] != 'prepared':
            return pr, _notice('unknown', '合并回执已变化，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
        runtime_operations.mark_operation_unknown(ctx.conn, scope, op_id, now=ctx.now_iso)
    # The final local state guard remains after reservation; GitHub atomically
    # enforces the reviewed head SHA when the native merge command is sent.
    boundary()
    if db.get_runtime_operation(ctx.conn, scope, op_id)['status'] != 'unknown':
        return pr, _notice('unknown', '合并回执已变化，继续只读核对 PR，不会重复提交', delivery, ctx.now_iso)
    try:
        result = gh_client.pr_merge(repo, delivery['pr_number'], sha=core.delivery_merge_head(delivery))
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
