"""Deterministic Actions/Release batches; SQLite remains the single state source.

Projection and completion_gate are SQL-only. Explicit command handlers perform
read-only evidence checks and the single durably reserved workflow dispatch.
"""
import json
import os
import re
from urllib.parse import quote

from . import (core, db, delivery_contract, gh_client, git_delivery, parent_closeout as closeout,
               process_owner, project_config, remote, runtime_operations, stage_contract, util)
from . import release_github as github

DEFAULT_POLICY=dict(enabled=False,channel='prerelease',workflow_path='.github/workflows/pilot-release.yml',
                    workflow_ref='main',version_base='0.1.0',required_assets=[],required_jobs=[])
BATCH_TOOL='github_release_batch'
DISPATCH_TOOL='github_release_dispatch'
MANIFEST_NAME='kaneopilot-release.json'
_IDENTITY_KEYS=('task_id','project_id','repository','run_id','plan_sha256','requirements_sha256',
                'review_key','review_report_path','review_report_sha256','save_state')


class ReleaseBlocked(util.KanpilotError):
    def __init__(self,code,message):
        self.code,self.message=code,message
        super().__init__(code,message)


def _require(ok,code,message):
    if not ok:
        raise ReleaseBlocked(code,message)


def validate_policy(raw):
    if not isinstance(raw,dict):
        raise util.bad_request('release 必须为对象')
    result=dict(DEFAULT_POLICY,**raw)
    if type(result['enabled']) is not bool or result['channel'] not in ('prerelease','stable'):
        raise util.bad_request('发布开关或渠道无效')
    path=result['workflow_path'];ref=result['workflow_ref'];version=result['version_base']
    if (not isinstance(path,str) or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml',path)
            or '..' in path.rsplit('/',1)[-1]):
        raise util.bad_request('workflow_path 必须为 .github/workflows/ 下的 yml/yaml 文件')
    if (not isinstance(ref,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}',ref)
            or ref.startswith('refs/') or any(part in ('','.','..') for part in ref.split('/')) or re.fullmatch(r'[0-9a-fA-F]{40,64}',ref)):
        raise util.bad_request('workflow_ref 必须为短分支或标签名，不能使用 refs/ 前缀或裸 SHA')
    if not isinstance(version,str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)',version):
        raise util.bad_request('version_base 必须为三个非负十进制数')
    for key in ('required_assets','required_jobs'):
        rows=result[key]
        if (not isinstance(rows,list) or len(rows)>100 or any(not isinstance(row,str) or not row.strip()
                or len(row.encode('utf-8'))>255 or any(ord(c)<32 for c in row) for row in rows)
                or len(rows)!=len(set(rows))):
            raise util.bad_request(key+' 必须为无重复的非空名称列表')
        result[key]=list(rows)
    if any('/' in name or '\\' in name or name in ('.','..',MANIFEST_NAME)
           or any(c in name for c in '*?[]:') for name in result['required_assets']):
        raise util.bad_request('required_assets 只接受安装包文件名，不接受路径、URL或通配符')
    if result['enabled'] and not result['required_assets']:
        raise util.bad_request('开启发布时必须声明 required_assets')
    return {key:result[key] for key in DEFAULT_POLICY}


def _policy(conn,task):
    _,_,items,_=project_config.load_raw_mappings(conn)
    entry=items.get(task['project_id']) or {};automation=entry.get('automation') or {}
    return validate_policy(automation.get('release') or {}),dict(policy_revision=automation.get('policy_revision',0),
        repository=entry.get('repository'))


def _identity(task):
    extra=db.task_extra(task);delivery=core.delivery_of(task)
    return dict({key:task[key] for key in _IDENTITY_KEYS},head_sha=delivery.get('head_sha'),
        delivery_sha256=util.sha256_text(util.jdump({key:delivery.get(key) for key in
            ('mode','head_sha','base_sha','base_branch','branch','pr_number','pr_url')})),
        evidence_sha256=util.sha256_text(util.jdump({key:extra.get(key) for key in
            ('review_history','review_conclusion','engine_verdict','plan_verify')})))


def _root(conn,task):
    if not task['review_source_task_id']:
        return task
    root=db.get_task(conn,task['review_source_task_id'])
    _require(root is not None and root['task_id']!=task['task_id'] and root['project_id']==task['project_id']
             and root['repository']==task['repository'],'release_group_invalid','修复任务的原始任务归属无法确认')
    return root


def _scope(project_id):
    return 'release-project:'+project_id


def _batch(conn,batch_id):
    row=conn.execute('SELECT * FROM runtime_operations WHERE operation_id=? AND tool=?',('batch:'+batch_id,BATCH_TOOL)).fetchone()
    return util.jload(row['result_json']) if row else None


def _batches(conn,project_id=None):
    if project_id:
        rows=conn.execute('SELECT * FROM runtime_operations WHERE tool=? AND scope_id=? ORDER BY created_at,operation_id',
                          (BATCH_TOOL,_scope(project_id)))
    else:
        rows=conn.execute('SELECT * FROM runtime_operations WHERE tool=? ORDER BY updated_at,operation_id',(BATCH_TOOL,))
    return [util.jload(row['result_json']) for row in rows if row['result_json']]


def _current(conn,batch):
    if not batch or not isinstance(batch.get('members'),list):
        return False
    return all((task:=db.get_task(conn,member['task_id'])) is not None and _identity(task)==member
               for member in batch['members'])


def _referenced(conn,task):
    ref=db.task_extra(task).get('release_ref') or {}
    batch=_batch(conn,ref.get('batch_id')) if ref.get('batch_id') else None
    return batch if batch and not batch.get('abandoned') and any(row['task_id']==task['task_id'] for row in batch['members']) else None


def _attached(conn,task):
    batch=_referenced(conn,task)
    return batch if batch and _current(conn,batch) else None


