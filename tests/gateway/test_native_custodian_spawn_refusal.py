"""A definite pre-fork refusal cannot strand native execution as an unknown effect."""

import asyncio
import errno
import json
import os
import shlex
from types import SimpleNamespace

import pytest

from tests.gateway.test_native_terminal_stop_authority import _runtime
from tests.waiting import async_wait_until


def _refuse_custodian(monkeypatch, registry, marker):
    from hermes_cli.process_custody import NATIVE_CUSTODIAN_SOURCE
    from tools import process_registry

    actual = process_registry.subprocess.Popen
    attempts, reservations, descriptors = [], [], []

    def popen(argv, *args, **kwargs):
        if (isinstance(argv, list) and NATIVE_CUSTODIAN_SOURCE in argv
                and marker in argv[-1]):
            attempts.append(argv)
            descriptors.extend((fd, os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in kwargs['pass_fds'])
            reservations.extend(getattr(session, 'approval_task_reservation', None)
                                for session in registry._running.values() if marker in session.command)
            raise BlockingIOError(errno.EAGAIN, 'Native custodian refused before fork')
        return actual(argv, *args, **kwargs)

    monkeypatch.setattr(process_registry.subprocess, 'Popen', popen)
    return attempts, reservations, descriptors


def _assert_no_start(runtime, marker, reservations, descriptors):
    from tools.process_registry import _checkpoint_path

    assert not runtime.registry._running
    sessions = [session for session in runtime.registry._finished.values() if marker in session.command]
    assert sessions, 'The original no-start receipts must remain inspectable'
    for session in sessions:
        assert session.exited and session.completion_reason == 'failed_start'
        assert session.pid is None and session.custody_pid is None and session.process is None
        assert not session._local_launch_attempted and not session._local_launch_pending
        assert session.exit_code is None and not session.reconciliation_required
        assert session._custody is None and session.approval_task_reservation is None
        assert not session.notify_on_complete and session._completion_event.is_set()
    for reservation in filter(None, reservations):
        assert not reservation.active() and not reservation.lease.live()
    for descriptor, device, inode in descriptors:
        try:
            current = os.fstat(descriptor)
        except OSError:
            continue
        assert (current.st_dev, current.st_ino) != (device, inode), 'Original custody socket leaked'
    assert runtime.registry.completion_queue.empty()
    checkpoint = _checkpoint_path()
    assert not checkpoint.exists() or not json.loads(checkpoint.read_text())


async def _submit(runtime, name, arguments):
    from gateway.session_contract import Submission
    from gateway.session_local import create_local_session
    from hermes_state_runtime import get_session_admission

    ref = create_local_session(runtime.authority, runtime.actor, {'request_id': name,
        'source': 'cli', 'model': 'fixture', 'toolsets': [], 'cwd': str(runtime.tmp_path)})
    await runtime.authority.attach(runtime.actor, ref)
    route = runtime.authority.sessions[ref.session_id].route
    runtime.plans[route] = arguments
    admitted = await runtime.authority.submit(runtime.actor, Submission(
        name + '-submit', ref, {'text': 'Run this authorized command'}, 'queue'))
    await async_wait_until(lambda: route in runtime.receipts and get_session_admission(
        runtime.db, admission_id=admitted.admission_id)['status'] == 'terminal',
        timeout=40, what='real native terminal producer settlement')
    return ref, runtime.receipts[route]


async def _finish(runtime):
    await asyncio.to_thread(runtime.registry.kill_all, source='test-cleanup')
    await asyncio.gather(*(live.task for live in runtime.authority.sessions.values() if live.task),
                         return_exceptions=True)
    runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('backend', ['foreground', 'pipe', 'pty'])
@pytest.mark.parametrize('refused', [False, True])
async def test_native_terminal_definite_spawn_refusal_is_known_no_effect(
        tmp_path, monkeypatch, backend, refused):
    if backend == 'pty':
        pytest.importorskip('ptyprocess')
    runtime = await _runtime(tmp_path, monkeypatch)
    effect = tmp_path / 'never-launched'
    command = 'printf native > ' + shlex.quote(str(effect))
    try:
        with pytest.MonkeyPatch.context() as patch:
            attempts, reservations, descriptors = _refuse_custodian(
                patch, runtime.registry, str(effect)) if refused else ([], [], [])
            arguments = {'command': command, 'background': backend != 'foreground', 'pty': backend == 'pty'}
            if backend != 'foreground':
                arguments['notify'] = True
            ref, receipt = await _submit(runtime, 'spawn-refusal', arguments)
        if refused:
            assert attempts and 'refused before fork' in receipt.get('error', ''), receipt
            assert not effect.exists()
            _assert_no_start(runtime, str(effect), reservations, descriptors)
        else:
            assert not receipt.get('error'), receipt
            if backend != 'foreground':
                session = runtime.registry.get(receipt['session_id'])
                assert await asyncio.to_thread(session._completion_event.wait, 10)
                assert session.exit_code == 0
            assert effect.read_text() == 'native'
        await runtime.authority.interrupt(runtime.actor, ref,
                                         runtime.authority._handle(ref).execution_generation)
        if refused:
            assert not effect.exists()
            _assert_no_start(runtime, str(effect), reservations, descriptors)
            fresh = tmp_path / 'fresh-authorized-effect'
            _, current = await _submit(runtime, 'fresh-authorized', {
                'command': 'printf fresh > ' + shlex.quote(str(fresh))})
            assert not current.get('error') and fresh.read_text() == 'fresh', current
            assert all(not value.lease.live() for value in filter(None, reservations))
    finally:
        await _finish(runtime)


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_native_configured_quick_spawn_refusal_does_not_block_stop(tmp_path, monkeypatch):
    from gateway.session_commands import execute_command
    from gateway.session_local import create_local_session
    from tests.gateway.test_native_quick_command_authority import _configure

    runtime = await _runtime(tmp_path, monkeypatch)
    effect = tmp_path / 'quick-never-launched'
    _configure(tmp_path / 'home', {'refused': 'printf forbidden > ' + shlex.quote(str(effect))})
    ref = create_local_session(runtime.authority, runtime.actor, {'request_id': 'quick-refusal',
        'source': 'cli', 'model': 'fixture', 'toolsets': [], 'cwd': str(tmp_path)})
    await runtime.authority.attach(runtime.actor, ref)
    connection = SimpleNamespace(authority=runtime.authority, actor=runtime.actor, subscriptions={})
    try:
        with pytest.MonkeyPatch.context() as patch:
            attempts, reservations, descriptors = _refuse_custodian(patch, runtime.registry, str(effect))
            result = await execute_command(connection, ref, {'command': '/refused'})
        assert attempts and 'refused before fork' in str(result), result
        assert not effect.exists()
        _assert_no_start(runtime, str(effect), reservations, descriptors)
        await runtime.authority.interrupt(runtime.actor, ref,
                                         runtime.authority._handle(ref).execution_generation)
        _assert_no_start(runtime, str(effect), reservations, descriptors)
        assert not effect.exists()
    finally:
        await _finish(runtime)
