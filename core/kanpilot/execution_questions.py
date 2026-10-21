"""Durable execution input relay (§5.28): SQLite owns questions and wait time.

Runtime mutations occur only through receipted clients outside write transactions.
Unknown deliveries freeze answers; neither polling nor reconnect resends them.
"""
from __future__ import annotations

import copy
import os
import json
import uuid
from pathlib import Path

from . import constants, core, db, dsh_events, headless_adapter as headless, round_store, runtime_client, runtime_operations, util

MAX_QUESTIONS = 20
MAX_OPTIONS = 100
MAX_QUESTIONS_BYTES = 64 * 1024
MAX_ANSWERS_BYTES = 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 512:
        raise util.bad_request('执行问题身份必须是非空字符串且不超过512字节')
    return value


def normalize_questions(questions):
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise util.bad_request('执行问题须为1至20题')
    if len(canonical(questions).encode('utf-8')) > MAX_QUESTIONS_BYTES:
        raise util.bad_request('执行问题超过64 KiB，请在 Plus 回答')
    result = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise util.bad_request('执行问题格式无效')
        title = q.get('title', q.get('question'))
        if not isinstance(title, str) or not title.strip():
            raise util.bad_request('执行问题缺少题目')
        description = q.get('description', '')
        options = q.get('options', [])
        if not isinstance(description, str) or not isinstance(options, list) or len(options) > MAX_OPTIONS:
            raise util.bad_request('执行问题描述或选项无效')
        normalized = []
        for j, option in enumerate(options):
            if isinstance(option,str): option={'label':option}
            if not isinstance(option, dict) or not isinstance(option.get('label'), str) or not option['label'].strip() or not isinstance(option.get('description', ''), str):
                raise util.bad_request('执行问题选项无效')
            normalized.append({'id':f'o{j}', 'label':option['label'], 'description':option.get('description','')})
        entry = {'id':f'q{i}', 'title':title, 'description':description, 'options':normalized}
        for name, source, default in (('multi_select','multiSelect',False), ('allow_custom','allowCustom',True), ('required','required',False)):
            value = q.get(name, q.get(source, default))
            if not isinstance(value, bool):
                raise util.bad_request('执行问题选择规则无效')
            entry[name] = value
        result.append(entry)
    if len(canonical(result).encode('utf-8')) > MAX_QUESTIONS_BYTES:
        raise util.bad_request('规范化执行问题超过64 KiB，请在 Plus 回答')
    return result


def validate_wire_answers(answers, session_id, request_id, tool_call_id):
    if not isinstance(answers, list) or not 1 <= len(answers) <= MAX_QUESTIONS:
        raise util.bad_request('执行答案须为1至20题')
    for row in answers:
        if row is not None and (not isinstance(row, list) or not 1 <= len(row) <= MAX_OPTIONS or any(not isinstance(v,str) or not v.strip() for v in row)):
            raise util.bad_request('每题答案必须是 null 或至多100个非空字符串')
    if len(canonical(answers).encode('utf-8')) > MAX_ANSWERS_BYTES:
        raise util.bad_request('执行答案超过1 MiB')
    args = {'sessionId':_identity(session_id), 'requestId':_identity(request_id), 'expectedToolCallId':_identity(tool_call_id), 'answers':answers,'confirm':True}
    # Same encoding as Plus client; allow its bounded numeric request id.
    envelope = {'jsonrpc':'2.0','id':2**63-1,'method':'tools/call','params':{'name':'pi_asks_resolve','arguments':args}}
    if len(json.dumps(envelope, ensure_ascii=False).encode('utf-8')) > constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES:
        raise util.bad_request('完整执行答案 MCP 请求超过2 MiB')
    return answers