def _unsent(conn,batch):
    return bool(batch and not batch.get('abandoned') and batch.get('dispatch_status')=='not_sent'
        and batch['state'] in ('dispatching','blocked') and not batch.get('workflow_run_id')
        and db.get_runtime_operation(conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id']) is None)


def _pending(conn,project_id):
    rows=conn.execute("SELECT * FROM runtime_operations WHERE scope_id=? AND tool=? AND status IN ('prepared','unknown')",
                      (_scope(project_id),DISPATCH_TOOL)).fetchall()
    return rows


def _historical_build_source(conn,batch):
    """Archived source batches remain read-only across subsequent repairs."""
    for member in batch['members']:
        task=db.get_task(conn,member['task_id'])
        if task is None:
            continue
        extra=db.task_extra(task)
        if ((extra.get('build_failure_hold') or {}).get('release_batch_id')==batch['batch_id']
                or any(source.get('release_batch_id')==batch['batch_id']
                       for source in extra.get('build_failure_sources') or [])):
            return True
    return False


def _dto(batch,required=False,*,conn=None):
    state=batch['state'];titles=dict(ready='待构建发布',waiting_group='等待关联修复',dispatching='发布调度中',unknown='发布调度待确认',
        queued='构建已排队',building='构建中',publishing='发布中',blocked='发布受阻',failed='构建失败',published='已发布')
    value=dict(state=state,title=titles[state],reason=batch.get('reason') or titles[state],required=required,
        channel=batch['policy']['channel'],batch_id=batch['batch_id'],root_task_id=batch['root_task_id'],
        member_task_ids=[row['task_id'] for row in batch['members']],target_sha=batch['target_sha'],
        context_sha256=batch['context_sha256'],version=batch['version'],tag=batch['tag'],build_number=batch['build_number'],
        assets=batch.get('assets') or [],blockers=batch.get('blockers') or [],can_start=False,
        can_reconcile=state!='published' and batch.get('dispatch_status')!='not_sent',
        can_abandon=_unsent(conn,batch) if conn is not None else False)
    if batch.get('dispatch_status')=='not_sent' and not batch.get('abandoned'):
        value['can_start']=_unsent(conn,batch) if conn is not None else False
    for key in ('workflow_run_id','workflow_url','release_url','checked_at'):
        if batch.get(key) is not None:
            value[key]=batch[key]
    return value


def _last_preflight_blocker(task,delivery,member_ids):
    """Display an accepted observation only while its delivery identity matches."""
    try:
        contract=delivery.get('stage_contracts',{}).get('sync')
        result=delivery.get('stage_results',{}).get('sync')
        if not isinstance(contract,dict) or not isinstance(result,dict):return None
        delivery_contract.validate_current(contract,task,dict(request_id=contract['request_id'],run_id=contract['run_id']),delivery,'sync')
        result=stage_contract.validate_result(result,contract)
        if result['outcome']!='blocked' or len(result['evidence'])!=1:return None
        observation=result['evidence'][0]
        if observation.get('source')!='core_independent' or observation.get('kind')!='delivery_observation':return None
        outcome=observation.get('result') or {};value=outcome.get('release') or {}
        if (outcome.get('outcome')!='waiting' or value.get('state')!='blocked'
                or value.get('root_task_id')!=task['task_id'] or value.get('member_task_ids')!=member_ids
                or not isinstance(value.get('reason'),str) or not value['reason']
                or not isinstance(value.get('blockers'),list) or not value['blockers']):return None
        return value
    except (util.KanpilotError,KeyError,TypeError,AttributeError):
        return None


def _build_repair_merged(task):
    """A new reviewed PR merge permits explicit release preparation only."""
    from . import task_ai_action
    hold=db.task_extra(task).get('build_failure_hold') or {}
    delivery=core.delivery_of(task)
    receipt=task_ai_action.delivery_continuation(task,allow_paused=True)
    action=(receipt or {}).get('action') or {}
    return bool(hold and action.get('source_kind')=='build_failure'
        and action.get('action_id')==hold.get('action_id')
        and delivery.get('build_failure_action_id')==hold.get('action_id')
        and type(delivery.get('pr_number')) is int and delivery['pr_number']!=action.get('source_pr_number')
        and delivery.get('pr_state')=='MERGED' and delivery.get('merged_at'))


def projection(conn,task):
    """SQL only; unrelated old done tasks acquire no automatic release intent."""
    from . import external_build_repair as external
    external_value=external.summary(conn,task)
    hold=db.task_extra(task).get('build_failure_hold')
    if hold and not _build_repair_merged(task):
        old=_batch(conn,hold.get('release_batch_id'))
        if old:
            value=_dto(old,True,conn=conn)
            value.update(is_historical=True,can_start=False,can_reconcile=False,can_abandon=False,
                reason='原失败批次已归档；构建修复只创建新 PR，合并与再次构建发布须另行确认')
            return value
    batch=_attached(conn,task)
    ref=db.task_extra(task).get('release_ref') or {}
    policy,_revision=_policy(conn,task)
    required=task['status']!='done' and (policy['enabled'] or ref.get('required') is True
        or bool(db.task_extra(task).get('external_build_repair_ref')))
    if db.task_extra(task).get('external_build_repair_ref'):
        try:
            linked=external.accepted(conn,task)
            _require(linked is not None,'external_repair_changed','外部修复关联回执或任务身份已变化')
            external.require_policy(conn,task,linked)
        except ReleaseBlocked as exc:
            if external_value:
                external_value.update(can_prepare_verification=False,can_sync_verification=False,can_restart_verification=False)
            return dict(state='blocked',title='发布受阻',reason=str(exc),required=required,channel=policy['channel'],
                root_task_id=task['task_id'],member_task_ids=[task['task_id']],assets=[],
                blockers=[dict(code=exc.code,message=str(exc))],can_start=False,can_reconcile=False,external_repair=external_value)
    if batch:
        value=_dto(batch,required,conn=conn)
        if batch.get('dispatch_status')=='not_sent':value['can_start']=value['can_start'] and policy['enabled']
        value['can_link_external_repair']=bool(batch['state']=='failed' and policy['enabled'] and policy['channel']=='prerelease'
            and batch['policy']['channel']=='prerelease' and not task['review_source_task_id']
            and not conn.execute('SELECT 1 FROM tasks WHERE review_source_task_id=? LIMIT 1',(task['task_id'],)).fetchone()
            and not hold and external_value is None and external.locally_linkable(conn,task))
        if external_value:value['external_repair']=external_value
        return value
    if external_value:
        verification=external_value.get('verification') or {}
        stage=verification.get('state')
        verified=stage=='verified'
        title={'prepared':'待确认验证','planning':'验证规划中','running':'验证执行中',
            'unknown':'验证结果待核对','blocked':'验证受阻','failed':'验证失败','verified':'待构建发布'}.get(stage,'等待完整验证')
        reason=verification.get('reason') or ('完整验证已通过，可准备新预发布' if verified else
            '正在规划冻结目标的完整验证' if stage=='planning' else '正在执行冻结目标的完整验证' if stage=='running' else
            '原验证结果尚不明确，请核对原尝试' if stage=='unknown' else '外部修复已关联；完整验证与新预发布须分别确认')
        blockers=([dict(code=verification.get('failure_code') or 'release_target_unverified',message=reason)]
            if stage in ('blocked','failed') else [])
        return dict(state='ready' if verified else 'blocked',title=title,
            reason=reason,required=required,channel=policy['channel'],
            root_task_id=task['task_id'],member_task_ids=[task['task_id']],assets=[],blockers=blockers,
            can_start=verified and policy['enabled'],can_reconcile=False,can_link_external_repair=False,external_repair=external_value)
    old=_referenced(conn,task)
    if _unsent(conn,old):
        value=_dto(old,required,conn=conn)
        value.update(state='blocked',title='发布受阻',reason='原冻结批次的成员身份已变化；可弃用确定未发送的批次，再重新准备',can_start=False)
        return value
    from . import review_delivery
    if review_delivery.candidate(conn,task) is not None:
        return dict(state='waiting_group',title='待 PR 交付',
            reason=review_delivery.WAITING_REASON,
            required=required,channel=policy['channel'],root_task_id=task['task_id'],
            member_task_ids=[task['task_id']],assets=[],blockers=[],can_start=False,can_reconcile=False)
    pending=_pending(conn,task['project_id'])
    if pending:
        frozen=util.jload(pending[0]['result_json']) or {};old=_batch(conn,frozen.get('batch_id'))
        if old:
            value=_dto(old,required,conn=conn);value.update(state='unknown',title='发布调度待确认',
                reason='项目已有发布调度结果未知，先核对原批次；不会重复发送',can_reconcile=True)
            return value
    if not policy['enabled'] and not ref.get('required'):
        return None
    try:
        root=_root(conn,task)
    except ReleaseBlocked as exc:
        return dict(state='blocked',title='发布受阻',reason=str(exc),required=required,channel=policy['channel'],
            root_task_id=task['task_id'],member_task_ids=[task['task_id']],assets=[],
            blockers=[dict(code=exc.code,message=str(exc))],can_start=False,can_reconcile=False)
    related=list(conn.execute('SELECT * FROM tasks WHERE review_source_task_id=?',(root['task_id'],)))
    ids=sorted({root['task_id'],*(row['task_id'] for row in related)})
    def locally_ready(row):
        extra=db.task_extra(row);d=core.delivery_of(row)
        return (row['status'] in ('in_review','done') and db.task_is_active(row)
            and extra.get('review_conclusion')=='pass' and bool(row['run_id'] and row['review_report_sha256'])
            and d.get('mode')=='pr' and bool(d.get('merged_at')) and not core.review_repair_of(row)
            and not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') AND kind!='delivery' LIMIT 1",
                                 (row['task_id'],)).fetchone())
    waiting=(bool(closeout._local(conn,root,software_ready=True)['blockers']) or any(not locally_ready(row) for row in related)
             if related else not locally_ready(root))
    state='waiting_group' if waiting else 'ready'
    title='等待关联修复' if waiting else '待构建发布'
    reason='等待原始任务与全部修复具备合并和验证证据' if waiting else '核对已有交付证据后构建并发布版本'
    delivery=core.delivery_of(root)
    if waiting and not related and db.task_extra(root).get('review_conclusion')=='pass' and delivery.get('mode')=='pr' and not delivery.get('merged_at'):
        title='等待 PR 合并' if delivery.get('pr_number') else 'PR 交付中'
        notice=delivery.get('auto_merge') or {}
        reason=(notice.get('reason') if notice.get('head_sha')==delivery.get('head_sha') else None) or '评审已通过，正在推进 PR 交付；合并后按项目策略构建发布'
    value=dict(state=state,title=title,reason=reason,
        required=required,channel=policy['channel'],root_task_id=root['task_id'],member_task_ids=ids,
        assets=[],blockers=[],can_start=policy['enabled'] and not waiting,can_reconcile=False)
    blocked=_last_preflight_blocker(root,delivery,ids) if not waiting else None
    if blocked:
        value.update(state='blocked',title='发布受阻',reason='上次发布核对：'+blocked['reason'],
                     blockers=blocked['blockers'],can_start=False)
    if not related:
        from . import release_verification
        verification=release_verification.ordinary_summary(conn,root,blocked)
        if verification:
            value.update(verification)
            attempt=verification.get('verification') or {}
            stage=attempt.get('state')
            if stage:
                verified=stage=='verified'
                reason=attempt.get('reason') or ('完整验证已通过，可继续构建发布' if verified else
                    '正在规划合并后源码的完整验证' if stage=='planning' else
                    '正在执行合并后源码的完整验证' if stage=='running' else
                    '原验证结果尚不明确，请核对原尝试')
                value.update(state='ready' if verified else 'blocked',
                    title={'planning':'验证规划中','running':'验证执行中','unknown':'验证结果待核对',
                        'blocked':'验证受阻','failed':'验证失败','verified':'待构建发布'}.get(stage,'等待完整验证'),
                    reason=reason,can_start=verified,
                    blockers=([dict(code=attempt.get('failure_code') or 'release_target_unverified',message=reason)]
                        if stage in ('blocked','failed') else []))
    return value


def completion_gate(conn,task):
    """No network or files; only a core-accepted same-identity batch opens done."""
    value=projection(conn,task)
    if value is None or not value['required']:
        return None
    batch=_attached(conn,task)
    if batch and batch['state']=='published':
        row=db.get_runtime_operation(conn,_scope(task['project_id']),'batch:'+batch['batch_id'])
        if row is not None and row['status']=='accepted' and batch.get('publication_proof'):
            return None
    return dict(code='release_required',message=(value.get('reason') or '缺少当前交付的发布证据'))


def _verified_feed_checkpoint(repo,evidence,target):
    """Build the verified tree itself when only existing channel feeds advanced."""
    verified=closeout.github.commit(repo,evidence['head_sha'])
    _require(verified['sha']==evidence['head_sha'] and verified['source_sha256']==evidence['source_sha256'],
             'release_target_unverified','原发布检查点与正式验证源码不符')
    if not verified.get('records') or not target.get('records'):
        return None
    def sources(snapshot):
        return {row['path']:row for row in snapshot['records']
                if not row['path'].startswith('.pi/') and not git_delivery.is_build_artifact(row['path'])}
    before,after=sources(verified),sources(target)
    changed={path for path in before.keys() | after.keys() if before.get(path)!=after.get(path)}
    if (not changed or not changed<={'release/preview.json','release/latest.json'}
            or any(path not in before or path not in after
                   or before[path]['mode']!='100644' or after[path]['mode']!='100644' for path in changed)):
        return None
    return verified


def _single_proof(conn,task,*,frozen_target=None,allow_unverified_target=False,external_candidate=None):
    from . import external_build_repair as external
    explicit_recovery=(external_candidate is not None and external.recoverable_delivery_failure(task)
        and external.locally_linkable(conn,task))
    _require((task['status'] in ('in_review','done') or explicit_recovery) and db.task_is_active(task),'release_not_ready','任务尚未具备软件交付证据')
    _require(not core.review_repair_of(task),'release_repair_active','任务仍有当前修复，不启动构建发布')
    _require(not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') AND kind!='delivery' LIMIT 1",
                             (task['task_id'],)).fetchone(),'release_request_active','当前任务仍有规划、执行或评审请求')
    hashes={};evidence=closeout._task_evidence(conn,task,hashes);extra=db.task_extra(task);d=core.delivery_of(task)
    history=extra.get('review_history') or [];review=history[-1] if history else {}
    _require(extra.get('review_conclusion')=='pass' and review.get('conclusion')=='pass'
        and all(review.get(key)==task[column] for key,column in (('execution_run_id','run_id'),('review_key','review_key'),
             ('report_sha256','review_report_sha256'),('report_path','review_report_path')))
        and review.get('head_sha')==d.get('head_sha') and evidence['exact_coverage'],
        'release_review_invalid','发布需要当前执行、最终提交及逐字验证对应的正式评审通过')
    closeout._report_bytes(task['review_report_path'],task['review_report_sha256'],hashes)
    repo=git_delivery.origin_github_repo(task['repository'])
    _require(repo and d.get('mode')=='pr' and type(d.get('pr_number')) is int,'release_pr_missing','发布需要同仓库已合并的 PR')
    pr=closeout.github.pr(repo,d['pr_number'])
    expected='https://github.com/'+repo+'/pull/'+str(d['pr_number'])
    _require(pr.get('state')=='MERGED' and pr.get('headRefOid')==d['head_sha'] and pr.get('url')==expected
        and d.get('pr_url')==expected and pr.get('baseRefName')==d.get('base_branch')
        and pr.get('headRefName')==d.get('branch') and pr.get('isCrossRepository') is False,
        'release_pr_mismatch','发布 PR 合并、仓库或 head/base 身份不符')
    external_receipt=external.accepted(conn,task)
    external_target=external_candidate or (external_receipt or {}).get('candidate')
    if extra.get('external_build_repair_ref'):
        _require(external_receipt is not None,'external_repair_changed','外部修复关联回执或任务身份已变化')
        external.require_policy(conn,task,external_receipt)
    current_base=closeout.github.commit(repo,d['base_branch'])['sha']
    if external_target:
        _require(frozen_target in (None,external_target['target_sha']),'external_repair_changed','验证目标与已关联外部修复不符')
        frozen_target=external_target['target_sha']
    target=frozen_target or (github.merge_commit_sha(repo,d['pr_number'],d['head_sha']) if task['status']=='done'
            else current_base)
    commit=closeout.github.commit(repo,target)
    if external_target:
        _require(commit['source_sha256']==external_target['target_source_sha256']
            and external.preservation(repo,d['pr_number'],d['head_sha'],target)==external_target['preservation_kind'],
            'external_repair_changed','外部修复源码或原交付保留证明已变化')
    _require((bool(external_target) or closeout.github.contains(repo,d['head_sha'],target))
        and closeout.github.contains(repo,target,current_base),'release_target_unverified','发布目标不包含原交付或已不在目标分支历史中')
    if (task['status']!='done' and frozen_target is None and not external_target
            and not allow_unverified_target and not extra.get('release_verification_ref')
            and commit['source_sha256']!=evidence['source_sha256']):
        checkpoint=_verified_feed_checkpoint(repo,evidence,commit)
        if checkpoint is not None:
            # The containment check above already proves this head is in main.
            # Keep its entire verified fingerprint, including the original feeds.
            target,commit=checkpoint['sha'],checkpoint
    supplemental=None
    if (external_target or commit['source_sha256']!=evidence['source_sha256'] or extra.get('release_verification_ref')) and not allow_unverified_target:
        from . import release_verification
        supplemental=release_verification.verified_proof(conn,task)
        _require(supplemental is not None,'release_target_unverified','发布目标不是本轮已验证的完整源码')
        target=supplemental['target_sha'];commit=closeout.github.commit(repo,target)
        _require(commit['source_sha256']==supplemental['target_source_sha256']
            and (bool(external_target) or closeout.github.contains(repo,d['head_sha'],target))
            and closeout.github.contains(repo,target,current_base),
            'release_target_unverified','独立验证目标源码或包含关系已变化')
    live=remote.get_task(remote.get_client(),task['task_id'])
    _require(closeout._remote_identity(live,task) and (live.get('task') or {}).get('status') in ('in-review','done'),
        'release_source_changed','Kaneo 任务、项目、需求或评审状态已变化')
    result=dict(github_repository=repo,target_sha=target,target_source_sha256=commit['source_sha256'],
                evidence_sha256=hashes,reused_evidence=[evidence],required_repairs=[])
    if external_receipt is not None:
        result['external_build_repair']=dict(context_sha256=external_receipt['context_sha256'],**external_receipt['candidate'])
    if supplemental is not None:
        result['release_verification']=supplemental
    return result


