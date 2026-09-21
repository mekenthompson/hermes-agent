"""Live launch adapters for the shared bounded-admission ledger.

The controller is intentionally policy-free.  This module is the only place
where real Kanban/delegation launch paths derive their existing concurrency
limits and verify a writer's worktree evidence before it may start.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.admission import Admission, AdmissionController, AdmissionLimits
from hermes_cli.admission_contract import (
    AdmissionCaller,
    AdmissionErrorCode,
    AdmissionRequest,
    AncestryEvidence,
    DependencyEvidence,
    WorktreeEvidence,
    WriterEvidence,
)
from hermes_constants import get_hermes_home

_LEDGER_NAME = "admission.db"


def ledger_path(home: Path | None = None) -> Path:
    """The one admission ledger for a runtime Hermes home."""
    inherited = os.environ.get("HERMES_ADMISSION_LEDGER", "").strip()
    if inherited:
        candidate = Path(inherited).expanduser()
        if candidate.is_absolute():
            return candidate
    return (home or get_hermes_home()).expanduser() / _LEDGER_NAME


def kanban_request_id(task_id: str, run_id: int) -> str:
    """Stable, attempt-scoped admission identity for one Kanban worker."""
    return f"kanban:{task_id}:run:{int(run_id)}"


def release_kanban_admission(task_id: str, run_id: int | None) -> bool:
    """Release one terminal worker's reservation exactly once, if it has one."""
    if run_id is None:
        return False
    path = ledger_path()
    if not path.exists():
        return False
    with sqlite3.connect(path, isolation_level=None) as connection:
        result = connection.execute(
            "UPDATE admission_requests SET state = 'released', lease_id = NULL, lease_expires_at = NULL "
            "WHERE request_id = ? AND state = 'running'",
            (kanban_request_id(task_id, run_id),),
        )
    return result.rowcount == 1


def renew_kanban_admission(task_id: str, run_id: int | None) -> bool:
    """Renew a live worker using its original durable lease span, never reviving one."""
    if run_id is None:
        return False
    path = ledger_path()
    if not path.exists():
        return False
    now = int(time.time())
    with sqlite3.connect(path, isolation_level=None) as connection:
        row = connection.execute(
            "SELECT created_at, lease_expires_at FROM admission_requests "
            "WHERE request_id = ? AND state = 'running'",
            (kanban_request_id(task_id, run_id),),
        ).fetchone()
        if row is None or row[1] is None or int(row[1]) <= now:
            return False
        lease_span = max(1, int(row[1]) - int(row[0]))
        result = connection.execute(
            "UPDATE admission_requests SET lease_expires_at = ? "
            "WHERE request_id = ? AND state = 'running' AND lease_expires_at > ?",
            (now + lease_span, kanban_request_id(task_id, run_id), now),
        )
    return result.rowcount == 1


def configured_gateway_capacity(*, kanban_cap: int | None = None, delegate_cap: int | None = None) -> int | None:
    """Use only already-existing launch caps, conservatively combined.

    Kanban's established memory-derived cap and delegation's established child
    cap are policy owned by their launchers.  This adapter adds no new numeric
    fallback: if neither launcher can establish a positive cap, admission
    fails closed.
    """
    if kanban_cap is None:
        try:
            from hermes_cli.kanban_db_dispatch import configured_max_in_progress, resolve_max_in_progress
            kanban_cap = resolve_max_in_progress(configured_max_in_progress())
        except Exception:
            kanban_cap = None
    if delegate_cap is None:
        try:
            from tools.delegate_tool_config import _get_max_concurrent_children
            delegate_cap = _get_max_concurrent_children()
        except Exception:
            delegate_cap = None
    caps = [value for value in (kanban_cap, delegate_cap) if isinstance(value, int) and not isinstance(value, bool) and value > 0]
    return min(caps) if caps else None


def _connect(home: Path | None = None) -> sqlite3.Connection:
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=120)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS admission_rejections ("
        "request_id TEXT NOT NULL, caller TEXT NOT NULL, reason TEXT NOT NULL, "
        "detail TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS admission_rejections_request "
        "ON admission_rejections(request_id, created_at)"
    )
    return connection


def _reject(request_id: str, caller: AdmissionCaller, reason: AdmissionErrorCode | str, detail: str, *, home: Path | None = None) -> Admission:
    reason_text = reason.value if isinstance(reason, AdmissionErrorCode) else str(reason)
    connection = _connect(home)
    try:
        connection.execute(
            "INSERT INTO admission_rejections (request_id, caller, reason, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, caller.value, reason_text, detail, int(time.time())),
        )
    finally:
        connection.close()
    return Admission(request_id, caller.value, "rejected", {"gateway": 1}, True, None, None, reason_text)


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], text=True, capture_output=True, timeout=15, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "git verification failed")
    return result.stdout.strip()


