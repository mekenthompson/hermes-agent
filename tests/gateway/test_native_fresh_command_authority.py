"""First SDK commands establish native ownership before effects and retain Stop fences."""

import asyncio
import json
import shlex
import threading

import pytest

from gateway.platforms.event import MessageEvent
from tests.gateway.test_native_periodic_task_authority import _finish
from tests.gateway.test_plugin_command_task_authority import _discord_runtime
from tests.waiting import async_wait_until


async def _fresh_runtime(tmp_path, monkeypatch, *, user_id='42'):
    runtime = await _discord_runtime(tmp_path, monkeypatch)
    # The fixture's other conversation initializes the real host, never this route.
    runtime.source = runtime.adapter.build_source(chat_id='first-command-room', chat_type='dm', user_id=user_id)
    runtime.key = runtime.runner.session_store._generate_session_key(runtime.source)
    assert runtime.runner.session_store.lookup_by_session_key(runtime.key) is None
    return runtime


async def _settle(runtime):
    from gateway.session_runtime_workers import mutation_tasks

    await async_wait_until(lambda: not runtime.adapter._background_tasks and not mutation_tasks(runtime.authority),
                           timeout=15, what='first command and native route settlement')
    await _finish(runtime)


async def _sdk_stop(runtime):
    arrival = runtime.authority.route_epoch(runtime.key)
    await runtime.adapter.handle_message(MessageEvent(text='/stop', source=runtime.source, message_id='first-stop'))
    await async_wait_until(lambda: runtime.authority.route_epoch(runtime.key) > arrival,
                           timeout=10, what='native SDK Stop epoch')
    return arrival


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('existing_entry', [False, True])
async def test_first_plugin_command_reserves_native_owner_before_background_effect(tmp_path, monkeypatch, existing_entry):
    from tools.approval_context import current_approval_task_context

    runtime = await _fresh_runtime(tmp_path, monkeypatch)
    previous = None
    if existing_entry:
        previous = await asyncio.to_thread(runtime.runner.session_store.get_or_create_session, runtime.source)
        assert runtime.authority.logical_owner(previous.session_id) not in runtime.authority.sessions
    observed = []
    effect = tmp_path / 'first-background-effect'

    def handler(_arg):
        origin = current_approval_task_context()
        reply = runtime.context.dispatch_tool('terminal', {
            'command': 'printf first > ' + shlex.quote(str(effect)), 'background': True})
        observed.append((origin, json.loads(reply)))
        return reply

    registration = runtime.context.register_command('first-background-effect', handler)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/first-background-effect', source=runtime.source,
                                                          message_id='first-plugin-input'))
        await async_wait_until(lambda: bool(observed), timeout=15, what='first background command')
        origin, reply = observed[0]
        assert origin is not None and origin.text == '/first-background-effect', reply
        assert origin.identity.startswith('messaging:') and origin.profile == runtime.authority.profile_id
        assert origin.session == runtime.key and origin.task_id.startswith('command:')
        assert not reply.get('error'), reply
        process = runtime.registry.get(reply['session_id'])
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        assert process.exit_code == 0 and effect.read_text() == 'first'
        entry = runtime.runner.session_store.lookup_by_session_key(runtime.key)
        assert runtime.authority.logical_owner(entry.session_id) in runtime.authority.sessions
        if previous is not None:
            assert entry.session_id == previous.session_id
    finally:
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('tool', ['write_file', 'terminal'])
@pytest.mark.parametrize('change', ['live', 'stop', 'epoch'])
async def test_first_plugin_command_rechecks_native_control_after_handler_wait(tmp_path, monkeypatch, tool, change):
    from hermes_state_runtime import begin_runtime_epoch
    from tools.approval_context import current_approval_task_context

    runtime = await _fresh_runtime(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    observed = []
    effect = tmp_path / 'delayed-first-effect'

    def handler(_arg):
        origin = current_approval_task_context()
        entered.set()
        assert release.wait(15)
        args = ({'path': str(effect), 'content': 'first'} if tool == 'write_file' else {
            'command': 'printf first > ' + shlex.quote(str(effect)), 'background': True})
        reply = runtime.context.dispatch_tool(tool, args)
        observed.append((origin, json.loads(reply)))
        return reply

    registration = runtime.context.register_command('delayed-first-effect', handler)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/delayed-first-effect', source=runtime.source,
                                                          message_id='first-delayed-input'))
        assert await asyncio.to_thread(entered.wait, 10)
        if change == 'stop':
            await _sdk_stop(runtime)
        elif change == 'epoch':
            begin_runtime_epoch(runtime.db, instance_id='replacement-first-command')
        release.set()
        await async_wait_until(lambda: bool(observed), timeout=15, what='delayed first command consumer')
        origin, reply = observed[0]
        assert origin is not None and origin.text == '/delayed-first-effect'
        assert origin.profile == runtime.authority.profile_id and origin.session == runtime.key
        if change == 'live':
            assert not reply.get('error'), reply
            if tool == 'terminal':
                process = runtime.registry.get(reply['session_id'])
                assert await asyncio.to_thread(process._completion_event.wait, 10)
                assert process.exit_code == 0
            assert effect.read_text() == 'first'
        else:
            assert reply.get('code') == 'task_authority_revoked', reply
            assert not effect.exists() and not runtime.registry.list_sessions()
    finally:
        release.set()
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
async def test_stop_during_first_native_route_reservation_cannot_revive_command_authority(tmp_path, monkeypatch):
    from gateway.session_runtime_workers import mutation_tasks
    from tools.approval_context import current_approval_task_context

    runtime = await _fresh_runtime(tmp_path, monkeypatch)
    entered, release, held = threading.Event(), threading.Event(), threading.Event()
    observed = []
    effect = tmp_path / 'after-first-reservation-stop'
    actual_reserve = runtime.authority._reserve_route

    def reserve_then_wait(source):
        reserved = actual_reserve(source)
        if source.chat_id == runtime.source.chat_id and not held.is_set():
            held.set()
            entered.set()
            assert release.wait(15)
        return reserved

    def handler(_arg):
        origin = current_approval_task_context()
        reply = runtime.context.dispatch_tool('write_file', {'path': str(effect), 'content': 'late'})
        observed.append((origin, json.loads(reply)))
        return reply

    registration = runtime.context.register_command('reserved-first-effect', handler)
    monkeypatch.setattr(runtime.authority, '_reserve_route', reserve_then_wait)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/reserved-first-effect', source=runtime.source,
                                                          message_id='first-reservation-input'))
        assert await asyncio.to_thread(entered.wait, 10)
        entry = runtime.runner.session_store.lookup_by_session_key(runtime.key)
        assert entry is not None and runtime.authority.logical_owner(entry.session_id) not in runtime.authority.sessions
        arrival = await _sdk_stop(runtime)
        release.set()
        await async_wait_until(lambda: not runtime.adapter._background_tasks and not mutation_tasks(runtime.authority),
                               timeout=15, what='stopped native reservation publication')
        assert held.is_set() and runtime.authority.route_epoch(runtime.key) > arrival
        assert not effect.exists() and not runtime.registry.list_sessions()
        assert not observed or observed[0][1].get('code') == 'task_authority_revoked', observed
        live = runtime.authority.sessions.get(runtime.authority.logical_owner(entry.session_id))
        assert live is not None
        assert all(not lease.live() for lease in getattr(live, 'approval_task_leases', ()))
    finally:
        release.set()
        registration.dispose()
        await _settle(runtime)


@pytest.mark.asyncio
async def test_first_plugin_command_does_not_reserve_or_execute_for_disallowed_user(tmp_path, monkeypatch):
    runtime = await _fresh_runtime(tmp_path, monkeypatch, user_id='unauthorized-user')
    observed = []
    effect = tmp_path / 'unauthorized-first-effect'

    def handler(_arg):
        observed.append(True)
        return runtime.context.dispatch_tool('write_file', {'path': str(effect), 'content': 'unauthorized'})

    registration = runtime.context.register_command('unauthorized-first-effect', handler)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/unauthorized-first-effect', source=runtime.source,
                                                          message_id='unauthorized-first-input'))
        await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=15, what='unauthorized first command refusal')
        assert not observed and not effect.exists()
        assert runtime.runner.session_store.lookup_by_session_key(runtime.key) is None
        assert not runtime.registry.list_sessions()
    finally:
        registration.dispose()
        await _settle(runtime)
