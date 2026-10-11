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
    if manual_plan(request):
        return True  # Explicit user authorization; never autonomous intake.
    return effective_mode(conn, task['project_id']) == 'live'