def _worktree_request(
    *, request_id: str, caller: AdmissionCaller, workspace: str, priority: int,
    dependency_ids: Iterable[str] = (), unresolved_dependency_ids: Iterable[str] = (),
    writer_id: str,
) -> AdmissionRequest:
    path = Path(workspace).expanduser().resolve()
    git_dir = _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    common_dir = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir == common_dir:
        raise RuntimeError("workspace is not a linked git worktree")
    branch = _git(path, "branch", "--show-current")
    if not branch:
        raise RuntimeError("workspace has no checked-out branch")
    head = _git(path, "rev-parse", "HEAD")
    # A source-writing task must be anchored to the repository's advertised
    # main line.  Falling back to a parent commit would turn arbitrary history
    # into 'proof', so missing remotes fail closed instead.
    base_ref = next((ref for ref in ("upstream/main", "origin/main") if _git_ref_exists(path, ref)), None)
    if base_ref is None:
        raise RuntimeError("workspace has no verified upstream/main or origin/main base")
    base = _git(path, "merge-base", "HEAD", base_ref)
    if not base:
        raise RuntimeError("workspace has no merge-base with its configured main line")
    now = int(time.time())
    dependencies = tuple(str(item) for item in dependency_ids)
    unresolved = tuple(str(item) for item in unresolved_dependency_ids)
    return AdmissionRequest(
        request_id=request_id,
        caller=caller,
        aggregate_demand={"gateway": 1},
        priority=priority,
        dependencies=DependencyEvidence("runtime", request_id, dependencies, unresolved, now),
        ancestry=AncestryEvidence(branch, head, base, (base,), now),
        worktree=WorktreeEvidence(str(path), git_dir, common_dir, branch, head, base, now),
        writer=WriterEvidence(writer_id, (f"repo:{common_dir}:branch:{branch}",)),
    )


def _git_ref_exists(workspace: Path, ref: str) -> bool:
    result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--verify", "--quiet", ref], capture_output=True, timeout=15, check=False)
    return result.returncode == 0


def admit_writer(
    *, request_id: str, caller: AdmissionCaller, workspace: str | None, priority: int,
    writer_id: str, dependencies: Iterable[str] = (), unresolved_dependencies: Iterable[str] = (),
    kanban_cap: int | None = None, delegate_cap: int | None = None,
) -> Admission:
    """Verify a linked worktree then reserve one shared gateway slot.

    Every denial is written to the runtime ledger's rejection journal.  A
    delegate that cannot show this proof is deliberately rejected as an
    unsupported write-capable launch, never relabelled as read-only.
    """
    if not workspace:
        return _reject(request_id, caller, AdmissionErrorCode.UNSUPPORTED_PATH, "missing isolated workspace")
    capacity = configured_gateway_capacity(kanban_cap=kanban_cap, delegate_cap=delegate_cap)
    if capacity is None:
        return _reject(request_id, caller, AdmissionErrorCode.RESOURCE_CAPACITY, "no existing positive launch cap")
    try:
        request = _worktree_request(
            request_id=request_id, caller=caller, workspace=workspace, priority=priority,
            dependency_ids=dependencies, unresolved_dependency_ids=unresolved_dependencies, writer_id=writer_id,
        )
    except Exception as exc:
        code = AdmissionErrorCode.UNSUPPORTED_PATH if caller is AdmissionCaller.DELEGATE else AdmissionErrorCode.UNSAFE_ANCESTRY
        return _reject(request_id, caller, code, str(exc))
    connection = _connect()
    try:
        controller = AdmissionController(connection, AdmissionLimits(running={"gateway": capacity}, queued={"gateway": capacity}))
        admission = controller.admit(request)
        if admission.state == "rejected":
            _record_controller_rejection(connection, admission, caller)
        return admission
    finally:
        connection.close()


def _record_controller_rejection(connection: sqlite3.Connection, admission: Admission, caller: AdmissionCaller) -> None:
    connection.execute(
        "INSERT INTO admission_rejections (request_id, caller, reason, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (admission.request_id, caller.value, admission.reason or "rejected", "controller rejected admission", int(time.time())),
    )


def release_admission(lease_id: str | None) -> None:
    if not lease_id:
        return
    connection = _connect()
    try:
        # A live config change must never prevent cleanup of a lease admitted
        # under the previous established cap.  Re-open with the ledger's stored
        # limits instead of recomputing the current policy.
        row = connection.execute(
            "SELECT limits_json FROM admission_configuration WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return
        limits = json.loads(row["limits_json"])
        AdmissionController(
            connection,
            AdmissionLimits(running=limits["running"], queued=limits["queued"]),
        ).release(lease_id)
    finally:
        connection.close()


def cancel_admission_request(request_id: str | None) -> bool:
    """Cancel an immediate-only pre-spawn request if it was queued.

    Queued requests have no lease, so cleanup uses their durable request ID. A
    missing ledger or an already-terminal request is an idempotent no-op.
    """
    if not request_id:
        return False
    path = ledger_path()
    if not path.exists():
        return False
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT limits_json FROM admission_configuration WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return False
        limits = json.loads(row["limits_json"])
        return AdmissionController(
            connection,
            AdmissionLimits(running=limits["running"], queued=limits["queued"]),
        ).cancel(request_id)
    finally:
        connection.close()
