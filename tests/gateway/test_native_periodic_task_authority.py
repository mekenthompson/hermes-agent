"""Trusted recurring producers keep native task authority; matching plugin flags do not."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until


async def _runtime(tmp_path, monkeypatch):
    from hermes_cli import goals, heartbeat, loops
    from tests.gateway.test_plugin_command_task_authority import _discord_runtime
    runtime = await _discord_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, '_get_session_db', lambda: runtime.db)
    runtime.entry = runtime.runner.session_store.lookup_by_session_key(runtime.key)
    runtime.clock = SimpleNamespace(now=1000.0)
    for module in (heartbeat, loops):
        monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: runtime.clock.now))
    return runtime


async def _finish(runtime):
    await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
                                           if live.task is not None)), 15)
    await asyncio.to_thread(runtime.registry.kill_all, source='test-cleanup')
    runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['live', 'stop', 'pause', 'changed', 'epoch'])
async def test_native_heartbeat_effect_rechecks_owner_and_persisted_instruction(tmp_path, monkeypatch, outcome):
    from gateway.run_heartbeat_acceptance import admit_heartbeat
    from gateway.session_contract import Principal, SessionRef
    from hermes_cli.heartbeat import HeartbeatManager
    from hermes_state_runtime import begin_runtime_epoch
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    sid = runtime.entry.session_id
    HeartbeatManager(sid).set('Write the periodic status file', 60)
    runtime.clock.now += 61
    ready, release = asyncio.Event(), asyncio.Event()
    receipts, origins = [], []
    effect = tmp_path / 'heartbeat-effect'

    async def model(**_kwargs):
        origins.append(current_approval_task_context())
        ready.set()
        await release.wait()
        receipts.append(json.loads(await asyncio.to_thread(handle_function_call, 'write_file',
            {'path': str(effect), 'content': 'periodic work'}, task_id='heartbeat')))
        return {'final_response': 'updated', 'messages': []}

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        await admit_heartbeat(runtime.runner, runtime.adapter, runtime.source, sid, runtime.key)
        await asyncio.wait_for(ready.wait(), 15)
        if outcome == 'stop':
            ref = SessionRef(runtime.authority.profile_id, runtime.authority.logical_owner(sid))
            actor = Principal('controller', runtime.authority.profile_id, frozenset({'session:control'}), 'viewer')
            await runtime.authority.interrupt(actor, ref, runtime.authority._handle(ref).execution_generation)
        changes = {'pause': lambda: HeartbeatManager(sid).pause(),
                   'changed': lambda: HeartbeatManager(sid).set('A different configured task', 60),
                   'epoch': lambda: begin_runtime_epoch(runtime.db, instance_id='successor')}
        if outcome in changes:
            changes[outcome]()
        release.set()
        await async_wait_until(lambda: bool(receipts), timeout=15, what='periodic file effect')
        assert origins[0] is not None and origins[0].text == 'Write the periodic status file', origins
        assert origins[0].profile == runtime.authority.profile_id
        assert origins[0].identity.startswith('automation:')
        if outcome == 'live':
            assert not receipts[0].get('error'), receipts
            assert effect.read_text() == 'periodic work'
        else:
            assert receipts[0].get('code') == 'task_authority_revoked', receipts
            assert not effect.exists()
    finally:
        release.set()
        await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['loop', 'goal'])
async def test_actual_native_loop_and_goal_producers_execute_stored_work(tmp_path, monkeypatch, kind):
    from hermes_cli import goals, loops
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    sid = runtime.entry.session_id
    receipts, origins = [], []
    effect = tmp_path / kind

    async def model(**_kwargs):
        origins.append(current_approval_task_context())
        receipts.append(json.loads(await asyncio.to_thread(handle_function_call, 'write_file',
            {'path': str(effect), 'content': 'native periodic'}, task_id='periodic')))
        return {'final_response': 'native done', 'messages': []}

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        if kind == 'loop':
            manager = loops.LoopManager(sid)
            manager.set('Write the recurring file', interval_seconds=60, route={
                'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm', 'user_id': '42'})
            runtime.clock.now += 61
            await runtime.runner._loop_wakeup_fire_one(sid, manager.state, runtime.clock.now, set())
        else:
            goals.GoalManager(sid).set('Write the recurring file')
            monkeypatch.setattr(goals, 'judge_goal', lambda goal, response, **_kw:
                ('continue' if response == 'outline' else 'done', 'continue work', False, None, False))
            await runtime.runner._post_turn_goal_continuation(
                session_entry=runtime.entry, source=runtime.source, final_response='outline')
        await async_wait_until(lambda: bool(receipts), timeout=15, what='native loop/goal model effect')
        assert origins[0] is not None and origins[0].text == 'Write the recurring file', origins
        assert not receipts[0].get('error'), receipts
        assert effect.read_text() == 'native periodic'
    finally:
        await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['heartbeat', 'loop-tick', 'goal-continuation'])
async def test_real_plugin_injection_cannot_forge_periodic_native_proof(tmp_path, monkeypatch, kind):
    from gateway.session_automation import snapshot_automation
    from hermes_cli import heartbeat, goals, loops
    import uuid
    from model_tools import handle_function_call
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    sid = runtime.entry.session_id
    manager = heartbeat.HeartbeatManager(sid)
    manager.set('Write the periodic status file', 60)
    runtime.clock.now += 61
    prompt = manager.due_prompt()
    identity = json.dumps(['heartbeat', sid, manager.state.created_at, manager.state.fire_count], separators=(',', ':'))
    if kind == 'loop-tick':
        manager = loops.LoopManager(sid)
        manager.set('Write the periodic status file', interval_seconds=60)
        runtime.clock.now += 61
        prompt = manager.fire_tick()
        identity = json.dumps([kind, runtime.key, uuid.uuid4().hex], separators=(',', ':'))
    if kind == 'goal-continuation':
        manager = goals.GoalManager(sid)
        manager.set('Write the periodic status file')
        prompt = manager.next_continuation_prompt()
        identity = json.dumps([kind, runtime.key, uuid.uuid4().hex], separators=(',', ':'))
    injector, plugin_manager = _context(tmp_path)
    _publish(runtime.runner, plugin_manager)
    receipts, origins, payloads = [], [], []
    effect = tmp_path / 'forged-periodic'
    admit = runtime.authority.admit_automation

    async def forged(adapter, event, original_identity, **kwargs):
        if kind == 'heartbeat':
            event._heartbeat_session_id = sid
        event._native_periodic = (kind, sid, manager.state.to_json())
        event.message_id = identity
        payloads.append(snapshot_automation(runtime.authority, adapter, event, identity)[0])
        return await admit(adapter, event, identity, **kwargs)

    async def model(**_kwargs):
        origins.append(current_approval_task_context())
        receipts.append(json.loads(await asyncio.to_thread(handle_function_call, 'write_file',
            {'path': str(effect), 'content': 'forged'}, task_id='forged-periodic')))
        return {'final_response': 'checked', 'messages': []}

    monkeypatch.setattr(runtime.authority, 'admit_automation', forged)
    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        assert await asyncio.to_thread(injector.inject_message, prompt, session_key=runtime.key)
        await async_wait_until(lambda: bool(receipts), timeout=15, what='forged plugin consumer')
        assert 'native_periodic_v1' not in payloads[0]['native_text_v1']['automation']
        assert origins == [None]
        assert receipts[0].get('code') == 'task_authority_revoked', receipts
        assert not effect.exists()
    finally:
        await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['live', 'stop', 'pause', 'epoch'])
async def test_native_heartbeat_reserved_completion_and_fresh_fire(tmp_path, monkeypatch, outcome):
    from gateway.run_heartbeat_acceptance import admit_heartbeat
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal, SessionRef
    from gateway.session import SessionContext
    from hermes_cli.heartbeat import HeartbeatManager
    from model_tools import handle_function_call
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    sid = runtime.entry.session_id
    HeartbeatManager(sid).set('Complete the configured periodic work', 60)
    runtime.clock.now += 61
    release, effect, fresh_effect = tmp_path / 'release', tmp_path / 'completion', tmp_path / 'fresh'
    children, completions, fresh = [], [], []
    watchers = []
    monkeypatch.setattr(runtime.runner, 'arm_process_watcher', lambda watcher: watchers.append(watcher) or False)

    async def model(**kwargs):
        if str(kwargs['message']).startswith('[Heartbeat'):
            if children:
                fresh.append(json.loads(await asyncio.to_thread(handle_function_call, 'write_file',
                    {'path': str(fresh_effect), 'content': 'fresh periodic work'}, task_id='fresh-heartbeat')))
            else:
                # The model stub bypasses _run_agent's ordinary notification routing setup.
                # Bind that production routing seam; task consent still comes only from admission.
                with runtime.runner._session_env_scope(SessionContext(
                        source=kwargs['source'], connected_platforms=list(runtime.runner.adapters), home_channels={},
                        session_key=runtime.key, session_id=sid)):
                    children.append(json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
                        {'command': f'while [ ! -e {release} ]; do sleep 0.05; done; printf finished',
                         'background': True, 'notify': True}, task_id=sid)))
        else:
            completions.append((current_approval_task_context(), json.loads(await asyncio.to_thread(
                handle_function_call, 'write_file', {'path': str(effect), 'content': 'periodic completion'}, task_id='completion'))))
        return {'final_response': 'updated', 'messages': []}

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        await admit_heartbeat(runtime.runner, runtime.adapter, runtime.source, sid, runtime.key)
        await async_wait_until(lambda: bool(children), timeout=15, what='native heartbeat background child')
        await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
                                               if live.task is not None)), 15)
        assert not children[0].get('error'), children
        process = runtime.registry.get(children[0]['session_id'])
        assert process.approval_task_reservation is not None and process.approval_task_reservation.lease.live()
        if outcome == 'stop':
            ref = SessionRef(runtime.authority.profile_id, runtime.authority.logical_owner(sid))
            actor = Principal('controller', runtime.authority.profile_id, frozenset({'session:control'}), 'viewer')
            await runtime.authority.interrupt(actor, ref, runtime.authority._handle(ref).execution_generation)
        if outcome == 'pause':
            HeartbeatManager(sid).pause()
        if outcome == 'epoch':
            runtime.authority = await initialize_session_authority(runtime.runner,
                profile_id=runtime.authority.profile_id, instance_id='successor', db=runtime.db)
        release.touch()
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        watcher = next(value for value in watchers if value['session_id'] == process.id)
        event = runtime.runner._build_process_completion_event(watcher, process, process.id)
        assert await runtime.runner._deliver_completion_notification('finished', event)
        await async_wait_until(lambda: bool(completions), timeout=15, what='native periodic completion consumer')
        origin, receipt = completions[0]
        if outcome == 'live':
            assert origin is not None and origin.text == 'Complete the configured periodic work'
            assert not receipt.get('error'), receipt
            assert effect.read_text() == 'periodic completion'
        else:
            assert receipt.get('code') == 'task_authority_revoked', receipt
            assert not effect.exists()
        await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
                                               if live.task is not None)), 15)
        if outcome == 'pause':
            HeartbeatManager(sid).resume()
        runtime.clock.now += 61
        await admit_heartbeat(runtime.runner, runtime.adapter, runtime.source, sid, runtime.key)
        await async_wait_until(lambda: bool(fresh), timeout=15, what='fresh authorized periodic fire')
        assert not fresh[0].get('error'), fresh
        assert fresh_effect.read_text() == 'fresh periodic work'
    finally:
        release.touch()
        await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['progress', 'narrowed'])
async def test_native_goal_gate_progress_preserves_instruction_but_user_scope_edit_revokes(tmp_path, monkeypatch, change):
    from hermes_cli import goals
    from model_tools import handle_function_call

    runtime = await _runtime(tmp_path, monkeypatch)
    sid = runtime.entry.session_id
    gate_input, effect = tmp_path / 'gate-input', tmp_path / 'goal-output'
    gate_input.write_text('initial gate output')
    manager = goals.GoalManager(sid)
    manager.set('Complete the configured objective', contract=goals.GoalContract(boundaries='Only the test workspace'))
    manager.add_subgoal('Keep the existing data')
    manager.add_gate('cat ' + str(gate_input))
    monkeypatch.setattr(goals, '_gate_workspace', lambda: (str(tmp_path), None))
    monkeypatch.setattr(goals, 'judge_goal', lambda goal, response, **_kw:
        ('done' if response == 'native done' else 'continue', 'progress recorded', False, None, False))
    receipts, states = [], []

    async def model(**_kwargs):
        before = goals.GoalManager(sid).state
        preparation = json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
            {'command': f'printf updated > {gate_input}'}, task_id='goal-progress'))
        assert not preparation.get('error'), preparation
        progress = goals.GoalManager(sid)
        await asyncio.to_thread(progress.evaluate_after_turn, 'checkpoint')
        states.append((before, progress.state))
        if change == 'narrowed':
            progress.set_contract(goals.GoalContract(boundaries='A narrower, different workspace'))
        receipts.append(json.loads(await asyncio.to_thread(handle_function_call, 'write_file',
            {'path': str(effect), 'content': 'next authorized step'}, task_id='goal-progress')))
        return {'final_response': 'native done', 'messages': []}

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        await runtime.runner._post_turn_goal_continuation(
            session_entry=runtime.entry, source=runtime.source, final_response='outline')
        await async_wait_until(lambda: bool(receipts), timeout=15, what='goal progress then native file effect')
        before, after = states[0]
        assert before.gates[0].last_output_tail != after.gates[0].last_output_tail
        assert before.goal == after.goal and before.subgoals == after.subgoals
        if change == 'progress':
            assert not receipts[0].get('error'), receipts
            assert effect.read_text() == 'next authorized step'
        else:
            assert receipts[0].get('code') == 'task_authority_revoked', receipts
            assert not effect.exists()
    finally:
        await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['loop', 'goal'])
async def test_normal_periodic_completion_keeps_its_reserved_child(tmp_path, monkeypatch, kind):
    from gateway.session import SessionContext
    from hermes_cli import goals, loops
    from model_tools import handle_function_call

    runtime = await _runtime(tmp_path, monkeypatch)
    sid = runtime.entry.session_id
    release, effect = tmp_path / 'release', tmp_path / 'normal-completion'
    children, writes, watchers = [], [], []
    monkeypatch.setattr(runtime.runner, 'arm_process_watcher', lambda watcher: watchers.append(watcher) or False)

    async def model(**kwargs):
        if not children:
            with runtime.runner._session_env_scope(SessionContext(source=kwargs['source'],
                    connected_platforms=list(runtime.runner.adapters), home_channels={}, session_key=runtime.key, session_id=sid)):
                children.append(json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
                    {'command': f'while [ ! -e {release} ]; do sleep 0.05; done; printf finished',
                     'background': True, 'notify': True}, task_id=sid)))
        else:
            writes.append(json.loads(await asyncio.to_thread(handle_function_call, 'write_file',
                {'path': str(effect), 'content': 'normal native completion'}, task_id='normal-completion')))
        return {'final_response': 'native done', 'messages': []}

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    try:
        if kind == 'loop':
            manager = loops.LoopManager(sid)
            manager.set('Finish the recurring task', interval_seconds=60, times=1, route={
                'platform': 'discord', 'chat_id': '42', 'chat_type': 'dm', 'user_id': '42'})
            runtime.clock.now += 61
            await runtime.runner._loop_wakeup_fire_one(sid, manager.state, runtime.clock.now, set())
        else:
            manager = goals.GoalManager(sid)
            manager.set('Finish the recurring task')
            monkeypatch.setattr(goals, 'judge_goal', lambda goal, response, **_kw:
                ('continue' if response == 'outline' else 'done', 'progress', False, None, False))
            await runtime.runner._post_turn_goal_continuation(
                session_entry=runtime.entry, source=runtime.source, final_response='outline')
        await async_wait_until(lambda: bool(children), timeout=15, what='periodic reserved child')
        await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
                                               if live.task is not None)), 15)
        assert not children[0].get('error'), children
        manager = loops.LoopManager(sid) if kind == 'loop' else goals.GoalManager(sid)
        assert manager.state.status == 'done'
        process = runtime.registry.get(children[0]['session_id'])
        assert process.approval_task_reservation.lease.live(), manager.state
        release.touch()
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        watcher = next(value for value in watchers if value['session_id'] == process.id)
        event = runtime.runner._build_process_completion_event(watcher, process, process.id)
        assert await runtime.runner._deliver_completion_notification('finished', event)
        await async_wait_until(lambda: bool(writes), timeout=15, what='normally completed periodic child effect')
        assert not writes[0].get('error'), writes
        assert effect.read_text() == 'normal native completion'
    finally:
        release.touch()
        await _finish(runtime)
