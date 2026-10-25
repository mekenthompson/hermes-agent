"""Private registered-worker routing; existing native ownership stays in the gateway."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os

_STORE = None
_BOUND = ContextVar('managed_background_descriptor', default=None)


def bind_worker_process_store(store, frame):
    global _STORE
    from agent.runtime_session_store import WorkerPersistenceError, is_worker_process
    if not is_worker_process() or store.scope['pid'] != os.getpid() or (_STORE is not None and _STORE[0] is not store):
        raise WorkerPersistenceError('managed_process_scope_mismatch')
    _STORE = store, frame


def digest(tool, arguments, observed_cwd=None):
    request = [tool, arguments] if observed_cwd is None else [tool, arguments, observed_cwd]
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@contextmanager
def background_descriptor(session):
    token = _BOUND.set(session)
    try:
        yield
    finally:
        _BOUND.reset(token)


def reserved_background():
    return _BOUND.get()


def worker_call(tool, arguments, *, observed_cwd=None):
    from agent.runtime_session_store import WorkerPersistenceError, is_worker_process
    from tools.approval_context import current_approval_task_context, require_live_task_setup
    if not is_worker_process():
        return None
    require_live_task_setup()
    origin = current_approval_task_context()
    if _STORE is None or origin is None:
        raise WorkerPersistenceError('Managed background process requires its original native worker authority.')
    from tools.managed_process_child import worker_process_binding
    child = worker_process_binding()
    if bool(origin.ancestry) != (child is not None):
        raise WorkerPersistenceError('Managed background process requires its original native child authority.')
    store, frame = child or _STORE
    wait = arguments.get('timeout')
    timeout = max(60, int(json.loads(frame['policy']['config_json']).get('approvals', {}).get('timeout', 300)) + 60,
                  wait + 60 if type(wait) is int else 60)
    with store.lock:
        sequence = store.journal['next_sequence']
        saved = store._apply('process.reserve', {'tool': tool, 'digest': digest(tool, arguments, observed_cwd)})
        scope = dict(store.scope)
    from websockets.exceptions import ConnectionClosed
    try:
        result = store.rpc('worker.persist', **scope, sequence=sequence, operation='process.call',
                           payload={'tool': tool, 'arguments': arguments, 'resource_id': saved['resource_id'],
                                    'observed_cwd': observed_cwd}, _timeout=timeout)
    except (TimeoutError, OSError, ConnectionClosed) as exc:
        with store.lock:
            store.failure = f"unknown_execution: inspect {saved['resource_id']} before retrying the process request."
        raise WorkerPersistenceError(store.failure) from exc
    return json.dumps(result, ensure_ascii=False)


def terminal_proxy(arguments, task_id):
    from agent.runtime_session_store import is_worker_process
    if not is_worker_process():
        return None
    from tools.terminal_tool import FOREGROUND_MAX_TIMEOUT, trailing_amp_background_command
    command, timeout = arguments.get('command'), arguments.get('timeout')
    background = (arguments.get('background') is True
        or (type(timeout) is int and timeout > FOREGROUND_MAX_TIMEOUT)
        or (isinstance(command, str) and trailing_amp_background_command(command) is not None))
    if not background:
        return None
    from tools.terminal_tool import resolve_recorded_session_cwd
    return worker_call('terminal', arguments, observed_cwd=resolve_recorded_session_cwd(task_id))
