from threading import Event

from tools.approval_context import ApprovalTaskLease, ApprovalTaskContext, ApprovalTaskReservation
from tools.process_registry import ProcessRegistry, ProcessSession


def test_handoff_removes_only_child_stop_fence_and_keeps_parent_revocation():
    parent_stopped = Event()
    child_stopped = Event()
    lease = ApprovalTaskLease(lambda: not parent_stopped.is_set())
    assert lease.reserve_child()
    reservation = ApprovalTaskReservation(
        lease,
        ApprovalTaskContext("request", "user", "profile", "session", "parent", "run", ("child",)),
        (parent_stopped, child_stopped),
    )

    assert reservation.transfer_to_parent(child_stopped)
    child_stopped.set()
    assert reservation.active()
    assert reservation.lease.live()

    parent_stopped.set()
    assert not reservation.active()
    assert not reservation.lease.live()
    reservation.release()


def test_handoff_cannot_revive_a_stopped_or_released_child_reservation():
    child_stopped = Event()
    lease = ApprovalTaskLease(lambda: True)
    assert lease.reserve_child()
    reservation = ApprovalTaskReservation(
        lease,
        ApprovalTaskContext("request", "user", "profile", "session", "parent", "run", ("child",)),
        (child_stopped,),
    )

    lease.close_parent()
    child_stopped.set()
    assert not reservation.transfer_to_parent(child_stopped)
    child_stopped.clear()
    assert not reservation.active()
    assert not lease.live()
    reservation.release()
    assert not reservation.transfer_to_parent(child_stopped)


def test_handoff_settles_a_lease_whose_owner_is_no_longer_current():
    owner_current = Event()
    owner_current.set()
    child_stopped = Event()
    lease = ApprovalTaskLease(owner_current.is_set)
    assert lease.reserve_child()
    reservation = ApprovalTaskReservation(
        lease,
        ApprovalTaskContext("request", "user", "profile", "session"),
        (child_stopped,),
    )

    owner_current.clear()
    assert not reservation.transfer_to_parent(child_stopped)
    assert reservation.stop_events == (child_stopped,)
    owner_current.set()
    assert lease.live()
    assert not reservation.active()
    assert not reservation.transfer_to_parent(child_stopped)


def test_handoff_refuses_revoked_lease_without_a_child_stop_event():
    lease = ApprovalTaskLease(lambda: True)
    assert lease.reserve_child()
    reservation = ApprovalTaskReservation(
        lease, ApprovalTaskContext("request", "user", "profile", "session"),
    )

    lease.revoke()
    assert not reservation.transfer_to_parent(None)
    assert not reservation.active()


def test_registry_handoff_keeps_stale_approval_and_process_with_child():
    owner_current = Event()
    owner_current.set()
    child_stopped = Event()
    lease = ApprovalTaskLease(owner_current.is_set)
    assert lease.reserve_child()
    reservation = ApprovalTaskReservation(
        lease,
        ApprovalTaskContext("request", "user", "profile", "session"),
        (child_stopped,),
    )
    registry = ProcessRegistry()
    session = ProcessSession(id="proc_stalelease", command="sleep 30", task_id="child",
                             owner_task_id="child", session_key="child-route")
    session.approval_task_reservation = reservation
    registry._running[session.id] = session

    owner_current.clear()
    assert registry.transfer_ownership(
        session.id, from_owner="child", to_owner="parent", to_task_id="parent-task",
        to_session_key="parent-route", child_stop_event=child_stopped,
        to_parent_session_id="parent-session",
    ) is None
    assert (session.owner_task_id, session.task_id, session.session_key) == ("child", "child", "child-route")
    owner_current.set()
    assert lease.live()
    assert not reservation.active()
    assert registry.transfer_ownership(
        session.id, from_owner="child", to_owner="parent", to_task_id="parent-task",
        to_session_key="parent-route", child_stop_event=child_stopped,
        to_parent_session_id="parent-session",
    ) is None
    assert session.owner_task_id == "child"
