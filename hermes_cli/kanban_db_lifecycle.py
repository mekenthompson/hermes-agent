"""Native block/resume mutations and durable request receipts in task_events.

Request identity and native event fences order retries; neither grants permission
or establishes the caller's identity. The caller still uses native authorization.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Optional

# These events change status, executor generation or ownership. Comments,
# heartbeats, checkpoints and artifacts do not revoke a pending instruction.
_AUTHORITY_EVENTS = (
    "created", "block_requested", "blocked", "unblocked", "block_loop_detected",
    "dependency_wait", "promoted", "claimed", "spawned", "worker_registered",
    "reclaimed", "stale", "crashed", "timed_out", "reconciled", "gave_up",
    "spawn_failed", "rate_limited", "protocol_violation",
    "completed", "archived", "scheduled", "assigned", "review_requested",
    "changes_requested", "review_reopened", "promoted_manual", "specified",
    "descendant_invalidated", "status", "linked", "unlinked", "decomposed",
    "edited", "imported", "model_override_set", "reasoning_effort_set",
)


def _validate_key(value, name):
    if value is not None and (
        not isinstance(value, str) or not value.strip() or len(value) > 512
    ):
        raise ValueError(f"{name} must be a nonempty string of at most 512 characters")


def _request_event(conn, task_id, idempotency_key):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='lifecycle_request' "
        "AND json_extract(payload, '$.idempotency_key')=? ORDER BY id LIMIT 1",
        (task_id, idempotency_key),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def request_received(conn, task_id, operation, idempotency_key):
    """Read a request's committed bool outcome without inferring it from task state."""
    _validate_key(idempotency_key, "idempotency_key")
    saved = _request_event(conn, task_id, idempotency_key)
    if saved is None:
        return None
    if saved["operation"] != operation:
        raise ValueError("lifecycle idempotency_key conflicts with its original operation")
    return bool(saved["result"])


def prune_events(conn, cutoff):
    """Keep request receipts and each task's latest control fence until deletion."""
    placeholders = ",".join("?" for _ in _AUTHORITY_EVENTS)
    cur = conn.execute(
        "DELETE FROM task_events WHERE created_at < ? AND kind NOT IN "
        "('decomposed','owner_admitted','native_owner_stop') "
        "AND id NOT IN (SELECT b.id FROM task_events b JOIN task_events o "
        "ON b.task_id=o.task_id AND b.run_id=o.run_id AND o.kind='owner_admitted' "
        "WHERE b.kind IN ('blocked','block_loop_detected','dependency_wait')) "
        "AND json_extract(payload, '$.idempotency_key') IS NULL "
        "AND task_id IN (SELECT id FROM tasks WHERE status IN ('done', 'archived')) "
        "AND id NOT IN (SELECT MAX(id) FROM task_events "
        f"WHERE kind IN ({placeholders}) GROUP BY task_id)",
        (cutoff, *_AUTHORITY_EVENTS),
    )
    return int(cur.rowcount or 0)


class _Request:
    """Used only while the owning lifecycle write transaction is held."""

    def __init__(self, conn, task_id, operation, key, *, expected_event_id=None,
                 origin_comment_key=None, **parameters):
        _validate_key(key, "idempotency_key")
        _validate_key(origin_comment_key, "origin_comment_key")
        if expected_event_id is not None and (
            type(expected_event_id) is not int or expected_event_id < 1
        ):
            raise ValueError("expected_event_id must be a positive native event ID")
        self.conn, self.task_id, self.operation, self.key = conn, task_id, operation, key
        self.expected_event_id, self.origin_comment_key = expected_event_id, origin_comment_key
        self.fingerprint = hashlib.sha256(json.dumps(
            [operation, expected_event_id, origin_comment_key, parameters], sort_keys=True,
        ).encode()).hexdigest()

    def replay(self):
        saved = _request_event(self.conn, self.task_id, self.key) if self.key is not None else None
        if saved is None:
            return None
        if saved["request_sha256"] != self.fingerprint:
            raise ValueError("lifecycle idempotency_key conflicts with its original request")
        return bool(saved["result"])

    def stale(self):
        anchors = []
        if self.expected_event_id is not None:
            row = self.conn.execute("SELECT id FROM task_events WHERE task_id=? AND id=?",
                                    (self.task_id, self.expected_event_id)).fetchone()
            if row is None:
                return "missing_native_event"
            anchors.append(row["id"])
        if self.origin_comment_key is not None:
            row = self.conn.execute(
                "SELECT id FROM task_events WHERE task_id=? AND kind='commented' "
                "AND json_extract(payload, '$.idempotency_key')=? ORDER BY id LIMIT 1",
                (self.task_id, self.origin_comment_key),
            ).fetchone()
            if row is None:
                return "missing_origin_comment"
            anchors.append(row["id"])
        if not anchors:
            return None
        placeholders = ",".join("?" for _ in _AUTHORITY_EVENTS)
        row = self.conn.execute(
            f"SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN ({placeholders}) LIMIT 1",
            (self.task_id, min(anchors), *_AUTHORITY_EVENTS),
        ).fetchone()
        return "stale_native_event" if row else None

    def finish(self, result, *, refusal=None):
        from hermes_cli import kanban_db as _kb

        if self.key is not None:
            payload = {"operation": self.operation, "idempotency_key": self.key,
                       "request_sha256": self.fingerprint, "result": result}
            if refusal is not None:
                payload["refusal"] = refusal
            _kb._append_event(self.conn, self.task_id, "lifecycle_request", payload)
        return result