def _software_proof(conn,root):
    children=list(conn.execute('SELECT * FROM tasks WHERE review_source_task_id=?',(root['task_id'],)))
    if children:
        saved=db.task_extra(root).get('parent_closeout') or {}
        frozen=saved.get('target_sha') if root['status']=='done' and saved.get('state')=='completed' else None
        local=closeout._local(conn,root,software_ready=True)
        proof=closeout._collect_evidence(conn,root,local,frozen_target=frozen,software_ready=True)
        # Exclude transient delivery requests, completion status observations and
        # our own release references; keep every frozen source/report fingerprint.
        return {key:value for key,value in proof.items() if key not in ('context','context_sha256','kaneo_children')}
    return _single_proof(conn,root)


def _version_inventory(conn,project_id,releases):
    _require(isinstance(releases,list),'release_versions_incomplete','未取得完整发布版本列表')
    versions=[]
    for row in releases:
        _require(isinstance(row,dict) and type(row.get('draft')) is bool and type(row.get('prerelease')) is bool,
                 'release_versions_incomplete','发布版本列表身份不完整')
        if not row['draft'] and not row['prerelease']:
            match=re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)',str(row.get('tag_name') or ''))
            if match:
                versions.append(tuple(int(part) for part in match.groups()))
    stable=sorted(batch['version'] for batch in _batches(conn,project_id) if batch['policy']['channel']=='stable')
    versions.extend(tuple(int(part) for part in value.split('.')) for value in stable)
    return sorted(set(versions)),stable


