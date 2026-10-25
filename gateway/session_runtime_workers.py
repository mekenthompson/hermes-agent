"""Physical worker liveness and cooperative Stop across every served authority."""
import asyncio

from gateway.session_authorities import all_authorities


def executing_sessions(authority):
    for sid, live in getattr(authority, 'sessions', {}).items():
        execution = getattr(getattr(live, 'event_stream', None), 'execution', None) or {}
        task = live.task
        if execution.get('execution_generation') is not None and task is not None and not task.done():
            yield sid, live, execution['execution_generation']


def uncounted_runtime_work(runner):
    """Count claims absent from the ordinary agent map, including pre-bootstrap managed turns."""
    running = getattr(runner, '_running_agents', {})
    authorities = all_authorities(runner)
    managed = sum(not worker.closed.is_set() for authority in authorities
                  for worker in getattr(authority, '_managed_workers', {}).values())
    return (managed + sum(live.route not in running and sid not in getattr(authority, '_managed_workers', {})
                for authority in authorities for sid, live, _ in executing_sessions(authority))
            + sum(len(mutation_tasks(authority)) + len(side_agents(authority))
                  + len(command_effects(authority)) + len(command_unknowns(authority))
                  for authority in authorities))


def command_effects(authority):
    """Physical quick commands remain profile work after their RPC observer disconnects."""
    return {task for live in getattr(authority, 'sessions', {}).values()
            for lease in list(getattr(live, 'approval_task_leases', ()))
            for task in lease.effect_tasks() if not task.done()}


def command_unknowns(authority, route=None):
    """Unknown command effects live in the existing durable process registry, beyond task exit."""
    from tools.process_registry import process_registry
    routes = {route} if route is not None else {live.route for live in getattr(authority, 'sessions', {}).values()
                                              if getattr(live, 'route', None)}
    with process_registry._lock:
        all_uncertain = [session for session in process_registry._running.values()
                         if session.reconciliation_required and not session.exited]
        uncertain = [session for session in all_uncertain if session.session_key in routes]
        private_ownership = any(getattr(session, 'approval_task_reservation', None) is not None
                                for session in all_uncertain)
        described_ownership = any(session.detached and session.native_origin_profile == str(authority.profile_id)
                                  and session.native_origin_session in routes for session in all_uncertain)
    found = {session.id: session for session in uncertain if session.owner_task_id.startswith('command:')}
    if len(found) == len(uncertain) and not private_ownership and not described_ownership:
        return list(found.values())
    from gateway.session_ancillary import _owner_objects, _ownership, _owned_locked
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import RuntimeStoreError
    for sid, live in authority.sessions.items():
        if live.route not in routes:
            continue
        ref = SessionRef(authority.profile_id, sid)
        try:
            agent, records = _owner_objects(authority, ref)
        except RuntimeStoreError as exc:
            if exc.reason != 'unsupported_projection':
                raise
            continue
        keys, owners = _ownership(authority, ref, agent, records)
        with process_registry._lock:
            found.update((session.id, session) for session in _owned_locked(process_registry, keys, owners, authority, live)
                         if session.reconciliation_required and not session.exited)
        from gateway.session_cron import observed_unknown_processes
        found.update((session.id, session) for session in observed_unknown_processes(
            authority, ref, all_uncertain))
    return list(found.values())


def side_agents(authority):
    """``(thread, holder)`` for each detached side agent (/bg, /btw, preview restart) whose thread
    has not unwound (``gateway/session_tui_side.py`` ``_spawn``). A stopped ``/btw`` whose fork
    is gone (``_SideQuestionStop.abandoned``) is no longer owner work."""
    from gateway.session_tui_side import _abandoned
    return [(thread, holder) for thread, holder in list(getattr(authority, '_side_agents', {}).items())
            if not _abandoned(holder)]


def interrupt_side_agents(runner, reason):
    """Hard-interrupt every counted side agent's holder across served authorities (the stop
    drain waits for them, so its timed-out interrupt must reach them). Returns how many."""
    from agent.interrupt_compat import request_hard_interrupt
    count = 0
    for authority in all_authorities(runner):
        for _, holder in side_agents(authority):
            if holder and holder[0] is not None:
                count += bool(request_hard_interrupt(holder[0], reason))
    return count


