"""Managed process requests reuse native worker receipts and the owner's process registry."""
import hashlib
import json
import uuid

from hermes_state_runtime import RuntimeStoreError, apply_worker_receipt


def request_digest(tool, arguments, observed_cwd=None):
    request = [tool, arguments] if observed_cwd is None else [tool, arguments, observed_cwd]
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def reserve_request(db, scope, sequence, payload):
    """Return (stable descriptor receipt, transaction-fresh); never persist freshness."""
    if (not isinstance(payload, dict) or set(payload) != {'tool', 'digest'}
            or payload['tool'] not in {'terminal', 'process_manage'}
            or not isinstance(payload['digest'], str) or len(payload['digest']) != 64
            or any(char not in '0123456789abcdef' for char in payload['digest'])):
        raise RuntimeStoreError('invalid_params')

    def reserve(_db, _conn, _session_id, request):
        return {'resource_id': 'proc_' + uuid.uuid4().hex[:12], **request, 'issued': False}

    return apply_worker_receipt(db, **{key: scope[key] for key in (
        'epoch', 'execution_id', 'session_id', 'generation')}, sequence=sequence,
        operation='process.reserve', payload=payload, handlers={'process.reserve': reserve}, _with_fresh=True)


def materialize_descriptor(registry, receipt, fresh, *, task_id, route, cwd, parent_session_id=None):
    """Only a fresh committed reservation may create an existing pending descriptor."""
    from tools.process_registry import ProcessSession

    resource_id = receipt['resource_id']
    with registry._lock:
        session = registry._running.get(resource_id) or registry._finished.get(resource_id)
        if session is not None:
            if session.detached:
                raise RuntimeStoreError('unknown_execution', detail='The original process control was lost; do not replay its command.')
            return session
        if not fresh:
            raise RuntimeStoreError('unknown_execution', detail='The reserved process descriptor is missing; do not replay its command.')
        session = ProcessSession(id=resource_id, command='[native process request reserved]',
            task_id=task_id, owner_task_id=task_id, session_key=route,
            parent_session_id=parent_session_id or task_id, cwd=cwd)
        session._native_local_launch = session._local_launch_pending = True
        session._managed_gateway_hosted = True  # Location provenance only; never an execution grant.
        session.reconciliation_required = 'Native process request reserved; backend start is not yet confirmed.'
        registry._running[resource_id] = session
    if registry._write_checkpoint() is False:
        from tools.process_registry_spawn import cancel_unstarted_local_launch
        cancel_unstarted_local_launch(registry, session, 'worker_checkpoint_refused')
        raise RuntimeStoreError('storage_unavailable', detail='Process checkpoint failed; target was not started.')
    return session


def claim_request(db, scope, sequence, payload):
    """Claim the existing receipt once before effects, returning replay facts, never authority."""
    from hermes_state_runtime import _epoch, _worker_assignment

    def write(conn):
        _epoch(conn, scope['epoch'])
        worker = _worker_assignment(conn, scope['execution_id'], scope['session_id'], scope['generation'])
        if worker['status'] != 'running' or worker['owner_epoch'] != scope['epoch']:
            raise RuntimeStoreError('stale_generation')
        row = conn.execute('SELECT result_json FROM worker_receipts WHERE execution_id=? AND sequence=?',
                           (scope['execution_id'], sequence)).fetchone()
        if row is None:
            raise RuntimeStoreError('not_found')
        saved = json.loads(row['result_json'])
        if (saved.get('tool') != payload['tool'] or payload.get('resource_id', saved.get('resource_id')) != saved.get('resource_id')
                or saved.get('digest') != request_digest(payload['tool'], payload['arguments'], payload.get('observed_cwd'))):
            raise RuntimeStoreError('admission_conflict')
        issued = saved['issued']
        if not issued:
            conn.execute('UPDATE worker_receipts SET result_json=? WHERE execution_id=? AND sequence=?',
                         (json.dumps(dict(saved, issued=True)), scope['execution_id'], sequence))
        return saved, not issued

    return db._execute_write(write)


