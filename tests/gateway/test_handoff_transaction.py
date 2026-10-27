"""Native handoff commits ownership and its existing approval fence as one transaction."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from types import SimpleNamespace

import pytest

from tests.gateway.test_process_handoff_stop import Parent, _child, _spawn
from tools.approval_context import (
    ApprovalTaskContext, ApprovalTaskLease, ApprovalTaskReservation,
    approval_task_revoked, background_approval_task, current_approval_task_context,
    reserve_background_approval_task,
)
from tools.process_registry_handoff import OWNERSHIP_FIELDS, handoff_process


@pytest.fixture
def owned_process(tmp_path, monkeypatch):
    import utils
    from tools import process_registry as module
    from tools.delegate_tool import _unregister_subagent

    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    parent = Parent()
    sid, child = _child(parent)
    process = _spawn(registry, tmp_path, sid, 'native-child-session', 'route')
    ancestor_stop = Event()
    lease = ApprovalTaskLease(lambda: True)
    assert lease.reserve_child()
    events = (ancestor_stop, child._hard_interrupt_requested)
    reservation = ApprovalTaskReservation(
        lease, ApprovalTaskContext('finish the authorized build', 'user', 'profile', 's'), events,
    )
    process.approval_task_reservation = reservation
    assert registry._write_checkpoint()
    original_write = registry._write_checkpoint
    atomic_write = utils.atomic_json_write
    owner = SimpleNamespace(registry=registry, process=process, parent=parent, sid=sid, child=child,
        lease=lease, reservation=reservation, events=events, ancestor_stop=ancestor_stop,
        original=tuple(getattr(process, field) for field in OWNERSHIP_FIELDS), module=module)
    try:
        yield owner
    finally:
        monkeypatch.setattr(registry, '_write_checkpoint', original_write)
        monkeypatch.setattr(utils, 'atomic_json_write', atomic_write)
        _unregister_subagent(sid)
        registry.kill_process(process.id)
        reservation.release()


def _identity(owner):
    return tuple(getattr(owner.process, field) for field in OWNERSHIP_FIELDS)


def _checkpoint(owner):
    return next(row for row in json.loads(owner.module._checkpoint_path().read_text())
                if row['session_id'] == owner.process.id)


def _handoff(owner):
    return handoff_process(owner.process.id, {'data': 'finish the build and report its output'}, owner.sid)


def _refuse_checkpoint(owner, monkeypatch, commit):
    if commit:
        return
    import utils
    write, checkpoint = utils.atomic_json_write, owner.module._checkpoint_path()
    def refused(path, *args, **kwargs):
        if path == checkpoint:
            raise PermissionError('fixture native handoff checkpoint refusal')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(utils, 'atomic_json_write', refused)


def _assert_native_cleanup_owner(owner, committed):
    if committed:
        assert owner.registry.kill_all(owner.sid, source='session.interrupt') == 0
        assert owner.registry.kill_all(owner.parent._current_task_id, source='session.interrupt') == 1
    else:
        assert owner.registry.kill_all(owner.sid, source='session.interrupt') == 1
    assert owner.process._completion_event.wait(20)
    assert owner.process.exited and owner.process.completion_reason == 'killed'
    assert owner.process.process.poll() is not None


def test_child_stop_before_handoff_cannot_transfer_or_revive(owned_process):
    owner = owned_process
    owner.child._hard_interrupt_requested.set()
    assert 'error' in _handoff(owner)
    assert _identity(owner) == owner.original
    assert _checkpoint(owner)['parent_session_id'] == 'native-child-session'
    assert owner.reservation.stop_events == owner.events
    assert not owner.reservation.active()
    owner.child._hard_interrupt_requested.clear()
    assert not owner.reservation.active()
    assert 'error' in _handoff(owner)
    assert _identity(owner) == owner.original
    _assert_native_cleanup_owner(owner, False)


@pytest.mark.parametrize('commit', [True, False], ids=['committed', 'write-refused'])
def test_approval_observer_sees_only_committed_parent_or_restored_stopped_child(owned_process, monkeypatch, commit):
    owner = owned_process
    _refuse_checkpoint(owner, monkeypatch, commit)
    entered, release, observing = Event(), Event(), Event()
    write = owner.registry._write_checkpoint

    def held_write(*args, **kwargs):
        # Validation already accepted this transfer, but captures must still see
        # the original child fence throughout the ownership write.
        owner.child._hard_interrupt_requested.set()
        entered.set()
        assert release.wait(20)
        return write(*args, **kwargs)

    def observe():
        observing.set()
        return owner.reservation.active()

    monkeypatch.setattr(owner.registry, '_write_checkpoint', held_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        transfer = pool.submit(_handoff, owner)
        try:
            assert entered.wait(20)
            assert owner.child._hard_interrupt_requested in owner.reservation.stop_events
            observation = pool.submit(observe)
            assert observing.wait(20)
            acquired = owner.reservation._lock.acquire(blocking=False)
            if acquired:
                owner.reservation._lock.release()
            assert not acquired, 'approval observers could enter an uncommitted handoff'
        finally:
            release.set()
        result = transfer.result(timeout=20)
        active = observation.result(timeout=20)
    monkeypatch.setattr(owner.registry, '_write_checkpoint', write)
    assert not owner.process.exited
    if commit:
        assert result['status'] == 'handed_off' and active
        assert owner.process.parent_session_id == 's'
        assert _checkpoint(owner)['ownership_generation'] == 1
        assert owner.reservation.stop_events == (owner.ancestor_stop,)
        owner.ancestor_stop.set()
        assert not owner.reservation.active(), 'an ancestor Stop was detached with the child'
    else:
        assert 'error' in result and not active
        assert _identity(owner) == owner.original
        assert _checkpoint(owner)['ownership_generation'] == owner.original[-1]
        assert owner.reservation.stop_events == owner.events
        owner.child._hard_interrupt_requested.clear()
        assert not owner.reservation.active(), 'rollback revived an already stopped child'
    _assert_native_cleanup_owner(owner, commit)


@pytest.mark.parametrize('commit', [True, False], ids=['committed', 'write-refused'])
def test_parent_lease_revocation_during_commit_never_restores_authorization(owned_process, monkeypatch, commit):
    owner = owned_process
    _refuse_checkpoint(owner, monkeypatch, commit)
    write = owner.registry._write_checkpoint

    def revoke_during_write(*args, **kwargs):
        assert owner.child._hard_interrupt_requested in owner.reservation.stop_events
        owner.lease.revoke()
        return write(*args, **kwargs)

    monkeypatch.setattr(owner.registry, '_write_checkpoint', revoke_during_write)
    result = _handoff(owner)
    monkeypatch.setattr(owner.registry, '_write_checkpoint', write)
    if commit:
        assert result['status'] == 'handed_off'
        assert owner.process.parent_session_id == 's'
        assert _checkpoint(owner)['ownership_generation'] == 1
    else:
        assert 'error' in result
        assert _identity(owner) == owner.original
        assert owner.reservation.stop_events == owner.events
        assert _checkpoint(owner)['ownership_generation'] == owner.original[-1]
    assert owner.process.approval_task_reservation is owner.reservation
    assert owner.reservation.lease is owner.lease and not owner.lease.live()
    with background_approval_task(owner.reservation, owner.process.id):
        assert current_approval_task_context() is None
        assert approval_task_revoked()
    owner.child._hard_interrupt_requested.clear()
    assert not owner.reservation.active() and not owner.lease.live()
    _assert_native_cleanup_owner(owner, commit)


def test_preauthorized_capture_during_failed_handoff_keeps_child_stop_fence(owned_process, monkeypatch):
    owner = owned_process
    _refuse_checkpoint(owner, monkeypatch, False)
    authorized, permit_capture, captured_ready = Event(), Event(), Event()
    write_entered, release_write, finish_capture = Event(), Event(), Event()
    reserve, write = owner.lease.reserve_child, owner.registry._write_checkpoint
    captured = []

    def paused_reserve():
        # The actual producer already passed approval_task_revoked(), including
        # its source reservation's active() check, before reaching this seam.
        authorized.set()
        assert permit_capture.wait(20)
        return reserve()

    def capture():
        with background_approval_task(owner.reservation, 'already-authorized-producer'):
            captured.append(reserve_background_approval_task())
            captured_ready.set()
            assert finish_capture.wait(20)

    def held_write(*args, **kwargs):
        write_entered.set()
        assert release_write.wait(20)
        return write(*args, **kwargs)

    monkeypatch.setattr(owner.lease, 'reserve_child', paused_reserve)
    monkeypatch.setattr(owner.registry, '_write_checkpoint', held_write)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            producer = pool.submit(capture)
            try:
                assert authorized.wait(20)
                transfer = pool.submit(_handoff, owner)
                assert write_entered.wait(20)
                permit_capture.set()
                assert captured_ready.wait(20)
                reservation = captured[0]
                assert reservation is not None
                during_write = reservation.stop_events
                owner.child._hard_interrupt_requested.set()
                release_write.set()
                assert 'error' in transfer.result(timeout=20)
                assert _identity(owner) == owner.original
                assert _checkpoint(owner)['ownership_generation'] == owner.original[-1]
                active = reservation.active()
                with background_approval_task(reservation, 'captured-background-unit'):
                    revoked = approval_task_revoked()
                    evidence = current_approval_task_context()
                assert revoked and evidence is None, 'failed handoff leaked usable task authorization'
                assert not active and during_write == owner.events
                owner.child._hard_interrupt_requested.clear()
                assert not reservation.active(), 'clearing Stop revived the escaped capture'
            finally:
                owner.child._hard_interrupt_requested.set()
                permit_capture.set()
                release_write.set()
                finish_capture.set()
            producer.result(timeout=20)
    finally:
        monkeypatch.setattr(owner.registry, '_write_checkpoint', write)
        for reservation in captured:
            if reservation is not None:
                reservation.release()
    _assert_native_cleanup_owner(owner, False)
