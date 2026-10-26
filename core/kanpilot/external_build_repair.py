"""Explicit, read-only proved association of an externally merged build repair."""
from . import (core, db, gh_client, git_delivery, release_delivery as release,
               runtime_operations as operations, util)

TOOL = 'external_build_repair'
_require = release._require


def _hash(value):
    return util.sha256_text(util.jdump(value))


def _scope(task_id):
    return 'external-build-repair:' + task_id


_DELIVERY_ENVIRONMENT_FAILURES=frozenset(('gh_unavailable','gh_not_logged_in','gh_missing','gh_timeout'))
_LOCAL_GATE_FIELDS=('status','stage','failure_code','failure_reason','failure_stage','blocked_at',
                    'blocked_summary','manual_recovery_required','auto_advance_paused')


def recoverable_delivery_failure(task):
    return (task['status']=='needs_user' and task['failure_stage']=='delivery'
        and task['failure_code'] in _DELIVERY_ENVIRONMENT_FAILURES)


def locally_linkable(conn,task):
    return (db.task_is_active(task) and (task['status'] in ('in_review','done')
        or recoverable_delivery_failure(task)) and not task['manual_recovery_required']
        and not db.paused(conn,'delivery') and not db.task_auto_paused(task)
        and not core.automation_of(task).get('manual_takeover'))


def _local_gate(conn,task):
    _require(locally_linkable(conn,task),'external_repair_local_state',
        '外部关联仅允许已就绪任务或可重新核证的 GitHub 交付环境故障；其他阻塞、暂停或人工接管须先处理')
    return dict({key:task[key] for key in _LOCAL_GATE_FIELDS},
        manual_takeover=bool(core.automation_of(task).get('manual_takeover')),
        delivery_paused=db.paused(conn,'delivery'))


def accepted(conn, task):
    """SQL-only trusted association; a task extra alone cannot grant an exception."""
    ref = db.task_extra(task).get('external_build_repair_ref') or {}
    digest = ref.get('context_sha256')
    row = db.get_runtime_operation(conn, _scope(task['task_id']), 'accept:' + digest) if isinstance(digest,str) else None
    value = util.jload(row['result_json']) if row and row['result_json'] else None
    if not (row and row['status']=='accepted' and row['harness_id']=='core' and row['tool']==TOOL
            and isinstance(value,dict) and row['request_sha256']==_hash(value)
            and value.get('context_sha256')==digest and value.get('identity')==release._identity(task)
            and _hash({k:v for k,v in value.items() if k!='context_sha256'})==digest):
        return None
    return value


def require_policy(conn, task, value):
    policy,mapping=release._policy(conn,task)
    _require(policy['enabled'] and policy['channel']=='prerelease'
        and policy==value['policy'] and mapping==value['policy_snapshot'],
        'external_repair_policy','外部修复关联后的预发布策略或仓库映射已变化，请先恢复并重新核对')


def _source_snapshot(batch, run_attempt=None):
    value={key:item for key,item in batch.items() if key not in ('checked_at','next_check_at')}
    value['run_attempt']=batch.get('run_attempt',run_attempt)
    # A successful poll fills these defaults even when the failed run is unchanged.
    value.setdefault('read_failures',0)
    value.setdefault('blockers',[])
    return value


def summary(conn, task, value=None):
    value = value or accepted(conn,task)
    if value is None:
        return None
    candidate=value['candidate']
    result={k:candidate[k] for k in ('pr_number','pr_url','target_sha','source_batch_id','source_tag')}
    result['context_sha256']=value['context_sha256']
    ref=db.task_extra(task).get('release_verification_ref') or {}
    result.update(can_prepare_verification=not bool(ref),can_sync_verification=bool(ref))
    if ref:
        from . import release_verification as verification
        result.update(verification.attempt_summary(conn,task))
    return result


