"""Native task authority survives normal memory queuing and is revoked by Stop."""
import asyncio
import threading
from types import SimpleNamespace

import pytest


async def native(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    actor = Principal('owner', 'default', frozenset({'session:create', 'session:read', 'session:control'}), 'viewer')
    ref = create_local_session(authority, actor, dict(request_id='review', source='cli', model='fixture', toolsets=[]))
    await authority.attach(actor, ref)
    return authority, actor, ref



@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_native_memory_queue_retains_authority_until_stop(tmp_path, monkeypatch, stopped):
    from gateway.session_command_approval import command_task_scope
    from agent.memory_manager import MemoryManager
    from tools.approval_context import current_approval_task_context

    authority, actor, ref = await native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    effect = tmp_path / "queued-memory.txt"
    observed = []
    def sync(*args, **kwargs):
        observed.append(current_approval_task_context())
        effect.write_text("authorized queued memory")
    manager = MemoryManager()
    manager.add_provider(SimpleNamespace(name="builtin", get_tool_schemas=list,
        initialize=lambda *a, **kw: None, sync_turn=sync, shutdown=lambda: None))
    manager.initialize_all(ref.session_id, hermes_home=str(tmp_path / "home"))
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(15)
    manager._submit_background(blocker)
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        with command_task_scope(authority, ref, actor.subject, "/review", authority.route_epoch(route)):
            manager.sync_all("user task", "completed response", session_id=ref.session_id)
        # The normal parent turn has closed before the memory worker can run.
        if stopped:
            await authority.interrupt(actor, ref, authority._handle(ref).execution_generation)
        release.set()
        assert await asyncio.to_thread(manager.flush_pending, 10)
        assert effect.exists() is (not stopped)
        if not stopped:
            assert observed[0] is not None and observed[0].identity == actor.subject
            assert observed[0].ancestry[-1] == "memory-worker"
        else:
            assert observed == []
    finally:
        release.set()
        await asyncio.to_thread(manager.shutdown_all)


@pytest.mark.asyncio
async def test_cancelled_memory_queue_releases_native_reservation(tmp_path, monkeypatch):
    from gateway.session_command_approval import command_task_scope
    from agent.memory_manager import MemoryManager

    authority, actor, ref = await native(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    manager = MemoryManager()
    manager.initialize_all(ref.session_id, hermes_home=str(tmp_path / "home"))
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(15)
    manager._submit_background(blocker)
    assert await asyncio.to_thread(entered.wait, 10)
    effect = tmp_path / "cancelled-memory.txt"
    try:
        with command_task_scope(authority, ref, actor.subject, "/review", authority.route_epoch(route)):
            manager._submit_background(lambda: effect.write_text("must not run"))
        live = authority.sessions[ref.session_id]
        lease, = list(live.approval_task_leases)
        assert lease.live()
        with manager._sync_executor_lock:
            pending = [future for future in manager._background_futures if not future.running()]
        assert len(pending) == 1 and pending[0].cancel()
        assert not lease.live(), "a cancelled queue entry cannot retain a task grant"
        release.set()
        assert await asyncio.to_thread(manager.flush_pending, 10)
        assert not effect.exists()
    finally:
        release.set()
        await asyncio.to_thread(manager.shutdown_all)
