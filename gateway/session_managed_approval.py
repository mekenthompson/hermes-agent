"""Original native admission authority and frozen approval readers for managed process tools."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import threading

_POLICY = ContextVar('managed_process_approval_policy', default=None)


@dataclass
class ManagedApprovalPolicy:
    config: dict
    yolo: bool
    safe: bool
    ignore: bool
    cache_key: str
    session_approved: set = field(default_factory=set)
    permanent: set = field(default_factory=set)
    permanent_baseline: set = field(default_factory=set)
    environments: dict = field(default_factory=dict)
    env_lock: object = field(default_factory=threading.RLock)
    workspace: str = ''
    interactive: bool = False
    ask: bool = False


def current_policy():
    return _POLICY.get()


@contextmanager
def approval_policy_scope(policy):
    token = _POLICY.set(policy)
    try:
        yield
    finally:
        _POLICY.reset(token)


def configure_policy(worker, frame):
    config = json.loads(frame['policy']['config_json'])
    turn = json.loads(frame['policy']['request_json'])['turn_v1']
    # This is the actual environment the owner selected for this interpreter.
    inherited = worker.launch_yolo
    worker.process_policy = ManagedApprovalPolicy(config, turn['yolo'] or inherited,
        frame['safe_mode'], frame['ignore_user_config'], frame['scope']['execution_id'])
    with approval_policy_scope(worker.process_policy):
        from tools.approval import _read_permanent_allowlist
        worker.process_policy.permanent = _read_permanent_allowlist()
        worker.process_policy.permanent_baseline = set(worker.process_policy.permanent)
    worker.process_frame = frame
    worker.process_policy.workspace = frame['policy']['cwd']
    worker.process_policy.interactive, worker.process_policy.ask = worker.launch_interactive, worker.launch_ask


def cached_environment(plan):
    """The original interpreter had a fresh per-worker cache, not the gateway's cache."""
    policy = current_policy()
    if policy is None:
        return None
    from tools.terminal_tool import _create_configured_env
    with policy.env_lock:
        if plan.effective_task_id not in policy.environments:
            policy.environments[plan.effective_task_id] = _create_configured_env(
                plan.config, plan.env_type, image=plan.image, cwd=plan.cwd,
                timeout=plan.effective_timeout, task_id=plan.effective_task_id, host_cwd=plan.host_cwd)
        return policy.environments[plan.effective_task_id]


def _managed_current(authority, ref, row, worker):
    if worker.stop.is_set():
        return False
    result = (authority.pending_results.get(row['admission_id']) or {}).get('result')
    if isinstance(result, dict) and not any(result.get(key) for key in ('failed', 'interrupted', 'error')):
        return True  # Native task scope still checks the exact claim and successful terminal receipt.
    if getattr(authority, '_managed_workers', {}).get(ref.session_id) is not worker:
        return False
    with authority.db._read_ctx() as conn:
        registered = conn.execute('SELECT status,owner_epoch,generation FROM worker_executions WHERE execution_id=?',
                                  (worker.scope['execution_id'],)).fetchone()
    return bool(registered and registered['status'] in {'registered', 'running'}
                and registered['owner_epoch'] == authority.epoch and registered['generation'] == row['generation'])


@contextmanager
def managed_admission_scope(authority, ref, row, worker):
    from gateway.config import Platform
    from gateway.session_ingress import _approval_task_scope, restore_native
    from gateway.platforms.event import MessageEvent
    from tools.approval_context import release_background_approval_task, reserve_background_approval_task

    live = authority.sessions[ref.session_id]
    if 'native_text_v1' in row['payload']:
        event = restore_native(row['payload'], authority.runner)
    else:
        event = MessageEvent(text=row['payload']['text'], source=live.source, message_id=row['admission_id'])
        if 'local_automation_v1' in row['payload']:
            from gateway.session_automation import restore_local_automation
            event = restore_local_automation(authority, ref, row)
    event._managed_worker_current = lambda: _managed_current(authority, ref, row, worker)
    with _approval_task_scope(authority, ref, row, live, event, is_api=live.source.platform == Platform.API_SERVER):
        worker.original_reservation = reserve_background_approval_task()
        try:
            yield
        finally:
            release_background_approval_task(worker.original_reservation)
            if getattr(worker, 'process_notifier', None) is not None:
                from tools import approval
                with approval._lock:
                    if approval._gateway_notify_cbs.get(live.route) is worker.process_notifier:
                        approval._gateway_notify_cbs.pop(live.route)
                        if worker.previous_process_notifier is not None:
                            approval._gateway_notify_cbs[live.route] = worker.previous_process_notifier
                        for entry in approval._gateway_queues.pop(live.route, []):
                            entry.event.set()
            from tools.terminal_tool_sudo import _sudo_password_cache, _sudo_password_cache_lock
            with _sudo_password_cache_lock:
                _sudo_password_cache.pop('managed:' + worker.scope['execution_id'], None)