def _files(repo, number):
    metadata=gh_client._run(['api','repos/'+repo+'/pulls/'+str(number)],read_only=True)
    total=metadata.get('changed_files') if isinstance(metadata,dict) else None
    _require(type(total) is int and 0<total<=3000,'external_repair_files','原 PR 改动文件数缺失或超过 GitHub 完整列表范围')
    rows=[]
    for page in range(1,(total+99)//100+1):
        batch=gh_client._run(['api','repos/'+repo+'/pulls/'+str(number)+'/files?per_page=100&page='+str(page)],read_only=True)
        _require(isinstance(batch,list) and len(batch)==min(100,total-len(rows)),
                 'external_repair_files','原 PR 文件列表缺页或不完整')
        rows.extend(batch)
    _require(len(rows)==total and all(isinstance(row,dict) and isinstance(row.get('filename'),str) for row in rows),
             'external_repair_files','原 PR 文件列表条目不可确认')
    _require(len({row['filename'] for row in rows})==total,
             'external_repair_files','原 PR 文件列表重复或不完整')
    paths=set()
    for row in rows:
        _require(isinstance(row,dict) and row.get('status') in ('added','removed','modified','renamed','copied','changed','unchanged'),
                 'external_repair_files','原 PR 文件状态不可确认')
        names=[row.get('filename')]
        if row.get('status')=='renamed':
            names.append(row.get('previous_filename'))
        for path in names:
            _require(isinstance(path,str) and path and not path.startswith('/')
                and all(p not in ('','.','..') for p in path.split('/')),
                'external_repair_files','原 PR 文件路径不可确认')
            paths.add(path)
    return sorted(paths)


def preservation(repo, original_number, head, target):
    try:
        if release.closeout.github.contains(repo,head,target):
            return 'ancestor'
    except gh_client.GhError as exc:
        if not (exc.kind=='not_found' and 'no common ancestor' in str(exc).lower()):
            raise
    old=release.closeout.github.commit(repo,head)
    new=release.closeout.github.commit(repo,target)
    _require(isinstance(old.get('records'),list) and isinstance(new.get('records'),list),
             'external_repair_files','完整源码条目不可读')
    old={r['path']:(r['mode'],r['blob']) for r in old['records']}
    new={r['path']:(r['mode'],r['blob']) for r in new['records']}
    for path in _files(repo,original_number):
        _require(old.get(path)==new.get(path),'external_repair_files','修复目标未完整保留原交付文件：'+path)
    return 'files'


def _candidate(conn,task,number):
    local_gate=_local_gate(conn,task)
    _require(type(number) is int and number>0,'external_repair_pr','修复 PR 编号无效')
    _require(not task['review_source_task_id'] and not conn.execute(
        'SELECT 1 FROM tasks WHERE review_source_task_id=? LIMIT 1',(task['task_id'],)).fetchone(),
        'external_repair_group','外部构建修复仅支持无二级修复的独立任务')
    policy,mapping=release._policy(conn,task)
    _require(policy['enabled'] and policy['channel']=='prerelease' and mapping['repository']==task['repository'],
             'external_repair_policy','外部构建修复仅支持已开启的预发布策略')
    existing=accepted(conn,task)
    _require(not db.task_extra(task).get('external_build_repair_ref') or existing is not None,
             'external_repair_changed','已有外部修复引用无法核对，不能覆盖')
    batch=release._batch(conn,existing['candidate']['source_batch_id']) if existing else release._attached(conn,task)
    _require(batch and batch['state']=='failed' and not batch.get('abandoned')
        and batch['policy']['channel']=='prerelease' and batch['root_task_id']==task['task_id']
        and batch['project_id']==task['project_id']
        and batch['members']==[release._identity(task)] and release._current(conn,batch),
        'external_repair_source','需要当前任务已确认失败的独立预发布批次')
    _require(not db.task_extra(task).get('build_failure_hold'),'external_repair_source','已有构建修复轮次，不能关联外部修复')
    repo=git_delivery.origin_github_repo(task['repository']);d=core.delivery_of(task)
    _require(repo==batch['repository'] and number!=d.get('pr_number'),'external_repair_pr','必须选择同仓库的不同修复 PR')
    run=release.github.run(repo,batch['workflow_run_id'])
    _require(release._run_matches(batch,run) and run['id']==batch['workflow_run_id']
        and run.get('status')=='completed' and run.get('conclusion') in ('failure','cancelled','timed_out','action_required','startup_failure')
        and (not batch.get('run_attempt') or batch['run_attempt']==run['run_attempt']),
        'external_repair_run','原失败 workflow run 或 attempt 已变化')
    pr=release.closeout.github.pr(repo,number)
    url='https://github.com/'+repo+'/pull/'+str(number)
    _require(pr.get('number')==number and pr.get('state')=='MERGED' and pr.get('url')==url
        and pr.get('baseRefName')==d.get('base_branch') and pr.get('isCrossRepository') is False,
        'external_repair_pr','修复 PR 必须是同仓库、同目标分支且已合并的 PR')
    target=release.github.merge_commit_sha(repo,number,pr['headRefOid'])
    commit=release.closeout.github.commit(repo,target)
    candidate=dict(task_id=task['task_id'],pr_number=number,pr_url=url,head_sha=pr['headRefOid'],
        base_branch=pr['baseRefName'],target_sha=target,target_source_sha256=commit['source_sha256'],
        source_batch_id=batch['batch_id'],source_tag=batch['tag'],source_target_sha=batch['target_sha'],
        source_workflow_run_id=run['id'],source_workflow_attempt=run['run_attempt'],
        preservation_kind=preservation(repo,d['pr_number'],d['head_sha'],target))
    proof=release._single_proof(conn,task,frozen_target=target,allow_unverified_target=True,external_candidate=candidate)
    source_refs=(existing or {}).get('source_refs') or {k:db.task_extra(task).get(k)
        for k in ('release_ref','release_verification_ref')}
    value=dict(identity=release._identity(task),candidate=candidate,policy=policy,policy_snapshot=mapping,
        source_refs=source_refs,source_batch_snapshot=_source_snapshot(batch,run['run_attempt']),
        evidence_sha256=proof['evidence_sha256'])
    if existing is None:
        value['source_gate']=local_gate
    elif 'source_gate' in existing:
        value['source_gate']=existing['source_gate']
    value['context_sha256']=_hash(value)
    return value


def cmd_prepare_external_build_repair(ctx,payload):
    task=db.require_task(ctx.conn,payload['task_id']);value=_candidate(ctx.conn,task,payload.get('pr_number'))
    return dict(task_id=task['task_id'],context_sha256=value['context_sha256'],
        candidate=dict(value['candidate'],context_sha256=value['context_sha256']))


def cmd_accept_external_build_repair(ctx,payload):
    _require(payload.get('confirm') is True,'external_repair_confirmation','关联外部修复需要明确确认')
    task=db.require_task(ctx.conn,payload['task_id']);extra=db.task_extra(task)
    value=_candidate(ctx.conn,task,payload.get('pr_number'))
    _require(payload.get('expected_context_sha256')==value['context_sha256'],
             'external_repair_changed','外部修复上下文已变化，请重新核对')
    old_refs={k:extra.get(k) for k in ('release_ref','release_verification_ref','external_build_repair_ref')}
    confirmation_gate=_local_gate(ctx.conn,task)
    with db.write_txn(ctx.conn):
        task=db.require_task(ctx.conn,task['task_id']);extra=db.task_extra(task)
        _require(release._identity(task)==value['identity'] and all(extra.get(k)==v for k,v in old_refs.items())
            and _local_gate(ctx.conn,task)==confirmation_gate,
                 'external_repair_changed','关联前任务身份或当前引用已变化')
        require_policy(ctx.conn,task,value)
        source=release._batch(ctx.conn,value['candidate']['source_batch_id'])
        _require(source and _source_snapshot(source,value['candidate']['source_workflow_attempt'])==value['source_batch_snapshot'],
                 'external_repair_changed','原失败批次状态或冻结身份已变化')
        existing=accepted(ctx.conn,task)
        if existing:
            _require(existing==value,'external_repair_changed','已有不同外部修复关联，不能覆盖')
        else:
            _require(value.get('source_gate')==confirmation_gate,
                'external_repair_changed','关联前本地状态或交付故障已变化')
            batch=release._attached(ctx.conn,task)
            _require(batch and batch['batch_id']==value['candidate']['source_batch_id'],
                     'external_repair_changed','原失败批次引用已变化')
            if old_refs['release_verification_ref']:
                from . import release_verification as verification
                row=db.get_runtime_operation(ctx.conn,verification._scope(task['task_id']),
                    verification._op(old_refs['release_verification_ref'].get('verification_id','')))
                _require(row and row['status'] in ('accepted','rejected'),
                         'external_repair_unknown','旧验证结果仍未知，先对账后关联')
            scope=_scope(task['task_id']);op='accept:'+value['context_sha256']
            operations.begin_operation(ctx.conn,scope,op,'core',TOOL,value,now=ctx.now_iso)
            operations.complete_operation(ctx.conn,scope,op,result=value,now=ctx.now_iso)
            sources=list(extra.get('build_failure_sources') or [])
            sources.append(dict(kind=TOOL,release_batch_id=batch['batch_id'],
                source_workflow_run_id=value['candidate']['source_workflow_run_id'],
                source_workflow_attempt=value['candidate']['source_workflow_attempt'],
                release_ref=old_refs['release_ref'],release_verification_ref=old_refs['release_verification_ref']))
            extra.pop('release_ref',None);extra.pop('release_verification_ref',None)
            extra.update(build_failure_sources=sources,external_build_repair_ref=dict(context_sha256=value['context_sha256']))
            if value['source_gate']['status']=='needs_user':
                core.transition_task(ctx.conn,task,'in_review',ctx.now_iso,'external-build-repair',
                    detail=dict(context_sha256=value['context_sha256'],recovered_failure_code=task['failure_code']),
                    extra_json=extra,**core.clear_block_fields())
            else:
                db.update_task(ctx.conn,task['task_id'],ctx.now_iso,extra_json=extra)
    return dict(ok=True,repair=summary(ctx.conn,db.require_task(ctx.conn,task['task_id'])))
