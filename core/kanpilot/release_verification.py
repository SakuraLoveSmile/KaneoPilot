"""Explicit, independently scoped Plus verification of an immutable release tree.

This module never runs project verification locally and never replaces execution
or Review evidence. Unknown writes are observed, never sent a second time.
"""
from pathlib import Path
import os
import re

from . import (constants, core, db, git_delivery,
               pi_desktop_worker as worker, release_delivery as release,
               round_store, runtime_operations as operations, util, verify_evidence)
from .runtime_client import HistoricalReadClient, RuntimeClient, stable_scope_id


TOOL = 'release_target_verification'
_require = release._require
_HISTORY_CONFLICT_REASON = '历史观察写回前原尝试或下游写状态已变化'


def _scope(task_id):
    return 'release-verify:' + task_id


def _op(verification_id):
    return 'verification:' + verification_id


def _hash(value):
    return util.sha256_text(util.jdump(value))


def _context(conn, task, frozen_target=None, *, restart=None):
    _require(not task['review_source_task_id'] and not conn.execute(
        'SELECT 1 FROM tasks WHERE review_source_task_id=? LIMIT 1',
        (task['task_id'],)).fetchone(), 'release_verification_group', '补验证仅支持无二级修复的独立任务')
    policy, mapping = release._policy(conn, task)
    _require(policy['enabled'] and mapping['repository'] == task['repository'],
             'release_repository_changed', '发布未开启或仓库映射已变化')
    _require(not db.paused(conn, 'delivery') and not db.task_auto_paused(task)
             and not core.automation_of(task).get('manual_takeover'),
             'release_paused', '交付已暂停或转为人工处理')
    proof = release._single_proof(conn, task, frozen_target=frozen_target,
                                  allow_unverified_target=True)
    row = round_store.require_round(conn, task['task_id'], task['run_id'])
    _require(row['backend'] == 'pi_desktop' and row['save_state'] == 'active'
             and row['run_status'] == 'finished',
             'release_verification_backend', '补验证需要当前已完成且未归档的 Plus 执行轮次')
    config = util.jload(row['config_json']) or {}
    _require(bool(config.get('connection_file'))
             and config.get('permission_mode') in constants.EXEC_PERMISSION_MODES,
             'release_verification_config', '原 Plus 执行配置缺少连接或权限模式')
    commands = proof['reused_evidence'][0]['commands']
    _require(bool(commands) and all(isinstance(c, str) and c.strip() for c in commands),
             'release_verification_commands', '冻结计划缺少逐字验证命令')
    context = dict(identity=release._identity(task), repository=task['repository'],
        github_repository=proof['github_repository'], target_sha=proof['target_sha'],
        target_source_sha256=proof['target_source_sha256'], commands=commands,
        runtime_config=config, original_evidence_sha256=proof['evidence_sha256'])
    if proof.get('external_build_repair'):
        context['external_build_repair']=proof['external_build_repair']
        source_bytes=Path(task['source_path']).read_bytes()
        plan_bytes=Path(task['plan_path']).read_bytes()
        _require(util.sha256_bytes(source_bytes)==proof['evidence_sha256'].get(task['source_path'])
            and util.sha256_bytes(plan_bytes)==task['plan_sha256'],
            'release_verification_changed','读取的冻结需求或计划正文已变化')
        source=source_bytes.decode('utf-8');plan=plan_bytes.decode('utf-8')
        context['original_materials']=dict(requirements=source,requirements_file_sha256=util.sha256_text(source),
            requirements_sha256=task['requirements_sha256'],plan=plan,plan_sha256=task['plan_sha256'])
    if restart is not None:
        context['restart'] = restart
    context['context_sha256'] = _hash(context)
    return context


def _load(conn, task_id, verification_id):
    row = db.get_runtime_operation(conn, _scope(task_id), _op(verification_id))
    _require(row is not None and row['tool'] == TOOL,
             'release_verification_missing', '找不到此任务的独立验证回执')
    value = util.jload(row['result_json']) or {}
    _require(value.get('verification_id') == verification_id
             and value.get('context', {}).get('identity', {}).get('task_id') == task_id,
             'release_verification_identity', '独立验证回执身份不符')
    return value


def _save(conn, value, now):
    previous = db.get_runtime_operation(conn, _scope(value['context']['identity']['task_id']),
                                        _op(value['verification_id']))
    if previous is not None and previous['status'] == 'accepted':
        return  # Accepted evidence is immutable, including under concurrent sync.
    state = value['state']
    operations.complete_operation(conn, _scope(value['context']['identity']['task_id']),
        _op(value['verification_id']), status=('accepted' if state == 'verified' else
        'rejected' if state in ('blocked', 'failed') else 'unknown'),
        result=value, now=now, session_id=value.get('session_id'),
        turn_id=value.get('execution_turn_id') or value.get('planning_turn_id'),
        proposal_id=(value.get('proposal') or {}).get('id'))


def _view(value):
    context = value.get('context') or value
    config = context['runtime_config']
    return dict(state=value.get('state', 'prepared'), target_sha=context['target_sha'],
        target_source_sha256=context['target_source_sha256'], commands=context['commands'],
        context_sha256=context['context_sha256'],
        model_summary={key: config.get(key) for key in
            ('provider_id', 'model_id', 'thinking_level', 'permission_mode')},
        **{key: value[key] for key in ('verification_id', 'session_id', 'planning_turn_id',
            'execution_turn_id', 'reason', 'failure_code', 'report_sha256') if value.get(key) is not None})


def _result(value):
    return dict(verification_id=value['verification_id'], verification=_view(value))


def attempt_summary(conn, task):
    """SQL-only presentation of the exact current core-owned attempt."""
    result = dict(can_prepare_verification=False, can_sync_verification=False,
                  can_restart_verification=False)
    ref = db.task_extra(task).get('release_verification_ref') or {}
    if not ref:
        return result
    try:
        attempt = _load(conn, task['task_id'], ref['verification_id'])
        context = attempt['context']
        row = db.get_runtime_operation(conn, _scope(task['task_id']), _op(attempt['verification_id']))
        bound = (row['harness_id'] == 'core' and row['request_sha256'] == _hash(context)
            and context['identity'] == release._identity(task)
            and ref == dict(verification_id=attempt['verification_id'], context_sha256=context['context_sha256'])
            and row['status'] == ('accepted' if attempt['state'] == 'verified' else
                                 'rejected' if attempt['state'] in ('blocked', 'failed') else 'unknown'))
        if not bound:
            return result
        result['verification'] = _view(attempt)
        result['can_sync_verification'] = (attempt['state'] not in ('verified', 'blocked', 'failed')
            or _known_unsent_guard_block(conn, attempt) or _history_recoverable(conn, attempt))
        result['can_restart_verification'] = bool(attempt['state'] == 'blocked'
            and attempt.get('failure_code') == 'release_verification_report'
            and attempt.get('session_id') and attempt.get('execution_turn_id')
            and not conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND status IN ('prepared','unknown') LIMIT 1",
                (_approval_operation(attempt)[0],)).fetchone())
    except (util.KanpilotError, KeyError, TypeError):
        pass
    return result


def ordinary_summary(conn, task, blocked):
    """Local eligibility only; preparation still verifies the complete target."""
    extra = db.task_extra(task)
    delivery = core.delivery_of(task)
    history = extra.get('review_history') or []
    review = history[-1] if history else {}
    policy, mapping = release._policy(conn, task)
    execution = round_store.get_round(conn, task['task_id'], task['run_id'])
    config = util.jload(execution['config_json']) if execution is not None else {}
    commands = (extra.get('plan_verify') or {}).get('commands')
    if (not db.task_is_active(task) or task['status'] != 'in_review'
            or task['manual_recovery_required'] or task['review_source_task_id']
            or extra.get('external_build_repair_ref') or extra.get('build_failure_hold')
            or core.review_repair_of(task)
            or conn.execute('SELECT 1 FROM tasks WHERE review_source_task_id=? LIMIT 1', (task['task_id'],)).fetchone()
            or not policy['enabled'] or mapping['repository'] != task['repository']
            or db.paused(conn, 'delivery') or db.task_auto_paused(task)
            or core.automation_of(task).get('manual_takeover')
            or conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') AND kind!='delivery' LIMIT 1",
                            (task['task_id'],)).fetchone()
            or execution is None or execution['backend'] != 'pi_desktop'
            or execution['save_state'] != 'active' or execution['run_status'] != 'finished'
            or not config or not config.get('connection_file')
            or config.get('permission_mode') not in constants.EXEC_PERMISSION_MODES
            or not isinstance(commands, list) or not commands
            or not all(isinstance(command, str) and command.strip() for command in commands)
            or delivery.get('mode') != 'pr' or delivery.get('pr_state') != 'MERGED'
            or type(delivery.get('pr_number')) is not int or not delivery.get('merged_at')
            or extra.get('review_conclusion') != 'pass' or review.get('conclusion') != 'pass'
            or not all(review.get(key) == task[column] for key, column in
                (('execution_run_id', 'run_id'), ('review_key', 'review_key'),
                 ('report_sha256', 'review_report_sha256'), ('report_path', 'review_report_path')))
            or review.get('head_sha') != delivery.get('head_sha')):
        return None
    ref = extra.get('release_verification_ref') or {}
    if ref:
        result = attempt_summary(conn, task)
        attempt = result.get('verification')
        if attempt:
            stored = _load(conn, task['task_id'], ref['verification_id'])['context']
            if stored.get('runtime_config') != config or stored.get('commands') != commands:
                return None
        return result
    blockers = (blocked or {}).get('blockers') or []
    if not blockers or any(row.get('code') != 'release_target_unverified' for row in blockers):
        return None
    return dict(can_prepare_verification=True, can_sync_verification=False,
                can_restart_verification=False)


def _unchanged(conn, task, value):
    context = _context(conn, task, value['context']['target_sha'], restart=value['context'].get('restart'))
    _require(context == value['context'], 'release_verification_changed',
             '任务、原评审、执行配置或补验证目标身份已变化')
    _restart_lineage(conn, context)


def _restart_operation(restart):
    return 'restart:' + restart['verification_id'] + ':' + restart['report_sha256']


