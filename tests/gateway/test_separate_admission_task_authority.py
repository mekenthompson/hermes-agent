"""Separate producer admissions cannot borrow the caller's live or revoked native task."""
import asyncio
import json
import threading

import pytest

from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_registered_discord_command_injection_has_no_command_authority(tmp_path, monkeypatch, revoked):
    from gateway.platforms.event import MessageEvent
    from model_tools import handle_function_call
    from tests.gateway.test_plugin_command_task_authority import _discord_runtime
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tools.approval_context import current_approval_task_context, revoke_current_approval_task

    runtime = await _discord_runtime(tmp_path, monkeypatch)
    injector, manager = _context(tmp_path)
    _publish(runtime.runner, manager)
    receipts, origins, parent_origins = [], [], []
    consumer_finished = threading.Event()
    effect, parent_effect = tmp_path / 'injected', tmp_path / 'parent'

    def handler(_arg):
        parent_origins.append(current_approval_task_context())
        if revoked:
            revoke_current_approval_task()
        assert injector.inject_message('notification, not a human instruction', session_key=runtime.key)
        assert consumer_finished.wait(15)
        parent_origins.append(current_approval_task_context())
        parent = json.loads(handle_function_call('terminal',
            {'command': f'printf parent > {parent_effect}', 'background': True}, task_id='parent'))
        if not revoked:
            assert not parent.get('error'), parent
            assert runtime.registry.get(parent['session_id'])._completion_event.wait(10)
        else:
            assert parent.get('code') == 'task_authority_revoked', parent
        return 'notification sent'

    async def answer(**_kwargs):
        try:
            origins.append(current_approval_task_context())
            receipts.append(json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
                {'command': f'printf leaked > {effect}', 'background': True}, task_id='injected')))
            return {'final_response': 'checked', 'messages': []}
        finally:
            consumer_finished.set()

    monkeypatch.setattr(runtime.runner, '_run_agent', answer)
    registration = runtime.context.register_command('inject-authority', handler)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/inject-authority', source=runtime.source, message_id='inject'))
        await async_wait_until(lambda: bool(receipts) and not runtime.adapter._background_tasks,
                               timeout=20, what='injected consumer and command completion')
        assert origins == [None], (origins, receipts)
        assert receipts[0].get('code') == 'task_authority_revoked', receipts
        assert 'no valid native task authority' in receipts[0]['error'], receipts
        assert not effect.exists()
        assert parent_origins[0] is not None
        assert (parent_origins[1] == parent_origins[0]) is (not revoked)
        assert parent_effect.exists() is (not revoked)
    finally:
        consumer_finished.set()
        registration.dispose()
        await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values()
                                               if live.task is not None)), 15)
        await asyncio.to_thread(runtime.registry.kill_all, source='test-cleanup')
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_native_desktop_plugin_injection_cannot_borrow_command_scope(tmp_path, monkeypatch, revoked):
    from gateway.session_command_approval import command_task_scope
    from gateway.session_local import create_local_session
    from model_tools import handle_function_call
    from tests.gateway.test_native_terminal_background_authority import _runtime
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tools.approval_context import current_approval_task_context, revoke_current_approval_task

    runner, authority, actor, _ref, registry = await _runtime(tmp_path, monkeypatch)
    ref = create_local_session(authority, actor, dict(request_id='desktop', source='gui', model='fixture', toolsets=[]))
    await authority.attach(actor, ref)
    route = authority.sessions[ref.session_id].route
    injector, manager = _context(tmp_path / 'home')
    _publish(runner, manager)
    effects, origins = [], []
    effect = tmp_path / 'desktop-injected'

    async def answer(**_kwargs):
        origins.append(current_approval_task_context())
        effects.append(json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
            {'command': f'printf leaked > {effect}', 'background': True}, task_id='desktop-injected')))
        return {'final_response': 'checked', 'messages': []}

    monkeypatch.setattr(runner, '_run_agent', answer)
    try:
        with command_task_scope(authority, ref, actor.subject, '/notify', authority.route_epoch(route)):
            parent = current_approval_task_context()
            assert parent is not None
            if revoked:
                revoke_current_approval_task()
            assert await asyncio.to_thread(injector.inject_message, 'untrusted notification', session_key=route)
            await async_wait_until(lambda: bool(effects), timeout=15, what='Desktop injected consumer')
            assert (current_approval_task_context() == parent) is (not revoked)
        assert origins == [None]
        assert effects[0].get('code') == 'task_authority_revoked', effects
        assert 'no valid native task authority' in effects[0]['error'], effects
        assert not effect.exists()
    finally:
        await asyncio.wait_for(asyncio.gather(*(live.task for live in authority.sessions.values()
                                               if live.task is not None)), 15)
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ambient', ['none', 'live', 'revoked'])
async def test_native_api_admission_keeps_foreground_policy_without_borrowing_task(tmp_path, monkeypatch, ambient):
    from contextlib import nullcontext
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session_api_turn import admit_api_turn, observe_api_turn
    from gateway.session_command_approval import command_task_scope
    from gateway.session_results import execution_result
    from model_tools import handle_function_call
    from tests.gateway.test_native_terminal_background_authority import _runtime
    from tools.approval_context import current_approval_task_context, revoke_current_approval_task

    runner, authority, actor, ref, registry = await _runtime(tmp_path, monkeypatch)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    adapter._session_db = authority.db
    runner.adapters[Platform.API_SERVER] = adapter
    observed = []
    effect = tmp_path / 'api-effect'

    async def handle(_event):
        origin = current_approval_task_context()
        foreground = json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
            {'command': f'printf foreground > {effect}'}, task_id='api-foreground'))
        background = json.loads(await asyncio.to_thread(handle_function_call, 'terminal',
            {'command': f'printf background > {effect}', 'background': True}, task_id='api-background'))
        observed.append((origin, foreground, background))
        execution_result.get()['result'] = {'final_response': 'checked', 'messages': []}
        return 'checked'

    monkeypatch.setattr(runner, '_handle_message', handle)
    route = authority.sessions[ref.session_id].route
    scope = (command_task_scope(authority, ref, actor.subject, '/api', authority.route_epoch(route))
             if ambient != 'none' else nullcontext())
    try:
        with scope:
            if ambient == 'revoked':
                revoke_current_approval_task()
            admitted = admit_api_turn(adapter, user_message='ordinary API request', conversation_history=[],
                                      request_id='api-request', session_id='api-session')
            await asyncio.wait_for(observe_api_turn(admitted), 15)
        assert len(observed) == 1
        origin, foreground, background = observed[0]
        assert origin is None
        if ambient == 'none':
            assert not foreground.get('error'), foreground
            assert effect.read_text() == 'foreground'
            assert background.get('code') == 'missing_task_authority', background
        else:
            assert foreground.get('code') == background.get('code') == 'task_authority_revoked', observed
            assert not effect.exists()
    finally:
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['live', 'stop', 'replacement'])
async def test_api_foreground_file_effect_waiting_for_path_lock(tmp_path, monkeypatch, outcome):
    from contextlib import contextmanager
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session_api_turn import admit_api_turn, observe_api_turn
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_contract import SessionRef
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_response, _mock_tool_call
    from tests.gateway.test_native_terminal_background_authority import _runtime
    from tools import file_tools, file_state
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations
    from tools.approval_context import current_approval_task_context
    from tools.interrupt import is_interrupted, is_thread_interrupted
    from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError

    runner, authority, actor, _ref, registry = await _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv('HERMES_NATIVE_FILE_READ', '1')
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    adapter._session_db = authority.db
    runner.adapters[Platform.API_SERVER] = adapter
    agent = _make_agent('write_file', platform='api_server')
    agent.session_id = 'api-locked-session'
    agent.model = 'test/model'
    agent._persist_disabled = True
    agent._session_db = None
    agent._fallback_chain = []
    effect = tmp_path / 'api-locked-effect'
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content=None, finish_reason='tool_calls', tool_calls=[
            _mock_tool_call('write_file', json.dumps({'path': str(effect), 'content': 'api effect'}))]),
        _mock_response(content='done'),
    ]
    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', lambda **_kwargs: (
        'test/model', {'api_key': 'fixture-key', 'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1'}))
    monkeypatch.setattr(TurnRunner, '_resolve_turn_agent', lambda *_args, **_kwargs: (agent, False))
    operations = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path), timeout=15, persistent=False))
    monkeypatch.setattr(file_tools, '_get_file_ops', lambda _task_id='default': operations)
    original_lock = file_state.lock_path
    waiting = threading.Event()
    effects = []

    @contextmanager
    def observed_lock(path):
        waiting.set()
        with original_lock(path):
            effects.append((threading.get_ident(), is_interrupted(), current_approval_task_context()))
            yield

    monkeypatch.setattr(file_state, 'lock_path', observed_lock)
    observer = None
    try:
        with original_lock(str(effect)):
            admitted = admit_api_turn(adapter, user_message='Write the requested file', conversation_history=[],
                                      request_id='api-locked', session_id='api-locked-session')
            observer = asyncio.create_task(observe_api_turn(admitted))
            assert await asyncio.to_thread(waiting.wait, 15)
            if outcome == 'stop':
                ref = SessionRef(authority.profile_id, 'api-locked-session')
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
                assert agent._hard_interrupt_requested.is_set()
            elif outcome == 'replacement':
                begin_runtime_epoch(authority.db, instance_id='successor')
                assert not agent._hard_interrupt_requested.is_set()
        try:
            result, _usage = await asyncio.wait_for(observer, 15)
        except RuntimeStoreError as error:
            assert outcome == 'replacement', error
            result = str(error)
        if outcome != 'live':
            assert not effect.exists(), result
        else:
            assert effect.read_text() == 'api effect', result
        assert effects and effects[0][1] is (outcome == 'stop') and effects[0][2] is None, effects
        assert not is_thread_interrupted(effects[0][0])
        assert not agent._hard_interrupt_requested.is_set()
        if outcome == 'replacement':
            return
        # The normal finalizer, not a test-side clear, prepares the next independent request.
        fresh_effect = tmp_path / 'api-fresh-effect'
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content=None, finish_reason='tool_calls', tool_calls=[
                _mock_tool_call('write_file', json.dumps({'path': str(fresh_effect), 'content': 'fresh request'}))]),
            _mock_response(content='done'),
        ]
        fresh = admit_api_turn(adapter, user_message='Write the fresh request', conversation_history=[],
                              request_id='api-fresh', session_id='api-locked-session')
        await asyncio.wait_for(observe_api_turn(fresh), 15)
        assert fresh_effect.read_text() == 'fresh request'
        assert effects[-1][1:] == (False, None), effects
    finally:
        if observer is not None and not observer.done():
            await asyncio.wait_for(observer, 15)
        agent.clear_interrupt()
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        authority.db.close()