def validate_arguments(tool, arguments):
    from tools.terminal_tool import TERMINAL_SCHEMA
    from tools.process_registry import PROCESS_SCHEMA
    schema = {'terminal': TERMINAL_SCHEMA, 'process_manage': PROCESS_SCHEMA}.get(tool)
    if schema is None or not isinstance(arguments, dict):
        raise RuntimeStoreError('invalid_params')
    parameters = schema['parameters']
    if set(arguments) - parameters['properties'].keys() or set(parameters.get('required', ())) - arguments.keys():
        raise RuntimeStoreError('invalid_params')
    for name, value in arguments.items():
        if not _matches(value, parameters['properties'][name]):
            raise RuntimeStoreError('invalid_params', field=name)


def _matches(value, schema):
    import math
    if 'anyOf' in schema:
        return any(_matches(value, candidate) for candidate in schema['anyOf'])
    if 'enum' in schema and value not in schema['enum']:
        return False
    kind = schema.get('type')
    if kind == 'boolean':
        return type(value) is bool
    if kind in {'integer', 'number'}:
        valid = type(value) is int if kind == 'integer' else type(value) in {int, float}
        return (valid and math.isfinite(value) and schema.get('minimum', value) <= value
                and value <= schema.get('maximum', value))
    if kind == 'string':
        return isinstance(value, str)
    if kind == 'array':
        return isinstance(value, list) and all(_matches(item, schema['items']) for item in value)
    if kind == 'object':
        if not isinstance(value, dict) or set(schema.get('required', ())) - value.keys():
            return False
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - properties.keys():
            return False
        return all(key not in properties or _matches(item, properties[key]) for key, item in value.items())
    return False


def _worker(connection, ref, params):
    from gateway.session_worker import _SCOPE, _claim, _verify
    from tools.approval_context import require_live_task_setup
    if set(params) != _SCOPE | {'epoch', 'sequence', 'operation', 'payload'}:
        raise RuntimeStoreError('invalid_params')
    if 'worker:adopt' not in connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    _verify(connection, ref, params, _claim(connection, ref, params))
    from tools.managed_process_child import owner_child_authority
    worker = owner_child_authority(connection.authority, params)
    if worker is None:
        worker = next((item for item in getattr(connection.authority, '_managed_workers', {}).values()
                   if getattr(item, 'scope', {}).get('execution_id') == params['execution_id']), None)
    reservation = getattr(worker, 'original_reservation', None)
    if worker is None or reservation is None or not reservation.active():
        raise RuntimeStoreError('permission_denied', detail='Original native managed authority is unavailable.')
    if worker.stop.is_set():
        raise RuntimeStoreError('stale_generation')
    from hermes_state_runtime import get_session_admission
    row = get_session_admission(connection.authority.db, admission_id=worker.admission['admission_id'])
    if (row is None or row['status'] != 'started' or row['owner_epoch'] != connection.authority.epoch
            or row['generation'] != worker.admission['generation']):
        raise RuntimeStoreError('stale_generation')
    return worker


def _allowed(worker, tool):
    from toolsets import resolve_toolset
    policy = worker.process_frame['policy']
    allowed = {name for group in policy['toolsets'] for name in resolve_toolset(group)}
    if tool not in allowed or (hasattr(worker, 'allowed_tools') and tool not in worker.allowed_tools):
        raise RuntimeStoreError('permission_denied')


def _authorize_process(authority, worker, registry, arguments):
    from hermes_state_runtime_workers import runtime_lineage
    if arguments['action'] == 'list':
        return
    process = registry.get(arguments.get('session_id', ''))
    if hasattr(worker, 'process_task_id'):
        if process is None or process.owner_task_id != worker.process_task_id:
            raise RuntimeStoreError('permission_denied')
        return
    with authority.db._read_ctx() as conn:
        owners = runtime_lineage(conn, worker.native_ref.session_id)
    route = authority.sessions[worker.native_ref.session_id].route
    if (process is None or process.session_key not in owners | {route}
            or not ({process.owner_task_id, process.task_id, process.parent_session_id} & owners)):
        raise RuntimeStoreError('permission_denied')


