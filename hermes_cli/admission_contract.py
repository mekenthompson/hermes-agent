"""Typed evidence exchanged before bounded work is admitted.

This module is intentionally policy-free.  Launch-path adapters collect and
verify their facts, then the admission controller decides whether a request may
reserve capacity.  Numeric ceilings remain controller policy, never request
configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Mapping


class AdmissionCaller(str, Enum):
    """Launch families that may present an aggregate-admission request."""

    KANBAN = "kanban"
    DELEGATE = "delegate"


class AdmissionErrorCode(str, Enum):
    """Stable, inspectable reasons for denied or invalid admission."""

    MISSING_EVIDENCE = "missing_evidence"
    MALFORMED_EVIDENCE = "malformed_evidence"
    UNSUPPORTED_CALLER = "unsupported_caller"
    CONFLICTING_WRITER = "conflicting_writer"
    UNSAFE_ANCESTRY = "unsafe_ancestry"
    DEPENDENCIES_UNSATISFIED = "dependencies_unsatisfied"
    RESOURCE_CAPACITY = "resource_capacity"


class AdmissionContractError(ValueError):
    """Raised when a caller presents evidence that cannot be evaluated safely."""

    def __init__(self, code: AdmissionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DependencyEvidence:
    """Board-native dependency facts collected before dispatch."""

    board_id: str
    task_id: str
    dependency_ids: tuple[str, ...]
    unresolved_dependency_ids: tuple[str, ...]
    verified_at: int

    def __post_init__(self) -> None:
        _nonempty(self.board_id, "board_id")
        _nonempty(self.task_id, "task_id")
        _positive_timestamp(self.verified_at)
        dependencies = _identifiers(self.dependency_ids, "dependency_ids")
        unresolved = _identifiers(self.unresolved_dependency_ids, "unresolved_dependency_ids")
        if not set(unresolved).issubset(dependencies):
            raise AdmissionContractError(
                AdmissionErrorCode.MALFORMED_EVIDENCE,
                "unresolved_dependency_ids must be a subset of dependency_ids",
            )


@dataclass(frozen=True)
class AncestryEvidence:
    """Git ancestry facts used to prove a writer belongs to its intended line."""

    branch: str
    head_sha: str
    base_sha: str
    ancestor_shas: tuple[str, ...]
    verified_at: int

    def __post_init__(self) -> None:
        _nonempty(self.branch, "branch")
        _sha(self.head_sha, "head_sha")
        _sha(self.base_sha, "base_sha")
        if not self.ancestor_shas:
            raise AdmissionContractError(AdmissionErrorCode.MISSING_EVIDENCE, "ancestor_shas is required")
        for sha in self.ancestor_shas:
            _sha(sha, "ancestor_shas")
        _positive_timestamp(self.verified_at)


@dataclass(frozen=True)
class WorktreeEvidence:
    """Resolved Git worktree facts; all paths must be absolute and distinct."""

    path: str
    git_dir: str
    common_dir: str
    branch: str
    head_sha: str
    base_sha: str
    verified_at: int

    def __post_init__(self) -> None:
        _absolute_path(self.path, "path")
        git_dir = _absolute_path(self.git_dir, "git_dir")
        common_dir = _absolute_path(self.common_dir, "common_dir")
        if git_dir == common_dir:
            raise AdmissionContractError(
                AdmissionErrorCode.MALFORMED_EVIDENCE,
                "git_dir and common_dir must identify a linked worktree",
            )
        _nonempty(self.branch, "branch")
        _sha(self.head_sha, "head_sha")
        _sha(self.base_sha, "base_sha")
        _positive_timestamp(self.verified_at)


@dataclass(frozen=True)
class WriterEvidence:
    """The mutation identity and complete resource names it intends to write."""

    writer_id: str
    write_targets: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.writer_id, "writer_id")
        targets = _identifiers(self.write_targets, "write_targets")
        if not targets:
            raise AdmissionContractError(AdmissionErrorCode.MISSING_EVIDENCE, "write_targets is required")


@dataclass(frozen=True)
class AdmissionRequest:
    """One policy-free request containing all evidence required for admission."""

    request_id: str
    caller: AdmissionCaller | str
    aggregate_demand: Mapping[str, int]
    priority: int
    dependencies: DependencyEvidence
    ancestry: AncestryEvidence
    worktree: WorktreeEvidence
    writer: WriterEvidence

    def __post_init__(self) -> None:
        _nonempty(self.request_id, "request_id")
        _positive_dimensions(self.aggregate_demand)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise AdmissionContractError(AdmissionErrorCode.MALFORMED_EVIDENCE, "priority must be an integer")


@dataclass(frozen=True)
class AdmissionResult:
    """Inspectable result returned by a launch-path admission decision."""

    request_id: str
    state: str
    reason: str | None = None
    lease_id: str | None = None
    lease_expires_at: int | None = None


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdmissionContractError(AdmissionErrorCode.MISSING_EVIDENCE, f"{label} is required")
    return value.strip()


def _positive_timestamp(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdmissionContractError(AdmissionErrorCode.MALFORMED_EVIDENCE, "verified_at must be a positive integer")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise AdmissionContractError(AdmissionErrorCode.MALFORMED_EVIDENCE, f"{label} must be a lowercase 40-character SHA")


def _absolute_path(value: str, label: str) -> str:
    _nonempty(value, label)
    path = PurePath(value)
    if not path.is_absolute():
        raise AdmissionContractError(AdmissionErrorCode.MALFORMED_EVIDENCE, f"{label} must be absolute")
    return str(path)


def _identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise AdmissionContractError(AdmissionErrorCode.MALFORMED_EVIDENCE, f"{label} must be a tuple")
    normalized = tuple(_nonempty(value, label) for value in values)
    if len(set(normalized)) != len(normalized):
        raise AdmissionContractError(AdmissionErrorCode.MALFORMED_EVIDENCE, f"{label} must not contain duplicates")
    return normalized


def _positive_dimensions(values: Mapping[str, int]) -> None:
    if not isinstance(values, Mapping) or not values:
        raise AdmissionContractError(
            AdmissionErrorCode.MISSING_EVIDENCE,
            "aggregate_demand must be a non-empty mapping of positive integers",
        )
    for name, units in values.items():
        _nonempty(name, "aggregate_demand resource name")
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise AdmissionContractError(
                AdmissionErrorCode.MALFORMED_EVIDENCE,
                "aggregate_demand values must be positive integers",
            )