def _restart_origin(conn, context):
    restart = context.get('restart')
    if restart is None:
        return None
    _require(isinstance(restart, dict) and set(restart) == {
        'verification_id', 'context_sha256', 'report_sha256', 'report_body_sha256',
        'session_id', 'planning_turn_id', 'execution_turn_id', 'execution_id'},
        'release_verification_restart', '重启验证的来源身份不完整')
    task_id = context['identity']['task_id']
    old = _load(conn, task_id, restart['verification_id'])
    row = db.get_runtime_operation(conn, _scope(task_id), _op(restart['verification_id']))
    _require(old['state'] == 'blocked' and old.get('failure_code') == 'release_verification_report'
        and row['status'] == 'rejected' and row['harness_id'] == 'core'
        and row['request_sha256'] == _hash(old['context'])
        and old['context']['context_sha256'] == restart['context_sha256']
        and old['session_id'] == restart['session_id']
        and old['planning_turn_id'] == restart['planning_turn_id']
        and old['execution_turn_id'] == restart['execution_turn_id']
        and {key: item for key, item in old['context'].items() if key not in ('context_sha256', 'restart')}
            == {key: item for key, item in context.items() if key not in ('context_sha256', 'restart')},
        'release_verification_restart', '重启验证来源、原评审或冻结目标已变化')
    return old


def _restart_lineage(conn, context):
    old = _restart_origin(conn, context)
    if old is None:
        return
    restart = context['restart']; task_id = context['identity']['task_id']
    payload = dict(restart=restart, context_sha256=context['context_sha256'])
    authorization = db.get_runtime_operation(conn, _scope(task_id), _restart_operation(restart))
    _require(authorization is not None and authorization['status'] == 'accepted'
        and authorization['harness_id'] == 'core' and authorization['tool'] == 'release_verification_restart'
        and authorization['request_sha256'] == _hash(payload)
        and util.jload(authorization['result_json']) == payload,
        'release_verification_restart', '缺少核心核对报告后的精确重启授权回执')


def _restart_no_unknown_writes(conn, old):
    _require(not conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND status IN ('prepared','unknown') LIMIT 1",
                             (_approval_operation(old)[0],)).fetchone(),
             'release_verification_restart', '旧尝试仍有未知下游操作，只能对账')


def _git(path, *args):
    return git_delivery._value(list(args), path, 60)


def _source(value):
    path = Path(value['worktree_path'])
    _require(not path.is_symlink() and path.is_dir(),
             'release_verification_source', '独立验证工作树缺失或是符号链接')
    context = value['context']
    _require(_git(path, 'rev-parse', 'HEAD') == context['target_sha']
             and _git(path, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
             and Path(_git(path, 'rev-parse', '--show-toplevel')).resolve() == path.resolve(),
             'release_verification_source', '独立工作树 HEAD 或 detached 身份已变化')
    expected = context['target_source_sha256']
    _require(git_delivery.tree_source_sha256(path, context['target_sha']) == expected
             and git_delivery.index_source_sha256(path) == expected
             and git_delivery.expected_index_source_sha256(path) == expected,
             'release_verification_source', '独立工作树源码或索引与冻结目标不一致')
    raw = git_delivery.source_state_sha256(path)
    if value.get('source_state_sha256'):
        _require(raw == value['source_state_sha256'], 'release_verification_source',
                 '补验证期间源码字节发生变化')
    return raw


def _worktree_path(conn, value):
    # A core-created path already persisted for this attempt is immutable. This
    # also preserves attempts started before the unified worktree location.
    if value.get('worktree_path'):
        return Path(value['worktree_path'])
    context=value['context'];task_id=context['identity']['task_id']
    root=Path(os.environ.get('KANPILOT_RELEASE_WORKTREE_ROOT',
        str(Path.home() / 'Documents/Code/Project/Project Worktree')))
    return root / Path(context['repository']).name / ('release-verify-'+task_id+'-'+value['verification_id'])


def _worktree(conn, value, now):
    context = value['context']; task_id = context['identity']['task_id']
    path = _worktree_path(conn,value)
    directory = path.parent
    payload = dict(repository=context['repository'], path=str(path), target_sha=context['target_sha'])
    slot = operations.reserve_downstream(conn, _scope(task_id),
        'worktree:' + value['verification_id'], 'core', 'release_verify_worktree', payload)
    value['worktree_path'] = str(path)
    if not slot['send']:
        _require(slot['status'] == 'accepted', 'release_verification_worktree_unknown',
                 '建工作树的结果未知，保留现场，不重复执行')
        value['source_state_sha256'] = _source(value)
        return
    _require(not path.exists(), 'release_verification_worktree_exists', '独立工作树目录已存在，禁止覆盖')
    directory.mkdir(parents=True, exist_ok=True)
    repository = context['repository']; target = context['target_sha']
    try:
        _git(repository, 'cat-file', '-e', target + '^{commit}')
    except git_delivery.GitDeliveryError:
        _git(repository, 'fetch', '--no-tags', 'origin', target)
    _git(repository, 'worktree', 'add', '--detach', str(path), target)
    value['source_state_sha256'] = _source(value)
    with db.write_txn(conn):
        operations.complete_operation(conn, _scope(task_id), 'worktree:' + value['verification_id'],
            result=payload, now=now)
        _save(conn, value, now)


def _client(value, read_only=False, *, require_input=False):
    config = value['context']['runtime_config']
    client = RuntimeClient(role='execution', invocation_id='releaseverify_' + value['verification_id'],
        task_id=None, run_id=None, request_id=None, project_path=value['worktree_path'],
        connection_file=config['connection_file'], runtime_config=config, read_only=read_only)
    required = (constants.PI_DESKTOP_GOAL_REPORT_TOOLS + ('pi_agent_status', 'pi_turn_get', 'pi_plans_pending')
        if read_only else constants.PI_DESKTOP_DISPATCH_TOOLS + constants.PI_DESKTOP_GOAL_REPORT_TOOLS)
    if require_input:
        required += ('pi_asks_pending',) if read_only else constants.PI_DESKTOP_INPUT_TOOLS
    client.connect(required_tools=required)
    return client


def _historical_client(value):
    client = HistoricalReadClient(value['context']['runtime_config']['connection_file'])
    client.connect(required_tools=constants.PI_DESKTOP_GOAL_REPORT_TOOLS +
        ('pi_session_get', 'pi_agent_status', 'pi_turn_get', 'pi_plans_pending', 'pi_asks_pending'))
    return client


def _history_cas_conflict(value):
    """One legacy local race is retryable attribution, never an approval grant."""
    return bool(value.get('state') == 'blocked' and value.get('history_read_only') is True
        and not value.get('approval_sent') and not value.get('approval_observation')
        and value.get('failure_code') == 'release_verification_history'
        and value.get('reason') == _HISTORY_CONFLICT_REASON)


def _history_candidate(value):
    return bool(not value.get('approval_sent') and value.get('session_id') and value.get('planning_turn_id')
        and (value['state'] == 'planning' or value['state'] == 'blocked'
             and value.get('failure_code') == 'release_verification_proposal'
             or value.get('history_read_only') is True and value['state'] == 'unknown'
             or _history_cas_conflict(value)
             or (value.get('approval_observation') or {}).get('source') == 'plus_history'
             and value['state'] in ('running', 'unknown')))


def _history_recoverable(conn, value):
    """SQL-only qualification; live reads still recheck every identity."""
    scope = _approval_operation(value)[0]
    return bool(_history_candidate(value) and not conn.execute(
        "SELECT 1 FROM runtime_operations WHERE scope_id=? AND (status IN ('prepared','unknown') OR tool='kp_runtime_proposal_resolve') LIMIT 1",
        (scope,)).fetchone())


def _history_task_snapshot(task):
    """Keep verification inputs; delivery polling does not change their identity."""
    snapshot = dict(task)
    for key in ('updated_at', 'kaneo_checked_at', 'last_progress'):
        snapshot.pop(key, None)
    extra = db.task_extra(task)
    delivery = dict(extra.get('delivery') or {})
    for key in ('last_sync_at', 'next_sync_at', 'auto_merge', 'stage_contracts', 'stage_results', 'publish_failures'):
        delivery.pop(key, None)
    extra['delivery'] = delivery
    snapshot['extra_json'] = extra
    return snapshot


def _history_snapshot(conn, value, *, include_operations=True):
    """Local inputs only, reused before I/O and inside the short commit lock.

    As in _single_proof, delivery requests may run while this read-only check
    observes the original Goal. Their leases/results and display timestamps
    are not frozen verification inputs; task identities and receipts still are.
    """
    task_id = value['context']['identity']['task_id']
    task = db.require_task(conn, task_id)
    scopes = (_scope(task_id), _approval_operation(value)[0],
              'external-build-repair:' + task_id, 'release-project:' + task['project_id'])
    return _hash(dict(
        tasks=[_history_task_snapshot(row) for row in conn.execute(
            'SELECT * FROM tasks WHERE task_id=? OR review_source_task_id=? ORDER BY task_id', (task_id, task_id))],
        rounds=[dict(row) for row in conn.execute(
            'SELECT * FROM execution_rounds WHERE task_id=? ORDER BY run_id', (task_id,))],
        requests=[dict(row) for row in conn.execute(
            "SELECT * FROM requests WHERE task_id=? AND kind!='delivery' ORDER BY request_id", (task_id,))],
        settings=[dict(row) for row in conn.execute("SELECT * FROM settings WHERE key IN "
            "('project_mappings','paused_global','paused_stages','session_runs_root') ORDER BY key")],
        operations=([dict(row) for row in conn.execute(
            'SELECT * FROM runtime_operations WHERE scope_id IN (?,?,?,?) ORDER BY scope_id,operation_id', scopes)]
            if include_operations else [])))


def _save_sync(ctx, value, expected, local_snapshot):
    """Short local CAS shared by automatic and manual sync; no remote work."""
    task_id = value['context']['identity']['task_id']
    with db.write_txn(ctx.conn):
        latest = _load(ctx.conn, task_id, value['verification_id'])
        task = db.require_task(ctx.conn, task_id)
        if (util.jdump(latest) != expected or _history_snapshot(ctx.conn, latest, include_operations=False) != local_snapshot
                or (db.task_extra(task).get('release_verification_ref') or {}) != dict(
                    verification_id=value['verification_id'], context_sha256=value['context']['context_sha256'])):
            return latest
        _save(ctx.conn, value, ctx.now_iso)
    return None


def recover_current(ctx, *, limit=25):
    """Continue already-authorized auto attempts; never start/restart anything."""
    if any(db.paused(ctx.conn, stage) for stage in ('monitor','dispatch','delivery')):
        return []
    rows = ctx.conn.execute("SELECT t.*,r.result_json AS verification_result,r.request_sha256 AS verification_request,"
        "r.status AS verification_status "
        "FROM tasks t JOIN runtime_operations r ON r.scope_id='release-verify:'||t.task_id "
        "AND r.tool=? AND r.harness_id='core' AND r.status IN ('unknown','rejected') "
        "WHERE t.save_state='active' AND COALESCE(t.auto_advance_paused,0)=0 "
        "AND json_valid(t.extra_json) AND json_valid(r.result_json) "
        "AND COALESCE(json_extract(t.extra_json,'$.automation.manual_takeover'),0)=0 "
        "AND r.operation_id='verification:'||json_extract(t.extra_json,'$.release_verification_ref.verification_id') "
        "AND json_extract(r.result_json,'$.verification_id')=json_extract(t.extra_json,'$.release_verification_ref.verification_id') "
        "AND json_extract(r.result_json,'$.context.context_sha256')=json_extract(t.extra_json,'$.release_verification_ref.context_sha256') "
        "AND json_extract(r.result_json,'$.context.runtime_config.permission_mode')='auto' "
        "AND json_extract(r.result_json,'$.state') IN ('planning','running','unknown','blocked') "
        "ORDER BY r.updated_at,t.task_id", (TOOL,))
    candidates = []; seen = set()
    try:
        # Apply the full local qualification before limiting remote work. Stale
        # roots must not permanently starve current attempts behind them. Close
        # the read cursor before sync can update any selected root.
        for task in rows:
            if len(candidates) >= limit:
                break
            value = util.jload(task['verification_result']) or {}
            ref = db.task_extra(task).get('release_verification_ref') or {}
            context = value.get('context') or {}; verification_id = value.get('verification_id')
            if (task['task_id'] in seen or not db.task_schedulable(task) or core.automation_of(task).get('manual_takeover')
                    or context.get('runtime_config', {}).get('permission_mode') != 'auto'
                    or (value.get('state') not in ('planning','running','unknown')
                        or task['verification_status'] != 'unknown') and not _history_cas_conflict(value)
                    or not verification_id or context.get('identity') != release._identity(task)
                    or ref != dict(verification_id=verification_id, context_sha256=context.get('context_sha256'))
                    or context.get('context_sha256', '')[:32] != verification_id
                    or _hash({k:v for k,v in context.items() if k != 'context_sha256'}) != context.get('context_sha256')
                    or task['verification_request'] != _hash(context)):
                continue
            seen.add(task['task_id'])
            candidates.append((task['task_id'], verification_id))
    finally:
        rows.close()
    results = []
    for task_id, verification_id in candidates:
        try:
            result = cmd_sync_release_verification(ctx, dict(task_id=task_id, verification_id=verification_id), automatic=True)
            results.append(dict(task_id=task_id, **result))
        except (util.KanpilotError, git_delivery.GitDeliveryError, OSError, ValueError) as exc:
            results.append(dict(task_id=task_id, verification_id=verification_id,
                status='deferred', code=getattr(exc, 'code', 'release_verification_recovery'), reason=util.redact(str(exc))))
    return results


def _history_local_guard(conn, task, value):
    """History is attribution only; local immutable receipts establish ownership."""
    task = db.require_task(conn, task['task_id'])
    _unchanged(conn, task, value); _source(value)
    _require(type(value.get('prompt_contract_version', 1)) is int and value.get('prompt_contract_version', 1) in (1, 2, 3)
        and (not value.get('prompt_sha256') or value['prompt_sha256'] == util.sha256_text(_prompt(value))),
        'release_verification_history', '原 prompt 冻结版本或哈希不符')
    ref = db.task_extra(task).get('release_verification_ref') or {}
    root = db.get_runtime_operation(conn, _scope(task['task_id']), _op(value['verification_id']))
    current = _load(conn, task['task_id'], value['verification_id'])
    worktree = dict(repository=value['context']['repository'], path=value['worktree_path'],
                    target_sha=value['context']['target_sha'])
    receipt = db.get_runtime_operation(conn, _scope(task['task_id']), 'worktree:' + value['verification_id'])
    _require(ref == dict(verification_id=value['verification_id'], context_sha256=value['context']['context_sha256'])
        and root is not None and root['harness_id'] == 'core' and root['tool'] == TOOL
        and root['status'] in ('unknown', 'rejected') and root['request_sha256'] == _hash(value['context'])
        and not current.get('approval_sent') and current.get('session_id') == value['session_id']
        and current.get('planning_turn_id') == value['planning_turn_id']
        and current.get('worktree_path') == value['worktree_path']
        and receipt is not None and receipt['status'] == 'accepted' and receipt['harness_id'] == 'core'
        and receipt['tool'] == 'release_verify_worktree' and receipt['request_sha256'] == _hash(worktree)
        and util.jload(receipt['result_json']) == worktree,
        'release_verification_history', '历史续接缺少当前任务与冻结工作树的精确核心回执')
    _restart_no_unknown_writes(conn, value)
    scope = _approval_operation(value)[0]
    _require(not conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND tool='kp_runtime_proposal_resolve' LIMIT 1",
        (scope,)).fetchone(), 'release_verification_history', '已有下游审批回执，不能转作历史观察')
    for row in conn.execute('SELECT session_id FROM runtime_operations WHERE scope_id=? AND session_id IS NOT NULL', (scope,)):
        _require(row['session_id'] == value['session_id'], 'release_verification_history', '历史会话与原运行时作用域不符')