def _process_result(worker, registry, arguments, result):
    if arguments['action'] != 'list' or not hasattr(worker, 'process_task_id'):
        return result
    projection = json.loads(result)
    with registry._lock:
        owned = {item.id for store in (registry._running, registry._finished) for item in store.values()
                 if item.owner_task_id == worker.process_task_id}
    projection['processes'] = [item for item in projection['processes'] if item['session_id'] in owned]
    return json.dumps(projection)


def _execute(connection, worker, params):
    from gateway.session_managed_approval import process_consumer_scope
    from tools.approval_context import require_live_task_setup
    from tools.managed_process_proxy import background_descriptor
    from tools.process_registry import process_registry as registry, _handle_process
    from tools.terminal_tool import _handle_terminal

    payload = params['payload']
    fields = {'tool', 'arguments', 'resource_id'}
    if (not isinstance(payload, dict) or not fields <= set(payload) <= fields | {'observed_cwd'}
            or (payload.get('observed_cwd') is not None and not isinstance(payload['observed_cwd'], str))):
        raise RuntimeStoreError('invalid_params')
    tool, arguments = payload['tool'], payload['arguments']
    _allowed(worker, tool)
    authority = connection.authority
    task_id = getattr(worker, 'process_task_id', params['session_id'])
    with process_consumer_scope(authority, worker, params['session_id']):
        require_live_task_setup()
        if tool == 'process_manage':
            validate_arguments(tool, arguments)
            _authorize_process(authority, worker, registry, arguments)
        saved, fresh = claim_request(authority.db, params, params['sequence'], payload)
        if saved['resource_id'] != payload['resource_id']:
            raise RuntimeStoreError('admission_conflict')
        if tool == 'process_manage':
            if not fresh and arguments['action'] not in {'list', 'poll', 'log', 'wait'}:
                raise RuntimeStoreError('unknown_execution', detail='This process control was already issued; inspect its actual state before retrying.')
            if arguments['action'] == 'handoff' and hasattr(worker, 'process_task_id'):
                from tools.managed_process_child import owner_handoff
                return owner_handoff(worker, registry, arguments)
            return _process_result(worker, registry, arguments, _handle_process(arguments, task_id=task_id))
        session = materialize_descriptor(registry, saved, False, task_id=task_id,
            route=authority.sessions[worker.native_ref.session_id].route, cwd=worker.process_policy.workspace,
            parent_session_id=params['session_id'])
        if not fresh:
            return json.dumps(registry.poll(session.id))
        try:
            validate_arguments(tool, arguments)
            from tools.terminal_tool import record_session_cwd, clear_session_cwd
            observed_cwd = payload.get('observed_cwd')
            if observed_cwd:
                record_session_cwd(task_id, observed_cwd)
            else:
                clear_session_cwd(task_id)
            with background_descriptor(session):
                return _handle_terminal(arguments, task_id=task_id, session_id=params['session_id'])
        finally:
            from tools.process_registry_spawn import cancel_unstarted_local_launch
            if session.pid_scope == 'host':
                cancel_unstarted_local_launch(registry, session, 'managed_process_not_started')