def normalize_answers(question, answers, *, draft=False):
    questions = question['questions']
    if not isinstance(answers,list) or len(answers) != len(questions):
        raise util.bad_request('答案必须按原顺序逐题对应，不能遗漏')
    result, wire = [], []
    for q, answer in zip(questions, answers):
        if not isinstance(answer,dict) or set(answer) != {'question_id','option_ids','text','skipped'} or answer.get('question_id') != q['id']:
            raise util.bad_request('答案题目身份或字段无效')
        ids, text, skipped = answer.get('option_ids'), answer.get('text'), answer.get('skipped')
        if not isinstance(ids,list) or any(not isinstance(v,str) for v in ids) or len(ids) != len(set(ids)) or not isinstance(text,str) or not isinstance(skipped,bool):
            raise util.bad_request('执行答案格式或重复选项无效')
        available = {o['id'] for o in q['options']}
        if set(ids) - available:
            raise util.bad_request('答案包含未知选项')
        if skipped and (ids or text):
            raise util.bad_request('跳过的答案不得包含选项或文本')
        if not q['multi_select'] and (len(ids) > 1 or ids and text.strip()):
            raise util.bad_request('单选自定义文本与选项互斥')
        if text.strip() and not q['allow_custom']:
            raise util.bad_request('本题不接受自定义文本')
        ordered = [o['id'] for o in q['options'] if o['id'] in ids]
        values = [o['label'] for o in q['options'] if o['id'] in ids]
        if text.strip(): values.append(text)
        if len(values) > MAX_OPTIONS or (not draft and not skipped and not values):
            raise util.bad_request('未跳过的答案须有有效值且不超过100项')
        result.append({'question_id':q['id'],'option_ids':ordered,'text':text,'skipped':skipped})
        wire.append(None if skipped or draft and not values else values)
    if len(canonical(result).encode('utf-8')) > MAX_ANSWERS_BYTES:
        raise util.bad_request('执行答案超过1 MiB')
    validate_wire_answers(wire,question['session_id'],question['request_id'],question['tool_call_id'])
    return result,wire


def records(task):
    return db.task_extra(task).get('execution_questions') or []


def open_records(task, run_id=None):
    run_id = run_id or task['run_id']
    return sorted([q for q in records(task) if q['run_id'] == run_id and q['state']=='open'],key=lambda q:(q['opened_at'],q['request_id']))


def guard_active(task):
    if open_records(task):
        raise util.KanpilotError('conflict','执行轮次正在等待回答；请回答、全部跳过或停止本轮')


def wait_seconds(task, now, *, run_id=None, since=None):
    current = util.parse_ts(now) if isinstance(now,str) else now
    lower = util.parse_ts(since) if isinstance(since,str) else since
    intervals=[]
    for item in db.task_extra(task).get('execution_input_wait_intervals') or []:
        if run_id is not None and item.get('run_id') != run_id: continue
        start,end = util.parse_ts(item.get('start')),util.parse_ts(item.get('end')) or current
        if start is None: continue
        start=max(start,lower) if lower else start
        end=min(end,current)
        if end>start: intervals.append((start,end))
    intervals.sort()
    merged=[]
    for start,end in intervals:
        if merged and start<=merged[-1][1]: merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else: merged.append((start,end))
    return sum((end-start).total_seconds() for start,end in merged)


def view(task, now=None, conn=None):
    now = now or util.now_iso()
    current = [q for q in records(task) if q['run_id']==task['run_id']]
    opened=open_records(task)
    if not current: return None
    q=copy.deepcopy(opened[0] if opened else max(current,key=lambda q:(q.get('closed_at') or q['opened_at'],q['request_id'])))
    q['queued']=max(0,len(opened)-1)
    end=util.parse_ts(q.get('closed_at')) or (util.parse_ts(now) if isinstance(now,str) else now)
    q['wait_seconds']=max(0,(end-util.parse_ts(q['opened_at'])).total_seconds())
    q['observer_alive']=False
    if conn is not None:
        try:
            job,run_dir=_job(conn,task,task['run_id'])
            row=round_store.require_round(conn,task['task_id'],task['run_id'])
            if row['backend']=='codex':
                from .codex_execution import job_alive
                q['observer_alive']=job_alive(job)
            else:
                from .cmd_dispatch import _observer_pid_alive
                q['observer_alive']=any(_observer_pid_alive(conn,task['task_id'],task['run_id'],pid,run_dir,job) for pid in (job.get('observe_pid'),row['pid']))
        except util.KanpilotError: pass
    # Internal receipts remain available for audits but not the stable UI DTO.
    for key in ('operations','stop_operation_id','abort_operation_id','stop_requested_at','termination_reason'): q.pop(key,None)
    return q


def _result(ctx, task_id, **extra):
    task=db.require_task(ctx.conn,task_id)
    return {'task_id':task_id,'run_id':task['run_id'],'execution_question':view(task,util.now_iso(),ctx.conn),**extra}


def preview(question):
    if not question or question['state']!='open': return None
    return {'request_id':question['request_id'],'count':len(question['questions']),
        'first_title':question['questions'][0]['title']}