def _history_approval(conn, client, task, value):
    """Reconcile one original SubmitGoal through a complete, unextended session."""
    from .pi_desktop_controller import read_transcript, message_text
    _history_local_guard(conn, task, value)
    sid, planning = value['session_id'], value['planning_turn_id']
    messages, complete, detail = read_transcript(client, sid)
    if not complete and detail and detail.startswith('transcript_read_failed:'):
        raise util.KanpilotError('remote_unavailable', detail)
    _require(complete and messages and all(isinstance(m, dict) for m in messages),
        'release_verification_history', detail or '缺少完整原会话转录')
    users = [m for m in messages if m.get('role') == 'user']
    _require(len(users) == 1 and users[0].get('turnId') == planning
        and (isinstance(users[0].get('content'), str) or isinstance(users[0].get('content'), list)
             and all(isinstance(block, dict) and block.get('type') == 'text'
                     and isinstance(block.get('text'), str) for block in users[0]['content']))
        and message_text(users[0]) == _prompt(value),
        'release_verification_history', '原冻结 prompt 不符或会话含追加用户输入')
    goals = [m for m in messages if (m.get('toolName') or m.get('name')) == 'SubmitGoal']
    _require(len(goals) == 1, 'release_verification_history', '转录缺少唯一原 SubmitGoal')
    tool = goals[0]; output = tool.get('toolResult')
    details = output.get('details') if isinstance(output, dict) else None
    original = details.get('proposal') if isinstance(details, dict) else None
    args = tool.get('toolArgs')
    _require(tool.get('role') == 'tool' and tool.get('turnId') == planning
        and tool.get('toolStatus') == 'success' and tool.get('status') == 'complete'
        and isinstance(original, dict) and original.get('kind') == 'goal'
        and original.get('status') == 'pending' and type(original.get('version')) is int
        and original['version'] == 1 and original.get('sessionId') == sid
        and original.get('turnId') == planning and bool(tool.get('toolCallId'))
        and original.get('toolCallId') == tool['toolCallId']
        and isinstance(original.get('id'), str) and bool(original['id'])
        and (not value.get('proposal') or value['proposal'] == original)
        and isinstance(args, dict),
        'release_verification_history', '原 SubmitGoal 工具结果或规划身份不符')
    plans = client.plans_pending(sid)
    _require(isinstance(plans.get('plans'), list) and not plans['plans']
        and isinstance(plans.get('history'), list) and len(plans['history']) == 1,
        'release_verification_history', '原会话的 Goal 历史不唯一或仍有待批准提案')
    history = plans['history'][0]
    _require(isinstance(history, dict) and history.get('kind') == 'goal'
        and history.get('status') == 'approved' and history.get('action') == 'approve'
        and type(history.get('version')) is int and history['version'] >= original['version']
        and all(history.get(k) == original.get(k) for k in
            ('id','sessionId','turnId','toolCallId','title','question','markdown','plan','artifact'))
        and all(history.get(k) == v for k, v in original.items()
            if k not in ('status','version','updatedAt','expiresAt'))
        and history.get('targetPermissionMode') == value['context']['runtime_config']['permission_mode']
        and isinstance(history.get('executionId'), str) and bool(history['executionId'])
        and history.get('executionKind') == 'goal' and history.get('executionState') in ('pending','running','completed'),
        'release_verification_history', '批准历史的 Goal 内容、身份或执行配置已漂移')
    markdown = original.get('markdown'); artifact = original.get('artifact')
    _require(isinstance(markdown, str) and bool(markdown.strip()) and original.get('plan') == markdown
        and args.get('markdown') == markdown and isinstance(args.get('title'), str)
        and args['title'].strip() == original.get('title') and isinstance(args.get('question'), str)
        and args['question'].strip() == original.get('question')
        and isinstance(artifact, dict) and artifact.get('workspaceKind') == 'project' and type(artifact.get('sizeBytes')) is int
        and artifact['sizeBytes'] == len(markdown.encode('utf-8'))
        and artifact.get('sha256') == util.sha256_text(markdown),
        'release_verification_history', 'Goal 正文、工具参数或 artifact 哈希不一致')
    relative = artifact.get('relativePath')
    _require(isinstance(relative, str) and relative.startswith('.pi/goal/')
        and not Path(relative).is_absolute() and '..' not in Path(relative).parts,
        'release_verification_history', 'Goal artifact 路径不属于原工作树')
    root = Path(value['worktree_path']); path = root / relative
    _require(not any(p.is_symlink() for p in [path, *path.parents] if p == root or root in p.parents)
        and path.is_file() and path.resolve().is_relative_to(root.resolve())
        and path.read_bytes() == markdown.encode('utf-8'),
        'release_verification_history', 'Goal artifact 本地字节或路径已变化')
    planning_record = client.turn_get(sid, planning, require_identity=True)
    _require(worker._turn_identity_matches(planning_record, sid, planning)
        and worker._turn_status(planning_record) == 'completed',
        'release_verification_history', '原规划轮次尚未完成或身份不符')
    if value.get('approval_observation'):
        saved = value['approval_observation']
        _require(saved.get('source') == 'plus_history' and saved.get('proposal') == original
            and all(history.get(k) == v for k, v in saved.get('history', {}).items()
                if k not in ('version', 'updatedAt', 'executionState'))
            and history['version'] >= saved['history']['version']
            and (not value.get('report_sha256') or saved.get('transcript_sha256') == _hash(messages)),
            'release_verification_history', '已观察的原批准历史或完整转录已变化')
    return dict(source='plus_history', proposal=original, history=history,
        prompt_sha256=util.sha256_text(_prompt(value)), transcript_sha256=_hash(messages))


