"""Native delegated-child scopes for the existing managed process transport."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
import json
import re
import threading

_BOUND = ContextVar('managed_process_native_child', default=None)


def worker_process_binding():
    return _BOUND.get()


def native_child_sessions(authority, root_session_ids):
    """Physical owner-minted descendants remain visible after child/worker close.

    This is ownership projection for existing resources, never execution
    authority: terminal children cannot use these rows to resume their work.
    """
    with authority.db._read_ctx() as conn:
        rows = conn.execute('''WITH RECURSIVE owned(session_id,execution_id) AS (
            SELECT session_id,execution_id FROM worker_executions
                WHERE session_id IN (SELECT value FROM json_each(?))
            UNION
            SELECT child.session_id,child.execution_id FROM owned parent
                JOIN worker_executions child ON child.kind='child'
                AND substr(child.execution_id,1,length(parent.execution_id)+7)=parent.execution_id||'/child:'
                AND instr(substr(child.execution_id,length(parent.execution_id)+8),'/')=0
                JOIN sessions s ON s.id=child.session_id AND s.parent_session_id=parent.session_id
        ) SELECT DISTINCT session_id FROM owned''', (json.dumps(sorted(root_session_ids)),)).fetchall()
    return {row[0] for row in rows}


@contextmanager
def managed_child_scope(child, task_id, writer_lease_id=None):
    """Bind only the real owner's reserved child store, inside its native task scope."""
    from agent.runtime_session_child import ChildSessionStore
    from agent.runtime_session_store import is_worker_process
    from tools.approval_context import current_approval_task_context, require_live_task_setup
    if not is_worker_process():
        yield
        return
    store = getattr(child, '_session_db', None)
    origin = current_approval_task_context()
    require_live_task_setup()
    if not isinstance(store, ChildSessionStore) or origin is None or origin.ancestry[-1:] != (task_id,):
        from agent.runtime_session_store import WorkerPersistenceError
        raise WorkerPersistenceError('managed_child_authority_missing')
    from tools.terminal_tool import resolve_recorded_session_cwd
    from hermes_constants import get_hermes_home
    from agent.runtime_cwd import scope_terminal_cwd
    from agent.tool_executor import _tool_search_scoped_names
    facts = store._process_scope_facts
    if facts is None:
        facts = {'task_id': task_id,
            'cwd': resolve_recorded_session_cwd(task_id) or scope_terminal_cwd(),
            'allowed_tools': sorted((set(getattr(child, 'valid_tool_names', ()))
                                    | set(_tool_search_scoped_names(child))) & {'terminal', 'process_manage'}),
            'profile_id': str(get_hermes_home()), 'writer_lease_id': writer_lease_id}
    elif (facts['task_id'] != task_id or facts['profile_id'] != str(get_hermes_home())
            or facts['writer_lease_id'] != writer_lease_id):
        from agent.runtime_session_store import WorkerPersistenceError
        raise WorkerPersistenceError('managed_child_authority_mismatch')
    # Schema repair re-enters the same child. Its observed shell cwd can change,
    # but the original workspace/tool/profile binding cannot become a new grant.
    store.bind_process_scope(facts)
    token = _BOUND.set((store, store.process_frame))
    try:
        yield
    finally:
        _BOUND.reset(token)


def cancel_child_agent(child):
    """Called outside agent locks after the native hard-stop latch is published."""
    from agent.runtime_session_child import ChildSessionStore
    store = getattr(child, '_session_db', None)
    if isinstance(store, ChildSessionStore):
        store.cancel_process_scope()


def _root_worker(authority, execution_id):
    from hermes_state_runtime import RuntimeStoreError
    from hermes_state_runtime_workers import CHILD_SEPARATOR
    root = execution_id.split(CHILD_SEPARATOR, 1)[0]
    worker = next((item for item in getattr(authority, '_managed_workers', {}).values()
                   if item.scope['execution_id'] == root), None)
    if worker is None or worker.stop.is_set():
        raise RuntimeStoreError('stale_generation')
    return worker


def _live_child(authority, params):
    from hermes_state_runtime import RuntimeStoreError, _epoch, _worker_assignment
    with authority.db._read_ctx() as conn:
        _epoch(conn, params['epoch'])
        row = _worker_assignment(conn, params['execution_id'], params['session_id'], params['generation'])
        parent_id = params['execution_id'].rsplit('/child:', 1)[0]
        parent = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (parent_id,)).fetchone()
        session = conn.execute('SELECT parent_session_id FROM sessions WHERE id=?', (params['session_id'],)).fetchone()
        if (row['kind'] != 'child' or row['status'] != 'running' or row['owner_epoch'] != authority.epoch
                or parent is None or parent['status'] != 'running' or parent['owner_epoch'] != authority.epoch
                or session['parent_session_id'] != parent['session_id']):
            raise RuntimeStoreError('stale_generation')
    return parent_id, parent['session_id']


