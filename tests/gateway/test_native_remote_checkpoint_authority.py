"""Remote background launch requires durable native admission before external effects."""

import asyncio
import shlex
import threading
from types import SimpleNamespace

import pytest

from tests.fakes.loopback_remote_process import LoopbackRemoteEnvironment
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('checkpoint', ['valid', 'failed', 'stop', 'epoch'])
async def test_native_remote_background_checkpoint_gate(tmp_path, monkeypatch, checkpoint):
    from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch, get_session_admission
    from tests.gateway.test_native_terminal_launch_stop_authority import _submit
    from tests.gateway.test_native_terminal_stop_authority import _cleanup, _runtime
    from tools import terminal_tool
    from tools.approval_context import current_approval_task_context

    runtime = await _runtime(tmp_path, monkeypatch)
    effect, remote_dir = tmp_path / 'remote-effect', tmp_path / 'remote'
    remote_dir.mkdir()
    tree = SimpleNamespace(command='printf remote > ' + shlex.quote(str(effect)), release=tmp_path / 'release')
    entered, release = threading.Event(), threading.Event()
    writes, launches, origins = [], [], []
    actual_write, actual_spawn = runtime.registry._write_checkpoint, terminal_tool.spawn_background_process
    monkeypatch.setattr(runtime.registry, '_env_temp_dir', lambda _env: str(remote_dir))

    def checkpoint_write(*args, **kwargs):
        pending = [session for session in runtime.registry._running.values()
                   if session.pid_scope == 'sandbox' and session.pid is None and session.reconciliation_required]
        if not pending:
            return actual_write(*args, **kwargs)
        first = not writes
        writes.extend(pending)
        if checkpoint == 'failed':
            return False
        result = actual_write(*args, **kwargs)
        if first and checkpoint in {'stop', 'epoch'}:
            entered.set()
            assert release.wait(15)
        return result

    def remote_spawn(**kwargs):
        origin = current_approval_task_context()
        assert origin is not None, 'the actual native admission must produce authority'
        origins.append(origin)
        remote = LoopbackRemoteEnvironment(kwargs['env'])
        actual_execute = remote.execute

        def execute(command, *args, **options):
            launches.append(command)
            return actual_execute(command, *args, **options)

        remote.execute = execute
        return actual_spawn(**{**kwargs, 'env': remote, 'env_type': 'fixture-remote'})

    monkeypatch.setattr(runtime.registry, '_write_checkpoint', checkpoint_write)
    monkeypatch.setattr(terminal_tool, 'spawn_background_process', remote_spawn)
    ref, route, admitted = await _submit(runtime, tree, name='remote-checkpoint')
    try:
        if checkpoint in {'stop', 'epoch'}:
            assert await asyncio.to_thread(entered.wait, 10)
            assert writes and not launches and not effect.exists()
            if checkpoint == 'stop':
                try:
                    await runtime.authority.interrupt(runtime.actor, ref,
                        runtime.authority._handle(ref).execution_generation)
                except RuntimeStoreError as exc:
                    assert exc.reason == 'unknown_execution', 'pending remote receipt cannot imply confirmed exit'
            else:
                begin_runtime_epoch(runtime.db, instance_id='remote-owner-replacement')
        release.set()
        await async_wait_until(lambda: route in runtime.receipts, timeout=15, what='actual native remote tool receipt')
        receipt = runtime.receipts[route]
        assert len(origins) == 1 and origins[0].task_id == admitted.admission_id
        assert origins[0].profile == runtime.authority.profile_id and origins[0].session == route
        assert writes, 'the real remote producer must cross its pending checkpoint writer'
        session = writes[0]
        if checkpoint == 'valid':
            assert not receipt.get('error') and launches, receipt
            await async_wait_until(effect.exists, timeout=10, what='real remote shell external effect')
            assert effect.read_text() == 'remote'
            assert await asyncio.to_thread(session._completion_event.wait, 10)
            assert session.exited and session.exit_code == 0
            await async_wait_until(lambda: get_session_admission(runtime.db,
                admission_id=admitted.admission_id)['status'] == 'terminal', timeout=10,
                what='actual native parent settlement')
        else:
            if checkpoint == 'failed' and launches:
                await async_wait_until(effect.exists, timeout=10, what='baseline unrecorded remote effect')
            assert not launches, {'receipt': receipt, 'effect': effect.exists(), 'remote_requests': len(launches)}
            assert receipt.get('error') and not effect.exists() and not list(remote_dir.iterdir()), receipt
            assert session.pid is None and session.exit_code is None
            assert session.exited and session.completion_reason == 'failed_start'
            assert not session.notify_on_complete and runtime.registry.completion_queue.empty()
            reservation = getattr(session, 'approval_task_reservation', None)
            assert reservation is None or not reservation.active(), 'NoStart cannot retain completion authority'
            if checkpoint == 'failed':
                assert 'checkpoint' in receipt['error'].lower() and 'not started' in receipt['error'].lower(), receipt
    finally:
        release.set()
        monkeypatch.setattr(runtime.registry, '_write_checkpoint', actual_write)
        await _cleanup(runtime, (tree,))