def _superseded_build_batch(conn,task,proof):
    """Exact failed stable predecessor; never retire it or release its version."""
    from . import task_ai_action
    if not _build_repair_merged(task):
        return None
    hold=db.task_extra(task).get('build_failure_hold') or {}
    action=task_ai_action.delivery_continuation(task,allow_paused=True)['action']
    old=_batch(conn,hold.get('release_batch_id'))
    if not old or old['policy']['channel']!='stable':
        return None
    member=next((row for row in old['members'] if row['task_id']==task['task_id']),{})
    _require(old.get('state')=='failed' and not old.get('abandoned')
        and old['batch_id']==action.get('release_batch_id') and old['project_id']==task['project_id']
        and old['root_task_id']==task['task_id'] and old['repository']==proof['github_repository']==action.get('github_repo')
        and old['context_sha256']==action.get('release_context_sha256')
        and old['target_sha']==action.get('release_target_sha') and proof['target_sha']!=old['target_sha']
        and old['workflow_run_id']==action.get('workflow_run_id') and old['tag']==action.get('release_tag')
        and member.get('run_id')==action.get('source_execution_run_id')
        and member.get('head_sha')==action.get('source_head_sha')
        and member.get('plan_sha256')==task['plan_sha256'] and member.get('requirements_sha256')==task['requirements_sha256'],
        'release_predecessor_mismatch','构建修复与原失败正式批次的归属或证据不符，不能排除版本占位')
    return old['batch_id']


def _stable_available(conn,project_id,policy,superseded_batch_id=None):
    if policy['channel']=='stable':
        published_predecessors=_published_stable_predecessors(conn,project_id)
        temporary=set()
        if superseded_batch_id:
            previous=_batch(conn,superseded_batch_id)
            chain=_stable_predecessor_chain(conn,previous) if previous else None
            _require(chain is not None,'release_predecessor_mismatch','失败构建的前驱链缺少可信回执或存在循环，未解除版本占位')
            temporary={superseded_batch_id,*chain}
        _require(not any(batch['policy']['channel']=='stable' and batch['state']!='published' and not batch.get('abandoned')
            and batch['batch_id'] not in temporary
            and batch['batch_id'] not in published_predecessors
            for batch in _batches(conn,project_id)),'release_stable_pending',
            '项目已有未发布的正式批次；先处理原批次，避免正式版本逆序发布')