def test_standalone_effect_guards_do_not_load_api_or_optional_transport(tmp_path):
    import subprocess
    import sys

    source = '''
import importlib.abc
import sys
class NoAPI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'gateway.session_api_turn', 'aiohttp'}:
            raise ImportError('standalone tool must not load ' + fullname)
sys.meta_path.insert(0, NoAPI())
from tools.approval_context import require_live_task_setup, revoked_task_tool_error
require_live_task_setup()
assert revoked_task_tool_error() is None
'''
    result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_validated_cron_does_not_inherit_completed_api_execution_fence(tmp_path, monkeypatch):
    from cron import scheduler
    from gateway import session_cron
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session_api_turn import admit_api_turn, observe_api_turn
    from gateway.session_results import execution_result
    from model_tools import handle_function_call
    from tests.gateway.test_native_cron_task_authority import _fire, _settled
    from tests.gateway.test_native_terminal_background_authority import _runtime
    from tools.approval_context import current_approval_task_context

    runner, authority, _actor, _ref, registry = await _runtime(tmp_path, monkeypatch)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = runner
    adapter._session_db = authority.db
    runner.adapters[Platform.API_SERVER] = adapter
    waiting, release = asyncio.Event(), asyncio.Event()
    execute = session_cron.execute
    fires, observed = [], []
    effect = tmp_path / 'independent-cron'

    async def held(*args):
        waiting.set()
        await release.wait()
        return await execute(*args)

    async def api_model(_event):
        assert current_approval_task_context() is None
        _job, receipt = await _fire(authority)
        fires.append(receipt)
        execution_result.get()['result'] = {'final_response': 'scheduled', 'messages': []}
        return 'scheduled'

    def cron_model(job, **_kwargs):
        assert current_approval_task_context() is not None
        observed.append(json.loads(handle_function_call('terminal',
            {'command': f'printf independent > {effect}', 'background': True}, task_id='independent-cron')))
        return True, 'document', 'done', None

    monkeypatch.setattr(runner, '_handle_message', api_model)
    monkeypatch.setattr(session_cron, 'execute', held)
    monkeypatch.setattr(scheduler, 'run_job', cron_model)
    try:
        admitted = admit_api_turn(adapter, user_message='Trigger the configured job', conversation_history=[],
                                  request_id='api-cron', session_id='api-cron-session')
        await asyncio.wait_for(observe_api_turn(admitted), 15)
        await asyncio.wait_for(waiting.wait(), 10)
        release.set()
        await _settled(authority, fires[0])
        assert not observed[0].get('error'), observed
        process = registry.get(observed[0]['session_id'])
        assert await asyncio.to_thread(process._completion_event.wait, 10)
        assert effect.read_text() == 'independent'
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*(live.task for live in authority.sessions.values()
                                               if live.task is not None)), 15)
        await asyncio.to_thread(registry.kill_all, source='test-cleanup')
        adapter._response_store.close()
        adapter._run_idempotency_store.close()
        authority.db.close()