def _observed_report(conn, client, task, value):
    observation = _history_approval(conn, client, task, value)
    sid = value['session_id']; status = client.agent_status(sid); inner = status.get('status') or status
    turn = inner.get('currentTurnId')
    if turn == value['planning_turn_id'] and inner.get('isRunning') is False and observation['history'].get('executionState') == 'pending':
        _require(not value.get('execution_turn_id'), 'release_verification_history', '历史执行轮次退回原规划轮次')
        return observation, None, None
    _require(isinstance(turn, str) and bool(turn) and turn != value['planning_turn_id']
        and (not value.get('execution_turn_id') or value['execution_turn_id'] == turn),
        'release_verification_history', '历史批准缺少唯一原执行轮次')
    record = client.turn_get(sid, turn, require_identity=True)
    _require(worker._turn_identity_matches(record, sid, turn),
        'release_verification_history', '历史执行轮次身份不符')
    execution_id = observation['history']['executionId']
    if value.get('execution_id'):
        _require(value['execution_id'] == execution_id, 'release_verification_history', '历史执行身份已变化')
    if worker._turn_status(record) == 'running' and inner.get('isRunning') is True:
        return observation, turn, None
    _require(inner.get('isRunning') is False and worker._turn_status(record) == 'completed'
        and observation['history'].get('executionState') == 'completed',
        'release_verification_history', '原执行尚未完成或会话仍运行')
    report = worker._fetch_report_payload(client, sid, execution_id, observation['proposal']['id'], {turn})
    _require(report.get('state') not in ('pending','draft','not_found'),
        'release_verification_report_pending', '原执行已完成，等待 Plus 传递正式验证报告；不重新执行')
    _require(report.get('state') == 'ready' and report.get('integrity_kind') == 'structured'
        and report.get('execution_status') == 'completed' and report.get('turn_id') == turn,
        'release_verification_report', '原执行缺少身份完整的 structured/ready/completed 报告')
    goal = report['report'].get('goal') or {}; artifact = observation['proposal']['artifact']
    _require(goal.get('markdown') == observation['proposal']['markdown']
        and goal.get('contractHash') == artifact['sha256'] and goal.get('contractPath') == artifact['relativePath'],
        'release_verification_history', '报告 Goal 正文或 artifact 身份不符')
    _source(value)
    return observation, turn, report


def trusted_runtime_commands(conn, scope, args, *, sql_only=False):
    """Local-only authorization for the independent verification invocation.

    The runtime checks source and evidence before reservation, then calls again
    with sql_only inside its short transaction to recheck persisted authority.
    Caller supplied text and the model's proposed Goal never grant commands.
    """
    def require(ok):
        if not ok:
            raise util.KanpilotError('forbidden', '独立发布验证 runtime 身份或冻结授权不符')

    invocation = scope.invocation_id or ''
    require(re.fullmatch(r'releaseverify_[0-9a-f]{32}', invocation) is not None)
    verification_id = invocation[len('releaseverify_'):]
    require(scope.role == 'execution' and not any((scope.task_id, scope.run_id, scope.request_id))
            and scope.scope_id == stable_scope_id('execution', invocation_id=invocation)
            and args.get('scope_id') == scope.scope_id)
    rows = list(conn.execute('SELECT * FROM runtime_operations WHERE operation_id=? AND tool=?',
                             (_op(verification_id), TOOL)))
    require(len(rows) == 1)
    row = rows[0]; value = util.jload(row['result_json']) or {}
    context = value.get('context') or {}; identity = context.get('identity') or {}
    task_id = identity.get('task_id')
    require(bool(task_id) and row['scope_id'] == _scope(task_id) and row['harness_id'] == 'core'
            and row['status'] == 'unknown' and row['request_sha256'] == _hash(context)
            and value.get('verification_id') == verification_id
            and value.get('state') == 'unknown' and value.get('approval_sent') is True
            and value.get('approval_phase') == 'pre_reservation'
            and not value.get('execution_turn_id') and not value.get('execution_id'))
    context_hash = context.get('context_sha256')
    require(isinstance(context_hash, str) and context_hash[:32] == verification_id
            and _hash({key: item for key, item in context.items() if key != 'context_sha256'}) == context_hash)
    _restart_lineage(conn, context)
    task = db.require_task(conn, task_id)
    if value.get('automatic_recovery') is True:
        require(not db.paused(conn, 'monitor') and not db.paused(conn, 'dispatch'))
    ref = db.task_extra(task).get('release_verification_ref') or {}
    require(ref == dict(verification_id=verification_id, context_sha256=context_hash)
            and db.task_is_active(task) and task['status'] in ('in_review', 'done')
            and release._identity(task) == identity and task['repository'] == context.get('repository')
            and not task['review_source_task_id'] and not core.review_repair_of(task)
            and not conn.execute('SELECT 1 FROM tasks WHERE review_source_task_id=? LIMIT 1', (task_id,)).fetchone()
            and not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') AND kind!='delivery' LIMIT 1",
                                 (task_id,)).fetchone()
            and not db.paused(conn, 'delivery') and not db.task_auto_paused(task)
            and not core.automation_of(task).get('manual_takeover'))
    policy, mapping = release._policy(conn, task)
    require(policy['enabled'] and mapping['repository'] == task['repository'])
    if context.get('external_build_repair'):
        from . import external_build_repair as external
        linked=external.accepted(conn,task)
        require(linked is not None and context['external_build_repair']==dict(
            context_sha256=linked['context_sha256'],**linked['candidate']))
        external.require_policy(conn,task,linked)
    original = round_store.require_round(conn, task_id, task['run_id'])
    config = context.get('runtime_config') or {}
    require(original['backend'] == 'pi_desktop' and original['save_state'] == 'active'
            and original['run_status'] == 'finished' and util.jload(original['config_json']) == config
            and scope.runtime_config == {key: config[key] for key in constants.EXEC_PI_DESKTOP_KEYS if key in config}
            and bool(scope.connection_file) and bool(config.get('connection_file')))
    path = _worktree_path(conn,value)
    require(bool(scope.project_path) and value.get('worktree_path') == str(path))
    if not sql_only:
        require(Path(scope.connection_file).expanduser().resolve() == Path(config['connection_file']).expanduser().resolve()
                and Path(scope.project_path).expanduser().resolve() == path.resolve())
    worktree_op = db.get_runtime_operation(conn, _scope(task_id), 'worktree:' + verification_id)
    worktree_payload = dict(repository=context['repository'], path=str(path), target_sha=context['target_sha'])
    require(worktree_op is not None and worktree_op['status'] == 'accepted'
            and worktree_op['tool'] == 'release_verify_worktree' and worktree_op['harness_id'] == 'core'
            and worktree_op['request_sha256'] == _hash(worktree_payload)
            and util.jload(worktree_op['result_json']) == worktree_payload)
    evidence = context.get('original_evidence_sha256') or {}
    require(bool(evidence))
    if not sql_only:
        require(all(Path(name).is_file() and util.sha256_file(Path(name)) == digest
                    for name, digest in evidence.items()))
        _source(value)
    proposal = value.get('proposal') or {}
    require(proposal.get('kind') == 'goal' and proposal.get('status') == 'pending'
            and proposal.get('sessionId') == value.get('session_id')
            and proposal.get('turnId') == value.get('planning_turn_id')
            and args.get('action') == 'accept' and not args.get('goal_continuation')
            and args.get('operation_id') == 'proposal_' + str(proposal.get('id')) + '_accept'
            and args.get('target_permission_mode') == config.get('permission_mode')
            and all(args.get(target) == proposal.get(source) and proposal.get(source) is not None
                for source, target in (('id', 'proposal_id'), ('toolCallId', 'tool_call_id'),
                    ('sessionId', 'session_id'), ('turnId', 'turn_id'), ('version', 'version')))
            and type(proposal.get('version')) is int and proposal['version'] > 0)
    require(worker._pending_goal_contract({'plans': [proposal]}, value['session_id'], value['planning_turn_id']) is not None
            and row['session_id'] == value['session_id'] and row['turn_id'] == value['planning_turn_id']
            and row['proposal_id'] == proposal['id'])
    commands = context.get('commands')
    require(isinstance(commands, list) and bool(commands)
            and all(isinstance(command, str) and command.strip() for command in commands)
            and (db.task_extra(task).get('plan_verify') or {}).get('commands') == commands)
    return list(commands)