def persist_work_count(authority, _task=None):
    """Re-publish the runner's in-flight count when owner work it counts (a drain, a tracked
    mutation, a mailbox operation) ends: the agent-slot release that last wrote
    ``gateway_state.json`` ran while that work was still live, so without this write the
    persisted count never returns to zero."""
    persist = getattr(getattr(authority, 'runner', None), '_persist_active_agents', None)
    if persist is not None:
        persist()


def mutation_tasks(authority):
    tasks = {*getattr(authority, '_mutation_tasks', ()), *getattr(authority, '_bot_mailbox_operations', ())}
    return [task for task in tasks if not task.done()]


def track_mutation(authority, operation):
    tasks = getattr(authority, '_mutation_tasks', None)
    if tasks is None:
        tasks = authority._mutation_tasks = set()
    task = asyncio.create_task(operation)
    tasks.add(task)
    def finished(done):
        tasks.discard(done)
        if not done.cancelled():
            done.exception()  # the observer may already have disconnected
        persist_work_count(authority)
    task.add_done_callback(finished)
    return task


async def tracked_write(authority, write, then=None, *, ordered=False, after=None):
    """Run a synchronous ledger write (``write()``) in a worker thread, off the owner loop.

    A held SQLite writer (``_WRITE_PATIENCE_S``) must not freeze every session, timer, socket,
    Stop and approval. The write and its loop-side follow-up (``then(result)``, sync or a
    coroutine: publication, scheduling, the claim's execution stamp) are one task tracked like ``track_mutation``, so
    retirement joins it, and shielded, so a cancelled caller cannot separate a commit from it.
    ``ordered`` writes (admissions) commit in call order, as they did on the loop: worker threads
    alone would let a later input take an earlier FIFO position.

    ``after(result)`` (an admission's drain scheduling) runs in the CALLER's own step once the
    write returns, as it did when admission was synchronous: the caller registers its delivery
    waiter before any drain it scheduled can claim, settle or pause that admission. A caller
    cancelled while the commit is in flight hands ``after`` to the commit's completion instead,
    so a committed row is never stranded without a drain."""
    order = getattr(authority, '_ordered_writes', None)
    if ordered and order is None:
        order = authority._ordered_writes = asyncio.Lock()

    async def run():
        if ordered:
            async with order:
                result = await asyncio.to_thread(write)
        else:
            result = await asyncio.to_thread(write)
        if then is None:
            return result
        followed = then(result)
        return await followed if asyncio.iscoroutine(followed) else followed
    task = track_mutation(authority, run())
    if after is None:
        return await asyncio.shield(task)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        def late(done):
            if not done.cancelled() and done.exception() is None:
                after(done.result())
        task.add_done_callback(late)
        raise
    after(result)
    return result


def start_turn_worker(runner, worker, agent_holder, run_sync):
    authority = track_turn_worker(runner, worker, agent_holder)
    worker.executor_task = asyncio.ensure_future(runner._run_in_executor_with_context(run_sync))
    if authority is not None:
        # Normal settlement releases the retained agent promptly. A cancelled asyncio waiter is
        # not physical completion; leave that entry until worker_done proves the thread exited.
        worker.executor_task.add_done_callback(lambda _: list(physical_workers(authority)))


def track_turn_worker(runner, worker, agent_holder):
    from gateway.session_authorities import active_authority
    authority = active_authority(runner)
    if authority is not None:
        workers = getattr(authority, '_turn_workers', None)
        if workers is None:
            workers = authority._turn_workers = {}
        list(physical_workers(authority))
        workers[id(worker)] = (worker, agent_holder)
    return authority


def physical_workers(authority):
    workers = getattr(authority, '_turn_workers', {})
    for key, (worker, holder) in list(workers.items()):
        if worker.worker_done.is_set():
            del workers[key]
        else:
            yield worker, holder


