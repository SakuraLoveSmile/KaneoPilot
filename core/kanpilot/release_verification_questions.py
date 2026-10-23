"""Input relay owned by an independent release verification, never its old run.

Questions and answers live beside the attempt's receipts, outside frozen inputs.
Unknown sends are only observed; polling never sends or invents an answer.
"""
from copy import deepcopy
from pathlib import Path
import os
import re

from . import (constants, core, db, execution_questions as inputs, pi_desktop_worker as worker,
               release_verification as verification, round_store, runtime_operations, util)
from .runtime_client import stable_scope_id


def _require(ok, message):
    if not ok:
        raise util.KanpilotError('release_verification_question', message)


def _current(conn, task):
    ref = db.task_extra(task).get('release_verification_ref') or {}
    if not ref.get('verification_id'):
        return None
    try:
        value = verification._load(conn, task['task_id'], ref['verification_id'])
    except util.KanpilotError:
        return None
    if ref.get('context_sha256') != value['context']['context_sha256']:
        return None
    return value


def _owned(conn, value):
    scope = stable_scope_id('execution', invocation_id='releaseverify_' + value['verification_id'])
    return runtime_operations.session_owned(conn, scope, value.get('session_id'))


def view(conn, task, now=None):
    value = _current(conn, task)
    if value is None or not value.get('input_questions'):
        return None
    records = value['input_questions']
    opened = [q for q in records if q['state'] == 'open']
    q = deepcopy(opened[0] if opened else records[-1])
    q['queued'] = max(0, len(opened) - 1)
    end = util.parse_ts(q.get('closed_at')) or (util.parse_ts(now) if isinstance(now,str) else now) or util.now_dt()
    q['wait_seconds'] = max(0, (end - util.parse_ts(q['opened_at'])).total_seconds())
    q['answerable'] = bool(q['answerable'] and _owned(conn, value) and db.task_is_active(task))
    q.pop('operations', None)
    return q


def open_question(conn, task, now=None):
    q = view(conn, task, now)
    return q if q and q['state'] == 'open' else None


def guard_active(conn, task):
    _require(open_question(conn, task) is None, '补验证正在等待回答，暂不能更改任务或移出看板')


def attention_fields(conn, task, now=None):
    q = open_question(conn, task, now)
    if q is None:
        return {}
    return dict(release_verification_question=q, question_preview=inputs.preview(q),
        failure_code='release_verification_question', failure_stage='delivery',
        failure_reason='补验证正在等待回答', blocked_summary=q['questions'][0]['title'] if q['questions'] else q.get('detail'),
        blocked_at=q['opened_at'])


def _resolutions(client, session_id):
    collector = worker._Collector(client, session_id, None, '')
    collector.poll()
    if collector.truncated or collector.read_error is not None or collector.unrecognized:
        return {}
    return collector.input_resolutions


def _close(q, now, kind, *, by=None):
    q.update(state='answered' if kind == 'answered' else 'expired', closed_at=now,
        observer_alive=False, answered_by=by if kind == 'answered' else None)


def _accepted_submission(conn,value,q):
    submitted = q.get('submitted') or {}
    operation = submitted.get('operation_id')
    if not operation:
        return False
    _,wire = inputs.normalize_answers(q,submitted['answers'])
    scope = stable_scope_id('execution',invocation_id='releaseverify_' + value['verification_id'])
    arguments = dict(scope_id=scope,operation_id=operation,session_id=q['session_id'],
        request_id=q['request_id'],expected_tool_call_id=q['tool_call_id'],answers=wire)
    receipt = runtime_operations.get_operation(conn,scope,operation)
    result = (util.jload(receipt['result_json']) or {}) if receipt is not None else {}
    return bool(receipt is not None and receipt['tool'] == 'kp_runtime_input_respond'
        and receipt['harness_id'] == 'pi_desktop' and receipt['status'] == 'accepted'
        and receipt['request_sha256'] == runtime_operations.compute_request_sha256(arguments)
        and result.get('session_id') == q['session_id'] and result.get('request_id') == q['request_id']
        and result.get('status') == 'answered')