def _prompt(value):
    context = value['context']
    materials=context.get('original_materials')
    scope=('\n原需求（完整冻结正文）：\n'+materials['requirements']+'\n冻结计划（包含全部验收要求）：\n'+materials['plan']
        +'\nGoal 必须覆盖以上原需求、计划全部验收要求与整个目标源码；逐字验证命令全部通过只是必要条件。' if materials else '')
    command_contract = ('\n每次 Bash 工具调用的 command 必须与对应冻结命令完整逐字一致，checks.command 也必须相同。'
        '冻结命令原文已有的 export 保留原样；不得为其他命令添加 export、PATH、cd、包装或复合命令前缀。'
        '每次 Bash 都是独立 shell，前一次 export 不会继承；宿主 PATH 已配置，若仍缺环境请如实报告受阻，'
        '不要改写冻结命令或编造成功证据。' if value.get('prompt_contract_version', 1) in (2, 3) else '')
    evidence_contract = ('\n每条命令结果的 evidences.kind 必须为 kind="tool_result"；'
        'refId 使用工具结果提示的真实 toolCallId，不是工具名称 bash、Bash、commandShellId 或文件路径。'
        'checks.evidenceRefs 填该条 evidences.id。若 SubmitGoalReport 返回证据引用纠正错误，'
        '在本轮使用已经执行的真实结果纠正引用再提交，不为修正引用重跑命令；'
        '没有可核证结果时如实报告受阻。' if value.get('prompt_contract_version', 1) == 3 else '')
    return ('仅验证冻结的发布目标，不实施业务修改。请先提出一个 Goal 契约，批准后逐字运行下列每条命令，'
        '在结构化 Goal 报告 checks 中记录原命令、result 和整数 exitCode。'
        '只能执行验证及必要依赖解析（例如离线 flutter pub get）；宿主自行创建的 .pi/** Goal附件、'
        '项目既定的生成缓存和构建产物允许出现，不属于业务源码；保留原始 git status 输出，'
        '按 HEAD、index 和业务源码判定变化，不要添加要求 git status 全空的验收条件。'
        '禁止模型主动编辑宿主 .pi 元数据。报告 evidenceRefs 引用本报告 evidences 内定义的证据 id；'
        '工具证据的 refId 必须填写本轮实际存在的 toolCallId，不得编造引用。'
        '禁止修改业务源码、计划、Review，'
        '禁止合并、推送或发布。源码变化即停止并报告失败。\n'
        '目标 SHA：' + context['target_sha'] + '\n上下文 SHA：' + context['context_sha256']
        + '\n上下文 SHA 是核心冻结记录指纹，不是 Git 对象，不要对它执行 git cat-file 检查。'
        + scope + command_contract + evidence_contract + '\n验证命令（逐字）：\n' + '\n'.join(context['commands']))


def cmd_prepare_release_verification(ctx, payload):
    task = db.require_task(ctx.conn, payload['task_id'])
    context = _context(ctx.conn, task)
    return dict(task_id=task['task_id'],context_sha256=context['context_sha256'], verification=_view(context))


def cmd_start_release_verification(ctx, payload):
    task = db.require_task(ctx.conn, payload['task_id'])
    context = _context(ctx.conn, task)
    _require(payload.get('expected_context_sha256') == context['context_sha256'],
             'release_verification_changed', '补验证确认上下文已变化，请重新准备')
    return _start_context(ctx, task, context)


def _start_context(ctx, task, context, *, previous_ref=None):
    verification_id = context['context_sha256'][:32]
    value = dict(verification_id=verification_id, context=context, state='unknown', prompt_contract_version=3)
    value['prompt_sha256'] = util.sha256_text(_prompt(value))
    with db.write_txn(ctx.conn):
        live_task = db.require_task(ctx.conn, task['task_id'])
        if previous_ref is not None:
            current_ref = db.task_extra(live_task).get('release_verification_ref') or {}
            next_ref = dict(verification_id=verification_id, context_sha256=context['context_sha256'])
            if current_ref == next_ref:
                return _result(_load(ctx.conn, task['task_id'], verification_id))
            _require(current_ref == previous_ref, 'release_verification_restart', '当前验证尝试已变化，不能覆盖其他尝试')
            old = _restart_origin(ctx.conn, context)
            _require(old is not None and release._identity(live_task) == context['identity'],
                     'release_verification_restart', '重启预约前任务或原验证身份已变化')
            _restart_no_unknown_writes(ctx.conn, old)
        slot = operations.begin_operation(ctx.conn, _scope(task['task_id']), _op(verification_id),
            'core', TOOL, context)
        if slot['status'] != 'prepared':
            return _result(_load(ctx.conn, task['task_id'], verification_id))
        # Reserve the unique send and persist its context in the same commit.
        # A crash can never expose an unknown receipt without its identity.
        _save(ctx.conn, value, ctx.now_iso)
        if context.get('restart') is not None:
            restart_payload = dict(restart=context['restart'], context_sha256=context['context_sha256'])
            operations.begin_operation(ctx.conn, _scope(task['task_id']), _restart_operation(context['restart']),
                'core', 'release_verification_restart', restart_payload)
            operations.complete_operation(ctx.conn, _scope(task['task_id']), _restart_operation(context['restart']),
                result=restart_payload, now=ctx.now_iso)
        db.set_extra(ctx.conn, live_task, ctx.now_iso,
            release_verification_ref=dict(verification_id=verification_id, context_sha256=context['context_sha256']))
    try:
        _worktree(ctx.conn, value, ctx.now_iso)
        _unchanged(ctx.conn, db.require_task(ctx.conn, task['task_id']), value)
        client = _client(value)
        _require(client.supports_required_verification_commands(),
                 'release_verification_unsupported', 'Plus 不支持冻结的 required verification commands')
        config = context['runtime_config']
        session = client.session_create(value['worktree_path'], mode=constants.PI_DESKTOP_EXEC_MODE,
            provider_id=config.get('provider_id'), model_id=config.get('model_id'),
            thinking_level=config.get('thinking_level'),
            title='发布目标补验证 ' + task['task_id'], project_name=task['project_name'])
        value['session_id'] = session['session']['id']
        with db.write_txn(ctx.conn):
            _save(ctx.conn, value, ctx.now_iso)
        client.session_configure(value['session_id'], mode=constants.PI_DESKTOP_EXEC_MODE,
            permission_mode=config['permission_mode'])
        sent = client.agent_prompt(value['session_id'], _prompt(value))
        value['planning_turn_id'] = sent.get('turnId')
        value['state'] = 'planning' if value['planning_turn_id'] else 'unknown'
    except (util.KanpilotError, git_delivery.GitDeliveryError, OSError, ValueError) as exc:
        value.update(state='unknown', reason=util.redact(str(exc)))
    with db.write_txn(ctx.conn):
        _save(ctx.conn, value, ctx.now_iso)
    return _result(value)


def _advanced_approval(conn, value, history):
    """Execution refreshes increment Plus proposal versions after approval.

    Such history is trusted only through the exact accepted runtime write and
    its immutable proposal content, never through a lower-bound version check.
    """
    original = value['proposal']; sid = value['session_id']
    scope_id, operation_id = _approval_operation(value)
    row = db.get_runtime_operation(conn, scope_id, operation_id)
    config = value['context']['runtime_config']; commands = value['context']['commands']
    arguments = dict(scope_id=scope_id, operation_id=operation_id, session_id=sid,
        turn_id=value['planning_turn_id'], proposal_id=original['id'], tool_call_id=original['toolCallId'],
        version=original['version'], action='accept', target_permission_mode=config['permission_mode'],
        required_verification_commands=commands)
    _require(row is not None and row['status'] == 'accepted' and row['harness_id'] == 'pi_desktop'
        and row['tool'] == 'kp_runtime_proposal_resolve' and row['request_sha256'] == _hash(arguments)
        and row['session_id'] == sid and row['proposal_id'] == original['id'],
        'release_verification_approval', '更新后的 Goal history 缺少精确已接受审批回执')
    result = util.jload(row['result_json']) or {}; detail = result.get('detail') or {}
    approved = detail.get('proposal') or {}; execution = detail.get('execution') or {}
    _require(result.get('resolved') is True and result.get('action') == 'accept'
        and result.get('session_id') == sid and result.get('proposal_id') == original['id']
        and detail.get('ok') is True and detail.get('action') == 'approve'
        and detail.get('targetPermissionMode') == config['permission_mode']
        and approved.get('status') == 'approved' and approved.get('action') == 'approve'
        and type(approved.get('version')) is int
        and approved['version'] in (original['version'], original['version'] + 1)
        and history['version'] >= approved['version']
        and approved.get('requiredVerificationCommands') == commands
        and history.get('requiredVerificationCommands') == commands
        and approved.get('targetPermissionMode') == config['permission_mode']
        and isinstance(original.get('artifact'), dict) and bool(original['artifact'].get('sha256'))
        and any(original.get(key) for key in ('markdown', 'plan')),
        'release_verification_approval', '已接受 Goal 的身份、命令或原始内容证据不完整')
    # Approval changes pending/status metadata and execution refreshes only
    # change their state/version/timestamp. Preserve every original content field.
    _require(all(approved.get(key) == item and history.get(key) == item
        for key, item in original.items() if key not in ('status', 'version', 'updatedAt'))
        and all(history.get(key) == item for key, item in approved.items()
                if key not in ('version', 'updatedAt', 'executionState'))
        and bool(execution.get('id')) and execution['id'] == approved.get('executionId')
        and execution.get('proposalId') == original['id'] and execution.get('sessionId') == sid
        and execution.get('requiredVerificationCommands') == commands
        and execution.get('artifact') == original['artifact']
        and execution.get('targetPermissionMode') == config['permission_mode']
        and (not value.get('execution_id') or value['execution_id'] == execution['id']),
        'release_verification_approval', 'Goal history 的内容、artifact 或执行身份与已接受审批不符')
    return execution['id']


def _approved(conn, client, value, *, require_receipt=False):
    proposal = value.get('proposal') or {}; sid = value['session_id']
    rows = [item for item in client.plans_pending(sid).get('history') or []
        if isinstance(item, dict) and item.get('kind') == 'goal' and item.get('status') == 'approved'
        and item.get('id') == proposal.get('id') and item.get('sessionId') == sid
        and item.get('turnId') == value['planning_turn_id']
        and item.get('toolCallId') == proposal.get('toolCallId')
        and type(item.get('version')) is int]
    _require(len(rows) == 1, 'release_verification_approval', '缺少本轮唯一已批准 Goal 身份')
    history = rows[0]
    if require_receipt:
        _require(value.get('approval_sent') is True and history.get('executionState') == 'completed',
            'release_verification_approval', '重启恢复缺少原审批发送或执行完成历史')
    if require_receipt or history['version'] not in (proposal.get('version'), proposal.get('version', 0) + 1):
        return _advanced_approval(conn, value, history)
    return None