async def process_request(connection, ref, params):
    import asyncio
    from gateway.session_runtime_workers import track_mutation
    from gateway.session_managed_approval import process_consumer_scope
    from tools.approval_context import require_live_task_setup
    from tools.process_registry import process_registry as registry

    worker = _worker(connection, ref, params)
    if params['operation'] == 'process.call':
        async def execute():
            result = await asyncio.to_thread(_execute, connection, worker, params)
            return json.loads(result)
        return await asyncio.shield(track_mutation(connection.authority, execute()))
    if params['operation'] != 'process.reserve':
        raise RuntimeStoreError('invalid_params')

    def reserve():
        with process_consumer_scope(connection.authority, worker, params['session_id']):
            require_live_task_setup()
            _allowed(worker, params['payload'].get('tool'))
            saved, fresh = reserve_request(connection.authority.db, params, params['sequence'], params['payload'])
            if saved['tool'] == 'terminal':
                materialize_descriptor(registry, saved, fresh,
                    task_id=getattr(worker, 'process_task_id', params['session_id']),
                    route=connection.authority.sessions[worker.native_ref.session_id].route,
                    cwd=worker.process_policy.workspace, parent_session_id=params['session_id'])
            return saved

    async def reserve_and_attach():
        saved = await asyncio.to_thread(reserve)
        if saved['tool'] == 'terminal':
            with registry._lock:
                session = registry._running.get(saved['resource_id']) or registry._finished.get(saved['resource_id'])
            if session is None:
                raise RuntimeStoreError('unknown_execution')
            await attach_lifetime(registry, session, worker)
        return saved

    return await asyncio.shield(track_mutation(connection.authority, reserve_and_attach()))


class _CompletionSignal:
    """Bridge this descriptor's existing completion event to its native owner loop."""
    def __init__(self, event, loop, future):
        self.event, self.loop, self.future = event, loop, future

    def set(self):
        self.event.set()
        self.loop.call_soon_threadsafe(self._resolve)

    def _resolve(self):
        if not self.future.done():
            self.future.set_result(None)

    def wait(self, timeout=None):
        return self.event.wait(timeout)

    def is_set(self):
        return self.event.is_set()

    def clear(self):
        self.event.clear()


def _lifetime_context(original):
    from contextvars import copy_context
    from tools.approval_context import completion_approval_task, require_live_task_setup, reserve_background_approval_task
    with completion_approval_task((original,), 'managed-resource-owner'):
        require_live_task_setup()
        child = reserve_background_approval_task()
        if child is None:
            raise RuntimeStoreError('permission_denied')
        with completion_approval_task((child,), 'managed-resource-lifetime'):
            return child, copy_context()


async def _watch_resource(registry, session, completed, reservation):
    import asyncio
    from gateway.session_ancillary import _stop_process_targets
    from gateway.session_command_process import _settle
    from tools.approval_context import release_background_approval_task
    from tools.process_registry_spawn import retain_remote_unknown
    try:
        await asyncio.shield(completed)
    except asyncio.CancelledError:
        if session.persist_on_release:
            try:
                await asyncio.wait_for(asyncio.shield(completed), timeout=5)
            except TimeoutError:
                retain_remote_unknown(registry, session,
                    'A persisted native process remains running or unconfirmed; reconcile it before declaring this profile drained.')
        else:
            await _settle(asyncio.create_task(asyncio.to_thread(
                _stop_process_targets, registry, [session.id], 'managed_resource_stop')))
        # The existing unknown descriptor keeps Stop/drain fenced. A historical
        # Task exception must not prevent fresh authorized physical reconciliation.
        raise
    finally:
        release_background_approval_task(reservation)


async def attach_lifetime(registry, session, worker):
    import asyncio

    async def install():
        loop, future = asyncio.get_running_loop(), asyncio.get_running_loop().create_future()
        previous = session._completion_event
        session._completion_event = signal = _CompletionSignal(previous, loop, future)
        if previous.is_set():
            signal.set()  # Known NoStart or actual completion won the attachment race.
        reservation, context = await asyncio.to_thread(_lifetime_context, worker.original_reservation)
        session._managed_lifetime_reservation = reservation
        task = asyncio.create_task(_watch_resource(registry, session, future, reservation), context=context)
        if not await asyncio.to_thread(reservation.lease.track_effect, task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise RuntimeStoreError('permission_denied')
        session._managed_lifetime_task = task

        def finished(done):
            if not done.cancelled():
                done.exception()  # Unknown records remain visible even if the requesting viewer disconnected.
        task.add_done_callback(finished)

    setup = getattr(session, '_managed_lifetime_setup', None)
    if setup is None:
        session._managed_lifetime_setup = setup = asyncio.create_task(install())
    await asyncio.shield(setup)
