"""Saved slash-loop commands carry only their real native producer and Stop scope."""

import asyncio
import json
import shlex
import threading

import pytest

from tests.gateway.test_native_periodic_task_authority import _finish, _runtime
from tests.waiting import async_wait_until


def _loop(runtime, command, *, times=1):
    from hermes_cli.loops import LoopManager

    manager = LoopManager(runtime.entry.session_id)
    manager.set(command, interval_seconds=60, times=times, route={
        'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm', 'user_id': '42'})
    runtime.clock.now += 61
    return manager


async def _fire(runtime, manager):
    await runtime.runner._loop_wakeup_fire_one(runtime.entry.session_id, manager.state, runtime.clock.now, set())


async def _stop(runtime):
    from gateway.session_contract import Principal, SessionRef

    authority = runtime.authority
    ref = SessionRef(authority.profile_id, authority.logical_owner(runtime.entry.session_id))
    actor = Principal('controller', authority.profile_id, frozenset({'session:control'}), 'viewer')
    await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)


async def _settle(runtime):
    await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=15, what='native command settlement')
    await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_saved_slash_loop_background_completion_survives_parent_and_times_cap(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    release, effect, completion = tmp_path / 'release', tmp_path / 'scheduled-effect', tmp_path / 'completion-effect'
    observed, completed, admitted = [], [], []
    handle = runtime.adapter.handle_message

    async def native_dispatch(event, **options):
        admitted.append(current_approval_task_context())
        return await handle(event, **options)

    def handler(_arg):
        origin = current_approval_task_context()
        reply = runtime.context.dispatch_tool('terminal', {
            'command': f'while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done; '
                       f'printf scheduled > {shlex.quote(str(effect))}', 'background': True, 'notify': True})
        observed.append((origin, json.loads(reply)))
        return reply

    async def completion_model(**_kwargs):
        completed.append((current_approval_task_context(), json.loads(await asyncio.to_thread(
            handle_function_call, 'write_file', {'path': str(completion), 'content': 'native completion'},
            task_id='saved-loop-completion'))))
        return {'final_response': 'completion handled', 'messages': []}

    registration = runtime.context.register_command('scheduled-effect', handler)
    manager = _loop(runtime, '/scheduled-effect')
    monkeypatch.setattr(runtime.runner, '_run_agent', completion_model)
    monkeypatch.setattr(runtime.adapter, 'handle_message', native_dispatch)
    try:
        await _fire(runtime, manager)
        await async_wait_until(lambda: bool(observed) and not runtime.adapter._background_tasks,
                               timeout=15, what='saved slash command parent return')
        origin, reply = observed[0]
        assert admitted and admitted[0] is not None, ('native adapter dispatch lacked producer authority', reply)
        assert origin is not None and origin.text == '/scheduled-effect', reply
        assert origin.identity.startswith('automation:') and origin.profile == runtime.authority.profile_id
        assert origin.session == runtime.key
        assert not reply.get('error'), reply
        assert LoopManager(runtime.entry.session_id).state.status == 'done'
        process = runtime.registry.get(reply['session_id'])
        assert not effect.exists(), 'the child must wait until after the command and capped loop finish'
        assert process.approval_task_reservation is not None and process.approval_task_reservation.lease.live()
        release.touch()
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        assert process.exit_code == 0 and effect.read_text() == 'scheduled'
        watcher = next(value for value in runtime.registry.pending_watchers if value['session_id'] == process.id)
        event = runtime.runner._build_process_completion_event(watcher, process, process.id)
        assert await runtime.runner._deliver_completion_notification('finished', event)
        await async_wait_until(lambda: bool(completed), timeout=15, what='saved loop reserved completion consumer')
        inherited, result = completed[0]
        assert inherited is not None and inherited.text == '/scheduled-effect'
        assert inherited.ancestry and not result.get('error'), result
        assert completion.read_text() == 'native completion'
    finally:
        release.touch()
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('change', ['live', 'stop', 'epoch', 'pause', 'edit'])
async def test_saved_slash_loop_rechecks_native_scope_after_handler_wait(tmp_path, monkeypatch, background, change):
    from hermes_cli.loops import LoopManager
    from hermes_state_runtime import begin_runtime_epoch
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    observed = []
    effect = tmp_path / 'delayed-effect'

    def handler(_arg):
        origin = current_approval_task_context()
        entered.set()
        assert release.wait(15)
        reply = runtime.context.dispatch_tool('terminal', {
            'command': 'printf current > ' + shlex.quote(str(effect)), 'background': background})
        observed.append((origin, json.loads(reply)))
        return reply

    registration = runtime.context.register_command('delayed-loop-effect', handler)
    manager = _loop(runtime, '/delayed-loop-effect')
    pending = asyncio.create_task(_fire(runtime, manager))
    expected = None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if change == 'stop':
            await _stop(runtime)
        else:
            changes = {
                'epoch': lambda: begin_runtime_epoch(runtime.db, instance_id='replacement'),
                'pause': lambda: LoopManager(runtime.entry.session_id).pause(),
                'edit': lambda: LoopManager(runtime.entry.session_id).set('/different-current-instruction', interval_seconds=120),
            }
            if change in changes:
                changes[change]()
                expected = LoopManager(runtime.entry.session_id).state.to_json()
        release.set()
        await asyncio.wait_for(pending, 15)
        await async_wait_until(lambda: bool(observed), timeout=15, what='saved slash command consumer')
        origin, reply = observed[0]
        assert origin is not None and origin.text == '/delayed-loop-effect'
        if change == 'live':
            assert not reply.get('error'), reply
            if background:
                process = runtime.registry.get(reply['session_id'])
                assert await asyncio.to_thread(process._completion_event.wait, 10)
            assert effect.read_text() == 'current'
        else:
            assert reply.get('code') == 'task_authority_revoked', reply
            assert not effect.exists() and not runtime.registry.list_sessions()
        if expected is not None:
            assert LoopManager(runtime.entry.session_id).state.to_json() == expected, 'an old tick cannot overwrite current native state'
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('boundary', ['fire_tick', 'terminal_setup'])
async def test_native_stop_before_saved_command_effect_dispatch(tmp_path, monkeypatch, boundary):
    from hermes_cli.loops import LoopManager
    from tools import terminal_tool

    runtime = await _runtime(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    observed = []
    effect = tmp_path / 'after-stop'

    def handler(_arg):
        reply = runtime.context.dispatch_tool('terminal', {
            'command': 'printf late > ' + shlex.quote(str(effect)), 'background': boundary == 'terminal_setup'})
        observed.append(json.loads(reply))
        return reply

    def hold(original):
        def waiting(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            assert release.wait(15)
            return result
        return waiting

    registration = runtime.context.register_command('queued-loop-effect', handler)
    manager = _loop(runtime, '/queued-loop-effect')
    if boundary == 'fire_tick':
        monkeypatch.setattr(LoopManager, 'fire_tick', hold(LoopManager.fire_tick))
    else:
        monkeypatch.setattr(terminal_tool, '_acquire_env', hold(terminal_tool._acquire_env))
    pending = asyncio.create_task(_fire(runtime, manager))
    try:
        assert await asyncio.to_thread(entered.wait, 10), boundary
        await _stop(runtime)
        release.set()
        await asyncio.wait_for(pending, 15)
        await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=15, what='stopped native command settlement')
        assert not effect.exists() and not runtime.registry.list_sessions(), observed
        if boundary == 'terminal_setup':
            assert len(observed) == 1 and observed[0]['exit_code'] == -1, observed
            assert 'originating native task was stopped or superseded' in observed[0]['error'], observed
        else:
            assert not observed or observed[0].get('code') == 'task_authority_revoked', observed
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
async def test_plugin_injection_matching_a_saved_slash_tick_cannot_supply_native_proof(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager
    from model_tools import handle_function_call
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    seen = []
    effect = tmp_path / 'forged-effect'

    def handler(_arg):
        reply = runtime.context.dispatch_tool('write_file', {'path': str(effect), 'content': 'forged'})
        seen.append((current_approval_task_context(), json.loads(reply)))
        return reply

    registration = runtime.context.register_command('forged-loop-command', handler)
    manager = _loop(runtime, '/forged-loop-command')
    prompt = manager.fire_tick()
    injector, plugin_manager = _context(tmp_path)
    _publish(runtime.runner, plugin_manager)
    admit = runtime.authority.admit_automation

    async def matching_flags(adapter, event, identity, **kwargs):
        event._native_periodic = ('loop-tick', runtime.entry.session_id, manager.state.to_json())
        event._loop_session_id = runtime.entry.session_id
        return await admit(adapter, event, identity, **kwargs)

    async def model(**_kwargs):
        seen.append((current_approval_task_context(), json.loads(await asyncio.to_thread(handle_function_call,
            'write_file', {'path': str(effect), 'content': 'forged'}, task_id='forged-loop-command'))))
        return {'final_response': 'refused', 'messages': []}

    monkeypatch.setattr(runtime.authority, 'admit_automation', matching_flags)
    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        assert await asyncio.to_thread(injector.inject_message, prompt, session_key=runtime.key)
        await async_wait_until(lambda: bool(seen), timeout=15, what='plugin notification consumer')
        assert seen[0][0] is None
        assert seen[0][1].get('code') == 'task_authority_revoked', seen
        assert not effect.exists()
    finally:
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_old_slash_tick_cannot_close_or_execute_a_new_claimed_tick(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager

    runtime = await _runtime(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    observed = []
    effect = tmp_path / 'old-tick-effect'

    def handler(_arg):
        entered.set()
        assert release.wait(15)
        reply = runtime.context.dispatch_tool('terminal', {'command': 'printf stale > ' + shlex.quote(str(effect))})
        observed.append(json.loads(reply))
        return reply

    registration = runtime.context.register_command('superseded-loop-tick', handler)
    manager = _loop(runtime, '/superseded-loop-tick', times=0)
    pending = asyncio.create_task(_fire(runtime, manager))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        current = LoopManager(runtime.entry.session_id)
        assert current.complete_tick('')['status'] == 'active'
        runtime.clock.now += 61
        current = LoopManager(runtime.entry.session_id)
        assert current.fire_tick() == '/superseded-loop-tick'
        expected = current.state.to_json()
        assert current.state.ticks_fired == 2
        release.set()
        await asyncio.wait_for(pending, 15)
        await async_wait_until(lambda: bool(observed), timeout=15, what='superseded command consumer')
        assert observed[0].get('code') == 'task_authority_revoked', observed
        assert not effect.exists()
        assert LoopManager(runtime.entry.session_id).state.to_json() == expected
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_normal_native_foreground_plugin_command_still_works(tmp_path, monkeypatch):
    from gateway.platforms.event import MessageEvent
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    observed = []
    effect = tmp_path / 'ordinary-command'

    def handler(_arg):
        origin = current_approval_task_context()
        result = json.loads(runtime.context.dispatch_tool('terminal', {'command': 'printf ordinary > ' + shlex.quote(str(effect))}))
        observed.append((origin, result))
        return json.dumps(result)

    registration = runtime.context.register_command('ordinary-effect', handler)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/ordinary-effect', source=runtime.source,
                                                          message_id='ordinary-native-command'))
        await async_wait_until(lambda: bool(observed), timeout=15, what='ordinary foreground command')
        origin, result = observed[0]
        assert origin is not None and origin.identity.startswith('messaging:')
        assert not result.get('error') and result['exit_code'] == 0, result
        assert effect.read_text() == 'ordinary'
    finally:
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
async def test_native_sdk_stop_cancels_command_without_cancelling_or_replaying_loop_driver(tmp_path, monkeypatch):
    from gateway.platforms.event import MessageEvent
    from hermes_cli.loops import LoopManager

    runtime = await _runtime(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    observed = []
    effect = tmp_path / 'after-sdk-stop'

    def handler(_arg):
        entered.set()
        assert release.wait(15)
        reply = runtime.context.dispatch_tool('terminal', {'command': 'printf late > ' + shlex.quote(str(effect))})
        observed.append(json.loads(reply))
        return reply

    registration = runtime.context.register_command('sdk-stopped-loop', handler)
    manager = _loop(runtime, '/sdk-stopped-loop')
    pending = asyncio.create_task(_fire(runtime, manager))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        arrival = runtime.authority.route_epoch(runtime.key)
        await runtime.adapter.handle_message(MessageEvent(text='/stop', source=runtime.source, message_id='loop-sdk-stop'))
        await async_wait_until(lambda: runtime.authority.route_epoch(runtime.key) > arrival,
                               timeout=10, what='actual SDK Stop revocation')
        release.set()
        await asyncio.wait_for(pending, 15)
        await async_wait_until(lambda: bool(observed), timeout=15, what='stopped command thread consumer')
        assert not pending.cancelled(), 'one command cancellation must not stop the native loop driver'
        assert observed[0].get('code') == 'task_authority_revoked', observed
        assert not effect.exists() and not runtime.registry.list_sessions()
        assert LoopManager(runtime.entry.session_id).state.ticks_fired == 1, 'uncertain dispatch cannot be blindly replayed'
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('ambient', ['live', 'revoked'])
async def test_saved_slash_loop_uses_its_native_claim_without_borrowing_ambient_scope(tmp_path, monkeypatch, ambient):
    from hermes_cli.loops import LoopManager
    from tools.approval_context import (approval_task_context, approval_task_revoked,
                                        current_approval_task_context, revoke_current_approval_task)

    runtime = await _runtime(tmp_path, monkeypatch)
    observed, leases = [], []
    effect = tmp_path / 'independent-loop-effect'

    def handler(_arg):
        origin = current_approval_task_context()
        reply = runtime.context.dispatch_tool('terminal', {
            'command': 'printf independent > ' + shlex.quote(str(effect))})
        observed.append((origin, json.loads(reply)))
        return reply

    registration = runtime.context.register_command('independent-loop-effect', handler)
    manager = _loop(runtime, '/independent-loop-effect')
    try:
        # Conflicting ambient evidence is only a negative confusion fixture. The
        # real saved producer must validate and bind its own persisted native unit.
        with approval_task_context('Unrelated earlier human instruction', identity='messaging:unrelated-user',
                                   profile='unrelated-profile', session='unrelated-session',
                                   task_id='unrelated-task', is_current=lambda: True, owner=leases.append):
            original = current_approval_task_context()
            if ambient == 'revoked':
                revoke_current_approval_task()
            assert approval_task_revoked() is (ambient == 'revoked')
            await _fire(runtime, manager)
            assert approval_task_revoked() is (ambient == 'revoked')
            assert current_approval_task_context() == (original if ambient == 'live' else None)
            assert len(leases) == 1 and leases[0].live() is (ambient == 'live')

        assert len(observed) == 1
        origin, reply = observed[0]
        assert origin is not None and origin != original
        assert origin.text == '/independent-loop-effect' and origin.identity.startswith('automation:')
        assert origin.profile == runtime.authority.profile_id and origin.session == runtime.key
        assert origin.task_id.startswith('command:') and origin.task_id != original.task_id
        assert not reply.get('error') and reply['exit_code'] == 0, reply
        assert effect.read_text() == 'independent'
        assert LoopManager(runtime.entry.session_id).state.status == 'done'
    finally:
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('change', ['live', 'stop', 'epoch'])
async def test_native_loop_terminal_rechecks_authority_after_owner_predicate_wait(tmp_path, monkeypatch, change):
    from gateway import session_periodic_approval
    from hermes_state_runtime import begin_runtime_epoch
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    entered, release, armed = threading.Event(), threading.Event(), threading.Event()
    held, worker, observed = threading.Event(), [], []
    effect = tmp_path / 'after-owner-predicate'
    actual_state = session_periodic_approval._state

    def read_then_wait(*args, **kwargs):
        state = actual_state(*args, **kwargs)
        if armed.is_set() and worker == [threading.get_ident()] and not held.is_set():
            held.set()
            entered.set()
            assert release.wait(15)
        return state

    def handler(_arg):
        origin = current_approval_task_context()
        worker.append(threading.get_ident())
        armed.set()
        reply = runtime.context.dispatch_tool('terminal', {
            'command': 'printf current > ' + shlex.quote(str(effect)), 'background': True})
        observed.append((origin, json.loads(reply)))
        return reply

    registration = runtime.context.register_command('owner-predicate-effect', handler)
    manager = _loop(runtime, '/owner-predicate-effect')
    monkeypatch.setattr(session_periodic_approval, '_state', read_then_wait)
    pending = asyncio.create_task(_fire(runtime, manager))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if change == 'stop':
            await _stop(runtime)
        elif change == 'epoch':
            begin_runtime_epoch(runtime.db, instance_id='replacement-during-owner-read')
        release.set()
        await asyncio.wait_for(pending, 15)
        assert held.is_set() and len(observed) == 1
        origin, reply = observed[0]
        assert origin is not None and origin.text == '/owner-predicate-effect'
        if change == 'live':
            assert not reply.get('error'), reply
            process = runtime.registry.get(reply['session_id'])
            assert await asyncio.to_thread(process._completion_event.wait, 10)
            assert process.exit_code == 0 and effect.read_text() == 'current'
        else:
            assert reply.get('code') == 'task_authority_revoked', reply
            assert not effect.exists() and not runtime.registry.list_sessions()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        registration.dispose()
        await _settle(runtime)