def install_approval_notifier(authority, worker):
    import asyncio
    from tools import approval
    from tools.approval import ack_gateway_approval
    loop, ref = asyncio.get_running_loop(), worker.native_ref
    route, generation = authority.sessions[ref.session_id].route, worker.admission['generation']

    async def publish(data):
        authority.check_approval_generation(ref.session_id, generation)
        authority.register_approval(ref.session_id, generation, route, data)

    def notify(data):
        asyncio.run_coroutine_threadsafe(publish(data), loop).result(timeout=10)
        ack_gateway_approval(route, data['request_id'])

    with approval._lock:
        worker.previous_process_notifier = approval._gateway_notify_cbs.get(route)
        worker.process_notifier = notify
        approval._gateway_notify_cbs[route] = notify


@contextmanager
def process_consumer_scope(authority, worker, physical_session_id):
    from contextlib import ExitStack
    from gateway.session import build_session_context
    from gateway.session_authorities import owner_scope
    from gateway.session_policy import bind_turn_workspace, policy_scope, restore_policy
    from agent.auxiliary_task_config import session_auxiliary_scope
    from gateway.session_managed_worker import worker_turn_scope
    from tools.approval_context import completion_approval_task, require_live_task_setup, set_current_session_key, reset_current_session_key
    from tools import terminal_tool

    reservation = getattr(worker, 'original_reservation', None)
    if reservation is None:
        from hermes_state_runtime import RuntimeStoreError
        raise RuntimeStoreError('permission_denied', detail='Managed process request has no original native authority.')
    frame, policy = worker.process_frame, restore_policy(worker.process_frame['policy'])
    live = authority.sessions[worker.native_ref.session_id]
    with ExitStack() as stack:
        # Borrow the original reservation. background_approval_task consumes its
        # argument and would prematurely release this worker's shared source.
        stack.enter_context(completion_approval_task((reservation,), 'managed-process'))
        try:
            require_live_task_setup()
        except ValueError as exc:
            from hermes_state_runtime import RuntimeStoreError
            raise RuntimeStoreError('permission_denied', detail='The original native managed authority is no longer live.') from exc
        stack.enter_context(owner_scope(authority, hydrate_secrets=True))
        stack.enter_context(policy_scope(policy))  # Match the child's frozen policy, never follow live profile edits.
        stack.enter_context(session_auxiliary_scope(worker.process_policy.config.get('auxiliary')))
        stack.enter_context(worker_turn_scope(frame))
        stack.enter_context(approval_policy_scope(worker.process_policy))
        bind_turn_workspace(policy, physical_session_id)
        entry = authority.runner.session_store.lookup_by_session_key(live.route)
        context = build_session_context(live.source, authority.runner.config, entry)
        context.session_id = physical_session_id
        stack.enter_context(authority.runner._session_env_scope(context))
        token = set_current_session_key(live.route)
        saved = dict(terminal_tool._callback_tls.__dict__)
        terminal_tool._callback_tls.__dict__.clear()
        try:
            with ExitStack() as child_scope:
                if hasattr(worker, 'process_task_id'):
                    from tools.managed_process_child import child_consumer_scope
                    child_scope.enter_context(child_consumer_scope(worker))
                yield
        finally:
            terminal_tool._callback_tls.__dict__.clear()
            terminal_tool._callback_tls.__dict__.update(saved)
            reset_current_session_key(token)
