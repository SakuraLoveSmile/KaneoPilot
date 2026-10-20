"""Keep engine-owned workers within live projects at every claim boundary."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from . import db, process_owner, project_config, util

WORKER_ENV = 'KANPILOT_ENGINE_WORKER'
MODE_ENV = 'KANPILOT_ENGINE_MODE'
FORCE_ENV = 'KANPILOT_ENGINE_FORCE'
_COMMAND_MODE = ContextVar('kanpilot_engine_command_mode', default=None)


@contextmanager
def command_mode(mode, force=False):
    """Scoped in-process core calls; does not mutate shared worker environment."""
    token = _COMMAND_MODE.set((mode, force))
    try:
        yield
    finally:
        _COMMAND_MODE.reset(token)


def manual_plan(request):
    return bool(request['stage'] == 'plan' and request['requested_by'] == 'user'
                and db.request_payload(request).get('manual_authorized') is True)


def manual_review_repair(conn, request, task=None):
    """Only the explicitly authorized current repair may cross legacy scope."""
    from . import core
    task = task if task is not None else db.get_task(conn, request['task_id'])
    if task is None or not db.task_schedulable(task):
        return False
    if request['stage'] == 'delivery':
        return action_delivery_allowed(conn, task, request)
    fix = core.review_repair_of(task)
    if (fix.get('authorized_by') != 'user' or not fix.get('authorized_at')
            or not fix.get('queued_at') or request['created_at'] < fix['queued_at']
            or fix.get('task_id') != task['task_id'] or fix.get('project_id') != task['project_id']
            or fix.get('repository') != task['repository']
            or fix.get('requirements_sha256') != task['requirements_sha256']
            or fix.get('plan_sha256') != task['plan_sha256']):
        return False
    delivery = core.delivery_of(task)
    if any(fix.get(k) != delivery.get(k) for k in ('branch', 'worktree_path')):
        return False
    if request['stage'] == 'dispatch':
        return (fix.get('state') == 'queued' and task['run_id'] == fix.get('source_execution_run_id')
                and db.request_payload(request).get('review_repair_key') == fix.get('review_key'))
    return (request['stage'] in ('monitor', 'review')
            and fix.get('state') == ('running' if request['stage'] == 'monitor' else 'awaiting_review')
            and task['run_id'] == fix.get('repair_execution_run_id'))


def action_delivery_allowed(conn, task, request=None, *, allow_paused=False):
    """A completed user action authorizes only its exact reviewed PR result."""
    from . import task_ai_action
    receipt = task_ai_action.delivery_continuation(task, allow_paused=allow_paused)
    if receipt is None:
        return False
    if not allow_paused and db.paused(conn, 'delivery'):
        return False
    fix = receipt['action']
    _, _, items, _ = project_config.load_raw_mappings(conn)
    mapping = items.get(task['project_id']) or {}
    if not mapping.get('enabled', True) or mapping.get('repository') != task['repository']:
        return False
    dispatch = db.get_request(conn, fix.get('request_id'))
    if (dispatch is None or dispatch['task_id'] != task['task_id'] or dispatch['kind'] != 'dispatch'
            or dispatch['run_id'] != task['run_id'] or dispatch['requested_by'] != 'user'
            or dispatch['status'] != 'succeeded'
            or (util.jload(dispatch['result_json']) or {}).get('outcome') != 'succeeded'
            or db.request_payload(dispatch).get('review_repair_key') != fix.get('review_key')):
        return False
    execution = conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
                             (task['task_id'], task['run_id'])).fetchone()
    if (execution is None or execution['backend'] != 'pi_desktop'
            or util.jload(execution['config_json']) != fix['action_config']
            or execution['worktree_path'] != fix['worktree_path']):
        return False
    identity = receipt['review_identity']
    review = db.get_request(conn, identity['request_id'])
    if (review is None or review['task_id'] != task['task_id'] or review['kind'] != 'review'
            or review['run_id'] != identity['run_id'] or review['status'] != 'succeeded'
            or (util.jload(review['result_json']) or {}).get('outcome') != 'succeeded'):
        return False
    if request is not None:
        payload = db.request_payload(request)
        if (request['task_id'] != task['task_id'] or request['kind'] != 'delivery'
                or request['stage'] != 'delivery' or request['created_at'] < receipt['registered_at']
                or payload.get('task_ai_action_id') != fix['action_id']
                or payload.get('expected_execution_run_id') != task['run_id']
                or payload.get('expected_head_sha') != receipt['head_sha']
                or payload.get('expected_plan_sha256') != task['plan_sha256']):
            return False
    return True


def effective_mode(conn, project_id):
    """Project setting wins, except an explicit engine process force override."""
    owned = os.environ.get(WORKER_ENV) == '1'
    context = _COMMAND_MODE.get()
    default = context[0] if context is not None else (os.environ.get(MODE_ENV, 'legacy') if owned else 'legacy')
    if default not in ('legacy', 'shadow', 'live'):
        return None
    _, _, items, _ = project_config.load_raw_mappings(conn)
    mapping = items.get(project_id) or {}
    mode = mapping.get('engine_mode') or (mapping.get('automation') or {}).get('engine_mode') or default
    if (context is not None and context[1]) or (context is None and owned and os.environ.get(FORCE_ENV) == '1'):
        mode = default
    return mode if mode in ('legacy', 'shadow', 'live') else None


def allows(conn, request, now):
    if os.environ.get(WORKER_ENV) != '1':
        return True  # Existing Desktop/CLI/manual runner contracts are unchanged.
    from .engine import health
    instance = os.environ.get(process_owner.INSTANCE_ENV)
    entry = (db.get_setting(conn, process_owner.SETTING, {}) or {}).get(instance)
    if not health._entry_active(entry, now):
        return False  # A stopped/replaced engine may not claim a new round.
    task = db.get_task(conn, request['task_id']) if request['task_id'] else None
    if task is None:
        return False
    if manual_plan(request) or manual_review_repair(conn, request, task):
        return True  # Explicit user authorization; never autonomous intake.
    return effective_mode(conn, task['project_id']) == 'live'