def _child_workspace(authority, original, requested, task_id, writer_lease_id):
    """Preserve inherited cwd or the producer's existing verified writer admission.

    Native writer admission proves linked-worktree/ancestry and exclusive writer
    ownership. It does not define a project ACL; cwd is not such an ACL either.
    An arbitrary model path or a git repository alone is never admission evidence.
    """
    from contextlib import closing
    import sqlite3
    import time
    from hermes_state_runtime import RuntimeStoreError
    if not isinstance(requested, str):
        raise RuntimeStoreError('permission_denied')
    root, candidate = Path(original).resolve(), Path(requested).resolve()
    if candidate == root and writer_lease_id is None:
        return str(candidate)
    if not isinstance(writer_lease_id, str):
        raise RuntimeStoreError('permission_denied', detail='Changed child workspace has no original native writer admission.')
    from hermes_cli.admission_contract import AdmissionCaller
    from hermes_cli.admission_runtime import _worktree_request, ledger_path
    evidence = _worktree_request(request_id=task_id, caller=AdmissionCaller.DELEGATE,
        workspace=str(candidate), priority=0, writer_id='delegate:' + task_id)
    path = ledger_path(Path(authority.profile_id))
    if not path.exists():
        raise RuntimeStoreError('permission_denied')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('SELECT source,state,writer_id,write_targets_json,lease_expires_at '
            'FROM admission_requests WHERE request_id=? AND lease_id=?', (task_id, writer_lease_id)).fetchone()
    if (row is None or row[:3] != (AdmissionCaller.DELEGATE.value + ':' + evidence.writer.writer_id,
                                  'running', evidence.writer.writer_id)
            or set(json.loads(row[3])) != set(evidence.writer.write_targets)
            or row[4] is None or row[4] <= time.time()):
        raise RuntimeStoreError('permission_denied')
    return str(candidate)


@dataclass
class ChildProcessAuthority:
    root_worker: object
    scope: dict
    task_id: str
    parent_task_id: str
    cwd: str
    allowed_tools: frozenset
    original_reservation: object
    stop: object
    process_policy: object
    bound: bool = False
    finished: bool = False
    threads: set = field(default_factory=set)
    lock: object = field(default_factory=threading.RLock)
    handed_off: list = field(default_factory=list)

    @property
    def process_task_id(self):
        return self.task_id

    @property
    def process_frame(self):
        return self.root_worker.process_frame

    @property
    def native_ref(self):
        return self.root_worker.native_ref

    @property
    def admission(self):
        return self.root_worker.admission


def owner_child_authority(authority, params):
    from hermes_state_runtime import RuntimeStoreError
    if '/child:' not in params['execution_id']:
        return None
    worker = _root_worker(authority, params['execution_id'])
    with worker._native_child_process_lock:
        _live_child(authority, params)
        child = worker._native_child_process_scopes.get(params['execution_id'])
        if (child is None or not child.bound or child.finished or child.stop.is_set()
                or not child.original_reservation.active()):
            raise RuntimeStoreError('permission_denied', detail='The original native child scope is unavailable.')
        return child


