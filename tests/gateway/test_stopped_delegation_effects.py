"""Native Stop fences queued workspace setup and delayed external memory callbacks."""
import asyncio
import json
import threading
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_native_background_memory_respects_stop(tmp_path, monkeypatch, stopped):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_command_approval import command_task_scope
    from gateway.session_context import set_session_vars
    from tests.tools.test_delegate_apiserver_background import _patch_delegate, _fake_parent
    from tools import async_delegation as delegation

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    dt = _patch_delegate(monkeypatch)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    actor = Principal('owner', 'default', frozenset({'session:create', 'session:read', 'session:control'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='review', source='cli', model='fixture', toolsets=[]))
    await authority.attach(actor, ref)
    route = authority.sessions[ref.session_id].route
    set_session_vars(platform='telegram', chat_id='chat', session_key=route, session_id=ref.session_id, async_delivery=True)
    entered, release, finalized = threading.Event(), threading.Event(), threading.Event()
    marker = tmp_path / 'memory-after-stop.txt'

    def child(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return {'task_index': 0, 'status': 'completed', 'summary': 'delayed completion'}

    def memory(task, result, **kwargs):
        marker.write_text(result)
        finalized.set()

    monkeypatch.setattr(dt, '_run_single_child', child)
    parent = _fake_parent()
    from agent.memory_manager import MemoryManager
    parent._memory_manager = MemoryManager()
    parent._memory_manager.add_provider(SimpleNamespace(name='builtin', get_tool_schemas=list,
        initialize=lambda *args, **kwargs: None, on_delegation=memory))
    parent._memory_manager.initialize_all(ref.session_id, hermes_home=str(tmp_path))
    delegation._reset_for_tests()
    try:
        with command_task_scope(authority, ref, actor.subject, '/review tests', authority.route_epoch(route)):
            reply = json.loads(await asyncio.to_thread(dt.delegate_task, goal='review tests', background=True, parent_agent=parent))
            assert reply['status'] == 'dispatched'
            assert await asyncio.to_thread(entered.wait, 10)
            if stopped:
                await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            release.set()
            from tools.process_registry import process_registry
            event = await asyncio.to_thread(process_registry.completion_queue.get, True, 10)
            assert event["delegation_id"] == reply["delegation_id"]
            assert marker.exists() is (not stopped)
            assert finalized.is_set() is (not stopped)
    finally:
        release.set()
        delegation._reset_for_tests()


@pytest.mark.asyncio
async def test_stopped_queued_child_does_not_create_worktree(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_command_approval import command_task_scope
    from gateway.session_context import set_session_vars
    from tests.tools.test_delegate_apiserver_background import _patch_delegate, _fake_parent
    from tools import async_delegation as delegation, delegate_tool as dt

    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    repo = tmp_path / 'repo'
    repo.mkdir()
    await asyncio.to_thread(subprocess.run, ['git', 'init', str(repo)], check=True, capture_output=True)
    await asyncio.to_thread(subprocess.run, ['git', '-c', 'user.name=review', '-c', 'user.email=review@example.com',
                    'commit', '--allow-empty', '-m', 'fixture'], cwd=repo, check=True, capture_output=True)
    actual_run = dt._run_single_child
    _patch_delegate(monkeypatch)
    fixture_build = dt._build_child_agent
    def build(**kwargs):
        child = fixture_build(**kwargs)
        child._delegate_repo_root = str(repo)
        child._delegate_admission_required = True
        child._delegate_output_schema = None
        return child
    monkeypatch.setattr(dt, '_build_child_agent', build)
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    def delayed_run(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        try:
            return actual_run(*args, **kwargs)
        finally:
            settled.set()
    monkeypatch.setattr(dt, '_run_single_child', delayed_run)
    monkeypatch.setattr('hermes_cli.admission_runtime.admit_writer',
        lambda **kwargs: SimpleNamespace(state='rejected', request_id='probe', reason='probe'))
    monkeypatch.setattr('hermes_cli.admission_runtime.cancel_admission_request', lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    actor = Principal('owner', 'default', frozenset({'session:create', 'session:read', 'session:control'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='review', source='cli', model='fixture', toolsets=[]))
    await authority.attach(actor, ref)
    route = authority.sessions[ref.session_id].route
    set_session_vars(platform='telegram', chat_id='chat', session_key=route, session_id=ref.session_id, async_delivery=True)
    parent = _fake_parent()
    parent._memory_manager = None
    delegation._reset_for_tests()
    try:
        with command_task_scope(authority, ref, actor.subject, '/review tests', authority.route_epoch(route)):
            reply = json.loads(await asyncio.to_thread(dt.delegate_task, goal='review tests', background=True, parent_agent=parent))
            assert reply['status'] == 'dispatched'
            assert await asyncio.to_thread(entered.wait, 10)
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
            release.set()
            assert await asyncio.to_thread(settled.wait, 10)
            assert not (repo / '.gitignore').exists(), 'native Stop still allowed worktree setup to edit the repository'
    finally:
        release.set()
        delegation._reset_for_tests()