def _job(conn, task, run_id):
    if task['run_id'] != run_id:
        raise util.KanpilotError('conflict','执行问题不属于当前轮次')
    row=round_store.require_round(conn,task['task_id'],run_id)
    if row['backend'] not in ('pi_desktop','codex'): raise util.KanpilotError('conflict','该后端不支持执行问题')
    try: run_dir=headless.resolve_source(conn).run_dir(task['task_id'],run_id)
    except headless.HeadlessError as exc: raise util.KanpilotError('validation_failed',exc.detail)
    job,error=dsh_events.read_job(run_dir,task['task_id'],run_id,backend=row['backend'])
    if job is None or not job.get('session_id') or not job.get('turn_id'):
        raise util.KanpilotError('validation_failed','执行问题运行记录身份不可核验')
    if row['backend']=='codex':
        from .codex_execution import InputClient
        job=InputClient(conn,task,run_id,read_only=True).job
    return job,run_dir


def _match(q,p):
    return all(q.get(k)==p.get(k) for k in ('run_id','session_id','turn_id','request_id','tool_call_id'))


def _store(conn,task,qs,now,*,before,failed=None):
    after=[q for q in qs if q['run_id']==task['run_id'] and q['state']=='open']
    intervals=copy.deepcopy(db.task_extra(task).get('execution_input_wait_intervals') or [])
    if not before and after: intervals.append({'run_id':task['run_id'],'start':now,'end':None})
    if before and not after:
        for item in intervals:
            if item['run_id']==task['run_id'] and item.get('end') is None: item['end']=now
    db.set_extra(conn,task,now,execution_questions=qs,execution_input_wait_intervals=intervals)
    fresh=db.require_task(conn,task['task_id'])
    if failed:
        core.block_task(conn,fresh,now,failed,'execution_failed','execution-input',stage='monitor')
    elif after:
        if task['status']=='running' or task['status']=='needs_user' and task['failure_code'] in ('observation_blocked','execution_input_required'):
            first=sorted(after,key=lambda q:(q['opened_at'],q['request_id']))[0]
            summary=f"执行中提问：{first['questions'][0]['title']}（共 {len(first['questions'])} 题）"
            core.block_task(conn,fresh,now,summary,'execution_input_required','execution-input',stage='monitor')
    elif task['status']=='needs_user' and task['failure_code']=='execution_input_required':
        core.transition_task(conn,fresh,'running',now,'execution-input',**core.clear_block_fields())


def _close(conn,task,qs,q,now,state,detail=None):
    before=bool(open_records(task))
    q.update(state=state,closed_at=now)
    if state=='answered':
        q['answered_by']='kaneopilot' if q['delivery']=='accepted' else ('unknown' if q['delivery'] in ('unknown','submitting') or any(o.get('delivery')=='unknown' for o in q.get('operations',[])) else 'plus')
    if q['delivery']=='submitting': q['delivery']='unknown'
    db.add_event(conn,now,'execution_question_answered' if state=='answered' else 'execution_question_closed',task_id=task['task_id'],actor='execution-input',detail={'request_id':q['request_id'],'run_id':q['run_id'],'state':state,'answered_by':q['answered_by'],'detail':detail})
    _store(conn,task,qs,now,before=before,failed=(detail or 'execution_question_'+state) if state in ('expired','stopped') else None)