def bind_owner_child(authority, params, commit):
    """After native child claim verification, create its own delegated reservation."""
    from hermes_state_runtime import RuntimeStoreError
    from tools.approval_context import (completion_approval_task, delegated_approval_task,
        require_live_task_setup, reserve_background_approval_task)
    from toolsets import resolve_toolset
    payload = params['payload']
    if (not isinstance(payload, dict) or set(payload) != {'task_id', 'cwd', 'allowed_tools', 'profile_id', 'writer_lease_id'}
            or payload['profile_id'] != params['profile_id']
            or not isinstance(payload['task_id'], str)
            or re.fullmatch(r'sa-\d+-[0-9a-f]{8}', payload['task_id']) is None
            or not isinstance(payload['allowed_tools'], list)
            or any(tool not in {'terminal', 'process_manage'} for tool in payload['allowed_tools'])):
        raise RuntimeStoreError('invalid_params')
    worker = _root_worker(authority, params['execution_id'])
    with worker._native_child_process_lock:
        parent_id, parent_sid = _live_child(authority, params)
        previous = worker._native_child_process_scopes.get(params['execution_id'])
        if previous is not None:
            if previous.scope['payload'] != payload or previous.finished or not previous.bound:
                raise RuntimeStoreError('admission_conflict')
            commit()
            return {'bound': True}
        parent = worker._native_child_process_scopes.get(parent_id)
        if '/child:' in parent_id and parent is None:
            raise RuntimeStoreError('permission_denied')
        source = parent or worker
        allowed = (parent.allowed_tools if parent else {
            name for group in worker.process_frame['policy']['toolsets'] for name in resolve_toolset(group)})
        if not set(payload['allowed_tools']).issubset(allowed):
            raise RuntimeStoreError('permission_denied')
        if any(item.task_id == payload['task_id'] for item in worker._native_child_process_scopes.values()):
            raise RuntimeStoreError('admission_conflict')
        from gateway.session_authorities import owner_scope
        from tools.terminal_tool import get_session_cwd
        with owner_scope(authority):
            inherited = get_session_cwd(parent.task_id if parent else parent_sid)
            if inherited is None and parent is None:
                inherited = get_session_cwd(worker.process_frame['route'])
        inherited = inherited or (parent.cwd if parent else worker.process_frame['policy']['cwd'])
        cwd = _child_workspace(authority, inherited, payload['cwd'], payload['task_id'], payload['writer_lease_id'])
        stop = threading.Event()
        with completion_approval_task((source.original_reservation,), 'native-child-parent'):
            with delegated_approval_task(payload['task_id'], stop_event=stop):
                require_live_task_setup()
                reservation = reserve_background_approval_task()
        if reservation is None:
            raise RuntimeStoreError('permission_denied')
        policy = replace(worker.process_policy, cache_key=params['execution_id'], workspace=cwd, environments={},
                         env_lock=threading.RLock())
        child = ChildProcessAuthority(worker, dict(params), payload['task_id'],
            parent.task_id if parent else parent_sid, cwd, frozenset(payload['allowed_tools']), reservation, stop, policy)
        worker._native_child_process_scopes[params['execution_id']] = child
        try:
            commit()
        except BaseException:
            child.finished = True
            child.original_reservation.release()
            raise
        child.bound = True
        return {'bound': True}


@contextmanager
def child_consumer_scope(child):
    """Use the native child's callback, cwd and thread cancellation fence."""
    from agent.runtime_cwd import set_session_cwd
    from tools import terminal_tool
    from tools.delegate_tool_config import _subagent_auto_approve, _subagent_auto_deny
    from tools.interrupt import set_interrupt
    from tools.approval_context import require_live_task_setup
    from utils import is_truthy_value
    tid = threading.get_ident()
    with child.lock:
        require_live_task_setup()
        if child.stop.is_set() or child.finished:
            raise ValueError('The native child was stopped or completed.')
        child.threads.add(tid)
    token = set_session_cwd(child.cwd)
    auto_approve = is_truthy_value(child.process_policy.config.get('delegation', {}).get('subagent_auto_approve', False))
    callback = (_subagent_auto_approve if auto_approve
                else _subagent_auto_deny)
    terminal_tool.set_approval_callback(callback)
    terminal_tool.record_session_cwd(child.task_id, child.cwd)
    try:
        yield
    finally:
        with child.lock:
            child.threads.discard(tid)
            set_interrupt(False, tid)
        token.var.reset(token)


def owner_child_finished(authority, params):
    """The existing child close reaps only its own unhanded lifecycle resources."""
    if '/child:' not in params['execution_id']:
        return
    worker = _root_worker(authority, params['execution_id'])
    with worker._native_child_process_lock:
        child = worker._native_child_process_scopes.get(params['execution_id'])
        if child is None or child.finished:
            return
        child.finished = True
        child.original_reservation.release()
    from tools.process_registry import process_registry
    process_registry.kill_all(child.task_id, source='agent_close')


def owner_child_account(authority, params):
    """The existing delegation result contains only this native child's resources."""
    from gateway.session_authorities import owner_scope
    from gateway.session_managed_approval import approval_policy_scope
    from tools.delegation_redaction import redact_child_text
    from tools.process_registry import process_registry, _output_tail
    import time
    from hermes_state_runtime import RuntimeStoreError
    if params['payload'] != {}:
        raise RuntimeStoreError('invalid_params')
    child = owner_child_authority(authority, params)
    if child is None:
        raise RuntimeStoreError('permission_denied')
    with owner_scope(authority, hydrate_secrets=True), approval_policy_scope(child.process_policy):
        result = {}
        with child.lock:
            if child.handed_off:
                result['handed_off_processes'] = [dict(item,
                    command=redact_child_text(item['command'], force=False)) for item in child.handed_off]
        leftover = process_registry.running_owned_by(child.task_id)
        if leftover:
            result['orphaned_processes'] = [{'session_id': session.id,
                'command': redact_child_text(session.command, force=False)[:200],
                'runtime_seconds': round(time.time() - session.started_at)} for session in leftover]
        unread = process_registry.unread_completions_owned_by(child.task_id)
        if unread:
            result['unread_completions'] = [{'session_id': session.id,
                'command': redact_child_text(session.command, force=False)[:200], 'exit_code': session.exit_code,
                'output_tail': redact_child_text(_output_tail(session, len(session.output_buffer)), force=False)[-600:]}
                for session in unread]
        return result