def stop_authority_work(authority):
    from gateway.run_runtime import stop_authority_turns
    from agent.interrupt_compat import request_hard_interrupt
    stop_authority_turns(authority, in_process=True)
    for live in getattr(authority, 'sessions', {}).values():
        for lease in list(getattr(live, 'approval_task_leases', ())):
            lease.revoke()
    for effect in command_effects(authority):
        if not effect.cancelling():
            effect.cancel()
    for _, holder in [*physical_workers(authority), *side_agents(authority)]:
        if holder and holder[0] is not None:
            request_hard_interrupt(holder[0], 'Profile runtime is stopping')
    for cancel in getattr(authority, '_cron_cancellations', {}).values():
        cancel.set()


async def join_authority_work(authority, timeout):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        stop_authority_work(authority)
        if (not list(physical_workers(authority)) and not list(executing_sessions(authority))
                and not mutation_tasks(authority) and not side_agents(authority) and not command_effects(authority)):
            require_command_cleanup(authority)
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('Profile workers did not stop; ownership retained')
        await asyncio.sleep(.05)


def require_command_cleanup(authority, route=None):
    from hermes_state_runtime import RuntimeStoreError
    if command_unknowns(authority, route):
        raise RuntimeStoreError('unknown_execution', detail='Native command cleanup is unconfirmed; reconcile its process and effects before retrying.')


async def interrupt_session(authority, actor, ref, generation):
    from hermes_state_runtime import RuntimeStoreError
    authority.authorize(actor, ref, 'session:control')
    handle = authority._handle(ref)
    if handle.execution_generation != generation:
        raise RuntimeStoreError('stale_generation')
    live, agent = authority.sessions[ref.session_id], None
    # A later admission can finish while an earlier detached child is still
    # running. Stop revokes every task owned by this live conversation.
    effects = set()
    with live.event_stream.lock:
        authority.fence_route(live.route)
        for lease in list(getattr(live, 'approval_task_leases', ())):
            lease.revoke()
            effects.update(lease.effect_tasks())
    if handle.execution_state == 'running':
        # Latches are written under the lock adopt_agent takes on the executor thread, BEFORE the
        # agent is called: an adoption racing the call consumes them. The agent is called outside it.
        with live.event_stream.lock:
            adopted_generation, agent = authority.adopted.get(ref.session_id, (None, None))
            if adopted_generation != generation:
                agent = authority.agent(ref)
                if not authority._runs_own_agent(ref):
                    # Accepted for this exact claim before its turn adopted an agent: whatever agent
                    # the turn adopts for this generation must not run the work as if no Stop arrived.
                    authority.pending_stops[ref.session_id] = generation
            if agent is not None:
                authority.delivered_stops[ref.session_id] = generation
    # Main's Stop: hard-cancel (aborts compression too) and end the session's own background
    # delegations (spawned by this session only: a viewer must not kill other gateway work).
    from agent.interrupt_compat import request_hard_interrupt
    from tools.async_delegation import interrupt_for_session
    if agent is not None:
        request_hard_interrupt(agent)
    for parent in dict.fromkeys((ref.session_id, authority.physical_target(ref))):
        interrupt_for_session(session_key=live.route, parent_session_id=parent, reason='user_stop')
    from gateway.session_ancillary import start_owned_process_stop
    from tools.process_registry import process_registry
    with process_registry._lock:
        process_work = bool(process_registry._running) or any(p._scope_stop_pending for p in process_registry._finished.values())
    processes = None
    if process_work:
        try:
            processes = start_owned_process_stop(authority, actor, ref, source='session.interrupt')
        except RuntimeStoreError as exc:
            if exc.reason != 'unsupported_projection':  # managed workers own their process-local registry
                raise
    if effects:
        for effect in effects:
            if not effect.cancelling():
                effect.cancel()
        done, pending = await asyncio.wait(effects, timeout=15)
        if pending or any(not effect.cancelled() and effect.exception() is not None for effect in done):
            raise RuntimeStoreError('unknown_execution', detail='Native command cleanup is unconfirmed; reconcile its process and effects before retrying.')
    if processes is not None and (await asyncio.shield(processes)).get('unconfirmed'):
        raise RuntimeStoreError('unknown_execution', detail='Native Stop could not confirm process exit; reconcile its process and effects before retrying.')
    require_command_cleanup(authority, live.route)
    return authority._handle(ref)