def _trusted_stable_predecessor(conn,successor):
    """Validate one immutable successor edge; a failed edge also needs its send receipt."""
    context_keys=('project_id','repository','root_task_id','members','proof','policy','policy_snapshot','workflow',
                  'stable_versions','frozen_stable_versions','version','superseded_build_batch_id')
    project_id=successor.get('project_id')
    old=_batch(conn,successor.get('superseded_build_batch_id'))
    row=db.get_runtime_operation(conn,_scope(project_id),'batch:'+successor['batch_id'])
    inputs=successor.get('inputs') or {}
    if (successor.get('state') not in ('failed','published') or successor['policy']['channel']!='stable'
            or not old or old['policy']['channel']!='stable' or old.get('state')!='failed' or old.get('abandoned')
            or old['project_id']!=project_id or old['repository']!=successor['repository']
            or old['root_task_id']!=successor['root_task_id'] or old['target_sha']==successor.get('target_sha')
            or not _historical_build_source(conn,old)
            or row is None or row['status'] not in ('prepared','accepted') or row['tool']!=BATCH_TOOL or row['harness_id']!='core'
            or any(key not in successor for key in context_keys)):
        return None
    context={key:successor[key] for key in context_keys}
    context_sha=util.sha256_text(util.jdump(context))
    if (context_sha!=successor.get('context_sha256')
            or row['request_sha256']!=runtime_operations.compute_request_sha256(dict(context_sha256=context_sha))
            or successor.get('tag')!='v'+successor['version']):
        return None
    try:
        old_version=tuple(int(part) for part in old['version'].split('.'))
        new_version=tuple(int(part) for part in successor['version'].split('.'))
    except (ValueError,TypeError):
        return None
    if len(old_version)!=3 or len(new_version)!=3 or new_version<=old_version:
        return None
    if successor['state']=='published':
        proof=successor.get('publication_proof') or {}
        if (row['status']!='accepted' or proof.get('target_sha')!=successor.get('target_sha')
                or proof.get('workflow_run_id')!=successor.get('workflow_run_id')
                or type(proof.get('run_attempt')) is not int or proof['run_attempt']<=0
                or type(proof.get('release_id')) is not int or proof['release_id']<=0
                or not isinstance(proof.get('manifest_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',proof['manifest_sha256'])
                or not inputs.get('task_manifest_sha256') or proof.get('task_manifest_sha256')!=inputs['task_manifest_sha256']):
            return None
    else:
        dispatch=db.get_runtime_operation(conn,_scope(project_id),'dispatch:'+successor['batch_id'])
        expected=dict(batch_id=successor['batch_id'],workflow=successor['workflow'],inputs=inputs)
        observed=util.jload(dispatch['result_json']) or {} if dispatch is not None else {}
        if (type(successor.get('workflow_run_id')) is not int or successor.get('dispatch_status')=='not_sent'
                or dispatch is None or dispatch['status']!='accepted' or dispatch['tool']!=DISPATCH_TOOL
                or dispatch['harness_id']!='github'
                or dispatch['request_sha256']!=runtime_operations.compute_request_sha256(expected)
                or observed.get('batch_id')!=successor['batch_id'] or observed.get('workflow_run_id')!=successor['workflow_run_id']):
            return None
    return old


def _stable_predecessor_chain(conn,successor):
    """No partial permission when any edge is damaged, cyclic or unconfirmed."""
    result=[];seen={successor['batch_id']}
    while successor.get('superseded_build_batch_id'):
        old=_trusted_stable_predecessor(conn,successor)
        if old is None or old['batch_id'] in seen:
            return None
        seen.add(old['batch_id']);result.append(old['batch_id']);successor=old
    return result


def _published_stable_predecessors(conn,project_id):
    """A verified publication durably resolves its complete trusted failed chain."""
    result=set()
    for successor in _batches(conn,project_id):
        if successor.get('state')=='published' and successor['policy']['channel']=='stable':
            chain=_stable_predecessor_chain(conn,successor)
            if chain is not None:
                result.update(chain)
    return result


def _prepare(ctx,task_id):
    task=db.require_task(ctx.conn,task_id);policy,revision=_policy(ctx.conn,task)
    _require(policy['enabled'],'release_disabled','项目尚未开启构建发布')
    _require(revision['repository']==task['repository'],'release_repository_changed','项目映射仓库与任务不一致')
    _require(not db.paused(ctx.conn,'delivery') and not db.task_auto_paused(task)
             and not core.automation_of(task).get('manual_takeover'),'release_paused','交付已暂停或转为人工处理')
    root=_root(ctx.conn,task);proof=_software_proof(ctx.conn,root)
    rows=[root]+list(ctx.conn.execute('SELECT * FROM tasks WHERE review_source_task_id=?',(root['task_id'],)))
    members=sorted((_identity(row) for row in rows),key=lambda row:row['task_id'])
    workflow=github.workflow_snapshot(proof['github_repository'],policy['workflow_path'],policy['workflow_ref'])
    superseded=_superseded_build_batch(ctx.conn,root,proof)
    _stable_available(ctx.conn,root['project_id'],policy,superseded)
    releases=github.list_releases(proof['github_repository']);versions,stable=_version_inventory(ctx.conn,root['project_id'],releases)
    version=('.'.join(str(part) for part in (*versions[-1][:2],versions[-1][2]+1)) if versions else policy['version_base'])
    context=dict(project_id=root['project_id'],repository=proof['github_repository'],root_task_id=root['task_id'],
        members=members,proof=proof,policy=policy,policy_snapshot=revision,workflow=workflow,
        stable_versions=[list(v) for v in versions],frozen_stable_versions=stable,version=version)
    if superseded:
        context['superseded_build_batch_id']=superseded
    context['context_sha256']=util.sha256_text(util.jdump(context))
    return context


def _save(conn,batch,now,status=None):
    # Terminal publication and abandonment cannot be undone by an older reader.
    current=_batch(conn,batch['batch_id'])
    if current and (_historical_build_source(conn,current) or current.get('state')=='published' or current.get('abandoned')
            or (current.get('workflow_run_id') and not batch.get('workflow_run_id'))):
        batch.clear();batch.update(current)
        return
    batch['checked_at']=now
    batch['next_check_at']=util.plus_seconds(60,util.parse_ts(now))
    runtime_operations.complete_operation(conn,_scope(batch['project_id']),'batch:'+batch['batch_id'],
        status=status or ('accepted' if batch['state']=='published' else 'prepared'),result=batch,now=now)


def _sync_snapshot(conn,batch):
    from . import release_dispatch_recovery as recovery
    rows=[db.get_runtime_operation(conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id']),
          recovery.receipt(conn,batch)]
    return util.jdump(dict(batch=_batch(conn,batch['batch_id']),operations=[dict(row) if row is not None else None for row in rows]))


def _newer_sync(conn,batch,snapshot):
    if _sync_snapshot(conn,batch)!=snapshot:
        current=_batch(conn,batch['batch_id'])
        return dict(outcome=current['state'],release=_dto(current,True,conn=conn))
    return None


def _failure(conn,batch,now,exc,*,state='blocked',expected_snapshot=None):
    batch=dict(batch,state=state,reason=util.redact(str(exc)),blockers=[dict(code=getattr(exc,'code','release_evidence_unconfirmed'),message=util.redact(str(exc)))])
    with db.write_txn(conn):
        if expected_snapshot is not None:
            newer=_newer_sync(conn,batch,expected_snapshot)
            if newer is not None:return newer
        if _historical_build_source(conn,batch):
            old=_batch(conn,batch['batch_id'])
            value=_dto(old,True,conn=conn)
            value.update(is_historical=True,can_start=False,can_reconcile=False,can_abandon=False,
                reason='原失败批次已归档，保留原回执；新的验证与发布须分别确认')
            return dict(outcome='blocked',release=value)
        _save(conn,batch,now)
    return dict(outcome=batch['state'],release=_dto(batch,True,conn=conn))


def cmd_prepare_task_release(ctx,payload):
    task=db.require_task(ctx.conn,payload['task_id']);attached=_attached(ctx.conn,task)
    if attached is None and _unsent(ctx.conn,_referenced(ctx.conn,task)):
        attached=_referenced(ctx.conn,task)
    if attached:
        return dict(task_id=task['task_id'],context_sha256=attached['context_sha256'],release=projection(ctx.conn,task))
    try:
        context=_prepare(ctx,task['task_id']);value=projection(ctx.conn,task)
        if value.get('state')=='blocked' and not value.get('batch_id'):
            value.update(state='ready',title='待构建发布',reason='已核对当前交付证据，可构建并发布版本',blockers=[],can_start=True)
        value.update(context_sha256=context['context_sha256'],target_sha=context['proof']['target_sha'],version=context['version'])
        return dict(task_id=task['task_id'],context_sha256=context['context_sha256'],release=value)
    except (ReleaseBlocked,closeout._Blocked,util.KanpilotError,gh_client.GhError,git_delivery.GitDeliveryError,OSError,ValueError) as exc:
        value=projection(ctx.conn,task)
        if value is None:
            value=dict(state='blocked',title='发布受阻',required=False,channel=DEFAULT_POLICY['channel'],root_task_id=task['task_id'],
                member_task_ids=[task['task_id']],assets=[],can_start=False,can_reconcile=False)
        value.update(state='blocked',title='发布受阻',reason=util.redact(str(exc)),can_start=False,
                     blockers=[dict(code=getattr(exc,'code','release_not_ready'),message=util.redact(str(exc)))])
        return dict(task_id=task['task_id'],context_sha256=None,release=value)


def _run_matches(batch,run):
    workflow=batch['workflow']
    return (isinstance(run,dict) and type(run.get('id')) is int and run['id']>0
        and run.get('workflow_id')==workflow['id'] and run.get('event')=='workflow_dispatch'
        and run.get('display_title')=='pilot-release-'+batch['batch_id'] and run.get('head_branch')==workflow['ref']
        and run.get('head_sha')==workflow['head_sha'] and (run.get('repository') or {}).get('full_name')==batch['repository']
        and run.get('html_url')=='https://github.com/'+batch['repository']+'/actions/runs/'+str(run['id'])
        and type(run.get('run_attempt')) is int and run['run_attempt']>0)


def _inputs(batch):
    manifest=dict(schema=1,batch_id=batch['batch_id'],repository=batch['repository'],project_id=batch['project_id'],
        root_task_id=batch['root_task_id'],target_sha=batch['target_sha'],
        members=[dict(task_id=m['task_id'],execution_run_id=m['run_id'],plan_sha256=m['plan_sha256'],
                      requirements_sha256=m['requirements_sha256'],head_sha=m['head_sha']) for m in batch['members']],
        channel=batch['policy']['channel'],version=batch['version'],tag=batch['tag'],build_number=batch['build_number'],
        required_assets=batch['policy']['required_assets'])
    raw=util.jdump(manifest)
    inputs=dict(target_sha=batch['target_sha'],release_batch_id=batch['batch_id'],task_manifest_json=raw,
                task_manifest_sha256=util.sha256_text(raw),tag_name=batch['tag'])
    _require(len(util.jdump(inputs).encode())<=60*1024,'release_input_too_large','发布输入超过 60 KiB，未调度')
    return manifest,inputs


def _process_identity():
    birth=process_owner.process_birth(os.getpid())
    _require(birth is not None,'release_writer_unknown','无法核对发布调度进程启动身份')
    return dict(birth,pid=os.getpid())


def _reserve(ctx,context):
    conn=ctx.conn
    with db.write_txn(conn):
        _require(not _pending(conn,context['project_id']),'release_dispatch_unknown','项目已有发布调度结果待确认，不能启动新批次')
        root=db.require_task(conn,context['root_task_id']);policy,revision=_policy(conn,root)
        _require(not any(_attached(conn,db.require_task(conn,m['task_id'])) for m in context['members']),
                 'release_batch_exists','任务已有冻结发布批次，请同步原批次')
        _require(not any(_unsent(conn,_referenced(conn,db.require_task(conn,m['task_id']))) for m in context['members']),
                 'release_abandon_required','成员仍关联确定未发送的旧批次，请先显式弃用')
        _require(policy==context['policy'] and revision==context['policy_snapshot'],'release_policy_changed','发布策略已变化')
        _require(all(_identity(db.require_task(conn,m['task_id']))==m for m in context['members']),
                 'release_members_changed','发布成员身份已变化')
        _versions,stable=_version_inventory(conn,context['project_id'],[])
        _require(stable==context['frozen_stable_versions'],'release_version_changed','已有正式批次占用版本，请重新准备')
        superseded=_superseded_build_batch(conn,root,context['proof'])
        _require(superseded==context.get('superseded_build_batch_id'),
                 'release_predecessor_mismatch','确认期间原失败正式批次的修复归属已变化')
        _stable_available(conn,context['project_id'],policy,superseded)
        existing=_batches(conn,context['project_id'])
        seconds=int(util.parse_ts(ctx.now_iso).timestamp())
        build=max(seconds,max((batch['build_number'] for batch in existing),default=0)+1)
        _require(0<build<=2147483647,'release_build_overflow','构建编号超出平台允许范围')
        batch_id=util.new_uuid();tag='v'+context['version']+('-pilot.'+str(build) if context['policy']['channel']=='prerelease' else '')
        batch=dict(context,batch_id=batch_id,tag=tag,build_number=build,target_sha=context['proof']['target_sha'],
                   state='dispatching',dispatch_status='not_sent',reason='发布批次已冻结，尚未发送构建请求',assets=[],blockers=[],created_at=ctx.now_iso)
        manifest,inputs=_inputs(batch);batch.update(task_manifest=manifest,inputs=inputs)
        runtime_operations.begin_operation(conn,_scope(batch['project_id']),'batch:'+batch_id,'core',BATCH_TOOL,
                                           dict(context_sha256=batch['context_sha256']),now=ctx.now_iso)
        _save(conn,batch,ctx.now_iso)
        for member in batch['members']:
            task=db.require_task(conn,member['task_id'])
            changes=dict(release_ref=dict(batch_id=batch_id,required=task['status']!='done'))
            hold=db.task_extra(task).get('build_failure_hold')
            if hold:
                _require(_build_repair_merged(task),'release_build_repair_hold',
                    '构建修复的新 PR 尚未合并，不能确认再次构建发布')
                changes['build_failure_hold']=dict(hold,release_authorized_batch_id=batch_id,
                    release_authorized_at=ctx.now_iso)
            db.set_extra(conn,task,ctx.now_iso,**changes)
    return batch


def cmd_start_task_release(ctx,payload):
    task=db.require_task(ctx.conn,payload['task_id']);batch=_attached(ctx.conn,task)
    if batch:
        if batch.get('dispatch_status')=='not_sent':
            _require(payload.get('expected_context_sha256')==batch['context_sha256'],
                'release_context_changed','必须确认原冻结批次上下文，才能继续首次发送')
            return _dispatch_reserved(ctx,batch,task)
        return dict(outcome=batch['state'],release=_dto(batch,task['status']!='done',conn=ctx.conn))
    _require(not _unsent(ctx.conn,_referenced(ctx.conn,task)),'release_abandon_required','原批次身份已变化，请先弃用确定未发送的批次')
    pending=_pending(ctx.conn,task['project_id'])
    if pending:
        old=util.jload(pending[0]['result_json']) or {};batch=_batch(ctx.conn,old.get('batch_id'))
        _require(batch is not None,'release_dispatch_unknown','项目已有未知发布操作，不能再次调度')
        return dict(outcome='unknown',release=dict(_dto(batch,task['status']!='done',conn=ctx.conn),state='unknown'))
    expected=payload.get('expected_context_sha256')
    _require(isinstance(expected,str) and re.fullmatch(r'[0-9a-f]{64}',expected),'release_context_required','必须提供当前发布上下文 SHA256')
    context=_prepare(ctx,task['task_id'])
    _require(context['context_sha256']==expected,'release_context_changed','发布目标或证据已变化，请重新准备')
    # Re-reading before reservation binds current Plus, Git, remote members,
    # workflow ref/head and complete version inventory; no SQLite lock spans I/O.
    _require(_prepare(ctx,task['task_id'])==context,'release_context_changed','核对期间发布证据或工作流已变化')
    batch=_reserve(ctx,context)
    return _dispatch_reserved(ctx,batch,task)


def _dispatch_reserved(ctx,batch,task):
    owner=None
    try:
        _require(github.tag_commit(batch['repository'],batch['tag']) is None,'release_tag_collision','发布 tag 已存在，禁止覆盖旧版本或资产')
        _require(_software_proof(ctx.conn,db.require_task(ctx.conn,batch['root_task_id']))==batch['proof']
            and github.workflow_snapshot(batch['repository'],batch['policy']['workflow_path'],batch['policy']['workflow_ref'])==batch['workflow'],
            'release_context_changed','发送前发布证据或工作流 ref/head 已变化，未发送')
        versions,stable=_version_inventory(ctx.conn,batch['project_id'],github.list_releases(batch['repository']))
        expected_versions={tuple(value) for value in batch['stable_versions']}
        expected_stable=list(batch['frozen_stable_versions'])
        if batch['policy']['channel']=='stable':
            expected_versions.add(tuple(int(part) for part in batch['version'].split('.')))
            expected_stable.append(batch['version'])
        _require(set(versions)==expected_versions and stable==sorted(expected_stable),
            'release_version_changed','发送前正式版本列表已变化，保留原批次，未发送')
        process=_process_identity();owner='release-dispatch:'+util.new_uuid()
        with db.write_txn(ctx.conn):
            current=_batch(ctx.conn,batch['batch_id'])
            _require(current is not None and not current.get('abandoned'),
                'release_batch_abandoned','原发布批次已弃用，禁止发送旧批次')
            dispatch_row=db.get_runtime_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'])
            if dispatch_row is not None:
                return dict(outcome=current['state'],release=_dto(current,task['status']!='done',conn=ctx.conn))
            _require(_unsent(ctx.conn,current) and current['context_sha256']==batch['context_sha256'],
                'release_not_unsent','原批次已不再是相同上下文的未发送批次，禁止发送')
            _require(all((member_task:=db.get_task(ctx.conn,member['task_id'])) is not None
                and (db.task_extra(member_task).get('release_ref') or {}).get('batch_id')==batch['batch_id']
                for member in current['members']),
                'release_ref_changed','发布成员已解除或改关联其他批次，禁止发送原批次')
            batch=current
            _require(not _pending(ctx.conn,batch['project_id']),'release_dispatch_unknown','项目已有未知调度，先核对原批次')
            _require(_current(ctx.conn,batch),'release_members_changed','调度前任务成员身份已变化')
            root=db.require_task(ctx.conn,batch['root_task_id']);policy,revision=_policy(ctx.conn,root)
            _require(policy==batch['policy'] and revision==batch['policy_snapshot'] and not db.paused(ctx.conn,'delivery')
                and not db.task_auto_paused(root) and not core.automation_of(root).get('manual_takeover'),
                'release_policy_changed','发送前发布策略或暂停状态已变化，未发送')
            core.acquire_lifecycle(ctx.conn,owner)
            runtime_operations.begin_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'],'github',DISPATCH_TOOL,
                dict(batch_id=batch['batch_id'],workflow=batch['workflow'],inputs=batch['inputs']),now=ctx.now_iso)
            runtime_operations.complete_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'],status='unknown',
                result=dict(batch_id=batch['batch_id'],lifecycle_owner=owner,process_identity=process),now=ctx.now_iso)
            batch.update(state='unknown',dispatch_status='sent_or_unknown',reason='调度结果待确认，不会重复发送');_save(ctx.conn,batch,ctx.now_iso)
        response=github.dispatch(batch['repository'],batch['workflow']['id'],batch['workflow']['ref'],batch['inputs'])
        run_id=response.get('workflow_run_id') if isinstance(response,dict) else None
        _require(type(run_id) is int and run_id>0 and response.get('run_url')=='https://api.github.com/repos/'+batch['repository']+'/actions/runs/'+str(run_id)
            and response.get('html_url')=='https://github.com/'+batch['repository']+'/actions/runs/'+str(run_id),
            'release_dispatch_unconfirmed','调度响应未返回精确运行身份')
        observed=github.run(batch['repository'],run_id)
        _require(_run_matches(batch,observed),'release_run_mismatch','Actions 运行与冻结工作流、ref/head或批次 marker 不符')
        batch.update(workflow_run_id=run_id,workflow_url=observed['html_url'],state='queued',reason='构建已排队')
        with db.write_txn(ctx.conn):
            _require(_current(ctx.conn,batch),'release_members_changed','登记调度结果前任务身份已变化')
            runtime_operations.complete_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'],
                result=dict(batch_id=batch['batch_id'],workflow_run_id=run_id),now=ctx.now_iso)
            _save(ctx.conn,batch,ctx.now_iso);core.release_lifecycle(ctx.conn,owner)
        return dict(outcome='waiting',release=_dto(batch,task['status']!='done',conn=ctx.conn))
    except Exception as exc:
        own_dispatch=db.get_runtime_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'])
        if own_dispatch is not None and own_dispatch['status'] in ('prepared','unknown'):
            return _failure(ctx.conn,batch,ctx.now_iso,exc,state='unknown')
        return _failure(ctx.conn,batch,ctx.now_iso,exc)
    finally:
        if owner:
            with db.write_txn(ctx.conn):
                core.release_lifecycle(ctx.conn,owner)