def cancel_owner_child(authority, params):
    """Retire this authenticated child through the existing native retirement path.

    The sequence references the current request; it never reserves the next
    receipt or waits for the producer's journal lock. Retirement is monotonic.
    """
    from hermes_state_runtime import RuntimeStoreError, _epoch, _worker_assignment
    from hermes_state_runtime_workers import _retire_children, descendant_range
    from tools.interrupt import set_interrupt
    if params['payload'] != {}:
        raise RuntimeStoreError('invalid_params')
    worker = _root_worker(authority, params['execution_id'])
    with worker._native_child_process_lock:
        def retire(conn):
            _epoch(conn, params['epoch'])
            row = _worker_assignment(conn, params['execution_id'], params['session_id'], params['generation'])
            parent = conn.execute('SELECT session_id FROM worker_executions WHERE execution_id=?',
                (params['execution_id'].rsplit('/child:', 1)[0],)).fetchone()
            session = conn.execute('SELECT parent_session_id FROM sessions WHERE id=?',
                                   (params['session_id'],)).fetchone()
            if (row['kind'] != 'child' or row['owner_epoch'] != authority.epoch or parent is None
                    or session['parent_session_id'] != parent['session_id']
                    or type(params['sequence']) is not int or not 0 <= params['sequence'] <= row['last_sequence'] + 1):
                raise RuntimeStoreError('permission_denied')
            descendants = conn.execute('SELECT execution_id,session_id FROM worker_executions '
                                      'WHERE execution_id>=? AND execution_id<?',
                                      descendant_range(params['execution_id'])).fetchall()
            _retire_children(conn, [(params['execution_id'], params['session_id']), *descendants])
            return {'retired': True}
        result = authority.db._execute_write(retire)
        children = [child for execution, child in worker._native_child_process_scopes.items()
                    if execution == params['execution_id'] or execution.startswith(params['execution_id'] + '/child:')]
        for child in children:
            with child.lock:
                child.stop.set()
                child.finished = True
                child.original_reservation.release()
                for tid in child.threads:
                    set_interrupt(True, tid, reason='native delegated child stopped')
    from tools.process_registry import process_registry
    for child in children:
        for session in process_registry.running_owned_by(child.task_id):
            task = getattr(session, '_managed_lifetime_task', None)
            if task is not None:
                task.get_loop().call_soon_threadsafe(task.cancel)
        process_registry.kill_all(child.task_id, source='managed_child_stop', consume_output=True)
    return result


def owner_handoff(child, registry, arguments):
    """Apply the existing transfer only for this verified live child and parent."""
    note = str(arguments.get('data') or '').strip()
    with child.lock:
        if not note or len(child.handed_off) >= 3 or child.finished or child.stop.is_set():
            return json.dumps({'error': 'handoff requires a live child, purpose note and available native handoff slot'})
        existing = registry.get(arguments['session_id'])
        lifetime = getattr(existing, '_managed_lifetime_reservation', None)
        if lifetime is not None and not lifetime.active():
            return json.dumps({'error': 'The native process lifetime was stopped before handoff.'})
        session = registry.transfer_ownership(arguments['session_id'], from_owner=child.task_id,
            to_owner=child.parent_task_id, to_task_id=child.parent_task_id,
            to_session_key=child.root_worker.process_frame['route'], note=note, child_stop_event=child.stop)
        if session is None:
            return json.dumps({'error': 'The process exited, changed ownership or its native task was stopped.'})
        if lifetime is not None and not lifetime.transfer_to_parent(child.stop):
            return json.dumps({'error': 'The native process lifetime was stopped before handoff.'})
        child.handed_off.append({'session_id': session.id, 'command': session.command, 'note': note})
    return json.dumps({'status': 'handed_off', 'session_id': session.id, 'command': session.command,
                       'note': 'Your live parent now owns this process and will receive its completion.'})
