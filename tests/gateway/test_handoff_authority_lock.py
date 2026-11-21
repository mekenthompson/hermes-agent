"""Native handoff never holds process/approval locks across the owner round-trip."""
from concurrent.futures import ThreadPoolExecutor
import os
import queue
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from agent.managed_worker import WorkerControls, encode_frame
from tests.gateway.test_handoff_transaction import (
    _checkpoint, _handoff, _identity, _refuse_checkpoint,
)
from tools.approval_context import ApprovalTaskContext, ApprovalTaskLease, ApprovalTaskReservation

pytestmark = pytest.mark.platforms('linux')
pytest_plugins = ('tests.gateway.test_handoff_transaction',)


def _unlocked_observer(owner, observed):
    complete = Event()
    def observe():
        locks = (owner.registry._lock, owner.reservation._lock, owner.lease._lock)
        acquired = []
        for lock in locks:
            held = lock.acquire(blocking=False)
            acquired.append(held)
            if held:
                lock.release()
        observed.append(tuple(acquired))
        if all(acquired):
            assert owner.registry.get(owner.process.id) is owner.process
            assert owner.reservation.active()
        complete.set()
    observer = Thread(target=observe)
    observer.start()
    assert complete.wait(5)
    observer.join(5)
    return True


@pytest.mark.parametrize('operation', ['registry', 'reservation'])
def test_owner_callback_reenters_native_records_without_locks(owned_process, operation):
    owner, observed = owned_process, []
    owner.lease.is_current = lambda: _unlocked_observer(owner, observed)
    if operation == 'registry':
        assert _handoff(owner)['status'] == 'handed_off'
        assert _checkpoint(owner)['parent_session_id'] == 's'
    else:
        assert owner.reservation.transfer_to_parent(owner.child._hard_interrupt_requested)
    assert observed and all(all(row) for row in observed)


@pytest.mark.parametrize('change', ['revoke-lease', 'stop-child', 'stop-ancestor', 'release-reservation'])
def test_revocation_during_authority_check_cannot_transfer(owned_process, change):
    owner = owned_process
    def authority():
        if change == 'revoke-lease':
            owner.lease.revoke()
        elif change == 'stop-child':
            owner.child._hard_interrupt_requested.set()
        elif change == 'stop-ancestor':
            owner.ancestor_stop.set()
        else:
            owner.reservation.release()
        return True
    owner.lease.is_current = authority
    assert 'error' in _handoff(owner)
    assert _identity(owner) == owner.original
    assert _checkpoint(owner)['ownership_generation'] == 0
    assert not owner.reservation.active()
    owner.child._hard_interrupt_requested.clear()
    owner.ancestor_stop.clear()
    assert not owner.reservation.active()


def test_changed_reservation_during_authority_check_is_preserved(owned_process):
    owner = owned_process
    lease = ApprovalTaskLease(lambda: True)
    assert lease.reserve_child()
    replacement = ApprovalTaskReservation(lease, ApprovalTaskContext('new request', 'user', 'profile', 's'))
    def authority():
        acquired = owner.registry._lock.acquire(blocking=False)
        assert acquired, 'owner callback cannot reenter its native record'
        try:
            owner.process.approval_task_reservation = replacement
        finally:
            owner.registry._lock.release()
        return True
    owner.lease.is_current = authority
    try:
        assert 'error' in _handoff(owner)
        assert _identity(owner) == owner.original
        assert owner.process.approval_task_reservation is replacement and replacement.active()
        assert _checkpoint(owner)['ownership_generation'] == 0
    finally:
        replacement.release()


@pytest.mark.parametrize('old_reply', [True, False])
def test_delayed_authority_reply_cannot_change_new_owner_or_its_grant(owned_process, old_reply):
    owner = owned_process
    entered, release = Event(), Event()
    calls, call_lock = [], Lock()
    def authority():
        with call_lock:
            first = not calls
            calls.append('current')
        if first:
            entered.set()
            assert release.wait(10)
            return old_reply
        return True
    owner.lease.is_current = authority
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(_handoff, owner)
        try:
            assert entered.wait(5)
            newer = owner.registry.transfer_ownership(owner.process.id, from_owner=owner.sid,
                to_owner='newer-owner', to_task_id='newer-task', to_session_key='newer-route',
                to_parent_session_id='newer-parent', note='newer handoff',
                child_stop_event=owner.child._hard_interrupt_requested)
            assert newer is owner.process
        finally:
            release.set()
        assert 'error' in old.result(timeout=10)
    assert owner.process.owner_task_id == 'newer-owner'
    assert _checkpoint(owner)['parent_session_id'] == 'newer-parent'
    assert owner.process.ownership_generation == 1
    assert owner.reservation.active() and owner.reservation.lease.live()


