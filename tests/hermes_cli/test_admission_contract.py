"""RED contracts for aggregate admission evidence across Kanban and delegates.

These tests define the source-side boundary only.  Wiring launch paths and
applying policy is deliberately owned by follow-on HF-375 work.
"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.admission import AdmissionController, AdmissionLimits
from hermes_cli.admission_contract import (
    AdmissionCaller,
    AdmissionContractError,
    AdmissionErrorCode,
    AdmissionRequest,
    AncestryEvidence,
    DependencyEvidence,
    WorktreeEvidence,
    WriterEvidence,
)


def _request(
    *,
    request_id: str = "req-1",
    caller: AdmissionCaller = AdmissionCaller.KANBAN,
    priority: int = 50,
    dependency_ids: tuple[str, ...] = (),
    unresolved_dependency_ids: tuple[str, ...] = (),
    ancestry: AncestryEvidence | None = None,
    worktree: WorktreeEvidence | None = None,
    writer: WriterEvidence | None = None,
) -> AdmissionRequest:
    return AdmissionRequest(
        request_id=request_id,
        caller=caller,
        aggregate_demand={"cpu": 1, "model": 1},
        priority=priority,
        dependencies=DependencyEvidence(
            board_id="operations",
            task_id="t_123",
            dependency_ids=dependency_ids,
            unresolved_dependency_ids=unresolved_dependency_ids,
            verified_at=1_789_912_800,
        ),
        ancestry=ancestry
        or AncestryEvidence(
            branch="hf375/bounded-admission",
            head_sha="a" * 40,
            base_sha="b" * 40,
            ancestor_shas=("b" * 40,),
            verified_at=1_789_912_800,
        ),
        worktree=worktree
        or WorktreeEvidence(
            path="/opt/data/workspace/hf375-admission",
            git_dir="/opt/data/workspace/canonical/hermes-fleet-private/.git/worktrees/hf375-admission",
            common_dir="/opt/data/workspace/canonical/hermes-fleet-private/.git",
            branch="hf375/bounded-admission",
            head_sha="a" * 40,
            base_sha="b" * 40,
            verified_at=1_789_912_800,
        ),
        writer=writer
        or WriterEvidence(
            writer_id="kanban:operations:t_123",
            write_targets=("repo:mekenthompson/hermes-agent:branch:hf375/bounded-admission",),
        ),
    )


@pytest.fixture
def controller():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield AdmissionController(
            connection,
            AdmissionLimits(
                running={"cpu": 2, "model": 1},
                queued={"cpu": 2, "model": 2},
            ),
            now=lambda: 100,
        )
    finally:
        connection.close()


def test_request_contract_carries_aggregate_demand_dependencies_priority_ancestry_and_worktree():
    request = _request(dependency_ids=("t_parent",), priority=75)

    assert request.aggregate_demand == {"cpu": 1, "model": 1}
    assert request.dependencies.dependency_ids == ("t_parent",)
    assert request.priority == 75
    assert request.ancestry.branch == request.worktree.branch
    assert request.writer.write_targets == (
        "repo:mekenthompson/hermes-agent:branch:hf375/bounded-admission",
    )


def test_contract_rejects_missing_evidence_with_a_machine_readable_code():
    with pytest.raises(AdmissionContractError) as error:
        _request(worktree=None, writer=WriterEvidence(writer_id="writer", write_targets=()))

    assert error.value.code is AdmissionErrorCode.MISSING_EVIDENCE


def test_contract_rejects_malformed_worktree_and_ancestry_evidence():
    with pytest.raises(AdmissionContractError) as error:
        _request(
            ancestry=AncestryEvidence(
                branch="hf375/bounded-admission",
                head_sha="not-a-sha",
                base_sha="b" * 40,
                ancestor_shas=("b" * 40,),
                verified_at=1_789_912_800,
            )
        )

    assert error.value.code is AdmissionErrorCode.MALFORMED_EVIDENCE


def test_shared_controller_accepts_a_complete_kanban_request(controller):
    result = controller.admit(_request())

    assert result.state == "running"
    assert result.request_id == "req-1"
    assert result.reason is None


def test_shared_controller_rejects_an_unsupported_caller_before_reserving_capacity(controller):
    request = _request(caller="manual")  # type: ignore[arg-type]

    result = controller.admit(request)

    assert result.state == "rejected"
    assert result.reason == AdmissionErrorCode.UNSUPPORTED_CALLER.value
    assert controller.usage("running") == {"cpu": 0, "model": 0}


def test_shared_controller_rejects_unsafe_ancestry_before_a_writer_starts(controller):
    request = _request(
        ancestry=AncestryEvidence(
            branch="hf375/unrelated",
            head_sha="c" * 40,
            base_sha="b" * 40,
            ancestor_shas=("d" * 40,),
            verified_at=1_789_912_800,
        )
    )

    result = controller.admit(request)

    assert result.state == "rejected"
    assert result.reason == AdmissionErrorCode.UNSAFE_ANCESTRY.value
    assert controller.usage("running") == {"cpu": 0, "model": 0}


def test_shared_controller_rejects_conflicting_writer_claims_before_the_second_start(controller):
    first = controller.admit(_request(request_id="first"))
    conflicting = controller.admit(
        _request(
            request_id="second",
            writer=WriterEvidence(
                writer_id="delegate:deleg_456",
                write_targets=("repo:mekenthompson/hermes-agent:branch:hf375/bounded-admission",),
            ),
        )
    )

    assert first.state == "running"
    assert conflicting.state == "rejected"
    assert conflicting.reason == AdmissionErrorCode.CONFLICTING_WRITER.value


def test_shared_controller_queues_dependency_blocked_work_and_promotes_the_highest_priority_ready_writer(controller):
    running = controller.admit(_request(request_id="running", priority=10))
    blocked = controller.admit(
        _request(
            request_id="blocked",
            priority=90,
            dependency_ids=("t_parent",),
            unresolved_dependency_ids=("t_parent",),
            writer=WriterEvidence(
                writer_id="kanban:operations:t_blocked",
                write_targets=("repo:mekenthompson/hermes-agent:branch:hf375/blocked",),
            ),
        )
    )
    low = controller.admit(
        _request(
            request_id="low",
            priority=10,
            writer=WriterEvidence(
                writer_id="delegate:deleg_low",
                write_targets=("repo:mekenthompson/hermes-agent:branch:hf375/low",),
            ),
        )
    )
    high = controller.admit(
        _request(
            request_id="high",
            priority=90,
            writer=WriterEvidence(
                writer_id="delegate:deleg_high",
                write_targets=("repo:mekenthompson/hermes-agent:branch:hf375/high",),
            ),
        )
    )

    assert blocked.state == "queued"
    assert blocked.reason == AdmissionErrorCode.DEPENDENCIES_UNSATISFIED.value
    assert low.state == "queued"
    assert high.state == "queued"
    assert controller.release(running.lease_id) is True
    assert controller.promote_next().request_id == "high"


def test_launch_path_router_rejects_an_unknown_parallel_writer_path():
    from hermes_cli.admission_contract import route_launch_path

    result = route_launch_path("delegate.legacy_helper", AdmissionCaller.DELEGATE, request_id="deleg_123")

    assert result.state == "rejected"
    assert result.reason == AdmissionErrorCode.UNSUPPORTED_PATH.value


@pytest.mark.parametrize(
    ("path", "caller"),
    [
        ("kanban.dispatch_lane", AdmissionCaller.KANBAN),
        ("kanban.worker_process", AdmissionCaller.KANBAN),
        ("delegate.batch", AdmissionCaller.DELEGATE),
        ("delegate.child_process", AdmissionCaller.DELEGATE),
    ],
)
def test_launch_path_router_accepts_only_its_declared_caller(path, caller):
    from hermes_cli.admission_contract import route_launch_path

    routed = route_launch_path(path, caller, request_id="request_123")
    wrong_caller = route_launch_path(path, AdmissionCaller.DELEGATE if caller is AdmissionCaller.KANBAN else AdmissionCaller.KANBAN, request_id="request_456")

    assert routed.state == "routed"
    assert wrong_caller.state == "rejected"
    assert wrong_caller.reason == AdmissionErrorCode.UNSUPPORTED_PATH.value
