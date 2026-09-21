"""Durable shared admission ledger for bounded concurrent work.

Kanban dispatchers and delegation runners pass the same SQLite connection to an
:class:`AdmissionController`.  The controller owns admission state, capacity
accounting, dependency readiness, and renewable leases; callers own their
execution-specific state transitions.  SQLite's ``BEGIN IMMEDIATE`` makes the
check-and-reserve operation one writer-critical section, so two runners cannot
both consume the final unit of a resource dimension.
"""
from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from hermes_cli.admission_contract import AdmissionCaller, AdmissionErrorCode, AdmissionRequest


_ACTIVE_STATES = ("queued", "running")


@dataclass(frozen=True)
class AdmissionLimits:
    """Positive capacity for every resource dimension in each lifecycle lane."""

    running: Mapping[str, int]
    queued: Mapping[str, int]

    def __post_init__(self) -> None:
        running = _normalized_dimensions(self.running, label="running limits")
        queued = _normalized_dimensions(self.queued, label="queued limits")
        if set(running) != set(queued):
            raise ValueError("running and queued limits must name the same resource dimensions")
        object.__setattr__(self, "running", running)
        object.__setattr__(self, "queued", queued)


@dataclass(frozen=True)
class Admission:
    """One durable request's current admission result."""

    request_id: str
    source: str
    state: str
    dimensions: dict[str, int]
    dependencies_satisfied: bool
    lease_id: str | None
    lease_expires_at: int | None
    reason: str | None = None
    priority: int = 0
    writer_id: str | None = None
    write_targets: tuple[str, ...] = ()


