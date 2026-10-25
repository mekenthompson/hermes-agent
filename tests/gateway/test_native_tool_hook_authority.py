"""Native Stop and unverified admissions refuse plugin hooks before effects."""
import asyncio
import json
from types import SimpleNamespace
import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('stopped', [False, True])
@pytest.mark.parametrize('boundary', ['pre', 'request', 'execution'])
async def test_native_task_authority_precedes_hooks(tmp_path, monkeypatch, concurrent, stopped, boundary):
    from tests.gateway.test_native_file_effect_stop import _native
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call
    from gateway.session_command_approval import command_task_scope
    from hermes_cli import plugins
    from tools.approval_context import approval_task_revoked
    authority, actor, ref = await _native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    agent = _make_agent('write_file', platform='telegram')
    hook_effect = tmp_path / 'hook-effect'
    tool_effect = tmp_path / 'tool-effect'
    observed = []
    from hermes_cli import middleware
    module, name = {
        'pre': (plugins, '_dispatch_pre_tool_call_hooks'),
        'request': (middleware, 'apply_tool_request_middleware'),
        'execution': (middleware, 'run_tool_execution_middleware'),
    }[boundary]
    original = getattr(module, name)
    def observed_hook(*args, **kwargs):
        observed.append(approval_task_revoked())
        hook_effect.write_text('hook ran')
        return original(*args, **kwargs)
    monkeypatch.setattr(module, name, observed_hook)
    try:
        with command_task_scope(authority, ref, actor.subject, '/probe', authority.route_epoch(route)):
            if stopped:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            assert approval_task_revoked() is stopped
            message = SimpleNamespace(content='', tool_calls=[_mock_tool_call('write_file', json.dumps({'path': str(tool_effect), 'content': 'blocked'}))])
            method = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
            messages = []
            await asyncio.to_thread(method, message, messages, 'probe-task')
        if stopped:
            assert not tool_effect.exists(), messages
            assert not hook_effect.exists(), f'Hook ran after native Stop: {observed}; results: {messages}'
        else:
            assert tool_effect.read_text() == 'blocked', messages
            assert hook_effect.exists() and observed and not any(observed)
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('replacement', ['stop', 'epoch'])
async def test_waiting_middleware_rechecks_before_later_hooks(tmp_path, monkeypatch, concurrent, replacement):
    import threading
    from tests.gateway.test_native_file_effect_stop import _native
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call
    from gateway.session_command_approval import command_task_scope
    from hermes_cli import plugins, middleware
    from hermes_state_runtime import begin_runtime_epoch

    authority, actor, ref = await _native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    agent = _make_agent('write_file', platform='telegram')
    entered, release = threading.Event(), threading.Event()
    hook_effect, tool_effect = tmp_path / 'late-hook', tmp_path / 'late-tool'
    request = middleware.apply_tool_request_middleware

    def held_request(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return request(*args, **kwargs)

    def forbidden_hook(*args, **kwargs):
        hook_effect.touch()
        raise AssertionError('A later execution hook started after revocation')

    monkeypatch.setattr(middleware, 'apply_tool_request_middleware', held_request)
    monkeypatch.setattr(middleware, 'run_tool_execution_middleware', forbidden_hook)
    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', forbidden_hook)
    pending = None
    try:
        with command_task_scope(authority, ref, actor.subject, '/probe', authority.route_epoch(route)):
            message = SimpleNamespace(content='', tool_calls=[_mock_tool_call('write_file', json.dumps(
                {'path': str(tool_effect), 'content': 'blocked'}))])
            method = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
            messages = []
            pending = asyncio.create_task(asyncio.to_thread(method, message, messages, 'waiting-hook-task'))
            assert await asyncio.to_thread(entered.wait, 10)
            if replacement == 'stop':
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            else:
                begin_runtime_epoch(authority.db, instance_id='successor')
            release.set()
            await asyncio.wait_for(pending, 15)
        assert not hook_effect.exists() and not tool_effect.exists(), messages
        assert any('task_authority_revoked' in str(message) for message in messages), messages
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        authority.db.close()

@pytest.mark.asyncio
async def test_real_plugin_ingress_no_grant_does_not_run_agent_pretool_hook(tmp_path, monkeypatch):
    from tests.gateway.test_plugin_command_task_authority import _discord_runtime
    from tests.gateway.test_plugin_origin_injection_authority import _context, _publish
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call, _mock_response
    from hermes_cli import plugins
    from tools.approval_context import approval_task_revoked
    from tests.waiting import async_wait_until
    runtime = await _discord_runtime(tmp_path, monkeypatch)
    injector, manager = _context(tmp_path)
    _publish(runtime.runner, manager)
    agent = _make_agent('write_file', platform='telegram')
    agent._persist_disabled = True
    agent._session_db = None
    hook_effect, tool_effect = tmp_path / 'ingress-hook-effect', tmp_path / 'ingress-tool-effect'
    observed, results = [], []
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content=None, finish_reason='tool_calls', tool_calls=[_mock_tool_call('write_file', json.dumps({'path': str(tool_effect), 'content': 'blocked'}))]),
        _mock_response(content='finished')]
    def pretool(*args, **kwargs):
        observed.append(approval_task_revoked())
        hook_effect.write_text('untrusted admission hook')
        return None, None
    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', pretool)
    async def answer(**kwargs):
        result = await asyncio.to_thread(agent.run_conversation, user_message=kwargs.get('message', 'notification'))
        results.append(result)
        return result
    monkeypatch.setattr(runtime.runner, '_run_agent', answer)
    try:
        assert await asyncio.to_thread(injector.inject_message, 'notification without native task grant', session_key=runtime.key)
        await async_wait_until(lambda: bool(results), timeout=20, what='real plugin ingress tool consumer')
        assert not tool_effect.exists(), results
        assert not hook_effect.exists(), f'Full injected admission ran hook with revoked={observed}; results={results}'
    finally:
        await asyncio.wait_for(asyncio.gather(*(live.task for live in runtime.authority.sessions.values() if live.task is not None)), 15)
        await asyncio.to_thread(runtime.registry.kill_all, source='test-cleanup')
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('dispatch', ['registry', 'invoke'])
async def test_direct_dispatch_rechecks_after_request_middleware(tmp_path, monkeypatch, dispatch):
    import threading
    from tests.gateway.test_native_file_effect_stop import _native
    from tests.agent.test_tool_call_guardrail_runtime import _make_agent
    from gateway.session_command_approval import command_task_scope
    from hermes_cli import plugins, middleware
    from model_tools import handle_function_call
    from agent.tool_invocation import invoke_tool

    authority, actor, ref = await _native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    agent = _make_agent('write_file')
    entered, release = threading.Event(), threading.Event()
    hook_effect, tool_effect = tmp_path / 'direct-hook', tmp_path / 'direct-tool'
    original = middleware.apply_tool_request_middleware

    def held_request(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)

    def forbidden_hook(*args, **kwargs):
        hook_effect.touch()
        raise AssertionError('Revoked direct dispatch entered a later hook')

    monkeypatch.setattr(middleware, 'apply_tool_request_middleware', held_request)
    monkeypatch.setattr(middleware, 'run_tool_execution_middleware', forbidden_hook)
    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', forbidden_hook)
    args = {'path': str(tool_effect), 'content': 'blocked'}
    call = ((lambda: handle_function_call('write_file', args, 'direct-native')) if dispatch == 'registry'
            else (lambda: invoke_tool(agent, 'write_file', args, 'direct-native')))
    pending = None
    try:
        with command_task_scope(authority, ref, actor.subject, '/probe', authority.route_epoch(route)):
            pending = asyncio.create_task(asyncio.to_thread(call))
            assert await asyncio.to_thread(entered.wait, 10)
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            release.set()
            result = json.loads(await asyncio.wait_for(pending, 15))
        assert result.get('code') == 'task_authority_revoked', result
        assert not hook_effect.exists() and not tool_effect.exists()
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        authority.db.close()
