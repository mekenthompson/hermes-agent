"""Actual periodic admission/bootstrap/control frames retain the native managed worker boundary."""
import asyncio
from contextlib import nullcontext
import json
import os
from types import SimpleNamespace

import pytest

from tests.gateway.test_managed_worker_authority_epoch import _worker_controls


async def _runtime(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_state import AsyncSessionDB, SessionDB
    from tools import process_registry, terminal_tool
    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions'))
    db = SessionDB(home / 'state.db')
    runner.session_store._db = db
    runner._session_db = AsyncSessionDB(db)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='owner', db=db)
    monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
    config = terminal_tool._get_env_config
    monkeypatch.setattr(terminal_tool, '_get_env_config', lambda: {**config(), 'env_type': 'local', 'cwd': str(tmp_path)})
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    return runner, authority, registry


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['stop', 'changed', 'epoch'])
async def test_managed_heartbeat_bootstrap_and_live_frame_own_actual_terminal(tmp_path, monkeypatch, replacement):
    from agent.managed_worker import validate_bootstrap
    from gateway import session_managed_worker
    from gateway.run_heartbeat_acceptance import admit_heartbeat
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from hermes_cli import goals, heartbeat
    from hermes_state_runtime import begin_runtime_epoch
    from model_tools import handle_function_call
    from tools.approval_context import approval_task_context

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, '_get_session_db', lambda: authority.db)
    actor = Principal('human', authority.profile_id, frozenset({'session:create', 'session:control'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='managed-heartbeat', source='cli', model='fixture',
        cwd=str(tmp_path), toolsets=[], ignore_user_config=True))
    live = authority.sessions[ref.session_id]
    entry = runner.session_store.lookup_by_session_key(live.route)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(heartbeat, 'time', SimpleNamespace(time=lambda: clock.now))
    heartbeat.HeartbeatManager(entry.session_id).set('Run the configured periodic child', 60)
    clock.now += 61
    await admit_heartbeat(runner, runner._adapter_for_source(live.source), live.source, entry.session_id, live.route)
    row, _head = await authority._claim_next(ref, live)
    policy = session_managed_worker.managed_policy(authority, ref)
    scope = {'profile_id': authority.profile_id, 'session_id': ref.session_id, 'execution_id': 'managed-periodic',
             'generation': row['generation'], 'pid': os.getpid(), 'birth': 0, 'secret': 'fixture-pipe', 'epoch': authority.epoch}
    frame = validate_bootstrap(json.loads(json.dumps(session_managed_worker._bootstrap(authority, ref, row, policy, scope))))
    task = frame.get('approval_task_v1')
    before, after = tmp_path / 'current-child', tmp_path / 'stale-child'
    try:
        with _worker_controls(monkeypatch, authority, ref, row) as controls:
            def write(path):
                # This is the worker's actual optional-bootstrap binding, with its real control consumer.
                bound = (approval_task_context(task['text'], identity=task['identity'], profile=task['profile'],
                    session=task['session'], task_id=task['admission'], run_id=str(task['generation']),
                    is_current=controls.task_current) if task else nullcontext())
                with bound:
                    return json.loads(handle_function_call('terminal',
                        {'command': f'printf periodic > {path}', 'background': True}, task_id='managed-periodic'))

            current = await asyncio.to_thread(write, before)
            assert not current.get('error'), (frame, current)
            assert task['text'] == 'Run the configured periodic child'
            assert await asyncio.to_thread(registry.get(current['session_id'])._completion_event.wait, 10)
            assert before.read_text() == 'periodic'
            if replacement == 'epoch':
                begin_runtime_epoch(authority.db, instance_id='successor')
            elif replacement == 'changed':
                heartbeat.HeartbeatManager(entry.session_id).set('Different work', 60)
            else:
                await authority.interrupt(actor, ref, row['generation'])
            stale = await asyncio.to_thread(write, after)
            assert stale.get('code') == 'task_authority_revoked', stale
            assert not after.exists()
            assert not await asyncio.to_thread(controls.task_current)
    finally:
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')


@pytest.mark.asyncio
async def test_managed_plugin_notification_stays_unverified_with_matching_heartbeat_flags(tmp_path, monkeypatch):
    from agent.managed_worker import validate_bootstrap
    from gateway import session_managed_worker
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from hermes_cli import goals, heartbeat
    from model_tools import handle_function_call
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tools.approval_context import approval_task_context

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, '_get_session_db', lambda: authority.db)
    actor = Principal('human', authority.profile_id, frozenset({'session:create'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='managed-plugin', source='cli', model='fixture',
        cwd=str(tmp_path), toolsets=[], ignore_user_config=True))
    live = authority.sessions[ref.session_id]
    entry = runner.session_store.lookup_by_session_key(live.route)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(heartbeat, 'time', SimpleNamespace(time=lambda: clock.now))
    manager = heartbeat.HeartbeatManager(entry.session_id)
    manager.set('Configured task, not plugin consent', 60)
    clock.now += 61
    prompt = manager.due_prompt()
    identity = json.dumps(['heartbeat', entry.session_id, manager.state.created_at, manager.state.fire_count], separators=(',', ':'))
    injector, plugin_manager = _context(tmp_path / 'home')
    _publish(runner, plugin_manager)
    admit = authority.admit_automation

    async def forged(adapter, event, original_identity, **kwargs):
        event._heartbeat_session_id = entry.session_id
        event._native_periodic = ('heartbeat', entry.session_id, manager.state.to_json())
        event.message_id = identity
        return await admit(adapter, event, identity, **kwargs)

    monkeypatch.setattr(authority, 'admit_automation', forged)
    assert await asyncio.to_thread(injector.inject_message, prompt, session_key=live.route)
    from hermes_state_runtime import list_session_admissions
    from tests.waiting import async_wait_until
    await async_wait_until(lambda: bool(list_session_admissions(authority.db, session_id=ref.session_id)),
                          timeout=15, what='managed plugin native admission')
    row, _head = await authority._claim_next(ref, live)
    policy = session_managed_worker.managed_policy(authority, ref)
    scope = {'profile_id': authority.profile_id, 'session_id': ref.session_id, 'execution_id': 'managed-plugin',
             'generation': row['generation'], 'pid': os.getpid(), 'birth': 0, 'secret': 'fixture-pipe', 'epoch': authority.epoch}
    frame = validate_bootstrap(json.loads(json.dumps(session_managed_worker._bootstrap(authority, ref, row, policy, scope))))
    task = frame.get('approval_task_v1')
    effect = tmp_path / 'forged-managed-effect'
    try:
        with _worker_controls(monkeypatch, authority, ref, row) as controls:
            def write():
                bound = (approval_task_context(task['text'], identity=task['identity'], profile=task['profile'],
                    session=task['session'], task_id=task['admission'], run_id=str(task['generation']),
                    is_current=controls.task_current) if task else nullcontext())
                with bound:
                    return json.loads(handle_function_call('write_file', {'path': str(effect), 'content': 'forged'}, task_id='managed-plugin'))
            result = await asyncio.to_thread(write)
            assert result.get('code') == 'task_authority_revoked', (frame, result)
            assert not effect.exists()
            assert not await asyncio.to_thread(controls.task_current)
    finally:
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')


@pytest.mark.asyncio
@pytest.mark.parametrize('stopped', [False, True])
async def test_managed_completion_uses_real_native_command_reservation(tmp_path, monkeypatch, stopped):
    from agent.managed_worker import validate_bootstrap
    from gateway import session_managed_worker
    from gateway.session_commands import execute_command
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session import SessionContext
    from hermes_cli import goals
    from model_tools import handle_function_call
    from tools.approval_context import approval_task_context

    runner, authority, registry = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, '_get_session_db', lambda: authority.db)
    actor = Principal('human', authority.profile_id, frozenset({'session:create', 'session:control', 'session:read'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='managed-completion', source='cli', model='fixture',
        cwd=str(tmp_path), toolsets=[], ignore_user_config=True))
    await authority.attach(actor, ref)
    live = authority.sessions[ref.session_id]
    entry = runner.session_store.lookup_by_session_key(live.route)
    release, effect = tmp_path / 'release', tmp_path / 'managed-completion'
    children, watchers = [], []
    monkeypatch.setattr(runner, 'arm_process_watcher', lambda watcher: watchers.append(watcher) or False)

    async def handler(_event):
        with runner._session_env_scope(SessionContext(source=_event.source,
                connected_platforms=list(runner.adapters), home_channels={}, session_key=live.route, session_id=entry.session_id)):
            children.append(json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
                {'command': f'while [ ! -e {release} ]; do sleep 0.05; done; printf finished',
                 'background': True, 'notify': True}, task_id=entry.session_id)))
        return 'child started'

    monkeypatch.setattr(runner, '_command_handler_table', lambda _commands: {'review': handler})
    try:
        await execute_command(SimpleNamespace(authority=authority, actor=actor, subscriptions={}),
                              ref, {'name': 'review', 'arg': 'Complete the authorized native work'}, dispatch=True)
        assert children and not children[0].get('error'), children
        process = registry.get(children[0]['session_id'])
        assert process.approval_task_reservation is not None
        if stopped:
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        release.touch()
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        watcher = next(value for value in watchers if value['session_id'] == process.id)
        event = runner._build_process_completion_event(watcher, process, process.id)
        assert await runner._deliver_completion_notification('finished', event)
        row, _head = await authority._claim_next(ref, live)
        policy = session_managed_worker.managed_policy(authority, ref)
        scope = {'profile_id': authority.profile_id, 'session_id': ref.session_id, 'execution_id': 'managed-completion',
                 'generation': row['generation'], 'pid': os.getpid(), 'birth': 0, 'secret': 'fixture-pipe', 'epoch': authority.epoch}
        frame = validate_bootstrap(json.loads(json.dumps(session_managed_worker._bootstrap(authority, ref, row, policy, scope))))
        task = frame['approval_task_v1']
        with _worker_controls(monkeypatch, authority, ref, row) as controls:
            def write():
                with approval_task_context(task['text'], identity=task['identity'], profile=task['profile'],
                    session=task['session'], task_id=task['admission'], run_id=str(task['generation']), is_current=controls.task_current):
                    return json.loads(handle_function_call('write_file', {'path': str(effect), 'content': 'native completion'}, task_id='managed-completion'))
            result = await asyncio.to_thread(write)
            if stopped:
                assert result.get('code') == 'task_authority_revoked', result
                assert not effect.exists()
            else:
                assert not result.get('error'), (task, result)
                assert task['text'] == '/review Complete the authorized native work'
                assert effect.read_text() == 'native completion'
    finally:
        release.touch()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