class AdmissionController:
    """Atomic resource admission over a caller-owned shared SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        limits: AdmissionLimits,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.connection = connection
        # The Kanban connection already uses sqlite3.Row; standalone delegate
        # callers may not, and this ledger needs stable named-column access.
        if self.connection.row_factory is None:
            self.connection.row_factory = sqlite3.Row
        self.limits = limits
        self._now = now
        self._ensure_schema()

    def request(
        self,
        source: str,
        dimensions: Mapping[str, int],
        *,
        request_id: str | None = None,
        dependencies_satisfied: bool = True,
        lease_seconds: int = 300,
        priority: int = 0,
        writer_id: str | None = None,
        write_targets: tuple[str, ...] | None = None,
        dependency_reason: str = "dependencies",
    ) -> Admission:
        """Create or resume an idempotent admission request.

        A request starts ``running`` only when every dimension fits and no
        eligible queued request is waiting.  Otherwise it joins the bounded
        queue, unless the queue lane is full.  This prevents a late arrival
        from racing around work already accepted by the ledger.
        """
        source = _nonempty(source, "source")
        dims = _normalized_dimensions(dimensions, label="dimensions")
        self._validate_request_dimensions(dims)
        request_id = request_id or f"admit_{secrets.token_hex(12)}"
        request_id = _nonempty(request_id, "request_id")
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        priority = _priority(priority)
        if (writer_id is None) != (write_targets is None):
            raise ValueError("writer_id and write_targets must be supplied together")
        if writer_id is not None:
            writer_id = _nonempty(writer_id, "writer_id")
            write_targets = _normalized_write_targets(write_targets)
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            existing = self._row(request_id)
            if existing is not None:
                admission = self._admission(existing)
                if admission.source != source or admission.dimensions != dims:
                    raise ValueError(f"request_id {request_id!r} does not match its original source and dimensions")
                return admission
            if writer_id is not None and self._has_writer_conflict(write_targets or ()):
                return Admission(
                    request_id, source, "rejected", dims, bool(dependencies_satisfied), None, None,
                    AdmissionErrorCode.CONFLICTING_WRITER.value, priority, writer_id, write_targets or (),
                )
            unschedulable = [
                name for name, units in dims.items() if units > self.limits.running[name]
            ]
            if unschedulable:
                return Admission(
                    request_id, source, "rejected", dims, bool(dependencies_satisfied), None, None,
                    "running_capacity", priority, writer_id, write_targets or (),
                )
            can_run = dependencies_satisfied and self._fits("running", dims)
            queue_ahead = self._has_eligible_queue()
            if can_run and not queue_ahead:
                admission = Admission(
                    request_id, source, "running", dims, bool(dependencies_satisfied),
                    f"lease_{secrets.token_hex(16)}", now + lease_seconds,
                    priority=priority, writer_id=writer_id, write_targets=write_targets or (),
                )
                self._insert(admission, now)
                return admission
            if not self._fits("queued", dims) and not self._preempt_lower_priority(dims, priority):
                return Admission(
                    request_id, source, "rejected", dims, bool(dependencies_satisfied), None, None,
                    "queue_capacity", priority, writer_id, write_targets or (),
                )
            reason = (
                dependency_reason
                if not dependencies_satisfied
                else ("queue_ahead" if queue_ahead else "capacity")
            )
            admission = Admission(
                request_id, source, "queued", dims, bool(dependencies_satisfied), None, None, reason,
                priority, writer_id, write_targets or (),
            )
            self._insert(admission, now)
            return admission

    def admit(self, request: AdmissionRequest) -> Admission:
        """Validate typed writer evidence, then reserve it in this shared ledger.

        The request object is deliberately the only entrypoint that evaluates
        caller identity, dependency readiness, ancestry, linked-worktree proof,
        and writer targets together.  A failed proof returns a durable-looking
        rejection without consuming resource capacity.
        """
        if not isinstance(request, AdmissionRequest):
            raise TypeError("request must be an AdmissionRequest")
        try:
            caller = AdmissionCaller(request.caller)
        except (TypeError, ValueError):
            return self._rejected(request, AdmissionErrorCode.UNSUPPORTED_CALLER)
        if not self._has_consistent_worktree_evidence(request):
            return self._rejected(request, AdmissionErrorCode.UNSAFE_ANCESTRY)
        return self.request(
            f"{caller.value}:{request.writer.writer_id}",
            request.aggregate_demand,
            request_id=request.request_id,
            dependencies_satisfied=not request.dependencies.unresolved_dependency_ids,
            priority=request.priority,
            writer_id=request.writer.writer_id,
            write_targets=request.writer.write_targets,
            dependency_reason=AdmissionErrorCode.DEPENDENCIES_UNSATISFIED.value,
        )

    def set_dependencies_satisfied(self, request_id: str, satisfied: bool = True) -> bool:
        """Mark a queued request eligible after its external dependency graph settles."""
        with self._write():
            result = self.connection.execute(
                "UPDATE admission_requests SET dependencies_satisfied = ? "
                "WHERE request_id = ? AND state = 'queued'",
                (1 if satisfied else 0, request_id),
            )
            return result.rowcount == 1

    def promote_next(self, *, lease_seconds: int = 300) -> Admission | None:
        """Promote the oldest dependency-ready queued request that now fits."""
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            rows = self.connection.execute(
                "SELECT * FROM admission_requests WHERE state = 'queued' "
                "AND dependencies_satisfied = 1 ORDER BY priority DESC, created_at, rowid"
            ).fetchall()
            for row in rows:
                candidate = self._admission(row)
                if not self._fits("running", candidate.dimensions):
                    continue
                promoted = Admission(
                    candidate.request_id, candidate.source, "running", candidate.dimensions, True,
                    f"lease_{secrets.token_hex(16)}", now + lease_seconds,
                    priority=candidate.priority, writer_id=candidate.writer_id,
                    write_targets=candidate.write_targets,
                )
                self.connection.execute(
                    "UPDATE admission_requests SET state = 'running', lease_id = ?, lease_expires_at = ?, reason = NULL "
                    "WHERE request_id = ? AND state = 'queued'",
                    (promoted.lease_id, promoted.lease_expires_at, promoted.request_id),
                )
                return promoted
        return None

    def renew(self, lease_id: str, *, lease_seconds: int = 300) -> bool:
        """Extend one still-running lease; a lost or expired lease cannot revive."""
        lease_seconds = _positive_int(lease_seconds, "lease_seconds")
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            result = self.connection.execute(
                "UPDATE admission_requests SET lease_expires_at = ? "
                "WHERE lease_id = ? AND state = 'running'",
                (now + lease_seconds, lease_id),
            )
            return result.rowcount == 1

    def release(self, lease_id: str) -> bool:
        """Release a running lease exactly once.  Promotion remains explicit."""
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            result = self.connection.execute(
                "UPDATE admission_requests SET state = 'released', lease_id = NULL, lease_expires_at = NULL "
                "WHERE lease_id = ? AND state = 'running'",
                (lease_id,),
            )
            return result.rowcount == 1

    def cancel(self, request_id: str) -> bool:
        """Discard a queued request that will not be promoted by its caller.

        Production pre-spawn adapters are immediate-only: a queued result is a
        failed launch attempt, not a promise that an unrelated future process
        will execute it. Cancellation is request-scoped because queued work has
        no lease to release.
        """
        request_id = _nonempty(request_id, "request_id")
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            result = self.connection.execute(
                "UPDATE admission_requests SET state = 'released', lease_id = NULL, lease_expires_at = NULL, "
                "reason = 'cancelled_before_spawn' WHERE request_id = ? AND state = 'queued'",
                (request_id,),
            )
            return result.rowcount == 1

    def get(self, request_id: str) -> Admission | None:
        """Return current state after lazily reclaiming expired running leases."""
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            row = self._row(request_id)
            return self._admission(row) if row is not None else None

    def usage(self, state: str) -> dict[str, int]:
        """Current reserved units for ``queued`` or ``running`` requests."""
        if state not in _ACTIVE_STATES:
            raise ValueError(f"usage state must be one of {_ACTIVE_STATES}")
        now = int(self._now())
        with self._write():
            self._expire_leases(now)
            return self._usage(state)

    def _ensure_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admission_requests (
                request_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'released', 'expired')),
                dimensions_json TEXT NOT NULL,
                dependencies_satisfied INTEGER NOT NULL CHECK (dependencies_satisfied IN (0, 1)),
                lease_id TEXT UNIQUE,
                lease_expires_at INTEGER,
                reason TEXT,
                created_at INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                writer_id TEXT,
                write_targets_json TEXT
            )
            """
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(admission_requests)")}
        for name, definition in (
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("writer_id", "TEXT"),
            ("write_targets_json", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE admission_requests ADD COLUMN {name} {definition}")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admission_configuration (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                limits_json TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS admission_requests_queue "
            "ON admission_requests(state, dependencies_satisfied, created_at)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS admission_requests_queue_priority "
            "ON admission_requests(state, dependencies_satisfied, priority DESC, created_at)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS admission_requests_lease "
            "ON admission_requests(state, lease_expires_at)"
        )
        limits_json = json.dumps(
            {"queued": self.limits.queued, "running": self.limits.running},
            sort_keys=True, separators=(",", ":"),
        )
        with self._write():
            row = self.connection.execute(
                "SELECT limits_json FROM admission_configuration WHERE singleton = 1"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO admission_configuration (singleton, limits_json) VALUES (1, ?)",
                    (limits_json,),
                )
            elif row["limits_json"] != limits_json:
                raise ValueError("all controllers sharing an admission ledger must use identical limits")

    @contextlib.contextmanager
    def _write(self):
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if owns_transaction:
                self.connection.rollback()
            raise
        else:
            if owns_transaction:
                self.connection.commit()

    def _validate_request_dimensions(self, dimensions: dict[str, int]) -> None:
        unknown = set(dimensions) - set(self.limits.running)
        if unknown:
            raise ValueError(f"unknown resource dimensions: {', '.join(sorted(unknown))}")

    def _row(self, request_id: str):
        return self.connection.execute(
            "SELECT * FROM admission_requests WHERE request_id = ?", (request_id,)
        ).fetchone()

    def _insert(self, admission: Admission, now: int) -> None:
        self.connection.execute(
            "INSERT INTO admission_requests "
            "(request_id, source, state, dimensions_json, dependencies_satisfied, lease_id, lease_expires_at, reason, created_at, priority, writer_id, write_targets_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                admission.request_id, admission.source, admission.state,
                json.dumps(admission.dimensions, sort_keys=True, separators=(",", ":")),
                1 if admission.dependencies_satisfied else 0, admission.lease_id,
                admission.lease_expires_at, admission.reason, now, admission.priority,
                admission.writer_id,
                json.dumps(admission.write_targets, separators=(",", ":")) if admission.writer_id else None,
            ),
        )

    def _admission(self, row) -> Admission:
        return Admission(
            str(row["request_id"]), str(row["source"]), str(row["state"]),
            _normalized_dimensions(json.loads(row["dimensions_json"]), label="stored dimensions"),
            bool(row["dependencies_satisfied"]), row["lease_id"], row["lease_expires_at"], row["reason"],
            int(row["priority"]), row["writer_id"], tuple(json.loads(row["write_targets_json"] or "[]")),
        )

    def _usage(self, state: str) -> dict[str, int]:
        total = {name: 0 for name in self.limits.running}
        rows = self.connection.execute(
            "SELECT dimensions_json FROM admission_requests WHERE state = ?", (state,)
        ).fetchall()
        for row in rows:
            for name, units in json.loads(row["dimensions_json"]).items():
                total[name] += int(units)
        return total

    def _fits(self, state: str, dimensions: dict[str, int]) -> bool:
        limit = self.limits.running if state == "running" else self.limits.queued
        usage = self._usage(state)
        return all(usage[name] + units <= limit[name] for name, units in dimensions.items())

    def _has_eligible_queue(self) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM admission_requests WHERE state = 'queued' "
            "AND dependencies_satisfied = 1 LIMIT 1"
        ).fetchone() is not None

    def _preempt_lower_priority(self, dimensions: dict[str, int], priority: int) -> bool:
        """Make room only by rejecting strictly lower-priority queued work.

        The mutation and subsequent capacity check run in the controller's
        existing write transaction. A displaced request expires explicitly, so
        its caller can retry after the competing claim has been released.
        """
        rows = self.connection.execute(
            "SELECT request_id FROM admission_requests WHERE state = 'queued' "
            "AND priority < ? ORDER BY priority ASC, created_at, rowid",
            (priority,),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE admission_requests SET state = 'expired', reason = 'priority_preempted' "
                "WHERE request_id = ? AND state = 'queued'",
                (row["request_id"],),
            )
            if self._fits("queued", dimensions):
                return True
        return self._fits("queued", dimensions)

    def _has_writer_conflict(self, write_targets: tuple[str, ...]) -> bool:
        """Reject overlap, missing, or malformed evidence in active ledger rows.

        A legacy row without writer proof cannot safely coexist with a new
        writer.  Treating it as free would reopen the parallel-writer race this
        ledger exists to close.
        """
        rows = self.connection.execute(
            "SELECT writer_id, write_targets_json FROM admission_requests "
            "WHERE state IN ('queued', 'running')"
        ).fetchall()
        requested = set(write_targets)
        for row in rows:
            if not row["writer_id"] or not row["write_targets_json"]:
                return True
            try:
                existing = _normalized_write_targets(tuple(json.loads(row["write_targets_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                return True
            if requested.intersection(existing):
                return True
        return False

    @staticmethod
    def _has_consistent_worktree_evidence(request: AdmissionRequest) -> bool:
        ancestry = request.ancestry
        worktree = request.worktree
        return (
            ancestry.branch == worktree.branch
            and ancestry.head_sha == worktree.head_sha
            and ancestry.base_sha == worktree.base_sha
            and ancestry.base_sha in ancestry.ancestor_shas
        )

    @staticmethod
    def _rejected(request: AdmissionRequest, code: AdmissionErrorCode) -> Admission:
        return Admission(
            request.request_id, str(request.caller), "rejected", dict(request.aggregate_demand),
            not request.dependencies.unresolved_dependency_ids, None, None, code.value,
            request.priority, request.writer.writer_id, request.writer.write_targets,
        )

    def _expire_leases(self, now: int) -> None:
        self.connection.execute(
            "UPDATE admission_requests SET state = 'expired', lease_id = NULL, lease_expires_at = NULL, reason = 'lease_expired' "
            "WHERE state = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (now,),
        )


def _nonempty(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _positive_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _normalized_dimensions(values: Mapping[str, int], *, label: str) -> dict[str, int]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError(f"{label} must be a non-empty mapping of positive integers")
    normalized: dict[str, int] = {}
    for raw_name, raw_units in values.items():
        name = _nonempty(str(raw_name), f"{label} resource name")
        normalized[name] = _positive_int(raw_units, f"{label} values")
    return normalized


def _priority(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("priority must be an integer")
    return value


def _normalized_write_targets(values: tuple[str, ...] | None) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise ValueError("write_targets must be a non-empty tuple")
    normalized = tuple(_nonempty(value, "write_target") for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("write_targets must not contain duplicates")
    return normalized
