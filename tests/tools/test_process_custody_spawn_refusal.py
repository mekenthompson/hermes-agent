"""A definite custodian Popen refusal has no target effect or retained OS handles."""
import errno
import os

import pytest

from tools.approval_context import approval_task_context, reserve_background_approval_task
from tools.process_registry import ProcessRegistry


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('stage,logged', [('popen', False), ('popen', True), ('socketpair', False), ('output', True)])
def test_refused_custodian_closes_handles_and_finishes_same_unstarted_scope(
        tmp_path, monkeypatch, cancelled, stage, logged):
    from tools import process_registry as facade
    from tools import process_registry_custody as custody_module
    from tools import process_registry_spawn as spawn

    registry = ProcessRegistry()
    monkeypatch.setattr(facade, 'process_registry', registry)
    session = registry._new_session('never launched', 'original', 'original', 'route', str(tmp_path))
    session._native_local_launch = session._local_launch_pending = True
    spawn.retain_remote_unknown(registry, session, spawn._LOCAL_PENDING)
    handles, reservations, output_fds = [], [], []
    original_custody = custody_module.LocalCustody
    original_output = facade.open_output_log
    original_cancel = spawn.cancel_unstarted_local_launch

    def refusal():
        reservation = session.approval_task_reservation
        reservations.append(reservation)
        if cancelled:
            reservation.lease.revoke()
        raise BlockingIOError(errno.EAGAIN, 'Custodian creation refused before fork')

    def control(*args):
        if stage == 'socketpair':
            refusal()
        value = original_custody(*args)
        handles.extend((value.socket, value.child_socket))
        return value

    def refuse(_argv, **kwargs):
        if logged:
            output_fds.append(kwargs['stdout'])
        refusal()

    def output(path):
        if stage == 'output':
            refusal()
        return original_output(path)

    def cancel_after_concurrent_stop(*args):
        # Stop lands after the creator drops the descriptor lock but before
        # the existing no-start helper acquires it: its intent must win.
        if cancelled:
            session.termination_source = 'session.interrupt'
        return original_cancel(*args)

    monkeypatch.setattr(custody_module, 'LocalCustody', control)
    monkeypatch.setattr(facade.subprocess, 'Popen', refuse)
    monkeypatch.setattr(facade, 'open_output_log', output)
    monkeypatch.setattr(spawn, 'cancel_unstarted_local_launch', cancel_after_concurrent_stop)
    with approval_task_context('Run the original command', identity='user', profile='profile', session='route',
                               task_id='original', run_id='1', is_current=lambda: True):
        session.approval_task_reservation = reserve_background_approval_task()
        with pytest.raises(BlockingIOError) as failed:
            custody_module.spawn_custodied(registry, session, ['never-launched'], {},
                                           log=tmp_path / 'output.log' if logged else None)
        assert failed.value.errno == errno.EAGAIN

    assert all(handle.fileno() == -1 for handle in handles)
    for descriptor in output_fds:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert session.exited and session.custody_scope_empty and not session._local_launch_attempted
    assert session.pid is None and session.custody_pid is None and session._custody is None
    assert session.completion_reason == 'failed_start' and session.exit_code is None
    assert not session.reconciliation_required and not session._local_launch_pending
    assert session.termination_source == ('session.interrupt' if cancelled else 'local_custodian_spawn_refused')
    assert not registry._running and registry.get(session.id) is session
    assert registry.kill_process(session.id)['status'] == 'already_exited'
    assert registry.completion_queue.empty()
    assert session.approval_task_reservation is None
    assert not reservations[0].active() and not reservations[0].lease.live()


@pytest.mark.platforms('linux')
def test_fd_error_after_returned_custodian_keeps_existing_resource_uncertain(tmp_path, monkeypatch):
    from tools import process_registry as facade
    from tools.process_registry_custody import spawn_custodied
    from tools.process_registry_spawn import _LOCAL_PENDING, retain_remote_unknown

    registry = ProcessRegistry()
    monkeypatch.setattr(facade, 'process_registry', registry)
    session = registry._new_session('never authorized', 'original', 'original', 'route', str(tmp_path))
    session._native_local_launch = session._local_launch_pending = True
    retain_remote_unknown(registry, session, _LOCAL_PENDING)
    original_open, original_close, original_popen = facade.open_output_log, os.close, facade.subprocess.Popen
    output_fd, returned = [], []

    def output(path):
        descriptor = original_open(path)
        output_fd.append(descriptor)
        return descriptor

    def create(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        returned.append(process)
        return process

    def close(descriptor):
        original_close(descriptor)
        if output_fd == [descriptor] and returned:
            raise OSError(errno.EIO, 'Parent output descriptor close failed after process creation')

    monkeypatch.setattr(facade, 'open_output_log', output)
    monkeypatch.setattr(facade.subprocess, 'Popen', create)
    monkeypatch.setattr(os, 'close', close)
    try:
        with approval_task_context('Original command', identity='user', profile='profile', session='route',
                                   task_id='original', run_id='1', is_current=lambda: True):
            with pytest.raises(OSError, match='after process creation'):
                spawn_custodied(registry, session, ['touch', str(tmp_path / 'never-go')], {},
                                 log=tmp_path / 'output.log')
        assert returned and session.process is returned[0] and session._custody.proc is returned[0]
        assert not session.exited and session.reconciliation_required and session._local_launch_pending
        assert session.completion_reason != 'failed_start' and session.id in registry._running
        assert session.approval_task_reservation.active()
        assert not session._local_launch_attempted and not (tmp_path / 'never-go').exists()
    finally:
        if session._custody is not None:
            session._custody.close()  # no GO: EOF makes the actual armed keeper exit without a target
            session._custody.child_socket.close()
        for process in returned:
            process.wait(timeout=5)
        registry._finish_exited(session, returned[0].returncode if returned else None)