def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None, expected_event_id: Optional[int] = None,
) -> bool:
    """``running``/``ready`` -> ``blocked`` (or ``todo`` / ``triage``, see
    :func:`_route_block`). ``kind='dependency'`` with no incomplete parent is
    re-kinded to ``needs_input`` (sticky) so ``recompute_ready`` cannot
    promote it into a context-free respawn. ``transient`` still counts
    toward the loop breaker so a forever-flaky task escalates. True on any
    transition.

    An already-``blocked`` card that the failure breaker parked UNTYPED
    (``block_kind IS NULL``, no live run) is classified in place when *kind*
    is supplied: ``block_kind``/``block_recurrences`` are set and a ``blocked``
    audit event is appended, while status, failure evidence and the terminal
    runs stay exactly as the breaker left them. A typed block, a card with a
    live run, or a kind-less call on a blocked card are still refused.

    ``expected_run_id`` is the worker's proof that it is blocking its own run.
    A block without it comes from a supervisor (dashboard, CLI, an integration
    such as a Linear Stop), so the run's live, fingerprint-verified host-local
    worker is ended once the txn commits and the hook has fired
    (:func:`halt_blocked_worker`, which never raises) instead of running on
    until the reaper's grace expires.
    """
    from hermes_cli import kanban_db as _kb

    if kind is not None and kind not in _kb.VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(_kb.VALID_BLOCK_KINDS)} or None")
    request = _Request(conn, task_id, "block", idempotency_key, expected_event_id=expected_event_id,
                       reason=reason, kind=kind, expected_run_id=expected_run_id)
    with _kb.write_txn(conn):
        replay = request.replay()
        if replay is not None:
            return replay
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        refusal = request.stale()
        if refusal:
            return request.finish(False, refusal=refusal)
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready, so that policy could never be attached later
        # (#117363). Classify in place; never re-type or flap status. A caller
        # asserting run ownership (``expected_run_id``) cannot own a parked
        # card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _kb._row_get(cur_row, "block_kind") is not None:
                # A supervisor's explicit Stop still supersedes a previously
                # saved resume instruction when the card was already blocked.
                if expected_run_id is None:
                    _kb._append_event(conn, task_id, "block_requested", {"kind": kind, "reason": reason})
                return request.finish(False)
            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1 "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, task_id),
            ).rowcount
            if classified != 1:
                if expected_run_id is None:
                    _kb._append_event(conn, task_id, "block_requested", {"kind": kind, "reason": reason})
                return request.finish(False)
            _kb._append_event(conn, task_id, "blocked", {
                "kind": kind, "reason": reason, "classified_in_place": True,
            })
            return request.finish(True)
        source_status = _kb._retry_status_for_run(conn, task_id) if cur_row["status"] == "running" else "ready"
        requested_kind = kind
        rekind_reason = None
        # ``dependency`` only waits on incomplete parents. A worker filing that
        # kind with none open would park in ``todo`` and ``recompute_ready``
        # would promote+respawn it context-free on the next tick. Re-kind to
        # ``needs_input`` so it is sticky until a human unblocks.
        if kind == "dependency" and _kb._parents_satisfied(conn, task_id):
            kind = "needs_input"
            rekind_reason = "no_open_parent"
        new_status, event_kind, set_sql, params, payload = _kb._route_block(
            kind, reason, source_status, prev_kind=_kb._row_get(cur_row, "block_kind"),
            prev_recurrences=int(_kb._row_get(cur_row, "block_recurrences") or 0),
        )
        payload['supervisor_stop'] = expected_run_id is None
        if rekind_reason:
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
        sql = f"""
                UPDATE tasks
                   SET status        = '{new_status}',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       {set_sql}
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """
        params = (*params, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return request.finish(False)
        run_id = _kb._end_or_synthesize_run(
            conn, task_id, outcome="blocked", status="blocked", summary=reason, synthesize=bool(reason),
        )
        _kb._append_event(conn, task_id, event_kind, payload, run_id=run_id)
        request.finish(True)
        blocked_task = _kb.get_task(conn, task_id)
        dependency_block = kind == "dependency"
        if dependency_block:
            # Historical ordering: the dependency lane fires inside the txn.
            _kb._fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    _kb._release_kanban_admission(task_id, run_id)
    if not dependency_block:
        _kb._fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    if expected_run_id is None:  # filed by a supervisor, not the worker: halt it now, no reap grace
        _kb.halt_blocked_worker(conn, run_id)
    return True
def unblock_task(
    conn: sqlite3.Connection, task_id: str, *, idempotency_key: Optional[str] = None,
    origin_comment_key: Optional[str] = None, expected_event_id: Optional[int] = None,
) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first."""
    from hermes_cli import kanban_db as _kb

    now = int(time.time())
    request = _Request(conn, task_id, "unblock", idempotency_key,
                       expected_event_id=expected_event_id, origin_comment_key=origin_comment_key)
    with _kb.write_txn(conn):
        replay = request.replay()
        if replay is not None:
            return replay
        if _kb._task_status(conn, task_id) is None:
            return False
        from hermes_cli.kanban_owner_recovery import owner_stop_unresolved
        if owner_stop_unresolved(conn, task_id):
            return request.finish(False, refusal='native_stop_unknown')
        refusal = request.stale()
        if refusal:
            return request.finish(False, refusal=refusal)
        resume_status = (
            _kb._resume_status_from_events(conn, task_id)
            if _kb._task_status(conn, task_id) == "blocked"
            else "ready"
        )
        _kb._reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled"), now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _kb._landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
        # is a fresh start for the retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')", (new_status, task_id),
        )
        if cur.rowcount != 1:
            return request.finish(False)
        _kb._append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
        return request.finish(True)