def observe(conn, value, client, status, now):
    """Read current asks into this in-memory attempt; caller performs its CAS."""
    inner = status.get('status') or status
    existing = value.get('input_questions', [])
    opened = [q for q in existing if q['state'] == 'open']
    summary = inner.get('pendingInputs') or {}
    if not opened and not summary.get('count'):
        if value.get('reason') == '补验证正在等待回答':
            value.pop('reason',None)
        return False
    value['input_questions'] = existing
    sid = value['session_id']
    pending = client.input_pending(sid)
    _require(pending.get('sessionId') == sid and isinstance(pending.get('asks'), list),
        '补验证问题会话身份或格式不符')
    asks = pending['asks']
    _require(len(asks) <= inputs.MAX_QUESTIONS, '补验证同时等待的问题过多，请在 Plus 处理')
    turn = inner.get('currentTurnId')
    phase = 'execution' if value.get('approval_sent') or value.get('execution_turn_id') or value.get('approval_observation') else 'planning'
    history_only = phase == 'execution' and not value.get('approval_sent')
    expected = value.get('execution_turn_id') if phase == 'execution' else value['planning_turn_id']
    if asks:
        if phase == 'execution':
            if history_only:
                _require(value.get('approval_observation') and expected == turn,
                    '只读历史提问缺少原独立执行归属')
            else:
                verification._approved(conn, client, value)
            _require(turn and turn != value['planning_turn_id'] and (not expected or expected == turn),
                '补验证问题不属于原独立执行轮次')
            value['execution_turn_id'] = turn
        else:
            _require(turn == expected, '补验证问题不属于原规划轮次')
        record = client.turn_get(sid, turn, require_identity=True)
        _require(inner.get('sessionId', sid) == sid and inner.get('isRunning') is True
            and worker._turn_identity_matches(record, sid, turn) and worker._turn_status(record) == 'running',
            '补验证提问轮次已变化或不再运行')
    seen = set()
    for ask in asks:
        request = inputs._identity(ask.get('requestId'))
        tool = inputs._identity(ask.get('toolCallId'))
        _require(request not in seen and ask.get('turnId') == turn,
            '补验证问题重复或轮次不符')
        seen.add(request)
        normalized = inputs.normalize_questions(ask.get('questions'))
        fingerprint = util.sha256_text(inputs.canonical(normalized))
        old = next((q for q in existing if q['request_id'] == request), None)
        if old is not None:
            _require(old['turn_id'] == turn and old['tool_call_id'] == tool
                and old['questions_sha256'] == fingerprint and old['state'] == 'open',
                '原补验证问题身份或内容已变化，请重新核对')
            old['observer_alive'] = True
            old['answerable'] = _owned(conn, value) and not history_only
            continue
        q = dict(verification_id=value['verification_id'],context_sha256=value['context']['context_sha256'],
            run_id=value['context']['identity']['run_id'],phase=phase,source='pi_desktop',
            session_id=sid,turn_id=turn,request_id=request,tool_call_id=tool,
            questions_sha256=fingerprint,questions=normalized,
            draft=[dict(question_id=q['id'],option_ids=[],text='',skipped=False) for q in normalized],
            submitted=None,operations=[],state='open',delivery='none',answerable=_owned(conn,value) and not history_only,
            answered_by=None,opened_at=now,closed_at=None,observer_alive=True,detail=None)
        if not q['answerable']:
            q['detail'] = ('原 Goal 仅可只读观察，请在 Plus 回答' if history_only else
                '缺少本项目原会话创建归属回执，请在 Plus 回答')
        existing.append(q)
    missing = [q for q in opened if q['request_id'] not in seen]
    resolutions = _resolutions(client, sid) if missing else {}
    for q in missing:
        q['observer_alive'] = bool(inner.get('isRunning') is True and inner.get('currentTurnId') == q['turn_id'])
        kind = resolutions.get((q['turn_id'], q['tool_call_id']))
        if _accepted_submission(conn,value,q):
            q['delivery'] = 'accepted'
            operation = next((o for o in q['operations'] if o['operation_id'] == q['submitted']['operation_id']),None)
            if operation: operation['delivery'] = 'accepted'
            _close(q,now,'answered',by='kaneopilot')
        elif kind in ('answered','stopped','aborted'):
            _close(q,now,kind,by=None if q.get('submitted') else 'plus')
    for q in opened:
        if q['state'] == 'open' and q['delivery'] == 'submitting':
            operation = next((o for o in q['operations'] if o['operation_id'] == q['submitted']['operation_id']),None)
            if operation and not util.pid_alive(operation.get('pid')):
                q['delivery'] = operation['delivery'] = 'unknown'
    waiting = any(q['state'] == 'open' for q in existing)
    if waiting:
        value.update(state='running' if phase == 'execution' else 'planning',reason='补验证正在等待回答')
        value.pop('failure_code',None)
    elif value.get('reason') == '补验证正在等待回答':
        value.pop('reason',None)
    return waiting