def test_failed_checkpoint_checks_owner_without_locks_and_latches_stale_grant(owned_process, monkeypatch):
    owner, observed = owned_process, []
    current = Event()
    current.set()
    def authority():
        _unlocked_observer(owner, observed)
        return current.is_set()
    owner.lease.is_current = authority
    _refuse_checkpoint(owner, monkeypatch, False)
    write = owner.registry._write_checkpoint
    def failed(*args, **kwargs):
        current.clear()
        return write(*args, **kwargs)
    monkeypatch.setattr(owner.registry, '_write_checkpoint', failed)
    assert 'error' in _handoff(owner)
    assert _identity(owner) == owner.original
    assert not owner.reservation.active()
    current.set()
    assert not owner.reservation.active()
    assert observed and all(all(row) for row in observed)


def test_actual_worker_current_reply_can_reenter_registry_before_handoff(owned_process, monkeypatch):
    owner = owned_process
    requests = queue.Queue()
    controls = WorkerControls.__new__(WorkerControls)
    controls.stopped, controls.finish, controls.lock = Event(), Event(), Lock()
    controls.task_checks, controls.clarifications = {}, {}
    controls.channel = SimpleNamespace(send=lambda kind, **payload: requests.put({'type': kind, **payload}))
    read_fd, write_fd = os.pipe()
    reader, writer = os.fdopen(read_fd, 'rb'), os.fdopen(write_fd, 'wb', buffering=0)
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=reader))
    control_thread = Thread(target=controls.read)
    control_thread.start()
    owner.lease.is_current = controls.task_current
    queried = []
    def respond():
        frame = requests.get(timeout=5)
        queried.append(owner.registry.get(owner.process.id))
        writer.write(encode_frame({'type': 'approval_task_check', 'request_id': frame['request_id'], 'current': True}))
    response = Thread(target=respond)
    response.start()
    try:
        assert _handoff(owner)['status'] == 'handed_off'
        response.join(15)
        assert queried == [owner.process]
    finally:
        writer.write(encode_frame({'type': 'finish'}))
        control_thread.join(15)
        writer.close()
        reader.close()
        response.join(15)
    assert controls.finish.is_set() and not control_thread.is_alive() and not response.is_alive()


def test_failed_attempt_reply_cannot_revoke_a_newer_committed_reservation(owned_process, monkeypatch):
    import utils
    owner = owned_process
    entered, release, failed = Event(), Event(), Event()
    write = utils.atomic_json_write
    calls, call_lock = [], Lock()
    def refuse_once(path, *args, **kwargs):
        if path == owner.module._checkpoint_path() and not failed.is_set():
            failed.set()
            raise PermissionError('first transfer checkpoint refused')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(utils, 'atomic_json_write', refuse_once)
    def authority():
        with call_lock:
            calls.append('current')
            second = len(calls) == 2
        if second:
            entered.set()
            assert release.wait(10)
            return False
        return True
    owner.lease.is_current = authority
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(_handoff, owner)
        try:
            assert entered.wait(5)
            newer = owner.registry.transfer_ownership(owner.process.id, from_owner=owner.sid,
                to_owner='newer-owner', to_task_id='newer-task', to_session_key='newer-route',
                to_parent_session_id='newer-parent', note='newer accepted transfer',
                child_stop_event=owner.child._hard_interrupt_requested)
            assert newer is owner.process
            owner.child._hard_interrupt_requested.set()
        finally:
            release.set()
        assert 'error' in old.result(timeout=10)
    assert owner.process.ownership_generation == 1 and owner.process.owner_task_id == 'newer-owner'
    assert _checkpoint(owner)['parent_session_id'] == 'newer-parent'
    assert owner.reservation.active() and owner.reservation.lease.live()