def _recover_writer(ctx,batch):
    from . import release_dispatch_recovery as recovery
    held=db.get_setting(ctx.conn,'lifecycle_owner','') or ''
    if not held:
        return
    rows=[db.get_runtime_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id']),
          recovery.receipt(ctx.conn,batch)]
    owners=[util.jload(row['result_json']) or {} for row in rows if row is not None]
    owners=[value for value in owners if value.get('lifecycle_owner')==held]
    _require(len(owners)==1,'release_lifecycle_busy','其他生命周期操作正在执行，请稍后核对')
    frozen=owners[0]
    process=frozen.get('process_identity') or {};pid=process.get('pid')
    _require(type(pid) is int and pid>0 and all(type(process.get(k)) is int for k in ('started_seconds','started_microseconds')),
             'release_writer_unknown','原调度进程身份无法确认，不能回收锁')
    birth=process_owner.process_birth(pid)
    dead=any(birth[k]!=process[k] for k in birth) if birth is not None else not util.pid_alive(pid)
    _require(dead,'release_writer_active','原发布调度进程仍在执行或无法确认退出')
    with db.write_txn(ctx.conn):
        _require(db.get_setting(ctx.conn,'lifecycle_owner','')==held,'release_lifecycle_busy','核对期间生命周期锁已变化')
        core.release_lifecycle(ctx.conn,held)