def _target(conn, payload, *, allow_closed=False, full=False):
    task = db.require_task(conn,payload['task_id'])
    value = _current(conn,task)
    _require(value is not None and value['verification_id'] == payload['verification_id']
        and value['context']['context_sha256'] == payload['expected_context_sha256'],
        '补验证尝试已变化，原草稿保留')
    row = db.get_runtime_operation(conn,verification._scope(task['task_id']),verification._op(value['verification_id']))
    context = value['context']
    _require(row['harness_id'] == 'core' and row['status'] in ('unknown','rejected')
        and row['request_sha256'] == verification._hash(context)
        and context['context_sha256'][:32] == value['verification_id']
        and verification._hash({k:v for k,v in context.items() if k != 'context_sha256'}) == context['context_sha256']
        and db.task_is_active(task) and task['status'] in ('in_review','done')
        and not db.paused(conn,'delivery') and not db.task_auto_paused(task)
        and not core.automation_of(task).get('manual_takeover')
        and verification.release._identity(task) == context['identity']
        and value['state'] not in ('verified','failed') and _owned(conn,value),
        '补验证输入授权或任务身份已变化')
    original = round_store.require_round(conn,task['task_id'],task['run_id'])
    _require(original['backend'] == 'pi_desktop' and original['save_state'] == 'active'
        and original['run_status'] == 'finished' and util.jload(original['config_json']) == context['runtime_config'],
        '补验证原执行轮次或配置已变化')
    policy,mapping = verification.release._policy(conn,task)
    _require(policy['enabled'] and mapping['repository'] == task['repository']
        and not task['review_source_task_id']
        and not conn.execute('SELECT 1 FROM tasks WHERE review_source_task_id=? LIMIT 1',(task['task_id'],)).fetchone()
        and not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') AND kind!='delivery' LIMIT 1",
            (task['task_id'],)).fetchone(), '补验证发布策略或任务处理归属已变化')
    verification._restart_lineage(conn,context)
    q = next((q for q in value.get('input_questions',[]) if q['request_id'] == payload['request_id']),None)
    _require(q is not None and (allow_closed or q['state'] == 'open') and q['answerable']
        and q['questions_sha256'] == payload['questions_sha256'],
        '补验证问题已关闭、不可回答或内容指纹不符')
    _require(q['verification_id'] == value['verification_id'] and q['context_sha256'] == context['context_sha256']
        and q['run_id'] == context['identity']['run_id'] and q['session_id'] == value.get('session_id')
        and q['phase'] in ('planning','execution')
        and q['turn_id'] == value.get('planning_turn_id' if q['phase'] == 'planning' else 'execution_turn_id')
        and q['questions_sha256'] == util.sha256_text(inputs.canonical(q['questions'])),
        '补验证问题与原尝试的可信身份不符')
    if full:
        verification._unchanged(conn,task,value); verification._source(value)
    return task,value,q


