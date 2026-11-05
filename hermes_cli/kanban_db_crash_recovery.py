"""One recovery respawn for a crashed run that left a PR behind (HF-628).

The ``active_pr`` respawn guard keeps a second worker from re-implementing work a
PR already carries. A crash or interruption (a gateway restart killing the
worker mid-run) is not a handoff, but parking that card as ``needs_input`` turns
a routine restart into a false human blocker. Instead the dispatcher allows ONE
respawn per crashed run and leaves the new worker a note naming the PR, so it
continues that PR rather than opening another. A clean exit, a protocol
violation, a second crash of the same run and anything unreadable keep the guard.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

CRASH_RECOVERY_EVENT = "crash_recovery_respawn"


def _latest_closed_run(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT id, error FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()


def recovery_run_id(conn: sqlite3.Connection, task_id: str, latest_run: Any) -> Optional[int]:
    """Id of the newest closed run when it ended in a genuine crash/interruption
    (``crashed``, not a clean-exit protocol violation) and no recovery respawn
    has been spent on it yet; else ``None``. Fail closed on anything unreadable."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE, WORKER_OUTPUT_SEP
    if latest_run is None or latest_run["outcome"] != "crashed":
        return None
    try:
        if _kb._json_dict(latest_run["metadata"]).get("protocol_violation"):
            return None
        run = _latest_closed_run(conn, task_id)
        if run is None or "protocol violation" in (run["error"] or "").partition(WORKER_OUTPUT_SEP)[0]:
            return None
        run_id = int(run["id"])
        for pr_row in conn.execute("SELECT created_at, body FROM task_comments WHERE task_id = ?", (task_id,)):
            if _RESPAWN_GUARD_PR_URL_RE.search(_kb._lossy_text(pr_row["body"]) or "") \
                    and _kb._to_epoch(pr_row["created_at"]) is None:
                return None  # unreadable PR-comment time: stay failed-closed
        for ev in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                               (task_id, CRASH_RECOVERY_EVENT)):
            if _kb._json_dict(ev["payload"]).get("run_id") == run_id:
                return None  # already spent on this crashed run
        return run_id
    except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError):
        return None


def recovery_pending(conn: sqlite3.Connection, task_id: str, lane: str, guard_reason: Optional[str]) -> bool:
    """True when the guard let a ready card through ONLY because of crash recovery."""
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    return (guard_reason is None and lane == "ready"
            and check_respawn_guard(conn, task_id, lane=lane, allow_crash_recovery=False) == "active_pr")


def record_recovery(conn: sqlite3.Connection, task_id: str) -> None:
    """After a recovery claim: spend the one bypass and leave the worker a note."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE
    run = _latest_closed_run(conn, task_id)
    url = None
    for row in conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (task_id,)):
        m = _RESPAWN_GUARD_PR_URL_RE.search(_kb._lossy_text(row["body"]) or "")
        if m:
            url = m.group(0)
            break
    if run is None or url is None:
        return
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, CRASH_RECOVERY_EVENT, {"run_id": int(run["id"]), "pr_url": url})
    _kb.add_comment(
        conn, task_id, author="dispatcher",
        body=(f"Crash recovery: the previous worker run crashed or was interrupted. An existing PR "
              f"from this task exists: {url}. Continue/supervise that PR to this task's acceptance "
              f"criteria. Do NOT open a new PR."))


def note_recovery(conn: sqlite3.Connection, task_id: str, recovery: bool) -> None:
    """Record a claimed recovery; a failed note must not strand the claimed task."""
    if not recovery:
        return
    from hermes_cli import kanban_db as _kb
    try:
        record_recovery(conn, task_id)
    except (sqlite3.Error, OSError, ValueError):
        _kb._log.warning("kanban dispatcher: crash-recovery note for %s failed", task_id, exc_info=True)