def _verify_publication(batch,run):
    repo=batch['repository'];attempt=run['run_attempt']
    rows=github.jobs(repo,run['id'],attempt)
    for required in batch['policy']['required_jobs']:
        matches=[row for row in rows if isinstance(row,dict) and row.get('name')==required]
        _require(len(matches)==1 and matches[0].get('status')=='completed' and matches[0].get('conclusion')=='success',
                 'release_job_incomplete','必需构建 job 未成功：'+required)
    release=github.release(repo,batch['tag'])
    _require(isinstance(release,dict) and release.get('draft') is False and release.get('tag_name')==batch['tag']
             and release.get('prerelease') is (batch['policy']['channel']=='prerelease')
             and release.get('html_url')=='https://github.com/'+repo+'/releases/tag/'+quote(batch['tag'],safe=''),
             'release_not_published','正式 Release 尚未发布或渠道/tag身份不符')
    _require(github.tag_commit(repo,batch['tag'])==batch['target_sha'],'release_target_mismatch','Release tag 不指向冻结的构建源码')
    raw=github.provenance(repo,run['id'])
    _require(isinstance(raw,bytes) and len(raw)<=1024*1024,'release_manifest_invalid','provenance manifest无效或过大')
    try:
        manifest=json.loads(raw)
    except (ValueError,UnicodeError) as exc:
        raise ReleaseBlocked('release_manifest_invalid','provenance manifest解析失败') from exc
    expected=dict(schema=1,batch_id=batch['batch_id'],repository=repo,target_sha=batch['target_sha'],
        task_manifest_sha256=batch['inputs']['task_manifest_sha256'],task_manifest=batch['task_manifest'],
        workflow_run_id=run['id'],run_attempt=attempt,channel=batch['policy']['channel'],version=batch['version'],
        tag=batch['tag'],build_number=batch['build_number'])
    _require(isinstance(manifest,dict) and all(type(manifest.get(key)) is type(value) and manifest.get(key)==value for key,value in expected.items()),
             'release_manifest_mismatch','发布 manifest 与冻结任务、源码、版本或运行身份不符')
    canonical=util.jdump(manifest['task_manifest'])
    _require(canonical==batch['inputs']['task_manifest_json']
        and util.sha256_text(canonical)==batch['inputs']['task_manifest_sha256'],
        'release_manifest_mismatch','发布 manifest 内嵌任务规范 JSON 与冻结输入不符')
    assets=release.get('assets') or [];provenance=[row for row in assets if isinstance(row,dict) and row.get('name')==MANIFEST_NAME]
    _require(len(provenance)==1,'release_manifest_missing','Release缺少唯一的发布 manifest资产')
    _require(github.asset_bytes(repo,provenance[0])==raw,'release_manifest_different','Actions artifact与Release manifest字节不一致')
    declared=manifest.get('assets')
    _require(isinstance(declared,list) and all(isinstance(row,dict) and isinstance(row.get('name'),str) for row in declared)
        and len({row['name'] for row in declared})==len(declared) and not any(row['name']==MANIFEST_NAME for row in declared),
        'release_assets_invalid','发布 manifest资产声明无效或重复')
    verified=[]
    for name in batch['policy']['required_assets']:
        rows=[row for row in assets if isinstance(row,dict) and row.get('name')==name]
        records=[row for row in declared if row['name']==name]
        _require(len(rows)==len(records)==1,'release_asset_missing','Release缺少必需资产：'+name)
        asset,record=rows[0],records[0]
        _require(type(record.get('size')) is int and record['size']>0 and type(asset.get('size')) is int
            and asset['size']==record['size'] and isinstance(record.get('sha256'),str)
            and re.fullmatch(r'[0-9a-f]{64}',record['sha256']) and asset.get('digest')=='sha256:'+record['sha256'],
            'release_asset_digest_mismatch','Release资产缺少一致的正大小或GitHub sha256 digest：'+name)
        url='https://github.com/'+repo+'/releases/download/'+quote(batch['tag'],safe='')+'/'+quote(name,safe='')
        _require(asset.get('browser_download_url')==url,'release_asset_origin_mismatch','资产下载地址不属于同仓库同tag')
        verified.append(dict(name=name,url=url,size=record['size'],sha256=record['sha256']))
    return dict(release_url=release['html_url'],assets=verified,publication_proof=dict(workflow_run_id=run['id'],run_attempt=attempt,
        target_sha=batch['target_sha'],manifest_sha256=util.sha256_bytes(raw),release_id=release.get('id'),
        task_manifest_sha256=batch['inputs']['task_manifest_sha256'],assets=verified))