def cmd_report_execution_question(ctx,payload):
    now=ctx.now_iso
    with db.write_txn(ctx.conn):
        task=db.require_task(ctx.conn,payload['task_id']); job,_=_job(ctx.conn,task,payload['run_id'])
        for key in ('session_id','turn_id'):
            if job.get(key)!=payload.get(key): raise util.KanpilotError('conflict','执行问题与实际 job 身份不符')
        for key in ('session_id','turn_id','request_id','tool_call_id'): _identity(payload.get(key))
        qs=copy.deepcopy(records(task))
        q=next((q for q in qs if q['session_id']==payload['session_id'] and q['request_id']==payload['request_id']),None)
        state=payload['state']
        if state not in ('open','closed','expired'): raise util.bad_request('执行问题回报状态无效')
        if q and not _match(q,payload): raise util.KanpilotError('conflict','执行问题身份不匹配')
        if state=='open':
            if job.get('backend')=='codex':
                pending=job.get('pending_input') or {}
                if (job.get('status')!='waiting_input' or pending.get('requestId')!=payload['request_id']
                        or pending.get('toolCallId')!=payload['tool_call_id'] or pending.get('turnId')!=payload['turn_id']
                        or pending.get('questions')!=payload.get('questions')):
                    raise util.KanpilotError('conflict','Codex 问题与持久等待记录不符')
            try:
                questions=normalize_questions(payload.get('questions')); answerable=True; rejected=None
            except util.KanpilotError as exc:
                if job.get('backend')=='codex': raise
                questions=[{'id':'q0','title':'问题格式超限或无效，请在 Plus 回答','description':'','multi_select':False,'allow_custom':False,'required':False,'options':[]}]
                answerable=False; rejected=exc.message
            if not job.get('scope_id'):
                answerable=False; rejected='历史会话只读恢复，请在 Plus 回答'
            fingerprint=util.sha256_bytes(canonical(questions if rejected=='历史会话只读恢复，请在 Plus 回答' or answerable else {'rejected':True,'sha256':util.sha256_bytes(canonical(payload.get('questions')).encode('utf-8'))}).encode('utf-8'))
            if q:
                if q['questions_sha256'] != fingerprint: raise util.KanpilotError('conflict','执行问题内容已变化')
                return _result(ctx,task['task_id'],deduplicated=True)
            if task['status']!='running' and not (task['status']=='needs_user' and task['failure_code'] in ('observation_blocked','execution_input_required')):
                raise util.KanpilotError('conflict','任务当前不能接收执行问题')
            before=bool(open_records(task))
            q={**{k:payload[k] for k in ('run_id','session_id','turn_id','request_id','tool_call_id')},'source':'codex' if job.get('backend')=='codex' else 'plus_asktool','questions_sha256':fingerprint,'answerable':answerable,'state':'open','delivery':'none','answered_by':None,'opened_at':now,'closed_at':None,'questions':questions,'draft':[{'question_id':v['id'],'option_ids':[],'text':'','skipped':False} for v in questions],'submitted':None,'operations':[]}
            if rejected: q['rejection_reason']=rejected
            qs.append(q); _store(ctx.conn,task,qs,now,before=before)
            db.add_event(ctx.conn,now,'execution_question_opened',task_id=task['task_id'],actor='execution-input',detail={'request_id':q['request_id'],'run_id':q['run_id'],'questions_sha256':fingerprint,'answerable':answerable})
        elif q and q['state']=='open':
            if job.get('backend')=='codex':
                raise util.KanpilotError('conflict','Codex 问题须按持久答案消费或停止证据核对关闭')
            _close(ctx.conn,task,qs,q,now,('expired' if q.get('termination_reason')=='execution_question_expired' else 'stopped') if q.get('stop_operation_id') else ('answered' if state=='closed' else 'expired'),payload.get('detail'))
    return _result(ctx,payload['task_id'])


def _answer_target(conn,payload, *, allow_closed=False):
    task=db.require_task(conn,payload['task_id']); _job(conn,task,payload.get('run_id') or task['run_id'])
    qs=copy.deepcopy(records(task)); q=next((q for q in qs if q['run_id']==task['run_id'] and q['request_id']==payload['request_id']),None)
    if q is None or (q['state']!='open' and not allow_closed) or not q['answerable'] or q['questions_sha256']!=payload['questions_sha256']:
        raise util.KanpilotError('conflict','执行问题已关闭、不可答或指纹不符')
    return task,qs,q


def cmd_save_execution_answers(ctx,payload):
    with db.write_txn(ctx.conn):
        task,qs,q=_answer_target(ctx.conn,payload)
        answers,_=normalize_answers(q,payload['answers'],draft=True)
        if (q['delivery'] in ('submitting','unknown') or q.get('source')=='codex' and q['delivery']=='accepted') and (q.get('submitted') or {}).get('answers')!=answers:
            raise util.KanpilotError('conflict','送达结果未确定，冻结答案不可修改')
        q['draft']=answers; db.set_extra(ctx.conn,task,ctx.now_iso,execution_questions=qs)
    return _result(ctx,payload['task_id'])