def _restart_report_after_runtime_reset(conn, client, value, status):
    """Recover restart eligibility only; this never establishes verified proof."""
    from .pi_desktop_controller import read_transcript, message_text
    sid, planning, turn = (value.get(key) for key in ('session_id', 'planning_turn_id', 'execution_turn_id'))
    proposal = value.get('proposal') or {}
    _require(value.get('state') == 'blocked' and value.get('failure_code') == 'release_verification_report'
        and all(isinstance(value.get(key), str) and value[key] for key in
            ('session_id', 'planning_turn_id', 'execution_turn_id', 'execution_id', 'report_id'))
        and planning != turn and isinstance(proposal, dict)
        and bool(proposal.get('id')) and bool(proposal.get('toolCallId'))
        and all(isinstance(value.get(key), str) and re.fullmatch('[0-9a-f]{64}', value[key])
            for key in ('report_sha256', 'report_body_sha256'))
        and isinstance(value.get('report_payload'), dict),
        'release_verification_restart', '重启恢复缺少原失败报告的完整身份与字节')
    def idle(inner):
        return (inner.get('sessionId') == sid and inner.get('isRunning') is False
            and inner.get('currentTurnId') is None
            and not inner.get('pendingPermission')
            and type(inner.get('pendingToolConfirmations', 0)) is int
            and inner.get('pendingToolConfirmations', 0) == 0)
    _require(idle(status), 'release_verification_turn', '重启恢复的会话仍运行、待确认或身份不符')
    _require(type(value.get('prompt_contract_version', 1)) is int
        and value.get('prompt_contract_version', 1) in (1, 2, 3)
        and (not value.get('prompt_sha256') or value['prompt_sha256'] == util.sha256_text(_prompt(value))),
        'release_verification_history', '原 prompt 冻结版本或哈希不符')
    messages, complete, detail = read_transcript(client, sid)
    _require(complete and messages and all(isinstance(m, dict)
        and isinstance(m.get('turnId'), str) and m['turnId'] in (planning, turn) for m in messages)
        and messages[-1]['turnId'] == turn,
        'release_verification_history', detail or '重启恢复缺少完整原规划及执行归属')
    users = [m for m in messages if m.get('role') == 'user']
    _require(len(users) == 1 and users[0].get('turnId') == planning
        and (isinstance(users[0].get('content'), str) or isinstance(users[0].get('content'), list)
            and all(isinstance(block, dict) and block.get('type') == 'text'
                and isinstance(block.get('text'), str) for block in users[0]['content']))
        and message_text(users[0]) == _prompt(value),
        'release_verification_history', '原冻结 prompt 不符或会话含追加用户输入')
    goals = [m for m in messages if (m.get('toolName') or m.get('name')) == 'SubmitGoal']
    _require(len(goals) == 1, 'release_verification_history', '转录缺少唯一原 SubmitGoal')
    tool = goals[0]; output = tool.get('toolResult')
    details = output.get('details') if isinstance(output, dict) else None
    _require(tool.get('role') == 'tool' and tool.get('turnId') == planning
        and tool.get('toolStatus') == 'success' and tool.get('status') == 'complete'
        and tool.get('toolCallId') == proposal['toolCallId']
        and isinstance(details, dict) and details.get('proposal') == proposal,
        'release_verification_history', '原 SubmitGoal 工具结果或规划身份不符')
    if (value.get('approval_observation') or {}).get('source') == 'plus_history':
        task = db.require_task(conn, value['context']['identity']['task_id'])
        observation = _history_approval(conn, client, task, value)
        execution_id = observation['history']['executionId']
        _require(observation['history'].get('executionState') == 'completed'
            and execution_id == value['execution_id'],
            'release_verification_history', '原历史执行未完成或执行身份已变化')
    else:
        execution_id = _approved(conn, client, value, require_receipt=True)
    record = client.turn_get(sid, turn, require_identity=True)
    _require(worker._turn_identity_matches(record, sid, turn)
        and worker._turn_status(record) == 'completed'
        and worker._strict_turn_time(record.get('endedAt')) is not None,
        'release_verification_turn', '原执行未完成、缺少结束时间或身份不符')
    report = worker._fetch_report_payload(client, sid, execution_id, proposal['id'], {turn})
    _require(report.get('state') == 'ready' and report.get('integrity_kind') == 'structured'
        and report.get('execution_status') == 'completed' and report.get('turn_id') == turn
        and report.get('execution_id') == value['execution_id']
        and (report.get('report_id') or report['report'].get('reportId')) == value['report_id']
        and report.get('report_sha256') == value['report_sha256']
        and _hash(report['report']) == value['report_body_sha256']
        and report.get('raw') == value['report_payload'],
        'release_verification_report', '原报告身份、哈希或原始正文已变化')
    current = client.agent_status(sid); current = current.get('status') or current
    _require(idle(current), 'release_verification_turn', '重启恢复读取期间会话状态已变化')
    _source(value)
    return report


def _completed_report(conn, client, value, *, for_restart=False):
    if for_restart:
        status = client.agent_status(value['session_id']); inner = status.get('status') or status
        if inner.get('isRunning') is False and inner.get('currentTurnId') is None:
            return _restart_report_after_runtime_reset(conn, client, value, inner)
    if (value.get('approval_observation') or {}).get('source') == 'plus_history':
        task = db.require_task(conn, value['context']['identity']['task_id'])
        _observation, _turn, report = _observed_report(conn, client, task, value)
        _require(report is not None, 'release_verification_report', '原历史执行仍未完成')
        _require(not value.get('report_sha256') or report['report_sha256'] == value['report_sha256']
            and _hash(report['report']) == value.get('report_body_sha256')
            and report.get('raw') == value.get('report_payload'),
            'release_verification_report', '原历史报告身份、哈希或原始正文已变化')
        return report
    sid = value['session_id']; turn = value['execution_turn_id']
    approved_execution = _approved(conn, client, value)
    status = client.agent_status(sid); inner = status.get('status') or status
    _require(inner.get('isRunning') is False and inner.get('currentTurnId') == turn,
             'release_verification_turn', '独立验证会话仍运行或当前执行轮次已变化')
    record = client.turn_get(sid, turn, require_identity=True)
    _require(worker._turn_identity_matches(record, sid, turn) and worker._turn_status(record) == 'completed',
             'release_verification_turn', '独立验证执行轮次未完成或身份不符')
    report = worker._fetch_report_payload(client, sid, value.get('execution_id') or approved_execution, value['proposal']['id'], {turn})
    _require(report.get('state') not in ('pending','draft','not_found'),
             'release_verification_report_pending', '原执行已完成，等待 Plus 传递正式验证报告；不重新执行')
    _require(report.get('state') == 'ready' and report.get('integrity_kind') == 'structured'
             and report.get('execution_status') == 'completed'
             and report.get('turn_id') == turn and bool(report.get('execution_id')),
             'release_verification_report', '缺少本轮 structured/ready/completed 报告')
    _source(value)
    return report


def _report(conn, client, value, *, report=None):
    report = report if report is not None else _completed_report(conn, client, value)
    _require(report.get('verdict') == 'met', 'release_verification_report', '本轮报告未满足全部 Goal 条件')
    body = report['report']
    _require(all(isinstance(item, dict) and item.get('verdict') == 'met' for item in body.get('criteria') or [])
             and all(isinstance(item, dict) and item.get('status') == 'completed' for item in body.get('steps') or [])
             and not (body.get('execution') or {}).get('errorCode'),
             'release_verification_report', '验证报告含未满足条件、未完成步骤或执行错误')
    checks = body.get('checks') or []
    # A later failed run supersedes an earlier pass of that exact command.
    # A differently spelled normalized equivalent cannot erase that failure.
    latest = [[item for item in checks if isinstance(item, dict) and item.get('command') == command]
              for command in value['context']['commands']]
    _require(verify_evidence.match_goal_checks(value['context']['commands'], checks)['ok']
        and all(rows and rows[-1].get('result') == 'passed' and type(rows[-1].get('exitCode')) is int
                and rows[-1]['exitCode'] == 0 for rows in latest),
        'release_verification_checks', '冻结计划的逐字验证命令没有全部通过并记录退出码 0')
    _host_evidence(body, value['context']['commands'])
    _require(not value.get('approval_observation'), 'release_verification_report',
        '历史观察不授予验证通过；缺少本项目精确审批回执及冻结命令绑定')
    _source(value)
    return dict(report_sha256=report['report_sha256'], report_body_sha256=_hash(body),
                execution_id=report['execution_id'], checks=checks)


def _record_report(value, report):
    value.update(report_sha256=report['report_sha256'], report_body_sha256=_hash(report['report']),
        report_id=report.get('report_id') or report['report'].get('reportId'),
        execution_id=report['execution_id'], report_payload=report.get('raw'))


def _host_evidence(body, commands):
    """Host-owned observations prove native Bash exits, not model assertions.

    Resolution only locates recorded IDs; a Bash callId may be unresolved even
    when the Host independently proves its exact command and exit status.
    """
    checks = body.get('checks'); observations = body.get('checkObservations')
    evidences = body.get('evidences'); resolutions = body.get('evidenceResolution')
    def require(ok, detail):
        _require(ok, 'release_verification_report', '验证证据不足：' + detail)
    require(all(isinstance(rows, list) for rows in (checks, observations, evidences, resolutions)),
        '缺少 Host checkObservations 或 evidenceResolution')
    require(all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] for row in checks)
        and len({row['id'] for row in checks}) == len(checks), 'check 身份缺失或重复')
    require(all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] for row in evidences)
        and len({row['id'] for row in evidences}) == len(evidences), '证据身份缺失或重复')
    evidence_by_id = {row['id']: row for row in evidences}
    require(all(isinstance(row, dict) and isinstance(row.get('evidenceId'), str) and row['evidenceId'] in evidence_by_id
        and row.get('state') in ('recorded', 'unresolved') for row in resolutions)
        and len(resolutions) == len(evidences)
        and len({row['evidenceId'] for row in resolutions}) == len(resolutions),
        'Host evidenceResolution 缺项、重复或身份不符')
    require(all(isinstance(row, dict) and isinstance(row.get('checkId'), str) and row['checkId'] in {check['id'] for check in checks}
        for row in observations) and len({row['checkId'] for row in observations}) == len(observations),
        'Host checkObservations 身份重复或不符')
    for command in commands:
        rows = [row for row in checks if row.get('command') == command]
        require(len(rows) == 1, '冻结命令缺少唯一 check：' + command)
        check = rows[0]; refs = check.get('evidenceRefs')
        require(isinstance(refs, list) and bool(refs) and all(isinstance(ref, str) for ref in refs)
            and len(set(refs)) == len(refs) and all(ref in evidence_by_id for ref in refs)
            and any(evidence_by_id[ref].get('kind') == 'tool_result'
                and isinstance(evidence_by_id[ref].get('refId'), str) and evidence_by_id[ref]['refId'].strip()
                for ref in refs), '冻结命令缺少真实工具证据引用：' + command)
        matches = [row for row in observations if row.get('checkId') == check['id']]
        require(len(matches) == 1 and matches[0].get('command') == command
            and matches[0].get('result') == 'passed' and type(matches[0].get('exitCode')) is int
            and matches[0]['exitCode'] == 0 and matches[0].get('evidenceIds') == refs,
            'Host 未确认原命令通过及整数退出码 0：' + command)