def _live(value, q, *, read_only=True):
    client = verification._client(value,read_only=read_only,require_input=True)
    status = client.agent_status(q['session_id']); inner = status.get('status') or status
    pending = client.input_pending(q['session_id'])
    _require(pending.get('sessionId') == q['session_id'] and isinstance(pending.get('asks'),list),
        '补验证问题会话不符')
    matches = [a for a in pending['asks'] if a.get('requestId') == q['request_id']]
    _require(len(matches) == 1, '原补验证问题已不再等待回答，请先核对')
    ask = matches[0]
    _require(inner.get('sessionId',q['session_id']) == q['session_id'] and inner.get('isRunning') is True
        and inner.get('currentTurnId') == q['turn_id'] and ask.get('turnId') == q['turn_id']
        and ask.get('toolCallId') == q['tool_call_id']
        and util.sha256_text(inputs.canonical(inputs.normalize_questions(ask.get('questions')))) == q['questions_sha256'],
        '原补验证问题的轮次、工具或内容已变化')
    turn = client.turn_get(q['session_id'],q['turn_id'],require_identity=True)
    _require(worker._turn_identity_matches(turn,q['session_id'],q['turn_id'])
        and worker._turn_status(turn) == 'running', '补验证提问轮次已结束')
    return client


def _result(conn, task_id, **extra):
    task = db.require_task(conn,task_id); value = _current(conn,task)
    _require(value is not None,'当前补验证引用已变化，请重新载入任务')
    return dict(task_id=task_id,run_id=task['run_id'],verification_id=value['verification_id'],
        context_sha256=value['context']['context_sha256'],release_verification_question=view(conn,task),**extra)


def cmd_save_release_verification_answers(ctx,payload):
    task,value,q = _target(ctx.conn,payload,full=True)
    expected = util.jdump(value)
    answers,_ = inputs.normalize_answers(q,payload['answers'],draft=True)
    _live(value,q)
    with db.write_txn(ctx.conn):
        _,fresh,current = _target(ctx.conn,payload)
        _require(util.jdump(fresh) == expected,'补验证问题已更新，请重新载入后保存')
        _require(current['delivery'] not in ('submitting','unknown')
            or current['submitted']['answers'] == answers,'送达结果未知，冻结答案不可修改')
        current['draft'] = answers
        verification._save(ctx.conn,fresh,ctx.now_iso)
    return _result(ctx.conn,task['task_id'])


def cmd_submit_release_verification_answers(ctx,payload):
    operation = inputs._identity(payload['operation_id'])
    task,value,q = _target(ctx.conn,payload,allow_closed=True,full=True)
    if payload.get('skip_all') is True:
        _require('answers' not in payload,'skip_all 与 answers 互斥')
        raw = [dict(question_id=q['id'],option_ids=[],text='',skipped=True) for q in q['questions']]
    else:
        raw = payload.get('answers')
    answers,wire = inputs.normalize_answers(q,raw)
    old = next((o for o in q['operations'] if o['operation_id'] == operation),None)
    if old:
        _require(old['answers'] == answers,'operation_id 已用于不同答案')
        return _result(ctx.conn,task['task_id'],status=old['delivery'],deduplicated=True)
    expected = util.jdump(value)
    _live(value,q)
    with db.write_txn(ctx.conn):
        _,fresh,current = _target(ctx.conn,payload)
        _require(util.jdump(fresh) == expected,'补验证问题已更新，请重新载入后提交')
        _require(current['delivery'] != 'submitting','补验证答案正在提交')
        _require(current['delivery'] != 'unknown' or current['submitted']['answers'] == answers,
            '送达结果未知，只能显式重试同一冻结答案')
        current.update(draft=answers,delivery='submitting',submitted=dict(answers=answers,operation_id=operation,
            at=ctx.now_iso,by='kaneopilot'))
        current['operations'].append(dict(operation_id=operation,answers=answers,delivery='submitting',pid=os.getpid(),at=ctx.now_iso))
        verification._save(ctx.conn,fresh,ctx.now_iso)
        value,q = fresh,current
    delivery = 'unknown'
    try:
        client = _live(value,q,read_only=False)
        result = client.input_respond(q['session_id'],q['request_id'],q['tool_call_id'],wire,operation_id=operation)
        if (result.get('session_id') == q['session_id'] and result.get('request_id') == q['request_id']
                and result.get('status') == 'answered'):
            delivery = 'accepted'
        # not_pending proves this call did not answer; it cannot settle an
        # earlier unknown send or explain who consumed the original question.
    except util.KanpilotError:
        pass  # The call may have reached Plus. A pending snapshot proves nothing.
    with db.write_txn(ctx.conn):
        fresh = verification._load(ctx.conn,task['task_id'],value['verification_id'])
        current = next((item for item in fresh.get('input_questions',[]) if item['request_id'] == q['request_id']
            and item['tool_call_id'] == q['tool_call_id'] and item['questions_sha256'] == q['questions_sha256']),None)
        if current:
            receipt = next((o for o in current['operations'] if o['operation_id'] == operation),None)
            if receipt: receipt['delivery'] = delivery
            if (current.get('submitted') or {}).get('operation_id') == operation:
                current['delivery'] = delivery
                if delivery == 'accepted' and current['state'] == 'open':
                    _close(current,util.now_iso(),'answered',by='kaneopilot')
            verification._save(ctx.conn,fresh,util.now_iso())
    return _result(ctx.conn,task['task_id'],status=delivery)


