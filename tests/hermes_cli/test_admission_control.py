"""Behavior contracts for the shared bounded-admission ledger.

The ledger deliberately accepts a caller-owned SQLite connection so Kanban and
non-Kanban executors can coordinate through the same durable store without a
process-local semaphore.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from hermes_cli.admission import AdmissionController, AdmissionLimits


@pytest.fixture
def controller():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield AdmissionController(
            conn,
            AdmissionLimits(running={"cpu": 2, "model": 1}, queued={"cpu": 2, "model": 2}),
            now=lambda: 100,
        )
    finally:
        conn.close()


def test_admits_only_when_every_resource_dimension_has_capacity(controller):
    first = controller.request("kanban:t1", {"cpu": 1, "model": 1}, lease_seconds=30)
    second = controller.request("delegate:d1", {"cpu": 1, "model": 1}, lease_seconds=30)

    assert first.state == "running"
    assert first.lease_id
    assert second.state == "queued"
    assert second.reason == "capacity"


def test_dependency_blocked_work_is_queued_without_consuming_running_capacity(controller):
    blocked = controller.request(
        "kanban:child", {"cpu": 2, "model": 1}, dependencies_satisfied=False,
    )
    admitted = controller.request("delegate:d1", {"cpu": 2, "model": 1})

    assert blocked.state == "queued"
    assert blocked.reason == "dependencies"
    assert admitted.state == "running"


def test_releasing_a_lease_promotes_the_oldest_eligible_queue_entry(controller):
    running = controller.request("kanban:t1", {"cpu": 2, "model": 1})
    first_waiter = controller.request("delegate:d1", {"cpu": 1, "model": 1})
    second_waiter = controller.request("kanban:t2", {"cpu": 1, "model": 1})

    assert controller.release(running.lease_id) is True
    promoted = controller.promote_next()

    assert promoted is not None
    assert promoted.request_id == first_waiter.request_id
    assert promoted.state == "running"
    assert controller.get(second_waiter.request_id).state == "queued"


def test_expired_lease_is_reclaimed_before_a_new_admission(controller):
    clock = [100]
    connection = sqlite3.connect(":memory:", isolation_level=None)
    controller = AdmissionController(
        connection,
        AdmissionLimits(running={"cpu": 1}, queued={"cpu": 1}),
        now=lambda: clock[0],
    )
    try:
        stale = controller.request("kanban:t1", {"cpu": 1}, lease_seconds=5)
        clock[0] = 106
        successor = controller.request("delegate:d1", {"cpu": 1}, lease_seconds=5)

        expired = controller.get(stale.request_id)
        assert stale.state == "running"
        assert successor.state == "running"
        assert expired is not None
        assert expired.state == "expired"
    finally:
        connection.close()


def test_duplicate_request_id_is_idempotent_and_does_not_double_count(controller):
    first = controller.request("kanban:t1", {"cpu": 1, "model": 1}, request_id="r1")
    retried = controller.request("kanban:t1", {"cpu": 1, "model": 1}, request_id="r1")

    assert retried == first
    assert controller.usage("running") == {"cpu": 1, "model": 1}


def test_dependency_readiness_can_promote_a_previously_blocked_request(controller):
    blocked = controller.request("kanban:child", {"cpu": 1, "model": 1}, dependencies_satisfied=False)

    assert controller.set_dependencies_satisfied(blocked.request_id) is True
    promoted = controller.promote_next()

    assert promoted is not None
    assert promoted.request_id == blocked.request_id
    assert promoted.state == "running"


def test_full_queue_rejects_another_request_without_consuming_running_capacity():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    controller = AdmissionController(
        connection, AdmissionLimits(running={"cpu": 1}, queued={"cpu": 1}), now=lambda: 100,
    )
    try:
        controller.request("kanban:t1", {"cpu": 1})
        controller.request("delegate:d1", {"cpu": 1})
        rejected = controller.request("kanban:t2", {"cpu": 1})

        assert rejected.state == "rejected"
        assert rejected.reason == "queue_capacity"
        assert controller.usage("running") == {"cpu": 1}
        assert controller.usage("queued") == {"cpu": 1}
    finally:
        connection.close()


def test_eligible_waiter_prevents_later_work_from_bypassing_the_queue(controller):
    controller.request("kanban:t1", {"cpu": 1, "model": 1})
    waiter = controller.request("delegate:d1", {"cpu": 1, "model": 1})
    later = controller.request("kanban:t2", {"cpu": 1, "model": 1})

    assert waiter.state == "queued"
    assert later.state == "queued"
    assert later.reason == "queue_ahead"


def test_rejects_dimensions_that_can_never_fit_the_running_capacity(controller):
    rejected = controller.request("delegate:oversized", {"cpu": 1, "model": 2}, request_id="oversized")

    assert rejected.state == "rejected"
    assert rejected.reason == "running_capacity"
    assert controller.get("oversized") is None


def test_shared_ledger_rejects_incompatible_resource_limits(tmp_path):
    path = tmp_path / "admission.db"
    first = sqlite3.connect(path, isolation_level=None)
    second = sqlite3.connect(path, isolation_level=None)
    try:
        AdmissionController(first, AdmissionLimits(running={"cpu": 1}, queued={"cpu": 1}))
        with pytest.raises(ValueError, match="identical limits"):
            AdmissionController(second, AdmissionLimits(running={"cpu": 2}, queued={"cpu": 2}))
    finally:
        first.close()
        second.close()


def test_concurrent_callers_cannot_bypass_the_final_running_slot(tmp_path):
    db_path = tmp_path / "admission.db"
    limits = AdmissionLimits(running={"cpu": 1}, queued={"cpu": 2})
    # Create the schema before the concurrent connections arrive.
    AdmissionController(sqlite3.connect(db_path, isolation_level=None), limits).connection.close()
    start = threading.Barrier(3)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def request(source: str) -> None:
        connection = sqlite3.connect(db_path, isolation_level=None, timeout=5)
        try:
            controller = AdmissionController(connection, limits)
            start.wait(timeout=5)
            outcomes.append(controller.request(source, {"cpu": 1}).state)
        except BaseException as exc:  # test should surface thread failures
            errors.append(exc)
        finally:
            connection.close()

    workers = [threading.Thread(target=request, args=(f"delegate:d{i}",)) for i in range(2)]
    for worker in workers:
        worker.start()
    start.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)

    assert not errors
    assert sorted(outcomes) == ["queued", "running"]


def test_rejects_mismatched_retry_and_invalid_resource_shapes(controller):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    controller = AdmissionController(conn, AdmissionLimits(running={"cpu": 1}, queued={"cpu": 1}))
    try:
        controller.request("kanban:t1", {"cpu": 1}, request_id="r1")
        with pytest.raises(ValueError, match="does not match"):
            controller.request("delegate:d1", {"cpu": 1}, request_id="r1")
        with pytest.raises(ValueError, match="positive integer"):
            controller.request("delegate:d2", {"cpu": 0})
    finally:
        conn.close()