def cmd_prepare_restart_release_verification(ctx,payload):
    task=db.require_task(ctx.conn,payload['task_id'])
    ref=db.task_extra(task).get('release_verification_ref') or {}
    _require(ref.get('verification_id'),'release_verification_restart','没有当前独立验证可重新准备')
    old=_load(ctx.conn,task['task_id'],ref['verification_id'])
    _require(old['state']=='blocked' and old.get('failure_code')=='release_verification_report',
             'release_verification_restart','仅允许已完成报告未满足条件的当前独立验证显式重启')
    client=(_historical_client(old) if old.get('approval_observation') else _client(old,read_only=True,require_input=True))
    report=_completed_report(ctx.conn,client,old,for_restart=True)
    expected=dict(expected_verification_id=old['verification_id'],
        expected_context_sha256=old['context']['context_sha256'],expected_report_sha256=report['report_sha256'])
    context=_restart_context(ctx,dict(task_id=task['task_id'],**expected),prepare_only=True)
    return dict(task_id=task['task_id'],context_sha256=context['context_sha256'],verification=_view(context),**expected)


def cmd_restart_release_verification(ctx,payload):
    return _restart_context(ctx,payload)


def _restart_context(ctx, payload, *, prepare_only=False):
    """An explicit new attempt; never amend or promote the previous report."""
    for key, size in (('expected_verification_id', 32), ('expected_context_sha256', 64), ('expected_report_sha256', 64)):
        _require(isinstance(payload.get(key), str) and re.fullmatch('[0-9a-f]{' + str(size) + '}', payload[key]),
                 'release_verification_restart', '重启需要原验证、上下文及报告的精确 SHA 身份')
    task = db.require_task(ctx.conn, payload['task_id'])
    old = _load(ctx.conn, task['task_id'], payload['expected_verification_id'])
    _require(old['state'] == 'blocked' and old.get('failure_code') == 'release_verification_report'
        and old['context']['context_sha256'] == payload['expected_context_sha256']
        and old.get('session_id') and old.get('execution_turn_id'),
        'release_verification_restart', '仅允许已完成报告未满足条件的当前独立验证显式重启')
    previous_ref = dict(verification_id=old['verification_id'], context_sha256=old['context']['context_sha256'])
    ref = db.task_extra(task).get('release_verification_ref') or {}
    # A completed authorization and new root were committed together before any
    # external operation. Replay returns that attempt even after the ref moved.
    key = 'restart:' + old['verification_id'] + ':' + payload['expected_report_sha256']
    existing = db.get_runtime_operation(ctx.conn, _scope(task['task_id']), key)
    if existing is not None:
        _require(not prepare_only,'release_verification_restart','当前重启已建立，改为同步现有尝试')
        saved = util.jload(existing['result_json']) or {}; restart = saved.get('restart') or {}
        _require(restart.get('verification_id') == old['verification_id']
            and restart.get('context_sha256') == payload['expected_context_sha256']
            and restart.get('report_sha256') == payload['expected_report_sha256']
            and isinstance(saved.get('context_sha256'), str),
            'release_verification_restart', '原重启授权与本次确认身份不符')
        new = _load(ctx.conn, task['task_id'], saved['context_sha256'][:32])
        _require(ref == dict(verification_id=new['verification_id'], context_sha256=new['context']['context_sha256']),
                 'release_verification_restart', '当前验证引用已指向其他尝试')
        _unchanged(ctx.conn, task, new)
        return _result(new)
    _require(ref == previous_ref, 'release_verification_restart', '当前验证引用已变化，不能重启旧尝试')
    _unchanged(ctx.conn, task, old); _source(old)
    _restart_no_unknown_writes(ctx.conn, old)
    client = (_historical_client(old) if old.get('approval_observation') else _client(old, read_only=True, require_input=True))
    report = _completed_report(ctx.conn, client, old, for_restart=True)
    _require(report['report_sha256'] == payload['expected_report_sha256'],
             'release_verification_restart', '原验证报告已变化，请重新核对')
    plans = client.plans_pending(old['session_id'])
    asks = client.input_pending(old['session_id'])
    _require(isinstance(plans.get('plans'), list)
        and not any(isinstance(item, dict) and item.get('status') == 'pending' for item in plans['plans'])
        and asks.get('sessionId') == old['session_id'] and isinstance(asks.get('asks'), list) and not asks['asks'],
        'release_verification_restart', '旧尝试仍有待批准 Goal 或待回答问题')
    _unchanged(ctx.conn, db.require_task(ctx.conn, task['task_id']), old); _source(old)
    restart = dict(verification_id=old['verification_id'], context_sha256=old['context']['context_sha256'],
        report_sha256=report['report_sha256'], report_body_sha256=_hash(report['report']),
        session_id=old['session_id'], planning_turn_id=old['planning_turn_id'],
        execution_turn_id=old['execution_turn_id'], execution_id=report['execution_id'])
    context = _context(ctx.conn, task, old['context']['target_sha'], restart=restart)
    if prepare_only:
        _restart_origin(ctx.conn,context)
        return context
    return _start_context(ctx, task, context, previous_ref=previous_ref)


def _approval_operation(value):
    return (stable_scope_id('execution', invocation_id='releaseverify_' + value['verification_id']),
            'proposal_' + str((value.get('proposal') or {}).get('id')) + '_accept')


def _known_unsent_guard_block(conn, value):
    """A durable pre-reservation phase or the exact legacy rejection is unsent.

    Any downstream receipt, even rejected/unknown, rules out this path.
    """
    legacy = (value['state'] == 'blocked' and value.get('reason') == '缺少验证要求所属执行轮次'
              and value.get('failure_code') in (None, 'forbidden'))
    pending = value['state'] == 'unknown' and value.get('approval_phase') == 'pre_reservation'
    if not ((legacy or pending) and value.get('approval_sent') is True
            and not value.get('execution_turn_id') and not value.get('execution_id')
            and value.get('session_id') and value.get('planning_turn_id')
            and (value.get('proposal') or {}).get('id')):
        return False
    return db.get_runtime_operation(conn, *_approval_operation(value)) is None


def _approve_goal(ctx, task, value, client):
    """Recheck the live pending proposal immediately before the unique write."""
    _unchanged(ctx.conn, db.require_task(ctx.conn, task['task_id']), value)
    _source(value)
    sid, planning = value['session_id'], value['planning_turn_id']
    expected = value['proposal']
    current = worker._pending_goal_contract(client.plans_pending(sid), sid, planning)
    _require(current is not None and current == expected,
        'release_verification_proposal', '原 Goal 提案已变化或过期，不能批准')
    status = client.agent_status(sid); inner = status.get('status') or status
    record = client.turn_get(sid, planning, require_identity=True)
    _require(worker._turn_identity_matches(record, sid, planning)
        and worker._turn_status(record) == 'completed'
        and inner.get('currentTurnId') == planning and inner.get('isRunning') is False,
        'release_verification_proposal', '原 Goal 规划轮次尚未完成或当前轮次已变化')
    # Existing downstream receipts, including unknown/rejected, never authorize
    # this explicit known-unsent recovery path to send another approval.
    _require(db.get_runtime_operation(ctx.conn, *_approval_operation(value)) is None,
             'release_verification_approval_sent', 'Goal 审批已有运行时回执，不能重复发送')
    _require((db.task_extra(db.require_task(ctx.conn, task['task_id'])).get('release_verification_ref') or {}) == dict(
        verification_id=value['verification_id'], context_sha256=value['context']['context_sha256']),
        'release_verification_changed', '当前验证引用已变化，不能批准旧 Goal')
    _require(not value.get('automatic_recovery') or not any(db.paused(ctx.conn, stage) for stage in ('monitor','dispatch')),
        'release_verification_paused', '后台验证已暂停，保留原尝试，未发送审批')
    client.plans_resolve(expected['id'], sid, planning, expected['toolCallId'], 'approve',
        version=expected['version'], target_permission_mode=value['context']['runtime_config']['permission_mode'],
        required_verification_commands=value['context']['commands'])
    value['approval_phase'] = 'submitted'


def _sync_history(ctx, task, value, client):
    previous = value.get('history_previous_error') or {key: value[key] for key in
        ('state', 'failure_code', 'reason', 'report_sha256', 'report_body_sha256') if key in value}
    observation, turn, report = _observed_report(ctx.conn, client, task, value)
    observation['previous_error'] = (value.get('approval_observation') or {}).get('previous_error') or previous
    value.update(approval_observation=observation, proposal=observation['proposal'],
        execution_turn_id=turn, execution_id=observation['history']['executionId'])
    if report is None:
        value.update(state='running' if turn else 'unknown', reason='已只读接回用户批准的原 Goal，等待原执行报告')
        return
    _record_report(value, report)
    reason = '历史观察不授予验证通过；缺少本项目精确审批回执及冻结命令绑定'
    try:
        _host_evidence(report['report'], value['context']['commands'])
    except util.KanpilotError as exc:
        reason = str(exc) + '；' + reason
    value.update(state='blocked', failure_code='release_verification_report', reason=reason)