def cmd_sync_release_batch(ctx,payload):
    batch=_batch(ctx.conn,payload['batch_id'])
    _require(batch is not None,'release_batch_missing','发布批次不存在')
    _require(not _historical_build_source(ctx.conn,batch),
             'release_build_repair_hold','原失败批次已归档，不能覆盖或重发；构建修复的合并与再次发布须另行确认')
    if batch.get('abandoned'):
        return dict(outcome='abandoned',release=_dto(batch,False,conn=ctx.conn))
    if batch['state']=='published':
        return dict(outcome='published',release=_dto(batch,True,conn=ctx.conn))
    snapshot=_sync_snapshot(ctx.conn,batch)
    try:
        _require(batch.get('dispatch_status')!='not_sent','release_not_sent','冻结批次尚未发送；可继续启动原批次，不会创建新批次')
        _require(_current(ctx.conn,batch),'release_members_changed','发布成员执行、计划、需求或评审身份已变化；保留原批次不回填')
        _recover_writer(ctx,batch)
        if not batch.get('workflow_run_id'):
            runs=github.find_runs(batch['repository'],batch['workflow']['id'])
            matches=[run for run in runs if _run_matches(batch,run)]
            _require(len(matches)<=1,'release_run_ambiguous','找到多个相同批次 marker 的运行，不能猜测归属')
            if not matches:
                reason='GitHub 暂未查到本批次运行；将自动核对，暂不重新发送'
                batch.update(state='unknown',reason=reason,read_failures=0,
                    blockers=[dict(code='release_run_not_observed',message=reason)])
                with db.write_txn(ctx.conn):
                    newer=_newer_sync(ctx.conn,batch,snapshot)
                    if newer is not None:return newer
                    _require(not _historical_build_source(ctx.conn,batch),'release_build_repair_hold','原失败批次已归档，保留原回执')
                    _save(ctx.conn,batch,ctx.now_iso)
                return dict(outcome='unknown',release=_dto(batch,True,conn=ctx.conn))
            run=matches[0];batch.update(workflow_run_id=run['id'],workflow_url=run['html_url'])
        run=github.run(batch['repository'],batch['workflow_run_id'])
        _require(_run_matches(batch,run),'release_run_mismatch','Actions运行与冻结批次、工作流或ref/head不符')
        batch['run_attempt']=run['run_attempt']
        status=run.get('status')
        if status=='completed' and run.get('conclusion')!='success':
            batch.update(state='failed',reason='构建运行未成功；可在Actions手动重跑后同步结果',blockers=[])
        elif status=='completed':
            verified=_verify_publication(batch,run)
            latest=github.run(batch['repository'],batch['workflow_run_id'])
            _require(_run_matches(batch,latest) and latest['run_attempt']==run['run_attempt']
                and latest.get('status')=='completed' and latest.get('conclusion')=='success',
                'release_run_changed','发布核验期间 Actions 运行或 attempt 已变化，未接受旧发布证据')
            _require(_current(ctx.conn,batch),'release_members_changed','写回发布证据前成员身份已变化')
            batch.update(verified,state='published',reason='安装包与Release发布证据已核验',blockers=[])
        else:
            _require(status in ('queued','in_progress','waiting','pending','requested'),'release_run_status_unknown','Actions运行状态未知')
            batch.update(state='building' if status=='in_progress' else 'queued',reason='构建中' if status=='in_progress' else '等待构建运行',blockers=[])
        batch['read_failures']=0
        with db.write_txn(ctx.conn):
            newer=_newer_sync(ctx.conn,batch,snapshot)
            if newer is not None:return newer
            _require(not _historical_build_source(ctx.conn,batch),'release_build_repair_hold','原失败批次已归档，保留原回执')
            _require(_current(ctx.conn,batch),'release_members_changed','保存发布回执前成员身份已变化')
            _save(ctx.conn,batch,ctx.now_iso)
            row=db.get_runtime_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'])
            if row is not None:
                runtime_operations.complete_operation(ctx.conn,_scope(batch['project_id']),'dispatch:'+batch['batch_id'],
                    result=dict(batch_id=batch['batch_id'],workflow_run_id=run['id']),now=ctx.now_iso)
            from . import release_dispatch_recovery as recovery
            recovery.observe(ctx.conn,batch,run['id'],ctx.now_iso)
        return dict(outcome='published' if batch['state']=='published' else 'waiting',release=_dto(batch,True,conn=ctx.conn))
    except (ReleaseBlocked,util.KanpilotError,gh_client.GhError,git_delivery.GitDeliveryError,OSError,ValueError) as exc:
        state=('unknown' if not batch.get('workflow_run_id') and batch.get('dispatch_status')!='not_sent' else 'blocked')
        if isinstance(exc,gh_client.GhError) and exc.kind in ('timeout','transient'):
            # A failed GET says nothing about a running build or accepted dispatch.
            # Keep its last observed stage so the existing scheduler can reconcile
            # it; never resend the workflow, and stop after three failed polls.
            batch['read_failures']=int(batch.get('read_failures') or 0)+1
            if batch['read_failures']<3:
                if (batch['state'] in ('queued','building','publishing')
                        or (batch['state']=='unknown' and batch.get('dispatch_status')=='sent_or_unknown'
                            and any(row['operation_id']=='dispatch:'+batch['batch_id']
                                    for row in _pending(ctx.conn,batch['project_id'])))):
                    state=batch['state']
                    exc=gh_client.GhError(exc.kind,'发布结果读取暂时失败，将自动核对（'
                        +str(batch['read_failures'])+'/3）：'+exc.message)
            else:
                state='blocked'
        return _failure(ctx.conn,batch,ctx.now_iso,exc,state=state,expected_snapshot=snapshot)


def cmd_sync_release_batches(ctx,payload):
    limit=payload.get('limit',10)
    if type(limit) is not int or not 1<=limit<=50:
        raise util.bad_request('limit须为1..50整数')
    batches=[batch for batch in _batches(ctx.conn) if batch['state']!='published' and not batch.get('abandoned')
             and not _historical_build_source(ctx.conn,batch)][:limit]
    return dict(results=[dict(batch_id=batch['batch_id'],**cmd_sync_release_batch(ctx,dict(batch_id=batch['batch_id']))) for batch in batches])


def sync_existing(ctx,project_ids=None,limit=3):
    """Bounded scheduler poll; never creates, resends or retries failed batches."""
    _require(type(limit) is int and 1<=limit<=50,'bad_request','limit须为1..50整数')
    allowed=set(project_ids) if project_ids is not None else None
    batches=[batch for batch in _batches(ctx.conn) if not batch.get('abandoned')
        and not _historical_build_source(ctx.conn,batch)
        and (allowed is None or batch['project_id'] in allowed)
        and (not batch.get('next_check_at') or util.parse_ts(batch['next_check_at'])<=util.parse_ts(ctx.now_iso))
        and (batch['state'] in ('queued','building','publishing') or (batch['state']=='unknown'
            and batch.get('dispatch_status')=='sent_or_unknown'
            and any(row['operation_id']=='dispatch:'+batch['batch_id']
                    for row in _pending(ctx.conn,batch['project_id']))))][:limit]
    return dict(results=[dict(batch_id=batch['batch_id'],**cmd_sync_release_batch(ctx,dict(batch_id=batch['batch_id']))) for batch in batches])


def cmd_abandon_unsent_release(ctx,payload):
    """Explicitly retire only a batch that provably never reserved a send."""
    with db.write_txn(ctx.conn):
        batch=_batch(ctx.conn,payload['batch_id'])
        _require(batch is not None,'release_batch_missing','发布批次不存在')
        _require(payload.get('expected_context_sha256')==batch['context_sha256'],
                 'release_context_changed','冻结批次上下文已变化，未弃用')
        _require(_unsent(ctx.conn,batch),'release_abandon_forbidden','仅从未存在调度回执的未发送批次可以弃用；未知或已发送批次禁止弃用')
        batch.update(abandoned=True,abandoned_at=ctx.now_iso,state='blocked',reason='已显式弃用未发送批次，保留审计记录',blockers=[])
        _save(ctx.conn,batch,ctx.now_iso)
        for member in batch['members']:
            task=db.get_task(ctx.conn,member['task_id'])
            if task is not None and (db.task_extra(task).get('release_ref') or {}).get('batch_id')==batch['batch_id']:
                extra=db.task_extra(task);extra.pop('release_ref',None)
                db.update_task(ctx.conn,task['task_id'],ctx.now_iso,extra_json=extra)
    value=_dto(batch,False,conn=ctx.conn);value.update(can_start=False,can_reconcile=False)
    return dict(outcome='abandoned',release=value)


def after_merge(ctx,task,payload=None):
    value=projection(ctx.conn,task)
    if db.task_extra(task).get('external_build_repair_ref') and _attached(ctx.conn,task) is None:
        return dict(outcome='blocked',release=value)
    hold=db.task_extra(task).get('build_failure_hold')
    if hold:
        authorized=_attached(ctx.conn,task)
        if not authorized or hold.get('release_authorized_batch_id')!=authorized['batch_id']:
            return dict(outcome='blocked',release=value)
    if value is None:
        return dict(outcome='disabled',release=None)
    batch=_attached(ctx.conn,task)
    if batch:
        if batch.get('dispatch_status')=='not_sent' and batch['state']=='dispatching':
            result=_dispatch_reserved(ctx,batch,task)
            return dict(outcome='blocked' if result['release']['state']=='blocked' else 'waiting',release=result['release'])
        if batch['state'] in ('failed','blocked','unknown','dispatching'):
            return dict(outcome='blocked' if batch['state'] in ('failed','blocked') else 'waiting',release=_dto(batch,task['status']!='done',conn=ctx.conn))
        if batch['state']!='published' and not util.ts_due(batch.get('next_check_at'),util.parse_ts(ctx.now_iso)):
            return dict(outcome='waiting',release=_dto(batch,task['status']!='done',conn=ctx.conn))
        result=cmd_sync_release_batch(ctx,dict(batch_id=batch['batch_id']))
        return dict(outcome='published' if result['release']['state']=='published' else
                    'blocked' if result['release']['state'] in ('blocked','failed') else 'waiting',release=result['release'])
    if task['status']=='done':
        return dict(outcome='disabled',release=value)
    prepared=cmd_prepare_task_release(ctx,dict(task_id=task['task_id']))
    if not prepared.get('context_sha256') or not prepared['release'].get('can_start'):
        return dict(outcome='waiting' if prepared['release'].get('state')=='waiting_group' else 'blocked',release=prepared['release'])
    result=cmd_start_task_release(ctx,dict(task_id=task['task_id'],expected_context_sha256=prepared['context_sha256']))
    return dict(outcome='published' if result['release']['state']=='published' else
                'blocked' if result['release']['state'] in ('blocked','failed') else 'waiting',release=result['release'])