def _client(conn,task,run_id,*,read_only,connect=True):
    job,run_dir=_job(conn,task,run_id)
    if job.get('backend')=='codex':
        from .codex_execution import InputClient
        return InputClient(conn,task,run_id,read_only=read_only),job
    try: spec=util.jload((run_dir/'spec.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): raise util.KanpilotError('validation_failed','缺少原执行 spec')
    if not isinstance(spec,dict) or spec.get('task_id')!=task['task_id'] or spec.get('run_id')!=run_id:
        raise util.KanpilotError('conflict','原执行 spec 身份不符')
    config=spec.get('config') or {}
    row=round_store.require_round(conn,task['task_id'],run_id)
    frozen=util.jload(row['config_json']) or {}
    if job.get('config') != config or frozen and frozen != config:
        raise util.KanpilotError('conflict','执行问题原配置快照不符')
    scope_id=runtime_client.stable_scope_id('execution',task_id=task['task_id'],run_id=run_id)
    if read_only and not job.get('scope_id'):
        client=runtime_client.HistoricalReadClient(config.get('connection_file'),timeout=10)
        if connect: client.connect(required_tools=constants.PI_DESKTOP_INPUT_TOOLS)
        return client,job
    if job.get('scope_id')!=scope_id or not runtime_operations.session_owned(conn,scope_id,job['session_id']):
        raise util.KanpilotError('forbidden','执行 session 无 accepted create 归属证据')
    client=runtime_client.RuntimeClient(role='execution',connection_file=config.get('connection_file'),project_path=spec.get('cwd'),runtime_config=config,task_id=task['task_id'],run_id=run_id,read_only=read_only,lease_seconds=spec.get('timeout_seconds'),timeout=10)
    if connect: client.connect(required_tools=constants.PI_DESKTOP_INPUT_TOOLS)
    return client,job


def ensure_observer(ctx,task_id,run_id):
    from . import cmd_dispatch
    try: return cmd_dispatch.cmd_round_observe(ctx,{'task_id':task_id,'run_id':run_id,'executor':'execution-input'})
    except (util.KanpilotError,OSError) as exc:
        return {'status':'deferred','code':exc.code if isinstance(exc,util.KanpilotError) else 'internal'}


def cmd_submit_execution_answers(ctx,payload):
    operation=_identity(payload['operation_id'])
    with db.write_txn(ctx.conn):
        task,qs,q=_answer_target(ctx.conn,payload,allow_closed=True)
        _client(ctx.conn,task,payload['run_id'],read_only=False,connect=False)
        if payload.get('skip_all') is True:
            if 'answers' in payload: raise util.bad_request('skip_all 与 answers 互斥')
            raw=[{'question_id':v['id'],'option_ids':[],'text':'','skipped':True} for v in q['questions']]
        else: raw=payload.get('answers')
        answers,wire=normalize_answers(q,raw)
        old=next((o for o in q['operations'] if o['operation_id']==operation),None)
        if old:
            if old['answers']!=answers: raise util.KanpilotError('conflict','operation_id 已用于不同答案')
            return _result(ctx,task['task_id'],status=old['delivery'],deduplicated=True)
        if q.get('source')=='codex' and q['delivery'] in ('submitting','unknown','accepted'):
            raise util.KanpilotError('conflict','Codex 答案已提交或送达未知，禁止另建操作重发')
        if q['state']!='open': raise util.KanpilotError('conflict','执行问题已关闭')
        if q['delivery']=='submitting': raise util.KanpilotError('conflict','执行答案正在提交')
        if q['delivery']=='unknown' and q['submitted']['answers']!=answers:
            raise util.KanpilotError('conflict','送达结果未知，只能显式重试同一冻结答案')
        q.update(draft=answers,delivery='submitting',submitted={'answers':answers,'operation_id':operation,'at':ctx.now_iso,'by':payload.get('by') or 'kaneopilot'})
        q['operations'].append({'operation_id':operation,'answers':answers,'delivery':'submitting','at':ctx.now_iso,'pid':os.getpid()})
        db.set_extra(ctx.conn,task,ctx.now_iso,execution_questions=qs)
        expected=copy.deepcopy(q)
    delivery='unknown'; result=None
    try:
        client,_=_client(ctx.conn,task,payload['run_id'],read_only=False)
        result=client.input_respond(q['session_id'],q['request_id'],q['tool_call_id'],wire,operation_id=operation)
        delivery='accepted' if result.get('status')=='answered' else 'not_pending'
    except util.KanpilotError:
        # Failure may be after send. Never infer not_applied from the snapshot.
        pass
    with db.write_txn(ctx.conn):
        fresh=db.require_task(ctx.conn,task['task_id']); qs=copy.deepcopy(records(fresh))
        target=next((v for v in qs if _match(v,expected) and v['questions_sha256']==expected['questions_sha256']),None)
        if target:
            receipt=next((o for o in target['operations'] if o['operation_id']==operation),None)
            if receipt: receipt['delivery']=delivery
            current=(target.get('submitted') or {}).get('operation_id')==operation and fresh['run_id']==expected['run_id'] and target['state']=='open'
            if current:
                target['delivery']=delivery
                if delivery=='accepted' and target.get('source')!='codex': _close(ctx.conn,fresh,qs,target,util.now_iso(),'answered')
                else: db.set_extra(ctx.conn,fresh,util.now_iso(),execution_questions=qs)
            else: db.set_extra(ctx.conn,fresh,util.now_iso(),execution_questions=qs)
    if delivery in ('not_pending','unknown') or q.get('source')=='codex':
        cmd_reconcile_execution_question(ctx,{'task_id':task['task_id'],'run_id':payload['run_id']})
    observer=ensure_observer(ctx,task['task_id'],payload['run_id'])
    return _result(ctx,task['task_id'],status=delivery,observer=observer)


def cmd_reconcile_execution_question(ctx,payload):
    task=db.require_task(ctx.conn,payload['task_id']); job,_=_job(ctx.conn,task,payload['run_id'])
    with db.write_txn(ctx.conn):
        task=db.require_task(ctx.conn,payload['task_id']); qs=copy.deepcopy(records(task))
        changed=False
        for q in qs:
            if q['run_id']==payload['run_id'] and q['state']=='open' and q['delivery']=='submitting':
                operation=next((o for o in q['operations'] if o['operation_id']==q['submitted']['operation_id']),None)
                if operation and not util.pid_alive(operation.get('pid')):
                    operation['delivery']='unknown'; q['delivery']='unknown'; changed=True
        if changed: db.set_extra(ctx.conn,task,util.now_iso(),execution_questions=qs)
    task=db.require_task(ctx.conn,payload['task_id']); opened=copy.deepcopy(open_records(task,payload['run_id']))
    if job.get('backend')=='codex':
        return _reconcile_codex(ctx,task,payload['run_id'],opened)
    if not opened: return _result(ctx,task['task_id'])
    try:
        client,job=_client(ctx.conn,task,payload['run_id'],read_only=True)
        status=client.agent_status(job['session_id']); inner=status.get('status') or status
        pending=client.input_pending(job['session_id'])
        if pending.get('sessionId')!=job['session_id']: raise util.KanpilotError('conflict','pending session 身份不符')
        ids={a['requestId'] for a in pending['asks']}
        observations={}; collector=None
        for q in opened:
            record=client.turn_get(q['session_id'],q['turn_id'],require_identity=True)
            from .pi_desktop_worker import _turn_identity_matches,_turn_status,_Collector
            if not _turn_identity_matches(record,q['session_id'],q['turn_id']): raise util.KanpilotError('conflict','turn 身份不符')
            state=_turn_status(record)
            observed=None
            if state in ('completed','error','aborted'):
                observed='expired'
                if state=='completed' and not q.get('stop_operation_id'):
                    if collector is None:
                        collector=_Collector(client,q['session_id'],None,'')
                        collector.poll()
                    if (not collector.truncated and collector.read_error is None and not collector.unrecognized
                            and collector.input_resolutions.get((q['turn_id'],q['tool_call_id']))=='answered'):
                        observed='answered'
                if q.get('stop_operation_id') and q.get('termination_reason')!='execution_question_expired': observed='stopped'
            elif state=='running' and inner.get('currentTurnId')==q['turn_id'] and inner.get('isRunning') is True and q['request_id'] not in ids:
                observed='answered'
            observations[q['request_id']]=observed
        for expected in opened:
            observed=observations[expected['request_id']]
            if observed:
                with db.write_txn(ctx.conn):
                    fresh=db.require_task(ctx.conn,task['task_id']); _job(ctx.conn,fresh,payload['run_id'])
                    qs=copy.deepcopy(records(fresh)); target=next((q for q in qs if _match(q,expected) and q['questions_sha256']==expected['questions_sha256'] and q['state']=='open'),None)
                    if target: _close(ctx.conn,fresh,qs,target,util.now_iso(),observed,target.get('termination_reason'))
    except util.KanpilotError:
        return _result(ctx,task['task_id'],status='unknown')
    return _result(ctx,task['task_id'])


def _codex_consumed(conn,client,q):
    """A missing ask is never delivery proof; bind the original consumed mailbox."""
    submitted=q.get('submitted') or {}
    operation=submitted.get('operation_id')
    if not operation: return False
    record=client.turn_get(q['session_id'],q['turn_id'],require_identity=True)
    if (record.get('sessionId')!=q['session_id'] or record.get('turnId')!=q['turn_id']
            or record.get('source')!='codex_adapter' or record.get('inputResolution')!='answered'
            or record.get('answerOperationId')!=operation or not util.parse_ts(record.get('answerConsumedAt'))):
        return False
    _,wire=normalize_answers(q,submitted['answers'])
    payload={'scope_id':client.scope_id,'session_id':q['session_id'],'request_id':q['request_id'],
             'tool_call_id':q['tool_call_id'],'answers':wire,'operation_id':operation}
    receipt=runtime_operations.get_operation(conn,client.scope_id,operation)
    return bool(receipt is not None and receipt['harness_id']=='codex'
                and receipt['tool']=='codex_execution_input' and receipt['status']=='accepted'
                and receipt['request_sha256']==runtime_operations.compute_request_sha256(payload)
                and record.get('answerSha256')==util.sha256_bytes(util.jdump(payload).encode('utf-8')))


def _codex_stop_accepted(conn,scope,stop,session_id):
    if stop.get('session_id')!=session_id: return False
    for field,tool in (('operation_id','kp_runtime_stop'),('abort_operation_id','kp_runtime_abort')):
        operation=stop.get(field)
        receipt=runtime_operations.get_operation(conn,scope,operation) if operation else None
        payload={'scope_id':scope,'session_id':session_id,'operation_id':operation}
        if (receipt is not None and receipt['harness_id']=='codex' and receipt['tool']==tool
                and receipt['status']=='accepted' and receipt['session_id']==session_id
                and receipt['request_sha256']==runtime_operations.compute_request_sha256(payload)):
            return True
    return False


def _reconcile_codex(ctx,task,run_id,opened):
    from .codex_execution import require_settled
    try:
        client,job=_client(ctx.conn,task,run_id,read_only=True)
        consumed={q['request_id']:_codex_consumed(ctx.conn,client,q) for q in opened}
        settled=False
        try:
            require_settled(job); settled=True
        except util.KanpilotError: pass
        with db.write_txn(ctx.conn):
            fresh=db.require_task(ctx.conn,task['task_id']); current,_=_job(ctx.conn,fresh,run_id)
            if current!=job: return _result(ctx,task['task_id'],status='unknown')
            qs=copy.deepcopy(records(fresh))
            stop=db.task_extra(fresh).get('execution_stop') or {}
            stopped=settled and stop.get('run_id')==run_id and _codex_stop_accepted(ctx.conn,client.scope_id,stop,job['session_id'])
            for expected in opened:
                q=next((q for q in qs if _match(q,expected) and q['questions_sha256']==expected['questions_sha256'] and q['state']=='open'),None)
                if not q: continue
                if consumed[q['request_id']]:
                    q['delivery']='accepted'
                    for operation in q['operations']:
                        if operation['operation_id']==q['submitted']['operation_id']: operation['delivery']='accepted'
                    _close(ctx.conn,fresh,qs,q,util.now_iso(),'answered')
                elif settled:
                    _close(ctx.conn,fresh,qs,q,util.now_iso(),'stopped' if stopped else 'expired',stop.get('reason') or job.get('detail'))
                fresh=db.require_task(ctx.conn,task['task_id'])
            if stopped:
                stop=dict(stop,status='stopped',confirmed_at=util.now_iso())
                db.set_extra(ctx.conn,fresh,util.now_iso(),execution_stop=stop)
                fresh=db.require_task(ctx.conn,task['task_id'])
                if fresh['status']=='running' or fresh['status']=='needs_user' and fresh['failure_code']=='execution_input_required':
                    core.block_task(ctx.conn,fresh,util.now_iso(),stop.get('reason') or 'user_stop','execution_failed','execution-input',stage='monitor')
        return _result(ctx,task['task_id'],status='stopped' if stopped else 'observed')
    except util.KanpilotError:
        return _result(ctx,task['task_id'],status='unknown')


def _stop_codex(ctx,payload):
    abort=payload.get('abort') is True
    op=_identity(payload['operation_id']); reason=payload.get('reason') or 'user_stop'
    if reason not in ('user_stop','execution_question_expired'): raise util.bad_request('停止原因无效')
    with db.write_txn(ctx.conn):
        task=db.require_task(ctx.conn,payload['task_id']); job,_=_job(ctx.conn,task,payload['run_id'])
        if task['status'] not in ('running','needs_user'):
            raise util.KanpilotError('conflict','当前没有活动 Codex 执行轮次')
        client,_=_client(ctx.conn,task,payload['run_id'],read_only=False,connect=False)
        stop=copy.deepcopy(db.task_extra(task).get('execution_stop') or {})
        if stop and stop.get('run_id')!=payload['run_id']: stop={}
        if not stop and job.get('status') not in ('running','waiting_input'):
            raise util.KanpilotError('conflict','Codex 已不在活动执行状态，请读取最终结果')
        if abort:
            stamp=util.parse_ts(stop.get('requested_at'))
            receipt=runtime_operations.get_operation(ctx.conn,client.scope_id,stop.get('operation_id')) if stop else None
            expected={'scope_id':client.scope_id,'session_id':job['session_id'],'operation_id':stop.get('operation_id')}
            if (stamp is None or (util.now_dt()-stamp).total_seconds()<10 or receipt is None
                    or receipt['harness_id']!='codex' or receipt['tool']!='kp_runtime_stop'
                    or receipt['request_sha256']!=runtime_operations.compute_request_sha256(expected)
                    or op==stop.get('operation_id')):
                raise util.KanpilotError('conflict','停止身份或十秒宽限尚未满足，不允许 abort')
            if stop.get('abort_operation_id') not in (None,op): raise util.KanpilotError('conflict','abort 结果未知，禁止重发')
            stop['abort_operation_id']=op
        else:
            if stop and (stop.get('operation_id')!=op or stop.get('reason')!=reason):
                raise util.KanpilotError('conflict','停止结果尚未确定，不重发')
            if not stop: stop={'run_id':payload['run_id'],'session_id':job['session_id'],'turn_id':job['turn_id'],'operation_id':op,'requested_at':ctx.now_iso,'reason':reason,'status':'unknown'}
        qs=copy.deepcopy(records(task))
        for q in qs:
            if q['run_id']==payload['run_id'] and q['state']=='open':
                q.update(stop_operation_id=stop['operation_id'],stop_requested_at=stop['requested_at'],termination_reason=reason)
                if abort: q['abort_operation_id']=op
        db.set_extra(ctx.conn,task,ctx.now_iso,execution_questions=qs,execution_stop=stop)
    try:
        client,_=_client(ctx.conn,task,payload['run_id'],read_only=False)
        client._write('kp_runtime_abort' if abort else 'kp_runtime_stop',op,{'session_id':job['session_id']})
    except util.KanpilotError: pass
    result=cmd_reconcile_execution_question(ctx,{'task_id':task['task_id'],'run_id':payload['run_id']})
    if result.get('status')!='stopped': result['status']='unknown'
    return result


def cmd_stop_execution(ctx,payload):
    row=round_store.require_round(ctx.conn,payload['task_id'],payload['run_id'])
    if row['backend']=='codex': return _stop_codex(ctx,payload)
    abort=payload.get("abort") is True
    reason=payload.get("reason") or "user_stop"
    if reason not in ("user_stop","execution_question_expired"):
        raise util.bad_request("停止原因无效")
    with db.write_txn(ctx.conn):
        task=db.require_task(ctx.conn,payload["task_id"]); job,_=_job(ctx.conn,task,payload["run_id"])
        if task["status"] not in ("running","needs_user") or not open_records(task):
            raise util.KanpilotError("conflict","当前没有等待回答的活动执行轮次")
        _client(ctx.conn,task,payload["run_id"],read_only=False,connect=False)
        op=_identity(payload["operation_id"]); qs=copy.deepcopy(records(task))
        active=[q for q in qs if q["run_id"]==payload["run_id"] and q["state"]=="open"]
        if abort:
            scope=runtime_client.stable_scope_id("execution",task_id=task["task_id"],run_id=task["run_id"])
            for q in active:
                stamp=util.parse_ts(q.get("stop_requested_at"))
                receipt=runtime_operations.get_operation(ctx.conn,scope,q.get("stop_operation_id")) if q.get("stop_operation_id") else None
                if (stamp is None or (util.now_dt()-stamp).total_seconds()<10 or receipt is None
                        or receipt["tool"]!="kp_runtime_stop" or receipt["session_id"] not in (None,job["session_id"])
                        or receipt["request_sha256"] != runtime_operations.compute_request_sha256({"scope_id":scope,"session_id":job["session_id"],"operation_id":q["stop_operation_id"]})
                        or op==q.get("stop_operation_id")):
                    raise util.KanpilotError("conflict","停止身份或十秒宽限尚未满足，不允许 abort")
                if q.get("abort_operation_id") and q["abort_operation_id"]!=op: raise util.KanpilotError("conflict","abort 结果未知，禁止重发")
                q["abort_operation_id"]=op
        else:
            for q in active:
                if q.get("stop_operation_id") and (q["stop_operation_id"]!=op or q.get("termination_reason")!=reason): raise util.KanpilotError("conflict","停止结果尚未确定，不重发")
                q.setdefault("stop_requested_at",ctx.now_iso)
                q.setdefault("stop_operation_id",op)
                q.setdefault("termination_reason",reason)
        db.set_extra(ctx.conn,task,ctx.now_iso,execution_questions=qs)
    status="unknown"
    try:
        client,_=_client(ctx.conn,task,payload["run_id"],read_only=False)
        client._write("kp_runtime_abort" if abort else "kp_runtime_stop",op,{"session_id":job["session_id"]}); status="accepted"
    except util.KanpilotError: pass
    cmd_reconcile_execution_question(ctx,payload)
    return _result(ctx,task["task_id"],status=status,observer=ensure_observer(ctx,task["task_id"],payload["run_id"]))