def cmd_sync_release_verification(ctx, payload, *, automatic=False):
    task = db.require_task(ctx.conn, payload['task_id'])
    value = _load(ctx.conn, task['task_id'], payload['verification_id'])
    original_value = util.jdump(value)
    recover_unsent = _known_unsent_guard_block(ctx.conn, value)
    observe_history = _history_candidate(value)
    history_approval_forbidden = bool(value.get('history_approval_forbidden') or _history_cas_conflict(value))
    if observe_history:
        value['history_read_only'] = True
        if history_approval_forbidden:
            value['history_approval_forbidden'] = True
    if observe_history and value['state'] == 'blocked' and not value.get('history_previous_error'):
        value['history_previous_error'] = {key: value[key] for key in
            ('state', 'failure_code', 'reason', 'report_sha256', 'report_body_sha256') if key in value}
    if value['state'] in ('failed', 'blocked') and not recover_unsent and not observe_history:
        return _result(value)
    try:
        local_snapshot = _history_snapshot(ctx.conn, value, include_operations=False)
        history_snapshot = _history_snapshot(ctx.conn, value) if observe_history else None
        if not observe_history:
            _unchanged(ctx.conn, task, value)
        if value['state'] == 'verified':
            verified_proof(ctx.conn, task)
            return _result(value)
        if not value.get('session_id') or not value.get('planning_turn_id'):
            value.update(state='unknown', reason='原会话或 prompt 结果未知；保留回执，不重新发送')
        else:
            if not observe_history:
                _source(value)
            client = (_historical_client(value) if observe_history else
                _client(value, read_only=bool(value.get('approval_sent') and not recover_unsent)))
            sid = value['session_id']; planning = value['planning_turn_id']
            status = client.agent_status(sid); inner = status.get('status') or status
            # An independent verification has no original execution observer.
            # Capture its own asks before interpreting an absent Goal proposal.
            from . import release_verification_questions as questions
            # A first external approval may already have moved to execution.
            # Attribute that turn through the complete read-only history path
            # before allowing its questions to choose an execution identity.
            if (not observe_history or inner.get('currentTurnId') == planning) and questions.observe(
                    ctx.conn, value, client, status, ctx.now_iso):
                if observe_history:
                    _history_local_guard(ctx.conn, task, value)
                latest = _save_sync(ctx, value, original_value, local_snapshot)
                return _result(latest or value)
            if observe_history:
                plans = client.plans_pending(sid)
                pending = worker._pending_goal_contract(plans, sid, planning)
                if pending is not None:
                    _require(not history_approval_forbidden and not value.get('approval_observation') and all(isinstance(row, dict)
                        and row == pending for row in plans.get('history') or []),
                        'release_verification_proposal', '待批准 Goal 与 history 不唯一或内容不符')
                    observe_history = False
                    value.pop('history_read_only', None)
                    client = _client(value)
                elif value.get('approval_observation') or (not plans.get('plans') and any(
                        isinstance(row, dict) and row.get('status') == 'approved' for row in plans.get('history') or [])):
                    _sync_history(ctx, task, value, client)
                    questions.observe(ctx.conn, value, client, client.agent_status(sid), ctx.now_iso)
                    _history_local_guard(ctx.conn, db.require_task(ctx.conn, task['task_id']), value)
                    with db.write_txn(ctx.conn):
                        latest = _load(ctx.conn, task['task_id'], value['verification_id'])
                        if util.jdump(latest) != original_value:
                            return _result(latest)
                        if (_history_snapshot(ctx.conn, latest) != history_snapshot
                                or not _history_recoverable(ctx.conn, latest)):
                            prior_error = (value.get('approval_observation') or {}).get('previous_error') or value.get('history_previous_error')
                            value = util.jload(original_value)
                            value.update(state='unknown', failure_code='release_verification_history_conflict',
                                reason=_HISTORY_CONFLICT_REASON, history_read_only=True, history_approval_forbidden=True)
                            if prior_error:
                                value['history_previous_error'] = prior_error
                            _save(ctx.conn, value, ctx.now_iso)
                            return _result(value)
                        _save(ctx.conn, value, ctx.now_iso)
                    return _result(value)
                else:
                    _require(not plans.get('plans') and not plans.get('history')
                        and inner.get('isRunning') is True and inner.get('currentTurnId') == planning,
                        'release_verification_proposal', '缺少本轮唯一可批准 Goal')
                    _history_local_guard(ctx.conn, task, value)
                    value['state'] = 'planning'
                    latest = _save_sync(ctx, value, original_value, local_snapshot)
                    return _result(latest or value)
            if recover_unsent:
                # Keep the original rejected receipt intact unless the exact
                # pending Goal is still present, valid, and definitely unsent.
                pending = worker._pending_goal_contract(client.plans_pending(sid), sid, planning)
                _require(pending is not None and pending == value['proposal'],
                    'release_verification_proposal', '原待批准 Goal 已变化或过期，保留阻塞')
                value.update(state='unknown', approval_phase='pre_reservation')
                value['automatic_recovery'] = automatic
                value.pop('reason', None); value.pop('failure_code', None)
                latest = _save_sync(ctx, value, original_value, local_snapshot)
                if latest is not None:
                    return _result(latest)
                original_value = util.jdump(value)
                _approve_goal(ctx, task, value, client)
            if not value.get('approval_sent'):
                proposal = worker._pending_goal_contract(client.plans_pending(sid), sid, planning)
                if proposal is None:
                    _require(inner.get('isRunning') is True and inner.get('currentTurnId') == planning,
                             'release_verification_proposal', '缺少本轮唯一可批准 Goal')
                    value['state'] = 'planning'
                else:
                    record = client.turn_get(sid, planning, require_identity=True)
                    _require(worker._turn_identity_matches(record, sid, planning)
                             and worker._turn_status(record) == 'completed'
                             and inner.get('currentTurnId') == planning and inner.get('isRunning') is False,
                             'release_verification_proposal', 'Goal 规划轮次尚未完成或当前轮次不符')
                    _source(value)
                    value.update(proposal=proposal, approval_sent=True, state='unknown', approval_phase='pre_reservation')
                    value['automatic_recovery'] = automatic
                    latest = _save_sync(ctx, value, original_value, local_snapshot)
                    if latest is not None:
                        return _result(latest)
                    original_value = util.jdump(value)
                    _approve_goal(ctx, task, value, client)
            if value.get('approval_sent'):
                _approved(ctx.conn, client, value)
                status = client.agent_status(sid); inner = status.get('status') or status
                turn = inner.get('currentTurnId')
                if turn == planning or not turn:
                    value.update(state='unknown', reason='Goal 已批准，等待明确的独立执行轮次')
                else:
                    _require(not value.get('execution_turn_id') or value['execution_turn_id'] == turn,
                             'release_verification_turn', '独立验证执行轮次发生变化')
                    record = client.turn_get(sid, turn, require_identity=True)
                    _require(worker._turn_identity_matches(record, sid, turn),
                             'release_verification_turn', '独立验证执行轮次身份不符')
                    value['execution_turn_id'] = turn
                    if worker._turn_status(record) == 'running' and inner.get('isRunning') is True:
                        value['state'] = 'running'
                    else:
                        report = _completed_report(ctx.conn, client, value)
                        _record_report(value, report)
                        value.update(_report(ctx.conn, client, value, report=report), state='verified')
                        value.pop('reason', None)
    except (util.KanpilotError, git_delivery.GitDeliveryError, OSError, ValueError) as exc:
        # A lost approval response stays observable; no second approval or prompt.
        if observe_history:
            # An observation remains provisional until its final source/task
            # guard commits. Failed reconciliation must not persist partial
            # attribution or a report collected against a changed source.
            prior_error = value.get('history_previous_error')
            value = util.jload(original_value)
            value['history_read_only'] = True
            if history_approval_forbidden:
                value['history_approval_forbidden'] = True
            if prior_error:
                value['history_previous_error'] = prior_error
        code = getattr(exc, 'code', '')
        if automatic and code == 'forbidden' and any(db.paused(ctx.conn, stage) for stage in ('monitor','dispatch')):
            code = 'release_verification_paused'
        uncertain = (code in ('remote_unavailable', 'timeout', 'runtime_unknown', 'release_verification_approval_sent',
                             'release_verification_report_pending', 'release_verification_paused')
                     or code == 'release_verification_history_conflict'
                     or value.get('approval_sent') and code == 'release_verification_approval')
        value.update(state='unknown' if uncertain else 'blocked', reason=util.redact(str(exc)), failure_code=code)
    # Preserve accepted evidence if a later read detects drift.
    old = _load(ctx.conn, task['task_id'], value['verification_id'])
    if old['state'] != 'verified':
        latest = _save_sync(ctx, value, original_value, local_snapshot)
        if latest is not None:
            return _result(latest)
    elif util.jdump(old) != original_value:
        return _result(old)
    return _result(value)


def verified_proof(conn, task):
    ref = db.task_extra(task).get('release_verification_ref') or {}
    if not ref.get('verification_id'):
        return None
    value = _load(conn, task['task_id'], ref['verification_id'])
    row = db.get_runtime_operation(conn, _scope(task['task_id']), _op(ref['verification_id']))
    _require(row['status'] == 'accepted' and value['state'] == 'verified'
             and ref.get('context_sha256') == value['context']['context_sha256'],
             'release_target_unverified', '独立发布目标验证尚未通过')
    _unchanged(conn, task, value)
    _source(value)
    fresh = _report(conn, _client(value, read_only=True), value)
    _require(all(fresh[key] == value.get(key) for key in fresh),
             'release_verification_report_changed', '独立验证报告或检查记录已变化')
    _unchanged(conn, db.require_task(conn, task['task_id']), value)
    return dict(verification_id=value['verification_id'], context_sha256=ref['context_sha256'],
        target_sha=value['context']['target_sha'], target_source_sha256=value['context']['target_source_sha256'],
        session_id=value['session_id'], planning_turn_id=value['planning_turn_id'],
        execution_turn_id=value['execution_turn_id'], proposal_id=value['proposal']['id'], **fresh)
