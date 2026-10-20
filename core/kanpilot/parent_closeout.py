"""Explicit, evidence-bound parent completion (protocol §5.27.18).

The projection is SQL-only. Only the explicit command reads files/services;
verification always remains in Plus, and unknown writes are never resent.
"""
import os
import re
from pathlib import Path

from . import (bridge, constants, core, db, gh_client, git_delivery, planfile, remote,
               process_owner, runtime_operations, util, verification_recovery, verify_evidence)
from . import parent_closeout_github as github

ACCEPTANCE_NOTE = '仅确认软件交付及现有自动验证；原计划的真机、真实服务及人工体验仍未核验，不代表人工已验收。'
_TOOL = 'closeout_parent_task'


class _Blocked(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _require(ok, code, message):
    if not ok:
        raise _Blocked(code, message)


def _scope(task_id):
    return 'parent-closeout:' + task_id


def _pending(conn, task_id):
    return conn.execute("SELECT * FROM runtime_operations WHERE scope_id=? AND tool=? "
                        "AND status IN ('prepared','unknown') ORDER BY created_at,operation_id LIMIT 1",
                        (_scope(task_id), _TOOL)).fetchone()


def _task_snapshot(task):
    result = dict(task)
    # Observations and our own receipt are not new task intent. In particular,
    # sync-tasks may observe remote done while an unknown write is reconciled.
    for key in ('updated_at', 'kaneo_checked_at', 'kaneo_status'):
        result.pop(key, None)
    extra = db.task_extra(task)
    extra.pop('parent_closeout', None)
    result['extra_json'] = extra
    return result


def _local(conn, parent, *, software_ready=False):
    extra = db.task_extra(parent)
    children = list(conn.execute('SELECT * FROM tasks WHERE review_source_task_id=? ORDER BY task_id',
                                 (parent['task_id'],)))
    history = extra.get('review_history') or []
    entries = [row for row in history if isinstance(row, dict)
               and row.get('execution_run_id') == parent['run_id']
               and row.get('conclusion') == 'changes_requested']
    blockers = []
    def block(code, message):
        blockers.append(dict(code=code, message=message))
    involved = {parent['task_id']: parent}
    for child in children:
        involved[child['task_id']] = child
    for entry in entries:
        if entry.get('fix_task_id') and entry['fix_task_id'] not in involved:
            found = db.get_task(conn, entry['fix_task_id'])
            if found is not None:
                involved[found['task_id']] = found
    # A newly created repair of a child is new intent even before its request
    # has been queued. Include those rows in the fingerprint and fail closed;
    # this command only accepts the explicitly bound direct two-level chain.
    pending_descendants=[child['task_id'] for child in children if child['task_id']!=parent['task_id']]
    visited_descendants=set()
    while pending_descendants:
        source=pending_descendants.pop()
        if source in visited_descendants:
            continue
        visited_descendants.add(source)
        for descendant in conn.execute('SELECT * FROM tasks WHERE review_source_task_id=?',(source,)):
            if descendant['task_id'] not in involved:
                involved[descendant['task_id']]=descendant
                pending_descendants.append(descendant['task_id'])
                block('unbound_nested_repair','二级任务出现新的下级修复，需先确认完整修复链后再收尾')
    if not parent['run_id'] or not entries:
        block('review_cycle_missing', '当前执行周期缺少可绑定的原始评审与二级修复记录')
    if parent['status'] not in (('in_review','done') if software_ready else ('in_review',)) or extra.get('review_conclusion') != 'changes_requested' or not db.task_is_active(parent):
        block('parent_not_eligible', '原始任务不在当前待修复评审周期的活跃列表中')
    seen_keys, bound_ids = set(), set()
    for entry in entries:
        identity = tuple(entry.get(k) for k in ('fix_task_id','review_key','report_sha256','head_sha'))
        if not all(identity) or entry.get('review_key') in seen_keys:
            block('repair_binding_ambiguous', '原始评审的修复身份缺失或重复，不能推断闭环')
            continue
        seen_keys.add(entry['review_key'])
        if entry['fix_task_id'] in bound_ids:
            block('repair_binding_ambiguous', '多个原始问题周期指向同一二级任务，需明确修复绑定')
        bound_ids.add(entry['fix_task_id'])
        child = involved.get(entry['fix_task_id'])
        if (child is None or child['review_source_task_id'] != parent['task_id']
                or child['project_id'] != parent['project_id'] or child['repository'] != parent['repository']
                or db.task_save_state(child) == 'purged' or child['task_id'] == parent['task_id']):
            block('repair_identity_mismatch', '二级修复任务缺失、已移除或不属于同一原始任务与仓库')
        if entry.get('head_sha') != core.delivery_of(parent).get('head_sha'):
            block('parent_head_changed', '原始评审不对应当前登记的功能提交')
    if set(c['task_id'] for c in children) != bound_ids:
        block('unbound_child', '存在未绑定当前原始评审周期的直接二级任务，不能漏过收尾')
    current = [entry for entry in entries if
               entry.get('fix_task_id') == parent['fix_task_id'] and entry.get('review_key') == parent['review_key']
               and entry.get('report_sha256') == parent['review_report_sha256']
               and entry.get('report_path') == parent['review_report_path']]
    if len(current) != 1:
        block('current_review_mismatch', '当前原始评审登记与必需修复集合不一致')
    # Follow persisted relation edges, including hidden/purged rows. A cycle is
    # not made safe by a display filter or by all visible cards saying done.
    for start in involved:
        cursor, visited = start, set()
        while cursor:
            if cursor in visited:
                block('relation_cycle', '父子任务关联存在循环，需先修正关联')
                break
            visited.add(cursor)
            row = db.get_task(conn, cursor)
            if row is None:
                break
            cursor = row['review_source_task_id']
    requests, rounds = [], []
    for task_id in sorted(involved):
        task = involved[task_id]
        rows = list(conn.execute('SELECT * FROM requests WHERE task_id=? ORDER BY request_id',(task_id,)))
        requests.extend(dict(r) for r in rows)
        if any(r['status'] in ('queued','claimed') and (not software_ready or r['kind']!='delivery') for r in rows):
            block('active_request', '原始任务或二级任务仍有活动请求，需等待当前流程结束')
        repair = core.review_repair_of(task)
        if repair and repair.get('state') in ('held','queued','running','awaiting_review'):
            block('active_repair', '原始任务或二级任务已有直接修复，不能同时收尾')
        row = conn.execute('SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?',
                           (task_id,task['run_id'])).fetchone()
        if row is not None:
            rounds.append({k:row[k] for k in row.keys() if k != 'updated_at'})
    snapshot = dict(tasks=[_task_snapshot(involved[k]) for k in sorted(involved)],
                    requests=requests, rounds=rounds)
    context = util.sha256_text(util.jdump(snapshot))
    complete = sum(child['status'] == 'done' for child in children)
    return dict(children=children, entries=entries, blockers=blockers, context=context,
                snapshot=snapshot, required=len(bound_ids), completed=complete)


def candidate(conn, task):
    """Cheap current-cycle gate. Old closeout records cannot capture a new run."""
    if task is None:
        return False
    if _pending(conn, task['task_id']) is not None:
        return True
    extra = db.task_extra(task)
    saved = extra.get('parent_closeout') or {}
    original = saved.get('original_evidence') or {}
    if (task['status'] == 'done' and saved.get('state') == 'completed'
            and original.get('task_id') == task['task_id']
            and original.get('run_id') == task['run_id']
            and original.get('plan_sha256') == task['plan_sha256']
            and original.get('requirements_sha256') == task['requirements_sha256']
            and original.get('head_sha') == core.delivery_of(task).get('head_sha')
            and any(entry.get('execution_run_id')==task['run_id']
                    and all(entry.get(key)==task[column] for key,column in
                        (('review_key','review_key'),('report_path','review_report_path'),
                         ('report_sha256','review_report_sha256'),('fix_task_id','fix_task_id')))
                    for entry in saved.get('required_repairs') or [])):
        return True
    return bool(task['status'] == 'in_review' and extra.get('review_conclusion') == 'changes_requested'
                and any(isinstance(entry, dict) and entry.get('execution_run_id') == task['run_id']
                        and entry.get('conclusion') == 'changes_requested' and entry.get('fix_task_id')
                        for entry in extra.get('review_history') or []))


def projection(conn, task, now=None):
    """SQL-only DTO; no filesystem, network, subprocess, or persisted mutation."""
    if not candidate(conn, task):
        return None
    local = _local(conn, task)
    saved = db.task_extra(task).get('parent_closeout') or {}
    pending = _pending(conn, task['task_id'])
    blockers = list(local['blockers'])
    if task['status'] == 'done' and saved.get('state') == 'completed':
        state, reason, blockers = 'completed', '收尾校验已通过，已同步完成', []
    elif pending is not None:
        state, reason = 'unknown', '完成同步结果待确认；仅只读核对，不重复发送'
    elif blockers:
        state, reason = 'blocked', blockers[0]['message']
    elif local['completed'] < local['required']:
        state, reason = 'waiting_children', '等待所有二级修复任务完成后校验原始任务'
    elif saved.get('state') == 'blocked' and saved.get('context_sha256') == local['context']:
        state = 'blocked'
        blockers = saved.get('blockers') or []
        reason = blockers[0]['message'] if blockers else '收尾证据尚未齐备，可重新核对'
    else:
        state, reason = 'ready', '二级修复已完成，校验已有交付与验证证据后同步原始任务'
    titles = dict(waiting_children='等待二级任务',ready='待收尾校验',blocked='收尾校验受阻',
                  unknown='完成同步待确认',completed='已完成')
    dto = dict(state=state,title=titles[state],reason=reason,context_sha256=local['context'],
               can_complete=state in ('ready','unknown') or (state == 'blocked' and not local['blockers']),
               required_children=local['required'],completed_children=local['completed'],
               blockers=blockers,acceptance_note=ACCEPTANCE_NOTE)
    for key in ('checked_at','evidence_task_id','target_branch','target_sha'):
        if saved.get(key):
            dto[key]=saved[key]
    return dto


def _simple_flutter_files(command):
    # Intentionally avoid shlex equivalence: quotes, escaping, options, prefixes,
    # substitutions, operators, traversal and globs are outside this one rule.
    if not isinstance(command,str) or not re.fullmatch(
            r'flutter test(?: test/[A-Za-z0-9_./-]+_test\.dart)+',command):
        return None
    paths=command.split(' ')[2:]
    if any(any(p in ('','.','..') for p in path.split('/')) for path in paths):
        return None
    return set(paths)


def coverage_mapping(commands, checks, paths):
    """Cross-plan reuse only; last relevant records are authoritative.

    An explicit check of the required command outranks inferred file coverage,
    even when that explicit check failed. Without an exact record, the last
    structurally covering flutter check decides; older green checks cannot
    erase a later failed, unknown, or unexecuted result.
    """
    records=[row for row in checks or [] if isinstance(row,dict)
             and isinstance(row.get('command'),str)]
    result=[]
    existing_paths=set(paths)
    for command in commands:
        exact=[row for row in records if row['command']==command]
        selected=exact[-1] if exact else None
        basis='exact'
        if not exact:
            required=_simple_flutter_files(command)
            if not required or not required <= existing_paths:
                return None
            for row in records:
                files=_simple_flutter_files(row['command'])
                if files and required <= files:
                    selected=row
            if selected is None or not _simple_flutter_files(selected['command']) <= existing_paths:
                return None
            basis='flutter_test_file_superset'
        if (selected is None or type(selected.get('exitCode')) is not int
                or selected['exitCode']!=0 or selected.get('result')!='passed'
                or selected.get('disposition') not in (None,'executed')):
            return None
        result.append(dict(command=command,check_command=selected['command'],
                           check_id=selected.get('id'),basis=basis))
    return result


def _report_bytes(path, digest, hashes):
    _require(bool(path and digest), 'review_report_missing', '评审报告身份不完整')
    p=Path(path)
    _require(not p.is_symlink() and p.is_file(), 'review_report_missing', '原始或最终评审报告不可读')
    actual=util.sha256_file(p)
    _require(actual == digest, 'review_report_changed', '原始或最终评审报告字节已变化')
    hashes[str(p)]=actual


def _task_evidence(conn, task, hashes):
    _require(task['source_path'] and task['source_sha256'] and task['requirements_sha256'],
             'source_identity_missing','任务缺少冻结需求来源身份')
    source_file_sha=util.sha256_file(Path(task['source_path']))
    _source,source_sha,requirements=planfile.check_source_snapshot(task['source_path'],task['task_id'],task['project_id'])
    _require((source_sha,requirements)==(task['source_sha256'],task['requirements_sha256']),
             'source_identity_changed','任务冻结需求来源已变化')
    _require(util.sha256_file(Path(task['source_path']))==source_file_sha,
             'source_identity_changed','读取期间冻结需求来源已变化')
    hashes[task['source_path']]=source_file_sha
    evidence=verification_recovery.read_goal_evidence(conn,task)
    _require(evidence['coverage']['ok'], 'verify_missing', task['title']+'：自身冻结验证命令未全部通过')
    report=evidence['report'].get('report') or {}
    _require(all(isinstance(row,dict) and row.get('verdict')=='met' for row in report.get('criteria') or [])
             and all(isinstance(row,dict) and row.get('status')=='completed' for row in report.get('steps') or [])
             and not (report.get('execution') or {}).get('errorCode'),
             'report_incomplete','正式 Plus 报告仍有未满足验收、未完成步骤或执行错误')
    checks=report.get('checks') or []
    exact_coverage=all(any(isinstance(row,dict) and row.get('command')==command
                    and type(row.get('exitCode')) is int and row['exitCode']==0
                    and row.get('result')=='passed' for row in checks) for command in evidence['commands'])
    receipt=db.task_extra(task).get('engine_verdict') or {}
    binding=receipt.get('binding') or {}
    _require(receipt.get('verification_ok') is True and receipt.get('evidence_source')=='goal_report'
             and receipt.get('execution_run_id')==task['run_id'] and receipt.get('plan_sha256')==task['plan_sha256']
             and receipt.get('report_sha256')==evidence['report'].get('report_sha256')
             and receipt.get('execution_turn_id')==evidence['report'].get('turn_id')
             and receipt.get('evidence_sha256')==evidence['hashes']
             and binding.get('task_id')==task['task_id'] and binding.get('execution_run_id')==task['run_id']
             and binding.get('plan_sha256')==task['plan_sha256']
             and binding.get('commands_sha256')==util.sha256_text(util.jdump(evidence['commands']))
             and bool(binding.get('source_sha256'))
             and binding.get('source_sha256')==evidence['job'].get('verification_source_sha256')
             and evidence['job'].get('verification_evidence_version')==1
             and bool(receipt.get('index_source_sha256')),
             'verification_receipt_changed','正式 Plus 验证回执、报告或已验证源码快照身份不符')
    delivery=core.delivery_of(task)
    head=delivery.get('head_sha')
    _require(head and any(row.get('execution_run_id')==task['run_id'] and row.get('sha')==head
                         for row in delivery.get('commits') or []),
             'delivery_identity_missing','交付提交未精确绑定当前执行轮次')
    _require(binding.get('worktree_path')==delivery.get('worktree_path')
             and binding.get('branch')==delivery.get('branch'),
             'verification_binding_changed','验证记录的工作树与交付分支身份不符')
    _require(git_delivery.tree_source_sha256(task['repository'],head)==receipt['index_source_sha256'],
             'source_tree_changed','已提交源码 tree 与本轮验证快照不一致')
    hashes.update(evidence['hashes'])
    return dict(task_id=task['task_id'],run_id=task['run_id'],plan_sha256=task['plan_sha256'],
                requirements_sha256=task['requirements_sha256'],head_sha=head,
                source_sha256=receipt['index_source_sha256'],report_sha256=receipt['report_sha256'],
                report_id=evidence['report'].get('report_id'),commands=evidence['commands'],checks=checks,
                exact_coverage=exact_coverage)


def _collect_evidence(conn,parent,local,*,frozen_target=None,software_ready=False):
    _require(not local['blockers'],'local_context_blocked',local['blockers'][0]['message'] if local['blockers'] else '')
    _require(local['required']>0 and (all(child['status'] in ('in_review','done') for child in local['children'])
             if software_ready else local['completed']==local['required']),
             'children_incomplete','二级修复任务尚未全部完成')
    hashes={}
    for entry in local['entries']:
        _report_bytes(entry.get('report_path'),entry.get('report_sha256'),hashes)
    kaneo_children=[]
    kaneo_client=remote.get_client()
    for child in local['children']:
        observed=remote.get_task(kaneo_client,child['task_id'])
        _require(_remote_identity(observed,child)
                 and (observed.get('task') or {}).get('status') in (constants.DONE_SLUGS | constants.REVIEW_SLUGS if software_ready else constants.DONE_SLUGS),
                 'child_remote_changed','二级任务在 Kaneo 已重开，或项目、需求身份已变化')
        kaneo_children.append(dict(task_id=child['task_id'],project_id=child['project_id'],
            requirements_sha256=child['requirements_sha256'],status=(observed.get('task') or {}).get('status')))
    all_tasks=[parent]+local['children']
    evidence={task['task_id']:_task_evidence(conn,task,hashes) for task in all_tasks}
    repo=git_delivery.origin_github_repo(parent['repository'])
    _require(repo,'github_repository_unknown','无法核对同一 GitHub 仓库的交付结果')
    branch=core.delivery_of(parent).get('base_branch')
    _require(branch,'target_branch_missing','原始任务未登记目标分支')
    current_tip=github.commit(repo,branch)
    tip=github.commit(repo,frozen_target) if frozen_target else current_tip
    if frozen_target:
        _require(tip['sha']==frozen_target and github.contains(repo,frozen_target,current_tip['sha']),
                 'frozen_target_removed','当前目标分支不再包含原完成操作冻结的目标提交')
    required_heads=[evidence[t['task_id']]['head_sha'] for t in all_tasks]
    children=[]
    for child in local['children']:
        extra=db.task_extra(child);delivery=core.delivery_of(child);head=delivery.get('head_sha')
        history=extra.get('review_history') or []
        reviews=[r for r in history if isinstance(r,dict) and r.get('execution_run_id')==child['run_id']
                 and r.get('head_sha')==head and r.get('conclusion')=='pass'
                 and r.get('review_key')==child['review_key']
                 and r.get('report_sha256')==child['review_report_sha256']
                 and r.get('report_path')==child['review_report_path']]
        _require(extra.get('review_conclusion')=='pass' and len(reviews)==1,
                 'final_review_missing','二级任务缺少当前执行及最终提交对应的正式 Review pass')
        _report_bytes(child['review_report_path'],child['review_report_sha256'],hashes)
        _require(delivery.get('mode')=='pr' and delivery.get('base_branch')==branch
                 and type(delivery.get('pr_number')) is int and delivery['pr_number']>0,
                 'pr_identity_missing','二级任务缺少同目标分支的 PR 交付身份')
        expected_url='https://github.com/'+repo+'/pull/'+str(delivery['pr_number'])
        _require(delivery.get('pr_url')==expected_url,'pr_repository_mismatch','二级任务 PR 不属于当前 GitHub 仓库')
        pr=github.pr(repo,delivery['pr_number'])
        _require(pr.get('number')==delivery['pr_number'] and pr.get('url')==expected_url
                 and pr.get('state')=='MERGED' and pr.get('headRefOid')==head
                 and pr.get('headRefName')==delivery.get('branch') and pr.get('baseRefName')==branch
                 and pr.get('isCrossRepository') is False and util.parse_ts(pr.get('mergedAt')) is not None,
                 'pr_not_merged','真实 GitHub PR 尚未合并或 head/base/仓库身份不符')
        _require(delivery.get('base_sha') and github.contains(repo,delivery['base_sha'],head),
                 'pr_base_mismatch','PR 最终提交不包含冻结目标基线')
        remote_commit=github.commit(repo,head)
        _require(remote_commit['sha']==head and remote_commit['source_sha256']==evidence[child['task_id']]['source_sha256'],
                 'github_source_mismatch','远端 PR 提交源码与正式验证快照不符')
        children.append(dict(evidence[child['task_id']],review_key=child['review_key'],
            review_report_sha256=child['review_report_sha256'],pr_number=delivery['pr_number'],pr_url=expected_url))
    _require(all(github.contains(repo,head,tip['sha']) for head in required_heads),
             'target_missing_changes','目标分支尚未包含原始功能与全部修复提交')
    checkpoint=None;mapping=None;all_coverage=None
    for child in children:
        if not child['exact_coverage'] or child['source_sha256']!=tip['source_sha256']:
            continue
        if not all(github.contains(repo,head,child['head_sha']) for head in required_heads):
            continue
        commit=github.commit(repo,child['head_sha'])
        coverage={task['task_id']:coverage_mapping(evidence[task['task_id']]['commands'],child['checks'],commit['paths'])
                  for task in all_tasks}
        if all(value is not None for value in coverage.values()):
            checkpoint=child;mapping=coverage[parent['task_id']];all_coverage=coverage;break
    _require(checkpoint is not None,'integrated_checkpoint_missing',
             '缺少包含全部功能与修复、覆盖父任务验证且源码与目标分支一致的单一最终检查点')
    verification_recovery._require_unchanged(hashes)
    return dict(context_sha256=local['context'],context=local['snapshot'],
                required_repairs=local['entries'],reused_evidence=children,kaneo_children=kaneo_children,
                original_evidence=evidence[parent['task_id']],evidence_task_id=checkpoint['task_id'],
                github_repository=repo,target_branch=branch,target_sha=tip['sha'],
                target_source_sha256=tip['source_sha256'],coverage_mapping=mapping,all_task_coverage=all_coverage,
                evidence_sha256=hashes,acceptance_note=ACCEPTANCE_NOTE)


def _result(conn,task_id,outcome,*,code=None,message=None):
    dto=projection(conn,db.require_task(conn,task_id))
    if dto is None:
        dto=dict(state='blocked',title='收尾校验受阻',reason=message or '任务不再适用收尾校验',
                 context_sha256='',can_complete=False,required_children=0,completed_children=0,
                 blockers=[],acceptance_note=ACCEPTANCE_NOTE)
    if code:
        dto=dict(dto,state='unknown' if outcome=='unknown' else 'blocked',
                 title='完成同步待确认' if outcome=='unknown' else '收尾校验受阻',reason=message,
                 blockers=[dict(code=code,message=message)])
    return dict(outcome=outcome,parent_closeout=dto)


def _same_context(conn,task_id,context):
    return _local(conn,db.require_task(conn,task_id))['context']==context


def _remote_identity(live,parent):
    task=live.get('task') or {}
    return (task.get('id')==parent['task_id'] and task.get('projectId')==parent['project_id']
            and live.get('requirements_sha256')==parent['requirements_sha256'])


def _reconcile_owner(conn, task_id, pending, frozen):
    """Recover only this operation's dead process, using kernel PID birth time.

    Missing/unreadable identity is not proof that a write stopped. Other
    lifecycle owners and a still-live original writer retain their lock.
    """
    recorded=frozen.get('process_identity') or {}
    expected_owner=frozen.get('lifecycle_owner')
    held=db.get_setting(conn,'lifecycle_owner','') or ''
    dead=False
    if held:
        _require(held==expected_owner and isinstance(expected_owner,str),
                 'lifecycle_busy','其他生命周期操作仍在执行，完成同步仅保留只读待确认')
        pid=recorded.get('pid')
        _require(type(pid) is int and pid>0 and all(type(recorded.get(key)) is int
                 for key in ('started_seconds','started_microseconds')),
                 'writer_identity_unknown','原完成写入进程身份无法确认，不能释放生命周期锁')
        birth=process_owner.process_birth(pid)
        if birth is not None:
            dead=any(birth[key]!=recorded[key] for key in birth)
        else:
            dead=not util.pid_alive(pid)
        _require(dead,'writer_still_active','原完成同步进程仍在执行或无法确认退出，请稍后只读核对')
    owner='closeout-parent-reconcile:'+util.new_uuid()
    birth=process_owner.process_birth(os.getpid())
    _require(birth is not None,'writer_identity_unknown','无法固化核对进程启动身份')
    with db.write_txn(conn):
        actual=_pending(conn,task_id)
        _require(actual is not None and actual['operation_id']==pending['operation_id']
                 and actual['result_json']==pending['result_json'],
                 'operation_changed','待确认完成操作已变化，请刷新后重核')
        _require((db.get_setting(conn,'lifecycle_owner','') or '')==held,
                 'lifecycle_busy','核对期间生命周期操作已变化')
        if held and dead:
            core.release_lifecycle(conn,held)
        core.acquire_lifecycle(conn,owner)
        runtime_operations.complete_operation(conn,_scope(task_id),pending['operation_id'],status='unknown',
            result=dict(frozen,lifecycle_owner=owner,process_identity=dict(birth,pid=os.getpid())))
    return owner


def _accept(ctx,parent,proof,operation_id,owner):
    conn=ctx.conn
    with db.write_txn(conn):
        _require(db.get_setting(conn,'lifecycle_owner','')==owner,
                 'lifecycle_busy','完成结算不再持有本操作生命周期锁')
        _require(_same_context(conn,parent['task_id'],proof['context_sha256']),
                 'context_changed','完成同步期间本机任务身份已变化，保留结果待核对')
        current=db.require_task(conn,parent['task_id'])
        from . import release_delivery
        release_block=release_delivery.completion_gate(conn,current)
        _require(release_block is None,'release_required',(release_block or {}).get('message','发布证据尚未就绪'))
        saved=dict(proof,state='completed',checked_at=ctx.now_iso,blockers=[],operation_id=operation_id)
        extra=db.task_extra(current);extra['parent_closeout']=saved
        core.transition_task(conn,current,'done',ctx.now_iso,'user',
            detail=dict(via='closeout-parent-task',evidence_task_id=proof['evidence_task_id']),
            extra_json=extra,kaneo_status='done',kaneo_checked_at=ctx.now_iso,**core.clear_block_fields())
        runtime_operations.complete_operation(conn,_scope(parent['task_id']),operation_id,
            result=saved,now=ctx.now_iso)
        core.release_lifecycle(conn,owner)
    return _result(conn,parent['task_id'],'completed')


def cmd_closeout_parent_task(ctx,payload):
    conn=ctx.conn;task_id=payload.get('task_id');expected=payload.get('expected_context_sha256')
    if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):
        raise util.bad_request('expected_context_sha256 必须为当前收尾上下文 SHA256')
    parent=db.require_task(conn,task_id)
    if parent['status']=='done' and candidate(conn,parent) and (db.task_extra(parent).get('parent_closeout') or {}).get('state')=='completed':
        return _result(conn,task_id,'completed')
    pending=_pending(conn,task_id)
    if pending is not None:
        frozen=util.jload(pending['result_json']) or {}
        proof=frozen.get('proof')
        reconcile_owner=None
        try:
            reconcile_owner=_reconcile_owner(conn,task_id,pending,frozen)
            # A fresh UI context cannot create a new operation around an older
            # unknown completion. Recover its dead lock even if intent changed.
            _require(proof and _same_context(conn,task_id,proof.get('context_sha256')),
                     'unconfirmed_operation','已有完成同步结果未知，且当前任务身份已变化；不会重复发送')
            client=remote.get_client();live=remote.get_task(client,task_id)
            _require(_remote_identity(live,parent),'remote_identity_changed','远端任务、项目或需求已变化')
            if (live.get('task') or {}).get('status')!='done':
                return _result(conn,task_id,'unknown')
            fresh=_collect_evidence(conn,parent,_local(conn,parent),frozen_target=proof.get('target_sha'))
            _require(fresh==proof,'evidence_changed','未知完成同步的冻结证据或目标分支已变化')
            return _accept(ctx,parent,proof,pending['operation_id'],reconcile_owner)
        except (_Blocked,util.KanpilotError,gh_client.GhError,git_delivery.GitDeliveryError,bridge.BridgeError,OSError,ValueError) as exc:
            return _result(conn,task_id,'unknown',code='reconcile_blocked',message=util.redact(str(exc)))
        finally:
            if reconcile_owner:
                with db.write_txn(conn):
                    core.release_lifecycle(conn,reconcile_owner)
    if not candidate(conn,parent):
        return _result(conn,task_id,'blocked',code='not_applicable',message='当前任务不适用原始任务收尾校验')
    local=_local(conn,parent)
    if expected!=local['context']:
        return _result(conn,task_id,'blocked',code='context_changed',message='任务或二级修复已变化，请刷新后重新核对')
    owner=None;operation_id=None
    try:
        birth=process_owner.process_birth(os.getpid())
        _require(birth is not None,'writer_identity_unknown','无法固化完成写入进程的启动身份')
        proof=_collect_evidence(conn,parent,local)
        from . import release_delivery
        release_block=release_delivery.completion_gate(conn,parent)
        _require(release_block is None,'release_required',(release_block or {}).get('message','发布证据尚未就绪'))
        client=remote.get_client();live=remote.get_task(client,task_id)
        _require(_remote_identity(live,parent),'remote_identity_changed','远端任务、项目或需求与冻结记录不符')
        status=(live.get('task') or {}).get('status')
        _require(status in ('in-review','done'),'remote_status_changed','远端任务已离开评审状态，不能自动完成')
        # Re-read evidence and fixed target tip immediately before reserving the
        # write. No network or file checks run while holding SQLite's write lock.
        fresh=_collect_evidence(conn,db.require_task(conn,task_id),_local(conn,db.require_task(conn,task_id)))
        _require(fresh==proof,'evidence_changed','核验期间报告、源码或目标分支已变化，请刷新后重核')
        live=remote.get_task(client,task_id)
        _require(_remote_identity(live,parent),'remote_identity_changed','写入前远端任务、项目或需求已变化')
        status=(live.get('task') or {}).get('status')
        _require(status in ('in-review','done'),'remote_status_changed','写入前远端状态已变化，不能完成')
        with db.write_txn(conn):
            _require(_same_context(conn,task_id,expected),'context_changed','核验期间任务或活动请求已变化')
            _require(_pending(conn,task_id) is None,'unconfirmed_operation','已有完成同步待确认，不能再次发送')
            owner='closeout-parent-task:'+util.new_uuid();core.acquire_lifecycle(conn,owner)
            operation_id='complete:'+expected
            receipt=runtime_operations.begin_operation(conn,_scope(task_id),operation_id,'core',_TOOL,
                dict(task_id=task_id,context_sha256=expected,target_sha=proof['target_sha']),now=ctx.now_iso)
            _require(receipt['status']=='prepared','operation_already_used','同身份完成操作已有终态，不能重复发送')
            runtime_operations.complete_operation(conn,_scope(task_id),operation_id,status='unknown',
                result=dict(proof=proof,lifecycle_owner=owner,process_identity=dict(birth,pid=os.getpid())),now=ctx.now_iso)
        # Unknown is durable before the call. A lost response/crash therefore
        # admits only read-only reconciliation, including after an app restart.
        if status!='done':
            remote.update_status(client,task_id,parent['project_id'],'done','in-review')
        after=remote.get_task(client,task_id)
        _require(_remote_identity(after,parent) and (after.get('task') or {}).get('status')=='done',
                 'remote_completion_unknown','完成写入后远端身份或 done 状态无法确认')
        return _accept(ctx,parent,proof,operation_id,owner)
    except (_Blocked,util.KanpilotError,gh_client.GhError,git_delivery.GitDeliveryError,bridge.BridgeError,OSError,ValueError) as exc:
        if _pending(conn,task_id) is not None:
            return _result(conn,task_id,'unknown',code='completion_unknown',message=util.redact(str(exc)))
        code=getattr(exc,'code','evidence_unavailable');message=util.redact(str(exc))
        with db.write_txn(conn):
            if _same_context(conn,task_id,expected):
                db.set_extra(conn,db.require_task(conn,task_id),ctx.now_iso,
                    parent_closeout=dict(state='blocked',context_sha256=expected,checked_at=ctx.now_iso,
                                         blockers=[dict(code=code,message=message)],acceptance_note=ACCEPTANCE_NOTE))
        return _result(conn,task_id,'blocked',code=code,message=message)
    finally:
        if owner:
            with db.write_txn(conn):
                core.release_lifecycle(conn,owner)