def cmd_reconcile_release_verification_answers(ctx,payload):
    task,value,q = _target(ctx.conn,payload,allow_closed=True,full=True)
    if q['state'] == 'open':
        expected = util.jdump(value)
        snapshot = verification._history_snapshot(ctx.conn,value,include_operations=False)
        client = verification._client(value,read_only=True,require_input=True)
        observe(ctx.conn,value,client,client.agent_status(value['session_id']),ctx.now_iso)
        verification._save_sync(ctx,value,expected,snapshot)
    return _result(ctx.conn,task['task_id'])


def trusted_runtime_input(conn,scope,args,*,sql_only=False):
    """Only this attempt's frozen submitted answer authorizes its runtime write."""
    invocation = scope.invocation_id or ''
    _require(re.fullmatch(r'releaseverify_[0-9a-f]{32}',invocation) is not None,
        '缺少可信的独立补验证输入作用域')
    vid = invocation[len('releaseverify_'):]
    rows = list(conn.execute('SELECT result_json FROM runtime_operations WHERE operation_id=? AND tool=?',
        (verification._op(vid),verification.TOOL)))
    _require(len(rows) == 1,'独立补验证输入回执不唯一')
    value = util.jload(rows[0]['result_json'])
    q = next((q for q in value.get('input_questions',[]) if q['request_id'] == args['request_id']),None)
    _require(q is not None,'补验证没有此问题')
    task,value,q = _target(conn,dict(task_id=value['context']['identity']['task_id'],verification_id=vid,
        expected_context_sha256=value['context']['context_sha256'],request_id=q['request_id'],
        questions_sha256=q['questions_sha256']),full=not sql_only)
    config = value['context']['runtime_config']
    _require(scope.role == 'execution' and not any((scope.task_id,scope.run_id,scope.request_id))
        and scope.scope_id == stable_scope_id('execution',invocation_id=invocation)
        and args['scope_id'] == scope.scope_id and args['session_id'] == q['session_id']
        and args['expected_tool_call_id'] == q['tool_call_id']
        and scope.runtime_config == {k:config[k] for k in constants.EXEC_PI_DESKTOP_KEYS if k in config}
        and str(scope.project_path) == value['worktree_path']
        and bool(scope.connection_file) and Path(scope.connection_file).expanduser().resolve() == Path(config['connection_file']).expanduser().resolve(),
        '独立补验证输入作用域或目标身份不符')
    submitted = q.get('submitted') or {}
    _,wire = inputs.normalize_answers(q,submitted.get('answers'))
    _require(q['delivery'] == 'submitting' and submitted.get('operation_id') == args['operation_id']
        and wire == args['answers'],'补验证 runtime 答案与用户冻结提交不符')
